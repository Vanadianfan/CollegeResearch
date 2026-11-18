"""centrality/sensitivity_cutoff.py - 有時間截斷（預設120分）鐵路網參數敏感度分析入口"""

from __future__ import annotations

import argparse
from pathlib import Path

from shared.params import DEFAULT_TIME_CUTOFF_S
from .sensitivity import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_RAIL_DB,
    build_sensitivity_db_path,
    run_sensitivity_pipeline,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="執行有時間截斷（預設120分 = 7200秒）鐵路網 OAT 參數敏感度分析，預設輸出至 network_sensitivity/ 目錄"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_RAIL_DB, help="原始路網資料庫路徑")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="有時間截斷敏感度分析結果資料庫路徑（預設依 region 寫入 network_sensitivity/）",
    )
    parser.add_argument(
        "--time-cutoff",
        type=float,
        default=DEFAULT_TIME_CUTOFF_S,
        help="時間截斷閾值（秒），預設為 7200.0（120分鐘）",
    )
    parser.add_argument("--scenarios", nargs="+", default=None, help="指定執行的情境名稱（預設執行全部7種）")
    parser.add_argument("--force", action="store_true", help="強制重新計算已存在的情境")
    parser.add_argument(
        "--region",
        choices=("tokyo", "kinki"),
        default=None,
        help="限定計算特定區域 (tokyo / kinki)，預設為全部營運路線",
    )
    args = parser.parse_args()

    out_db = args.output or build_sensitivity_db_path(
        DEFAULT_OUTPUT_DIR,
        region=args.region,
        mode="cutoff",
    )

    return run_sensitivity_pipeline(
        rail_db_path=args.database,
        out_db_path=out_db,
        time_cutoff_s=args.time_cutoff,
        scenario_names=args.scenarios,
        force=args.force,
        region=args.region,
    )


if __name__ == "__main__":
    raise SystemExit(main())
