# LangSmith screenshots (manual capture)

These captures require your LangSmith login, so they are **not** generated automatically.
Save each image in this folder with the exact file name below. Do not include the API key,
account settings, workspace members or any page outside the `agroops-intelligence` project.

## Checklist

| # | File | Where to click | What must be visible |
|---|---|---|---|
| 1 | `langsmith_project_runs.png` | Tracing → project `agroops-intelligence` | Run list with latency, tokens and cost columns; include the five `agroops.job` roots of the load test |
| 2 | `langsmith_root_output_job_B.png` | Open trace `01a141a6-bfee-7a23-85ff-b86c5af76e80` | The root **Output** panel: `agents_consulted`, `sources_count`, `has_answer`, `status: DONE` |
| 3 | `langsmith_trace_waterfall.png` | Open trace `01a141a6-da5d-7d10-af79-9a422d59af34` (job C) | Expanded tree: supervisor, operations, knowledge, `knowledge.retrieve`, `openai.embeddings`, `openai.chat`, with latencies |
| 4 | `langsmith_hitl_threads.png` | Open the two HITL traces of job `7cce4c42563b4346817f87370daf9312` (`01a141a7-2e7b-7bd2-82fd-7d8ed8805074` and `01a141a7-36df-7f63-b24a-4a5768a560ce`) | Trace 1 with `job_status: waiting_approval`, trace 2 with `resumed: true` and `done`, same `job_id` |
| 5 | `langsmith_llm_call.png` | Any `openai.chat` run inside trace #3 | Model `gpt-4o-mini`, input and output tokens, cost |
| 6 | `langsmith_embedding_nv.png` | The `openai.embeddings` run inside trace #3 | Real token count and cost shown as NO VERIFICADO (LangSmith has no price for `text-embedding-3-small`) |
| 7 | `langsmith_metadata_filter.png` | Filter bar: `metadata.job_id = 7cce4c42563b4346817f87370daf9312` | The filter applied and both traces listed |
| 8 | `langsmith_failed_job.png` (optional) | Trace `01a13fd3-ccc7-7240-8016-7bc8af7b0dd9` (mock project `agroops-intelligence-mocks`) | Status `error` and root Output `status: FAILED`, `error_type: TimeoutError` |

## After capturing

1. Check that no capture shows the API key, the approval key or account settings.
2. Reference the files from `FINAL_TEST_REPORT.md` (section "Checklist de capturas") and mark them done there.
