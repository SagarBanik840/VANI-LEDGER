"""All /v1/* domain endpoints for VANI-LEDGER with strict security, rate limits, and envelopes."""

from __future__ import annotations

import html
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from vani_ledger.api.security import limiter, require_merchant
from vani_ledger.capture import (
    CsvImportPayload,
    InvalidUploadError,
    UploadTooLargeError,
    import_sales_csv,
    process_bill_upload,
    process_voice_note,
)
from vani_ledger.capture.bill_extractor import DEMO_BILL_IMAGE_PATH
from vani_ledger.config import PROJECT_ROOT, get_settings
from vani_ledger.forecast_core.policy import compute_budget_reorder_plan, quantize_money
from vani_ledger.forecast_core.simulator import (
    build_forecast_snapshot,
    get_snapshot_cache,
    simulate_override,
)
from vani_ledger.mcp_server.server import (
    build_forecast_payload,
    clear_mcp_call_history,
    get_mcp_inspector_payload,
    record_mcp_invocation,
)
from vani_ledger.schemas import (
    ApiMeta,
    ApiResponse,
    BillCapturePayload,
    ExplainPayload,
    ExplainRequest,
    ForecastPayload,
    OverrideCommitPayload,
    OverrideCommitRequest,
    OverrideSimulatePayload,
    OverrideSimulateRequest,
    ReorderPlanPayload,
    VoiceIntentPayload,
    VoiceNoteRequest,
    sanitize_text,
)
from vani_ledger.services.narrative import generate_grounded_narrative
from vani_ledger.storage.db import get_db
from vani_ledger.storage.models import Merchant, Override
from vani_ledger.storage.repository import (
    get_latest_forecast,
    get_override_by_idempotency_key,
    get_sku_for_merchant,
    list_latest_forecasts_for_merchant,
    list_skus,
    write_audit_log,
)

SAMPLE_CSV_PATH: Path = PROJECT_ROOT / "data" / "fixtures" / "d2c_sample_sales.csv"
BACKTEST_REPORT_PATH: Path = PROJECT_ROOT / "eval" / "latest_backtest.json"

router = APIRouter(prefix="/v1")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", f"req_{uuid4().hex[:12]}")


def _elapsed_ms(request: Request) -> float:
    start = getattr(request.state, "start_time", None)
    if start is None:
        return 0.0
    return round((time.perf_counter() - start) * 1000.0, 2)


def _meta(request: Request, *, model_version: str | None = None) -> ApiMeta:
    resolved_version = model_version or get_settings().model_version
    return ApiMeta(
        request_id=_request_id(request),
        model_version=resolved_version,
        latency_ms=_elapsed_ms(request),
    )


def _override_to_payload(row: Override) -> OverrideCommitPayload:
    return OverrideCommitPayload(
        override_id=UUID(str(row.id)),
        sku_id=UUID(str(row.sku_id)),
        suggested_qty=int(row.suggested_qty),
        manual_qty=int(row.manual_qty),
        cash_delta=quantize_money(row.cash_delta),
        stockout_risk_before=round(float(row.stockout_risk_before), 4),
        stockout_risk_after=round(float(row.stockout_risk_after), 4),
        days_cover=round(float(row.days_cover), 2),
        spoilage_risk=round(float(row.spoilage_risk), 4),
        within_budget=bool(row.within_budget),
        reason=row.reason,
        created_at=row.created_at,
    )


# ── Forecasts & Grounded Explainability ────────────────────────────────────────


@router.get(
    "/forecasts/{sku_id}",
    response_model=ApiResponse[ForecastPayload],
    tags=["forecasts"],
)
@limiter.limit("120/minute")
def get_forecast_for_sku(
    request: Request,
    response: Response,
    sku_id: UUID,
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[ForecastPayload]:
    t0 = time.perf_counter()
    sku = get_sku_for_merchant(db, merchant.id, str(sku_id))
    if sku is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found")

    forecast = get_latest_forecast(db, merchant.id, sku.id)
    if forecast is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No forecast found for SKU; run training first",
        )

    payload = build_forecast_payload(sku, forecast)
    record_mcp_invocation(
        "forecast_sku_demand",
        arguments={"sku_id": str(sku_id), "horizon_days": payload.horizon_days},
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        ok=True,
        result_summary={
            "sku_code": payload.sku_code,
            "recommended_qty": payload.recommended_qty,
            "service_level": payload.service_level,
        },
        caller="api",
    )
    return ApiResponse[ForecastPayload](
        data=payload,
        meta=_meta(request, model_version=payload.model_version),
        error=None,
    )


@router.post(
    "/forecasts/{sku_id}/explain",
    response_model=ApiResponse[ExplainPayload],
    tags=["forecasts"],
)
@limiter.limit("30/minute")
def explain_forecast_for_sku(
    request: Request,
    response: Response,
    sku_id: UUID,
    body: ExplainRequest | None = None,
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[ExplainPayload]:
    t0 = time.perf_counter()
    resolved_body = body if body is not None else ExplainRequest()

    sku = get_sku_for_merchant(db, merchant.id, str(sku_id))
    if sku is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found")

    forecast = get_latest_forecast(db, merchant.id, sku.id)
    if forecast is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No forecast found for SKU; run training first",
        )

    forecast_payload = build_forecast_payload(sku, forecast)
    explain_payload = generate_grounded_narrative(
        forecast_payload,
        language=resolved_body.language,
    )
    record_mcp_invocation(
        "explain_forecast_drivers",
        arguments={"sku_id": str(sku_id), "language": resolved_body.language},
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        ok=True,
        result_summary={
            "recommended_qty": explain_payload.recommended_qty,
            "grounded": explain_payload.grounded,
            "source": explain_payload.source,
        },
        caller="api",
    )
    return ApiResponse[ExplainPayload](
        data=explain_payload,
        meta=_meta(request, model_version=forecast_payload.model_version),
        error=None,
    )


# ── Sub-50ms Override Simulation & Atomic Audited Commit ───────────────────────


@router.post(
    "/overrides/simulate",
    response_model=ApiResponse[OverrideSimulatePayload],
    tags=["overrides"],
)
@limiter.limit("120/minute")
def simulate_override_route(
    request: Request,
    response: Response,
    body: OverrideSimulateRequest,
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[OverrideSimulatePayload]:
    """Hot-path <50ms in-memory override simulation (zero DB hits when snapshot cache is warm)."""
    t0 = time.perf_counter()
    cache = get_snapshot_cache()
    snapshot = cache.get(merchant.id, body.sku_id)

    if snapshot is None:
        sku = get_sku_for_merchant(db, merchant.id, str(body.sku_id))
        if sku is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found")
        forecast = get_latest_forecast(db, merchant.id, sku.id)
        if forecast is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No forecast found for SKU",
            )
        snapshot = build_forecast_snapshot(sku, forecast)
        cache.put(snapshot)
        cache.set_merchant_budget(merchant.id, quantize_money(merchant.weekly_budget))

    weekly_budget = cache.get_merchant_budget(merchant.id, quantize_money(merchant.weekly_budget))
    payload = simulate_override(
        snapshot,
        manual_qty=body.manual_qty,
        weekly_budget=weekly_budget,
    )
    sim_ms = (time.perf_counter() - t0) * 1000.0
    record_mcp_invocation(
        "simulate_manual_override",
        arguments={"sku_id": str(body.sku_id), "manual_qty": body.manual_qty},
        latency_ms=sim_ms,
        ok=True,
        result_summary={
            "suggested_qty": payload.suggested_qty,
            "manual_qty": payload.manual_qty,
            "cash_freed": str(payload.cash_freed),
            "stockout_risk_delta": payload.stockout_risk_delta,
        },
        caller="api",
    )
    return ApiResponse[OverrideSimulatePayload](
        data=payload,
        meta=_meta(request, model_version=snapshot.model_version),
        error=None,
    )


@router.post(
    "/overrides/commit",
    response_model=ApiResponse[OverrideCommitPayload],
    tags=["overrides"],
)
@limiter.limit("30/minute")
def commit_override_route(
    request: Request,
    response: Response,
    body: OverrideCommitRequest,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[OverrideCommitPayload]:
    """Atomically commit an audited manual override with Idempotency-Key replay protection."""
    raw_idem = (
        idempotency_key_header
        if (idempotency_key_header is not None and idempotency_key_header.strip())
        else body.idempotency_key
    )
    idem_key: str | None = None
    if raw_idem is not None and raw_idem.strip():
        try:
            cleaned_idem = sanitize_text(raw_idem)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        if cleaned_idem and len(cleaned_idem) > 128:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Idempotency-Key exceeds maximum length of 128 characters",
            )
        idem_key = cleaned_idem or None

    if idem_key is not None:
        existing = get_override_by_idempotency_key(db, merchant.id, idem_key)
        if existing is not None:
            return ApiResponse[OverrideCommitPayload](
                data=_override_to_payload(existing),
                meta=_meta(request),
                error=None,
            )

    sku = get_sku_for_merchant(db, merchant.id, str(body.sku_id))
    if sku is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SKU not found")

    forecast = get_latest_forecast(db, merchant.id, sku.id)
    if forecast is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No forecast found for SKU",
        )

    snapshot = build_forecast_snapshot(sku, forecast)
    sim = simulate_override(
        snapshot,
        manual_qty=body.manual_qty,
        weekly_budget=quantize_money(merchant.weekly_budget),
    )

    safe_reason = html.escape(body.reason, quote=True) if body.reason else None
    override_id = str(uuid4())
    override_row = Override(
        id=override_id,
        merchant_id=merchant.id,
        sku_id=sku.id,
        forecast_id=forecast.id,
        suggested_qty=sim.suggested_qty,
        manual_qty=sim.manual_qty,
        cash_delta=sim.cash_delta,
        stockout_risk_before=Decimal(f"{sim.stockout_risk_before:.4f}"),
        stockout_risk_after=Decimal(f"{sim.stockout_risk_after:.4f}"),
        days_cover=Decimal(f"{sim.days_cover:.2f}"),
        spoilage_risk=Decimal(f"{sim.spoilage_risk:.4f}"),
        within_budget=sim.within_budget,
        reason=safe_reason,
        idempotency_key=idem_key,
    )

    db.add(override_row)
    write_audit_log(
        db,
        merchant_id=merchant.id,
        action="OVERRIDE_COMMITTED",
        entity_type="override",
        entity_id=override_id,
        payload={
            "sku_id": sku.id,
            "sku_code": sku.sku_code,
            "forecast_id": forecast.id,
            "suggested_qty": sim.suggested_qty,
            "manual_qty": sim.manual_qty,
            "cash_delta": str(sim.cash_delta),
            "stockout_risk_before": sim.stockout_risk_before,
            "stockout_risk_after": sim.stockout_risk_after,
            "days_cover": sim.days_cover,
            "spoilage_risk": sim.spoilage_risk,
            "within_budget": sim.within_budget,
            "reason": safe_reason,
            "idempotency_key": idem_key,
        },
        request_id=_request_id(request),
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if idem_key is not None:
            replayed = get_override_by_idempotency_key(db, merchant.id, idem_key)
            if replayed is not None:
                return ApiResponse[OverrideCommitPayload](
                    data=_override_to_payload(replayed),
                    meta=_meta(request),
                    error=None,
                )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Duplicate idempotency key conflict",
        ) from exc

    db.refresh(override_row)
    return ApiResponse[OverrideCommitPayload](
        data=_override_to_payload(override_row),
        meta=_meta(request, model_version=forecast.model_version),
        error=None,
    )


# ── Multi-SKU Weekly Budget Reorder Plan (Case 03) ─────────────────────────────


@router.get(
    "/reorder-plan",
    response_model=ApiResponse[ReorderPlanPayload],
    tags=["policy"],
)
@limiter.limit("60/minute")
def get_reorder_plan(
    request: Request,
    response: Response,
    weekly_budget: Decimal | None = Query(
        default=None,
        ge=Decimal("0"),
        le=Decimal("100000000"),
    ),
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[ReorderPlanPayload]:
    budget = (
        quantize_money(weekly_budget)
        if weekly_budget is not None
        else quantize_money(merchant.weekly_budget)
    )
    skus, _total = list_skus(db, merchant.id, limit=100, offset=0)
    forecasts_by_sku = list_latest_forecasts_for_merchant(db, merchant.id)
    plan = compute_budget_reorder_plan(skus, forecasts_by_sku, budget)
    return ApiResponse[ReorderPlanPayload](
        data=plan,
        meta=_meta(request),
        error=None,
    )


# ── Multimodal Capture (Bill / CSV / Hinglish Voice) ───────────────────────────


@router.post(
    "/capture/bill",
    response_model=ApiResponse[BillCapturePayload],
    tags=["capture"],
)
@limiter.limit("10/minute")
async def capture_bill_route(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[BillCapturePayload]:
    t0 = time.perf_counter()
    settings = get_settings()
    raw_bytes = await file.read(settings.max_upload_bytes + 1)
    if len(raw_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Upload exceeds maximum allowed size of {settings.max_upload_bytes} bytes",
        )

    try:
        payload = process_bill_upload(
            db,
            merchant.id,
            raw_bytes,
            file.content_type,
            filename=file.filename,
            request_id=_request_id(request),
        )
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except InvalidUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record_mcp_invocation(
        "ingest_bill_ledger",
        arguments={"source": payload.source_adapter, "line_items": len(payload.line_items)},
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        ok=True,
        result_summary={
            "math_ok": payload.math_check.ok,
            "delta": str(payload.math_check.delta),
            "demo_document": payload.demo_document,
        },
        caller="api",
    )
    return ApiResponse[BillCapturePayload](
        data=payload,
        meta=_meta(request),
        error=None,
    )


@router.get("/capture/sample-bill", tags=["capture"])
@limiter.limit("60/minute")
def get_sample_bill_fixture(
    request: Request,
    response: Response,
    _merchant: Merchant = Depends(require_merchant),
) -> Response:
    """Serve the golden demo_bill.png bytes so the UI can exercise real POST /v1/capture/bill."""
    if not DEMO_BILL_IMAGE_PATH.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo bill fixture image not found",
        )
    return Response(content=DEMO_BILL_IMAGE_PATH.read_bytes(), media_type="image/png")


@router.post(
    "/capture/csv",
    response_model=ApiResponse[CsvImportPayload],
    tags=["capture"],
)
@limiter.limit("10/minute")
async def capture_csv_route(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[CsvImportPayload]:
    settings = get_settings()
    raw_bytes = await file.read(settings.max_upload_bytes + 1)
    if len(raw_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"CSV exceeds maximum allowed size of {settings.max_upload_bytes} bytes",
        )

    try:
        payload = import_sales_csv(
            db,
            merchant.id,
            raw_bytes,
            filename=file.filename,
            request_id=_request_id(request),
        )
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    except InvalidUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return ApiResponse[CsvImportPayload](
        data=payload,
        meta=_meta(request),
        error=None,
    )


@router.get("/capture/sample-csv", tags=["capture"])
@limiter.limit("60/minute")
def get_sample_csv_fixture(
    request: Request,
    response: Response,
    _merchant: Merchant = Depends(require_merchant),
) -> Response:
    """Serve the golden D2C sales CSV so the UI can exercise real POST /v1/capture/csv."""
    if not SAMPLE_CSV_PATH.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sample D2C CSV fixture not found",
        )
    return Response(content=SAMPLE_CSV_PATH.read_bytes(), media_type="text/csv")


@router.post(
    "/capture/voice-note",
    response_model=ApiResponse[VoiceIntentPayload],
    tags=["capture"],
)
@limiter.limit("20/minute")
def capture_voice_note_route(
    request: Request,
    response: Response,
    body: VoiceNoteRequest,
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[VoiceIntentPayload]:
    payload = process_voice_note(
        db,
        merchant.id,
        body.transcript,
        request_id=_request_id(request),
    )
    return ApiResponse[VoiceIntentPayload](
        data=payload,
        meta=_meta(request),
        error=None,
    )


# ── Honest ML Backtest & FastMCP Inspector ─────────────────────────────────────


@router.get(
    "/eval/backtest",
    response_model=ApiResponse[dict[str, Any]],
    tags=["evaluation"],
)
@limiter.limit("30/minute")
def get_backtest_report(
    request: Request,
    response: Response,
    _merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[dict[str, Any]]:
    if BACKTEST_REPORT_PATH.is_file():
        report = json.loads(BACKTEST_REPORT_PATH.read_text(encoding="utf-8"))
    else:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        from eval.backtest import run_backtest

        bind = db.get_bind()
        target_eng = bind if isinstance(bind, Engine) else None
        report = run_backtest(target_engine=target_eng, save_report=True, gate=False)

    return ApiResponse[dict[str, Any]](
        data=report,
        meta=_meta(request, model_version=str(report.get("model_version", "")) or None),
        error=None,
    )


@router.get(
    "/mcp/tools",
    response_model=ApiResponse[dict[str, Any]],
    tags=["mcp"],
)
@limiter.limit("60/minute")
def get_mcp_tools_route(
    request: Request,
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    _merchant: Merchant = Depends(require_merchant),
) -> ApiResponse[dict[str, Any]]:
    payload = get_mcp_inspector_payload(limit=limit)
    return ApiResponse[dict[str, Any]](
        data=payload,
        meta=_meta(request),
        error=None,
    )


# ── Deterministic Demo Reset ───────────────────────────────────────────────────


@router.post(
    "/demo/reset",
    response_model=ApiResponse[dict[str, Any]],
    tags=["system"],
)
@limiter.limit("5/minute")
def demo_reset_route(
    request: Request,
    response: Response,
    merchant: Merchant = Depends(require_merchant),
    db: Session = Depends(get_db),
) -> ApiResponse[dict[str, Any]]:
    """Run clean deterministic reseed + train + in-memory cache reload for stage rehearsals."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.seed import seed_database
    from scripts.train import train_and_populate

    bind = db.get_bind()
    target_eng = bind if isinstance(bind, Engine) else None
    db.close()

    seed_counts = seed_database(target_engine=target_eng, reset=True)
    train_summary = train_and_populate(target_engine=target_eng, save_artifacts=True)

    cache = get_snapshot_cache()
    cache.clear()
    clear_mcp_call_history()

    warmed_count = 0
    if target_eng is not None:
        fresh_factory = sessionmaker(
            bind=target_eng,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        with fresh_factory() as fresh_db:
            warmed_count = cache.warm_from_db(fresh_db)
            write_audit_log(
                fresh_db,
                merchant_id=merchant.id,
                action="RESET",
                entity_type="system",
                entity_id="demo_reset",
                payload={
                    "skus": seed_counts.get("skus", 0),
                    "sales": seed_counts.get("sales", 0),
                    "forecasts": train_summary.get("total_forecasts", 0),
                    "cache_warmed": warmed_count,
                },
                request_id=_request_id(request),
            )
            fresh_db.commit()

    return ApiResponse[dict[str, Any]](
        data={
            "reset": True,
            "seed_counts": seed_counts,
            "forecasts_written": train_summary.get("total_forecasts", 0),
            "cache_snapshots_warmed": warmed_count,
            "hero_skus": train_summary.get("hero_skus", {}),
        },
        meta=_meta(request, model_version=str(train_summary.get("model_version", "")) or None),
        error=None,
    )
