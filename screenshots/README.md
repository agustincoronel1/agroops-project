# Evidencia de LangSmith (capturas manuales)

Las capturas requieren el login de LangSmith, así que no se generan automáticamente.
No deben mostrar la API key, configuración de cuenta, miembros del equipo ni páginas fuera del
proyecto `agroops-intelligence`.

Estado al 2026-10-21: **evidencia suficiente para la rúbrica de observabilidad**, con dos huecos
menores documentados abajo. Las capturas originales (`CAPTURA1`…`CAPTURA6`, del 15/10) se conservan.

## Capturas de la corrida final (21/10)

Todas corresponden a los jobs de `docs/evidencia/casos_reales/corrida_4_final.json`.

| Archivo | Qué demuestra | Requisito que cubre | Veredicto |
|---|---|---|---|
| `langsmith_project_runs.png` | Lista de traces del proyecto (última 1 día) con 10 raíces `agroops.job`: C1, K1, B1, H1, I1, N1 y los dos HITL (aprobado con `APPLIED`, rechazado con `REJECTED`). Columnas de latencia, tokens y costo. | Al menos 5 ejecuciones con trazabilidad, latencia, tokens y costo | **Correcta.** Es la evidencia principal. |
| `langsmith_individual_runs.png` | Lista de runs filtrada por `name:agroops.job` (90 runs). Muestra cada ejecución individual con su latencia y su resultado. Las columnas de tokens y costo aparecen vacías en esta vista. | Trazabilidad por ejecución | **Correcta, complementaria.** Las columnas de tokens y costo están en `langsmith_project_runs.png`. |
| `langsmith_trace_waterfall.png` | Thread del job H1 (`247c5040…`): supervisor, `knowledge.retrieve`, `openai.embeddings`, `openai.chat`. Resumen 7,79 s, 1.368K tokens, costo $0,0003159. Output del knowledge con fuente `procedimiento_control_sembradora.md`, `retrieval: "hybrid"`, distancia 0,186. Metadata `job_id`, `establishment_id`, `resumed: false`. | Supervisor, RAG híbrido, latencia, tokens, costo | **Correcta.** Muestra el RAG híbrido en la salida real. |
| `langsmith_embedding_nv.png` | Embedding `text-embedding-3-small` del mismo job: 21 tokens y latencia 0,51 s. | Tokens reales de embeddings | **Parcial.** El campo de costo aparece vacío, sin la etiqueta `NO VERIFICADO`. Ver huecos. |
| `langsmith_hitl_runs.png` | Runs de los dos HITL (aprobación y rechazo): `approval` con `GraphInterrupt`, `prepare_incident`, `register_incident`, y costo por llamada `openai.chat` (por ejemplo $0,00004965 y $0,0000570). | HITL, costo por llamada | **Correcta.** El `GraphInterrupt` en `approval` es la pausa esperada de LangGraph, no un error de ejecución. |
| `langsmith_hitl_pending.png` | Primera traza del job HITL aprobado (`2276e79e…`): `job_status: waiting_approval`, `resumed: false`, misma `thread_id`. | HITL: pausa | **Correcta.** |
| `langsmith_hitl_resumed.png` | Segunda traza del mismo job: `job_status: done`, `resumed: true`, misma `job_id`. Agrupadas en un solo thread. | HITL: reanudación | **Correcta.** Juntas con la anterior demuestran las dos trazas del HITL. |

## Captura adicional (15/10)

| Archivo | Qué demuestra | Veredicto |
|---|---|---|
| `langsmith_metrics_20261015.png` | Lista de 26 traces del 15/10 (prueba de carga): ocho `agroops.job` con latencia, tokens y costo. | **Complementaria.** Da una segunda serie de ejecuciones trazadas, de una sesión anterior. |

## Capturas originales (15/10, conservadas)

| Archivo | Qué demuestra |
|---|---|
| `CAPTURA1.png` | Root `agroops.job` del job `09ab6ab1…`: latencia, tokens, costo y Output del root; waterfall con los nodos. |
| `CAPTURA2.png`, `CAPTURA3.png` | Salida de `knowledge.retrieve` del mismo job. |
| `CAPTURA4.png` | Salida JSON de `knowledge.retrieve` con tags y metadata. |
| `CAPTURA6.png` | Swagger `GET /tasks/09ab…` con `DONE`. Integración FastAPI, no LangSmith. |

## Huecos

1. **Filtro por `metadata.job_id` (checklist #7): falta.** La vista de thread de los dos HITL ya agrupa las
   dos trazas por `job_id` y `thread_id`, así que el requisito queda cubierto en lo esencial. Una captura del
   filtro lo haría explícito, pero no es imprescindible.
2. **Costo del embedding como `NO VERIFICADO` (checklist #6): parcial.** LangSmith no tiene precio para
   `text-embedding-3-small`. La captura muestra los tokens reales, pero no la etiqueta. El informe lo documenta
   como `NO VERIFICADO` en el texto. Si la rúbrica exige ver la etiqueta en la interfaz, es una captura más.

## Retención

LangSmith conserva las trazas 14 días en este proyecto. Las capturas de la corrida final son la evidencia
visual; los reportes exportados en `docs/evidencia/trazas/` son la copia permanente de los datos.

## Checklist de referencia (del 15/10)

Los nombres de esta tabla son los que el checklist original esperaba. Los que dicen "Falta" no existen
como archivo en esta carpeta. Los IDs apuntan a trazas de la corrida del 15/10, que siguen en LangSmith.
El estado de cada ítem es el de la tabla de arriba.

| # | Archivo esperado | Estado |
|---|---|---|
| 1 | `langsmith_project_runs.png` | Cumplido |
| 2 | `langsmith_root_output_job_B.png` | Cubierto por `CAPTURA1.png` (otro job) |
| 3 | `langsmith_trace_waterfall.png` | Cumplido (`langsmith_trace_waterfall.png`) |
| 4 | `langsmith_hitl_threads.png` | Cumplido por `langsmith_hitl_pending.png` y `langsmith_hitl_resumed.png` |
| 5 | `langsmith_llm_call.png` | Cumplido por `langsmith_hitl_runs.png` (costo por llamada) |
| 6 | `langsmith_embedding_nv.png` | Parcial (ver huecos) |
| 7 | `langsmith_metadata_filter.png` | Falta (hueco menor) |
| 8 | `langsmith_failed_job.png` (opcional) | Falta. Hay una traza fallida de los tests de crash, job `e6a481f35d92432388921f4365ea2ed4`. |

## Después de capturar

1. Verificar que ninguna captura muestre la API key, la key de aprobación ni configuración de cuenta.
2. Referenciar los archivos en el README y en `docs/validacion_final.md`.
