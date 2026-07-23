"""Read and validate the official e-Stat T001142 ZIP archives."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


STATS_ID = "T001142"
POPULATION_FIELD = "T001142001"
EXPECTED_PREFIX = f"tbl{STATS_ID}Q"
ARCHIVE_PATTERN = re.compile(r"^tblT001142Q(?P<code>\d{4})\.zip$")


@dataclass(frozen=True, slots=True)
class RawPopulationMesh:
    mesh_code: str
    population: int
    disclosure_status: int
    aggregation_target_mesh_code: str | None
    aggregated_mesh_codes: str | None
    source_archive: str


def population_archives(raw_root: Path) -> list[Path]:
    root = raw_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(
            f"人口メッシュの保存先が見つかりません: {root}\n"
            "scripts/download_population_data.py を先に実行してください。"
        )
    archives = sorted(path for path in root.glob(f"{EXPECTED_PREFIX}*.zip"))
    if not archives:
        raise FileNotFoundError(f"{root} に {EXPECTED_PREFIX}*.zip がありません。")
    return archives


def _optional_text(value: str | None) -> str | None:
    normalized = "" if value is None else value.strip()
    return normalized or None


def _parse_status(value: str | None, *, archive: Path, row_no: int) -> int:
    normalized = "" if value is None else value.strip()
    if normalized not in {"0", "1", "2"}:
        raise ValueError(
            f"{archive.name}:{row_no}: HTKSYORI が不正です: {value!r}"
        )
    return int(normalized)


def iter_archive_rows(archive: Path) -> Iterator[RawPopulationMesh]:
    match = ARCHIVE_PATTERN.fullmatch(archive.name)
    if match is None:
        raise ValueError(f"人口メッシュ ZIP の名前が不正です: {archive.name}")
    primary_code = match.group("code")
    if not zipfile.is_zipfile(archive):
        raise ValueError(f"有効な ZIP ではありません: {archive}")

    expected_member = f"{archive.stem}.txt"
    with zipfile.ZipFile(archive) as zipped:
        members = [item for item in zipped.infolist() if not item.is_dir()]
        if len(members) != 1 or members[0].filename != expected_member:
            raise ValueError(
                f"{archive.name}: {expected_member} だけを含む必要があります。"
            )
        with zipped.open(members[0]) as binary:
            text = io.TextIOWrapper(binary, encoding="cp932", newline="")
            reader = csv.DictReader(text)
            required = {
                "KEY_CODE",
                "HTKSYORI",
                "HTKSAKI",
                "GASSAN",
                POPULATION_FIELD,
            }
            actual = set(reader.fieldnames or ())
            missing = required - actual
            if missing:
                raise ValueError(
                    f"{archive.name}: 必要列がありません: {sorted(missing)}"
                )
            for row_no, row in enumerate(reader, start=2):
                mesh_code = (row.get("KEY_CODE") or "").strip()
                if not mesh_code:
                    # The official file has a second, Japanese-title header row.
                    continue
                if (
                    len(mesh_code) != 10
                    or not mesh_code.isdigit()
                    or not mesh_code.startswith(primary_code)
                ):
                    raise ValueError(
                        f"{archive.name}:{row_no}: KEY_CODE が不正です: "
                        f"{mesh_code!r}"
                    )
                raw_population = (row.get(POPULATION_FIELD) or "").strip()
                if not raw_population.isdigit():
                    raise ValueError(
                        f"{archive.name}:{row_no}: {POPULATION_FIELD} が"
                        f"非負整数ではありません: {raw_population!r}"
                    )
                yield RawPopulationMesh(
                    mesh_code=mesh_code,
                    population=int(raw_population),
                    disclosure_status=_parse_status(
                        row.get("HTKSYORI"), archive=archive, row_no=row_no
                    ),
                    aggregation_target_mesh_code=_optional_text(
                        row.get("HTKSAKI")
                    ),
                    aggregated_mesh_codes=_optional_text(row.get("GASSAN")),
                    source_archive=archive.name,
                )


def iter_population_rows(raw_root: Path) -> Iterator[RawPopulationMesh]:
    for archive in population_archives(raw_root):
        yield from iter_archive_rows(archive)
