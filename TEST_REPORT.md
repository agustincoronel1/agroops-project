# AgroOps Intelligence System — Informe de auditoría

Fecha de ejecución: 2026-10-08 · Plataforma: Windows 11 + Docker Desktop 29.6.2 · Commit auditado: `e1cb105` (+ cambios sin commitear: solo archivos de auditoría).

**Alcance y reglas respetadas:** no se modificó código productivo, no se hizo commit/push, no se borraron volúmenes ni datos existentes (solo se eliminaron los chunks de prueba `establishment_id=audit-b` creados por la auditoría; `demo` quedó intacto con 6 chunks). La API key de `.env` se usó sin imprimirla. Archivos agregados: `scripts/audit_api.py`, `scripts/audit_rag.py`, `backend/tests/test_graph_extra.py` y la carpeta `audit_out/` (salidas crudas = evidencia).

**Desvío de presupuesto (declarado):** el límite era 8 tareas reales con LLM. Se iniciaron **9**: T1–T6, T8 (7 completadas), T7 (interrumpida a propósito por un reinicio, facturación desconocida) y una tarea `"ping"` posterior al reinicio (no se inspeccionó su resultado; el contenedor se recreó después). Además hubo 6 tareas con modelo inexistente (rechazadas por OpenAI con 404, sin inferencia) y ~25 llamadas de embeddings.

---

## 1. Resumen ejecutivo

| Área | Estado |
|---|---|
| Tests automáticos (mock) | 39/39 PASS |
| Stack Docker real (backend + Redis + Chroma) | PASS (con hallazgos de persistencia de Redis) |
| Flujo real extremo a extremo con OpenAI + Chroma + Redis | PASS funcional, con debilidades de routing y umbral |
| Aislamiento por `establishment_id` en RAG | PASS (real) |
| Aislamiento en Operations Agent | **FAIL** (no usa `establishment_id`) |
| Recuperación de jobs RUNNING huérfanos | **FAIL** (bug confirmado) |
| Redis caído en `/tasks` | **FAIL** (500 sin controlar) |
| RAG avanzado (híbrido, re-ranking) | NO IMPLEMENTADO |

---

## 2. Tabla de pruebas

Leyenda: **mock** = sin servicios reales · **real** = Docker + Redis + Chroma (+ OpenAI cuando se indica).

| # | Prueba | Tipo | Resultado | Evidencia |
|---|---|---|---|---|
| 1.1 | `pytest` completo | mock | **PASS** | 39 passed (34 previos + 5 nuevos de auditoría), 2.8 s; `audit_out/pytest.txt` |
| 1.2 | `pip check` host / contenedor | real | **PASS** | "No broken requirements found" (ambos) |
| 1.3 | Import de todos los módulos `app.*` | real | **PASS** | 0 fallos de import |
| 2.1 | `docker compose config` | real | **PASS** | exit 0 |
| 2.2 | `docker compose build --no-cache backend` | real | **PASS** | 43 s |
| 2.3 | Arranque + health + Swagger | real | **PASS** | `/health` 200 (15 ms) `{"status":"ok",...,"redis":"ok"}`; `/docs` 200; `/openapi.json` 200 con `/health`, `/tasks`, `/tasks/{job_id}`, `/` |
| 2.4 | Apagado y reinicio (`restart`, `down`/`up`) | real | **PASS** (stack) / ver 10.x (datos) | servicios vuelven healthy; sin errores en logs del backend |
| 3.1 | 422: input vacío, faltante, muy largo (>5000), `establishment_id` inválido (`../etc`), body no-objeto | real | **PASS** | 5/5 casos devuelven 422 con detalle Pydantic (`audit_out/errors.txt`) |
| 3.2 | 404: job inexistente y ruta inexistente | real | **PASS** | `{"detail":"Job doesnotexist not found"}` |
| 3.3 | Redis caído → `/health` | real | **PASS** | 503 `{"status":"degraded","redis":"unavailable"}` en 7 ms |
| 3.4 | Redis caído → `POST /tasks` y `GET /tasks/{id}` | real | **FAIL** | 500 "Internal Server Error" (3.56 s en POST) + traceback en logs (BUG-02) |
| 3.5 | Recuperación tras volver Redis | real | **PASS** | `/health` 200 a los segundos; worker siguió procesando (job posterior DONE/FAILED según prueba 4.3) |
| 4.1 | `POST /tasks` → 202 + `job_id` inmediato | real | **PASS** | 202 en 25–93 ms, `status: PENDING` en la respuesta |
| 4.2 | `GET /tasks/{id}` y persistencia en Redis | real | **PASS** | job consultable; tras `restart` de Redis+backend el job DONE sigue disponible |
| 4.3 | Estados PENDING/RUNNING/DONE/FAILED | real | **PASS (parcial)** | RUNNING→DONE observado por polling (p. ej. T1: RUNNING 0.08 s → DONE 7.46 s); RUNNING→FAILED observado con modelo inválido. **PENDING solo visto en la respuesta del POST**; observarlo vía GET durante cola real: **NO VERIFICADO** (el worker lo toma en ms). Transición PENDING→RUNNING→DONE cubierta por tests mock |
| 4.4 | FAILED con error controlado (OpenAI real rechaza el modelo) | real | **PASS** | `FAILED`, `error: "NotFoundError: Error code: 404 ... model_not_found"` (1.52 s) |
| 5.1 | Cola real, 5 POST concurrentes | real | **PASS** | 5×202, 5 `job_id` únicos, POST 25–93 ms (pared 93 ms) |
| 5.2 | Procesamiento de 5 jobs concurrentes | real (LLM rechazado) | **PASS (acotado)** | los 5 terminan FAILED con error controlado; tiempos de fin 0.68/1.20/1.64/1.86/2.19 s ⇒ **procesamiento secuencial** (1 worker). **Concurrencia con LLM exitoso: NO VERIFICADO** (se evitó por costo) |
| 6.1 | State/reducers (`add_messages`) | mock | **PASS** | `test_messages_reducer_appends_and_dedups_by_id` |
| 6.2 | Routing condicional, prevención de loops, `max_steps`, `recursion_limit` | mock | **PASS** | `test_no_infinite_loop...`, `test_max_steps...`, `test_zero_max_steps...`, `test_recursion_limit_is_a_hard_stop` |
| 6.3 | Excepciones en nodos → job FAILED | mock + real | **PASS** | `test_agent_exception_propagates`, `test_graph_failure_marks_job_failed`; real: prueba 4.4 |
| 6.4 | `messages` del State realmente usado | mock | **FAIL (menor)** | el campo existe pero ningún nodo lo escribe (BUG-09) |
| 7.1 | Supervisor → solo Operations | real (LLM) | **PASS** | T1: `agents_consulted=["operations"]`, respuesta correcta con datos DEMO (Cosechadora rota, incidencia I1) |
| 7.2 | Supervisor → solo Knowledge | real (LLM) | **FAIL (no demostrado)** | T2, T4, T5, T8 (preguntas documentales) consultaron **ambos** agentes, Knowledge primero. Ninguna corrida consultó solo Knowledge (BUG-07) |
| 7.3 | Supervisor → ambos | real (LLM) | **PASS** | T3: `["operations","knowledge"]`; `agents_consulted` coincide con lo ejecutado en todas las corridas |
| 7.4 | Latencia end-to-end con LLM | real | informativo | T1 7.5 s, T2 8.8 s, T3 9.0 s, T4 8.6 s, T5 6.5 s, T6 7.6 s, T8 7.3 s |
| 8.1 | Ingesta TXT, MD y PDF con texto real | real (Chroma + embeddings OpenAI) | **PASS** | TXT 4 chunks, MD 2, PDF 2 (1 por página); `chunk_len_max=798` (≤800) |
| 8.2 | Metadata y embeddings | real | **PASS** | claves: `chunk_id, content_hash, document_id, establishment_id, page, source`; `page` ∈ {1,2} solo en PDF; embedding de 1536 dim |
| 8.3 | Idempotencia | real | **PASS** | reingesta: todo `skipped`, 8 chunks antes y después; archivo modificado reemplaza chunks (4→1, sin residuos); además el arranque con docs demo no duplicó (demo=6 antes y después de `down/up`) |
| 8.4 | Recuperación + fuentes verificables | real | **PASS** | "código de acceso del silo": 1.º resultado `finca_b_manual.pdf` **p.2**, dist 0.395, contiene el dato; T6 respondió `ZETA-4471` citando `[finca_b_manual.pdf, p.2]` |
| 9.1 | Aislamiento por `establishment_id` (retriever) | real | **PASS** | la consulta del secreto en `demo` devuelve solo docs `demo`; consulta de docs demo en `audit-b` devuelve solo docs de `audit-b`; establecimiento inexistente → `[]` |
| 9.2 | Aislamiento end-to-end (T5) | real (LLM) | **PASS** | desde `demo`, "código del silo de la Finca B" → "no se ha encontrado información"; el secreto no se filtró |
| 9.3 | Sin resultados / no inventar | real | **PASS (con reservas)** | "capital de Francia" → 0 chunks. T4 (glifosato) y T5: el LLM declinó correctamente. **Reserva:** milanesas→1 chunk (dist 0.739) y glifosato→4 chunks (0.53–0.62) pasaron el umbral 0.75 (BUG-06); la no-alucinación depende del prompt |
| 10.1 | Chroma persiste tras `down`/`up` (volumen nombrado `agroops-project_chroma_data`) | real | **PASS** | demo=6, audit-b=5 antes y después |
| 10.2 | Jobs en Redis tras `restart` | real | **PASS** | job DONE anterior sigue en Redis |
| 10.3 | Jobs en Redis tras `down`/`up` | real | **FAIL** | el mismo job → 404. Redis usa volumen **anónimo**, `appendonly no` (BUG-04) |
| 10.4 | Job RUNNING huérfano tras reinicio | real | **FAIL** | T7: RUNNING antes del `restart`; tras 10 s y 25 s sigue `RUNNING`, sin error, `updated_at` congelado (BUG-01) |
| 11.1 | Filtros de metadata | revisión de código | **PARCIAL** | solo filtro obligatorio por `establishment_id` + umbral de distancia |
| 11.2 | Búsqueda híbrida (BM25/keyword + vector) | revisión de código | **NO IMPLEMENTADO** | solo similitud vectorial |
| 11.3 | Re-ranking | revisión de código | **NO IMPLEMENTADO** | — |
| 12.1 | Secretos | real | **PASS** | `.env` ignorado y no versionado; `.env.example` sin claves; sin patrones `sk-...` en el repo (escaneo) |
| 12.2 | Higiene de repo | real | **FAIL (menor)** | 2002 archivos de `.venv/` y `__pycache__` versionados (BUG-05) |
| 12.3 | `.env`/README | real | **PASS con observaciones** | `.env` local tiene `OPENAI_API_KEY` y `OPENAI_MODEL` duplicados (BUG-10); README coherente con lo probado |

---

## 3. Comandos y resultados clave

```text
# Tests
cd backend && ../.venv/Scripts/python.exe -m pytest -q          -> 39 passed in 2.81s
pip check                                                        -> No broken requirements found
docker compose config -q                                         -> OK
docker compose build --no-cache backend                          -> 43 s, OK
docker compose up -d ; curl /health /docs /openapi.json /       -> 200 / 200 / 200 / 200

# Scripts reproducibles (stack levantado, raíz del repo)
.venv/Scripts/python scripts/audit_api.py errors
.venv/Scripts/python scripts/audit_api.py job "<texto>" [establishment_id]
.venv/Scripts/python scripts/audit_api.py concurrent 5
PYTHONPATH=backend .venv/Scripts/python scripts/audit_rag.py [--keep]
```

### Requests/responses relevantes

```text
POST /tasks {"user_input":""}                         -> 422 string_too_short
POST /tasks {"user_input":"x","establishment_id":"../etc"} -> 422 string_pattern_mismatch
GET  /tasks/doesnotexist                              -> 404 {"detail":"Job doesnotexist not found"}
(Redis detenido) GET /health                          -> 503 {"status":"degraded","service":"AgroOps Intelligence System","version":"0.1.0","redis":"unavailable"}
(Redis detenido) POST /tasks                          -> 500 "Internal Server Error"  (3.56 s)

POST /tasks {"user_input":"Que maquinaria esta fuera de servicio?"} (modelo inválido)
  -> 202 {"job_id":"c100...","status":"PENDING"}  (66 ms)
  -> FAILED, error: "NotFoundError: Error code: 404 - {... model_not_found}"  (RUNNING 0.07 s -> FAILED 1.52 s)

POST /tasks (establishment_id "audit-b") "codigo de acceso del silo y cada cuantas horas se revisa la bomba QX-900?"
  -> DONE, agents_consulted ["knowledge","operations"],
     sources: finca_b_manual.pdf p.2 (dist 0.19), p.1 (0.58), finca_b_procedimientos.md (0.59, 0.63)
     answer: "... ZETA-4471 ... cada 77 horas de uso [finca_b_manual.pdf, p.2]"
```

### Latencias de concurrencia (5 POST simultáneos)

| Métrica | Valor |
|---|---|
| Latencia POST por request | 93, 47, 25, 46, 50 ms (pared total 93 ms) |
| Estados HTTP | 5×202, 5 `job_id` únicos |
| Tiempo hasta terminal de cada job (contados desde el inicio del polling) | 0.68, 1.20, 1.64, 1.86, 2.19 s → ≈0.4–0.5 s por job, **acumulativo = ejecución en serie** |
| Nota | Los 5 jobs terminaron FAILED a propósito (modelo inexistente) para no gastar tokens. Con LLM real (~7–9 s por job) el último de 5 esperaría ≈35–45 s **(extrapolación, NO VERIFICADO)** |

---

## 4. Tests pasados/fallidos

- `pytest`: **39 passed, 0 failed** (mock). Nuevos en `tests/test_graph_extra.py`: reducer de mensajes, `max_steps=0`, hard stop por `recursion_limit`, propagación de excepciones, y documentación de que `messages` no se escribe.
- Pruebas reales: ver tabla (FAIL en 3.4, 6.4, 7.2, 10.3, 10.4, 12.2).
- Los tests con mocks usan un `FakeCollection`/embedder falsos: **no** validan el comportamiento interno de Chroma; eso se cubrió solo con las corridas reales de la sección 8–9.

---

## 5. Bugs y hallazgos por gravedad

**Alta**
- **BUG-01 — Jobs RUNNING huérfanos nunca se recuperan.** Si el backend/worker se reinicia durante un job, queda `RUNNING` para siempre (confirmado en T7: sigue RUNNING a los 25 s, sin error). El cliente que consulta nunca ve un estado final. *Propuesta (no aplicada):* al arrancar, marcar como FAILED/reencolar jobs `RUNNING` sin worker activo, o heartbeat + timeout.

**Media**
- **BUG-02 — Redis caído ⇒ `POST/GET /tasks` responden 500 sin controlar** (traceback en logs, 3.5 s de demora). Debería ser 503 con mensaje claro como `/health`.
- **BUG-03 — Operations Agent ignora `establishment_id`.** Siempre usa los datos DEMO fijos; para cualquier establecimiento (p. ej. `audit-b`) esos datos se envían al LLM. Rompe el aislamiento prometido para datos operativos (hoy mitigado solo porque son DEMO).
- **BUG-04 — Redis sin persistencia durable.** Volumen anónimo y `appendonly no`: tras `docker compose down`/`up` se pierden todos los jobs (verificado: 404). Con `restart` sí persisten.
- **BUG-05 — Repo sucio.** `.venv/` (2002 archivos) y `__pycache__/` están versionados aunque `.gitignore` ya los lista (se agregaron antes del `.gitignore`). Requiere `git rm -r --cached`.
- **BUG-06 — Umbral de relevancia laxo (`RAG_MAX_DISTANCE=0.75`).** Consultas irrelevantes aún recuperan chunks (0.53–0.74), y `sources` lista los chunks recuperados, no los que sustentan la respuesta (T4/T5 mostraron fuentes que no respaldan nada). El LLM declinó correctamente en T4/T5, pero la protección anti-alucinación descansa en el prompt.

**Baja**
- **BUG-07 — Routing del Supervisor poco selectivo.** En 5 de 7 corridas consultó ambos agentes aun con preguntas puramente documentales; nunca se observó "solo Knowledge" (LLM no determinista; solo 7 muestras).
- **BUG-08 — Worker secuencial**: un job lento bloquea la cola (head-of-line blocking); medido en prueba 5.2.
- **BUG-09 — `messages` del State no se usa** (declarado, nunca escrito).
- **BUG-10 — Configuración:** `.env` local con claves duplicadas (`OPENAI_API_KEY`, `OPENAI_MODEL`); `.env.example` fija `CHROMA_HOST=chroma`, lo que rompe scripts desde el host si se copia tal cual.
- **BUG-11 — Errores del proveedor en `job.error`:** se persiste/expone el mensaje crudo de OpenAI (puede revelar nombre de modelo/organización).
- **BUG-12 — Observabilidad mínima:** los logs INFO de la app no se ven (logging no configurado): la ingesta demo al arranque no deja rastro; `/health` no refleja Chroma ni OpenAI.
- **Sin TTL** de jobs en Redis, sin autenticación ni rate limiting en la API.
- Estilo de respuestas: saludo/firma redundantes ("Estimado usuario... Supervisor de AgroOps") por el prompt de síntesis.

---

## 6. NO VERIFICADO

- Extracción de PDFs reales complejos/escaneados (solo un PDF de texto generado a mano; PDF en blanco en tests).
- Concurrencia con LLM exitoso (5 jobs reales simultáneos) y comportamiento ante rate limits de OpenAI.
- Observación de `PENDING` vía `GET` con cola ocupada.
- Resultado y facturación de la tarea T7 (interrumpida) y de la tarea `"ping"`.
- Comportamiento de Chroma bajo carga o caída de Chroma durante una consulta (no se probó detener Chroma).
- Cold start desde máquina limpia (se reconstruyó solo la imagen del backend con `--no-cache`; las imágenes de Redis y Chroma ya estaban en caché).

---

## 7. Pendientes de la rúbrica

- **Human-in-the-Loop:** NO IMPLEMENTADO (excluido por indicación).
- **Checkpointer persistente de LangGraph** (Redis): NO IMPLEMENTADO; hoy el estado del grafo no sobrevive a un reinicio (relacionado con BUG-01).
- **Observabilidad** (trazas/métricas): NO IMPLEMENTADO.
- **RAG avanzado:** búsqueda híbrida y re-ranking NO IMPLEMENTADOS; filtros solo por establecimiento.
- **Tests de integración automatizados** (sin mocks) integrados al repo/CI: hoy son scripts manuales (`scripts/audit_*.py`).
- **Seguridad de API** (auth, rate limit): NO IMPLEMENTADO.
- Corregir BUG-01…BUG-06 antes de avanzar (requieren tu autorización para modificar código productivo).

---

## 8. Correcciones aplicadas (BUG-01 … BUG-06)

Estado tras la corrección: **51/51 tests (mock) PASS**; pruebas reales en Docker + Redis + Chroma en `audit_out/fix_*.txt`. Solo 2 tareas reales con LLM (una negativa, una positiva).

| Bug | Corrección | Prueba real | Resultado |
|---|---|---|---|
| BUG-01 huérfanos RUNNING | `JobService.fail_orphaned_running_jobs()` al arrancar, antes del worker: RUNNING → FAILED (`InterruptedError: ... not re-executed`), sin reencolar | Registro RUNNING inyectado en Redis real + `docker compose restart backend`: pasó a FAILED, job DONE intacto, cola vacía. **Limitación:** el huérfano fue *simulado* (no se mató un job real en vuelo) | PASS (con limitación) |
| BUG-04 Redis | volumen nombrado `redis_data` + `--appendonly yes` | job creado → `docker compose down` → `up`: sigue disponible (HTTP 200, FAILED) | PASS |
| BUG-03 Operations | datos demo solo para `demo`; otro establecimiento recibe "sin datos" sin llamar al LLM | en el contenedor: `get_snapshot('demo')`=snapshot, `finca-b`/`audit-b`=None. Extremo a extremo con LLM para otro establecimiento: **NO VERIFICADO** (por costo) | PASS (unitario real) / E2E NO VERIFICADO |
| BUG-02 Redis caído | handler global de `RedisError` → 503 `{"detail": "Job store (Redis) is temporarily unavailable..."}` | Redis detenido: POST 503 (9 ms), GET 503 (~4 s la primera vez por reintento DNS), sin traceback; recuperación: POST 202 | PASS |
| BUG-06 RAG | umbral **sin cambios** (0.75): en el corpus demo los rangos de distancia se solapan (positivas 0.29–0.64 vs negativas 0.51–0.94), cambiarlo perdería documentos relevantes. En su lugar el Knowledge Agent devuelve salida estructurada (`answered`, `supporting_passages`) y solo esos pasajes se informan como `sources`; si no sustentan, respuesta "sin información" y `sources=[]` | evaluación `scripts/eval_rag_relevance.py`: recall 11/11 antes y después. Tarea real negativa (glifosato): `sources=[]`, reconoce falta de info. Tarea real positiva (servicio tractor): 1 fuente (`maquinaria_demo.md`) en vez de 4 | PASS |
| BUG-05 repo | `git rm -r --cached` de `.venv` y `__pycache__`; `.gitignore` ampliado | 0 archivos `.venv`/`.pyc` en el índice; `.venv` local intacto | PASS |

Pendientes tras la corrección: ver el mensaje final de la sesión (citas `[pasaje N]`, solo 1 muestra positiva/negativa con LLM, juez de relevancia basado en LLM no determinista, huérfanos solo simulados, primer GET con Redis caído tarda ~4 s).
