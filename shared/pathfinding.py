"""shared/pathfinding.py - 兩站間時間成本最短路徑演算法模組"""

from __future__ import annotations

import heapq
import sqlite3
from typing import Any

from .params import DEFAULT_PARAMS, NetworkParams
from .transfer import (
    calculate_transfer_cost,
    fast_distance_m,
    get_all_operational_stop_coordinates,
    get_all_station_coordinates,
)

# 列車運行參數（維持模組層級常數以確保舊代碼完全相容）
TRAIN_SPEED_MPS = DEFAULT_PARAMS.train_speed_mps
SHINKANSEN_SPEED_MPS = DEFAULT_PARAMS.shinkansen_speed_mps
TRAIN_STOP_PENALTY_S = DEFAULT_PARAMS.train_stop_penalty_s


class RailwayGraph:
    """
    整合鐵路線路與步行轉乘之時空拓撲圖（預設使用 ekidata 運營網路與 N02 物理距離）。
    節點: operational_line_stop.id (或傳統模式下之 station.id)
    邊:
      1. 既有軌道邊: 代表兩相鄰停靠站間列車運行，邊標識為 operational_edge.id (整數邊號)
      2. 擴展轉乘邊: 代表上限距離內車站轉乘，邊標識為 '車站id1-車站id2'
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        params: NetworkParams = DEFAULT_PARAMS,
        region: str | None = None,
        use_operational: bool = True,
    ):
        self.params = params
        self.region = region if region in ("tokyo", "kinki") else None
        self.use_operational = use_operational
        self.adj: dict[int, list[tuple[int, float, Any, str]]] = {}
        # u -> list of (v, time_cost_s, edge_repr, edge_kind)
        # edge_kind: 'rail' | 'transfer'
        self._build_graph(connection)

    def _add_edge(self, u: int, v: int, cost_s: float, edge_repr: Any, edge_kind: str):
        self.adj.setdefault(u, []).append((v, cost_s, edge_repr, edge_kind))

    def _build_graph(self, connection: sqlite3.Connection):
        cur = connection.cursor()
        has_operational = (
            self.use_operational
            and cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'operational_edge'"
            ).fetchone()
            is not None
        )

        if has_operational:
            where_clause = ""
            sql_params: tuple[object, ...] = ()
            if self.region is not None:
                where_clause = "AND s_from.region = ? AND s_to.region = ?"
                sql_params = (self.region, self.region)

            cur.execute(
                f"""
                SELECT oe.id, oe.from_stop_id, oe.to_stop_id, oe.distance_m
                FROM operational_edge AS oe
                JOIN operational_line_stop AS s_from ON s_from.id = oe.from_stop_id
                JOIN operational_line_stop AS s_to ON s_to.id = oe.to_stop_id
                WHERE oe.direction = 'forward' {where_clause}
                """,
                sql_params,
            )
            for edge_id, s_from, s_to, dist_m in cur.fetchall():
                if s_from != s_to:
                    time_cost_s = (
                        dist_m / self.params.train_speed_mps
                    ) + self.params.train_stop_penalty_s
                    self._add_edge(s_from, s_to, time_cost_s, edge_id, "rail")

            station_coords = get_all_operational_stop_coordinates(
                connection, region=self.region
            )
        else:
            # 傳統 N02 station_connection 模式
            anchor_to_station: dict[int, int] = {}
            cur.execute("""
                SELECT a.id, sc.station_id
                FROM station_anchor AS a
                JOIN station_component AS sc ON sc.id = a.station_component_id
            """)
            for a_id, s_id in cur.fetchall():
                anchor_to_station[a_id] = s_id

            cur.execute("""
                SELECT sc.id, sc.from_anchor_id, sc.to_anchor_id, sc.distance_m, rl.provider_type_code
                FROM station_connection AS sc
                JOIN rail_line_component AS rlc ON rlc.id = sc.line_component_id
                JOIN rail_line AS rl ON rl.id = rlc.line_id
                WHERE sc.direction = 'forward'
            """)
            for conn_id, from_a, to_a, dist_m, provider_type_code in cur.fetchall():
                s_from = anchor_to_station.get(from_a)
                s_to = anchor_to_station.get(to_a)
                if s_from is not None and s_to is not None and s_from != s_to:
                    speed_mps = (
                        self.params.shinkansen_speed_mps
                        if provider_type_code == "1"
                        else self.params.train_speed_mps
                    )
                    time_cost_s = (dist_m / speed_mps) + self.params.train_stop_penalty_s
                    self._add_edge(s_from, s_to, time_cost_s, conn_id, "rail")

            station_coords = get_all_station_coordinates(connection)

        # 2. 載入並構建擴展轉乘邊
        stations_list = list(station_coords.items())
        n = len(stations_list)
        dist_limit = self.params.max_transfer_distance_m
        delta_deg = (dist_limit / 111000.0) * 1.5

        for i in range(n):
            s1, (lon1, lat1, _, _) = stations_list[i]
            for j in range(i + 1, n):
                s2, (lon2, lat2, _, _) = stations_list[j]
                if abs(lat1 - lat2) > delta_deg or abs(lon1 - lon2) > delta_deg:
                    continue

                dist_m = fast_distance_m(lon1, lat1, lon2, lat2)
                if dist_m <= dist_limit:
                    _, total_transfer_cost_s = calculate_transfer_cost(dist_m, params=self.params)
                    self._add_edge(s1, s2, total_transfer_cost_s, f"{s1}-{s2}", "transfer")
                    self._add_edge(s2, s1, total_transfer_cost_s, f"{s2}-{s1}", "transfer")


def find_shortest_path(
    graph: RailwayGraph,
    from_station_id: int,
    to_station_id: int,
    return_path: bool = True,
) -> dict[str, Any]:
    """
    計算任意兩個站之間的「時間成本最短路徑」。

    :param graph: 預先構建好的 RailwayGraph 物件
    :param from_station_id: 起點車站 ID
    :param to_station_id: 終點車站 ID
    :param return_path: 是否包含詳細路徑邊清單 (若為 False 僅計算數值，大幅節省記憶體與時間)
    :return: {
        "cost_s": 最短時間 (秒),
        "path": 詳細路徑邊表示列表 (既有邊為邊號數字，轉乘邊為 's1-s2' 字串),
        "stations": 經過的車站 ID 序列,
        "reachable": 是否可到達
    }
    """
    if from_station_id == to_station_id:
        return {
            "cost_s": 0.0,
            "path": [] if return_path else None,
            "stations": [from_station_id] if return_path else None,
            "reachable": True,
        }

    # Dijkstra 演算法
    dist: dict[int, float] = {from_station_id: 0.0}
    parent: dict[int, tuple[int, Any]] = {}  # v -> (u, edge_repr)
    heap: list[tuple[float, int]] = [(0.0, from_station_id)]

    while heap:
        cur_cost, u = heapq.heappop(heap)
        if u == to_station_id:
            break
        if cur_cost > dist.get(u, float("inf")):
            continue

        for v, edge_cost, edge_repr, _ in graph.adj.get(u, ()):
            new_cost = cur_cost + edge_cost
            if new_cost < dist.get(v, float("inf")):
                dist[v] = new_cost
                if return_path:
                    parent[v] = (u, edge_repr)
                heapq.heappush(heap, (new_cost, v))

    if to_station_id not in dist:
        return {
            "cost_s": float("inf"),
            "path": None,
            "stations": None,
            "reachable": False,
        }

    cost_result = round(dist[to_station_id], 2)
    if not return_path:
        return {
            "cost_s": cost_result,
            "path": None,
            "stations": None,
            "reachable": True,
        }

    # 重構路徑
    path_edges: list[Any] = []
    station_seq: list[int] = [to_station_id]
    curr = to_station_id
    while curr in parent:
        prev, edge_repr = parent[curr]
        path_edges.append(edge_repr)
        curr = prev
        station_seq.append(curr)

    path_edges.reverse()
    station_seq.reverse()

    return {
        "cost_s": cost_result,
        "path": path_edges,
        "stations": station_seq,
        "reachable": True,
    }
