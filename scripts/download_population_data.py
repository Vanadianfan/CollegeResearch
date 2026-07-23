#!/usr/bin/env python3
"""Download official e-Stat 2020 JGD2011 250 m population-mesh ZIPs."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = PROJECT_ROOT / "raw_data" / "e-stat_population_2020_250m"
STATS_ID = "T001142"
SEARCH_PAGE_URL = (
    "https://www.e-stat.go.jp/gis/statmap-search"
    "?page=1&type=1&toukeiCode=00200521&toukeiYear=2020"
    "&aggregateUnit=Q&serveyId=Q002005112020"
    f"&statsId={STATS_ID}&datum=2011"
)
SEARCH_API_URL = "https://www.e-stat.go.jp/gis/statmap-search/search_detail"
DOWNLOAD_URL = "https://www.e-stat.go.jp/gis/statmap-search/data"
USER_AGENT = "college-railway-research-population-downloader/1.0"
CHUNK_SIZE = 1024 * 1024
LINK_PATTERN = re.compile(
    r'href="(?P<href>/gis/statmap-search/data\?[^\"]+)"'
)
PAGE_PATTERN = re.compile(r'data-page="(?P<page>\d+)"')


@dataclass(frozen=True, slots=True)
class DownloadTarget:
    primary_mesh_code: str
    url: str

    @property
    def filename(self) -> str:
        return f"tbl{STATS_ID}Q{self.primary_mesh_code}.zip"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "令和2年国勢調査・JGD2011・250mメッシュ・人口及び世帯 "
            "(T001142) を e-Stat から取得します。"
        )
    )
    parser.add_argument(
        "--mesh-code",
        action="append",
        help="取得する一次メッシュ4桁。複数回指定可。省略時は全国を自動発見。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=RAW_ROOT,
        help="ZIP 保存先",
    )
    parser.add_argument("--force", action="store_true", help="既存 ZIP を再取得")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="同時ダウンロード数（既定: 4）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="対象コードと URL を表示して終了",
    )
    return parser.parse_args()


def request_bytes(url: str, *, attempts: int = 3) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read(), response.headers.get_content_type()
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts:
                raise
            time.sleep(float(2 ** (attempt - 1)))
    raise AssertionError("unreachable")


def search_parameters(page: int) -> dict[str, str]:
    return {
        "type": "1",
        "page": str(page),
        "toukeiCode": "00200521",
        "toukeiYear": "2020",
        "aggregateUnit": "Q",
        "serveyId": "Q002005112020",
        "statsId": STATS_ID,
        "datum": "2011",
        "mesh_data_flg": "1",
        "download_disp_flg": "1",
    }


def target_for_code(code: str) -> DownloadTarget:
    if len(code) != 4 or not code.isdigit():
        raise ValueError(f"一次メッシュコードは4桁です: {code!r}")
    query = urllib.parse.urlencode(
        {"statsId": STATS_ID, "code": code, "downloadType": "2"}
    )
    return DownloadTarget(code, f"{DOWNLOAD_URL}?{query}")


def _targets_from_detail(detail: str) -> list[DownloadTarget]:
    targets: list[DownloadTarget] = []
    for match in LINK_PATTERN.finditer(detail):
        href = html.unescape(match.group("href"))
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("statsId") != [STATS_ID] or query.get("downloadType") != ["2"]:
            continue
        codes = query.get("code", [])
        if len(codes) != 1:
            continue
        targets.append(target_for_code(codes[0]))
    return targets


def discover_targets() -> list[DownloadTarget]:
    targets_by_code: dict[str, DownloadTarget] = {}
    page = 1
    last_page = 1
    while page <= last_page:
        url = f"{SEARCH_API_URL}?{urllib.parse.urlencode(search_parameters(page))}"
        payload, content_type = request_bytes(url)
        if content_type != "application/json":
            raise RuntimeError(
                f"e-Stat 検索 API が JSON を返しません: {content_type}"
            )
        response = json.loads(payload.decode("utf-8"))
        detail = str(response.get("detail", ""))
        paginate = str(response.get("paginate", ""))
        page_numbers = [int(value) for value in PAGE_PATTERN.findall(paginate)]
        if page_numbers:
            last_page = max(last_page, *page_numbers)
        page_targets = _targets_from_detail(detail)
        if not page_targets:
            raise RuntimeError(f"e-Stat 検索結果 page={page} に ZIP がありません。")
        for target in page_targets:
            targets_by_code[target.primary_mesh_code] = target
        page += 1
    if not targets_by_code:
        raise RuntimeError("e-Stat から人口メッシュ一覧を取得できませんでした。")
    return [targets_by_code[code] for code in sorted(targets_by_code)]


def validate_archive(path: Path, target: DownloadTarget) -> None:
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"ZIP ではない応答です: {target.url}")
    expected = f"tbl{STATS_ID}Q{target.primary_mesh_code}.txt"
    with zipfile.ZipFile(path) as archive:
        files = [item for item in archive.infolist() if not item.is_dir()]
        if len(files) != 1 or files[0].filename != expected:
            raise RuntimeError(
                f"{target.filename} の内容が想定外です: "
                f"{[item.filename for item in files]}"
            )


def download_one(
    target: DownloadTarget,
    output: Path,
    force: bool,
) -> tuple[str, str, int]:
    destination = output / target.filename
    if destination.is_file() and not force:
        validate_archive(destination, target)
        return target.primary_mesh_code, "SKIP", destination.stat().st_size

    output.mkdir(parents=True, exist_ok=True)
    file_handle, temp_name = tempfile.mkstemp(
        prefix=f".{target.filename}-", suffix=".part", dir=output
    )
    temp_path = Path(temp_name)
    try:
        request = urllib.request.Request(
            target.url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
        )
        with os.fdopen(file_handle, "wb") as stream:
            for attempt in range(1, 4):
                try:
                    with urllib.request.urlopen(request, timeout=90) as response:
                        while chunk := response.read(CHUNK_SIZE):
                            stream.write(chunk)
                    break
                except (TimeoutError, urllib.error.URLError):
                    if attempt == 3:
                        raise
                    stream.seek(0)
                    stream.truncate()
                    time.sleep(float(2 ** (attempt - 1)))
        validate_archive(temp_path, target)
        os.replace(temp_path, destination)
        return target.primary_mesh_code, "OK", destination.stat().st_size
    except Exception:
        try:
            os.close(file_handle)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def format_bytes(byte_count: int) -> str:
    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def write_manifest(output: Path, targets: list[DownloadTarget]) -> None:
    manifest = {
        "dataset": "令和2年国勢調査 JGD2011 250mメッシュ 人口及び世帯",
        "stats_id": STATS_ID,
        "population_field": "T001142001",
        "source_page": SEARCH_PAGE_URL,
        "primary_mesh_codes": [target.primary_mesh_code for target in targets],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def download_population_data(
    *,
    mesh_codes: list[str] | None,
    output: Path,
    force: bool,
    workers: int,
    list_only: bool = False,
) -> int:
    if workers < 1 or workers > 16:
        raise ValueError("--workers は 1..16 で指定してください。")
    targets = (
        [target_for_code(code) for code in sorted(set(mesh_codes))]
        if mesh_codes
        else discover_targets()
    )
    print(f"対象: {len(targets)} 個の一次メッシュ / statsId={STATS_ID}")
    print(f"保存先: {output.expanduser().resolve()}")
    if list_only:
        for target in targets:
            print(f"  M{target.primary_mesh_code}  {target.url}")
        return 0

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_by_target = {
            executor.submit(download_one, target, output, force): target
            for target in targets
        }
        for future in concurrent.futures.as_completed(future_by_target):
            target = future_by_target[future]
            try:
                code, status, size = future.result()
            except Exception as exc:
                for other in future_by_target:
                    other.cancel()
                raise RuntimeError(f"M{target.primary_mesh_code}: {exc}") from exc
            total_bytes += size
            print(f"[{status}] M{code}  {format_bytes(size)}")
    write_manifest(output, targets)
    print(f"完了: {len(targets)} ZIP / {format_bytes(total_bytes)}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return download_population_data(
            mesh_codes=args.mesh_code,
            output=args.output,
            force=args.force,
            workers=args.workers,
            list_only=args.list,
        )
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"[ERROR] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
