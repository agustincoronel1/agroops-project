# Validación final de AgroOps

Fecha: 2026-10-21. Stack: `docker compose up --build` (backend FastAPI, Redis Stack, ChromaDB).
Modelos: `gpt-4o-mini` (routing, respuestas, extracción HITL) con `temperature=0`, y
`text-embedding-3-small` (embeddings). Trazas: LangSmith, proyecto `agroops-intelligence`.

## 1. Resumen

| Área | Resultado |
|---|---|
| Casos reales con OpenAI (8 casos, corrida final) | **41 de 41 checks** tras corregir un check mal diseñado (ver §4). Recalculado desde las respuestas guardadas, sin nuevas llamadas. |
| Pruebas de crash real (SIGKILL del backend) | **14 de 14 checks**. Sin jobs perdidos ni acciones duplicadas. |
| Tests unitarios | 125 pasan, 3 omitidos (integración sin variable). |
| Tests de integración con Chroma real | 3 pasan. |
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
6. **Check mal diseñado** (B1, "sin citas de archivos"). El check exigía que la parte de Operations no citara documentos, pero el caso B1 combina Operations con Knowledge, y Knowledge sí cita correctamente. Corregido: el check "sin citas" quedó solo para C1 (Operations puro). Los checks se recalcularon desde las respuestas guardadas de la corrida 4, sin nuevas llamadas (`scripts/real_cases_check.py --reevaluate`). El registro original quedó en `checks_before_revision`. Resultado: 41 de 41.
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
- `screenshots/`: capturas de LangSmith de la corrida final (`langsmith_project_runs.png`, `langsmith_trace_waterfall.png`, `langsmith_hitl_*.png`, etc.), con el veredicto de cada una en `screenshots/README.md`.
- `docs/evidencia/costos.json`: agregado de tokens y costos.
- Tests: `backend/tests/` (unitarios) y `backend/tests/integration/` (Chroma real, con `AGROOPS_INTEGRATION=1`).

## 8. Pendientes

1. Capturas de LangSmith: revisadas en `screenshots/`. La lista de runs del proyecto muestra diez raíces de la corrida 4, con latencia, tokens y costo. El RAG híbrido, el Supervisor y las dos trazas del HITL están documentados con capturas. Quedan dos huecos menores, detallados en `screenshots/README.md`: el filtro por `metadata.job_id` y la etiqueta `NO VERIFICADO` del costo del embedding (el informe la documenta en texto).
2. Decisión sobre las 16 trazas de corridas QA con LLM mock (`establishment_id` `qa-*`): no se borraron.
3. Rúbrica oficial: no está en el repositorio. Este informe se basa en los requisitos listados al inicio del proyecto.
