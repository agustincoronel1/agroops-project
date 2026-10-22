"""Pure metric helpers of scripts/load_test_tasks.py (no network, no OpenAI)."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "load_test_tasks.py"
_spec = importlib.util.spec_from_file_location("load_test_tasks", _PATH)
assert _spec and _spec.loader
lt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lt)


def test_nearest_rank_with_five_samples() -> None:
    values = [0.2, 0.5, 0.1, 0.4, 0.3]
    assert lt.percentile_nearest_rank(values, 50) == 0.3
    assert lt.percentile_nearest_rank(values, 95) == 0.5  # n=5: p95 is the max


def test_linear_percentile_differs_from_nearest_rank_for_small_n() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert lt.percentile_linear(values, 50) == pytest.approx(3.0)
    assert lt.percentile_linear(values, 95) == pytest.approx(4.8)  # NumPy default


def test_summary_counts_missing_values_instead_of_inventing_them() -> None:
    s = lt.summarize([1.0, None, 3.0])
    assert s["n"] == 2 and s["missing"] == 1 and s["max"] == 3.0
    assert lt.summarize([None])["p50"] is None


def test_job_metrics_queue_processing_total_and_unknown_cost() -> None:
    api = {"created_at": "2026-10-15T22:20:09.000000Z", "updated_at": "2026-10-15T22:20:30.000000Z"}
    traces = [
        {"start": "2026-10-15T22:20:20.000000+00:00", "latency_s": 4.0, "tokens_in": 10,
         "tokens_out": 2, "tokens_total": 12, "cost_total": 0.5},
        {"start": "2026-10-15T22:20:25.000000+00:00", "latency_s": 1.0, "tokens_in": 3,
         "tokens_out": 1, "tokens_total": 4, "cost_total": None},
    ]
    m = lt.job_metrics(api, traces, post_latency_s=0.05)
    assert m["queue_wait_s"] == pytest.approx(11.0)
    assert m["processing_s"] == pytest.approx(5.0)
    assert m["total_s"] == pytest.approx(21.0)
    assert m["tokens_total"] == 16 and m["tokens_in"] == 13
    assert m["cost_complete"] is False  # one run has no price: never estimated


def test_job_without_traces_reports_not_verified() -> None:
    m = lt.job_metrics({"created_at": "2026-10-15T22:20:09Z", "updated_at": "2026-10-15T22:20:12Z"}, [], None)
    assert m["processing_s"] is None and m["tokens_total"] is None and m["cost_complete"] is False
    job = {"metrics": m, "traces": [], "api": {"result": None}, "post_http": 202, "approve_http": None}
    gaps = lt.not_verified(job)
    assert any("processing_s" in g for g in gaps) and any("cost_usd" in g for g in gaps)


def test_refuses_to_post_without_confirmation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sys.argv", ["x", "--out", str(tmp_path / "o.json")])
    with pytest.raises(SystemExit, match="confirm-openai"):
        lt.main()


def test_unpriced_llm_run_makes_cost_incomplete() -> None:
    traces = [{
        "start": "2026-10-15T22:20:20+00:00", "latency_s": 2.0, "tokens_in": 5, "tokens_out": 1,
        "tokens_total": 6, "cost_total": 0.4,
        "nodes": {"openai.embeddings": {"run_type": "llm", "cost_known": False, "tokens_known": True,
                                        "n": 1, "latency_s": 0.3, "tokens": 5, "cost": 0.0}},
    }]
    m = lt.job_metrics({"created_at": "2026-10-15T22:20:09Z", "updated_at": "2026-10-15T22:20:12Z"}, traces, None)
    assert m["cost_complete"] is False  # root sum excludes the embedding: must not look complete
