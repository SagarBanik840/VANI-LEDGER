"""Block 1 exit-gate tests: schema, deterministic seed, health, auth, IDOR, SQL safety."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from scripts.seed import seed_database
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from vani_ledger.api.main import create_app
from vani_ledger.config import get_settings
from vani_ledger.schemas import OverrideCommitRequest, OverrideSimulateRequest
from vani_ledger.storage.db import create_db_engine, get_db
from vani_ledger.storage.models import Sale, Sku


@pytest.fixture()
def seeded_client(tmp_path: Path):
    """Build an isolated seeded SQLite WAL database and TestClient for Block 1 checks."""
    db_file = tmp_path / "test_vani.db"
    test_engine = create_db_engine(f"sqlite:///{db_file}")
    counts = seed_database(target_engine=test_engine, reset=True)
    test_session_local = sessionmaker(
        bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    app = create_app()

    def _override_get_db():
        db = test_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as client:
        yield client, test_session_local, counts

    test_engine.dispose()


def test_seed_counts_and_hero_sku_calibration(seeded_client) -> None:
    _client, session_local, counts = seeded_client
    assert counts["merchants"] == 2
    assert counts["skus"] == 43
    assert counts["primary_sales"] == 40 * 730
    assert counts["sales"] > 29_200
    assert counts["calendar_events"] >= 760

    with session_local() as db:
        hero = db.scalar(select(Sku).where(Sku.sku_code == "SKU-OIL-001"))
        assert hero is not None
        assert hero.name == "Festive Mustard Oil 1L"
        assert hero.hinglish_alias == "Sarso Tel 1L"
        assert hero.unit_cost == Decimal("150.00")
        assert hero.unit_price == Decimal("220.00")
        assert hero.lead_time_days == 3
        assert hero.shelf_life_days == 90

        dairy = db.scalar(select(Sku).where(Sku.sku_code == "SKU-DAIRY-001"))
        assert dairy is not None
        assert dairy.perishable is True
        assert dairy.shelf_life_days == 3

        d2c = db.scalar(select(Sku).where(Sku.sku_code == "SKU-D2C-001"))
        assert d2c is not None
        assert d2c.lead_time_days == 5
        assert d2c.moq == 10
        assert d2c.pack_size == 5

        total_sales = db.scalar(select(func.count(Sale.id))) or 0
        stockout_sales = (
            db.scalar(select(func.count(Sale.id)).where(Sale.stockout_flag.is_(True))) or 0
        )
        rate = stockout_sales / total_sales
        assert 0.02 <= rate <= 0.045, f"Unexpected stockout censoring rate: {rate:.4f}"


def test_health_endpoint_and_security_headers(seeded_client) -> None:
    client, _session_local, _counts = seeded_client
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["ok"] is True
    assert body["data"]["database"] == "ok"
    assert body["data"]["sku_count"] == 43
    assert body["data"]["sales_count"] > 29_200
    assert body["meta"]["request_id"].startswith("req_")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]


def test_auth_tenant_scoping_and_idor_returns_404(seeded_client) -> None:
    client, _session_local, _counts = seeded_client
    settings = get_settings()

    # Missing or wrong API key -> 401
    assert client.get("/v1/skus").status_code == 401
    assert client.get("/v1/skus", headers={"X-API-Key": "wrong-key"}).status_code == 401

    # Primary merchant sees 40 SKUs
    r1 = client.get("/v1/skus", headers={"X-API-Key": settings.vani_api_key})
    assert r1.status_code == 200
    assert r1.json()["data"]["total"] == 40

    # Secondary merchant sees 3 SKUs
    r2 = client.get("/v1/skus", headers={"X-API-Key": settings.secondary_api_key})
    assert r2.status_code == 200
    assert r2.json()["data"]["total"] == 3
    secondary_sku_id = r2.json()["data"]["items"][0]["id"]

    # Primary merchant requesting secondary merchant's SKU -> 404 (no IDOR leak)
    r_idor = client.get(
        f"/v1/skus/{secondary_sku_id}",
        headers={"X-API-Key": settings.vani_api_key},
    )
    assert r_idor.status_code == 404
    assert r_idor.json()["error"]["code"] == "NOT_FOUND"


def test_sqli_and_strict_schema_sanitization(seeded_client) -> None:
    client, _session_local, _counts = seeded_client
    settings = get_settings()
    headers = {"X-API-Key": settings.vani_api_key}

    # SQLi string in path UUID -> 422 validation error, never 500
    r_sqli = client.get("/v1/skus/' OR 1=1 --", headers=headers)
    assert r_sqli.status_code == 422
    assert r_sqli.json()["error"]["code"] == "VALIDATION_ERROR"

    # SQLi attempt in dynamic sort_by -> rejected by allowlist (422)
    r_sort = client.get("/v1/skus?sort_by=name;DROP%20TABLE%20sales;--", headers=headers)
    assert r_sort.status_code == 422

    # Mass assignment via extra="forbid" blocked at schema layer
    with pytest.raises(ValidationError):
        OverrideSimulateRequest.model_validate(
            {
                "sku_id": "aaaaaaaa-aaaa-4aaa-8aaa-000000000001",
                "manual_qty": 35,
                "merchant_id": "22222222-2222-4222-8222-222222222222",
            }
        )

    # Null byte in text rejected
    with pytest.raises(ValidationError):
        OverrideCommitRequest.model_validate(
            {
                "sku_id": "aaaaaaaa-aaaa-4aaa-8aaa-000000000001",
                "manual_qty": 35,
                "reason": "bad\x00input",
            }
        )


def test_zero_sql_string_interpolation_in_source() -> None:
    src_root = Path(__file__).resolve().parents[2] / "src" / "vani_ledger"
    bad_pattern = re.compile(
        r"(execute|text)\(\s*(f[\"']|[\"'][^\"']*[\"']\s*%|\s*[\"'][^\"']*\.format)"
    )
    offenders: list[str] = []
    for py_file in src_root.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        if bad_pattern.search(content):
            offenders.append(str(py_file))
    assert not offenders, f"Potential SQL string interpolation detected: {offenders}"
