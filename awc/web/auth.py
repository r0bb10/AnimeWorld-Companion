"""Shared request auth helpers."""

from fastapi import Header, HTTPException, Query

from ..core.config import settings


def require_api_key(
    apikey: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> str:
    configured = settings.awc_api_key
    provided = (apikey or x_api_key or "").strip()
    if not configured:
        return provided
    if provided != configured:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return provided
