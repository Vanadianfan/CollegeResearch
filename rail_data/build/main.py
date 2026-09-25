"""Command-line orchestration for an atomic full or line-subset build."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from rail_data.paths import PROJECT_ROOT

from .builder import build_database_model
from .corrections import load_corrections
from .geometry import DEFAULT_POINT_ON_SEGMENT_TOLERANCE
from .models import (
    AppliedCorrectionResult,
    AppliedSplitMergeCorrection,
    Correction,
    ImportIssue,
    PassengerAggregationSummary,
    ParallelDirectionAssignment,
    ParallelDirectionSkip,
    SplitMergeCorrection,
)
from .passenger_validation import (
    print_validation_summary,
    validate_station_group_passengers,
)
from .passengers import apply_group_passengers, locate_s12_input, parse_s12_2024
from .persistence import create_schema, validate_database, write_model
from .source import locate_input, parse_n02


DEFAULT_CORRECTIONS = Path(__file__).with_name("correction.txt")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "N02-24 GML と S12-25 の2024年乗降客数を "
            "路線別の鉄道トポロジ SQLite に変換します。"
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="N02-24 XML、ZIP、または N02-24_GML ディレクトリ",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "rail_network.sqlite",
        help="出力 SQLite パス（既定: rail_network.sqlite）",
    )
    parser.add_argument(
        "--s12-input",
        type=Path,
        help="S12-25 XML、ZIP、または S12-25_GML ディレクトリ",
    )
    parser.add_argument(
        "--line-name",
        action="append",
        help="完全一致する路線名のみ処理。複数回指定可。",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--point-tolerance",
        type=float,
        default=DEFAULT_POINT_ON_SEGMENT_TOLERANCE,
        help="端点が線分内部にあるか判定する経緯度許容差",
    )
    parser.add_argument(
        "--corrections",
        type=Path,
        default=DEFAULT_CORRECTIONS,
        help="原始トポロジ処理後に適用する修正ファイル（既定: build/correction.txt）",
    )
    return parser.parse_args()

def print_summary(
    connection: sqlite3.Connection,
    output_path: Path,
    issues: list[ImportIssue],
    applied_corrections: list[AppliedCorrectionResult],
    skipped_corrections: list[Correction],
    parallel_direction_assignments: list[ParallelDirectionAssignment],
    parallel_direction_skips: list[ParallelDirectionSkip],
    passenger_summary: PassengerAggregationSummary,
) -> None:
    tables = [
        "rail_line",
        "rail_line_component",
        "station_group",
        "station",
        "station_component",
        "station_anchor",
        "network_node",
        "atomic_segment",
        "graph_edge",
        "graph_edge_has_atomic_segment",
        "station_connection",
        "station_connection_has_graph_edge",
    ]
    print(f"SQLite 作成完了: {output_path}")
    for table in tables:
        count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count:,}")
    print(
        "  S12-25 passengers "
        f"({passenger_summary.source_record_count:,} source rows, 2024, "
        "station_group.passengers):"
    )
    print(
        f"    available: {passenger_summary.available_group_count:,} / "
        f"{passenger_summary.selected_group_count:,} groups"
    )
    print(
        "    NULL reasons: "
        f"incomplete_primary={passenger_summary.incomplete_group_count:,}, "
        f"primary_not_found={passenger_summary.missing_primary_group_count:,}, "
        f"no_station={passenger_summary.no_station_group_count:,}, "
        f"no_source_record={passenger_summary.no_source_record_group_count:,}"
    )
    print(
        "    S12 mapping: "
        f"stationCode={passenger_summary.mapped_by_station_code:,}, "
        f"geometry={passenger_summary.mapped_by_geometry:,}, "
        f"groupCode={passenger_summary.mapped_by_group_code:,}, "
        f"unmatched_active={passenger_summary.unmatched_active_record_count:,}"
    )
    print(
        "    excluded source rows: "
        f"duplicate_elsewhere={passenger_summary.ignored_duplicate_record_count:,}, "
        f"no_station={passenger_summary.ignored_no_station_record_count:,}"
    )
    print(f"  corrections_applied: {len(applied_corrections):,}")
    for correction in applied_corrections:
        if isinstance(correction, AppliedSplitMergeCorrection):
            source_pairs = " ".join(
                f"{first}+{second}"
                for first, second in correction.source_edge_pairs
            )
            merged = ", ".join(
                f"graph_edge#{edge_id} ({distance_m:.3f}m)"
                for edge_id, distance_m in zip(
                    correction.merged_edge_ids, correction.distance_ms
                )
            )
            print(
                f"    line {correction.line_no}: SM "
                f"node={correction.junction_node_id} pairs={source_pairs} -> "
                f"{merged}, split_node#{correction.split_node_id}"
            )
        else:
            source_path = " ".join(
                f"{edge_id}{'+' if forward else '-'}"
                for edge_id, forward in correction.source_edge_refs
            )
            print(
                f"    line {correction.line_no}: UM "
                f"node={correction.junction_node_id} path={source_path} -> "
                f"graph_edge#{correction.merged_edge_id}, "
                f"split_node#{correction.split_node_id}, "
                f"distance={correction.distance_m:.3f}m"
            )
    if skipped_corrections:
        print(f"  corrections_skipped: {len(skipped_corrections):,}")
        for correction in skipped_corrections:
            command = "SM" if isinstance(correction, SplitMergeCorrection) else "UM"
            print(
                f"    line {correction.line_no}: {command} "
                f"node={correction.junction_node_id} "
                "(--line-name build では完全版DBのIDを解決できないため)"
            )
    print(
        "  left_running_parallel_corridors: "
        f"{len(parallel_direction_assignments):,} applied / "
        f"{len(parallel_direction_skips):,} skipped"
    )
    if parallel_direction_assignments:
        method_counts = Counter(
            assignment.method for assignment in parallel_direction_assignments
        )
        print(
            "    methods: "
            + ", ".join(
                f"{method}={count:,}" for method, count in method_counts.items()
            )
        )
    for skipped in parallel_direction_skips[:10]:
        print(
            f"    SKIP component#{skipped.line_component_id} "
            f"node#{skipped.node_a_id}<->node#{skipped.node_b_id} "
            f"edges={skipped.edge_ids}: {skipped.reason}"
        )
    if len(parallel_direction_skips) > 10:
        print(f"    ... 他 {len(parallel_direction_skips) - 10:,} 件")
    print(f"  build_warnings (SQLiteには保存しません): {len(issues):,}")
    for issue_code, count in Counter(
        issue.issue_code for issue in issues
    ).most_common():
        print(f"    {issue_code}: {count:,}")
    for index, issue in enumerate(issues, start=1):
        entity = ""
        if issue.entity_table is not None:
            entity = f" entity={issue.entity_table}"
            if issue.entity_id is not None:
                entity += f"#{issue.entity_id}"
        print(f"    [{index}] {issue.severity.upper()} {issue.issue_code}{entity}")
        print(f"        {issue.message}")
        if issue.details:
            details = ", ".join(
                f"{key}={value}" for key, value in issue.details.items()
            )
            print(f"        {details}")


def main() -> None:
    args = parse_args()
    input_path = locate_input(args.input)
    s12_input_path = locate_s12_input(args.s12_input)
    corrections = load_corrections(args.corrections)
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        print(f"既存 SQLite を置き換えます: {output_path}")

    print(f"N02 読込: {input_path}")
    print(
        f"修正読込: {args.corrections.expanduser().resolve()} "
        f"({len(corrections):,} 件)"
    )
    curves, sections, stations = parse_n02(input_path)
    print(
        f"解析: {len(curves):,} curves / {len(sections):,} sections / "
        f"{len(stations):,} station features"
    )
    print(f"S12 読込: {s12_input_path}")
    s12_curves, s12_stations = parse_s12_2024(s12_input_path)
    print(
        f"S12 解析: {len(s12_curves):,} curves / "
        f"{len(s12_stations):,} passenger features (2024)"
    )
    selected_line_names = None if not args.line_name else set(args.line_name)
    model = build_database_model(
        curves,
        sections,
        stations,
        selected_line_names,
        args.point_tolerance,
        corrections,
    )
    passenger_summary = apply_group_passengers(
        model,
        curves,
        stations,
        s12_curves,
        s12_stations,
    )

    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(temp_fd)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(temp_path) as connection:
            create_schema(connection)
            with connection:
                write_model(connection, model)
                validate_database(connection)
            passenger_validation_summary = validate_station_group_passengers(
                connection,
                curves,
                stations,
                s12_curves,
                s12_stations,
            )
            issues: list[ImportIssue] = model["issues"]  # type: ignore[assignment]
            applied_corrections: list[AppliedCorrectionResult] = model[
                "applied_corrections"
            ]  # type: ignore[assignment]
            skipped_corrections: list[Correction] = model[
                "skipped_corrections"
            ]  # type: ignore[assignment]
            parallel_direction_assignments: list[ParallelDirectionAssignment] = model[
                "parallel_direction_assignments"
            ]  # type: ignore[assignment]
            parallel_direction_skips: list[ParallelDirectionSkip] = model[
                "parallel_direction_skips"
            ]  # type: ignore[assignment]
            print_summary(
                connection,
                output_path,
                issues,
                applied_corrections,
                skipped_corrections,
                parallel_direction_assignments,
                parallel_direction_skips,
                passenger_summary,
            )
            print_validation_summary(passenger_validation_summary)
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


if __name__ == "__main__":
    main()
