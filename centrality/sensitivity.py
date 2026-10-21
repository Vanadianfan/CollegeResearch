"""centrality/sensitivity.py - 鐵路網參數敏感度分析核心模組（支援無時間截斷與有時間截斷模式）"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

import pandas as pd

from shared.params import DEFAULT_TIME_CUTOFF_S, NetworkParams
from shared.pathfinding import RailwayGraph
from .calculate import compute_centralities

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RAIL_DB = PROJECT_ROOT / "rail_network.sqlite"
UNBOUNDED_OUTPUT_DB = PROJECT_ROOT / "network_sensitivity_unbounded.sqlite"
CUTOFF_OUTPUT_DB = PROJECT_ROOT / "network_sensitivity_cutoff.sqlite"


def build_scenarios(time_cutoff_s: float | None) -> list[NetworkParams]:
    """
    依指定之時間截斷條件（None 代表無截斷全域，或秒數代表時間截斷），
    產生 OAT 單變量攝動之 7 組測試劇本。
    """
    cutoff_desc = (
        f"時間制限{int(time_cutoff_s // 60)}分"
        if time_cutoff_s is not None
        else "時間制限なし"
    )
    return [
        NetworkParams(
            name="baseline",
            description=f"基準モデル (600m / 180s / 30s / {cutoff_desc})",
            max_transfer_distance_m=600.0,
            fixed_transfer_penalty_s=180.0,
            train_stop_penalty_s=30.0,
            time_cutoff_s=time_cutoff_s,
        ),
        NetworkParams(
            name="trans_300m",
            description=f"保守的乗換距離 (300m / 180s / 30s / {cutoff_desc})",
            max_transfer_distance_m=300.0,
            fixed_transfer_penalty_s=180.0,
            train_stop_penalty_s=30.0,
            time_cutoff_s=time_cutoff_s,
        ),
        NetworkParams(
            name="trans_800m",
            description=f"緩和的乗換距離 (800m / 180s / 30s / {cutoff_desc})",
            max_transfer_distance_m=800.0,
            fixed_transfer_penalty_s=180.0,
            train_stop_penalty_s=30.0,
            time_cutoff_s=time_cutoff_s,
        ),
        NetworkParams(
            name="penalty_90s",
            description=f"短縮乗換ペナルティ (600m / 90s / 30s / {cutoff_desc})",
            max_transfer_distance_m=600.0,
            fixed_transfer_penalty_s=90.0,
            train_stop_penalty_s=30.0,
            time_cutoff_s=time_cutoff_s,
        ),
        NetworkParams(
            name="penalty_300s",
            description=f"延長乗換ペナルティ (600m / 300s / 30s / {cutoff_desc})",
            max_transfer_distance_m=600.0,
            fixed_transfer_penalty_s=300.0,
            train_stop_penalty_s=30.0,
            time_cutoff_s=time_cutoff_s,
        ),
        NetworkParams(
            name="stop_15s",
            description=f"短縮停車ペナルティ (600m / 180s / 15s / {cutoff_desc})",
            max_transfer_distance_m=600.0,
            fixed_transfer_penalty_s=180.0,
            train_stop_penalty_s=15.0,
            time_cutoff_s=time_cutoff_s,
        ),
        NetworkParams(
            name="stop_60s",
            description=f"延長停車ペナルティ (600m / 180s / 60s / {cutoff_desc})",
            max_transfer_distance_m=600.0,
            fixed_transfer_penalty_s=180.0,
            train_stop_penalty_s=60.0,
            time_cutoff_s=time_cutoff_s,
        ),
    ]


UNBOUNDED_SCENARIOS = build_scenarios(time_cutoff_s=None)
CUTOFF_SCENARIOS = build_scenarios(time_cutoff_s=DEFAULT_TIME_CUTOFF_S)
STANDARD_SCENARIOS = CUTOFF_SCENARIOS


def ensure_sensitivity_schema(out_con: sqlite3.Connection) -> None:
    """
    建立感度分析 SQLite 所需資料表結構。
    network_sensitivity_unbounded.sqlite 與 network_sensitivity_cutoff.sqlite 採用完全一致之內部結構。
    """
    cur = out_con.cursor()
    scen_cols = {row[1] for row in cur.execute("PRAGMA table_info(scenario)")}
    expected_scen_cols = {
        "scenario_id",
        "name",
        "description",
        "max_transfer_distance_m",
        "fixed_transfer_penalty_s",
        "train_stop_penalty_s",
        "time_cutoff_s",
        "created_at",
    }
    if scen_cols and scen_cols != expected_scen_cols:
        cur.execute("DROP TABLE IF EXISTS station_centrality_result")
        cur.execute("DROP TABLE IF EXISTS scenario")

    res_cols = {row[1] for row in cur.execute("PRAGMA table_info(station_centrality_result)")}
    expected_res_cols = {
        "scenario_id",
        "station_id",
        "station_name",
        "line_name",
        "betweenness_centrality",
        "harmonic_centrality",
        "rank_bc",
        "rank_hc",
    }
    if res_cols and res_cols != expected_res_cols:
        cur.execute("DROP TABLE IF EXISTS station_centrality_result")

    out_con.executescript("""
        CREATE TABLE IF NOT EXISTS scenario (
            scenario_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            max_transfer_distance_m REAL NOT NULL,
            fixed_transfer_penalty_s REAL NOT NULL,
            train_stop_penalty_s REAL NOT NULL,
            time_cutoff_s REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS station_centrality_result (
            scenario_id TEXT NOT NULL REFERENCES scenario(scenario_id),
            station_id INTEGER NOT NULL,
            station_name TEXT NOT NULL,
            line_name TEXT NOT NULL,
            betweenness_centrality REAL NOT NULL,
            harmonic_centrality REAL NOT NULL,
            rank_bc INTEGER NOT NULL,
            rank_hc INTEGER NOT NULL,
            PRIMARY KEY (scenario_id, station_id)
        );

        CREATE INDEX IF NOT EXISTS idx_station_centrality_scenario
            ON station_centrality_result(scenario_id);
        CREATE INDEX IF NOT EXISTS idx_station_centrality_station
            ON station_centrality_result(station_id);

        CREATE TABLE IF NOT EXISTS correlation_matrix (
            metric TEXT NOT NULL,
            scenario_a TEXT NOT NULL,
            scenario_b TEXT NOT NULL,
            rho REAL NOT NULL,
            PRIMARY KEY (metric, scenario_a, scenario_b)
        );
    """)
    out_con.commit()


def get_station_info(rail_con: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """取得站點名稱與路線名稱字典: station_id -> (station_name, line_name)"""
    cur = rail_con.cursor()
    cur.execute("""
        SELECT s.id, s.name, l.name
        FROM station AS s
        JOIN rail_line AS l ON l.id = s.line_id
    """)
    return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def upsert_scenario_metadata(out_con: sqlite3.Connection, scenario: NetworkParams) -> None:
    """寫入或更新 scenario 元資料（包含三項 OAT 變動參數與時間截斷設定 time_cutoff_s）"""
    out_con.execute("""
        INSERT OR REPLACE INTO scenario (
            scenario_id, name, description,
            max_transfer_distance_m, fixed_transfer_penalty_s, train_stop_penalty_s, time_cutoff_s
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        scenario.name,
        scenario.name,
        scenario.description,
        scenario.max_transfer_distance_m,
        scenario.fixed_transfer_penalty_s,
        scenario.train_stop_penalty_s,
        scenario.time_cutoff_s,
    ))
    out_con.commit()


def run_scenario(
    scenario: NetworkParams,
    rail_con: sqlite3.Connection,
    out_con: sqlite3.Connection,
    station_info: dict[int, tuple[str, str]],
    force: bool = False,
) -> None:
    """執行單一情境之路網建置與中心性計算（媒介中心性 & 調和中心性），並存入獨立輸出庫"""
    upsert_scenario_metadata(out_con, scenario)

    existing = out_con.execute(
        "SELECT 1 FROM station_centrality_result WHERE scenario_id = ? LIMIT 1",
        (scenario.name,),
    ).fetchone()
    if existing and not force:
        print(f"[{scenario.name}] 已存在於結果庫中，跳過計算（使用 --force 可重新計算）。")
        return

    cutoff_str = f"{scenario.time_cutoff_s:.0f}s" if scenario.time_cutoff_s is not None else "None (全域)"
    print(f"\n==================================================")
    print(f"情境執行: {scenario.name} - {scenario.description}")
    print(
        f"  轉乘距離上限: {scenario.max_transfer_distance_m}m, "
        f"固定轉乘懲罰: {scenario.fixed_transfer_penalty_s}s, "
        f"停站懲罰: {scenario.train_stop_penalty_s}s, "
        f"時間截斷: {cutoff_str}"
    )
    print(f"==================================================")

    t0 = time.time()
    graph = RailwayGraph(rail_con, params=scenario)
    print(f"路網建立完成 ({time.time() - t0:.2f}s) - 節點數: {len(graph.adj)}, 邊數: {sum(len(v) for v in graph.adj.values())}")

    bc_dict, hc_dict = compute_centralities(graph, time_cutoff_s=scenario.time_cutoff_s)

    # 轉為 DataFrame 計算排名
    records = []
    for s_id in graph.adj.keys():
        s_name, l_name = station_info.get(s_id, ("不明", "不明"))
        records.append({
            "station_id": s_id,
            "station_name": s_name,
            "line_name": l_name,
            "betweenness_centrality": bc_dict.get(s_id, 0.0),
            "harmonic_centrality": hc_dict.get(s_id, 0.0),
        })

    df = pd.DataFrame(records)
    df["rank_bc"] = df["betweenness_centrality"].rank(ascending=False, method="min").astype(int)
    df["rank_hc"] = df["harmonic_centrality"].rank(ascending=False, method="min").astype(int)

    # 批次寫入 station_centrality_result
    out_con.execute("DELETE FROM station_centrality_result WHERE scenario_id = ?", (scenario.name,))
    insert_rows = [
        (
            scenario.name,
            row.station_id,
            row.station_name,
            row.line_name,
            row.betweenness_centrality,
            row.harmonic_centrality,
            row.rank_bc,
            row.rank_hc,
        )
        for row in df.itertuples(index=False)
    ]
    out_con.executemany("""
        INSERT OR REPLACE INTO station_centrality_result (
            scenario_id, station_id, station_name, line_name,
            betweenness_centrality, harmonic_centrality, rank_bc, rank_hc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, insert_rows)
    out_con.commit()
    print(f"[{scenario.name}] 成功儲存 {len(insert_rows)} 筆站點中心性數值至結果資料庫。")


def evaluate_and_report(out_con: sqlite3.Connection) -> tuple[pd.DataFrame, list[dict]]:
    """計算各攝動情境與 baseline 之 Spearman 相關係數與 Top-K Jaccard 相似度並產出摘要報告"""
    df_all = pd.read_sql_query(
        "SELECT scenario_id, station_id, station_name, line_name, rank_bc, rank_hc FROM station_centrality_result",
        out_con,
    )
    if df_all.empty:
        print("查無結果數據，無法產出報告。")
        return pd.DataFrame(), []

    std_order = [sc.name for sc in STANDARD_SCENARIOS]
    present_scenarios = df_all["scenario_id"].unique().tolist()
    scenarios = [s for s in std_order if s in present_scenarios] + [
        s for s in present_scenarios if s not in std_order
    ]

    metrics = [
        ("betweenness_centrality", "rank_bc", "Betweenness Centrality (媒介中心性)"),
        ("harmonic_centrality", "rank_hc", "Harmonic Centrality (調和中心性)"),
    ]

    # 僅比較各攝動情境與 baseline 之相關係數（符合 OAT 單變量攝動設計）
    out_con.execute("DELETE FROM correlation_matrix")
    corr_rows = []
    spearman_matrices: dict[str, pd.DataFrame] = {}

    for metric_name, rank_col, label in metrics:
        pivot_rank = df_all.pivot(index="station_id", columns="scenario_id", values=rank_col)[scenarios]
        spearman_mat = pivot_rank.corr(method="spearman")
        spearman_matrices[metric_name] = spearman_mat

        if "baseline" in scenarios:
            print("\n==================================================")
            print(f"=== Spearman Rank Correlation vs. baseline ({label}) ===")
            print("==================================================")
            for s_b in scenarios:
                if s_b == "baseline":
                    continue
                rho = spearman_mat.loc["baseline", s_b]
                corr_rows.append((metric_name, "baseline", s_b, round(float(rho), 6)))
                print(f"  baseline vs. {s_b:15s}: rho = {rho:.4f}")

    out_con.executemany("""
        INSERT INTO correlation_matrix (metric, scenario_a, scenario_b, rho)
        VALUES (?, ?, ?, ?)
    """, corr_rows)
    out_con.commit()

    # Jaccard 相似度 (Top 50)
    top_k = 50
    jaccard_results = []
    if "baseline" in scenarios:
        for metric_name, rank_col, label in metrics:
            baseline_top = set(
                df_all[(df_all["scenario_id"] == "baseline") & (df_all[rank_col] <= top_k)]["station_id"]
            )
            print(f"\n=== Top-{top_k} Jaccard 類似度 - {label} (與 baseline 對比) ===")
            for sc in scenarios:
                if sc == "baseline":
                    continue
                sc_top = set(
                    df_all[(df_all["scenario_id"] == sc) & (df_all[rank_col] <= top_k)]["station_id"]
                )
                inter = len(baseline_top & sc_top)
                union = len(baseline_top | sc_top)
                j_score = inter / union if union > 0 else 1.0
                print(f"  {sc:15s}: Jaccard = {j_score:.4f} (重合站數: {inter}/{top_k})")
                jaccard_results.append({
                    "metric": metric_name,
                    "scenario": sc,
                    "jaccard": round(j_score, 4),
                    "overlap_count": inter,
                })

    # 最大順位變動車站（baseline vs 關鍵攝動情境）
    for target_sc in ["trans_300m", "trans_800m", "penalty_90s", "penalty_300s", "stop_15s", "stop_60s"]:
        if "baseline" in scenarios and target_sc in scenarios:
            sub_df = df_all[df_all["scenario_id"].isin(["baseline", target_sc])]
            p_base = sub_df[sub_df["scenario_id"] == "baseline"].set_index("station_id")
            p_targ = sub_df[sub_df["scenario_id"] == target_sc].set_index("station_id")
            common_ids = p_base.index.intersection(p_targ.index)

            for metric_name, rank_col, label in metrics:
                shift_df = pd.DataFrame({
                    "station_name": p_base.loc[common_ids, "station_name"],
                    "line_name": p_base.loc[common_ids, "line_name"],
                    "rank_base": p_base.loc[common_ids, rank_col],
                    "rank_targ": p_targ.loc[common_ids, rank_col],
                })
                shift_df["rank_diff"] = shift_df["rank_base"] - shift_df["rank_targ"]
                shift_df["abs_diff"] = shift_df["rank_diff"].abs()
                top_shifts = shift_df.sort_values(by="abs_diff", ascending=False).head(5)

                print(f"\n=== 最大順位變動車站 Top 5 - {label} (Baseline vs {target_sc}) ===")
                for s_id, row in top_shifts.iterrows():
                    name = str(row["station_name"])
                    line = str(row["line_name"])
                    r_base = int(row["rank_base"])
                    r_targ = int(row["rank_targ"])
                    diff = int(row["rank_diff"])
                    print(f"  ID:{s_id:5d} {name:8s} ({line:15s}) Baseline:#{r_base:<5d} -> {target_sc}:#{r_targ:<5d} (變動: {diff:+d})")

    return spearman_matrices.get("betweenness_centrality", pd.DataFrame()), jaccard_results


def run_sensitivity_pipeline(
    *,
    rail_db_path: Path,
    out_db_path: Path,
    time_cutoff_s: float | None,
    scenario_names: list[str] | None = None,
    force: bool = False,
) -> int:
    """執行指定時間截斷條件下之完整 OAT 感度分析流程"""
    rail_db = rail_db_path.resolve()
    out_db = out_db_path.resolve()

    if not rail_db.exists():
        print(f"[ERROR] 找不到鐵路資料庫: {rail_db}")
        return 1

    all_scenarios = build_scenarios(time_cutoff_s=time_cutoff_s)
    selected_scenarios = all_scenarios
    if scenario_names:
        name_set = set(scenario_names)
        selected_scenarios = [sc for sc in all_scenarios if sc.name in name_set]
        if not selected_scenarios:
            print(f"[ERROR] 指定的情境名稱無效，可用名稱: {[sc.name for sc in all_scenarios]}")
            return 1

    cutoff_label = f"{time_cutoff_s:.0f}秒 ({time_cutoff_s / 60.0:.0f}分)" if time_cutoff_s is not None else "なし（全域）"
    print(f"讀取基礎路網: {rail_db}")
    print(f"輸出目標庫:   {out_db}")
    print(f"時間截斷設定: {cutoff_label}")

    with sqlite3.connect(rail_db) as rail_con, sqlite3.connect(out_db) as out_con:
        ensure_sensitivity_schema(out_con)
        station_info = get_station_info(rail_con)

        for sc in selected_scenarios:
            run_scenario(sc, rail_con, out_con, station_info, force=force)

        evaluate_and_report(out_con)

    print(f"\n敏感度分析計算完成！獨立資料庫已保存於: {out_db}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="執行鐵路網參數敏感度分析並儲存至獨立 SQLite")
    parser.add_argument("--database", type=Path, default=DEFAULT_RAIL_DB, help="原始路網資料庫路徑")
    parser.add_argument(
        "--mode",
        choices=("cutoff", "unbounded"),
        default="cutoff",
        help="分析模式: cutoff (預設 120 分時間截斷) 或 unbounded (無時間截斷)",
    )
    parser.add_argument(
        "--time-cutoff",
        type=float,
        default=DEFAULT_TIME_CUTOFF_S,
        help="cutoff 模式下之時間截斷閾值（秒），預設 7200（120分）",
    )
    parser.add_argument("--output", type=Path, default=None, help="敏感度分析結果資料庫路徑（預設依 mode 自動決定）")
    parser.add_argument("--scenarios", nargs="+", default=None, help="指定執行的情境名稱（預設執行全部7種）")
    parser.add_argument("--force", action="store_true", help="強制重新計算已存在的情境")
    args = parser.parse_args()

    if args.mode == "unbounded":
        cutoff_val = None
        out_db = args.output or UNBOUNDED_OUTPUT_DB
    else:
        cutoff_val = args.time_cutoff
        out_db = args.output or CUTOFF_OUTPUT_DB

    return run_sensitivity_pipeline(
        rail_db_path=args.database,
        out_db_path=out_db,
        time_cutoff_s=cutoff_val,
        scenario_names=args.scenarios,
        force=args.force,
    )


if __name__ == "__main__":
    raise SystemExit(main())
