"""One-command deterministic demo reset: seed + train + 3-fold walk-forward backtest (<15s)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ensure_project_root_on_path() -> None:
    root_str = str(PROJECT_ROOT)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def reset_demo(target_engine: Engine | None = None) -> dict[str, Any]:
    """Deterministically reset DB, train 5-stage ML pipeline, and generate backtest report."""
    _ensure_project_root_on_path()
    from eval.backtest import run_backtest

    from scripts.seed import seed_database
    from scripts.train import train_and_populate

    t0 = time.perf_counter()

    seed_counts = seed_database(target_engine=target_engine, reset=True)
    train_summary = train_and_populate(target_engine=target_engine, save_artifacts=True)
    backtest_report = run_backtest(
        target_engine=target_engine,
        save_report=True,
        gate=False,
    )

    elapsed = round(time.perf_counter() - t0, 2)
    return {
        "elapsed_seconds": elapsed,
        "seed_counts": seed_counts,
        "train_summary": train_summary,
        "backtest_report": backtest_report,
    }


def main() -> None:
    result = reset_demo()
    seed = result["seed_counts"]
    train = result["train_summary"]
    bt = result["backtest_report"]
    amp = bt["models"]["amplified_ensemble"]
    naive = bt["models"]["seasonal_naive"]
    gate_ok = bool(bt["gates"]["all_passed"])

    print(f"\n=== VANI-LEDGER Deterministic Demo Reset ({result['elapsed_seconds']:.2f}s) ===")
    print(
        f"[1/3 Seed]     merchants={seed['merchants']} | skus={seed['skus']} | "
        f"sales={seed['sales']:,} (primary={seed['primary_sales']:,}) | "
        f"calendar={seed['calendar_events']}"
    )
    print(
        f"[2/3 Train]    forecasts={train['total_forecasts']} | "
        f"as_of={train['as_of_date']} | version={train['model_version']}"
    )
    for code, info in train.get("hero_skus", {}).items():
        drivers_str = ", ".join(f"{d['name']} ({d['units']:+d})" for d in info.get("drivers", []))
        print(
            f"               {code}: qty={info['recommended_qty']} | "
            f"service={info['service_level']:.1%} | drivers=[{drivers_str}]"
        )
    print(
        f"[3/3 Backtest] Amplified WAPE={amp['revenue_weighted_wape']:.2%} vs "
        f"Naive={naive['revenue_weighted_wape']:.2%} "
        f"(lift {bt['lift_over_seasonal_naive_pct']:+.2f}%) | "
        f"A={amp['segment_wape']['A']:.2%} B={amp['segment_wape']['B']:.2%} "
        f"C={amp['segment_wape']['C']:.2%} | MASE={amp['mase']:.3f} | "
        f"p90 Cov={amp['p90_empirical_coverage']:.1%} | "
        f"GATE={'PASS' if gate_ok else 'FAIL'}"
    )


if __name__ == "__main__":
    main()
