"""Shared request auth helpers."""

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

from ..core.config import settings

_api_key_query = APIKeyQuery(name="apikey", auto_error=False)
_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


def require_api_key(
    apikey: str | None = Security(_api_key_query),
    x_api_key: str | None = Security(_api_key_header),
) -> str:
    configured = settings.awc_api_key
    provided = (apikey or x_api_key or "").strip()
    if not configured:
        return provided
    if provided != configured:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return provided
