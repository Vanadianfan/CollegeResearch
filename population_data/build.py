"""Build a compact SQLite database from e-Stat 2020 250 m population meshes."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path

from rail_data.paths import POPULATION_DB_PATH, POPULATION_RAW_ROOT

from .mesh import fifth_mesh_bounds
from .schema import POPULATION_SCHEMA_VERSION
from .source import POPULATION_FIELD, STATS_ID, iter_population_rows, population_archives
from .validation import validate_database_against_raw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="e-Stat 2020年250m人口メッシュを SQLite に整理します。"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=POPULATION_RAW_ROOT,
        help="tblT001142Q*.zip を置いたディレクトリ",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=POPULATION_DB_PATH,
        help="出力 SQLite（成功時に既存ファイルを原子的に置換）",
    )
    return parser.parse_args()


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        PRAGMA foreign_keys = ON;
        PRAGMA user_version = {POPULATION_SCHEMA_VERSION};

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE population_mesh (
            mesh_code TEXT PRIMARY KEY CHECK(length(mesh_code) = 10),
            primary_mesh_code TEXT NOT NULL CHECK(length(primary_mesh_code) = 4),
            population INTEGER NOT NULL CHECK(population >= 0),
            disclosure_status INTEGER NOT NULL
                CHECK(disclosure_status IN (0, 1, 2)),
            aggregation_target_mesh_code TEXT,
            aggregated_mesh_codes TEXT,
            west_lon REAL NOT NULL,
            south_lat REAL NOT NULL,
            east_lon REAL NOT NULL,
            north_lat REAL NOT NULL,
            CHECK(west_lon < east_lon),
            CHECK(south_lat < north_lat)
        );

        CREATE INDEX idx_population_mesh_primary
            ON population_mesh(primary_mesh_code);
        CREATE INDEX idx_population_mesh_population
            ON population_mesh(population);
        """
    )


def populate(connection: sqlite3.Connection, raw_root: Path) -> tuple[int, int]:
    insert = """
        INSERT INTO population_mesh VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    count = 0
    population_sum = 0
    batch: list[tuple[object, ...]] = []
    for row in iter_population_rows(raw_root):
        bounds = fifth_mesh_bounds(row.mesh_code)
        batch.append(
            (
                row.mesh_code,
                row.mesh_code[:4],
                row.population,
                row.disclosure_status,
                row.aggregation_target_mesh_code,
                row.aggregated_mesh_codes,
                bounds.west_lon,
                bounds.south_lat,
                bounds.east_lon,
                bounds.north_lat,
            )
        )
        count += 1
        population_sum += row.population
        if len(batch) >= 10_000:
            connection.executemany(insert, batch)
            batch.clear()
    if batch:
        connection.executemany(insert, batch)

    archive_count = len(population_archives(raw_root))
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        [
            ("dataset", "令和2年国勢調査 250mメッシュ 人口及び世帯"),
            ("census_year", "2020"),
            ("datum", "JGD2011"),
            ("mesh_size_m", "250"),
            ("stats_id", STATS_ID),
            ("population_field", POPULATION_FIELD),
            ("source_archive_count", str(archive_count)),
            ("row_count", str(count)),
            ("population_sum", str(population_sum)),
        ],
    )
    return count, population_sum


def build_database(raw_root: Path, output_path: Path) -> None:
    input_path = raw_root.expanduser().resolve()
    output = output_path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    file_handle, temp_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".sqlite.tmp", dir=output.parent
    )
    os.close(file_handle)
    temp_path = Path(temp_name)
    try:
        connection = sqlite3.connect(temp_path)
        try:
            create_schema(connection)
            count, population_sum = populate(connection, input_path)
            connection.commit()
        finally:
            connection.close()

        validate_database_against_raw(temp_path, input_path)
        os.replace(temp_path, output)
        print(f"SQLite 作成完了: {output}")
        print(f"  population_mesh: {count:,}")
        print(f"  population total: {population_sum:,}")
        print(f"  source archives: {len(population_archives(input_path)):,}")
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        build_database(args.input, args.output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
