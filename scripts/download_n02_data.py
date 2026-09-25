#!/usr/bin/env python3
"""Download the MLIT N02-24 railway GML dataset."""

from __future__ import annotations

import argparse
import sys
import urllib.error

from download_zip_data import RAW_DATA_DIR, ZipDataset, download_zip_dataset


N02_DATASET = ZipDataset(
    name="国土数値情報 N02-24 鉄道データ",
    url="https://nlftp.mlit.go.jp/ksj/gml/data/N02/N02-24/N02-24_GML.zip",
    destination=RAW_DATA_DIR / "N02-24_GML",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="国土数値情報 N02-24 鉄道 GML を取得します。"
    )
    parser.add_argument("--force", action="store_true", help="既存データを置換")
    parser.add_argument(
        "--list", action="store_true", help="保存先と URL を表示して終了"
    )
    return parser.parse_args()


def download_n02_data(*, force: bool, list_only: bool = False) -> int:
    if list_only:
        print(f"  {N02_DATASET.destination.relative_to(RAW_DATA_DIR.parent)}")
        print(f"    {N02_DATASET.url}")
        return 0
    download_zip_dataset(N02_DATASET, force=force)
    return 0


def main() -> int:
    args = parse_args()
    try:
        result = download_n02_data(force=args.force, list_only=args.list)
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if not args.list:
        print("完了しました。")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
