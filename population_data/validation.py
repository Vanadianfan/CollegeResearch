"""Independent raw-to-SQLite validation for the population mesh database."""

from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path

from rail_data.paths import POPULATION_DB_PATH, POPULATION_RAW_ROOT

from .mesh import fifth_mesh_bounds
from .schema import connect_database
from .source import iter_population_rows


def validate_database_against_raw(database: Path, raw_root: Path) -> None:
    raw_count = 0
    raw_sum = 0
    raw_codes: set[str] = set()
    for row in iter_population_rows(raw_root):
        if row.mesh_code in raw_codes:
            raise RuntimeError(f"生データで KEY_CODE が重複しています: {row.mesh_code}")
        raw_codes.add(row.mesh_code)
        raw_count += 1
        raw_sum += row.population

    connection = connect_database(database)
    try:
        db_count, db_sum = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(population), 0) FROM population_mesh"
        ).fetchone()
        if (db_count, db_sum) != (raw_count, raw_sum):
            raise RuntimeError(
                "人口件数または合計が生データと一致しません: "
                f"raw=({raw_count}, {raw_sum}), db=({db_count}, {db_sum})"
            )
        db_codes = {
            row[0] for row in connection.execute("SELECT mesh_code FROM population_mesh")
        }
        if db_codes != raw_codes:
            missing = sorted(raw_codes - db_codes)[:5]
            extra = sorted(db_codes - raw_codes)[:5]
            raise RuntimeError(
                f"KEY_CODE 集合が一致しません: missing={missing}, extra={extra}"
            )
        for row in connection.execute(
            """
            SELECT mesh_code, primary_mesh_code,
                   west_lon, south_lat, east_lon, north_lat
            FROM population_mesh
            """
        ):
            mesh_code = row[0]
            if row[1] != mesh_code[:4]:
                raise RuntimeError(f"一次メッシュコード不一致: {mesh_code}")
            expected = fifth_mesh_bounds(mesh_code)
            actual = row[2:]
            wanted = (
                expected.west_lon,
                expected.south_lat,
                expected.east_lon,
                expected.north_lat,
            )
            if any(
                not math.isclose(value, target, abs_tol=1e-12)
                for value, target in zip(actual, wanted)
            ):
                raise RuntimeError(f"メッシュ境界が不正です: {mesh_code}")
    finally:
        connection.close()

    print("[OK] 2020年250m人口メッシュ 逆引き検証")
    print(f"  raw/DB rows: {raw_count:,}")
    print(f"  population total: {raw_sum:,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="population_mesh.sqlite を e-Stat ZIP から逆引き検証します。"
    )
    parser.add_argument("database", nargs="?", type=Path, default=POPULATION_DB_PATH)
    parser.add_argument("--input", type=Path, default=POPULATION_RAW_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_database_against_raw(args.database, args.input)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
