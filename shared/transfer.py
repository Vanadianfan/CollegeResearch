"""shared/transfer.py - 車站轉乘判斷與轉乘成本模組"""

from __future__ import annotations

import math
import sqlite3
from typing import Any

# 參數設定
WALK_SPEED_MPS = 80.0 / 60.0       # 80 m/min ~= 1.333 m/s
CIRCUITY_FACTOR = 1.2              # 站內/街區繞行係數
FIXED_TRANSFER_PENALTY_S = 180.0   # 固定轉乘懲罰 (3分鐘: 剪票口、上下階梯、平均候車)
MAX_TRANSFER_DISTANCE_M = 600.0    # 歐氏距離轉乘上限 600m


def fast_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """
    以局部等角投影計算精確地面距離 (公尺)，消除高緯度經度壓縮。
    東京約北緯35度，1度經度約91km，此函數可精確反映地面距離。
    """
    lat_rad = math.radians((lat1 + lat2) / 2.0)
    dx = math.radians(lon2 - lon1) * 6378137.0 * math.cos(lat_rad)
    dy = math.radians(lat2 - lat1) * 6378137.0
    return math.hypot(dx, dy)


def calculate_transfer_cost(distance_m: float) -> tuple[float, float]:
    """
    計算轉乘時間成本。
    若兩線路交會於完全同一個點 (distance_m ~ 0)，純步行時間為 0，其餘固定轉乘成本 (180s) 照常計入。
    回傳: (walk_time_s, total_transfer_cost_s)
    """
    if distance_m < 1e-4:
        walk_time_s = 0.0
    else:
        walk_time_s = (distance_m * CIRCUITY_FACTOR) / WALK_SPEED_MPS
    total_cost_s = walk_time_s + FIXED_TRANSFER_PENALTY_S
    return walk_time_s, total_cost_s


def get_all_station_coordinates(connection: sqlite3.Connection) -> dict[int, tuple[float, float, str, str]]:
    """
    載入所有車站之代表坐標 (primary anchor)。
    回傳: station_id -> (lon, lat, station_name, line_name)
    """
    query = """
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
            s.id, n.lon, n.lat, s.name, l.name
        FROM ranked_anchor AS ra
        JOIN station AS s ON s.id = ra.station_id
        JOIN rail_line AS l ON l.id = s.line_id
        JOIN network_node AS n ON n.id = ra.node_id
        WHERE ra.rank_no = 1
    """
    coords = {}
    for row in connection.execute(query):
        coords[row[0]] = (row[1], row[2], row[3], row[4])
    return coords


def get_transferable_stations(
    connection: sqlite3.Connection,
    station_id: int,
    max_distance_m: float = MAX_TRANSFER_DISTANCE_M,
    station_coords: dict[int, tuple[float, float, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """
    給出一個車站 id，輸出所有距離在 600m 以內的可轉乘車站列表與詳細資訊。
    """
    if station_coords is None:
        station_coords = get_all_station_coordinates(connection)

    if station_id not in station_coords:
        return []

    base_lon, base_lat, base_name, base_line = station_coords[station_id]
    results: list[dict[str, Any]] = []

    # 粗篩經緯度範圍 (0.008度約 700~900m)
    delta_deg = (max_distance_m / 111000.0) * 1.5

    for other_id, (o_lon, o_lat, o_name, o_line) in station_coords.items():
        if other_id == station_id:
            continue
        if abs(base_lat - o_lat) > delta_deg or abs(base_lon - o_lon) > delta_deg:
            continue

        dist_m = fast_distance_m(base_lon, base_lat, o_lon, o_lat)
        if dist_m <= max_distance_m:
            walk_s, total_s = calculate_transfer_cost(dist_m)
            results.append({
                "station_id": other_id,
                "station_name": o_name,
                "line_name": o_line,
                "distance_m": round(dist_m, 2),
                "walk_time_s": round(walk_s, 1),
                "total_time_s": round(total_s, 1),
                "edge_repr": f"{station_id}-{other_id}",
            })

    results.sort(key=lambda item: item["distance_m"])
    return results


def get_transferable_station_ids(
    connection: sqlite3.Connection,
    station_id: int,
    max_distance_m: float = MAX_TRANSFER_DISTANCE_M,
    station_coords: dict[int, tuple[float, float, str, str]] | None = None,
) -> list[int]:
    """
    給出一個車站 id，僅輸出符合定義的可轉乘車站 id 清單。
    """
    stations = get_transferable_stations(
        connection,
        station_id,
        max_distance_m=max_distance_m,
        station_coords=station_coords,
    )
    return [item["station_id"] for item in stations]
