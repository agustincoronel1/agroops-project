"""Reproducible live checks against a running stack (docker compose up).

Usage: .venv/Scripts/python scripts/audit_api.py <check> [args]
Checks: errors | job <text> [establishment_id] | concurrent <n> | poll <job_id>
No secrets are read or printed.
"""

import asyncio
import json
import sys
import time

import httpx

BASE = "http://localhost:8000"


async def post_task(c: httpx.AsyncClient, text: str, est: str | None = None) -> tuple[httpx.Response, float]:
    body = {"user_input": text}
    if est:
        body["establishment_id"] = est
    t = time.perf_counter()
    r = await c.post(f"{BASE}/tasks", json=body)
    return r, time.perf_counter() - t


async def poll(c: httpx.AsyncClient, job_id: str, timeout: float = 90) -> dict:
    """Poll fast and record every distinct status with its time offset."""
    t0 = time.perf_counter()
    seen: list[tuple[str, float]] = []
    while time.perf_counter() - t0 < timeout:
        d = (await c.get(f"{BASE}/tasks/{job_id}")).json()
        if not seen or seen[-1][0] != d["status"]:
            seen.append((d["status"], round(time.perf_counter() - t0, 2)))
        if d["status"] in ("DONE", "FAILED"):
            d["_transitions"] = seen
            d["_total_s"] = round(time.perf_counter() - t0, 2)
            return d
        await asyncio.sleep(0.1)
    return {"status": "TIMEOUT", "_transitions": seen}


async def errors() -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        cases = [
            ("POST empty user_input", "post", "/tasks", {"user_input": ""}),
            ("POST missing user_input", "post", "/tasks", {}),
            ("POST bad establishment_id", "post", "/tasks", {"user_input": "x", "establishment_id": "../etc"}),
            ("POST too long input", "post", "/tasks", {"user_input": "a" * 5001}),
            ("POST invalid JSON type", "post", "/tasks", ["x"]),
            ("GET unknown job", "get", "/tasks/doesnotexist", None),
            ("GET unknown route", "get", "/nope", None),
        ]
        for name, m, path, body in cases:
            r = await (c.post(BASE + path, json=body) if m == "post" else c.get(BASE + path))
            print(f"{name}: {r.status_code} {r.text[:160]}")


async def job(text: str, est: str | None) -> None:
    async with httpx.AsyncClient(timeout=30) as c:
        r, lat = await post_task(c, text, est)
        print("POST", r.status_code, f"{lat*1000:.0f}ms", r.json())
        d = await poll(c, r.json()["job_id"])
        print(json.dumps(d, ensure_ascii=False, indent=2))


async def concurrent(n: int) -> None:
    async with httpx.AsyncClient(timeout=60) as c:
        t0 = time.perf_counter()
        res = await asyncio.gather(*[post_task(c, f"concurrent probe {i}") for i in range(n)])
        wall = time.perf_counter() - t0
        lats = [lat * 1000 for _, lat in res]
        ids = [r.json().get("job_id") for r, _ in res]
        print("statuses", [r.status_code for r, _ in res])
        print("POST latency ms", [round(x) for x in lats], "wall", f"{wall*1000:.0f}ms")
        print("unique job_ids", len(set(ids)), "of", n)
        done = await asyncio.gather(*[poll(c, i) for i in ids])
        print("final", [(d["status"], d.get("_total_s")) for d in done])
        print("errors", {d.get("error") for d in done})


async def main() -> None:
    cmd = sys.argv[1]
    if cmd == "errors":
        await errors()
    elif cmd == "job":
        await job(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "concurrent":
        await concurrent(int(sys.argv[2]))
    elif cmd == "poll":
        async with httpx.AsyncClient() as c:
            print(json.dumps(await poll(c, sys.argv[2]), ensure_ascii=False, indent=2))


asyncio.run(main())
