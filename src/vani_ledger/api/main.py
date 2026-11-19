"""FastAPI application entrypoint with lifespan cache warmup, UI serving & security headers."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.orm import Session

from vani_ledger.api.routes import router as v1_router
from vani_ledger.api.security import limiter, require_merchant
from vani_ledger.config import get_settings
from vani_ledger.forecast_core.simulator import get_snapshot_cache
from vani_ledger.schemas import (
    ApiError,
    ApiMeta,
    ApiResponse,
    HealthPayload,
    SkuListPayload,
    SkuSummary,
)
from vani_ledger.storage.db import get_db, init_db
from vani_ledger.storage.models import Merchant
from vani_ledger.storage.repository import get_health_counts, get_sku_for_merchant, list_skus

logger = logging.getLogger("vani_ledger.api")

API_DIR = Path(__file__).resolve().parent
STATIC_DIR = API_DIR / "static"
TEMPLATES_DIR = API_DIR / "templates"
MAX_JSON_BODY_BYTES = 1 * 1024 * 1024  # 1 MB cap for non-multipart bodies
MULTIPART_OVERHEAD_BYTES = 128 * 1024  # Header/boundary allowance on multipart uploads


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", f"req_{uuid4().hex[:12]}")


def _elapsed_ms(request: Request) -> float:
    start = getattr(request.state, "start_time", None)
    if start is None:
        return 0.0
    return round((time.perf_counter() - start) * 1000.0, 2)


def _warm_cache_for_app(app: FastAPI) -> int:
    """Warm ForecastSnapshotCache using the active (possibly test-overridden) DB dependency."""
    cache = get_snapshot_cache()
    db_dep = app.dependency_overrides.get(get_db, get_db)
    db_gen = db_dep()
    try:
        db = next(db_gen)
        count = cache.warm_from_db(db)
        logger.info("Warmed ForecastSnapshotCache with %d SKU snapshots", count)
        return count
    except Exception as exc:
        logger.warning("ForecastSnapshotCache warmup skipped: %s", type(exc).__name__)
        return 0
    finally:
        try:
            db_gen.close()
        except Exception:
            logger.debug("Ignoring db generator close warning during cache warmup")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()
    init_db()
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        _warm_cache_for_app(application)
        yield

    app = FastAPI(
        title="VANI-LEDGER API",
        version="0.1.0",
        description="Explainable demand forecasting and multimodal inventory copilot (PS 01).",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def request_context_and_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        req_id = f"req_{uuid4().hex[:12]}"
        request.state.request_id = req_id
        request.state.start_time = time.perf_counter()

        # Early Content-Length guard (1 MB JSON cap; 10 MB + boundary cap for multipart)
        raw_cl = request.headers.get("content-length")
        if raw_cl is not None:
            try:
                content_length = int(raw_cl)
            except ValueError:
                content_length = 0
            content_type = (request.headers.get("content-type") or "").lower()
            is_multipart = content_type.startswith("multipart/form-data")
            limit = (
                settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES
                if is_multipart
                else MAX_JSON_BODY_BYTES
            )
            if content_length > limit:
                response: Response = JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content=ApiResponse[None](
                        data=None,
                        meta=ApiMeta(request_id=req_id, latency_ms=_elapsed_ms(request)),
                        error=ApiError(
                            code="PAYLOAD_TOO_LARGE",
                            message=f"Request body exceeds maximum allowed size ({limit} bytes)",
                        ),
                    ).model_dump(mode="json"),
                )
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)

        response.headers["X-Request-ID"] = req_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        return response

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        _ = exc
        req_id = _request_id(request)
        response = JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content=ApiResponse[None](
                data=None,
                meta=ApiMeta(request_id=req_id, latency_ms=_elapsed_ms(request)),
                error=ApiError(code="RATE_LIMITED", message="Too many requests; retry later"),
            ).model_dump(mode="json"),
        )
        response.headers["Retry-After"] = "60"
        if hasattr(request.state, "view_rate_limit"):
            response = request.app.state.limiter._inject_headers(
                response, request.state.view_rate_limit
            )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        req_id = _request_id(request)
        first = exc.errors()[0] if exc.errors() else {"msg": "Invalid request payload"}
        loc = ".".join(str(part) for part in first.get("loc", []))
        msg = f"{loc}: {first.get('msg', 'Invalid value')}" if loc else str(first.get("msg"))
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=ApiResponse[None](
                data=None,
                meta=ApiMeta(request_id=req_id, latency_ms=_elapsed_ms(request)),
                error=ApiError(code="VALIDATION_ERROR", message=msg),
            ).model_dump(mode="json"),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        req_id = _request_id(request)
        code_map = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "CONFLICT",
            413: "PAYLOAD_TOO_LARGE",
            422: "VALIDATION_ERROR",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=ApiResponse[None](
                data=None,
                meta=ApiMeta(request_id=req_id, latency_ms=_elapsed_ms(request)),
                error=ApiError(
                    code=code_map.get(exc.status_code, f"HTTP_{exc.status_code}"),
                    message=str(exc.detail),
                ),
            ).model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        req_id = _request_id(request)
        logger.exception("Unhandled error for request_id=%s", req_id, exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ApiResponse[None](
                data=None,
                meta=ApiMeta(request_id=req_id, latency_ms=_elapsed_ms(request)),
                error=ApiError(code="INTERNAL_ERROR", message="Internal server error"),
            ).model_dump(mode="json"),
        )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse, tags=["ui"])
    @limiter.limit("60/minute")
    def cockpit_ui(request: Request, response: Response) -> HTMLResponse:
        """Serve the zero-CDN Editorial Obsidian & Gold Seller Cockpit UI."""
        rendered = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "demo_api_key": settings.vani_api_key,
                "model_version": settings.model_version,
            },
        )
        for hdr_key, hdr_val in response.headers.items():
            rendered.headers[hdr_key] = hdr_val
        return rendered

    @app.get("/health", response_model=ApiResponse[HealthPayload], tags=["system"])
    @limiter.limit("60/minute")
    def health(
        request: Request,
        response: Response,
        db: Session = Depends(get_db),
    ) -> ApiResponse[HealthPayload]:
        try:
            sku_count, sales_count = get_health_counts(db)
            db_state: Literal["ok", "error"] = "ok"
            overall: Literal["healthy", "degraded"] = "healthy"
            ok = True
        except Exception:
            sku_count, sales_count = 0, 0
            db_state = "error"
            overall = "degraded"
            ok = False

        return ApiResponse[HealthPayload](
            data=HealthPayload(
                ok=ok,
                status=overall,
                database=db_state,
                sku_count=sku_count,
                sales_count=sales_count,
                model_version=settings.model_version,
                offline_ready=True,
            ),
            meta=ApiMeta(
                request_id=_request_id(request),
                model_version=settings.model_version,
                latency_ms=_elapsed_ms(request),
            ),
            error=None,
        )

    @app.get("/v1/skus", response_model=ApiResponse[SkuListPayload], tags=["catalog"])
    @limiter.limit("120/minute")
    def get_skus(
        request: Request,
        response: Response,
        category: str | None = Query(default=None, max_length=64),
        search: str | None = Query(default=None, max_length=100),
        sort_by: Literal["sku_code", "name", "category", "unit_price", "abc_segment"] = "sku_code",
        order: Literal["asc", "desc"] = "asc",
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=10_000),
        merchant: Merchant = Depends(require_merchant),
        db: Session = Depends(get_db),
    ) -> ApiResponse[SkuListPayload]:
        rows, total = list_skus(
            db,
            merchant.id,
            category=category,
            search=search,
            sort_by=sort_by,
            order=order,
            limit=limit,
            offset=offset,
        )
        return ApiResponse[SkuListPayload](
            data=SkuListPayload(
                items=[SkuSummary.model_validate(row) for row in rows],
                total=total,
                limit=limit,
                offset=offset,
            ),
            meta=ApiMeta(
                request_id=_request_id(request),
                model_version=settings.model_version,
                latency_ms=_elapsed_ms(request),
            ),
            error=None,
        )

    @app.get("/v1/skus/{sku_id}", response_model=ApiResponse[SkuSummary], tags=["catalog"])
    @limiter.limit("120/minute")
    def get_sku_detail(
        request: Request,
        response: Response,
        sku_id: UUID,
        merchant: Merchant = Depends(require_merchant),
        db: Session = Depends(get_db),
    ) -> ApiResponse[SkuSummary]:
        row = get_sku_for_merchant(db, merchant.id, str(sku_id))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found")
        return ApiResponse[SkuSummary](
            data=SkuSummary.model_validate(row),
            meta=ApiMeta(
                request_id=_request_id(request),
                model_version=settings.model_version,
                latency_ms=_elapsed_ms(request),
            ),
            error=None,
        )

    app.include_router(v1_router)
    return app


app = create_app()
