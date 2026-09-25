#!/usr/bin/env python3
"""Shared downloader for public ZIP datasets stored under ``raw_data/``."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "raw_data"
USER_AGENT = "railway-research-raw-data-downloader/1.0"
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ZipDataset:
    """Parameters required to download and install one public ZIP dataset."""

    name: str
    url: str
    destination: Path
    sha256: str | None = None


def format_bytes(byte_count: int) -> str:
    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")


def _download_to_temp(dataset: ZipDataset) -> Path:
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_handle, temp_name = tempfile.mkstemp(
        prefix=".download-", suffix=".zip", dir=RAW_DATA_DIR
    )
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    request = urllib.request.Request(
        dataset.url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )

    try:
        with os.fdopen(file_handle, "wb") as output:
            with urllib.request.urlopen(request, timeout=60) as response:
                content_type = response.headers.get_content_type()
                if content_type not in {
                    "application/zip",
                    "application/octet-stream",
                }:
                    raise RuntimeError(
                        f"ZIP ではない応答です: Content-Type={content_type}"
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
        if not zipfile.is_zipfile(temp_path):
            raise RuntimeError("応答内容は有効な ZIP ファイルではありません。")
        if dataset.sha256 and digest.hexdigest().lower() != dataset.sha256.lower():
            raise RuntimeError(
                "SHA-256 が一致しません: "
                f"expected={dataset.sha256}, actual={digest.hexdigest()}"
            )
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _validate_zip_member(member: zipfile.ZipInfo) -> None:
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


def _extract_zip(temp_path: Path, destination: Path, force: bool) -> None:
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=RAW_DATA_DIR) as temp_dir:
        extracted = Path(temp_dir)
        with zipfile.ZipFile(temp_path) as archive:
            for member in archive.infolist():
                _validate_zip_member(member)
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


def download_zip_dataset(dataset: ZipDataset, *, force: bool) -> None:
    """Download, validate, and extract a dataset unless it already exists."""

    relative = dataset.destination.relative_to(PROJECT_ROOT)
    if dataset.destination.exists() and not force:
        print(f"[SKIP] {dataset.name}: {relative} は既に存在します。")
        return

    print(f"[GET]  {dataset.name}")
    print(f"       {dataset.url}")
    temp_path = _download_to_temp(dataset)
    try:
        _extract_zip(temp_path, dataset.destination, force)
        print(f"[OK]   {relative}")
    finally:
        temp_path.unlink(missing_ok=True)
