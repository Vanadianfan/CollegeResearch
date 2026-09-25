#!/usr/bin/env python3
"""Orchestrate the project's independent raw-data downloaders."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from download_n02_data import download_n02_data
from download_population_data import download_population_data
from download_s12_data import download_s12_data


# ---------------------------------------------------------------------------
# Ekidata.jp parameters remain here because it is the only optional source
# requiring a login cookie.  Public datasets have independent downloaders.
# ---------------------------------------------------------------------------

EKIDATA_JOIN_URL = "https://ekidata.jp/dl/f.php?t=6&d=20260618"
EKIDATA_LINE_URL = "https://ekidata.jp/dl/f.php?t=3&d=20260618"
EKIDATA_STATION_URL = "https://ekidata.jp/dl/f.php?t=5&d=20260713"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "raw_data"
USER_AGENT = "railway-research-raw-data-downloader/1.0"
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Download:
    name: str
    url: str
    destination: Path
    sha256: str | None = None
    requires_login: bool = False


EKIDATA_DOWNLOADS: tuple[Download, ...] = (
    Download(
        name="駅データ.jp 接続駅",
        url=EKIDATA_JOIN_URL,
        destination=RAW_DATA_DIR / "駅データ.jp" / "join20260618.csv",
        requires_login=True,
    ),
    Download(
        name="駅データ.jp 路線",
        url=EKIDATA_LINE_URL,
        destination=RAW_DATA_DIR / "駅データ.jp" / "line20260618free.csv",
        requires_login=True,
    ),
    Download(
        name="駅データ.jp 駅",
        url=EKIDATA_STATION_URL,
        destination=RAW_DATA_DIR / "駅データ.jp" / "station20260713free.csv",
        requires_login=True,
    ),
)

DATASET_NAMES = ("n02", "s12", "population", "ekidata")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="研究用の外部生データを raw_data/ にダウンロードします。"
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=DATASET_NAMES,
        dest="datasets",
        help=(
            "取得対象。複数回指定可。省略時は公開データの n02、s12、population。"
            "population は2020年250m人口の全国ZIP、"
            "ekidata はログイン Cookie が必要です。"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "既存の対象ファイル／ディレクトリを新しいダウンロードで"
            "置換します。"
        ),
    )
    parser.add_argument(
        "--ekidata-cookie",
        default=os.environ.get("EKIDATA_COOKIE"),
        metavar="COOKIE",
        help=(
            "駅データ.jp のログイン Cookie（例: PHPSESSID=...）。"
            "環境変数 EKIDATA_COOKIE でも指定できます。"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="設定済みの保存先と URL を表示して終了します。",
    )
    return parser.parse_args()


def format_bytes(byte_count: int) -> str:
    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")


def print_downloads() -> None:
    print("[n02]")
    download_n02_data(force=False, list_only=True)
    print("[s12]")
    download_s12_data(force=False, list_only=True)
    print("[population]")
    print("  raw_data/e-stat_population_2020_250m/")
    print("    e-Stat T001142（一次メッシュ一覧を実行時に自動取得）")
    print("[ekidata]")
    for item in EKIDATA_DOWNLOADS:
        print(f"  {item.destination.relative_to(PROJECT_ROOT)}")
        print(f"    {item.url}")


def request_headers(item: Download, ekidata_cookie: str | None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if item.requires_login:
        if not ekidata_cookie:
            raise RuntimeError(
                "駅データ.jp は会員ログインが必要です。"
                "ブラウザでログイン後、"
                "--ekidata-cookie 'PHPSESSID=...' または環境変数 EKIDATA_COOKIE "
                "を指定してください。"
            )
        headers["Cookie"] = ekidata_cookie
        headers["Referer"] = "https://ekidata.jp/dl/?p=1"
    return headers


def download_to_temp(item: Download, ekidata_cookie: str | None) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_handle, temp_name = tempfile.mkstemp(
        prefix=".download-", suffix=item.destination.suffix, dir=RAW_DATA_DIR
    )
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    request = urllib.request.Request(
        item.url, headers=request_headers(item, ekidata_cookie)
    )

    try:
        with os.fdopen(file_handle, "wb") as output:
            with urllib.request.urlopen(request, timeout=60) as response:
                content_type = response.headers.get_content_type()
                if item.requires_login and content_type in {
                    "text/html",
                    "application/xhtml+xml",
                }:
                    raise RuntimeError(
                        "駅データ.jp からログインページが返されました。"
                        "Cookie の有効期限を確認してください。"
                    )

                downloaded = 0
                while chunk := response.read(CHUNK_SIZE):
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    print(
                        f"\r  受信: {format_bytes(downloaded)}",
                        end="",
                        flush=True,
                    )
        print()
        if downloaded == 0:
            raise RuntimeError("空のファイルが返されました。")
        if item.sha256 and digest.hexdigest().lower() != item.sha256.lower():
            raise RuntimeError(
                "SHA-256 が一致しません: "
                f"expected={item.sha256}, actual={digest.hexdigest()}"
            )
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def install_file(temp_path: Path, destination: Path, force: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raise FileExistsError(destination)
    temp_path.replace(destination)


def fetch(item: Download, force: bool, ekidata_cookie: str | None) -> None:
    relative = item.destination.relative_to(PROJECT_ROOT)
    if item.destination.exists() and not force:
        print(f"[SKIP] {item.name}: {relative} は既に存在します。")
        return

    print(f"[GET]  {item.name}")
    print(f"       {item.url}")
    temp_path = download_to_temp(item, ekidata_cookie)
    try:
        install_file(temp_path, item.destination, force)
        print(f"[OK]   {relative}")
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    if args.list:
        print_downloads()
        return 0

    datasets = args.datasets or ["n02", "s12", "population"]
    try:
        for dataset in datasets:
            if dataset == "n02":
                download_n02_data(force=args.force)
            elif dataset == "s12":
                download_s12_data(force=args.force)
            elif dataset == "population":
                download_population_data(
                    mesh_codes=None,
                    output=RAW_DATA_DIR / "e-stat_population_2020_250m",
                    force=args.force,
                    workers=4,
                )
            else:
                for item in EKIDATA_DOWNLOADS:
                    fetch(item, args.force, args.ekidata_cookie)
    except (OSError, RuntimeError, urllib.error.URLError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if "ekidata" not in datasets:
        print(
            "[INFO] 駅データ.jp は会員ログインが必要なため、"
            "自動ダウンロード対象外です。"
        )
        print("       必要な場合は https://ekidata.jp/dl/ から取得してください。")
        print(
            "       station*free.csv / line*free.csv / join*.csv を "
            "raw_data/駅データ.jp/ に配置します。"
        )
    print("完了しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
