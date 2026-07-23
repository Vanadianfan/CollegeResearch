from __future__ import annotations

import argparse
import json
import sqlite3
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rail_data.paths import PROJECT_ROOT
from rail_data.schema import REQUIRED_TABLES, SCHEMA_VERSION


@dataclass
class CheckResult:
    level: str
    message: str
    details: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="檢查 rail_network.sqlite，並輸出可縮放的鐵道路網 HTML。"
    )
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        default=PROJECT_ROOT / "rail_network.sqlite",
        help="rail_data.build.main 產生的 SQLite",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="HTML 輸出路徑（既定: <database>_visualizer.html）",
    )
    parser.add_argument(
        "--line-id",
        action="append",
        type=int,
        help="只輸出指定 rail_line.id；可重複指定",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只驗證 SQLite，不輸出 HTML",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="把警告也視為失敗",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="輸出後用預設瀏覽器開啟",
    )
    return parser.parse_args()


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }


def validate_database(connection: sqlite3.Connection) -> list[CheckResult]:
    results: list[CheckResult] = []
    schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if schema_version != SCHEMA_VERSION:
        return [
            CheckResult(
                "ERROR",
                f"DB schema version={schema_version}；最新版は {SCHEMA_VERSION} です。"
                " rail_data.build.main で再構築してください。",
            )
        ]
    missing = sorted(REQUIRED_TABLES - table_names(connection))
    if missing:
        return [CheckResult("ERROR", f"缺少必要資料表: {', '.join(missing)}")]
    station_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(station)")
    }
    if "source_id" not in station_columns:
        return [
            CheckResult(
                "ERROR",
                "station 缺少 source_id；請用最新版 rail_data.build.main 重建 DB",
            )
        ]
    group_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(station_group)")
    }
    if "passengers" not in group_columns:
        return [
            CheckResult(
                "ERROR",
                "station_group に passengers がありません。最新版の build で再構築してください。",
            )
        ]

    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        results.append(
            CheckResult(
                "ERROR", f"外部鍵錯誤 {len(foreign_keys)} 筆；首筆: {foreign_keys[0]}"
            )
        )
    else:
        results.append(CheckResult("OK", "外部鍵完整"))

    blank_source_ids = connection.execute(
        "SELECT COUNT(*) FROM station WHERE TRIM(source_id) = ''"
    ).fetchone()[0]
    results.append(
        CheckResult(
            "ERROR" if blank_source_ids else "OK",
            f"station.source_id 空值 {blank_source_ids} 筆"
            if blank_source_ids
            else "station 原始 source 編號齊全",
        )
    )

    invalid_coords = connection.execute(
        """
        SELECT COUNT(*) FROM network_node
        WHERE lon NOT BETWEEN -180.0 AND 180.0
           OR lat NOT BETWEEN -90.0 AND 90.0
        """
    ).fetchone()[0]
    results.append(
        CheckResult(
            "ERROR" if invalid_coords else "OK",
            f"座標範圍異常 {invalid_coords} 筆"
            if invalid_coords
            else "節點座標範圍正常",
        )
    )

    edge_distance_errors = connection.execute(
        """
        SELECT COUNT(*)
        FROM (
            SELECT ge.id
            FROM graph_edge AS ge
            LEFT JOIN graph_edge_has_atomic_segment AS relation
                ON relation.graph_edge_id = ge.id
            LEFT JOIN atomic_segment AS s
                ON s.id = relation.atomic_segment_id
            WHERE ge.edge_kind = 'rail'
            GROUP BY ge.id
            HAVING ABS(ge.distance_m - COALESCE(SUM(s.length_m), 0.0)) > 0.001
        )
        """
    ).fetchone()[0]
    results.append(
        CheckResult(
            "ERROR" if edge_distance_errors else "OK",
            f"graph_edge 距離加總錯誤 {edge_distance_errors} 筆"
            if edge_distance_errors
            else "graph_edge 距離加總一致",
        )
    )

    chain_errors = count_chain_errors(connection)
    results.append(
        CheckResult(
            "ERROR" if chain_errors else "OK",
            f"graph_edge 線段順序或方向錯誤 {chain_errors} 筆"
            if chain_errors
            else "graph_edge 線段順序與方向一致",
        )
    )

    connection_distance_errors = connection.execute(
        """
        SELECT COUNT(*) FROM station_connection
        WHERE ABS(
            distance_m -
            (from_station_offset_m + gap_length_m + to_station_offset_m)
        ) > 0.001
        """
    ).fetchone()[0]
    results.append(
        CheckResult(
            "ERROR" if connection_distance_errors else "OK",
            f"station_connection 距離公式錯誤 {connection_distance_errors} 筆"
            if connection_distance_errors
            else "station_connection 距離公式一致",
        )
    )

    connection_path_errors = count_connection_path_errors(connection)
    results.append(
        CheckResult(
            "ERROR" if connection_path_errors else "OK",
            f"station_connection_has_graph_edge 路徑不連續 {connection_path_errors} 筆"
            if connection_path_errors
            else "station_connection_has_graph_edge 路徑連續",
        )
    )

    if schema_version >= 5:
        forbidden_directions = connection.execute(
            """
            SELECT COUNT(*)
            FROM station_connection_has_graph_edge AS relation
            JOIN graph_edge AS ge ON ge.id = relation.graph_edge_id
            WHERE (ge.direction = 'forward' AND relation.forward = 0)
               OR (ge.direction = 'backward' AND relation.forward = 1)
            """
        ).fetchone()[0]
        results.append(
            CheckResult(
                "ERROR" if forbidden_directions else "OK",
                f"station_connection 違反單向連線 {forbidden_directions} 筆"
                if forbidden_directions
                else "station_connection 遵守單向連線",
            )
        )

        invalid_connection_directions = connection.execute(
            "SELECT COUNT(*) FROM station_connection WHERE direction <> 'forward'"
        ).fetchone()[0]
        missing_reverse_connections = connection.execute(
            """
            SELECT COUNT(*)
            FROM station_connection AS c
            WHERE NOT EXISTS (
                SELECT 1
                FROM station_connection AS reverse
                WHERE reverse.from_anchor_id = c.to_anchor_id
                  AND reverse.to_anchor_id = c.from_anchor_id
            )
            """
        ).fetchone()[0]
        directional_errors = invalid_connection_directions + missing_reverse_connections
        results.append(
            CheckResult(
                "ERROR" if directional_errors else "OK",
                "有向 station_connection 不完整: "
                f"direction錯誤={invalid_connection_directions}, "
                f"缺少反向={missing_reverse_connections}"
                if directional_errors
                else "每個 station_connection 都可直接按 from→to 查詢",
            )
        )

    internal_station_rows = connection.execute(
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
        ORDER BY reached.connection_id, reached.sequence_no
        """
    ).fetchall()
    results.append(
        CheckResult(
            "ERROR" if internal_station_rows else "OK",
            f"station_connection 中途通過其他車站 {len(internal_station_rows)} 筆"
            if internal_station_rows
            else "station_connection 沒有穿越第三個車站",
            [
                f"connection_id={row[0]}, node_id={row[1]}, anchor_id={row[2]}"
                for row in internal_station_rows[:20]
            ],
        )
    )

    missing_anchor_rows = connection.execute(
        """
        SELECT
            s.id,
            s.source_id,
            s.station_code,
            s.name,
            g.group_code,
            g.display_name,
            l.id,
            l.name,
            l.operator_name,
            s.geometry_status,
            GROUP_CONCAT(sc.id || ':' || sc.anchor_status, ', ')
        FROM station AS s
        JOIN station_group AS g ON g.id = s.group_id
        JOIN rail_line AS l ON l.id = s.line_id
        LEFT JOIN station_component AS sc ON sc.station_id = s.id
        WHERE NOT EXISTS (
            SELECT 1 FROM station_component AS anchor_component
            JOIN station_anchor AS a ON a.station_component_id = anchor_component.id
            WHERE anchor_component.station_id = s.id
        )
        GROUP BY s.id
        ORDER BY s.id
        """
    ).fetchall()
    missing_anchor_details = [
        (
            f"station_id={row[0]}, source_id={row[1]}, station_code={row[2]}, "
            f"station={row[3]}, group={row[5]} ({row[4]}), "
            f"line_id={row[6]}, line={row[7]}, operator={row[8]}, "
            f"geometry_status={row[9]}, components={row[10] or '-'}"
        )
        for row in missing_anchor_rows
    ]
    missing_anchors = len(missing_anchor_rows)
    results.append(
        CheckResult(
            "WARN" if missing_anchors else "OK",
            f"沒有錨點的車站 {missing_anchors} 筆"
            if missing_anchors
            else "每個車站都有路網錨點",
            missing_anchor_details,
        )
    )

    topology_errors = connection.execute(
        """
        WITH degree AS (
            SELECT node_id, COUNT(*) AS degree
            FROM (
                SELECT from_node_id AS node_id FROM atomic_segment
                UNION ALL
                SELECT to_node_id AS node_id FROM atomic_segment
            )
            GROUP BY node_id
        )
        SELECT COUNT(*)
        FROM network_node AS n
        JOIN degree AS d ON d.node_id = n.id
        WHERE (n.topology_type = 'junction' AND d.degree < 3)
           OR (n.topology_type <> 'junction' AND d.degree >= 3)
        """
    ).fetchone()[0]
    results.append(
        CheckResult(
            "WARN" if topology_errors else "OK",
            f"分岔分類與度數不一致 {topology_errors} 筆"
            if topology_errors
            else "分岔分類與節點度數一致",
        )
    )

    blank_group_names = connection.execute(
        "SELECT COUNT(*) FROM station_group WHERE TRIM(display_name) = ''"
    ).fetchone()[0]
    results.append(
        CheckResult(
            "WARN" if blank_group_names else "OK",
            f"顯示名為空的車站組 {blank_group_names} 筆"
            if blank_group_names
            else "車站組顯示名齊全",
        )
    )

    invalid_passenger_groups = connection.execute(
        "SELECT COUNT(*) FROM station_group WHERE passengers < 0"
    ).fetchone()[0]
    available_passenger_groups, missing_passenger_groups = connection.execute(
        """
        SELECT COUNT(passengers), COUNT(*) - COUNT(passengers)
        FROM station_group
        """
    ).fetchone()
    results.append(
        CheckResult(
            "ERROR" if invalid_passenger_groups else "OK",
            f"station_group.passengers に負数 {invalid_passenger_groups} 件"
            if invalid_passenger_groups
            else (
                "2024年乗降客数: "
                f"利用可能 {available_passenger_groups} 組 / "
                f"欠測 {missing_passenger_groups} 組"
            ),
        )
    )

    return results


def count_chain_errors(connection: sqlite3.Connection) -> int:
    errors = 0
    edge_rows = connection.execute(
        "SELECT id, from_node_id, to_node_id FROM graph_edge WHERE edge_kind = 'rail'"
    )
    for edge_id, expected_start, expected_end in edge_rows:
        rows = connection.execute(
            """
            SELECT relation.sequence_no, relation.forward,
                   s.from_node_id, s.to_node_id
            FROM graph_edge_has_atomic_segment AS relation
            JOIN atomic_segment AS s ON s.id = relation.atomic_segment_id
            WHERE relation.graph_edge_id = ?
            ORDER BY relation.sequence_no
            """,
            (edge_id,),
        ).fetchall()
        if not rows:
            errors += 1
            continue
        current = expected_start
        valid = True
        for expected_sequence, (sequence_no, forward, from_node, to_node) in enumerate(
            rows
        ):
            if sequence_no != expected_sequence:
                valid = False
                break
            start, end = (from_node, to_node) if forward else (to_node, from_node)
            if start != current:
                valid = False
                break
            current = end
        if not valid or current != expected_end:
            errors += 1
    return errors


def count_connection_path_errors(connection: sqlite3.Connection) -> int:
    errors = 0
    connection_rows = connection.execute(
        """
        SELECT c.id, from_anchor.node_id, to_anchor.node_id
        FROM station_connection AS c
        JOIN station_anchor AS from_anchor ON from_anchor.id = c.from_anchor_id
        JOIN station_anchor AS to_anchor ON to_anchor.id = c.to_anchor_id
        """
    )
    for connection_id, expected_start, expected_end in connection_rows:
        rows = connection.execute(
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
        valid = bool(rows)
        for expected_sequence, (sequence_no, forward, from_node, to_node) in enumerate(
            rows
        ):
            start, end = (from_node, to_node) if forward else (to_node, from_node)
            if sequence_no != expected_sequence or start != current:
                valid = False
                break
            current = end
        if not valid or current != expected_end:
            errors += 1
    return errors


def sql_filter(
    line_ids: list[int] | None,
    alias: str,
    column: str = "line_id",
) -> tuple[str, list[int]]:
    if not line_ids:
        return "", []
    placeholders = ", ".join("?" for _ in line_ids)
    return f" AND {alias}.{column} IN ({placeholders})", line_ids


def load_visual_data(
    connection: sqlite3.Connection, line_ids: list[int] | None
) -> dict[str, Any]:
    line_where, line_parameters = sql_filter(line_ids, "l", "id")
    lines = [
        {
            "id": row[0],
            "railway_type_code": row[1],
            "provider_type_code": row[2],
            "name": row[3],
            "operator": row[4],
        }
        for row in connection.execute(
            """
            SELECT l.id, l.railway_type_code, l.provider_type_code,
                   l.name, l.operator_name
            FROM rail_line AS l
            WHERE 1 = 1
            """
            + line_where
            + " ORDER BY l.id",
            line_parameters,
        )
    ]

    node_coords = {
        row[0]: (row[1], row[2])
        for row in connection.execute("SELECT id, lon, lat FROM network_node")
    }

    edge_where, edge_parameters = sql_filter(line_ids, "s")
    edge_query = (
        """
        SELECT
            ge.id, ge.distance_m, ge.from_node_id, ge.to_node_id,
            s.line_id, relation.sequence_no, relation.forward,
            s.from_node_id, s.to_node_id
        FROM graph_edge AS ge
        JOIN graph_edge_has_atomic_segment AS relation
            ON relation.graph_edge_id = ge.id
        JOIN atomic_segment AS s ON s.id = relation.atomic_segment_id
        WHERE ge.edge_kind = 'rail'
        """
        + edge_where
        + " ORDER BY ge.id, relation.sequence_no"
    )
    edges_by_id: dict[int, dict[str, Any]] = {}
    edge_ids_by_node: dict[int, set[int]] = {}
    for row in connection.execute(edge_query, edge_parameters):
        (
            edge_id,
            distance_m,
            from_node_id,
            to_node_id,
            line_id,
            _sequence_no,
            forward,
            segment_from,
            segment_to,
        ) = row
        item = edges_by_id.setdefault(
            edge_id,
            {
                "id": edge_id,
                "line": line_id,
                "distance": round(distance_m, 3),
                "from": from_node_id,
                "to": to_node_id,
                "points": [],
            },
        )
        start, end = (
            (segment_from, segment_to) if forward else (segment_to, segment_from)
        )
        edge_ids_by_node.setdefault(start, set()).add(edge_id)
        edge_ids_by_node.setdefault(end, set()).add(edge_id)
        if not item["points"]:
            item["points"].append(node_coords[start])
        item["points"].append(node_coords[end])

    station_where, station_parameters = sql_filter(line_ids, "s")
    stations = [
        {
            "node": row[0],
            "lon": row[1],
            "lat": row[2],
            "station": row[3],
            "source_id": row[4],
            "station_code": row[5],
            "station_name": row[6],
            "group": row[7],
            "group_code": row[8],
            "group_name": row[9],
            "passengers": row[10],
            "line": row[11],
            "line_name": row[12],
            "topology": row[13],
        }
        for row in connection.execute(
            """
            WITH ranked_anchor AS (
                SELECT
                    a.node_id,
                    sc.station_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY sc.station_id
                        ORDER BY a.is_primary DESC, sc.component_no, a.anchor_no
                    ) AS rank_no
                FROM station_anchor AS a
                JOIN station_component AS sc ON sc.id = a.station_component_id
            )
            SELECT
                ra.node_id, n.lon, n.lat,
                s.id, s.source_id, s.station_code, s.name,
                g.id, g.group_code, g.display_name, g.passengers,
                s.line_id, l.name, n.topology_type
            FROM ranked_anchor AS ra
            JOIN station AS s ON s.id = ra.station_id
            JOIN station_group AS g ON g.id = s.group_id
            JOIN rail_line AS l ON l.id = s.line_id
            JOIN network_node AS n ON n.id = ra.node_id
            WHERE ra.rank_no = 1
            """
            + station_where
            + " ORDER BY s.id",
            station_parameters,
        )
    ]
    for station in stations:
        station["graph_edges"] = sorted(edge_ids_by_node.get(station["node"], ()))

    junction_where, junction_parameters = sql_filter(line_ids, "n")
    junctions = [
        {"node": row[0], "lon": row[1], "lat": row[2], "line": row[3]}
        for row in connection.execute(
            """
            SELECT n.id, n.lon, n.lat, n.line_id
            FROM network_node AS n
            WHERE n.topology_type = 'junction'
            """
            + junction_where
            + " ORDER BY n.id",
            junction_parameters,
        )
    ]

    all_points = [point for edge in edges_by_id.values() for point in edge["points"]]
    if not all_points:
        raise RuntimeError("指定範圍沒有可視化的 graph_edge。")
    lons = [point[0] for point in all_points]
    lats = [point[1] for point in all_points]
    counts = {
        "lines": len(lines),
        "edges": len(edges_by_id),
        "stations": len(stations),
        "junctions": len(junctions),
    }
    return {
        "bounds": [min(lons), min(lats), max(lons), max(lats)],
        "counts": counts,
        "lines": lines,
        "edges": list(edges_by_id.values()),
        "stations": stations,
        "junctions": junctions,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>N02 鉄道ネットワーク検証</title>
<style>
:root {
  color-scheme: light dark;
  --background: #f7f5f0;
  --foreground: #17212b;
  --muted: rgba(23, 33, 43, .65);
  --surface: rgba(247, 245, 240, .90);
  --border: rgba(23, 33, 43, .22);
  --rail: #687784;
  --station: #d54838;
  --junction: #166e7a;
  --selected: #e69121;
  --grid: rgba(23, 33, 43, .08);
  --route-1: #d53f55;
  --route-2: #2673c7;
  --route-3: #16855a;
  --route-4: #8454bd;
  --route-5: #c56d0a;
  --route-6: #008591;
}
@media (prefers-color-scheme: dark) {
  :root {
    --background: #11171d;
    --foreground: #e8edf1;
    --muted: rgba(232, 237, 241, .68);
    --surface: rgba(17, 23, 29, .90);
    --border: rgba(232, 237, 241, .20);
    --rail: #8495a3;
    --station: #ff7666;
    --junction: #62c4d0;
    --selected: #ffc56b;
    --grid: rgba(232, 237, 241, .07);
    --route-1: #ff7184;
    --route-2: #69a9f4;
    --route-3: #4fd39a;
    --route-4: #bd8df1;
    --route-5: #ffae52;
    --route-6: #53c9d2;
  }
}
* { box-sizing: border-box; }
html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
body {
  background: var(--background);
  color: var(--foreground);
  font: 14px/1.5 "Hiragino Sans", "Hiragino Kaku Gothic ProN",
    -apple-system, BlinkMacSystemFont, "Yu Gothic", YuGothic,
    "Noto Sans JP", "Noto Sans CJK JP", sans-serif;
}
canvas { display: block; width: 100%; height: 100%; touch-action: none; cursor: grab; }
canvas.dragging { cursor: grabbing; }
.hud {
  position: absolute;
  top: 12px;
  left: 12px;
  max-width: min(380px, calc(100% - 24px));
  padding: 9px 11px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  backdrop-filter: blur(8px);
  pointer-events: none;
}
.hud p { margin: 0; }
.hud p + p { margin-top: 4px; }
.muted { color: var(--muted); }
.legend { display: flex; flex-wrap: wrap; gap: 5px 13px; margin-top: 7px; }
.legend span { display: inline-flex; align-items: center; gap: 5px; }
.dot { width: 9px; height: 9px; border-radius: 50%; }
.station-dot { background: var(--station); }
.junction-dot { border: 2px solid var(--junction); background: var(--background); }
.line-dot { width: 14px; height: 2px; background: var(--rail); }
.detail {
  position: absolute;
  right: 12px;
  bottom: 12px;
  max-width: min(440px, calc(100% - 24px));
  min-height: 38px;
  padding: 9px 11px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  backdrop-filter: blur(8px);
  pointer-events: none;
  white-space: pre-line;
}
.detail.route-detail {
  width: min(440px, calc(100% - 24px));
  max-height: min(66vh, 620px);
  overflow-y: auto;
  white-space: normal;
}
.route-detail-heading { font-weight: 600; }
.route-detail-summary { margin-top: 2px; color: var(--muted); }
.route-entry {
  margin-top: 9px;
  padding: 8px 9px;
  border: 1px solid var(--border);
  border-left: 4px solid var(--route-color);
  border-radius: 6px;
  background: color-mix(in srgb, var(--surface) 92%, var(--route-color));
}
.route-entry-title { display: flex; align-items: center; gap: 7px; font-weight: 600; }
.route-swatch { width: 12px; height: 4px; border-radius: 3px; background: var(--route-color); }
.route-entry dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 8px; margin: 5px 0 0; }
.route-entry dt { color: var(--muted); }
.route-entry dd { margin: 0; overflow-wrap: anywhere; }
.route-color-1 { --route-color: var(--route-1); }
.route-color-2 { --route-color: var(--route-2); }
.route-color-3 { --route-color: var(--route-3); }
.route-color-4 { --route-color: var(--route-4); }
.route-color-5 { --route-color: var(--route-5); }
.route-color-6 { --route-color: var(--route-6); }
.top-controls {
  position: absolute;
  right: 12px;
  top: 12px;
  display: grid;
  justify-items: end;
  gap: 8px;
}
.route-mode-control {
  display: flex;
  align-items: center;
  gap: 11px;
  max-width: min(290px, calc(100vw - 24px));
  padding: 9px 11px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  backdrop-filter: blur(8px);
  cursor: pointer;
}
.route-mode-control:hover { border-color: color-mix(in srgb, var(--foreground) 42%, transparent); }
.route-mode-control:has(input:focus-visible) { outline: 2px solid var(--selected); outline-offset: 2px; }
.route-mode-copy { display: grid; flex: 1; }
.route-mode-copy strong { font-weight: 600; }
.route-mode-copy small { color: var(--muted); }
.route-mode-control input { width: 19px; height: 19px; margin: 0; accent-color: var(--selected); cursor: pointer; }
.reset {
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  color: var(--foreground);
  padding: 7px 10px;
  font: inherit;
  cursor: pointer;
}
.reset:focus-visible { outline: 2px solid var(--selected); outline-offset: 2px; }
@media (max-width: 520px) {
  .hud { right: 12px; top: 160px; max-width: none; }
  .top-controls { left: 12px; justify-items: stretch; }
  .route-mode-control { max-width: none; }
  .reset { justify-self: end; }
  .detail { left: 12px; bottom: 12px; max-width: none; }
  .detail.route-detail { width: auto; max-height: 50vh; }
}
</style>
</head>
<body>
<canvas id="map" role="img" aria-label="N02鉄道ネットワーク。塗りつぶし点は駅、中抜き点は分岐、線は接続、ラベルは測地距離を示します。"></canvas>
<section class="hud" aria-live="polite">
  <p id="counts"></p>
  <p class="muted">ドラッグで移動、ホイールまたはトラックパッドの2本指操作で拡大・縮小。駅にカーソルを合わせると同じグループを強調表示します。</p>
  <div class="legend" aria-label="凡例">
    <span><i class="dot station-dot"></i>駅</span>
    <span><i class="dot junction-dot"></i>分岐</span>
    <span><i class="dot line-dot"></i>接続</span>
  </div>
</section>
<div class="top-controls">
  <label class="route-mode-control" for="route-mode">
    <span class="route-mode-copy">
      <strong>路線モードを開きますか？</strong>
      <small>命中した路線全体を色分けして確認</small>
    </span>
    <input id="route-mode" type="checkbox">
  </label>
  <button class="reset" id="reset" type="button">全体を表示</button>
</div>
<aside class="detail" id="detail" aria-live="polite">ノードまたは接続にカーソルを合わせるか、クリックすると詳細を表示します。</aside>
<script id="network-data" type="application/json">__NETWORK_DATA__</script>
<script>
(() => {
  "use strict";
  const data = JSON.parse(document.getElementById("network-data").textContent);
  const canvas = document.getElementById("map");
  const ctx = canvas.getContext("2d");
  const detail = document.getElementById("detail");
  const counts = document.getElementById("counts");
  const resetButton = document.getElementById("reset");
  const routeModeToggle = document.getElementById("route-mode");
  const styles = () => getComputedStyle(document.documentElement);
  const tau = Math.PI * 2;
  const maxMercatorLat = 85.05112878;
  const edgeHitRadius = 12;
  const coincidentEdgeTolerance = 0.75;
  const parallelDirectionThreshold = Math.cos(15 * Math.PI / 180);
  const pointers = new Map();
  let dpr = 1;
  let width = 1;
  let height = 1;
  let fitScale = 1;
  let view = { scale: 1, tx: 0, ty: 0 };
  let dragOrigin = null;
  let pinchOrigin = null;
  let hovered = null;
  let selected = null;
  let routeMode = false;
  let framePending = false;

  const lineById = new Map(data.lines.map(line => [line.id, line]));
  const edgeById = new Map(data.edges.map(edge => [edge.id, edge]));
  const edgesByLine = new Map();
  const stationNodes = new Set(data.stations.map(station => station.node));
  const stationsByGroup = new Map();
  data.edges.forEach(edge => {
    edge.world = edge.points.map(point => project(point[0], point[1]));
    const xs = edge.world.map(point => point[0]);
    const ys = edge.world.map(point => point[1]);
    edge.box = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
    const lineEdges = edgesByLine.get(edge.line) || [];
    lineEdges.push(edge);
    edgesByLine.set(edge.line, lineEdges);
  });
  data.stations.forEach(station => {
    station.world = project(station.lon, station.lat);
    const members = stationsByGroup.get(station.group) || [];
    members.push(station);
    stationsByGroup.set(station.group, members);
  });
  data.junctions = data.junctions.filter(junction => !stationNodes.has(junction.node));
  data.junctions.forEach(junction => { junction.world = project(junction.lon, junction.lat); });

  counts.textContent = `${data.counts.lines.toLocaleString()} 路線・` +
    `${data.counts.stations.toLocaleString()} 駅・` +
    `${data.counts.junctions.toLocaleString()} 分岐・` +
    `${data.counts.edges.toLocaleString()} 接続`;

  function project(lon, lat) {
    const safeLat = Math.max(-maxMercatorLat, Math.min(maxMercatorLat, lat));
    const x = (lon + 180) / 360;
    const radians = safeLat * Math.PI / 180;
    const y = (1 - Math.log(Math.tan(radians) + 1 / Math.cos(radians)) / Math.PI) / 2;
    return [x, y];
  }

  function screen(point) {
    return [point[0] * view.scale + view.tx, point[1] * view.scale + view.ty];
  }

  function resetView() {
    const min = project(data.bounds[0], data.bounds[3]);
    const max = project(data.bounds[2], data.bounds[1]);
    const spanX = Math.max(1e-9, max[0] - min[0]);
    const spanY = Math.max(1e-9, max[1] - min[1]);
    const padding = Math.min(54, Math.max(18, Math.min(width, height) * 0.06));
    fitScale = Math.min((width - padding * 2) / spanX, (height - padding * 2) / spanY);
    view.scale = fitScale;
    view.tx = (width - (min[0] + max[0]) * fitScale) / 2;
    view.ty = (height - (min[1] + max[1]) * fitScale) / 2;
    selected = null;
    updateDetail();
    requestDraw();
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const oldWidth = width;
    const oldHeight = height;
    dpr = Math.min(2, window.devicePixelRatio || 1);
    width = Math.max(1, rect.width);
    height = Math.max(1, rect.height);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    if (oldWidth === 1 && oldHeight === 1) {
      resetView();
    } else {
      view.tx += (width - oldWidth) / 2;
      view.ty += (height - oldHeight) / 2;
      requestDraw();
    }
  }

  function requestDraw() {
    if (framePending) return;
    framePending = true;
    requestAnimationFrame(() => {
      framePending = false;
      draw();
    });
  }

  function draw() {
    const css = styles();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    drawGrid(css);
    drawEdges(css);
    drawJunctions(css);
    drawStations(css);
    drawLabels(css);
  }

  function drawGrid(css) {
    const zoom = view.scale / fitScale;
    if (zoom < 1.4) return;
    const step = zoom > 18 ? 0.1 : zoom > 5 ? 0.5 : 1;
    ctx.strokeStyle = css.getPropertyValue("--grid");
    ctx.lineWidth = 1;
    const west = unprojectX((0 - view.tx) / view.scale);
    const east = unprojectX((width - view.tx) / view.scale);
    const north = unprojectY((0 - view.ty) / view.scale);
    const south = unprojectY((height - view.ty) / view.scale);
    ctx.beginPath();
    for (let lon = Math.ceil(west / step) * step; lon <= east; lon += step) {
      const x = screen(project(lon, 0))[0];
      ctx.moveTo(x, 0); ctx.lineTo(x, height);
    }
    for (let lat = Math.ceil(south / step) * step; lat <= north; lat += step) {
      const y = screen(project(0, lat))[1];
      ctx.moveTo(0, y); ctx.lineTo(width, y);
    }
    ctx.stroke();
  }

  function unprojectX(x) { return x * 360 - 180; }
  function unprojectY(y) {
    return Math.atan(Math.sinh(Math.PI * (1 - 2 * y))) * 180 / Math.PI;
  }

  function visibleBox(box, margin = 8) {
    const a = screen([box[0], box[1]]);
    const b = screen([box[2], box[3]]);
    return Math.max(a[0], b[0]) >= -margin && Math.min(a[0], b[0]) <= width + margin &&
      Math.max(a[1], b[1]) >= -margin && Math.min(a[1], b[1]) <= height + margin;
  }

  function drawEdges(css) {
    const rail = css.getPropertyValue("--rail");
    const selectedColor = css.getPropertyValue("--selected");
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    function strokeEdge(edge, color, lineWidth) {
      if (!visibleBox(edge.box)) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = lineWidth;
      ctx.beginPath();
      edge.world.forEach((point, index) => {
        const [x, y] = screen(point);
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    for (const edge of data.edges) {
      const active = !routeMode && isActive("edge", edge.id);
      strokeEdge(edge, active ? selectedColor : rail, active ? 3.2 : 1.25);
    }

    if (!routeMode) return;
    const match = routeMatch(hovered || selected);
    match.lineIds.forEach((lineId, index) => {
      const widthOffset = (match.lineIds.length - index - 1) * 1.45;
      const color = css.getPropertyValue(`--route-${index % 6 + 1}`);
      for (const edge of edgesByLine.get(lineId) || []) {
        strokeEdge(edge, color, 3.4 + widthOffset);
      }
    });
  }

  function routeMatch(feature) {
    const edges = [];
    const stations = [];
    if (feature && feature.type === "edge") {
      edges.push(...(feature.items || [feature.item]));
    } else if (feature && feature.type === "station") {
      stations.push(...(feature.items || [feature.item]));
      for (const station of stations) {
        for (const edgeId of station.graph_edges || []) {
          const edge = edgeById.get(edgeId);
          if (edge) edges.push(edge);
        }
      }
    }
    const uniqueEdges = [...new Map(edges.map(edge => [edge.id, edge])).values()];
    const lineIds = [];
    const seenLineIds = new Set();
    for (const edge of uniqueEdges) {
      if (!seenLineIds.has(edge.line)) {
        seenLineIds.add(edge.line);
        lineIds.push(edge.line);
      }
    }
    for (const station of stations) {
      if (!seenLineIds.has(station.line)) {
        seenLineIds.add(station.line);
        lineIds.push(station.line);
      }
    }
    return { edges: uniqueEdges, stations, lineIds };
  }

  function drawJunctions(css) {
    const nodeScale = routeNodeScale();
    ctx.strokeStyle = css.getPropertyValue("--junction");
    ctx.fillStyle = css.getPropertyValue("--background");
    ctx.lineWidth = routeMode
      ? Math.max(0.15, Math.min(1.7, 0.45 * nodeScale))
      : 1.7;
    for (const junction of data.junctions) {
      const [x, y] = screen(junction.world);
      if (x < -9 || x > width + 9 || y < -9 || y > height + 9) continue;
      const active = isActive("junction", junction.node);
      const radius = routeMode
        ? Math.min(active ? 5 : 3.4, (active ? 1.4 : 1) * nodeScale)
        : active ? 5 : 3.4;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, tau);
      ctx.fill();
      ctx.stroke();
    }
  }

  function drawStations(css) {
    const stationColor = css.getPropertyValue("--station");
    const selectedColor = css.getPropertyValue("--selected");
    const activeGroup = activeStationGroup();
    const nodeScale = routeNodeScale();
    for (const station of data.stations) {
      const [x, y] = screen(station.world);
      if (x < -9 || x > width + 9 || y < -9 || y > height + 9) continue;
      const active = routeMode
        ? isActive("station", station.station)
        : activeGroup !== null && station.group === activeGroup;
      ctx.beginPath();
      ctx.arc(x, y, (routeMode ? Math.min(3.1, 0.8 * nodeScale) : 3.1), 0, tau);
      ctx.fillStyle = active ? selectedColor : stationColor;
      ctx.fill();
      if (station.topology === "junction") {
        ctx.strokeStyle = css.getPropertyValue("--junction");
        ctx.lineWidth = routeMode
          ? Math.max(0.15, Math.min(1.5, 0.35 * nodeScale))
          : 1.5;
        ctx.stroke();
      }
    }
  }

  function drawLabels(css) {
    const zoom = view.scale / fitScale;
    const activeGroup = activeStationGroup();
    const showAllStations = zoom >= 2.2;
    if (!showAllStations && activeGroup === null) return;
    const occupied = [];
    ctx.font = '12px "Hiragino Sans", "Hiragino Kaku Gothic ProN", -apple-system, BlinkMacSystemFont, "Yu Gothic", YuGothic, sans-serif';
    ctx.textBaseline = "middle";
    ctx.fillStyle = css.getPropertyValue("--foreground");
    ctx.strokeStyle = css.getPropertyValue("--background");
    ctx.lineWidth = 3.5;
    ctx.lineJoin = "round";

    function drawStationLabel(station) {
      const [x, y] = screen(station.world);
      if (x < 0 || x > width || y < 0 || y > height) return;
      const label = station.group_name;
      const textWidth = ctx.measureText(label).width;
      const box = [x + 6, y - 8, x + 10 + textWidth, y + 8];
      if (collides(box, occupied)) return;
      occupied.push(box);
      ctx.strokeText(label, x + 8, y);
      ctx.fillText(label, x + 8, y);
    }

    if (activeGroup !== null) {
      for (const station of stationsByGroup.get(activeGroup) || []) {
        drawStationLabel(station);
      }
    }
    if (showAllStations) {
      for (const station of data.stations) {
        if (station.group !== activeGroup) drawStationLabel(station);
      }
    }

    if (zoom < 4.2) return;
    ctx.font = '11px "Hiragino Sans", "Hiragino Kaku Gothic ProN", -apple-system, BlinkMacSystemFont, "Yu Gothic", YuGothic, sans-serif';
    for (const edge of data.edges) {
      if (!visibleBox(edge.box)) continue;
      const screenPoints = edge.world.map(screen);
      const screenLength = polylineLength(screenPoints);
      if (screenLength < 100) continue;
      const mid = pointAtLength(screenPoints, screenLength / 2);
      const label = formatDistance(edge.distance);
      const textWidth = ctx.measureText(label).width;
      const box = [mid[0] - textWidth / 2 - 3, mid[1] - 8, mid[0] + textWidth / 2 + 3, mid[1] + 8];
      if (collides(box, occupied)) continue;
      occupied.push(box);
      ctx.textAlign = "center";
      ctx.strokeText(label, mid[0], mid[1]);
      ctx.fillText(label, mid[0], mid[1]);
      ctx.textAlign = "start";
    }
  }

  function collides(box, boxes) {
    return boxes.some(other => !(box[2] < other[0] || box[0] > other[2] || box[3] < other[1] || box[1] > other[3]));
  }

  function polylineLength(points) {
    let total = 0;
    for (let i = 1; i < points.length; i += 1) total += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
    return total;
  }

  function pointAtLength(points, target) {
    let traversed = 0;
    for (let i = 1; i < points.length; i += 1) {
      const length = Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
      if (traversed + length >= target) {
        const ratio = length === 0 ? 0 : (target - traversed) / length;
        return [
          points[i - 1][0] + (points[i][0] - points[i - 1][0]) * ratio,
          points[i - 1][1] + (points[i][1] - points[i - 1][1]) * ratio,
        ];
      }
      traversed += length;
    }
    return points[points.length - 1];
  }

  function featureContains(feature, type, id) {
    if (!feature || feature.type !== type) return false;
    if (feature.ids) return feature.ids.includes(id);
    return feature.id === id;
  }

  function isActive(type, id) {
    return featureContains(selected, type, id) || featureContains(hovered, type, id);
  }

  function activeStationGroup() {
    if (routeMode) return null;
    if (hovered && hovered.type === "station") return hovered.item.group;
    if (selected && selected.type === "station") return selected.item.group;
    return null;
  }

  function routeNodeScale() {
    if (!routeMode) return 1;
    const zoom = view.scale / fitScale;
    return Math.max(0.015, Math.min(8, Math.pow(zoom / 8, 2)));
  }

  function nodeHitRadius() {
    if (!routeMode) return edgeHitRadius;
    return Math.max(1, Math.min(7, 1.8 * routeNodeScale()));
  }

  function formatDistance(metres) {
    return metres < 1000 ? `${Math.round(metres)} m` : `${(metres / 1000).toFixed(metres < 10000 ? 2 : 1)} km`;
  }

  function zoomAt(x, y, factor) {
    const oldScale = view.scale;
    const nextScale = Math.max(fitScale * 0.75, Math.min(fitScale * 1800, oldScale * factor));
    const worldX = (x - view.tx) / oldScale;
    const worldY = (y - view.ty) / oldScale;
    view.scale = nextScale;
    view.tx = x - worldX * nextScale;
    view.ty = y - worldY * nextScale;
    requestDraw();
  }

  function nearestFeature(x, y) {
    let best = null;
    const hitRadius = nodeHitRadius();
    let bestDistance = hitRadius;
    const stationCandidates = [];
    for (const station of data.stations) {
      const point = screen(station.world);
      const distance = Math.hypot(point[0] - x, point[1] - y);
      if (distance < hitRadius) stationCandidates.push({ station, distance });
      if (distance < bestDistance) {
        bestDistance = distance;
        best = { type: "station", id: station.station, item: station };
      }
    }
    if (routeMode && stationCandidates.length) {
      stationCandidates.sort((a, b) => a.distance - b.distance || a.station.station - b.station.station);
      const nearestDistance = stationCandidates[0].distance;
      const stations = stationCandidates
        .filter(candidate => candidate.distance <= nearestDistance + coincidentEdgeTolerance)
        .map(candidate => candidate.station);
      return {
        type: "station",
        id: stations[0].station,
        ids: stations.map(station => station.station),
        item: stations[0],
        items: stations,
      };
    }
    for (const junction of data.junctions) {
      const point = screen(junction.world);
      const distance = Math.hypot(point[0] - x, point[1] - y);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = { type: "junction", id: junction.node, item: junction };
      }
    }
    if (best) return best;

    const edgeCandidates = [];
    for (const edge of data.edges) {
      if (!visibleBox(edge.box, edgeHitRadius)) continue;
      const hit = nearestPointOnEdge(edge, x, y);
      if (hit && hit.distance < edgeHitRadius) edgeCandidates.push({ edge, hit });
    }
    if (edgeCandidates.length === 0) return null;

    edgeCandidates.sort((a, b) => a.hit.distance - b.hit.distance || a.edge.id - b.edge.id);
    const nearest = edgeCandidates[0];
    const coincident = edgeCandidates.filter(candidate =>
      candidate.hit.distance <= nearest.hit.distance + coincidentEdgeTolerance &&
      directionsAreParallel(candidate.hit, nearest.hit)
    );
    const edges = coincident.map(candidate => candidate.edge);
    return {
      type: "edge",
      id: edges[0].id,
      ids: edges.map(edge => edge.id),
      item: edges[0],
      items: edges,
    };
  }

  function nearestPointOnEdge(edge, x, y) {
    const points = edge.world.map(screen);
    let nearest = null;
    for (let i = 1; i < points.length; i += 1) {
      const hit = pointSegmentHit(x, y, points[i - 1], points[i]);
      if (!nearest || hit.distance < nearest.distance) nearest = hit;
    }
    return nearest;
  }

  function pointSegmentHit(x, y, a, b) {
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const lengthSquared = dx * dx + dy * dy;
    const ratio = lengthSquared === 0 ? 0 : Math.max(0, Math.min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / lengthSquared));
    return {
      distance: Math.hypot(x - (a[0] + ratio * dx), y - (a[1] + ratio * dy)),
      dx,
      dy,
    };
  }

  function directionsAreParallel(a, b) {
    const aLength = Math.hypot(a.dx, a.dy);
    const bLength = Math.hypot(b.dx, b.dy);
    if (aLength === 0 || bLength === 0) return false;
    const cosine = Math.abs((a.dx * b.dx + a.dy * b.dy) / (aLength * bLength));
    return cosine >= parallelDirectionThreshold;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  function graphEdgeNumbers(edges) {
    return edges.length ? edges.map(edge => `#${edge.id}`).join("、") : "なし";
  }

  function updateRouteDetail(feature) {
    if (!feature || !["edge", "station"].includes(feature.type)) return false;
    const match = routeMatch(feature);
    const heading = feature.type === "edge"
      ? `命中 graph_edge：${graphEdgeNumbers(match.edges)}`
      : `駅：${[...new Set(match.stations.map(station => station.station_name))].map(escapeHtml).join("、")}`;
    const edgeSummary = feature.type === "station"
      ? `<div class="route-detail-summary">関連 graph_edge：${graphEdgeNumbers(match.edges)}</div>`
      : "";
    const entries = match.lineIds.map((lineId, index) => {
      const line = lineById.get(lineId);
      const hitEdges = match.edges.filter(edge => edge.line === lineId);
      const allEdges = edgesByLine.get(lineId) || [];
      const name = line ? line.name : "不明な路線";
      const operator = line ? line.operator : "不明";
      const railwayType = line ? line.railway_type_code : "不明";
      const providerType = line ? line.provider_type_code : "不明";
      return `<section class="route-entry route-color-${index % 6 + 1}">
        <div class="route-entry-title"><i class="route-swatch"></i>rail_line #${lineId}</div>
        <dl>
          <dt>路線名</dt><dd>${escapeHtml(name)}</dd>
          <dt>事業者</dt><dd>${escapeHtml(operator)}</dd>
          <dt>鉄道区分コード</dt><dd>${escapeHtml(railwayType)}</dd>
          <dt>事業者区分コード</dt><dd>${escapeHtml(providerType)}</dd>
          <dt>命中 graph_edge</dt><dd>${graphEdgeNumbers(hitEdges)}</dd>
          <dt>路線全体</dt><dd>${allEdges.length.toLocaleString("ja-JP")} graph_edge</dd>
        </dl>
      </section>`;
    }).join("");
    detail.classList.add("route-detail");
    detail.innerHTML = `<div class="route-detail-heading">${heading}</div>${edgeSummary}
      <div class="route-detail-summary">命中 rail_line：${match.lineIds.length}件</div>${entries}`;
    return true;
  }

  function updateDetail() {
    const feature = hovered || selected;
    if (!feature) {
      detail.classList.remove("route-detail");
      detail.textContent = routeMode
        ? "駅または接続にカーソルを合わせると、命中した路線全体を確認できます。"
        : "ノードまたは接続にカーソルを合わせるか、クリックすると詳細を表示します。";
      return;
    }
    if (routeMode && updateRouteDetail(feature)) return;
    detail.classList.remove("route-detail");
    if (feature.type === "station") {
      const station = feature.item;
      const members = stationsByGroup.get(station.group) || [station];
      const routes = [...new Set(members.map(member => member.line_name))].join("、");
      const suffix = station.topology === "junction" ? "（分岐を兼ねる）" : "";
      const passengers = station.passengers === null
        ? "欠測（完全な組合計を確定できません）"
        : `${station.passengers.toLocaleString("ja-JP")} 人/日`;
      detail.textContent = [
        `${station.group_name}（グループ #${station.group}）`,
        `グループコード：${station.group_code}`,
        `2024年1日あたり乗降客数：${passengers}`,
        `同グループの表示駅：${members.length}件`,
        `路線：${routes}`,
        `対象駅：${station.line_name}・${station.station_name}${suffix}`,
        `出典ID：${station.source_id}`,
      ].join("\n");
    } else if (feature.type === "junction") {
      const line = lineById.get(feature.item.line);
      detail.textContent = [
        `分岐ノード #${feature.item.node}`,
        `路線：${line ? line.name : "不明な路線"}`,
      ].join("\n");
    } else {
      const edges = feature.items || [feature.item];
      const lines = [];
      const seenLineIds = new Set();
      for (const edge of edges) {
        if (seenLineIds.has(edge.line)) continue;
        seenLineIds.add(edge.line);
        const line = lineById.get(edge.line);
        lines.push(line ? `${line.name}（${line.operator}）` : `不明な路線（ID ${edge.line}）`);
      }
      if (edges.length === 1) {
        detail.textContent = [
          `接続 #${edges[0].id}`,
          `路線：${lines[0]}`,
          `距離：${formatDistance(edges[0].distance)}`,
        ].join("\n");
      } else {
        detail.textContent = [
          `同位置の接続：${edges.length}件`,
          "路線：",
          ...lines.map(line => `・${line}`),
          "接続と距離：",
          ...edges.map(edge => `・#${edge.id}：${formatDistance(edge.distance)}`),
        ].join("\n");
      }
    }
  }

  canvas.addEventListener("wheel", event => {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const factor = Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * 0.008);
    zoomAt(event.clientX - rect.left, event.clientY - rect.top, factor);
  }, { passive: false });

  canvas.addEventListener("pointerdown", event => {
    canvas.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, [event.clientX, event.clientY]);
    canvas.classList.add("dragging");
    if (pointers.size === 1) {
      dragOrigin = { x: event.clientX, y: event.clientY, tx: view.tx, ty: view.ty };
    } else if (pointers.size === 2) {
      const points = [...pointers.values()];
      pinchOrigin = {
        distance: Math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1]),
        scale: view.scale,
        tx: view.tx,
        ty: view.ty,
        center: [(points[0][0] + points[1][0]) / 2, (points[0][1] + points[1][1]) / 2],
      };
      dragOrigin = null;
    }
  });

  canvas.addEventListener("pointermove", event => {
    if (pointers.has(event.pointerId)) {
      pointers.set(event.pointerId, [event.clientX, event.clientY]);
      if (pointers.size === 1 && dragOrigin) {
        view.tx = dragOrigin.tx + event.clientX - dragOrigin.x;
        view.ty = dragOrigin.ty + event.clientY - dragOrigin.y;
        requestDraw();
      } else if (pointers.size === 2 && pinchOrigin) {
        const rect = canvas.getBoundingClientRect();
        const points = [...pointers.values()];
        const center = [(points[0][0] + points[1][0]) / 2, (points[0][1] + points[1][1]) / 2];
        const distance = Math.hypot(points[1][0] - points[0][0], points[1][1] - points[0][1]);
        view.scale = pinchOrigin.scale;
        view.tx = pinchOrigin.tx;
        view.ty = pinchOrigin.ty;
        zoomAt(
          center[0] - rect.left,
          center[1] - rect.top,
          distance / Math.max(1, pinchOrigin.distance)
        );
        view.tx += center[0] - pinchOrigin.center[0];
        view.ty += center[1] - pinchOrigin.center[1];
      }
      return;
    }
    const rect = canvas.getBoundingClientRect();
    hovered = nearestFeature(event.clientX - rect.left, event.clientY - rect.top);
    updateDetail();
    requestDraw();
  });

  function releasePointer(event) {
    pointers.delete(event.pointerId);
    if (pointers.size === 0) {
      canvas.classList.remove("dragging");
      dragOrigin = null;
      pinchOrigin = null;
    } else if (pointers.size === 1) {
      const remaining = [...pointers.values()][0];
      dragOrigin = { x: remaining[0], y: remaining[1], tx: view.tx, ty: view.ty };
      pinchOrigin = null;
    }
  }
  canvas.addEventListener("pointerup", event => {
    if (dragOrigin && Math.hypot(event.clientX - dragOrigin.x, event.clientY - dragOrigin.y) < 4) {
      const rect = canvas.getBoundingClientRect();
      selected = nearestFeature(event.clientX - rect.left, event.clientY - rect.top);
      updateDetail();
      requestDraw();
    }
    releasePointer(event);
  });
  canvas.addEventListener("pointercancel", releasePointer);
  canvas.addEventListener("pointerleave", () => {
    if (pointers.size === 0) {
      hovered = null;
      updateDetail();
      requestDraw();
    }
  });
  routeModeToggle.addEventListener("change", () => {
    routeMode = routeModeToggle.checked;
    hovered = null;
    selected = null;
    updateDetail();
    requestDraw();
  });
  resetButton.addEventListener("click", resetView);
  window.addEventListener("resize", resize);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", requestDraw);
  resize();
})();
</script>
</body>
</html>
"""


def render_html(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028")
    return HTML_TEMPLATE.replace("__NETWORK_DATA__", payload)


def default_output_path(database: Path) -> Path:
    return database.with_name(f"{database.stem}_visualizer.html")


def main() -> None:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"找不到 SQLite: {database}")

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        checks = validate_database(connection)
        for result in checks:
            print(f"[{result.level}] {result.message}")
            for detail in result.details:
                print(f"       - {detail}")
        has_error = any(result.level == "ERROR" for result in checks)
        has_warning = any(result.level == "WARN" for result in checks)
        if has_error or (args.strict and has_warning):
            raise SystemExit(1)
        if args.check_only:
            return
        data = load_visual_data(connection, args.line_id)

    output = (args.output or default_output_path(database)).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(data), encoding="utf-8")
    print(f"HTML 作成完了: {output}")
    print(
        "  "
        f"{data['counts']['lines']:,} 路線 / "
        f"{data['counts']['stations']:,} 車站 / "
        f"{data['counts']['junctions']:,} 分岔 / "
        f"{data['counts']['edges']:,} 連線"
    )
    if args.open:
        webbrowser.open(output.as_uri())


if __name__ == "__main__":
    main()
