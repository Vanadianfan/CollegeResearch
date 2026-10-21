"""centrality/sensitivity_unbounded.py - 無時間截斷（全域）鐵路網參數敏感度分析入口"""

from __future__ import annotations

import argparse
from pathlib import Path

from .sensitivity import (
    DEFAULT_RAIL_DB,
    UNBOUNDED_OUTPUT_DB,
    run_sensitivity_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="執行無時間截斷（全域）鐵路網 OAT 參數敏感度分析，並儲存至 network_sensitivity_unbounded.sqlite"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_RAIL_DB, help="原始路網資料庫路徑")
    parser.add_argument("--output", type=Path, default=UNBOUNDED_OUTPUT_DB, help="無時間截斷敏感度分析結果資料庫路徑")
    parser.add_argument("--scenarios", nargs="+", default=None, help="指定執行的情境名稱（預設執行全部7種）")
    parser.add_argument("--force", action="store_true", help="強制重新計算已存在的情境")
    args = parser.parse_args()

    return run_sensitivity_pipeline(
        rail_db_path=args.database,
        out_db_path=args.output,
        time_cutoff_s=None,
        scenario_names=args.scenarios,
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
