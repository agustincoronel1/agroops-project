"""Real end-to-end cases against the Docker Compose stack with REAL OpenAI calls.

Usage: python scripts/real_cases_check.py [--only CASE1,CASE2] [--out results.json]
Results (no secrets) go to docs/evidencia/casos_reales/ unless --out is given.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
from app.agents.operations_data import get_machinery, get_tasks  # noqa: E402

OUT = REPO / "docs" / "evidencia" / "casos_reales" / "corrida_nueva.json"
BASE = "http://localhost:8000"
APPROVAL_KEY = dotenv_values(REPO / ".env").get("APPROVAL_API_KEY") or ""
client = httpx.Client(base_url=BASE, timeout=60, limits=httpx.Limits(max_keepalive_connections=0))
RESULTS: list[dict] = []


def check(results: dict, name: str, ok: bool, detail: object = "") -> None:
    results["checks"].append({"check": name, "ok": bool(ok), "detail": str(detail)[:200]})


def wait(job_id: str, statuses: set[str], timeout: int = 180) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/tasks/{job_id}").json()
        if job.get("status") in statuses:
            return job
        time.sleep(1)
    return job


def submit(text: str, est: str) -> str:
    r = client.post("/tasks", json={"user_input": text, "establishment_id": est})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def citations(answer: str) -> set[str]:
    return set(re.findall(r"\[([^\[\]\n]+\.[A-Za-z0-9]{1,5})\]", answer or ""))


def run_case(key: str, text: str, est: str, expect_agents: list[str], extra=None) -> dict | None:
    if not selected(key):
        return None
    job_id = submit(text, est)
    job = wait(job_id, {"DONE", "FAILED", "WAITING_APPROVAL"})
    res = job.get("result") or {}
    r = {"case": key, "job_id": job_id, "establishment": est, "question": text,
         "status": job.get("status"), "error": job.get("error"),
         "agents_consulted": res.get("agents_consulted"),
         "answer": res.get("answer"), "sources": res.get("sources"), "checks": []}
    check(r, "status DONE", job.get("status") == "DONE", job.get("status"))
    options = expect_agents if expect_agents and isinstance(expect_agents[0], list) else [expect_agents]
    check(r, f"delegación {options}", res.get("agents_consulted") in options,
          res.get("agents_consulted"))
    if extra:
        extra(r, res)
    RESULTS.append(r)
    return r


def ops_case(r: dict, res: dict) -> None:
    """'Fuera de servicio' = status 'broken' in the DEMO data (maintenance is reported apart)."""
    answer = (res.get("answer") or "").lower()
    broken = [m.name for m in get_machinery() if m.status == "broken"]
    check(r, "menciona la maquinaria rota real (datos DEMO)",
          all(name.lower() in answer for name in broken), broken)
    check(r, "sin citas de archivos (operations no es documental)", not citations(res.get("answer")))


def ops_pending_tasks(r: dict, res: dict) -> None:
    answer = (res.get("answer") or "").lower()
    pending = [t.description for t in get_tasks() if t.status == "pending"]
    check(r, "menciona la tarea pendiente real", all(p.split()[-1].lower() in answer for p in pending), pending)


def knowledge_case(allowed: set[str], must: list[str]):
    def inner(r: dict, res: dict) -> None:
        srcs = res.get("sources") or []
        names = {s["source"] for s in srcs}
        cited = citations(res.get("answer"))
        check(r, "fuentes son documentos del establecimiento", names and names <= allowed, sorted(names))
        check(r, "citas ⊆ fuentes recuperadas", cited <= names, sorted(cited))
        check(r, "respuesta contiene el dato esperado",
              all(m.lower() in (res.get("answer") or "").lower() for m in must), must)
        r["retrieval_modes"] = sorted({s["retrieval"] for s in srcs})
    return inner


def no_leak(forbidden: str):
    """The user repeats the code; what must not leak is the other farm's PROCEDURE text."""
    secret_phrases = ["planilla de campo", "tablero de la sembradora", "encargado de campo"]

    def inner(r: dict, res: dict) -> None:
        srcs = {s["source"] for s in (res.get("sources") or [])}
        check(r, "sin fuentes de otra finca", forbidden not in srcs, sorted(srcs))
        answer = (res.get("answer") or "").lower()
        check(r, "no revela el procedimiento de otra finca",
              not any(p in answer for p in secret_phrases), [p for p in secret_phrases if p in answer])
    return inner


def no_info(r: dict, res: dict) -> None:
    check(r, "sin fuentes", not res.get("sources"), res.get("sources"))
    check(r, "sin citas inventadas", not citations(res.get("answer")), citations(res.get("answer")))


def hitl(text: str, approve: bool | None, key: str) -> dict | None:
    if not selected(key):
        return None
    job_id = submit(text, "demo")
    job = wait(job_id, {"WAITING_APPROVAL", "FAILED", "DONE"})
    r = {"case": key, "job_id": job_id, "establishment": "demo", "question": text,
         "status": job.get("status"), "error": job.get("error"), "checks": [],
         "pending_action": job.get("pending_action")}
    check(r, "llega a WAITING_APPROVAL", job.get("status") == "WAITING_APPROVAL", job.get("status"))
    pend = job.get("pending_action") or {}
    check(r, "acción pendiente con severidad/máquina extraídas",
          pend.get("payload", {}).get("machine") and pend.get("payload", {}).get("severity"),
          pend.get("payload"))
    if approve is not None:
        resp = client.post(f"/tasks/{job_id}/approve", json={"approve": approve, "reason": "QA"},
                           headers={"X-API-Key": APPROVAL_KEY})
        check(r, f"decisión aceptada ({'aprobar' if approve else 'rechazar'})", resp.status_code == 200,
              resp.status_code)
        done = wait(job_id, {"DONE", "FAILED"})
        res = done.get("result") or {}
        r["status_after"] = done.get("status")
        r["action_status"] = res.get("action_status")
        r["answer"] = res.get("answer")
        check(r, "estado final DONE", done.get("status") == "DONE", done.get("status"))
        expected = "APPLIED" if approve else "REJECTED"
        check(r, f"action_status {expected}", res.get("action_status") == expected, res.get("action_status"))
        incident_id = f"{job_id}-incident"
        exists = subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "EXISTS",
             f"agroops:incident:{incident_id}"],
            capture_output=True, text=True, cwd=REPO).stdout.strip()
        check(r, "incidencia en Redis solo si se aprobó", exists == ("1" if approve else "0"), exists)
    RESULTS.append(r)
    return r


ONLY: set[str] | None = None


def selected(key: str) -> bool:
    return ONLY is None or key in ONLY


def main() -> None:
    global OUT, ONLY
    sys.stdout.reconfigure(encoding="utf-8")
    if "--only" in sys.argv:
        ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(","))
    if "--out" in sys.argv:
        OUT = Path(sys.argv[sys.argv.index("--out") + 1])
    demo_docs = {"maquinaria_demo.md", "procedimientos_demo.md", "tareas_agropecuarias_demo.md"}
    run_case("C1_operations", "¿Qué maquinaria está fuera de servicio y qué tareas están pendientes?",
             "demo", ["operations"],
             lambda r, res: (ops_case(r, res), ops_pending_tasks(r, res)))
    run_case("K1_knowledge", "¿Cuál es el procedimiento de triple lavado del tanque de la pulverizadora?",
             "demo", ["knowledge"], knowledge_case(demo_docs, ["lavado"]))
    run_case("B1_both", "¿Qué maquinaria está fuera de servicio y qué procedimiento de lavado aplica después de fumigar?",
             "demo", ["operations", "knowledge"],
             lambda r, res: (ops_case(r, res), knowledge_case(demo_docs, ["lavado"])(r, res)))
    run_case("H1_hybrid_code", "¿Qué debo verificar en la sembradora según el código CTRL-7731-ALFA?",
             "finca-real-qa", ["knowledge"],
             knowledge_case({"procedimiento_control_sembradora.md"}, ["CTRL-7731-ALFA"]))
    run_case("I1_isolation", "¿Qué debo verificar en la sembradora según el código CTRL-7731-ALFA?",
             "demo", [["knowledge"], []], no_leak("procedimiento_control_sembradora.md"))
    run_case("N1_no_info", "¿Cuál es la receta de la torta de chocolate?", "demo",
             [[], ["knowledge"]], no_info)
    hitl("Registrá una incidencia: la cosechadora pierde presión hidráulica", True, "HITL_approve")
    hitl("Registrá una incidencia: el tractor tiene la batería descargada", False, "HITL_reject")
    OUT.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(r["checks"]) for r in RESULTS)
    ok = sum(c["ok"] for r in RESULTS for c in r["checks"])
    for r in RESULTS:
        print(f"{r['case']:<18} status={r['status']:<17} agents={r.get('agents_consulted')}")
        for c in r["checks"]:
            print(f"    {'PASS' if c['ok'] else 'FAIL'}  {c['check']}  -> {c['detail'][:120]}")
    print(f"\nChecks: {ok}/{total} PASS. Results: {OUT}")


if __name__ == "__main__":
    main()
