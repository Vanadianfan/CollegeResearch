"""centrality/calculate.py - ネットワーク構造中心性（Betweenness & Harmonic）計算・更新モジュール"""

from __future__ import annotations

import argparse
import heapq
import sqlite3
import time
from pathlib import Path

from shared.params import DEFAULT_PARAMS, DEFAULT_TIME_CUTOFF_S, NetworkParams
from shared.pathfinding import RailwayGraph

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "rail_network.sqlite"

_UNSET = object()


def ensure_centrality_columns(connection: sqlite3.Connection) -> None:
    """station の地域カラムおよび operational_line_stop の中心性・地域カラムが存在することを確認する。"""
    cur = connection.cursor()
    columns = {row[1] for row in cur.execute("PRAGMA table_info(station)")}
    if "region" not in columns:
        cur.execute("ALTER TABLE station ADD COLUMN region TEXT CHECK(region IN ('tokyo', 'kinki'))")

    has_op_stop = (
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'operational_line_stop'"
        ).fetchone()
        is not None
    )
    if not has_op_stop:
        raise RuntimeError(
            "operational_line_stop table is missing; centrality calculation requires the operational topology."
        )

    ols_columns = {row[1] for row in cur.execute("PRAGMA table_info(operational_line_stop)")}
    if "betweenness_centrality" not in ols_columns:
        cur.execute("ALTER TABLE operational_line_stop ADD COLUMN betweenness_centrality REAL DEFAULT 0.0")
    if "harmonic_centrality" not in ols_columns:
        cur.execute("ALTER TABLE operational_line_stop ADD COLUMN harmonic_centrality REAL DEFAULT 0.0")
    if "region" not in ols_columns:
        cur.execute("ALTER TABLE operational_line_stop ADD COLUMN region TEXT CHECK(region IN ('tokyo', 'kinki'))")
    if "regional_betweenness_centrality" not in ols_columns:
        cur.execute("ALTER TABLE operational_line_stop ADD COLUMN regional_betweenness_centrality REAL")
    if "regional_harmonic_centrality" not in ols_columns:
        cur.execute("ALTER TABLE operational_line_stop ADD COLUMN regional_harmonic_centrality REAL")
    connection.commit()


def compute_centralities(
    graph: RailwayGraph,
    time_cutoff_s: float | None | object = _UNSET,
) -> tuple[dict[int, float], dict[int, float]]:
    """
    Brandesのアルゴリズムを用いて加重ネットワークの媒介中心性（Betweenness Centrality）を計算し、
    最短経路探索と同時に正規化調和中心性（Harmonic Centrality）を算出する。
    time_cutoff_s が指定された場合（または graph.params.time_cutoff_s が設定されている場合）、
    その時間閾値（秒）以内の到達可能駅ペアのみを対象として計算を行う。
    """
    cutoff: float | None = (
        graph.params.time_cutoff_s if time_cutoff_s is _UNSET else time_cutoff_s  # type: ignore[assignment]
    )

    nodes = list(graph.adj.keys())
    num_nodes = len(nodes)
    if num_nodes <= 2:
        return {n: 0.0 for n in nodes}, {n: 0.0 for n in nodes}

    betweenness: dict[int, float] = {node: 0.0 for node in nodes}
    harmonic: dict[int, float] = {node: 0.0 for node in nodes}

    scale = 1.0 / ((num_nodes - 1) * (num_nodes - 2)) if num_nodes > 2 else 1.0
    cutoff_label = f"{cutoff:.0f}秒 ({cutoff / 60.0:.0f}分)" if cutoff is not None else "なし（全域）"

    print(f"全{num_nodes}駅のネットワーク構造中心性を計算開始（時間制限: {cutoff_label}）...")
    start_time = time.time()
    report_interval = max(1, num_nodes // 10)

    for idx, s in enumerate(nodes):
        if idx > 0 and idx % report_interval == 0:
            elapsed = time.time() - start_time
            progress = (idx / num_nodes) * 100
            eta = (elapsed / idx) * (num_nodes - idx)
            print(f"  進捗: {progress:5.1f}% ({idx}/{num_nodes}) - 経過: {elapsed:.1f}s / 残り予測: {eta:.1f}s")

        # 1. Forward Dijkstra from source s (with optional time cutoff)
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
                if cutoff is not None and alt > cutoff + 1e-9:
                    continue
                cur_d = dist.get(v, float("inf"))
                if alt < cur_d - 1e-9:
                    dist[v] = alt
                    sigma[v] = sigma[u]
                    pred[v] = [u]
                    heapq.heappush(heap, (alt, v))
                elif abs(alt - cur_d) <= 1e-9:
                    sigma[v] = sigma.get(v, 0.0) + sigma[u]
                    pred[v].append(u)

        # 調和中心性 (Harmonic Centrality: 1 / (N - 1) * sum_{v != s, d(s,v) <= cutoff} 1 / d(s, v))
        if num_nodes > 1:
            inv_sum = sum(
                1.0 / d_val for target, d_val in dist.items() if target != s and d_val > 0.0
            )
            harmonic[s] = inv_sum / (num_nodes - 1)

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

    # 媒介中心性の正規化および丸め処理
    for node in betweenness:
        betweenness[node] = round(betweenness[node] * scale, 8)
        harmonic[node] = round(harmonic[node], 8)

    return betweenness, harmonic


def _sync_station_regions_from_stops(connection: sqlite3.Connection) -> None:
    """operational_line_stop.region (直接アンカー優先、station_group フォールバック) を station.region へ反映する。"""
    has_ols_region = any(
        row[1] == "region"
        for row in connection.execute("PRAGMA table_info(operational_line_stop)")
    )
    if not has_ols_region:
        return
    connection.execute(
        """
        UPDATE station
        SET region = CASE
            WHEN EXISTS (
                SELECT 1
                FROM station_component AS sc
                JOIN station_anchor AS a ON a.station_component_id = sc.id
                JOIN operational_line_stop AS ols ON ols.station_anchor_id = a.id
                WHERE sc.station_id = station.id
            ) THEN (
                SELECT MIN(ols.region)
                FROM station_component AS sc
                JOIN station_anchor AS a ON a.station_component_id = sc.id
                JOIN operational_line_stop AS ols ON ols.station_anchor_id = a.id
                WHERE sc.station_id = station.id AND ols.region IS NOT NULL
            )
            ELSE (
                SELECT MIN(ols2.region)
                FROM station AS s2
                JOIN station_component AS sc2 ON sc2.station_id = s2.id
                JOIN station_anchor AS a2 ON a2.station_component_id = sc2.id
                JOIN operational_line_stop AS ols2 ON ols2.station_anchor_id = a2.id
                WHERE s2.group_id = station.group_id AND ols2.region IS NOT NULL
            )
        END
        """
    )


def _compute_and_write_regional(
    connection: sqlite3.Connection,
    params: NetworkParams,
    time_cutoff_s: float | None,
    reg: str,
) -> int:
    """指定地域 (tokyo / kinki) のみのサブグラフで中心性を算出し、regional_* カラムを更新する。"""
    print(f"地域別中心性を算出中 ({reg})...")
    reg_graph = RailwayGraph(connection, params=params, region=reg)
    reg_bc, reg_hc = compute_centralities(reg_graph, time_cutoff_s=time_cutoff_s)

    target_stop_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM operational_line_stop WHERE region = ?", (reg,)
        )
    ]
    stop_rows = [
        (reg_bc.get(stop_id, 0.0), reg_hc.get(stop_id, 0.0), stop_id)
        for stop_id in target_stop_ids
    ]
    connection.executemany(
        """
        UPDATE operational_line_stop
        SET regional_betweenness_centrality = ?, regional_harmonic_centrality = ?
        WHERE id = ?
        """,
        stop_rows,
    )
    return len(stop_rows)


def calculate_and_update_centrality(
    db_path: Path | str = DEFAULT_DB,
    time_cutoff_s: float | None = DEFAULT_TIME_CUTOFF_S,
    region: str | None = None,
) -> None:
    """運営停車駅 (operational_line_stop) の構造中心性（全国および地域別）を算出し、SQLiteへ保存する。"""
    db_file = Path(db_path).resolve()
    print(f"データベース接続: {db_file}")

    params = NetworkParams(time_cutoff_s=time_cutoff_s)
    region_filter = region if region in ("tokyo", "kinki") else None
    with sqlite3.connect(db_file) as con:
        ensure_centrality_columns(con)

        if region_filter is None:
            graph = RailwayGraph(con, params=params, region=None)
            betweenness, harmonic = compute_centralities(graph, time_cutoff_s=time_cutoff_s)
            print("operational_line_stop テーブルへ全国中心性指標を書き込み中...")
            all_stop_ids = [
                row[0] for row in con.execute("SELECT id FROM operational_line_stop")
            ]
            stop_rows = [
                (betweenness.get(stop_id, 0.0), harmonic.get(stop_id, 0.0), stop_id)
                for stop_id in all_stop_ids
            ]
            con.executemany(
                """
                UPDATE operational_line_stop
                SET betweenness_centrality = ?, harmonic_centrality = ?
                WHERE id = ?
                """,
                stop_rows,
            )
            print(f"{len(stop_rows)}運営停車駅の全国中心性指標を更新しました。")

        _sync_station_regions_from_stops(con)
        con.execute(
            """
            UPDATE operational_line_stop
            SET regional_betweenness_centrality = NULL, regional_harmonic_centrality = NULL
            WHERE region IS NULL
            """
        )

        target_regions = ("tokyo", "kinki") if region_filter is None else (region_filter,)
        for reg in target_regions:
            count = _compute_and_write_regional(con, params, time_cutoff_s, reg)
            print(f"  {reg}: {count}運営停車駅の地域別中心性を更新しました。")

        con.commit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="鉄道ネットワークの媒介中心性・調和中心性を算出し、SQLiteを更新します。"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB, help="対象SQLiteデータベースパス")
    parser.add_argument(
        "--time-cutoff",
        type=float,
        default=DEFAULT_TIME_CUTOFF_S,
        help="所要時間上限（秒）。デフォルト: 7200（120分）",
    )
    parser.add_argument(
        "--unbounded",
        action="store_true",
        help="時間制限を適用せず全域で計算する",
    )
    parser.add_argument(
        "--region",
        choices=["all", "tokyo", "kinki"],
        default="all",
        help="計算対象地域（all / tokyo / kinki）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cutoff_val = None if args.unbounded else args.time_cutoff
    calculate_and_update_centrality(
        db_path=args.database,
        time_cutoff_s=cutoff_val,
        region=None if args.region == "all" else args.region,
    )
