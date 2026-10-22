"""Load test for POST /tasks with JSON export (reproducible).

Two modes:

  # Sends REAL jobs (OpenAI cost). Refused unless --confirm-openai is given.
  python scripts/load_test_tasks.py --confirm-openai --out load_test_results.json

  # No OpenAI, no POST: rebuilds the same JSON from jobs that already ran.
  python scripts/load_test_tasks.py --collect-only --out load_test_results.json \\
      --job A_operations=<job_id> --job B_knowledge=<job_id> ...

Timing definitions (see README "Latency: HTTP, processing and queue wait"):
  post_latency_s   client-side time of POST /tasks (HTTP acceptance only)
  queue_wait_s     first LangSmith root start - API created_at (worker was busy)
  processing_s     sum of LangSmith root run latencies of the job
  total_s          API updated_at (terminal) - API created_at

Values LangSmith does not return are None and are listed under "not_verified".
The API key of LangSmith and the approval key are read from .env and never written out.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "agroops-intelligence"
SCHEMA = "agroops-load-test/1"
TERMINAL = {"DONE", "FAILED"}

# Fixed query set: one case per agent path. "E_hitl" is approved automatically once it waits.
QUERIES: dict[str, str] = {
    "A_operations": "¿Qué tareas están pendientes y qué maquinaria está en mantenimiento o rota en el establecimiento?",
    "B_knowledge": "¿Qué debo hacer al terminar una aplicación de agroquímicos según el procedimiento?",
    "C_supervisor_both": "Considerando las tareas pendientes del establecimiento y el procedimiento documentado, ¿qué debo priorizar esta semana?",
    "D_no_info": "¿Cuál es la dosis recomendada de vacuna contra la fiebre aftosa para terneros Angus?",
    "E_hitl": "Registrá una incidencia de maquinaria: la cosechadora perdió presión hidráulica, severidad alta.",
}


# --- pure metric helpers (unit-tested) ------------------------------------------------------


def percentile_nearest_rank(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the ceil(p/100 * n)-th smallest value. n=5 -> p95 is the max."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[k - 1]


def percentile_linear(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (NumPy default). Differs from nearest-rank for n<20."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def summarize(values: list[float | None]) -> dict[str, Any]:
    present = [v for v in values if v is not None]
    missing = len(values) - len(present)
    if not present:
        return {"n": 0, "missing": missing, "p50": None, "p95_nearest_rank": None,
                "p95_linear": None, "mean": None, "max": None}
    return {
        "n": len(present),
        "missing": missing,
        "p50": percentile_nearest_rank(present, 50),
        "p95_nearest_rank": percentile_nearest_rank(present, 95),
        "p95_linear": percentile_linear(present, 95),
        "mean": sum(present) / len(present),
        "max": max(present),
    }


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def job_metrics(api: dict[str, Any], traces: list[dict[str, Any]], post_latency_s: float | None) -> dict[str, Any]:
    """Derive queue wait, processing and total time from the API state and the LangSmith roots."""
    created = _ts(api.get("created_at"))
    updated = _ts(api.get("updated_at"))
    starts = [_ts(t.get("start")) for t in traces if t.get("start")]
    queue = (min(starts) - created).total_seconds() if starts and created else None
    processing = (
        sum(t["latency_s"] for t in traces) if traces and all(t.get("latency_s") is not None for t in traces) else None
    )
    total = (updated - created).total_seconds() if created and updated else None
    tokens = [t.get("tokens_total") for t in traces]
    costs = [t.get("cost_total") for t in traces]
    return {
        "post_latency_s": post_latency_s,
        "queue_wait_s": queue,
        "processing_s": processing,
        "total_s": total,
        "tokens_in": _sum_known([t.get("tokens_in") for t in traces]),
        "tokens_out": _sum_known([t.get("tokens_out") for t in traces]),
        "tokens_total": _sum_known(tokens),
        "cost_usd_known_parts": _sum_known(costs),
        # The root total excludes runs LangSmith cannot price (e.g. embeddings): any such llm run
        # makes the job's cost incomplete, so it is reported as NO VERIFICADO rather than summed quietly.
        "cost_complete": bool(traces)
        and all(c is not None for c in costs)
        and all(n["cost_known"] for t in traces for n in (t.get("nodes") or {}).values() if n["run_type"] == "llm"),
    }


def _sum_known(values: list[Any]) -> Any:
    if not values or any(v is None for v in values):
        return None
    return sum(values)


def not_verified(job: dict[str, Any]) -> list[str]:
    gaps = []
    m = job["metrics"]
    if job.get("post_http") is None:
        gaps.append("post_http (HTTP status of POST not captured)")
    if m["post_latency_s"] is None:
        gaps.append("post_latency_s (timing not captured)")
    result = job["api"].get("result") or {}
    if result.get("action") and job.get("approve_http") is None:
        gaps.append("approve_http (approval HTTP status not captured; effect verified in API)")
    if m["processing_s"] is None:
        gaps.append("processing_s (no LangSmith root)")
    if m["tokens_total"] is None:
        gaps.append("tokens_total")
    if not m["cost_complete"]:
        gaps.append("cost_usd (some run has no LangSmith price: embeddings)")
    return gaps


# --- API ---------------------------------------------------------------------------------------


def _call(api: str, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        api + path, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _env_value(name: str) -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip() or None
    return None


def post_task(api: str, name: str, query: str) -> dict[str, Any]:
    started = time.time()
    code, body = _call(api, "POST", "/tasks", {"user_input": query, "establishment_id": "demo"})
    return {"name": name, "post_http": code, "job_id": body.get("job_id"),
            "post_status": body.get("status"), "post_latency_s": time.time() - started}


def run_load(api: str, approval_key: str | None, timeout_s: int) -> list[dict[str, Any]]:
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=len(QUERIES)) as ex:
        posted = list(ex.map(lambda kv: post_task(api, *kv), QUERIES.items()))
    rejected = [p["name"] for p in posted if p["post_http"] != 202 or not p["job_id"]]
    if rejected:
        raise SystemExit(f"POST /tasks not accepted for: {rejected}")
    return _poll(api, posted, approval_key, timeout_s, t0)


def _poll(api: str, posted: list[dict[str, Any]], approval_key: str | None, timeout_s: int, t0: float) -> list[dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {p["job_id"]: {"approve_http": None, "last": {}} for p in posted}
    deadline = time.time() + timeout_s
    while time.time() < deadline and any(p["job_id"] not in _done_ids(state) for p in posted):
        for job_id in list(state):
            if job_id in _done_ids(state):
                continue
            _, st = _call(api, "GET", f"/tasks/{job_id}")
            state[job_id]["last"] = st
            if st.get("status") == "WAITING_APPROVAL" and state[job_id]["approve_http"] is None:
                if not approval_key:
                    raise SystemExit("APPROVAL_API_KEY missing in .env: cannot approve the HITL case")
                code, _ = _call(api, "POST", f"/tasks/{job_id}/approve",
                                {"approve": True, "reason": "load test approval"},
                                {"X-API-Key": approval_key})
                state[job_id]["approve_http"] = code
        time.sleep(1)
    return [{**p, "approve_http": state[p["job_id"]]["approve_http"], "api": state[p["job_id"]]["last"]} for p in posted]


def _done_ids(state: dict[str, dict[str, Any]]) -> set[str]:
    return {j for j, s in state.items() if s["last"].get("status") in TERMINAL}


def fetch_state(api: str, job_id: str) -> dict[str, Any]:
    _, st = _call(api, "GET", f"/tasks/{job_id}")
    return st


# --- LangSmith -----------------------------------------------------------------------------


def collect_traces(job_id: str) -> tuple[list[dict[str, Any]], str | None]:
    """Root runs for a job with real LangSmith metrics. Returns (traces, error_or_None)."""
    key = _env_value("LANGSMITH_API_KEY")
    if not key:
        return [], "LANGSMITH_API_KEY not set in .env"
    os.environ["LANGSMITH_API_KEY"] = key
    try:
        from langsmith_api import fetch_job_traces
    except ImportError as exc:  # pragma: no cover - environment problem
        return [], f"langsmith not importable ({type(exc).__name__})"
    project = _env_value("LANGSMITH_PROJECT") or PROJECT
    out = []
    for t in fetch_job_traces(job_id, project):
        out.append({
            "trace_id": t["trace_id"],
            "url": t["url"],
            "start": t["start"],
            "latency_s": t["latency_s"],
            "status": t["status"],
            "job_status_meta": t["job_status_meta"],
            "resumed": t["resumed"],
            "outputs": t["outputs"],
            "tokens_in": t["tokens_in"],
            "tokens_out": t["tokens_out"],
            "tokens_total": t["tokens_total"],
            "cost_total": None if t["cost_total"] is None else float(t["cost_total"]),
            "nodes": _node_aggregate(t["node"]),
        })
    return out, None


def _node_aggregate(run: Any) -> dict[str, dict[str, Any]]:
    acc: dict[str, dict[str, Any]] = {}

    def walk(parent: Any) -> None:
        for ch in sorted(parent.child_runs or [], key=lambda x: x.start_time):
            lat = (ch.end_time - ch.start_time).total_seconds() if ch.start_time and ch.end_time else 0.0
            a = acc.setdefault(ch.name, {"run_type": ch.run_type, "n": 0, "latency_s": 0.0,
                                         "tokens": 0, "tokens_known": True, "cost": 0.0, "cost_known": True})
            a["n"] += 1
            a["latency_s"] += lat
            if ch.total_tokens is None:
                a["tokens_known"] = False
            else:
                a["tokens"] += ch.total_tokens
            if ch.total_cost is None:
                a["cost_known"] = False
            else:
                a["cost"] += float(ch.total_cost)
            walk(ch)

    walk(run)
    return acc


# --- assembly ------------------------------------------------------------------------------


def build_job(name: str, job_id: str, post: dict[str, Any] | None, api_state: dict[str, Any]) -> dict[str, Any]:
    traces, lerr = collect_traces(job_id)
    post_latency = post["post_latency_s"] if post else None
    job = {
        "name": name,
        "job_id": job_id,
        "post_http": post.get("post_http") if post else None,
        "post_status": post.get("post_status") if post else None,
        "approve_http": post.get("approve_http") if post else None,
        "api": {k: api_state.get(k) for k in ("status", "created_at", "updated_at", "result", "error")},
        "traces": traces,
        "langsmith_error": lerr,
    }
    job["metrics"] = job_metrics(job["api"], traces, post_latency)
    job["not_verified"] = not_verified(job) + ([f"langsmith: {lerr}"] if lerr else [])
    return job


def build_summary(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    def col(key: str) -> list[float | None]:
        return [j["metrics"][key] for j in jobs]

    return {
        "post_latency_s": summarize(col("post_latency_s")),
        "queue_wait_s": summarize(col("queue_wait_s")),
        "processing_s": summarize(col("processing_s")),
        "total_s": summarize(col("total_s")),
        "tokens_total_sum": _sum_known(col("tokens_total")),
        "cost_usd_sum_known": _sum_known([j["metrics"]["cost_usd_known_parts"] for j in jobs]),
        "jobs_done": sum(1 for j in jobs if j["api"].get("status") == "DONE"),
        "jobs": len(jobs),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--confirm-openai", action="store_true", help="required to POST real jobs")
    ap.add_argument("--collect-only", action="store_true", help="no POST: rebuild from existing jobs")
    ap.add_argument("--job", action="append", default=[], metavar="NAME=JOB_ID[:POST_LATENCY_S]",
                    help="for --collect-only; POST_LATENCY_S optional, copied from the original run's log")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    if args.collect_only:
        pairs = []
        for item in args.job:
            name, _, rest = item.partition("=")
            job_id, _, latency = rest.partition(":")
            if not name or not job_id:
                raise SystemExit(f"bad --job {item!r}, expected NAME=JOB_ID[:POST_LATENCY_S]")
            post = {"post_latency_s": float(latency)} if latency else None
            pairs.append((name, job_id, post))
        if not pairs:
            raise SystemExit("--collect-only needs at least one --job NAME=JOB_ID")
        mode = "collect-only (no POST, no OpenAI)"
        results = [(n, j, p, fetch_state(args.api, j)) for n, j, p in pairs]
    else:
        if not args.confirm_openai:
            raise SystemExit("Refused: this sends real jobs with OpenAI cost. Add --confirm-openai.")
        mode = "live (POST /tasks, real OpenAI)"
        posted = run_load(args.api, _env_value("APPROVAL_API_KEY"), args.timeout)
        results = [(p["name"], p["job_id"], p, p["api"] or {}) for p in posted]

    jobs = [build_job(n, j, p, st) for n, j, p, st in results]
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "queries": QUERIES,
        "methodology": {
            "percentiles": "n = number of jobs. p95_nearest_rank = ceil(0.95*n)-th value; p95_linear = NumPy linear interpolation. Both shown: they differ for n < 20.",
            "post_latency_s": "client-side time of POST /tasks",
            "queue_wait_s": "LangSmith first root start - API created_at",
            "processing_s": "sum of LangSmith root run latencies",
            "total_s": "API updated_at - API created_at (includes approval wait for HITL jobs)",
            "cost": "LangSmith computed cost; embeddings have no price in LangSmith, so root cost excludes them (cost_complete=false)",
        },
        "jobs": jobs,
        "summary": build_summary(jobs),
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.out} ({len(jobs)} jobs, mode: {mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
