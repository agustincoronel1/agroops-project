"""Read-only LangSmith access shared by the report and load-test scripts.

Uses the current SDK API (``runs.query`` / ``runs.get_url``). The older
``list_runs`` / ``read_run`` / ``get_run_url`` calls are deprecated and will be removed.

Totals come from the ``llm`` runs of the trace (chain runs aggregate their children).
Values that LangSmith does not return stay ``None`` and are never estimated.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

SELECTS = [
    "ID", "NAME", "RUN_TYPE", "STATUS", "START_TIME", "END_TIME", "TRACE_ID", "IS_ROOT",
    "PARENT_RUN_IDS", "PROMPT_TOKENS", "COMPLETION_TOKENS", "TOTAL_TOKENS", "PROMPT_COST",
    "COMPLETION_COST", "TOTAL_COST", "EXTRA", "METADATA", "OUTPUTS",
]


def _job_filter(job_id: str) -> str:
    return f'and(eq(metadata_key, "job_id"), eq(metadata_value, "{job_id}"))'


def _seconds(start: Any, end: Any) -> float | None:
    return (end - start).total_seconds() if start and end else None


def _sum(values: list[Any]) -> tuple[float | None, bool]:
    """Sum known values. Returns (total, complete); total is None when nothing is known."""
    known = [v for v in values if v is not None]
    total = (int(sum(known)) if all(isinstance(v, int) for v in known) else float(sum(known))) if known else None
    return total, len(known) == len(values)


async def _query(client: Any, **kwargs: Any) -> list[Any]:
    return [run async for run in client.runs.query(selects=SELECTS, **kwargs)]


async def _fetch(job_id: str, project: str) -> tuple[list[Any], list[Any], dict[str, str]]:
    from langsmith import Client

    client = Client()
    project_id = str(client.read_project(project_name=project).id)
    roots = await _query(
        client, project_ids=[project_id], is_root=True, filter=_job_filter(job_id)
    )
    runs: list[Any] = []
    for root in roots:
        runs.extend(
            await _query(client, project_ids=[project_id], trace_id=str(root.trace_id))
        )
    url_by_trace: dict[str, str] = {}
    for root in roots:
        response = await client.runs.get_url(
            str(root.id), project_id=project_id, trace_id=str(root.trace_id)
        )
        url_by_trace[str(root.trace_id)] = response.url
    return roots, runs, url_by_trace


def _tree(runs: list[Any]) -> dict[str, SimpleNamespace]:
    nodes = {
        str(r.id): SimpleNamespace(
            id=str(r.id), name=r.name, run_type=r.run_type, status=r.status,
            start_time=r.start_time, end_time=r.end_time, trace_id=str(r.trace_id),
            parent_id=str(r.parent_run_ids[-1]) if r.parent_run_ids else None,
            prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
            total_tokens=r.total_tokens, total_cost=r.total_cost,
            prompt_cost=r.prompt_cost, completion_cost=r.completion_cost,
            extra=r.extra or {}, outputs=r.outputs,
            child_runs=[],
        )
        for r in runs
    }
    for node in nodes.values():
        if node.parent_id in nodes:
            nodes[node.parent_id].child_runs.append(node)
    for node in nodes.values():
        if node.parent_id is None:
            _aggregate(node)
    return nodes


def _aggregate(node: SimpleNamespace) -> tuple[list[Any], list[Any]]:
    """Attach ``agg_tokens`` / ``agg_cost``: the node's own value for llm runs, else the sum of
    the llm runs below it (what LangSmith used to report for chain runs)."""
    if node.run_type == "llm":
        node.agg_tokens, node.agg_cost = node.total_tokens, node.total_cost
        return [node.total_tokens], [node.total_cost]
    tokens: list[Any] = []
    costs: list[Any] = []
    for child in node.child_runs:
        child_tokens, child_costs = _aggregate(child)
        tokens.extend(child_tokens)
        costs.extend(child_costs)
    node.agg_tokens = _sum(tokens)[0]
    node.agg_cost = _sum(costs)[0]
    return tokens, costs


def fetch_job_traces(job_id: str, project: str) -> list[dict[str, Any]]:
    """One dict per root trace of the job, with real LangSmith metrics (read-only)."""
    roots, runs, url_by_trace = asyncio.run(_fetch(job_id, project))
    nodes = _tree(runs)
    traces: list[dict[str, Any]] = []
    for root in sorted(roots, key=lambda r: r.start_time):
        trace_id = str(root.trace_id)
        trace_nodes = [n for n in nodes.values() if n.trace_id == trace_id]
        llm = [n for n in trace_nodes if n.run_type == "llm"]
        tokens_in, _ = _sum([n.prompt_tokens for n in llm])
        tokens_out, _ = _sum([n.completion_tokens for n in llm])
        tokens_total, _ = _sum([n.total_tokens for n in llm])
        cost_total, cost_complete = _sum([n.total_cost for n in llm])
        root_node = nodes[str(root.id)]
        meta = root_node.extra.get("metadata", {}) or {}
        traces.append({
            "trace_id": trace_id,
            "url": url_by_trace.get(trace_id),
            "start": root.start_time.isoformat() if root.start_time else None,
            "latency_s": _seconds(root.start_time, root.end_time),
            "status": root.status,
            "job_status_meta": meta.get("job_status"),
            "establishment_id": meta.get("establishment_id"),
            "resumed": meta.get("resumed"),
            "outputs": root.outputs,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_total,
            "cost_total": cost_total,
            "cost_complete": cost_complete,
            "node": root_node,
            "runs": trace_nodes,
        })
    return traces
