"""Deterministic seed generator for VANI-LEDGER (40 SKUs x 730 days + Indian calendar).

Generates realistic Indian kirana, perishable dairy, and micro-D2C sales history
with weekday cycles, payday windows, festival pre-surge curves, and ~3% stockout censoring.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from vani_ledger.config import get_settings, hash_api_key, load_demo_config
from vani_ledger.storage.db import Base, init_db
from vani_ledger.storage.db import engine as default_engine
from vani_ledger.storage.models import CalendarEvent, Merchant, Sale, Sku

# 37 additional realistic Indian retail SKUs complementing the 3 Slide 6 hero SKUs (= 40 total)
CATALOG_TEMPLATES: list[dict[str, Any]] = [
    # Cooking Oil (5 more -> 6 total)
    {
        "sku_code": "SKU-OIL-002",
        "name": "Refined Sunflower Oil 1L",
        "hinglish_alias": "Surajmukhi Tel 1L",
        "category": "Cooking Oil",
        "unit": "pouch",
        "unit_cost": "132.00",
        "unit_price": "175.00",
        "lead_time_days": 3,
        "shelf_life_days": 120,
        "abc_segment": "A",
        "base_daily_mean": 4.8,
        "festival_sensitivity": 0.75,
        "noise_cv": 0.16,
    },
    {
        "sku_code": "SKU-OIL-003",
        "name": "Pure Cow Ghee 500ml",
        "hinglish_alias": "Desi Ghee 500ml",
        "category": "Cooking Oil",
        "unit": "jar",
        "unit_cost": "285.00",
        "unit_price": "360.00",
        "lead_time_days": 3,
        "shelf_life_days": 150,
        "abc_segment": "A",
        "base_daily_mean": 2.9,
        "festival_sensitivity": 1.10,
        "noise_cv": 0.18,
    },
    {
        "sku_code": "SKU-OIL-004",
        "name": "Groundnut Oil 1L",
        "hinglish_alias": "Moongfali Tel 1L",
        "category": "Cooking Oil",
        "unit": "bottle",
        "unit_cost": "178.00",
        "unit_price": "235.00",
        "lead_time_days": 4,
        "shelf_life_days": 120,
        "abc_segment": "B",
        "base_daily_mean": 2.1,
        "festival_sensitivity": 0.65,
        "noise_cv": 0.22,
    },
    {
        "sku_code": "SKU-OIL-005",
        "name": "Sesame Til Oil 500ml",
        "hinglish_alias": "Til Ka Tel 500ml",
        "category": "Cooking Oil",
        "unit": "bottle",
        "unit_cost": "165.00",
        "unit_price": "225.00",
        "lead_time_days": 4,
        "shelf_life_days": 150,
        "abc_segment": "C",
        "base_daily_mean": 0.55,
        "festival_sensitivity": 0.90,
        "noise_cv": 0.45,
    },
    {
        "sku_code": "SKU-OIL-006",
        "name": "Cold-Pressed Coconut Oil 500ml",
        "hinglish_alias": "Nariyal Tel 500ml",
        "category": "Cooking Oil",
        "unit": "bottle",
        "unit_cost": "148.00",
        "unit_price": "210.00",
        "lead_time_days": 3,
        "shelf_life_days": 180,
        "abc_segment": "B",
        "base_daily_mean": 1.9,
        "festival_sensitivity": 0.45,
        "noise_cv": 0.22,
    },
    # Dairy & Bakery (7 more -> 8 total, short shelf life / perishables)
    {
        "sku_code": "SKU-DAIRY-002",
        "name": "Toned Milk Pouch 500ml",
        "hinglish_alias": "Halka Doodh 500ml",
        "category": "Dairy & Bakery",
        "unit": "pack",
        "unit_cost": "24.00",
        "unit_price": "29.00",
        "salvage_value": "4.00",
        "lead_time_days": 1,
        "shelf_life_days": 2,
        "perishable": True,
        "abc_segment": "A",
        "base_daily_mean": 22.0,
        "festival_sensitivity": 0.30,
        "noise_cv": 0.13,
    },
    {
        "sku_code": "SKU-DAIRY-003",
        "name": "Fresh Malai Paneer 200g",
        "hinglish_alias": "Taaza Paneer 200g",
        "category": "Dairy & Bakery",
        "unit": "pack",
        "unit_cost": "68.00",
        "unit_price": "92.00",
        "salvage_value": "12.00",
        "lead_time_days": 1,
        "shelf_life_days": 4,
        "perishable": True,
        "abc_segment": "A",
        "base_daily_mean": 7.2,
        "festival_sensitivity": 0.85,
        "noise_cv": 0.17,
    },
    {
        "sku_code": "SKU-DAIRY-004",
        "name": "Set Curd Cup 400g",
        "hinglish_alias": "Dahi 400g",
        "category": "Dairy & Bakery",
        "unit": "cup",
        "unit_cost": "32.00",
        "unit_price": "45.00",
        "salvage_value": "5.00",
        "lead_time_days": 1,
        "shelf_life_days": 5,
        "perishable": True,
        "abc_segment": "A",
        "base_daily_mean": 9.4,
        "festival_sensitivity": 0.40,
        "noise_cv": 0.16,
    },
    {
        "sku_code": "SKU-DAIRY-005",
        "name": "Whole Wheat Sandwich Bread",
        "hinglish_alias": "Atta Bread",
        "category": "Dairy & Bakery",
        "unit": "loaf",
        "unit_cost": "36.00",
        "unit_price": "50.00",
        "salvage_value": "5.00",
        "lead_time_days": 1,
        "shelf_life_days": 4,
        "perishable": True,
        "abc_segment": "B",
        "base_daily_mean": 6.5,
        "festival_sensitivity": 0.20,
        "noise_cv": 0.18,
    },
    {
        "sku_code": "SKU-DAIRY-006",
        "name": "Salted Table Butter 100g",
        "hinglish_alias": "Makhan 100g",
        "category": "Dairy & Bakery",
        "unit": "pack",
        "unit_cost": "48.00",
        "unit_price": "60.00",
        "salvage_value": "15.00",
        "lead_time_days": 2,
        "shelf_life_days": 21,
        "perishable": True,
        "abc_segment": "B",
        "base_daily_mean": 4.4,
        "festival_sensitivity": 0.45,
        "noise_cv": 0.20,
    },
    {
        "sku_code": "SKU-DAIRY-007",
        "name": "Sweet Lassi Bottle 200ml",
        "hinglish_alias": "Meethi Lassi",
        "category": "Dairy & Bakery",
        "unit": "bottle",
        "unit_cost": "18.00",
        "unit_price": "25.00",
        "salvage_value": "2.00",
        "lead_time_days": 1,
        "shelf_life_days": 5,
        "perishable": True,
        "abc_segment": "B",
        "base_daily_mean": 5.2,
        "festival_sensitivity": 0.35,
        "noise_cv": 0.24,
    },
    {
        "sku_code": "SKU-DAIRY-008",
        "name": "Khoya Mawa Pack 250g",
        "hinglish_alias": "Mawa 250g",
        "category": "Dairy & Bakery",
        "unit": "pack",
        "unit_cost": "95.00",
        "unit_price": "135.00",
        "salvage_value": "10.00",
        "lead_time_days": 2,
        "shelf_life_days": 4,
        "perishable": True,
        "abc_segment": "C",
        "base_daily_mean": 0.85,
        "festival_sensitivity": 1.60,
        "noise_cv": 0.42,
    },
    # Staples (10 total)
    {
        "sku_code": "SKU-STP-001",
        "name": "Chakki Fresh Atta 5kg",
        "hinglish_alias": "Gehu Atta 5kg",
        "category": "Staples",
        "unit": "bag",
        "unit_cost": "188.00",
        "unit_price": "240.00",
        "lead_time_days": 2,
        "shelf_life_days": 90,
        "abc_segment": "A",
        "base_daily_mean": 6.4,
        "festival_sensitivity": 0.50,
        "noise_cv": 0.14,
    },
    {
        "sku_code": "SKU-STP-002",
        "name": "Basmati Aged Rice 5kg",
        "hinglish_alias": "Basmati Chawal 5kg",
        "category": "Staples",
        "unit": "bag",
        "unit_cost": "410.00",
        "unit_price": "540.00",
        "lead_time_days": 3,
        "shelf_life_days": 180,
        "abc_segment": "A",
        "base_daily_mean": 2.8,
        "festival_sensitivity": 0.80,
        "noise_cv": 0.17,
    },
    {
        "sku_code": "SKU-STP-003",
        "name": "Toor Arhar Dal 1kg",
        "hinglish_alias": "Arhar Dal 1kg",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "138.00",
        "unit_price": "178.00",
        "lead_time_days": 2,
        "shelf_life_days": 150,
        "abc_segment": "A",
        "base_daily_mean": 5.1,
        "festival_sensitivity": 0.35,
        "noise_cv": 0.15,
    },
    {
        "sku_code": "SKU-STP-004",
        "name": "Refined White Sugar 1kg",
        "hinglish_alias": "Cheeni 1kg",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "41.00",
        "unit_price": "52.00",
        "lead_time_days": 2,
        "shelf_life_days": 180,
        "abc_segment": "A",
        "base_daily_mean": 9.8,
        "festival_sensitivity": 0.85,
        "noise_cv": 0.14,
    },
    {
        "sku_code": "SKU-STP-005",
        "name": "Chana Besan 1kg",
        "hinglish_alias": "Besan 1kg",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "82.00",
        "unit_price": "112.00",
        "lead_time_days": 2,
        "shelf_life_days": 120,
        "abc_segment": "B",
        "base_daily_mean": 3.5,
        "festival_sensitivity": 0.95,
        "noise_cv": 0.19,
    },
    {
        "sku_code": "SKU-STP-006",
        "name": "Assam CTC Tea 250g",
        "hinglish_alias": "Chai Patti 250g",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "105.00",
        "unit_price": "145.00",
        "lead_time_days": 3,
        "shelf_life_days": 180,
        "abc_segment": "B",
        "base_daily_mean": 3.9,
        "festival_sensitivity": 0.35,
        "noise_cv": 0.18,
    },
    {
        "sku_code": "SKU-STP-007",
        "name": "Iodized Salt 1kg",
        "hinglish_alias": "Namak 1kg",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "19.00",
        "unit_price": "28.00",
        "lead_time_days": 2,
        "shelf_life_days": 180,
        "abc_segment": "B",
        "base_daily_mean": 7.6,
        "festival_sensitivity": 0.15,
        "noise_cv": 0.16,
    },
    {
        "sku_code": "SKU-STP-008",
        "name": "Poha Flattened Rice 500g",
        "hinglish_alias": "Poha 500g",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "34.00",
        "unit_price": "48.00",
        "lead_time_days": 2,
        "shelf_life_days": 120,
        "abc_segment": "B",
        "base_daily_mean": 3.4,
        "festival_sensitivity": 0.30,
        "noise_cv": 0.21,
    },
    {
        "sku_code": "SKU-STP-009",
        "name": "Kashmiri Red Chilli Powder 200g",
        "hinglish_alias": "Lal Mirch 200g",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "78.00",
        "unit_price": "115.00",
        "lead_time_days": 3,
        "shelf_life_days": 180,
        "abc_segment": "C",
        "base_daily_mean": 1.0,
        "festival_sensitivity": 0.55,
        "noise_cv": 0.38,
    },
    {
        "sku_code": "SKU-STP-010",
        "name": "Organic Jaggery Powder 500g",
        "hinglish_alias": "Gud Powder 500g",
        "category": "Staples",
        "unit": "pack",
        "unit_cost": "58.00",
        "unit_price": "85.00",
        "lead_time_days": 4,
        "shelf_life_days": 150,
        "abc_segment": "C",
        "base_daily_mean": 0.72,
        "festival_sensitivity": 0.70,
        "noise_cv": 0.44,
    },
    # Snacks & Beverages (8 total)
    {
        "sku_code": "SKU-SNK-001",
        "name": "Aloo Bhujia Family Pack 400g",
        "hinglish_alias": "Namkeen Bhujia 400g",
        "category": "Snacks",
        "unit": "pack",
        "unit_cost": "72.00",
        "unit_price": "105.00",
        "lead_time_days": 2,
        "shelf_life_days": 90,
        "abc_segment": "A",
        "base_daily_mean": 6.0,
        "festival_sensitivity": 0.90,
        "noise_cv": 0.18,
    },
    {
        "sku_code": "SKU-SNK-002",
        "name": "Festive Soan Papdi Gift Box 500g",
        "hinglish_alias": "Soan Papdi Dibba",
        "category": "Snacks",
        "unit": "box",
        "unit_cost": "105.00",
        "unit_price": "165.00",
        "lead_time_days": 3,
        "shelf_life_days": 60,
        "abc_segment": "B",
        "base_daily_mean": 1.8,
        "festival_sensitivity": 1.85,
        "noise_cv": 0.28,
    },
    {
        "sku_code": "SKU-SNK-003",
        "name": "Mango Fruit Drink 1.2L",
        "hinglish_alias": "Aam Juice 1.2L",
        "category": "Snacks",
        "unit": "bottle",
        "unit_cost": "54.00",
        "unit_price": "75.00",
        "lead_time_days": 2,
        "shelf_life_days": 120,
        "abc_segment": "A",
        "base_daily_mean": 7.5,
        "festival_sensitivity": 0.70,
        "noise_cv": 0.17,
    },
    {
        "sku_code": "SKU-SNK-004",
        "name": "Butter Marie Biscuits 250g",
        "hinglish_alias": "Marie Biscuit",
        "category": "Snacks",
        "unit": "pack",
        "unit_cost": "26.00",
        "unit_price": "38.00",
        "lead_time_days": 2,
        "shelf_life_days": 120,
        "abc_segment": "B",
        "base_daily_mean": 5.6,
        "festival_sensitivity": 0.25,
        "noise_cv": 0.19,
    },
    {
        "sku_code": "SKU-SNK-005",
        "name": "Instant Masala Noodles 4-Pack",
        "hinglish_alias": "Masala Noodles",
        "category": "Snacks",
        "unit": "pack",
        "unit_cost": "44.00",
        "unit_price": "60.00",
        "lead_time_days": 2,
        "shelf_life_days": 150,
        "abc_segment": "A",
        "base_daily_mean": 8.4,
        "festival_sensitivity": 0.20,
        "noise_cv": 0.15,
    },
    {
        "sku_code": "SKU-SNK-006",
        "name": "Roasted Salted Cashews 200g",
        "hinglish_alias": "Kaju Pack 200g",
        "category": "Snacks",
        "unit": "pack",
        "unit_cost": "195.00",
        "unit_price": "275.00",
        "lead_time_days": 4,
        "shelf_life_days": 120,
        "abc_segment": "B",
        "base_daily_mean": 1.6,
        "festival_sensitivity": 1.45,
        "noise_cv": 0.26,
    },
    {
        "sku_code": "SKU-SNK-007",
        "name": "Spiced Makhana Foxnuts 100g",
        "hinglish_alias": "Bhuna Makhana",
        "category": "Snacks",
        "unit": "pack",
        "unit_cost": "88.00",
        "unit_price": "135.00",
        "lead_time_days": 4,
        "shelf_life_days": 90,
        "abc_segment": "C",
        "base_daily_mean": 0.65,
        "festival_sensitivity": 0.85,
        "noise_cv": 0.46,
    },
    {
        "sku_code": "SKU-SNK-008",
        "name": "Imported Dark Chocolate Bar 90g",
        "hinglish_alias": "Dark Chocolate",
        "category": "Snacks",
        "unit": "bar",
        "unit_cost": "140.00",
        "unit_price": "210.00",
        "lead_time_days": 5,
        "shelf_life_days": 150,
        "abc_segment": "C",
        "base_daily_mean": 0.42,
        "festival_sensitivity": 0.90,
        "noise_cv": 0.52,
    },
    # Micro-D2C (7 more -> 8 total, higher margins, MOQ & pack_size constraints)
    {
        "sku_code": "SKU-D2C-002",
        "name": "Neem & Tulsi Face Wash 150ml",
        "hinglish_alias": "Neem Facewash",
        "category": "Micro-D2C",
        "unit": "tube",
        "unit_cost": "95.00",
        "unit_price": "225.00",
        "lead_time_days": 5,
        "shelf_life_days": 180,
        "moq": 10,
        "pack_size": 5,
        "abc_segment": "A",
        "base_daily_mean": 4.9,
        "festival_sensitivity": 0.55,
        "noise_cv": 0.17,
    },
    {
        "sku_code": "SKU-D2C-003",
        "name": "Saffron Kumkumadi Serum 30ml",
        "hinglish_alias": "Kesar Serum 30ml",
        "category": "Micro-D2C",
        "unit": "bottle",
        "unit_cost": "240.00",
        "unit_price": "599.00",
        "lead_time_days": 5,
        "shelf_life_days": 180,
        "moq": 5,
        "pack_size": 5,
        "abc_segment": "B",
        "base_daily_mean": 2.4,
        "festival_sensitivity": 0.95,
        "noise_cv": 0.22,
    },
    {
        "sku_code": "SKU-D2C-004",
        "name": "Sandalwood Brass Diya Gift Set",
        "hinglish_alias": "Pital Diya Set",
        "category": "Micro-D2C",
        "unit": "set",
        "unit_cost": "210.00",
        "unit_price": "449.00",
        "lead_time_days": 5,
        "shelf_life_days": 365,
        "moq": 5,
        "pack_size": 5,
        "abc_segment": "B",
        "base_daily_mean": 1.4,
        "festival_sensitivity": 1.75,
        "noise_cv": 0.30,
    },
    {
        "sku_code": "SKU-D2C-005",
        "name": "Artisanal Masala Chai Tin 200g",
        "hinglish_alias": "Masala Chai Dabba",
        "category": "Micro-D2C",
        "unit": "tin",
        "unit_cost": "135.00",
        "unit_price": "299.00",
        "lead_time_days": 4,
        "shelf_life_days": 180,
        "moq": 10,
        "pack_size": 5,
        "abc_segment": "B",
        "base_daily_mean": 3.1,
        "festival_sensitivity": 0.65,
        "noise_cv": 0.21,
    },
    {
        "sku_code": "SKU-D2C-006",
        "name": "Handloom Cotton Table Runner",
        "hinglish_alias": "Cotton Runner",
        "category": "Micro-D2C",
        "unit": "piece",
        "unit_cost": "220.00",
        "unit_price": "499.00",
        "lead_time_days": 5,
        "shelf_life_days": 365,
        "moq": 5,
        "pack_size": 5,
        "abc_segment": "C",
        "base_daily_mean": 0.68,
        "festival_sensitivity": 1.15,
        "noise_cv": 0.45,
    },
    {
        "sku_code": "SKU-D2C-007",
        "name": "Soy Wax Mogra Scented Candle",
        "hinglish_alias": "Mogra Candle",
        "category": "Micro-D2C",
        "unit": "jar",
        "unit_cost": "115.00",
        "unit_price": "279.00",
        "lead_time_days": 4,
        "shelf_life_days": 365,
        "moq": 10,
        "pack_size": 5,
        "abc_segment": "C",
        "base_daily_mean": 0.80,
        "festival_sensitivity": 1.35,
        "noise_cv": 0.42,
    },
    {
        "sku_code": "SKU-D2C-008",
        "name": "Copper Insulated Water Bottle 1L",
        "hinglish_alias": "Tamba Bottle 1L",
        "category": "Micro-D2C",
        "unit": "bottle",
        "unit_cost": "310.00",
        "unit_price": "699.00",
        "lead_time_days": 5,
        "shelf_life_days": 365,
        "moq": 5,
        "pack_size": 5,
        "abc_segment": "C",
        "base_daily_mean": 0.52,
        "festival_sensitivity": 0.85,
        "noise_cv": 0.48,
    },
]

SECONDARY_MERCHANT_SKUS: list[dict[str, Any]] = [
    {
        "id": "bbbbbbbb-bbbb-4bbb-8bbb-000000000001",
        "sku_code": "VERMA-OIL-001",
        "name": "Verma Mustard Oil 1L",
        "hinglish_alias": "Verma Sarso Tel",
        "category": "Cooking Oil",
        "unit": "bottle",
        "unit_cost": "148.00",
        "unit_price": "215.00",
        "lead_time_days": 3,
        "shelf_life_days": 90,
        "abc_segment": "A",
        "base_daily_mean": 3.0,
        "festival_sensitivity": 0.8,
        "noise_cv": 0.18,
    },
    {
        "id": "bbbbbbbb-bbbb-4bbb-8bbb-000000000002",
        "sku_code": "VERMA-STP-001",
        "name": "Verma Chakki Atta 5kg",
        "hinglish_alias": "Verma Atta",
        "category": "Staples",
        "unit": "bag",
        "unit_cost": "185.00",
        "unit_price": "235.00",
        "lead_time_days": 2,
        "shelf_life_days": 90,
        "abc_segment": "A",
        "base_daily_mean": 5.0,
        "festival_sensitivity": 0.4,
        "noise_cv": 0.16,
    },
    {
        "id": "bbbbbbbb-bbbb-4bbb-8bbb-000000000003",
        "sku_code": "VERMA-SNK-001",
        "name": "Verma Dry Fruit Mix 250g",
        "hinglish_alias": "Verma Mewa",
        "category": "Snacks",
        "unit": "pack",
        "unit_cost": "210.00",
        "unit_price": "295.00",
        "lead_time_days": 4,
        "shelf_life_days": 120,
        "abc_segment": "B",
        "base_daily_mean": 1.5,
        "festival_sensitivity": 1.2,
        "noise_cv": 0.25,
    },
]

# Day-of-week multipliers (Mon=0 .. Sun=6)
KIRANA_DOW = [0.92, 0.90, 0.94, 0.96, 1.04, 1.14, 1.10]
DAIRY_DOW = [1.05, 1.04, 1.03, 1.02, 1.01, 0.94, 0.91]
D2C_DOW = [0.95, 0.96, 1.00, 1.02, 1.06, 1.04, 0.97]


def _compute_calendar_row(
    target_date: date, festivals: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute signed distance to nearest Indian festival and smooth pre-surge uplift weight."""
    best_dist = 999
    nearest_name: str | None = None
    total_weight = 0.0
    is_holiday = False
    is_window = False

    for fest in festivals:
        f_date = date.fromisoformat(fest["date"])
        signed_dist = (f_date - target_date).days  # +k means festival is k days ahead
        if abs(signed_dist) < abs(best_dist):
            best_dist = signed_dist
            nearest_name = fest["name"]

        pre_days = int(fest["pre_days"])
        post_days = int(fest["post_days"])
        peak_uplift = float(fest["peak_uplift"])

        if signed_dist == 0:
            is_holiday = True

        if -post_days <= signed_dist <= pre_days:
            is_window = True
            # Asymmetric wholesale stocking curve peaking ~5 days before festival
            if signed_dist >= 0:
                center = pre_days * 0.42
                sigma = max(pre_days * 0.35, 2.0)
                w = peak_uplift * math.exp(-0.5 * ((signed_dist - center) / sigma) ** 2)
            else:
                w = peak_uplift * 0.25 * math.exp(signed_dist / max(post_days, 1))
            total_weight = max(total_weight, w)

    clamped_dist = max(-30, min(60, best_dist))
    is_payday = target_date.day <= 5 or target_date.day >= 25
    return {
        "event_date": target_date,
        "festival_name": nearest_name if abs(best_dist) <= 14 else None,
        "days_to_nearest_festival": clamped_dist,
        "festival_pre_surge_weight": Decimal(f"{min(total_weight, 1.5):.4f}"),
        "is_festival_window": is_window,
        "is_payday_window": is_payday,
        "is_holiday": is_holiday,
    }


def _dow_multiplier(category: str, dow: int) -> float:
    if category == "Dairy & Bakery":
        return DAIRY_DOW[dow]
    if category == "Micro-D2C":
        return D2C_DOW[dow]
    return KIRANA_DOW[dow]


def seed_database(target_engine: Engine | None = None, *, reset: bool = True) -> dict[str, int]:
    """Deterministically populate merchants, Indian calendar, 43 SKUs, and 29k+ sales rows."""
    cfg = load_demo_config()
    settings = get_settings()
    rng = np.random.default_rng(int(cfg["seed"]))

    eng = target_engine or default_engine
    if reset:
        Base.metadata.drop_all(bind=eng)
    init_db(eng)

    session_factory = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    as_of = date.fromisoformat(cfg["as_of_date"])
    history_days = int(cfg["history_days"])
    start_date = as_of - timedelta(days=history_days - 1)
    cal_end_date = as_of + timedelta(days=35)
    festivals = cfg["festivals"]
    stockout_rate = float(cfg.get("stockout_rate", 0.03))
    now_utc = datetime.now(UTC)

    with session_factory() as db:
        # 1. Merchants (Primary + Secondary for IDOR / tenant isolation verification)
        primary_cfg = cfg["merchant"]
        secondary_cfg = cfg["secondary_merchant"]

        primary_merchant = Merchant(
            id=primary_cfg["id"],
            name=primary_cfg["name"],
            api_key_hash=hash_api_key(settings.vani_api_key),
            weekly_budget=Decimal(str(primary_cfg["weekly_budget"])),
            timezone=primary_cfg["timezone"],
            created_at=now_utc,
        )
        secondary_merchant = Merchant(
            id=secondary_cfg["id"],
            name=secondary_cfg["name"],
            api_key_hash=hash_api_key(settings.secondary_api_key),
            weekly_budget=Decimal(str(secondary_cfg["weekly_budget"])),
            timezone=secondary_cfg["timezone"],
            created_at=now_utc,
        )
        db.add_all([primary_merchant, secondary_merchant])

        # 2. Indian Festival & Payday Calendar
        calendar_rows: list[dict[str, Any]] = []
        cal_by_date: dict[date, dict[str, Any]] = {}
        cur = start_date
        while cur <= cal_end_date:
            row = _compute_calendar_row(cur, festivals)
            calendar_rows.append(row)
            cal_by_date[cur] = row
            cur += timedelta(days=1)
        db.bulk_insert_mappings(CalendarEvent, calendar_rows)

        # 3. SKUs (40 for Primary Merchant + 3 for Secondary Merchant)
        primary_sku_specs: list[dict[str, Any]] = list(cfg["hero_skus"])
        for idx, tmpl in enumerate(CATALOG_TEMPLATES, start=4):
            spec = dict(tmpl)
            spec["id"] = f"aaaaaaaa-aaaa-4aaa-8aaa-{idx:012d}"
            spec.setdefault("salvage_value", "0.00")
            spec.setdefault("moq", 1)
            spec.setdefault("pack_size", 1)
            spec.setdefault("perishable", False)
            spec.setdefault("on_hand", f"{max(2, round(spec['base_daily_mean'] * 2.2)):.3f}")
            spec.setdefault("on_order", "0.000")
            primary_sku_specs.append(spec)

        sku_models: list[Sku] = []
        for spec in primary_sku_specs:
            sku_models.append(
                Sku(
                    id=spec["id"],
                    merchant_id=primary_merchant.id,
                    sku_code=spec["sku_code"],
                    name=spec["name"],
                    hinglish_alias=spec.get("hinglish_alias"),
                    category=spec["category"],
                    unit=spec["unit"],
                    unit_cost=Decimal(str(spec["unit_cost"])),
                    unit_price=Decimal(str(spec["unit_price"])),
                    salvage_value=Decimal(str(spec.get("salvage_value", "0.00"))),
                    lead_time_days=int(spec["lead_time_days"]),
                    shelf_life_days=int(spec["shelf_life_days"]),
                    moq=int(spec.get("moq", 1)),
                    pack_size=int(spec.get("pack_size", 1)),
                    perishable=bool(spec.get("perishable", False)),
                    on_hand=Decimal(str(spec.get("on_hand", "5.000"))),
                    on_order=Decimal(str(spec.get("on_order", "0.000"))),
                    abc_segment=str(spec["abc_segment"]),
                    active=True,
                )
            )

        for spec in SECONDARY_MERCHANT_SKUS:
            sku_models.append(
                Sku(
                    id=spec["id"],
                    merchant_id=secondary_merchant.id,
                    sku_code=spec["sku_code"],
                    name=spec["name"],
                    hinglish_alias=spec.get("hinglish_alias"),
                    category=spec["category"],
                    unit=spec["unit"],
                    unit_cost=Decimal(str(spec["unit_cost"])),
                    unit_price=Decimal(str(spec["unit_price"])),
                    salvage_value=Decimal("0.00"),
                    lead_time_days=int(spec["lead_time_days"]),
                    shelf_life_days=int(spec["shelf_life_days"]),
                    moq=1,
                    pack_size=1,
                    perishable=False,
                    on_hand=Decimal("8.000"),
                    on_order=Decimal("0.000"),
                    abc_segment=str(spec["abc_segment"]),
                    active=True,
                )
            )
        db.add_all(sku_models)
        db.flush()

        # 4. Daily Sales History (40 x 730 = 29,200 primary + 3 x 90 = 270 secondary)
        sales_rows: list[dict[str, Any]] = []
        all_dates = [start_date + timedelta(days=i) for i in range(history_days)]

        def _append_sales_for_skus(
            merchant_id: str, sku_specs: list[dict[str, Any]], dates: list[date]
        ) -> None:
            n_days = len(dates)
            for spec in sku_specs:
                base_mean = float(spec["base_daily_mean"])
                fest_sens = float(spec["festival_sensitivity"])
                noise_cv = float(spec["noise_cv"])
                category = str(spec["category"])
                unit_price = Decimal(str(spec["unit_price"]))
                is_sparse = str(spec["abc_segment"]) == "C"
                channel = "online" if category == "Micro-D2C" else "offline"

                for day_idx, d in enumerate(dates):
                    cal = cal_by_date[d]
                    dow_mult = _dow_multiplier(category, d.weekday())
                    payday_mult = 1.09 if cal["is_payday_window"] else 0.97
                    fest_w = float(cal["festival_pre_surge_weight"])
                    fest_mult = 1.0 + fest_sens * fest_w
                    promo_flag = bool(
                        (cal["is_festival_window"] and rng.random() < 0.25)
                        or (not cal["is_festival_window"] and rng.random() < 0.04)
                    )
                    promo_mult = 1.15 if promo_flag else 1.0
                    # Gentle +/-4% multi-year drift so models see realistic non-stationarity
                    trend_mult = 0.97 + 0.06 * (day_idx / max(n_days - 1, 1))

                    lam = max(
                        0.05,
                        base_mean * dow_mult * payday_mult * fest_mult * promo_mult * trend_mult,
                    )

                    if is_sparse and rng.random() < 0.35:
                        true_qty = 0
                    else:
                        # Gamma-Poisson mixture for realistic retail overdispersion
                        gamma_scale = max(noise_cv**2, 0.01)
                        noisy_lam = float(
                            rng.gamma(shape=1.0 / gamma_scale, scale=lam * gamma_scale)
                        )
                        true_qty = int(rng.poisson(max(noisy_lam, 0.01)))

                    # Avoid stockout censoring on the very last 3 days of the hero demo SKU
                    allow_stockout = not (
                        spec["sku_code"] == "SKU-OIL-001" and day_idx >= n_days - 7
                    )
                    stockout_flag = bool(allow_stockout and rng.random() < stockout_rate)
                    observed_qty = (
                        int(math.floor(true_qty * rng.uniform(0.0, 0.25)))
                        if stockout_flag
                        else true_qty
                    )

                    sales_rows.append(
                        {
                            "merchant_id": merchant_id,
                            "sku_id": spec["id"],
                            "sold_date": d,
                            "qty": Decimal(str(observed_qty)),
                            "unit_price": unit_price,
                            "promo_flag": promo_flag,
                            "stockout_flag": stockout_flag,
                            "channel": channel,
                            "source_doc_id": None,
                            "created_at": now_utc,
                        }
                    )

        _append_sales_for_skus(primary_merchant.id, primary_sku_specs, all_dates)
        _append_sales_for_skus(secondary_merchant.id, SECONDARY_MERCHANT_SKUS, all_dates[-90:])
        db.bulk_insert_mappings(Sale, sales_rows)
        db.commit()

        return {
            "merchants": 2,
            "calendar_events": len(calendar_rows),
            "skus": len(sku_models),
            "sales": len(sales_rows),
            "primary_sales": len(primary_sku_specs) * len(all_dates),
        }


def main() -> None:
    counts = seed_database(reset=True)
    print(
        f"Seeded VANI-LEDGER DB: merchants={counts['merchants']} "
        f"skus={counts['skus']} calendar_events={counts['calendar_events']} "
        f"sales={counts['sales']} (primary={counts['primary_sales']})"
    )


if __name__ == "__main__":
    main()
