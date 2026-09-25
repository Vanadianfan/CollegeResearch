"""detour/calculate.py - 全駅間最短時間経路計算・保存モジュール（迂回コスト前処理）"""

from __future__ import annotations

import argparse
import heapq
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from shared.pathfinding import RailwayGraph

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "rail_network.sqlite"


def ensure_shortest_path_table(connection: sqlite3.Connection) -> None:
    """全駅間の最短経路コストを保持する station_shortest_path テーブルを生成する。"""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS station_shortest_path (
            from_station_id INTEGER NOT NULL REFERENCES station(id),
            to_station_id INTEGER NOT NULL REFERENCES station(id),
            cost_s REAL NOT NULL,
            PRIMARY KEY (from_station_id, to_station_id)
        );
        CREATE INDEX IF NOT EXISTS idx_station_shortest_path_from
            ON station_shortest_path(from_station_id);
        CREATE INDEX IF NOT EXISTS idx_station_shortest_path_to
            ON station_shortest_path(to_station_id);
    """)
    connection.commit()


def calculate_all_shortest_paths(
    db_path: Path | str = DEFAULT_DB,
    source_station_ids: Iterable[int] | None = None,
    limit: int | None = None,
    batch_size: int = 50000,
) -> int:
    """
    全駅間の最短時間経路コストを算出し、station_shortest_path テーブルへ一括保存する。
    """
    db_file = Path(db_path).resolve()
    print(f"データベース接続: {db_file}")

    with sqlite3.connect(db_file) as con:
        ensure_shortest_path_table(con)
        print("RailwayGraph（経路探索グラフ）構築中...")
        graph = RailwayGraph(con)

        all_nodes = list(graph.adj.keys())
        if source_station_ids is not None:
            sources = [s for s in source_station_ids if s in graph.adj]
        else:
            sources = all_nodes

        if limit is not None:
            sources = sources[:limit]

        total_sources = len(sources)
        print(f"全{total_sources}駅からの最短時間経路コストを算出します...")

        inserted_count = 0
        batch: list[tuple[int, int, float]] = []
        start_time = time.time()
        report_interval = max(1, total_sources // 10)

        for idx, s in enumerate(sources):
            if idx > 0 and idx % report_interval == 0:
                elapsed = time.time() - start_time
                progress = (idx / total_sources) * 100
                eta = (elapsed / idx) * (total_sources - idx)
                print(f"  進捗: {progress:5.1f}% ({idx}/{total_sources}) - 経過: {elapsed:.1f}s / 残り予測: {eta:.1f}s（一時保存: {len(batch) + inserted_count}件）")

            # Single-source Dijkstra
            dist: dict[int, float] = {s: 0.0}
            heap: list[tuple[float, int]] = [(0.0, s)]
            while heap:
                d, u = heapq.heappop(heap)
                if d > dist.get(u, float("inf")):
                    continue
                for v, cost, _, _ in graph.adj.get(u, ()):
                    alt = d + cost
                    if alt < dist.get(v, float("inf")):
                        dist[v] = alt
                        heapq.heappush(heap, (alt, v))

            for target, cost in dist.items():
                batch.append((s, target, round(cost, 2)))

            if len(batch) >= batch_size:
                con.executemany("""
                    INSERT OR REPLACE INTO station_shortest_path (from_station_id, to_station_id, cost_s)
                    VALUES (?, ?, ?)
                """, batch)
                con.commit()
                inserted_count += len(batch)
                batch.clear()

        if batch:
            con.executemany("""
                INSERT OR REPLACE INTO station_shortest_path (from_station_id, to_station_id, cost_s)
                VALUES (?, ?, ?)
            """, batch)
            con.commit()
            inserted_count += len(batch)
            batch.clear()

        total_time = time.time() - start_time
        print(f"最短経路コストの計算と保存が完了しました（全{inserted_count}件、所要時間: {total_time:.2f}秒）")
        return inserted_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="全駅間の最短時間経路コストを計算し、SQLiteに保存します。")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB, help="SQLite データベースパス")
    parser.add_argument("--limit", type=int, default=None, help="計算対象とする出発駅数の上限（テスト用）")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    calculate_all_shortest_paths(db_path=args.database, limit=args.limit)
