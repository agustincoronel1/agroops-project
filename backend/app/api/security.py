import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings

# Declares the scheme in OpenAPI so Swagger shows the "Authorize" button.
api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Approval API key (configured with APPROVAL_API_KEY).",
)


async def require_approval_key(
    provided: Annotated[str | None, Security(api_key_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Fail closed: without a configured key the approval endpoint is disabled."""
    expected = settings.approval_api_key
    if expected is None or not expected.get_secret_value():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Approval is disabled: APPROVAL_API_KEY is not configured.",
        )
    if provided is None or not secrets.compare_digest(
        provided.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
