"""centrality/calculate.py - ネットワーク構造中心性（Betweenness & Closeness）計算・更新モジュール"""

from __future__ import annotations

import heapq
import sqlite3
import sys
import time
from pathlib import Path

from shared.pathfinding import RailwayGraph

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "rail_network.sqlite"


def ensure_centrality_columns(connection: sqlite3.Connection) -> None:
    """station テーブルに betweenness_centrality および closeness_centrality カラムが存在することを確認する。"""
    cur = connection.cursor()
    columns = {row[1] for row in cur.execute("PRAGMA table_info(station)")}
    if "betweenness_centrality" not in columns:
        cur.execute("ALTER TABLE station ADD COLUMN betweenness_centrality REAL DEFAULT 0.0")
    if "closeness_centrality" not in columns:
        cur.execute("ALTER TABLE station ADD COLUMN closeness_centrality REAL DEFAULT 0.0")
    connection.commit()


def compute_centralities(graph: RailwayGraph) -> tuple[dict[int, float], dict[int, float]]:
    """
    Brandesのアルゴリズムを用いて加重ネットワークの媒介中心性（Betweenness Centrality）を計算し、
    最短経路探索と同時に正規化近接中心性（Closeness Centrality）を算出する。
    """
    nodes = list(graph.adj.keys())
    num_nodes = len(nodes)
    if num_nodes <= 2:
        return {n: 0.0 for n in nodes}, {n: 0.0 for n in nodes}

    betweenness: dict[int, float] = {node: 0.0 for node in nodes}
    closeness: dict[int, float] = {node: 0.0 for node in nodes}

    scale = 1.0 / ((num_nodes - 1) * (num_nodes - 2)) if num_nodes > 2 else 1.0

    print(f"全{num_nodes}駅のネットワーク構造中心性を計算開始...")
    start_time = time.time()
    report_interval = max(1, num_nodes // 10)

    for idx, s in enumerate(nodes):
        if idx > 0 and idx % report_interval == 0:
            elapsed = time.time() - start_time
            progress = (idx / num_nodes) * 100
            eta = (elapsed / idx) * (num_nodes - idx)
            print(f"  進捗: {progress:5.1f}% ({idx}/{num_nodes}) - 経過: {elapsed:.1f}s / 残り予測: {eta:.1f}s")

        # 1. Forward Dijkstra from source s
        dist: dict[int, float] = {s: 0.0}
        sigma: dict[int, float] = {s: 1.0}
        pred: dict[int, list[int]] = {s: []}
        order: list[int] = []
        heap: list[tuple[float, int]] = [(0.0, s)]

        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, float("inf")):
                continue
            order.append(u)
            for v, cost, _, _ in graph.adj.get(u, ()):
                alt = d + cost
                cur_d = dist.get(v, float("inf"))
                if alt < cur_d - 1e-9:
                    dist[v] = alt
                    sigma[v] = sigma[u]
                    pred[v] = [u]
                    heapq.heappush(heap, (alt, v))
                elif abs(alt - cur_d) <= 1e-9:
                    sigma[v] = sigma.get(v, 0.0) + sigma[u]
                    pred[v].append(u)

        # 近接中心性 (Wasserman & Faust 正規化)
        reachable_count = len(dist) - 1
        if reachable_count > 0:
            total_dist = sum(dist.values())
            if total_dist > 0:
                closeness[s] = (reachable_count / total_dist) * (reachable_count / (num_nodes - 1))

        # 2. Backward Brandes accumulation
        delta: dict[int, float] = {v: 0.0 for v in order}
        for w in reversed(order):
            for v in pred.get(w, ()):
                if sigma[w] > 0:
                    delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                betweenness[w] += delta[w]

    total_time = time.time() - start_time
    print(f"中心性計算完了（総所要時間: {total_time:.2f}秒）")

    # 媒介中心性の正規化
    for node in betweenness:
        betweenness[node] = round(betweenness[node] * scale, 8)
        closeness[node] = round(closeness[node], 8)

    return betweenness, closeness


def calculate_and_update_centrality(db_path: Path | str = DEFAULT_DB) -> None:
    """全駅の構造中心性を算出し、SQLiteデータベースへ保存する。"""
    db_file = Path(db_path).resolve()
    print(f"データベース接続: {db_file}")

    with sqlite3.connect(db_file) as con:
        ensure_centrality_columns(con)
        graph = RailwayGraph(con)
        betweenness, closeness = compute_centralities(graph)

        print("station テーブルへ中心性指標を書き込み中...")
        update_rows = [
            (betweenness.get(s_id, 0.0), closeness.get(s_id, 0.0), s_id)
            for s_id in graph.adj.keys()
        ]
        con.executemany("""
            UPDATE station
            SET betweenness_centrality = ?, closeness_centrality = ?
            WHERE id = ?
        """, update_rows)
        con.commit()
        print(f"{len(update_rows)}駅の中心性指標を正常に更新しました。")


if __name__ == "__main__":
    calculate_and_update_centrality()
