"""Read-only connection, compatibility checks, and row conversion helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields
from pathlib import Path
from typing import TypeVar, cast

from .records import (
    DatabaseRecord,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    TABLE_RECORD_TYPES,
)


RecordT = TypeVar("RecordT", bound=DatabaseRecord)


class SchemaMismatchError(ValueError):
    """Raised when a database does not match this downstream schema."""


def column_names(record_type: type[DatabaseRecord]) -> tuple[str, ...]:
    """Return the persisted column names for a record type in DB order."""

    return tuple(field.name for field in fields(record_type))


def validate_database_schema(connection: sqlite3.Connection) -> None:
    """Check schema version, required tables, and exact ordered columns."""

    problems: list[str] = []
    actual_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if actual_version != SCHEMA_VERSION:
        problems.append(
            f"schema version is {actual_version}; expected {SCHEMA_VERSION}"
        )

    actual_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = sorted(REQUIRED_TABLES - actual_tables)
    if missing_tables:
        problems.append(f"missing tables: {', '.join(missing_tables)}")

    for table_name, record_type in TABLE_RECORD_TYPES.items():
        if table_name not in actual_tables:
            continue
        actual_columns = tuple(
            row[1]
            for row in connection.execute(f'PRAGMA table_info("{table_name}")')
        )
        expected_columns = column_names(record_type)
        if actual_columns != expected_columns:
            problems.append(
                f"{table_name} columns are {actual_columns!r}; "
                f"expected {expected_columns!r}"
            )

    if problems:
        raise SchemaMismatchError("; ".join(problems))


def connect_database(
    path: str | Path,
    *,
    validate: bool = True,
) -> sqlite3.Connection:
    """Open an existing railway DB read-only with named-row access enabled."""

    database_path = Path(path).expanduser().resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"database not found: {database_path}")

    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        if validate:
            validate_database_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def row_to_record(
    record_type: type[RecordT],
    row: sqlite3.Row | Mapping[str, object],
) -> RecordT:
    """Convert a named SQLite row or mapping into one schema record."""

    names = column_names(record_type)
    try:
        values = {name: row[name] for name in names}
    except (IndexError, KeyError) as error:
        raise SchemaMismatchError(
            f"row does not provide every column for {record_type.__name__}"
        ) from error
    # The concrete dataclass and its keyword names are both selected at runtime.
    # Express that dynamic constructor boundary without weakening RecordT for callers.
    constructor = cast(Callable[..., RecordT], record_type)
    return constructor(**values)


def iter_table_records(
    connection: sqlite3.Connection,
    table_name: str,
) -> Iterator[DatabaseRecord]:
    """Yield all rows of a declared table as typed records without buffering."""

    try:
        record_type = TABLE_RECORD_TYPES[table_name]
    except KeyError as error:
        raise ValueError(f"unknown schema table: {table_name}") from error

    names = column_names(record_type)
    quoted_columns = ", ".join(f'"{name}"' for name in names)
    query = f'SELECT {quoted_columns} FROM "{table_name}"'
    for row in connection.execute(query):
        yield row_to_record(record_type, row)
