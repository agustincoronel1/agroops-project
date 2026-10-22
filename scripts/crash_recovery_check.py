"""Real crash tests against the Docker Compose stack (SIGKILL on the backend container).

Scenarios:
  A. A job is RUNNING when the backend dies -> after restart it is FAILED with a public
     INTERRUPTED message, it is NOT re-executed, and no other job is lost.
  B. Jobs waiting in the Redis queue survive the crash and finish DONE exactly once.
  C. Crash right after a HITL approval registered the incident (LLM summary still pending)
     -> after restart the job is DONE with APPLIED and the incident exists ONCE.

Uses the real OpenAI API (cheap cases only). Read-only on LangSmith. Results go to
docs/evidencia/robustez/crash_recovery.json. Secrets are read from .env and never printed.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "evidencia" / "robustez" / "crash_recovery.json"
BASE = "http://localhost:8000"
APPROVAL_KEY = dotenv_values(REPO / ".env").get("APPROVAL_API_KEY") or ""
QUEUE = "agroops:jobs:queue"

client = httpx.Client(base_url=BASE, timeout=30, limits=httpx.Limits(max_keepalive_connections=0))
RESULTS: list[dict] = []
JOBS: dict[str, list[str]] = {}


def compose(*args: str) -> str:
    return subprocess.run(["docker", "compose", *args], cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout.strip()


def redis(*args: str) -> str:
    return compose("exec", "-T", "redis", "redis-cli", *args)


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})
    print(f"{'PASS' if ok else 'FAIL'}  {name}  -> {str(detail)[:120]}", flush=True)


def remember(scenario: str, *job_ids: str) -> None:
    JOBS.setdefault(scenario, []).extend(job_ids)


def job(job_id: str) -> dict:
    return client.get(f"/tasks/{job_id}").json()


def submit(text: str) -> str:
    r = client.post("/tasks", json={"user_input": text, "establishment_id": "demo"})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def wait(predicate, timeout: float = 60, step: float = 0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            value = predicate()
        except httpx.HTTPError:
            value = None
        if value:
            return value
        time.sleep(step)
    return None


def backend_ready() -> bool:
    try:
        return client.get("/health").status_code == 200
    except httpx.HTTPError:
        return False


def kill_backend() -> None:
    compose("kill", "-s", "SIGKILL", "backend")


def restart_backend() -> None:
    compose("up", "-d", "backend")
    if not wait(backend_ready, timeout=120, step=1):
        raise RuntimeError("backend did not become healthy after restart")


def scenario_a_running_job_interrupted() -> None:
    print("\n== A: crash while a job is RUNNING ==", flush=True)
    jid = submit("¿Qué maquinaria está fuera de servicio?")
    remember("A", jid)
    running = wait(lambda: job(jid).get("status") == "RUNNING", timeout=30, step=0.05)
    check("A: job alcanza RUNNING antes del crash", bool(running))
    kill_backend()
    restart_backend()
    after = job(jid)
    check("A: el job no se pierde (sigue existiendo)", after.get("status") is not None,
          after.get("status"))
    check("A: queda FAILED con mensaje público INTERRUPTED (sin detalle interno)",
          after.get("status") == "FAILED" and (after.get("error") or "").startswith("INTERRUPTED:"),
          after.get("error"))
    check("A: no se re-ejecuta (el estado no vuelve a RUNNING/DONE)",
          job(jid).get("status") == "FAILED")


def scenario_b_queued_jobs_survive() -> None:
    print("\n== B: queued jobs survive the crash ==", flush=True)
    ids = [
        submit("¿Qué maquinaria está fuera de servicio?"),
        submit("¿Cuál es la receta de la torta de chocolate?"),
        submit("¿Qué tareas están pendientes?"),
    ]
    wait(lambda: job(ids[0]).get("status") == "RUNNING", timeout=30, step=0.05)
    kill_backend()
    restart_backend()
    remember("B", *ids)
    first, *queued = ids
    check("B: el job en curso queda FAILED (INTERRUPTED)",
          job(first).get("status") == "FAILED", job(first).get("status"))
    for j in queued:
        wait(lambda j=j: job(j).get("status") in {"DONE", "FAILED"}, timeout=240, step=1)
    statuses = [job(j).get("status") for j in queued]
    check("B: los jobs encolados terminan DONE (no se pierden)",
          statuses == ["DONE", "DONE"], statuses)
    check("B: la cola de Redis quedó vacía (cada job se procesó una vez)",
          redis("LLEN", QUEUE) == "0", redis("LLEN", QUEUE))
    check("B: ningún job queda en RUNNING", all(job(j).get("status") != "RUNNING" for j in ids))


def scenario_c_crash_after_approved_action() -> None:
    print("\n== C: crash after the approved incident was written ==", flush=True)
    jid = submit("Registrá una incidencia: la cosechadora pierde presión hidráulica")
    remember("C", jid)
    waiting = wait(lambda: job(jid).get("status") == "WAITING_APPROVAL", timeout=60, step=0.3)
    check("C: job llega a WAITING_APPROVAL", bool(waiting))
    resp = client.post(f"/tasks/{jid}/approve", json={"approve": True, "reason": "crash test"},
                       headers={"X-API-Key": APPROVAL_KEY})
    check("C: aprobación aceptada", resp.status_code == 200, resp.status_code)
    key = f"agroops:incident:{jid}-incident"
    registered = wait(lambda: redis("EXISTS", key) == "1", timeout=30, step=0.02)
    check("C: incidencia escrita en Redis antes de matar el backend", bool(registered))
    kill_backend()
    restart_backend()
    done = wait(lambda: job(jid).get("status") == "DONE", timeout=120, step=0.5)
    res = job(jid).get("result") or {}
    check("C: tras el reinicio el job termina DONE", bool(done), job(jid).get("status"))
    check("C: action_status APPLIED", res.get("action_status") == "APPLIED", res.get("action_status"))
    incidents = redis("--scan", "--pattern", f"agroops:incident:{jid}*").splitlines()
    check("C: la incidencia existe exactamente una vez (sin duplicar la acción)",
          incidents == [key], incidents)


def trace_summary() -> dict:
    """Root traces per crash job (read-only). Shows how many runs each job produced."""
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("LANGSMITH_") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        from langsmith_api import fetch_job_traces
    except ImportError as exc:  # pragma: no cover - environment problem
        return {"error": type(exc).__name__}
    out: dict = {}
    for scenario, ids in JOBS.items():
        for jid in ids:
            try:
                found = fetch_job_traces(jid, "agroops-intelligence")
                out[jid] = {"scenario": scenario, "roots": len(found),
                            "root_status": [t["status"] for t in found],
                            "job_status_meta": [t["job_status_meta"] for t in found]}
            except Exception as exc:  # noqa: BLE001 - report, do not hide
                out[jid] = {"scenario": scenario, "error": type(exc).__name__}
    return out


def traces_only() -> int:
    """Recount LangSmith traces for the jobs saved by the last run (no new LLM calls)."""
    saved = json.loads(OUT.read_text(encoding="utf-8"))
    JOBS.update(saved["jobs"])
    saved["traces"] = trace_summary()
    OUT.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(saved["traces"], ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    if "--traces-only" in sys.argv:
        return traces_only()
    if not wait(backend_ready, timeout=30, step=1):
        print("backend is not running; start it with docker compose up -d", file=sys.stderr)
        return 1
    for scenario in (scenario_a_running_job_interrupted, scenario_b_queued_jobs_survive,
                     scenario_c_crash_after_approved_action):
        try:
            scenario()
        except Exception as exc:  # noqa: BLE001 - record and continue
            check(f"{scenario.__name__} sin excepciones", False, repr(exc))
    time.sleep(10)  # LangSmith flushes traces asynchronously
    traces = trace_summary()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"checks": RESULTS, "jobs": JOBS, "traces": traces},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(r["ok"] for r in RESULTS)
    print(f"\nChecks: {ok}/{len(RESULTS)} PASS. Results: {OUT}")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
