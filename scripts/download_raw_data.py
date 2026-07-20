#!/usr/bin/env python3
"""Download the project's external raw datasets into ``raw_data/``.

N02 and S12 are public ZIP files.  Ekidata.jp requires a free member login;
its download URLs are kept here as parameters, but a valid session cookie must
be supplied explicitly.  No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


# ---------------------------------------------------------------------------
# Download parameters.  Update these values when a source publishes a new
# edition.  SHA-256 is optional because the official files may be corrected in
# place; set it when a fixed, reproducible snapshot is required.
# ---------------------------------------------------------------------------

N02_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/N02/N02-24/N02-24_GML.zip"
S12_URL = "https://nlftp.mlit.go.jp/ksj/gml/data/S12/S12-25/S12-25_GML.zip"

EKIDATA_JOIN_URL = "https://ekidata.jp/dl/f.php?t=6&d=20260618"
EKIDATA_LINE_URL = "https://ekidata.jp/dl/f.php?t=3&d=20260618"
EKIDATA_STATION_URL = "https://ekidata.jp/dl/f.php?t=5&d=20260713"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "raw_data"
USER_AGENT = "railway-research-raw-data-downloader/1.0"
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Download:
    name: str
    url: str
    destination: Path
    archive: bool = False
    sha256: str | None = None
    requires_login: bool = False


DOWNLOADS: dict[str, tuple[Download, ...]] = {
    "n02": (
        Download(
            name="国土数値情報 N02-24 鉄道データ",
            url=N02_URL,
            destination=RAW_DATA_DIR / "N02-24_GML",
            archive=True,
        ),
    ),
    "s12": (
        Download(
            name="国土数値情報 S12-25 駅別乗降客数",
            url=S12_URL,
            destination=RAW_DATA_DIR / "S12-25_GML",
            archive=True,
        ),
    ),
    "ekidata": (
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
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="研究用の外部生データを raw_data/ にダウンロードします。"
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=tuple(DOWNLOADS),
        dest="datasets",
        help=(
            "取得対象。複数回指定可。省略時は公開データの n02 と s12。"
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
    for dataset, downloads in DOWNLOADS.items():
        print(f"[{dataset}]")
        for item in downloads:
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
    suffix = ".zip" if item.archive else item.destination.suffix
    file_handle, temp_name = tempfile.mkstemp(
        prefix=".download-", suffix=suffix, dir=RAW_DATA_DIR
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
                if item.archive and content_type not in {
                    "application/zip",
                    "application/octet-stream",
                }:
                    raise RuntimeError(
                        f"ZIP ではない応答です: Content-Type={content_type}"
                    )
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
        if item.archive and not zipfile.is_zipfile(temp_path):
            raise RuntimeError("応答内容は有効な ZIP ファイルではありません。")
        if item.sha256 and digest.hexdigest().lower() != item.sha256.lower():
            raise RuntimeError(
                "SHA-256 が一致しません: "
                f"expected={item.sha256}, actual={digest.hexdigest()}"
            )
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def validate_zip_member(member: zipfile.ZipInfo) -> None:
    path = PurePosixPath(member.filename)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"安全でない ZIP 内パスです: {member.filename}")
    if member.is_dir():
        return
    unix_mode = member.external_attr >> 16
    if (unix_mode & 0o170000) == 0o120000:
        raise RuntimeError(
            f"ZIP 内のシンボリックリンクを拒否しました: {member.filename}"
        )


def extract_zip(temp_path: Path, destination: Path, force: bool) -> None:
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=RAW_DATA_DIR) as temp_dir:
        extracted = Path(temp_dir)
        with zipfile.ZipFile(temp_path) as archive:
            for member in archive.infolist():
                validate_zip_member(member)
            archive.extractall(extracted)

        children = list(extracted.iterdir())
        has_named_wrapper = (
            len(children) == 1
            and children[0].is_dir()
            and children[0].name == destination.name
        )
        source = children[0] if has_named_wrapper else extracted
        if destination.exists():
            if not force:
                raise FileExistsError(destination)
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source == extracted:
            destination.mkdir()
            for child in children:
                shutil.move(str(child), destination / child.name)
        else:
            shutil.move(str(source), destination)


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
        if item.archive:
            extract_zip(temp_path, item.destination, force)
        else:
            install_file(temp_path, item.destination, force)
        print(f"[OK]   {relative}")
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    if args.list:
        print_downloads()
        return 0

    datasets = args.datasets or ["n02", "s12"]
    try:
        for dataset in datasets:
            for item in DOWNLOADS[dataset]:
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
