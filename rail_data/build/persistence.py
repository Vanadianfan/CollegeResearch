"""Create, populate, and validate the generated SQLite database."""

from __future__ import annotations

import sqlite3

from .models import (
    AnchorRow,
    ComponentDraft,
    ConnectionRow,
    GraphEdgeRow,
    LineComponentRow,
    NodeRow,
    RouteKey,
    SegmentRow,
    StationGroupRow,
    StationRow,
)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        -- The completed temporary file is atomically renamed to the requested
        -- output path.  Keep all pages in that one file; a WAL sidecar would
        -- otherwise be left behind under the temporary name.
        PRAGMA journal_mode = DELETE;
        PRAGMA synchronous = NORMAL;
        PRAGMA user_version = 8;

        CREATE TABLE rail_line (
            id INTEGER PRIMARY KEY,
            railway_type_code TEXT NOT NULL,
            provider_type_code TEXT NOT NULL,
            name TEXT NOT NULL,
            operator_name TEXT NOT NULL,
            UNIQUE(railway_type_code, provider_type_code, name, operator_name)
        );

        CREATE TABLE rail_line_component (
            id INTEGER PRIMARY KEY,
            line_id INTEGER NOT NULL REFERENCES rail_line(id),
            component_no INTEGER NOT NULL,
            node_count INTEGER NOT NULL,
            segment_count INTEGER NOT NULL,
            build_status TEXT NOT NULL,
            UNIQUE(line_id, component_no)
        );

        CREATE TABLE station_group (
            id INTEGER PRIMARY KEY,
            group_code TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            station_count INTEGER NOT NULL,
            passengers INTEGER CHECK(passengers IS NULL OR passengers >= 0)
        );

        CREATE TABLE station (
            id INTEGER PRIMARY KEY,
            source_id TEXT NOT NULL UNIQUE,
            station_code TEXT NOT NULL,
            group_id INTEGER NOT NULL REFERENCES station_group(id),
            line_id INTEGER NOT NULL REFERENCES rail_line(id),
            name TEXT NOT NULL,
            geometry_length_m REAL NOT NULL,
            geometry_status TEXT NOT NULL
        );

        CREATE TABLE station_component (
            id INTEGER PRIMARY KEY,
            station_id INTEGER NOT NULL REFERENCES station(id),
            component_no INTEGER NOT NULL,
            length_m REAL NOT NULL,
            geometry_class TEXT NOT NULL,
            midpoint_lon REAL,
            midpoint_lat REAL,
            anchor_status TEXT NOT NULL,
            UNIQUE(station_id, component_no)
        );

        CREATE TABLE network_node (
            id INTEGER PRIMARY KEY,
            line_id INTEGER NOT NULL REFERENCES rail_line(id),
            line_component_id INTEGER REFERENCES rail_line_component(id),
            lon REAL NOT NULL,
            lat REAL NOT NULL,
            topology_type TEXT NOT NULL,
            creation_method TEXT NOT NULL
        );

        CREATE TABLE atomic_segment (
            id INTEGER PRIMARY KEY,
            line_id INTEGER NOT NULL REFERENCES rail_line(id),
            line_component_id INTEGER REFERENCES rail_line_component(id),
            from_node_id INTEGER NOT NULL REFERENCES network_node(id),
            to_node_id INTEGER NOT NULL REFERENCES network_node(id),
            length_m REAL NOT NULL CHECK(length_m > 0),
            build_status TEXT NOT NULL,
            CHECK(from_node_id <> to_node_id),
            UNIQUE(line_id, from_node_id, to_node_id)
        );

        CREATE TABLE station_anchor (
            id INTEGER PRIMARY KEY,
            station_component_id INTEGER NOT NULL REFERENCES station_component(id),
            node_id INTEGER NOT NULL REFERENCES network_node(id),
            anchor_no INTEGER NOT NULL,
            method TEXT NOT NULL,
            position_m REAL,
            is_primary INTEGER NOT NULL CHECK(is_primary IN (0, 1)),
            status TEXT NOT NULL,
            UNIQUE(station_component_id, anchor_no)
        );

        CREATE TABLE graph_edge (
            id INTEGER PRIMARY KEY,
            line_component_id INTEGER NOT NULL REFERENCES rail_line_component(id),
            edge_kind TEXT NOT NULL CHECK(edge_kind IN ('rail', 'transfer')),
            from_node_id INTEGER NOT NULL REFERENCES network_node(id),
            to_node_id INTEGER NOT NULL REFERENCES network_node(id),
            direction TEXT NOT NULL CHECK(direction IN ('both', 'forward', 'backward')),
            distance_m REAL NOT NULL CHECK(distance_m >= 0),
            cost_s REAL,
            status TEXT NOT NULL,
            source_method TEXT NOT NULL
        );

        CREATE TABLE graph_edge_has_atomic_segment (
            id INTEGER PRIMARY KEY,
            graph_edge_id INTEGER NOT NULL REFERENCES graph_edge(id),
            atomic_segment_id INTEGER NOT NULL REFERENCES atomic_segment(id),
            sequence_no INTEGER NOT NULL,
            forward INTEGER NOT NULL CHECK(forward IN (0, 1)),
            UNIQUE(graph_edge_id, sequence_no)
        );

        CREATE TABLE station_connection (
            id INTEGER PRIMARY KEY,
            line_component_id INTEGER NOT NULL REFERENCES rail_line_component(id),
            from_anchor_id INTEGER NOT NULL REFERENCES station_anchor(id),
            to_anchor_id INTEGER NOT NULL REFERENCES station_anchor(id),
            direction TEXT NOT NULL CHECK(direction = 'forward'),
            from_station_offset_m REAL NOT NULL CHECK(from_station_offset_m >= 0),
            to_station_offset_m REAL NOT NULL CHECK(to_station_offset_m >= 0),
            gap_length_m REAL NOT NULL CHECK(gap_length_m >= 0),
            distance_m REAL NOT NULL CHECK(distance_m >= 0),
            path_status TEXT NOT NULL,
            CHECK(from_anchor_id <> to_anchor_id),
            UNIQUE(from_anchor_id, to_anchor_id)
        );

        CREATE TABLE station_connection_has_graph_edge (
            id INTEGER PRIMARY KEY,
            station_connection_id INTEGER NOT NULL REFERENCES station_connection(id),
            graph_edge_id INTEGER NOT NULL REFERENCES graph_edge(id),
            sequence_no INTEGER NOT NULL,
            forward INTEGER NOT NULL CHECK(forward IN (0, 1)),
            UNIQUE(station_connection_id, sequence_no)
        );

        CREATE INDEX idx_station_group ON station(group_id);
        CREATE INDEX idx_station_line ON station(line_id);
        CREATE INDEX idx_node_line_component ON network_node(line_component_id);
        CREATE INDEX idx_node_topology_type ON network_node(topology_type);
        CREATE INDEX idx_segment_line_component ON atomic_segment(line_component_id);
        CREATE INDEX idx_anchor_node ON station_anchor(node_id);
        CREATE INDEX idx_graph_edge_component ON graph_edge(line_component_id);
        CREATE INDEX idx_graph_edge_has_atomic_segment_atomic
            ON graph_edge_has_atomic_segment(atomic_segment_id);
        CREATE INDEX idx_connection_component ON station_connection(line_component_id);
        CREATE INDEX idx_station_connection_has_graph_edge_graph
            ON station_connection_has_graph_edge(graph_edge_id);
        """
    )

def write_model(connection: sqlite3.Connection, model: dict[str, object]) -> None:
    route_keys: list[RouteKey] = model["route_keys"]  # type: ignore[assignment]
    station_rows: list[StationRow] = model["station_rows"]  # type: ignore[assignment]
    components: list[ComponentDraft] = model["components"]  # type: ignore[assignment]
    nodes: list[NodeRow] = model["nodes"]  # type: ignore[assignment]
    segments: list[SegmentRow] = model["segments"]  # type: ignore[assignment]
    anchors: list[AnchorRow] = model["anchors"]  # type: ignore[assignment]
    line_components: list[LineComponentRow] = model["line_components"]  # type: ignore[assignment]
    graph_edges: list[GraphEdgeRow] = model["graph_edges"]  # type: ignore[assignment]
    connections: list[ConnectionRow] = model["connections"]  # type: ignore[assignment]
    group_rows: list[StationGroupRow] = model["group_rows"]  # type: ignore[assignment]
    connection.executemany(
        "INSERT INTO rail_line VALUES (?, ?, ?, ?, ?)",
        [
            (
                line_id,
                key.railway_type_code,
                key.provider_type_code,
                key.name,
                key.operator_name,
            )
            for line_id, key in enumerate(route_keys, start=1)
        ],
    )
    connection.executemany(
        """
        INSERT INTO station_group (
            id, group_code, display_name, station_count, passengers
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                row.id,
                row.group_code,
                row.display_name,
                row.station_count,
                row.passengers,
            )
            for row in group_rows
        ],
    )
    connection.executemany(
        """
        INSERT INTO station (
            id, source_id, station_code, group_id, line_id, name,
            geometry_length_m, geometry_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.id,
                row.source_id,
                row.station_code,
                row.group_id,
                row.line_id,
                row.name,
                row.geometry_length_m,
                row.geometry_status,
            )
            for row in station_rows
        ],
    )
    connection.executemany(
        "INSERT INTO station_component VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row.id,
                row.station_id,
                row.component_no,
                row.length_m,
                row.geometry_class,
                row.midpoint[0],
                row.midpoint[1],
                row.anchor_status,
            )
            for row in components
        ],
    )
    connection.executemany(
        "INSERT INTO rail_line_component VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                row.id,
                row.line_id,
                row.component_no,
                row.node_count,
                row.segment_count,
                row.build_status,
            )
            for row in line_components
        ],
    )
    connection.executemany(
        "INSERT INTO network_node VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row.id,
                row.line_id,
                row.line_component_id,
                row.lon,
                row.lat,
                row.topology_type,
                row.creation_method,
            )
            for row in nodes
        ],
    )
    connection.executemany(
        "INSERT INTO atomic_segment VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row.id,
                row.line_id,
                row.line_component_id,
                row.from_node_id,
                row.to_node_id,
                row.length_m,
                row.build_status,
            )
            for row in segments
        ],
    )
    connection.executemany(
        "INSERT INTO station_anchor VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row.id,
                row.station_component_id,
                row.node_id,
                row.anchor_no,
                row.method,
                row.position_m,
                row.is_primary,
                row.status,
            )
            for row in anchors
        ],
    )

    graph_edge_has_atomic_segment_rows = []
    next_graph_edge_has_atomic_segment_id = 1
    for edge in graph_edges:
        for sequence_no, (segment_id, forward) in enumerate(edge.segment_refs):
            graph_edge_has_atomic_segment_rows.append(
                (
                    next_graph_edge_has_atomic_segment_id,
                    edge.id,
                    segment_id,
                    sequence_no,
                    forward,
                )
            )
            next_graph_edge_has_atomic_segment_id += 1
    connection.executemany(
        "INSERT INTO graph_edge VALUES (?, ?, 'rail', ?, ?, ?, ?, NULL, "
        "'confirmed', 'N02_topology')",
        [
            (
                row.id,
                row.line_component_id,
                row.from_node_id,
                row.to_node_id,
                row.direction,
                row.distance_m,
            )
            for row in graph_edges
        ],
    )
    connection.executemany(
        "INSERT INTO graph_edge_has_atomic_segment VALUES (?, ?, ?, ?, ?)",
        graph_edge_has_atomic_segment_rows,
    )

    station_connection_has_graph_edge_rows = []
    next_station_connection_has_graph_edge_id = 1
    for row in connections:
        for sequence_no, (edge_id, forward) in enumerate(row.edge_refs):
            station_connection_has_graph_edge_rows.append(
                (
                    next_station_connection_has_graph_edge_id,
                    row.id,
                    edge_id,
                    sequence_no,
                    forward,
                )
            )
            next_station_connection_has_graph_edge_id += 1
    connection.executemany(
        "INSERT INTO station_connection VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'confirmed')",
        [
            (
                row.id,
                row.line_component_id,
                row.from_anchor_id,
                row.to_anchor_id,
                row.direction,
                row.from_station_offset_m,
                row.to_station_offset_m,
                row.gap_length_m,
                row.distance_m,
            )
            for row in connections
        ],
    )
    connection.executemany(
        "INSERT INTO station_connection_has_graph_edge VALUES (?, ?, ?, ?, ?)",
        station_connection_has_graph_edge_rows,
    )


def validate_database(connection: sqlite3.Connection) -> None:
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"外部キー検査に失敗しました: {foreign_key_errors[:5]}")

    station_source_errors = connection.execute(
        "SELECT id FROM station WHERE TRIM(source_id) = ''"
    ).fetchall()
    if station_source_errors:
        raise RuntimeError(
            f"station.source_id が空です: {station_source_errors[:5]}"
        )

    invalid_passenger_rows = connection.execute(
        "SELECT id, passengers FROM station_group WHERE passengers < 0"
    ).fetchall()
    if invalid_passenger_rows:
        raise RuntimeError(
            "station_group.passengers が負数です: "
            f"{invalid_passenger_rows[:5]}"
        )

    edge_errors = connection.execute(
        """
        SELECT ge.id, ge.distance_m, COALESCE(SUM(s.length_m), 0)
        FROM graph_edge AS ge
        LEFT JOIN graph_edge_has_atomic_segment AS ge_has_segment
            ON ge_has_segment.graph_edge_id = ge.id
        LEFT JOIN atomic_segment AS s
            ON s.id = ge_has_segment.atomic_segment_id
        WHERE ge.edge_kind = 'rail'
        GROUP BY ge.id
        HAVING ABS(ge.distance_m - COALESCE(SUM(s.length_m), 0)) > 0.001
        """
    ).fetchall()
    if edge_errors:
        raise RuntimeError(f"graph_edge 距離検査に失敗しました: {edge_errors[:5]}")

    connection_errors = connection.execute(
        """
        SELECT id, distance_m,
               from_station_offset_m + gap_length_m + to_station_offset_m
        FROM station_connection
        WHERE ABS(
            distance_m -
            (from_station_offset_m + gap_length_m + to_station_offset_m)
        ) > 0.001
        """
    ).fetchall()
    if connection_errors:
        raise RuntimeError(
            f"station_connection 距離検査に失敗しました: {connection_errors[:5]}"
        )

    connection_path_errors: list[int] = []
    connection_rows = connection.execute(
        """
        SELECT c.id, from_anchor.node_id, to_anchor.node_id
        FROM station_connection AS c
        JOIN station_anchor AS from_anchor ON from_anchor.id = c.from_anchor_id
        JOIN station_anchor AS to_anchor ON to_anchor.id = c.to_anchor_id
        """
    )
    for connection_id, expected_start, expected_end in connection_rows:
        edge_rows = connection.execute(
            """
            SELECT relation.sequence_no, relation.forward,
                   ge.from_node_id, ge.to_node_id
            FROM station_connection_has_graph_edge AS relation
            JOIN graph_edge AS ge ON ge.id = relation.graph_edge_id
            WHERE relation.station_connection_id = ?
            ORDER BY relation.sequence_no
            """,
            (connection_id,),
        ).fetchall()
        current = expected_start
        valid = bool(edge_rows)
        for expected_sequence, (sequence_no, forward, from_node, to_node) in enumerate(
            edge_rows
        ):
            start, end = (from_node, to_node) if forward else (to_node, from_node)
            if sequence_no != expected_sequence or start != current:
                valid = False
                break
            current = end
        if not valid or current != expected_end:
            connection_path_errors.append(connection_id)
    if connection_path_errors:
        raise RuntimeError(
            "station_connection_has_graph_edge 連続性検査に失敗しました: "
            f"{connection_path_errors[:5]}"
        )

    forbidden_direction_errors = connection.execute(
        """
        SELECT c.id, relation.sequence_no, ge.id, ge.direction, relation.forward
        FROM station_connection AS c
        JOIN station_connection_has_graph_edge AS relation
            ON relation.station_connection_id = c.id
        JOIN graph_edge AS ge ON ge.id = relation.graph_edge_id
        WHERE (ge.direction = 'forward' AND relation.forward = 0)
           OR (ge.direction = 'backward' AND relation.forward = 1)
        """
    ).fetchall()
    if forbidden_direction_errors:
        raise RuntimeError(
            "station_connection が graph_edge の単向制約に違反しています: "
            f"{forbidden_direction_errors[:5]}"
        )

    non_forward_connections = connection.execute(
        "SELECT id, direction FROM station_connection WHERE direction <> 'forward'"
    ).fetchall()
    if non_forward_connections:
        raise RuntimeError(
            "station_connection は from->to の有向レコードで保存してください: "
            f"{non_forward_connections[:5]}"
        )

    missing_reverse_connections = connection.execute(
        """
        SELECT c.id, c.from_anchor_id, c.to_anchor_id
        FROM station_connection AS c
        WHERE NOT EXISTS (
            SELECT 1
            FROM station_connection AS reverse
            WHERE reverse.from_anchor_id = c.to_anchor_id
              AND reverse.to_anchor_id = c.from_anchor_id
        )
        """
    ).fetchall()
    if missing_reverse_connections:
        raise RuntimeError(
            "station_connection の逆方向レコードがありません: "
            f"{missing_reverse_connections[:5]}"
        )

    internal_station_errors = connection.execute(
        """
        WITH reached_nodes AS (
            SELECT
                c.id AS connection_id,
                relation.sequence_no,
                MAX(relation.sequence_no) OVER (
                    PARTITION BY relation.station_connection_id
                ) AS final_sequence_no,
                CASE
                    WHEN relation.forward = 1 THEN ge.to_node_id
                    ELSE ge.from_node_id
                END AS reached_node_id,
                c.from_anchor_id,
                c.to_anchor_id
            FROM station_connection AS c
            JOIN station_connection_has_graph_edge AS relation
                ON relation.station_connection_id = c.id
            JOIN graph_edge AS ge ON ge.id = relation.graph_edge_id
        )
        SELECT reached.connection_id, reached.reached_node_id, anchor.id
        FROM reached_nodes AS reached
        JOIN station_anchor AS anchor ON anchor.node_id = reached.reached_node_id
        WHERE reached.sequence_no < reached.final_sequence_no
          AND anchor.id NOT IN (reached.from_anchor_id, reached.to_anchor_id)
        """
    ).fetchall()
    if internal_station_errors:
        raise RuntimeError(
            "station_connection が途中の駅を通過しています: "
            f"{internal_station_errors[:5]}"
        )
