"""Import S12-25 passenger observations into N02-24 station groups.

The persisted value is deliberately fail-closed.  It is the sum of distinct
2024 primary S12 observations in an N02 station group, or ``None`` when S12
does not provide every primary observation required for a complete group sum.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from rail_data.paths import S12_GML_ROOT, S12_XML_PATH, S12_ZIP_PATH

from .geometry import curve_signature
from .models import (
    Coord,
    ImportIssue,
    PassengerAggregationSummary,
    RawPassengerStation,
    RawStation,
    RouteKey,
    StationGroupRow,
)
from .source import (
    GML_ID,
    GML_NS,
    child_refs,
    child_text,
    local_name,
    parse_pos_list,
)


S12_FEATURE_NAME = "TheNumberofTheStationPassengersGettingonandoff"
VALID_DUPLICATE_CODES = {"1", "2", "3"}
VALID_DATA_STATUS_CODES = {"1", "2", "3", "4"}

# MLIT S12 semantics used below:
# duplicate: 1=primary observation, 2=value recorded at another station row,
#            3=station does not exist in that year.
# data status: 1=available, 2=not available, 3=not published, 4=no station.


def locate_s12_input(explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        path = explicit_path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"S12 入力が見つかりません: {path}")
        return path

    if S12_XML_PATH.is_file():
        return S12_XML_PATH
    if S12_GML_ROOT.is_dir():
        return S12_GML_ROOT
    if S12_ZIP_PATH.is_file():
        return S12_ZIP_PATH
    raise FileNotFoundError(
        f"{S12_XML_PATH} または {S12_ZIP_PATH} を検出できません。"
        "python3 setup.py を実行するか、--s12-input で指定してください。"
    )


@contextmanager
def open_s12_xml(path: Path) -> Iterator[BinaryIO]:
    if path.is_dir():
        xml_paths = sorted(path.glob("UTF-8/S12-*.xml")) or sorted(
            path.rglob("S12-*.xml")
        )
        if not xml_paths:
            raise FileNotFoundError(f"{path} に S12 XML がありません。")
        with xml_paths[-1].open("rb") as stream:
            yield stream
        return

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if "UTF-8" in PurePosixPath(name).parts
                and PurePosixPath(name).name.startswith("S12-")
                and name.endswith(".xml")
            )
            if not names:
                raise FileNotFoundError(f"{path} に UTF-8 の S12 XML がありません。")
            with archive.open(names[-1]) as stream:
                yield stream
        return

    with path.open("rb") as stream:
        yield stream


def _optional_nonnegative_int(text: str, source_id: str) -> int | None:
    if not text:
        return None
    try:
        value = int(text)
    except ValueError as error:
        raise ValueError(
            f"S12 {source_id}: passengers2024 が整数ではありません: {text!r}"
        ) from error
    if value < 0:
        raise ValueError(
            f"S12 {source_id}: passengers2024 が負数です: {value}"
        )
    return value


def parse_s12_2024(
    input_path: Path,
) -> tuple[dict[str, list[list[Coord]]], list[RawPassengerStation]]:
    curves: dict[str, list[list[Coord]]] = {}
    stations: list[RawPassengerStation] = []

    with open_s12_xml(input_path) as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            name = local_name(element.tag)
            if name == "Curve":
                curve_id = element.get(GML_ID, "")
                parts = [
                    parse_pos_list(pos_list.text)
                    for pos_list in element.findall(f".//{{{GML_NS}}}posList")
                ]
                curves[curve_id] = [part for part in parts if len(part) >= 2]
                element.clear()
            elif name == S12_FEATURE_NAME:
                station_refs = child_refs(element, "station")
                source_id = element.get(GML_ID, "")
                stations.append(
                    RawPassengerStation(
                        source_id=source_id,
                        curve_id=station_refs[0] if station_refs else "",
                        route_key=RouteKey(
                            child_text(element, "railroadDivision"),
                            child_text(element, "railroadCompanyClassification"),
                            child_text(element, "routeName"),
                            child_text(element, "administrationCompany"),
                        ),
                        name=child_text(element, "stationName"),
                        station_code=child_text(element, "stationCode"),
                        group_code=child_text(element, "groupCode"),
                        duplicate_code=child_text(element, "duplicate2024"),
                        data_status_code=child_text(element, "dataEorN2024"),
                        passengers=_optional_nonnegative_int(
                            child_text(element, "passengers2024"), source_id
                        ),
                    )
                )
                element.clear()

    if not curves or not stations:
        raise ValueError(
            f"S12 解析結果が不正です: curves={len(curves)}, "
            f"station passenger features={len(stations)}"
        )
    return curves, stations


def _unique_group(
    candidates: set[str] | None,
    *,
    source_id: str,
    key_name: str,
    key_value: str,
) -> str | None:
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError(
            f"S12 {source_id}: {key_name}={key_value!r} が複数の "
            f"N02 station_group に対応します: {sorted(candidates)}"
        )
    return next(iter(candidates))


def _validate_2024_record(record: RawPassengerStation) -> None:
    if record.duplicate_code not in VALID_DUPLICATE_CODES:
        raise ValueError(
            f"S12 {record.source_id}: 未知の duplicate2024="
            f"{record.duplicate_code!r}"
        )
    if record.data_status_code not in VALID_DATA_STATUS_CODES:
        raise ValueError(
            f"S12 {record.source_id}: 未知の dataEorN2024="
            f"{record.data_status_code!r}"
        )
    if record.duplicate_code == "3" and record.data_status_code != "4":
        raise ValueError(
            f"S12 {record.source_id}: duplicate2024=3 ですが "
            f"dataEorN2024={record.data_status_code!r} です"
        )
    if record.data_status_code == "4" and record.duplicate_code != "3":
        raise ValueError(
            f"S12 {record.source_id}: dataEorN2024=4 ですが "
            f"duplicate2024={record.duplicate_code!r} です"
        )
    if record.duplicate_code != "3" and not record.station_code:
        raise ValueError(
            f"S12 {record.source_id}: 2024年の対象記録に stationCode がありません"
        )
    if (
        record.duplicate_code == "1"
        and record.data_status_code == "1"
        and record.passengers is None
    ):
        raise ValueError(
            f"S12 {record.source_id}: dataEorN2024=1 の主記録に "
            "passengers2024 がありません"
        )


def apply_group_passengers(
    model: dict[str, object],
    n02_curves: dict[str, list[list[Coord]]],
    n02_stations: list[RawStation],
    s12_curves: dict[str, list[list[Coord]]],
    s12_stations: list[RawPassengerStation],
) -> PassengerAggregationSummary:
    """Attach conservative 2024 S12 totals to the model's group rows."""

    group_rows: list[StationGroupRow] = model["group_rows"]  # type: ignore[assignment]
    issues: list[ImportIssue] = model["issues"]  # type: ignore[assignment]
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
    mapped_counts = {"station_code": 0, "geometry": 0, "group_code": 0}
    unmatched_active_count = 0
    ignored_duplicate_count = 0
    ignored_no_station_count = 0
    unmatched_no_station_count = 0
    source_ids: set[str] = set()

    for record in s12_stations:
        if not record.source_id or record.source_id in source_ids:
            raise ValueError(
                f"S12 source_id が空または重複しています: {record.source_id!r}"
            )
        source_ids.add(record.source_id)
        _validate_2024_record(record)
        if record.duplicate_code == "2":
            ignored_duplicate_count += 1
        if record.duplicate_code == "3" and record.data_status_code == "4":
            ignored_no_station_count += 1
        by_code = _unique_group(
            groups_by_station_code.get(record.station_code),
            source_id=record.source_id,
            key_name="stationCode",
            key_value=record.station_code,
        )
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
            raise ValueError(
                f"S12 {record.source_id}: N02 対応候補が矛盾します: "
                f"stationCode={by_code}, geometry={by_geometry}, "
                f"groupCode={by_group_code}"
            )

        if by_code is not None:
            target_group = by_code
            method = "station_code"
        elif by_geometry is not None:
            target_group = by_geometry
            method = "geometry"
        elif by_group_code is not None:
            target_group = by_group_code
            method = "group_code"
        else:
            if record.duplicate_code == "3" and record.data_status_code == "4":
                unmatched_no_station_count += 1
                continue
            unmatched_active_count += 1
            issues.append(
                ImportIssue(
                    stage="S12_2024",
                    severity="warning",
                    entity_table="s12_station",
                    entity_id=None,
                    issue_code="S12_STATION_NOT_IN_N02",
                    message=(
                        "S12-25 の2024年対象駅を N02-24 の駅グループに対応できません: "
                        f"{record.name}"
                    ),
                    details={
                        "source_id": record.source_id,
                        "station_code": record.station_code or "-",
                        "group_code": record.group_code or "-",
                        "line_name": record.route_key.name,
                        "operator_name": record.route_key.operator_name,
                        "duplicate_code": record.duplicate_code,
                        "data_status_code": record.data_status_code,
                        "passengers": record.passengers,
                    },
                )
            )
            continue

        records_by_group[target_group].append(record)
        mapped_counts[method] += 1
        if record.duplicate_code == "1" and method != "station_code":
            issues.append(
                ImportIssue(
                    stage="S12_2024",
                    severity="warning",
                    entity_table="station_group",
                    entity_id=None,
                    issue_code=f"S12_PRIMARY_MATCHED_BY_{method.upper()}",
                    message=(
                        "S12-25 の主記録を stationCode ではなく "
                        f"{method} で N02-24 に対応しました: {record.name}"
                    ),
                    details={
                        "source_id": record.source_id,
                        "station_code": record.station_code or "-",
                        "s12_group_code": record.group_code or "-",
                        "n02_group_code": target_group,
                        "line_name": record.route_key.name,
                        "operator_name": record.route_key.operator_name,
                        "passengers": record.passengers,
                    },
                )
            )

    classified_source_count = (
        sum(mapped_counts.values())
        + unmatched_active_count
        + unmatched_no_station_count
    )
    if classified_source_count != len(s12_stations):
        raise AssertionError("S12 source record classification count is inconsistent")

    available_count = 0
    incomplete_count = 0
    missing_primary_count = 0
    no_station_group_count = 0
    no_source_record_count = 0

    for row in group_rows:
        records = records_by_group.get(row.group_code, [])
        primary_by_station_code: dict[str, RawPassengerStation] = {}
        has_duplicate_reference = False
        has_no_station_record = False

        for record in records:
            if record.duplicate_code == "2":
                has_duplicate_reference = True
                continue
            if record.duplicate_code == "3":
                has_no_station_record = True
                continue
            if record.station_code in primary_by_station_code:
                other = primary_by_station_code[record.station_code]
                raise ValueError(
                    f"S12 {record.source_id}: groupCode={row.group_code} 内で "
                    f"stationCode={record.station_code} の主記録が重複します "
                    f"({other.source_id}, {record.source_id})"
                )
            primary_by_station_code[record.station_code] = record

        primary = list(primary_by_station_code.values())
        if primary:
            incomplete = any(record.data_status_code != "1" for record in primary)
            if incomplete:
                row.passengers = None
                incomplete_count += 1
                continue
            row.passengers = sum(
                record.passengers for record in primary if record.passengers is not None
            )
            available_count += 1
        elif has_duplicate_reference:
            row.passengers = None
            missing_primary_count += 1
            issues.append(
                ImportIssue(
                    stage="S12_2024",
                    severity="warning",
                    entity_table="station_group",
                    entity_id=row.id,
                    issue_code="S12_PRIMARY_RECORD_NOT_FOUND",
                    message=(
                        "duplicate2024=2 はありますが、加算元となる主記録を "
                        f"同じ N02 駅グループで確認できません: {row.display_name}"
                    ),
                    details={
                        "group_code": row.group_code,
                        "display_name": row.display_name,
                    },
                )
            )
        elif has_no_station_record:
            row.passengers = None
            no_station_group_count += 1
        else:
            row.passengers = None
            no_source_record_count += 1

    classified_group_count = (
        available_count
        + incomplete_count
        + missing_primary_count
        + no_station_group_count
        + no_source_record_count
    )
    if classified_group_count != len(group_rows):
        raise AssertionError("S12 station_group classification count is inconsistent")

    return PassengerAggregationSummary(
        source_record_count=len(s12_stations),
        selected_group_count=len(group_rows),
        available_group_count=available_count,
        incomplete_group_count=incomplete_count,
        missing_primary_group_count=missing_primary_count,
        no_station_group_count=no_station_group_count,
        no_source_record_group_count=no_source_record_count,
        mapped_by_station_code=mapped_counts["station_code"],
        mapped_by_geometry=mapped_counts["geometry"],
        mapped_by_group_code=mapped_counts["group_code"],
        unmatched_active_record_count=unmatched_active_count,
        ignored_duplicate_record_count=ignored_duplicate_count,
        ignored_no_station_record_count=ignored_no_station_count,
    )
