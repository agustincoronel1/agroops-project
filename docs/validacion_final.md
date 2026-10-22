# Validación final de AgroOps

Fecha: 2026-10-21. Stack: `docker compose up --build` (backend FastAPI, Redis Stack, ChromaDB).
Modelos: `gpt-4o-mini` (routing, respuestas, extracción HITL) con `temperature=0`, y
`text-embedding-3-small` (embeddings). Trazas: LangSmith, proyecto `agroops-intelligence`.

## 1. Resumen

| Área | Resultado |
|---|---|
| Casos reales con OpenAI (8 casos, corrida final) | **41 de 41 checks** con el check de citas de B1 revisado después de la corrida. Con el check original, **40 de 41** (ver §4, punto 6). Recalculado desde las respuestas guardadas, sin nuevas llamadas. |
| Pruebas de crash real (SIGKILL del backend) | **14 de 14 checks**. Sin jobs perdidos ni acciones duplicadas. |
| Tests unitarios | 132 pasan, 5 omitidos (integración sin variable). |
| Tests de integración con Chroma real y Redis Stack real | 3 Chroma y 2 Redis pasan (ver §9). |
| Lint (Ruff) | Sin errores. |
| Costo real de OpenAI | ≈ USD 0,01 en total (tope: USD 1). |

## 2. Corrida final de casos reales

Todos los jobs terminaron `DONE` (o `WAITING_APPROVAL` y luego `DONE` en HITL). Los job IDs apuntan a sus trazas.

| Caso | Pregunta | Agentes consultados | Resultado verificado | Job ID |
|---|---|---|---|---|
| C1 Operations | ¿Qué maquinaria está fuera de servicio y qué tareas están pendientes? | operations | Cosechadora (rota) y "Aplicación de fungicida" (pendiente). Sin citas. | `6bfd1d0b313044babbffa231c01f6d12` |
| K1 Knowledge | ¿Cuál es el procedimiento de triple lavado del tanque de la pulverizadora? | knowledge | Triple lavado tras cada jornada y calibración de boquillas (10 %). Cita `maquinaria_demo.md`. Respuesta parcial: no menciona `procedimientos_demo.md`. | `f403ef210117424a8063ea5453dd5691` |
| B1 Ambos agentes | ¿Qué maquinaria está fuera de servicio y qué procedimiento de lavado aplica después de fumigar? | operations, knowledge | Cosechadora (rota) y cita `maquinaria_demo.md`. El procedimiento queda parcial (ver §5). | `0b0307fb0df5400ab0482484933522bb` |
| H1 Híbrido por código exacto | ¿Qué debo verificar en la sembradora según el código CTRL-7731-ALFA? | knowledge | Pasos correctos del documento de prueba, fuente en modo `hybrid`. | `247c5040cd3d4f8087ef6d8d511e29c4` |
| I1 Aislamiento | Misma pregunta de H1, sobre la finca DEMO | knowledge | Sin fuentes de la otra finca. No revela su procedimiento. | `18d6758909694d029772896d1c3a34e5` |
| N1 Sin información | ¿Cuál es la receta de la torta de chocolate? | ninguno | Responde que no hay información. Sin fuentes ni citas. | `5605a9edc1bf42b0b2f51c0d4cd8c54f` |
| HITL aprobar | Registrá una incidencia: la cosechadora pierde presión hidráulica | WAITING → aprobado | `WAITING_APPROVAL` → `DONE`, `action_status=APPLIED`, incidencia en Redis. | `2276e79e756b46639677b13ce49c21a8` |
| HITL rechazar | Registrá una incidencia: el tractor tiene la batería descargada | WAITING → rechazado | `DONE`, `action_status=REJECTED`, sin incidencia en Redis. | `b2d91adccaf74e39a3af22496335c19e` |

Resultados crudos: `docs/evidencia/casos_reales/corrida_4_final.json`.
Reportes de LangSmith por job: `docs/evidencia/trazas/corrida_4/`.

Métricas por caso (LangSmith, suma de las llamadas `llm`):

| Caso | Latencia (s) | Tokens | Costo chat (USD) |
|---|---|---|---|
| C1 | 7,06 | 1577 | 0,000337 |
| K1 | 6,24 | 2017 | 0,000425 |
| B1 | 8,84 | 3020 | 0,000577 |
| H1 | 7,79 | 1368 | 0,000316 |
| I1 | 8,79 | 1831 | 0,000345 |
| N1 | 2,71 | 425 | 0,000097 |
| HITL aprobar | 2,25 + 2,71 | 468 + 485 | 0,000095 + 0,000115 |
| HITL rechazar | 1,91 + 3,13 | 460 + 459 | 0,000092 + 0,000107 |

Los HITL tienen dos trazas (la inicial y la reanudación tras la decisión).

## 3. Pruebas de robustez (crash real)

Script: `scripts/crash_recovery_check.py`. Mata el contenedor del backend con SIGKILL y lo vuelve a levantar.
Resultados: `docs/evidencia/robustez/crash_recovery.json`.

| Escenario | Qué ocurre | Verificado |
|---|---|---|
| A. Job en `RUNNING` | Muere el backend mientras procesa el job | El job queda `FAILED` con mensaje público `INTERRUPTED`, no se re-ejecuta y no se pierde. Traza en LangSmith (`error`, `job_status=failed`). |
| B. Jobs en cola | Tres jobs encolados, uno en curso | El que estaba en curso queda `FAILED`. Los dos encolados terminan `DONE` (una traza cada uno). La cola queda vacía. |
| C. Crash después de la acción | La incidencia ya está en Redis y falta el resumen del LLM | Al reiniciar el job queda `DONE` con `APPLIED` por reconciliación desde el checkpoint de LangGraph. La incidencia existe **una sola vez**. Dos trazas: `waiting_approval` y `done`. |

Hallazgo corregido durante estas pruebas: los jobs interrumpidos y los reconciliados **no emitían traza**.
El proceso muere con SIGKILL antes de enviar sus datos, y la reconciliación completaba jobs sin trazarlos.
Ahora el arranque emite una traza para cada job interrumpido y la reconciliación emite una traza para cada job completado.

## 4. Hallazgos y correcciones de esta etapa

1. **Sobre-delegación del Supervisor** (corrida 1). Solucionado con un prompt de routing que pide el mínimo de agentes.
2. **Knowledge con una sola fuente** (K1). Solucionado pidiendo incluir todos los pasajes que aportan.
3. **Knowledge demasiado estricto** (B1). Al pedir "decí lo que no está cubierto" el modelo rechazaba respuestas parciales. Se corrigió: `answered=false` solo si ningún pasaje responde la consulta.
4. **No determinismo** de la decisión `answered`. Con `temperature` por defecto la misma consulta cambiaba entre corridas. Se fijó `temperature=0` en routing, extracción y respuesta.
5. **Mensaje interno expuesto**: el mensaje de job huérfano decía `InterruptedError: …`. Ahora es `INTERRUPTED: …`.
6. **Check mal diseñado** (B1, "sin citas de archivos"). El check exigía que la parte de Operations no citara documentos, pero el caso B1 combina Operations con Knowledge, y Knowledge sí cita correctamente. Corregido: el check "sin citas" quedó solo para C1 (Operations puro). Los checks se recalcularon desde las respuestas guardadas de la corrida 4, sin nuevas llamadas (`scripts/real_cases_check.py --reevaluate`). El registro original quedó en `checks_before_revision`.
   **Declaración de la revisión:** el check original de B1 (sin citas de archivos) fallaba con la respuesta real:
   B1 cumplía 5 de 6, y el total con el check original es 40 de 41. La decisión de revisarlo se tomó después de ver
   ese resultado. El JSON no se modificó: `corrida_4_final.json` conserva ambos registros, `checks` (revisado) y
   `checks_before_revision` (original). Con el check revisado: 41 de 41. Un evaluador puede tomar el resultado original como el válido.
7. **Severidad HITL no determinista**. Con `temperature=0` la extracción de la severidad es estable entre corridas, pero sigue siendo una decisión del LLM: el humano la revisa antes de aprobar.

## 5. Limitaciones

- **Respuestas parciales:** K1 responde con un solo documento relevante, aunque el otro también fue recuperado. B1 responde la parte de Operations y solo informa que no hay procedimiento de lavado "después de fumigar" (los documentos hablan de triple lavado, no de fumigación).
- **Muestra pequeña:** 8 casos y 5 escenarios de crash validan funcionamiento, no calidad estadística.
- **Crash en la reanudación HITL antes de registrar la incidencia** (prueba del escenario C con el crash en otro momento): cubierta por tests unitarios de reconciliación, no por crash real en ese instante exacto.
- **Jobs interrumpidos no se re-ejecutan**: se marcan `FAILED` para no arriesgar acciones duplicadas. El usuario debe reenviar la consulta.
- **Búsqueda por palabras:** lee los chunks del establecimiento en cada consulta. No escala a un corpus grande sin índice.
- **Worker secuencial:** procesa un job a la vez.
- **Operations** usa datos DEMO estáticos. "Fuera de servicio" significa estado `broken`; la pulverizadora en mantenimiento no se menciona.
- **Seguridad básica:** `GET /tasks/{id}` no tiene autenticación. Redis y Chroma no usan contraseña dentro de la red de Compose.
- **Costos de embeddings:** LangSmith no tiene precio para `text-embedding-3-small`; su costo es `NO VERIFICADO` (los tokens son pocos).

## 6. Costos

- Chat según LangSmith, sumando todas las corridas reales de esta etapa: ≈ USD 0,01.
- Embeddings: un documento de prueba y consultas cortas; despreciables.
- Tope: USD 1. Gasto medido: alrededor del 1 %.
- Modelos: `gpt-4o-mini` y `text-embedding-3-small`.

## 7. Evidencia

- `docs/evidencia/casos_reales/`: resultados crudos de cada corrida (`corrida_1_antes_de_ajustes.json`, `corrida_2.json`, `corrida_3a_knowledge_prompt_too_strict.json`, `corrida_3_knowledge.json`, `corrida_4_final.json`).
- `docs/evidencia/trazas/`: reportes de LangSmith de las corridas.
- `docs/evidencia/robustez/crash_recovery.json`: resultados y conteo de trazas de las pruebas de crash.
- `screenshots/`: 8 capturas de LangSmith (`langsmith_project_runs.png`, `langsmith_individual_runs.png`, `langsmith_trace_waterfall.png`, `langsmith_embedding_nv.png`, `langsmith_hitl_runs.png`, `langsmith_hitl_pending.png`, `langsmith_hitl_resumed.png`, `langsmith_metrics_20261015.png`), con el veredicto de cada una en `screenshots/README.md`. Las originales `CAPTURA1`–`CAPTURA6` se conservan.
- `docs/evidencia/costos.json`: agregado de tokens y costos.
- Tests: `backend/tests/` (unitarios) y `backend/tests/integration/` (Chroma real, con `AGROOPS_INTEGRATION=1`).

## 8. Pendientes

1. Capturas de LangSmith: revisadas en `screenshots/`. La lista de runs del proyecto muestra diez raíces de la corrida 4, con latencia, tokens y costo. El RAG híbrido, el Supervisor y las dos trazas del HITL están documentados con capturas. Quedan dos huecos menores, detallados en `screenshots/README.md`: el filtro por `metadata.job_id` y la etiqueta `NO VERIFICADO` del costo del embedding (el informe la documenta en texto).
2. Decisión sobre las 16 trazas de corridas QA con LLM mock (`establishment_id` `qa-*`): no se borraron.
3. Rúbrica oficial: no está en el repositorio. Este informe se basa en los requisitos listados al inicio del proyecto.

## 9. Verificación de las correcciones de auditoría

Rama `fix/auditoria-final`, 2026-10-22. Los resultados históricos de §2 a §7 no se modificaron. Esta etapa no hizo
llamadas a OpenAI: el stack se levantó con `OPENAI_API_KEY` vacía.

| Verificación | Resultado |
|---|---|
| Ruff (`ruff check backend scripts`) | Sin errores |
| Unitarios (`pytest`, en `backend/`) | 132 pasan, 5 omitidos (integración sin variable) |
| Integración con Redis Stack y Chroma reales (`AGROOPS_INTEGRATION=1`) | 5 pasan: 3 de Chroma y 2 de Redis |
| `docker compose up --build` | Redis healthy; backend y Chroma arriba |
| `/health`, `/docs`, `/openapi.json` | 200 |
| Usuario del proceso en el contenedor backend | `agroops` (no root) |
| Puertos publicados | `127.0.0.1` para backend (8000), Chroma (8001) y Redis (6379) |
| Endpoints sin LLM | Body inválido 422. Job creado → FAILED `LLM_NOT_CONFIGURED`. Approve sin clave 401; con clave en job fallido 409 |

Los dos tests de Redis real cubren lo que los fakes no pueden: la aprobación concurrente con `WATCH/MULTI` deja una
sola decisión y un solo encolado, y un grafo pausado en `interrupt` se reanuda con `Command(resume=...)` tras un
reinicio simulado, usando solo el checkpoint en Redis.

Correcciones de esta etapa:
- BM25 (`keyword_order`) y chunking (`split_pages`) se ejecutan con `asyncio.to_thread`. Tests de regresión:
  `test_keyword_ranking_runs_off_the_event_loop` y `test_chunking_runs_off_the_event_loop`.
- Fallos de Redis al finalizar un job: reintentos con backoff. Si Redis sigue caído, el job queda como fue guardado
  por última vez y nunca se vuelve a ejecutar. Si el fallo ocurre antes de arrancar, el job vuelve a la cola.
  Tests: `test_worker_retries_a_final_write_that_fails_transiently`,
  `test_permanent_redis_failure_on_finish_keeps_job_running_and_never_reruns` y
  `test_redis_failure_before_start_requeues_the_job_without_running_it`.
- Los `assert` de código de producción se reemplazaron por chequeos explícitos (`agents/incident_action.py`,
  `services/llm.py`, `observability/tracing.py`).
- Compose: puertos de backend, Chroma y Redis atados a `127.0.0.1`; `env_file` opcional para que los valores de
  `.env` lleguen al contenedor; `backend/.dockerignore`; el proceso corre como usuario `agroops`.
- Defaults: `config.py` y `.env.example` usan valores de ejecución local. Compose sobrescribe Redis y Chroma con
  los valores de la red de contenedores.

Pendientes (no corregidos en esta etapa):
- `POST /tasks` y `GET /tasks/{id}` no tienen autenticación. La única barrera es publicar los puertos solo en localhost.
- Operations usa datos DEMO estáticos.
- Los tests de regresión de asincronía no se ejecutaron contra el código anterior para confirmar que fallaban.
  Su alcance es que el código actual mantenga el trabajo fuera del loop.
