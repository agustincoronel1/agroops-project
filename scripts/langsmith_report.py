"""Print the LangSmith trace tree of a job with REAL metrics (latency, tokens, cost).

Usage: python scripts/langsmith_report.py <job_id> [--project NAME]

Reads LANGSMITH_API_KEY / LANGSMITH_PROJECT from the environment or .env. Values
LangSmith did not return are printed as NO VERIFICADO (never estimated). Read-only.
"""

import argparse
import os
import sys
from pathlib import Path

from langsmith_api import fetch_job_traces

NV = "NO VERIFICADO"


def _load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("LANGSMITH_") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _fmt(v: object, spec: str = "") -> str:
    return NV if v is None else format(v, spec)


def _latency(run) -> float | None:
    if run.start_time and run.end_time:
        return (run.end_time - run.start_time).total_seconds()
    return None


def _print_tree(node, depth: int = 0) -> None:
    model = (node.extra or {}).get("metadata", {}).get("ls_model_name")
    suffix = f" model={model}" if node.run_type in ("llm", "embedding") else ""
    print(
        f"{'  ' * depth}- {node.name} [{node.run_type}] {_fmt(_latency(node), '.2f')}s"
        f" tok={_fmt(node.agg_tokens)} cost={_fmt(node.agg_cost)}{suffix}"
    )
    for child in sorted(node.child_runs, key=lambda r: r.start_time):
        _print_tree(child, depth + 1)


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("job_id")
    ap.add_argument("--project", default=os.environ.get("LANGSMITH_PROJECT", "agroops-intelligence"))
    args = ap.parse_args()
    traces = fetch_job_traces(args.job_id, args.project)
    if not traces:
        print(f"No traces for job_id={args.job_id} in project {args.project}")
        return 1
    for trace in traces:
        root = trace["node"]
        print(f"\n=== trace {trace['trace_id']}  (name={root.name}, status={root.status}) ===")
        print(f"url: {trace['url'] or NV}")
        print(
            f"job_id={args.job_id} establishment_id={trace['establishment_id']} "
            f"job_status={trace['job_status_meta']} resumed={trace['resumed']}"
        )
        cost_note = "" if trace["cost_complete"] else " (lower bound: some model prices are NO VERIFICADO)"
        print(
            f"total latency: {_fmt(trace['latency_s'], '.2f')}s | tokens in/out/total (llm runs): "
            f"{_fmt(trace['tokens_in'])}/{_fmt(trace['tokens_out'])}/{_fmt(trace['tokens_total'])} | "
            f"cost total (USD): {_fmt(trace['cost_total'])}{cost_note}"
        )
        _print_tree(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
