"""Reverse-validate persisted station-group passenger values against raw S12.

This module intentionally does not call ``apply_group_passengers``.  It starts
from the rows already written to SQLite, rebuilds an N02-to-S12 reverse index,
and proves that every NULL lacks a complete, safely attributable primary sum.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from rail_data.paths import PROJECT_ROOT
from rail_data.schema import connect_database

from .geometry import curve_signature
from .models import Coord, RawPassengerStation, RawStation
from .passengers import locate_s12_input, parse_s12_2024
from .source import locate_input, parse_n02


NULL_REASON_DATA_UNAVAILABLE = "data_unavailable"
NULL_REASON_NONPUBLIC = "nonpublic"
NULL_REASON_MIXED_INCOMPLETE = "mixed_incomplete"
NULL_REASON_PRIMARY_NOT_FOUND = "primary_not_found"
NULL_REASON_NO_STATION = "no_station"
NULL_REASON_NO_SOURCE_RECORD = "no_source_record"

NULL_REASON_LABELS = {
    NULL_REASON_DATA_UNAVAILABLE: "dataEorN2024=2（データなし）",
    NULL_REASON_NONPUBLIC: "dataEorN2024=3（非公開）",
    NULL_REASON_MIXED_INCOMPLETE: "データなし・非公開が混在",
    NULL_REASON_PRIMARY_NOT_FOUND: "duplicate2024=2 のみ（同組に主記録なし）",
    NULL_REASON_NO_STATION: "duplicate2024=3（当該年度は駅なし）",
    NULL_REASON_NO_SOURCE_RECORD: "対応する S12 記録なし",
}


@dataclass(frozen=True)
class NullPassengerCheck:
    group_id: int
    group_code: str
    display_name: str
    reason: str
    source_record_count: int
    primary_source_ids: tuple[str, ...]
    primary_status_codes: tuple[str, ...]
    duplicate_reference_count: int


@dataclass(frozen=True)
class PassengerReverseValidationSummary:
    database_group_count: int
    numeric_group_count: int
    null_group_count: int
    null_reason_counts: dict[str, int]
    null_checks: tuple[NullPassengerCheck, ...]


def _validate_source_record(record: RawPassengerStation) -> None:
    if record.duplicate_code not in {"1", "2", "3"}:
        raise RuntimeError(
            f"S12 {record.source_id}: duplicate2024 が不正です: "
            f"{record.duplicate_code!r}"
        )
    if record.data_status_code not in {"1", "2", "3", "4"}:
        raise RuntimeError(
            f"S12 {record.source_id}: dataEorN2024 が不正です: "
            f"{record.data_status_code!r}"
        )
    if record.duplicate_code == "3" and record.data_status_code != "4":
        raise RuntimeError(
            f"S12 {record.source_id}: duplicate2024=3 と "
            f"dataEorN2024={record.data_status_code} が矛盾します"
        )
    if record.data_status_code == "4" and record.duplicate_code != "3":
        raise RuntimeError(
            f"S12 {record.source_id}: dataEorN2024=4 と "
            f"duplicate2024={record.duplicate_code} が矛盾します"
        )
    if (
        record.duplicate_code == "1"
        and record.data_status_code == "1"
        and record.passengers is None
    ):
        raise RuntimeError(
            f"S12 {record.source_id}: 有効な主記録に passengers2024 がありません"
        )


def _reverse_index_s12_records(
    n02_curves: dict[str, list[list[Coord]]],
    n02_stations: list[RawStation],
    s12_curves: dict[str, list[list[Coord]]],
    s12_stations: list[RawPassengerStation],
) -> dict[str, list[RawPassengerStation]]:
    """Independently map S12 rows back to their unique N02 group code."""

    all_group_codes = {station.group_code for station in n02_stations}
    groups_by_station_code: dict[str, set[str]] = defaultdict(set)
    groups_by_geometry: dict[tuple[tuple[Coord, ...], ...], set[str]] = defaultdict(
        set
    )
    for station in n02_stations:
        if station.station_code:
            groups_by_station_code[station.station_code].add(station.group_code)
        parts = n02_curves.get(station.curve_id, [])
        if parts:
            groups_by_geometry[curve_signature(parts)].add(station.group_code)

    records_by_group: dict[str, list[RawPassengerStation]] = defaultdict(list)
    seen_source_ids: set[str] = set()
    for record in s12_stations:
        if not record.source_id or record.source_id in seen_source_ids:
            raise RuntimeError(
                f"S12 source_id が空または重複しています: {record.source_id!r}"
            )
        seen_source_ids.add(record.source_id)
        _validate_source_record(record)

        code_candidates = groups_by_station_code.get(record.station_code, set())
        if len(code_candidates) > 1:
            raise RuntimeError(
                f"S12 {record.source_id}: stationCode={record.station_code} が "
                f"複数の N02 組に対応します: {sorted(code_candidates)}"
            )
        by_code = next(iter(code_candidates)) if code_candidates else None

        parts = s12_curves.get(record.curve_id, [])
        geometry_candidates = (
            groups_by_geometry.get(curve_signature(parts), set()) if parts else set()
        )
        by_geometry = (
            next(iter(geometry_candidates)) if len(geometry_candidates) == 1 else None
        )
        by_group_code = (
            record.group_code if record.group_code in all_group_codes else None
        )
        resolved = {value for value in (by_code, by_geometry, by_group_code) if value}
        if len(resolved) > 1:
            raise RuntimeError(
                f"S12 {record.source_id}: 反向対応候補が矛盾します: "
                f"stationCode={by_code}, geometry={by_geometry}, "
                f"groupCode={by_group_code}"
            )

        target_group = by_code or by_geometry or by_group_code
        if target_group is not None:
            records_by_group[target_group].append(record)

    return records_by_group


def _primary_records(
    group_code: str,
    records: list[RawPassengerStation],
) -> list[RawPassengerStation]:
    primary_by_station_code: dict[str, RawPassengerStation] = {}
    for record in records:
        if record.duplicate_code != "1":
            continue
        existing = primary_by_station_code.get(record.station_code)
        if existing is not None:
            raise RuntimeError(
                f"N02 groupCode={group_code}: stationCode={record.station_code} "
                f"の S12 主記録が重複します: "
                f"{existing.source_id}, {record.source_id}"
            )
        primary_by_station_code[record.station_code] = record
    return list(primary_by_station_code.values())


def _null_reason(
    primary: list[RawPassengerStation],
    records: list[RawPassengerStation],
) -> str:
    if primary:
        statuses = {record.data_status_code for record in primary}
        if statuses <= {"1"}:
            raise AssertionError("complete primary records do not have a NULL reason")
        if "2" in statuses and "3" in statuses:
            return NULL_REASON_MIXED_INCOMPLETE
        if "3" in statuses:
            return NULL_REASON_NONPUBLIC
        return NULL_REASON_DATA_UNAVAILABLE
    if any(record.duplicate_code == "2" for record in records):
        return NULL_REASON_PRIMARY_NOT_FOUND
    if any(record.duplicate_code == "3" for record in records):
        return NULL_REASON_NO_STATION
    return NULL_REASON_NO_SOURCE_RECORD


def validate_station_group_passengers(
    connection: sqlite3.Connection,
    n02_curves: dict[str, list[list[Coord]]],
    n02_stations: list[RawStation],
    s12_curves: dict[str, list[list[Coord]]],
    s12_stations: list[RawPassengerStation],
) -> PassengerReverseValidationSummary:
    """Prove persisted values and every NULL against an independent reverse scan."""

    records_by_group = _reverse_index_s12_records(
        n02_curves,
        n02_stations,
        s12_curves,
        s12_stations,
    )
    n02_group_codes = {station.group_code for station in n02_stations}
    database_rows = connection.execute(
        """
        SELECT id, group_code, display_name, passengers
        FROM station_group
        ORDER BY id
        """
    ).fetchall()

    errors: list[str] = []
    null_checks: list[NullPassengerCheck] = []
    numeric_count = 0
    for group_id, group_code, display_name, persisted_value in database_rows:
        if group_code not in n02_group_codes:
            errors.append(
                f"station_group#{group_id} groupCode={group_code} は N02 にありません"
            )
            continue

        records = records_by_group.get(group_code, [])
        primary = _primary_records(group_code, records)
        complete = bool(primary) and all(
            record.data_status_code == "1" and record.passengers is not None
            for record in primary
        )
        expected_value = (
            sum(
                record.passengers
                for record in primary
                if record.passengers is not None
            )
            if complete
            else None
        )

        if persisted_value is not None:
            numeric_count += 1
            if not complete:
                errors.append(
                    f"station_group#{group_id} {display_name} ({group_code}) は "
                    f"passengers={persisted_value} ですが、完全な主記録がありません"
                )
            elif persisted_value != expected_value:
                errors.append(
                    f"station_group#{group_id} {display_name} ({group_code}): "
                    f"DB={persisted_value}, S12再集計={expected_value}"
                )
            continue

        if complete:
            errors.append(
                f"station_group#{group_id} {display_name} ({group_code}) は NULL "
                f"ですが、S12から {expected_value} 人/日を完全に再構成できます"
            )
            continue

        reason = _null_reason(primary, records)
        null_checks.append(
            NullPassengerCheck(
                group_id=group_id,
                group_code=group_code,
                display_name=display_name,
                reason=reason,
                source_record_count=len(records),
                primary_source_ids=tuple(record.source_id for record in primary),
                primary_status_codes=tuple(
                    sorted({record.data_status_code for record in primary})
                ),
                duplicate_reference_count=sum(
                    record.duplicate_code == "2" for record in records
                ),
            )
        )

    if errors:
        preview = "\n  - ".join(errors[:20])
        suffix = "" if len(errors) <= 20 else f"\n  ... 他 {len(errors) - 20} 件"
        raise RuntimeError(
            "station_group.passengers 逆引き検証に失敗しました:\n  - "
            + preview
            + suffix
        )

    null_reason_counts = dict(Counter(check.reason for check in null_checks))
    null_count = len(null_checks)
    if numeric_count + null_count != len(database_rows):
        raise AssertionError("passenger reverse-validation group count is inconsistent")

    return PassengerReverseValidationSummary(
        database_group_count=len(database_rows),
        numeric_group_count=numeric_count,
        null_group_count=null_count,
        null_reason_counts=null_reason_counts,
        null_checks=tuple(null_checks),
    )


def print_validation_summary(
    summary: PassengerReverseValidationSummary,
    *,
    show_null_details: bool = False,
) -> None:
    print("[OK] station_group.passengers 逆引き検証")
    print(
        f"  DB全体: {summary.database_group_count:,} 組 / "
        f"数値 {summary.numeric_group_count:,} / NULL {summary.null_group_count:,}"
    )
    for reason, label in NULL_REASON_LABELS.items():
        print(f"  NULL {label}: {summary.null_reason_counts.get(reason, 0):,} 組")
    if show_null_details:
        for check in summary.null_checks:
            sources = ",".join(check.primary_source_ids) or "-"
            statuses = ",".join(check.primary_status_codes) or "-"
            print(
                f"  [NULL] group#{check.group_id} {check.display_name} "
                f"({check.group_code}) reason={check.reason}, "
                f"source_rows={check.source_record_count}, "
                f"primary={sources}, statuses={statuses}, "
                f"duplicate2={check.duplicate_reference_count}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "SQLite の station_group.passengers を N02-24 と S12-25 から "
            "独立に逆引き検証します。"
        )
    )
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "rail_network.sqlite",
        help="検証対象 SQLite（既定: rail_network.sqlite）",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="N02-24 XML、ZIP、または N02-24_GML ディレクトリ",
    )
    parser.add_argument(
        "--s12-input",
        type=Path,
        help="S12-25 XML、ZIP、または S12-25_GML ディレクトリ",
    )
    parser.add_argument(
        "--show-null-details",
        action="store_true",
        help="NULL の全 station_group と根拠を表示します。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    n02_input = locate_input(args.input)
    s12_input = locate_s12_input(args.s12_input)
    n02_curves, _, n02_stations = parse_n02(n02_input)
    s12_curves, s12_stations = parse_s12_2024(s12_input)
    with connect_database(args.database) as connection:
        try:
            summary = validate_station_group_passengers(
                connection,
                n02_curves,
                n02_stations,
                s12_curves,
                s12_stations,
            )
        except RuntimeError as error:
            print(f"[ERROR] {error}", file=sys.stderr)
            raise SystemExit(1) from error
    print_validation_summary(summary, show_null_details=args.show_null_details)


if __name__ == "__main__":
    main()
