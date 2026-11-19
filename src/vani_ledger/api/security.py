"""Authentication, tenant scoping, and SlowAPI rate-limiting primitives."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from vani_ledger.config import get_settings, hash_api_key
from vani_ledger.storage.db import get_db
from vani_ledger.storage.models import Merchant
from vani_ledger.storage.repository import authenticate_merchant_by_key_hash


def get_rate_limit_identity(request: Request) -> str:
    """Key rate limits by authenticated merchant_id when present, else client IP."""
    merchant_id = getattr(request.state, "merchant_id", None)
    if merchant_id:
        return f"merchant:{merchant_id}"
    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"key:{hash_api_key(api_key)[:16]}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=get_rate_limit_identity,
    default_limits=["120/minute"],
    headers_enabled=True,
    enabled=get_settings().rate_limit_enabled,
)


def require_merchant(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Merchant:
    """Authenticate caller via X-API-Key and bind server-side merchant_id onto request.state."""
    if not x_api_key or not x_api_key.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )
    candidate_hash = hash_api_key(x_api_key.strip())
    merchant = authenticate_merchant_by_key_hash(db, candidate_hash)
    if merchant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    request.state.merchant_id = merchant.id
    return merchant
