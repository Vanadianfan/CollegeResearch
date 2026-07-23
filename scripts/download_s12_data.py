#!/usr/bin/env python3
"""Download the MLIT S12-25 station ridership GML dataset."""

from __future__ import annotations

import argparse
import sys
import urllib.error

from download_zip_data import RAW_DATA_DIR, ZipDataset, download_zip_dataset


S12_DATASET = ZipDataset(
    name="国土数値情報 S12-25 駅別乗降客数",
    url="https://nlftp.mlit.go.jp/ksj/gml/data/S12/S12-25/S12-25_GML.zip",
    destination=RAW_DATA_DIR / "S12-25_GML",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="国土数値情報 S12-25 駅別乗降客数 GML を取得します。"
    )
    parser.add_argument("--force", action="store_true", help="既存データを置換")
    parser.add_argument(
        "--list", action="store_true", help="保存先と URL を表示して終了"
    )
    return parser.parse_args()


def download_s12_data(*, force: bool, list_only: bool = False) -> int:
    if list_only:
        print(f"  {S12_DATASET.destination.relative_to(RAW_DATA_DIR.parent)}")
        print(f"    {S12_DATASET.url}")
        return 0
    download_zip_dataset(S12_DATASET, force=force)
    return 0


def main() -> int:
    args = parse_args()
    try:
        result = download_s12_data(force=args.force, list_only=args.list)
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if not args.list:
        print("完了しました。")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
