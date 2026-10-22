"""Public error model.

Only a `ServiceError` carries text that may leave the service (job `error`, API responses).
Anything else is an internal failure: its details (hostnames, SDK messages, stack traces)
are logged server-side and replaced by a generic public message.
"""

from typing import ClassVar


class ServiceError(Exception):
    code: ClassVar[str] = "SERVICE_ERROR"
    public_message: ClassVar[str] = "Ocurrió un error al procesar la consulta."

    def __init__(self, detail: str | None = None) -> None:
        # `detail` is for logs and tests only; it is never part of the public message.
        super().__init__(detail or self.public_message)


class KnowledgeBaseUnavailable(ServiceError):
    code = "KNOWLEDGE_UNAVAILABLE"
    public_message = "La base documental no está disponible. Reintentá más tarde."


class LLMUnavailable(ServiceError):
    code = "LLM_UNAVAILABLE"
    public_message = "El servicio de lenguaje no respondió. Reintentá más tarde."


class LLMNotConfigured(ServiceError):
    code = "LLM_NOT_CONFIGURED"
    public_message = "OPENAI_API_KEY no está configurada en el servidor."


class EmbeddingDimensionMismatch(ServiceError):
    code = "EMBEDDING_DIMENSION_MISMATCH"
    public_message = (
        "El modelo de embeddings no coincide con la base documental indexada. "
        "Reindexá los documentos con el modelo configurado."
    )


class AgentOutputInvalid(ServiceError):
    code = "AGENT_OUTPUT_INVALID"
    public_message = "Un agente devolvió una respuesta inválida. Reintentá la consulta."


class ActionRejectedFailure(ServiceError):
    code = "ACTION_REJECTED_FAILED"
    public_message = (
        "La acción fue rechazada y no se modificaron datos, pero la consulta falló "
        "al generar la respuesta."
    )


INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
INTERNAL_ERROR_MESSAGE = "Ocurrió un error interno. Revisá los logs del servicio."


def public_error(exc: BaseException) -> str:
    """Text stored in Job.error and returned by the API. Never includes internals."""
    if isinstance(exc, ServiceError):
        return f"{exc.code}: {exc.public_message}"
    return f"{INTERNAL_ERROR_CODE}: {INTERNAL_ERROR_MESSAGE}"
