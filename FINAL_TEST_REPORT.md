# AgroOps: reporte final de pruebas (trazabilidad, rendimiento y concurrencia)

Written for: el equipo de AgroOps que revisa la integración con LangSmith.

Versión 2. Reconstruido con los resultados de la corrida real del 2026-10-15 (timestamps en UTC). Datos crudos: [load_test_results.json](load_test_results.json), generado por [scripts/load_test_tasks.py](scripts/load_test_tasks.py) en modo `--collect-only`, sin llamadas a OpenAI.

## 1. Resumen

| Área | Resultado |
|---|---|
| Suite de tests (`pytest`, backend) | **PASS**: 106 passed, 0 failed |
| Operations (tareas y maquinaria DEMO) | **PASS** |
| Knowledge (RAG con fuentes verificables) | **PASS** |
| Supervisor (consulta que requiere ambos agentes) | **PASS** |
| HITL (WAITING_APPROVAL → aprobar → DONE/APPLIED) | **PASS** |
| Sin información (sources=[] y sin datos inventados) | **PASS** |
| Citas en la respuesta (no inventar fuentes) | **FAIL en la corrida original** (cita "[Fuente: Knowledge Agent]"). **Corregido en código** con tests de regresión PASS. Validación en vivo: **NO VERIFICADO** (no hubo llamadas a OpenAI autorizadas). |
| Carga: 5 POST concurrentes | **PASS** (5/5 DONE). Hallazgo: el worker procesa en serie. |
| Output real en el root `agroops.job` | **PASS** (5 de 5 jobs; ambas trazas de E) |
| Código HTTP del POST | **PASS** para B–E (202). **NO VERIFICADO** para A (ver §8). |
| Código HTTP de la aprobación de E | **NO VERIFICADO** (ver §8). Su efecto sí está verificado en la API. |
| Costo de embeddings | **NO VERIFICADO** (LangSmith no tiene precio para `text-embedding-3-small`). Tokens reales. |
| Bloqueo del event loop en ingestión | **FAIL encontrado y corregido** (ver §7). Test de regresión PASS. |
| `docker compose up --build` | **PASS** (stack arriba, `/health` OK) |
| Capturas de LangSmith | **PENDIENTE** (requieren tu login; checklist en [screenshots/README.md](screenshots/README.md)) |

Presupuesto de OpenAI real: **8 jobs** en total en el proyecto (3 de sesiones anteriores + 5 del lote), dentro del límite de 10. **Esta versión no hizo llamadas a OpenAI.**

## 2. Pruebas y resultados

| # | Prueba | Job (caso) | Resultado | Evidencia |
|---|---|---|---|---|
| 1 | Operations: tareas y maquinaria DEMO | `da7fef4b6f254a6ab70e6dfd3ebcdd88` (A) | **PASS** | Tarea pendiente: "Aplicación de fungicida" en Lote Norte (T1 `pending`; T2 es `in_progress` y T3 `done`, correctamente omitidas). Pulverizadora en mantenimiento (servicio 2026-10-10). Cosechadora rota (2026-10-01). Coincide con `operations_data.py`. |
| 2 | Knowledge: RAG con fuentes | `f4cad154fc8645f0b35717952d63bfd9` (B) | **PASS** en fuentes; **FAIL** en cita (ver fila 2b) | `sources=[{source: procedimientos_demo.md, chunk_id: b99b63b935a0ca44-0000, distance: 0.385}]`. El procedimiento (triple lavado y limpieza del equipo) está en `procedimientos_demo.md` línea 11. |
| 2b | Citas en la respuesta | B | **FAIL** (corrida original) → **corregido en código** | La respuesta decía "[Fuente: Knowledge Agent]", que no es un documento. Causa: el prompt de síntesis pedía citar sin recibir los nombres reales. Fix: la síntesis recibe la lista exacta de documentos con soporte y tiene prohibido citar otra cosa. Tests de regresión PASS. **Validación en vivo: NO VERIFICADO** (requiere una llamada a OpenAI). |
| 3 | Supervisor: requiere ambos agentes | `efd60c24132644328038b789a7aee194` (C) | **PASS** | `agents_consulted=[operations, knowledge]`, tres decisiones del supervisor. Fuente: `tareas_agropecuarias_demo.md` (chunk `4905234984b50e1f-0001`, distance 0.498). La incidencia "roya en bordes" (Lote Norte) está en `operations_data.py`. |
| 4 | HITL: espera → aprobación → APPLIED | `7cce4c42563b4346817f87370daf9312` (E) | **PASS** | Traza 1 `waiting_approval` con `pending_action` generado por el LLM. Tras aprobar: `action={status: executed, incident_id: 7cce4c42563b4346817f87370daf9312-incident, created: true}`, `action_status=APPLIED`, `DONE`. Traza 2 `done` con `resumed=True`. |
| 5 | Sin información | `288438466b4342578e8081ea36dd89e5` (D) | **PASS** | `sources=[]`, `agents_consulted=[knowledge]`. Respuesta: no hay información sobre la dosis; recomienda consultar a un veterinario. No da dosis. El knowledge node llamó al LLM (970 tokens), así que el retriever devolvió pasajes y el LLM los descartó (`answered=false`). |
| 6 | Carga: 5 POST simultáneos | A–E (ver §5) | **PASS** con hallazgo | 5/5 DONE. POST p95 0.054 s. El worker procesa de a un job (cola de hasta 28 s). |
| T | Suite de tests | — | **PASS** | `cd backend && pytest`: 106 passed. |

## 3. Job IDs y Trace IDs (proyecto `agroops-intelligence`)

URL base: `https://smith.langchain.com/o/5f98afe5-9555-4add-89f4-cec016cca5d6/projects/p/24139510-421b-489e-88a3-6455cc875068/r/<trace_id>`

| Caso | Job ID | Trace ID | Traza | Estado en API |
|---|---|---|---|---|
| A operations | `da7fef4b6f254a6ab70e6dfd3ebcdd88` | `01a141a7-11bf-7950-a5cc-ae00b9bbf5af` | root (done) | DONE |
| B knowledge | `f4cad154fc8645f0b35717952d63bfd9` | `01a141a6-bfee-7a23-85ff-b86c5af76e80` | root (done) | DONE |
| C supervisor | `efd60c24132644328038b789a7aee194` | `01a141a6-da5d-7d10-af79-9a422d59af34` | root (done) | DONE |
| D sin info | `288438466b4342578e8081ea36dd89e5` | `01a141a6-fe7c-7a52-b8be-f1ce01c5ce33` | root (done) | DONE |
| E HITL | `7cce4c42563b4346817f87370daf9312` | `01a141a7-2e7b-7bd2-82fd-7d8ed8805074` | root (waiting_approval) | DONE |
| E HITL | `7cce4c42563b4346817f87370daf9312` | `01a141a7-36df-7f63-b24a-4a5768a560ce` | root (done, resumed) | DONE |

Jobs de sesiones anteriores (no repetidos): `2071f1c407d24ac0b50f770a196854e9` (RAG + operations, trace `01a141a0-4017-7fc1-a7d9-c385226bb716`), `68ba70ad5c884d6e82e50e5ea0829bf4` (HITL, traces `01a13fd2-bf67-7e13-b620-7736c3f74bd1` y `01a13fd2-de2a-7100-89ee-671228094dc1`), `b8e8fd6ca466414292cc9240a4fc3a1b` (RAG, trace `01a13fd2-06a5-7ec2-8401-288e7ccc5886`).

### Output del root (leído desde LangSmith)

```json
{"action_status": null, "agents_consulted": ["knowledge"], "error_type": null,
 "has_answer": true, "job_id": "f4cad154fc8645f0b35717952d63bfd9", "sources_count": 1, "status": "DONE"}
```

Los cinco roots DONE y las dos trazas de E tienen Output con ese esquema. La traza `waiting_approval` de E tiene `status=WAITING_APPROVAL`, `action_status=PENDING_APPROVAL` (solo de traza), `has_answer=false`. Ningún output contiene texto de respuesta, prompts, la pregunta del usuario ni contenido de documentos.

## 4. Latencias, tokens y costos reales

Fuente: LangSmith, por job (`read_run` sobre cada root y sus hijos). Costo calculado por LangSmith con su tabla de precios; no comparado con factura de OpenAI.

| Caso | Trazas | Procesamiento | Tokens in / out / total | Costo USD (LangSmith) | Costo completo |
|---|---|---|---|---|---|
| A operations | 1 | 7.35 s | 2521 / 413 / 2934 | 0.00062235 | **NO** (embedding NV) |
| B knowledge | 1 | 6.77 s | 1548 / 181 / 1729 | 0.00033765 | **NO** (embedding NV) |
| C supervisor | 1 | 9.25 s | 2700 / 660 / 3360 | 0.00079710 | **NO** (embedding NV) |
| D sin info | 1 | 4.93 s | 1525 / 195 / 1720 | 0.00034230 | **NO** (embedding NV) |
| E HITL | 2 (1.53 s + 2.09 s) | 3.61 s | 753 / 143 / 896 | 0.00019875 | Sí (sin embeddings) |
| **Total** | 6 | 31.91 s | 9047 / 1592 / **10 639** | **0.00229815** (suma de partes conocidas) | — |

Lectura del costo: el costo de cada root **excluye los embeddings**, porque LangSmith no les asigna precio. Por eso los jobs A–D están marcados como costo incompleto. El total de 0.00229815 USD es una **cota inferior** del costo real. Tokens de embeddings: 94 en total (reales; ver tabla por nodo).

### Por nodo (agregado de las 6 trazas)

| Nodo | Tipo | Ejecuciones | Latencia acumulada | Tokens | Costo USD |
|---|---|---|---|---|---|
| `agroops.graph` | chain | 6 | 31.89 s | 10 639 | 0.002298 |
| `openai.chat` | llm | 24 | 29.35 s (promedio 1.22 s) | 10 545 | 0.002298 |
| `supervisor` | chain | 12 | 20.85 s | 4 813 | 0.001224 |
| `knowledge` | chain | 4 | 6.03 s | 3 934 | 0.000636 |
| `operations` | chain | 2 | 4.10 s | 1 699 | 0.000400 |
| `knowledge.retrieve` | retriever | 4 | 2.39 s | 94 | NO VERIFICADO |
| `openai.embeddings` | llm | 4 | 2.35 s | 94 | NO VERIFICADO |
| `prepare_incident` | chain | 1 | 0.83 s | 193 | 0.000038 |
| `approval` | chain | 2 | 0.01 s | 0 | NO VERIFICADO (sin LLM) |
| `_route` | chain | 12 | 0.01 s | 0 | NO VERIFICADO (sin LLM) |

Los agregados por nodo incluyen a sus hijos; no se suman entre sí.

## 5. Concurrencia: 5 jobs simultáneos

Los 5 POST se enviaron en paralelo desde un mismo cliente. Todos tienen `created_at` dentro de 15 ms (22:20:09).

| Caso | POST (s) | Espera en cola (s) | Procesamiento (s) | Total hasta terminal (s) | Estado |
|---|---|---|---|---|---|
| B knowledge | 0.034 | 0.01 | 6.77 | 6.78 | DONE |
| C supervisor | 0.049 | 6.78 | 9.25 | 16.03 | DONE |
| D sin info | 0.049 | 16.02 | 4.93 | 20.95 | DONE |
| A operations | 0.053 | 20.95 | 7.35 | 28.31 | DONE |
| E HITL | 0.054 | 28.31 | 3.61 | 32.54 | DONE (APPLIED) |

Los inicios de procesamiento confirman la serialización: B 22:20:09 → C 22:20:16 → D 22:20:25 → A 22:20:30 → E 22:20:37 (espera de aprobación) → reanudación de E 22:20:40. Cada job empieza cuando termina el anterior.

## 6. p50 / p95 y metodología

Muestra: n = 5 jobs. Se reportan **dos métodos** de p95 porque difieren con n pequeño:
- **nearest-rank**: el ceil(0.95·n)-ésimo valor. Con n = 5 es el máximo.
- **lineal** (interpolación, por defecto de NumPy).

| Métrica | p50 | p95 nearest-rank | p95 lineal | Media | Máx |
|---|---|---|---|---|---|
| POST (HTTP, aceptación) | 0.0491 s | 0.0544 s | 0.0541 s | 0.0480 s | 0.0544 s |
| Procesamiento (LangSmith) | 6.77 s | 9.25 s | 8.87 s | 6.38 s | 9.25 s |
| Espera en cola | 16.02 s | 28.31 s | 26.84 s | 14.42 s | 28.31 s |
| Total hasta terminal | 20.95 s | 32.54 s | 31.69 s | 20.92 s | 32.54 s |

**Limitaciones:** con 5 muestras el p95 no es estadísticamente significativo. Los 5 casos son heterogéneos (distinto número de llamadas), así que la dispersión mezcla tipo de consulta con cola. Son valores descriptivos de esta corrida, no un objetivo de servicio. El total incluye la espera de aprobación de E, que controla el driver (sondeo cada 1 s).

## 7. Cuellos de botella y hallazgos

1. **Worker en serie (principal).** Con 5 jobs la cola llega a 28 s. Es la parte más grande de la latencia total. Cambiarlo requiere modificar el worker, lo que no se hizo en esta revisión.
2. **Llamadas LLM secuenciales.** `openai.chat` concentra el 92 % del tiempo del grafo (29.35 de 31.89 s) y el 99 % de los tokens.
3. **Supervisor, el nodo más costoso:** 20.85 s y 4 813 tokens (45 % del total). Cada paso hace una llamada de routing, y la síntesis final es otra.
4. **Embeddings y retriever:** 2.35 s acumulados y 94 tokens. No son cuello de botella.
5. **Knowledge con pasajes descartados (D).** El retriever devolvió pasajes por debajo de `RAG_MAX_DISTANCE`, y el LLM los rechazó. Es el comportamiento esperado (no inventar), con 1 697 tokens de costo.
6. **Cita inventada (B, FAIL corregido).** Ver fila 2b. Corregido en el prompt de síntesis; falta validarlo en vivo.
7. **Bloqueo del event loop en ingestión (FAIL corregido).** La lectura de archivos y el parseo de PDF (`pypdf`) y el listado del directorio eran síncronos dentro de funciones `async`. Ahora corren en `asyncio.to_thread`. Un test de regresión mide que el loop siga avanzando durante un parseo lento. Auditoría del resto del backend: Redis (`redis.asyncio`), Chroma (`AsyncHttpClient`) y el checkpointer son asíncronos. No hay `time.sleep`, `requests` ni subprocesos dentro de `async def`.

## 8. Errores y pendientes

- **Driver original sin JSON (corregido).** El primer driver no escribió su resultado por un error de ruta de mi lado (formato `/c/...`). Los jobs ya habían corrido. El script ahora está en el repo como `scripts/load_test_tasks.py` (ruta por argumento, guardia `--confirm-openai`, modo `--collect-only`). El JSON actual se reconstruyó con ese modo, sin POST ni OpenAI.
- **Latencias POST de la corrida original:** se pasaron al modo `--collect-only` desde el log de la corrida (valores capturados en pantalla). Están marcados como copiados en `load_test_results.json`.
- **Código HTTP del POST de A: NO VERIFICADO.** Se perdió al cortar la salida. Sí se capturó `PENDING` y 0.053 s.
- **Código HTTP de la aprobación de E: NO VERIFICADO.** El driver original lo guardaba en memoria y se perdió. El efecto está verificado: traza de reanudación (`resumed=True`), incidencia creada (`created=true`), `action_status=APPLIED`.
- **Costo de embeddings: NO VERIFICADO.** Sus tokens son reales.
- **Costo raíz incompleto:** el total de 0.00229815 USD es cota inferior (excluye embeddings).
- **Validación en vivo de la corrección de citas: NO VERIFICADO.** Requiere una llamada real a OpenAI, que no se autorizó en esta revisión. La regla está cubierta por tests con LLM falso.
- **Trazas de ruido:** cada `_route` es un run propio. Cada arranque genera 3 `agroops.ingest` con `skipped=True`. Verificado: el reinicio de esta revisión no llamó a embeddings (los 4 embeddings de la ventana son hijos de jobs del lote, 22:20).
- **Deprecaciones del SDK** en `scripts/langsmith_report.py` (`list_runs`, `read_run`, `get_run_url`). Migrar antes de enero de 2027. El script de carga también las usa.
- **Warning de `langsmith.wrappers._openai_agents`** en pytest: viene de la librería, no de nuestro código.
- **Capturas de LangSmith: PENDIENTE** (ver §9).

## 9. Capturas manuales pendientes

El checklist completo, con clics y qué debe verse, está en [screenshots/README.md](screenshots/README.md).
Estado al 2026-10-21: hay 5 capturas (`CAPTURA1`, `CAPTURA2`, `CAPTURA3`, `CAPTURA4`, `CAPTURA6`) que
muestran una sola ejecución (job `09ab6ab1bd4d4a6f853110a244e51bb6`, 15/10). Faltan capturas de la lista de
runs con al menos cinco ejecuciones, del HITL con sus dos trazas, del filtro por `job_id` y del embedding
con costo NO VERIFICADO.

- [ ] 1. `langsmith_project_runs.png` (falta: lista de runs con cinco o más jobs de la corrida 4)
- [ ] 2. `langsmith_root_output_job_B.png` (parcial: CAPTURA1, otro job)
- [ ] 3. `langsmith_trace_waterfall.png` (parcial: CAPTURA1, otro job)
- [ ] 4. `langsmith_hitl_threads.png` (falta: las dos trazas del job `2276e79e756b46639677b13ce49c21a8`)
- [ ] 5. `langsmith_llm_call.png` (parcial: CAPTURA1 muestra tokens, no costo por llamada)
- [ ] 6. `langsmith_embedding_nv.png` (falta: embedding con costo NO VERIFICADO)
- [ ] 7. `langsmith_metadata_filter.png` (falta: filtro por `job_id`; CAPTURA4 muestra metadata sin filtro)
- [ ] 8. `langsmith_failed_job.png` (opcional; hay traza fallida de los tests de crash)

## 10. Cómo reproducir

```bash
# Tests (no usa OpenAI)
cd backend && pytest

# Reconstruir el JSON de carga desde jobs existentes (no usa OpenAI)
python scripts/load_test_tasks.py --collect-only --out load_test_results.json \
  --job A_operations=da7fef4b6f254a6ab70e6dfd3ebcdd88:0.05312037467956543 \
  --job B_knowledge=f4cad154fc8645f0b35717952d63bfd9:0.03439640998840332 \
  --job C_supervisor_both=efd60c24132644328038b789a7aee194:0.04905533790588379 \
  --job D_no_info=288438466b4342578e8081ea36dd89e5:0.049112558364868164 \
  --job E_hitl=7cce4c42563b4346817f87370daf9312:0.054363250732421875

# Carga real (USA OPENAI, 5 jobs): solo con autorización explícita
python scripts/load_test_tasks.py --confirm-openai --out load_test_results.json
```
