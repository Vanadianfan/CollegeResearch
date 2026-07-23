"""Read-only downstream contract for population_mesh.sqlite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path


POPULATION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PopulationMeshRecord:
    mesh_code: str
    primary_mesh_code: str
    population: int
    disclosure_status: int
    aggregation_target_mesh_code: str | None
    aggregated_mesh_codes: str | None
    west_lon: float
    south_lat: float
    east_lon: float
    north_lat: float


POPULATION_MESH_COLUMNS = tuple(
    field.name for field in fields(PopulationMeshRecord)
)


class PopulationSchemaMismatchError(ValueError):
    """Raised when a population database does not match this contract."""


def validate_database_schema(connection: sqlite3.Connection) -> None:
    problems: list[str] = []
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != POPULATION_SCHEMA_VERSION:
        problems.append(
            f"schema version is {version}; expected {POPULATION_SCHEMA_VERSION}"
        )
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='population_mesh'"
    ).fetchone()
    if table is None:
        problems.append("missing table: population_mesh")
    else:
        actual_columns = tuple(
            row[1]
            for row in connection.execute("PRAGMA table_info(population_mesh)")
        )
        if actual_columns != POPULATION_MESH_COLUMNS:
            problems.append(
                f"population_mesh columns are {actual_columns!r}; "
                f"expected {POPULATION_MESH_COLUMNS!r}"
            )
    if problems:
        raise PopulationSchemaMismatchError("; ".join(problems))


def connect_database(
    path: str | Path,
    *,
    validate: bool = True,
) -> sqlite3.Connection:
    database_path = Path(path).expanduser().resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"population database not found: {database_path}")
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if validate:
            validate_database_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection
