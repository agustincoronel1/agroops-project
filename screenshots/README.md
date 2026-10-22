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

## Status (2026-10-21)

Five captures exist (`CAPTURA1`, `CAPTURA2`, `CAPTURA3`, `CAPTURA4`, `CAPTURA6`). They were
reviewed; none shows the checklist traces above. Files are **not renamed** until the set is complete.

| Existing file | What it shows | Covers checklist item |
|---|---|---|
| `CAPTURA1.png` | Root `agroops.job` of job `09ab6ab1bd4d4a6f853110a244e51bb6` (15/10): latency 8.52 s, 2.021K tokens, cost $0.0005, Output panel (`status DONE`, `agents_consulted`, `has_answer`, `sources_count`), waterfall with `knowledge.retrieve`, `openai.embeddings` (text-embedding-3-small) and `openai.chat` (gpt-4o-mini) with tokens per call | #2 (other job), #3 (other job), part of #5 (tokens, no per-call cost), part of #6 (tokens, no NO VERIFICADO label) |
| `CAPTURA2.png` | `knowledge.retrieve` output (retrieved DEMO passages) for the same job | RAG evidence (not in the checklist) |
| `CAPTURA3.png` | Same retrieve output, more passages | RAG evidence (not in the checklist) |
| `CAPTURA4.png` | Same retrieve output as JSON, tags `agroops`/`worker`, metadata `job_id`, `establishment_id=demo`, `resumed=false` | Part of #7 (metadata, no filter bar) |
| `CAPTURA6.png` | Swagger `GET /tasks/09ab…` returning `DONE` with sources | FastAPI integration (not LangSmith) |

**Missing captures (needed for the "at least 5 executions" requirement):**

1. **#1 project run list (critical).** Tracing → project `agroops-intelligence` → runs list with the
   columns Latency, Total tokens and Total cost visible, showing the five final-run roots of
   `docs/evidencia/casos_reales/corrida_4_final.json` (job IDs in `docs/validacion_final.md` §2).
   Without it there is no single view of five or more executions.
2. **#4 HITL threads (critical for HITL traceability).** Job `2276e79e756b46639677b13ce49c21a8`
   (approve, corrida 4): both traces, the first with `job_status: waiting_approval`, the second
   with `resumed: true` and `done`, same `job_id`.
3. **#7 metadata filter.** The same job filtered by `metadata.job_id`, with both traces listed.
   Can be one capture together with #4.
4. **#6 embedding with NO VERIFICADO (needed by the "costs when available" requirement).** An
   `openai.embeddings` run from corrida 4 showing the real token count and the cost as NO VERIFICADO.

Optional: #5 (one `openai.chat` run with its cost; CAPTURA1 has tokens only) and #8 (failed job).
A failed-job trace from the crash tests exists (job `e6a481f35d92432388921f4365ea2ed4` in
`docs/evidencia/robustez/crash_recovery.json`, status `error`, `job_status=failed`).

Checklist targets for #1, #4 and #7 are now the corrida 4 jobs above. The earlier job IDs in the table
are from the 15/10 run and are kept as history.

## After capturing

1. Check that no capture shows the API key, the approval key or account settings.
2. Reference the files from `FINAL_TEST_REPORT.md` (section "Checklist de capturas") and mark them done there.
