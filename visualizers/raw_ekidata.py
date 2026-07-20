from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import font_manager
from matplotlib.collections import LineCollection

from rail_data.paths import RAW_DATA_ROOT

JAPANESE_FONT_PATH = Path("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc")
JAPANESE_FONT = font_manager.FontProperties(fname=JAPANESE_FONT_PATH)
FALLBACK_COLORS = plt.colormaps["tab20"].colors

REQUIRED_COLUMNS = {
    "station": {
        "station_cd",
        "station_name",
        "line_cd",
        "address",
        "lon",
        "lat",
        "e_status",
    },
    "line": {"line_cd", "line_name", "line_color_c", "e_status"},
    "join": {"line_cd", "station_cd1", "station_cd2"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="駅データ.jp の路線と駅をインタラクティブ表示します。"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="station/line/join CSV があるディレクトリ",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="GUI を開かず画像として保存するパス",
    )
    return parser.parse_args()


def locate_data_dir(explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        data_dir = explicit_path.expanduser().resolve()
        if not data_dir.is_dir():
            raise FileNotFoundError(f"データディレクトリが見つかりません: {data_dir}")
        return data_dir

    candidates = [
        path
        for path in RAW_DATA_ROOT.iterdir()
        if path.is_dir()
        and any(path.glob("station*free.csv"))
        and any(path.glob("line*free.csv"))
        and any(path.glob("join*.csv"))
    ]
    if not candidates:
        raise FileNotFoundError(
            "station*free.csv、line*free.csv、join*.csv を含む"
            "駅データ.jp ディレクトリが見つかりません。"
        )
    return candidates[0]


def latest_csv(data_dir: Path, pattern: str) -> Path:
    paths = sorted(data_dir.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"{data_dir} に {pattern} が見つかりません。")
    return paths[-1]


def require_columns(frame: pd.DataFrame, kind: str, path: Path) -> None:
    missing = REQUIRED_COLUMNS[kind] - set(frame.columns)
    if missing:
        names = ", ".join(sorted(missing))
        raise ValueError(f"{path.name} に必要な列がありません: {names}")


def load_data(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    station_path = latest_csv(data_dir, "station*free.csv")
    line_path = latest_csv(data_dir, "line*free.csv")
    join_path = latest_csv(data_dir, "join*.csv")

    stations = pd.read_csv(station_path, encoding="utf-8")
    lines = pd.read_csv(line_path, encoding="utf-8")
    joins = pd.read_csv(join_path, encoding="utf-8")

    require_columns(stations, "station", station_path)
    require_columns(lines, "line", line_path)
    require_columns(joins, "join", join_path)

    # 無料データには廃止駅・廃止路線も含まれるため、運用中のみを表示する。
    stations = stations.loc[stations["e_status"].eq(0)].copy()
    stations = stations.dropna(subset=["lon", "lat"])
    lines = lines.loc[lines["e_status"].eq(0)].copy()

    active_line_codes = set(lines["line_cd"])
    joins = joins.loc[joins["line_cd"].isin(active_line_codes)].copy()

    coordinates = stations.set_index("station_cd")[["lon", "lat"]]
    joins = joins.join(
        coordinates.rename(columns={"lon": "lon1", "lat": "lat1"}),
        on="station_cd1",
    )
    joins = joins.join(
        coordinates.rename(columns={"lon": "lon2", "lat": "lat2"}),
        on="station_cd2",
    )
    joins = joins.dropna(subset=["lon1", "lat1", "lon2", "lat2"])

    print(
        f"読込: {station_path.name} / {line_path.name} / {join_path.name}\n"
        f"運用中: {len(lines):,} 路線, {len(stations):,} 駅, "
        f"{len(joins):,} 区間"
    )
    return stations, lines, joins


def color_for_line(color_code: object, line_cd: int) -> object:
    if pd.notna(color_code):
        code = str(color_code).strip().lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{6}", code):
            return f"#{code}"
    return FALLBACK_COLORS[line_cd % len(FALLBACK_COLORS)]


def zoom_at_cursor(event) -> None:
    """触控パッドの2本指スクロールで、ポインタ位置を中心に拡大縮小する。"""
    ax = event.inaxes
    if ax is None or event.xdata is None or event.ydata is None:
        return

    step = max(-5.0, min(5.0, event.step))
    if step == 0:
        return

    scale = 1.15**step
    x_min, x_max = ax.get_xlim()
    y_min, y_max = ax.get_ylim()
    new_width = (x_max - x_min) / scale
    new_height = (y_max - y_min) / scale
    x_ratio = (event.xdata - x_min) / (x_max - x_min)
    y_ratio = (event.ydata - y_min) / (y_max - y_min)

    ax.set_xlim(
        event.xdata - new_width * x_ratio,
        event.xdata + new_width * (1 - x_ratio),
    )
    ax.set_ylim(
        event.ydata - new_height * y_ratio,
        event.ydata + new_height * (1 - y_ratio),
    )
    event.canvas.draw_idle()


def create_visualization(
    stations: pd.DataFrame,
    lines: pd.DataFrame,
    joins: pd.DataFrame,
):
    fig, ax = plt.subplots(figsize=(11, 10))

    line_colors = {
        int(row.line_cd): color_for_line(row.line_color_c, int(row.line_cd))
        for row in lines.itertuples()
    }
    segments = [
        [(row.lon1, row.lat1), (row.lon2, row.lat2)] for row in joins.itertuples()
    ]
    segment_colors = [line_colors[int(row.line_cd)] for row in joins.itertuples()]
    ax.add_collection(
        LineCollection(
            segments,
            colors=segment_colors,
            linewidths=0.75,
            alpha=0.72,
            zorder=1,
        )
    )

    station_records = stations.reset_index(drop=True)
    station_points = ax.scatter(
        station_records["lon"],
        station_records["lat"],
        s=5,
        color="#c9342f",
        edgecolors="none",
        picker=5,
        zorder=2,
    )

    line_names = lines.set_index("line_cd")["line_name"].to_dict()
    annotation = ax.annotate(
        "",
        xy=(0, 0),
        xytext=(10, 10),
        textcoords="offset points",
        fontproperties=JAPANESE_FONT,
        bbox={"boxstyle": "round,pad=0.35", "fc": "white", "alpha": 0.92},
        arrowprops={"arrowstyle": "->"},
        zorder=3,
    )
    annotation.set_visible(False)

    def show_station(event) -> None:
        if event.artist is not station_points or not event.ind.size:
            return
        station = station_records.iloc[int(event.ind[0])]
        line_name = line_names.get(station["line_cd"], "")
        address = "" if pd.isna(station["address"]) else str(station["address"])
        annotation.xy = (station["lon"], station["lat"])
        annotation.set_text(
            f"{station['station_name']}  [{int(station['station_cd'])}]\n"
            f"{line_name}\n{address}"
        )
        annotation.set_visible(True)
        event.canvas.draw_idle()

    mean_latitude = float(station_records["lat"].mean())
    ax.set_aspect(1 / math.cos(math.radians(mean_latitude)))
    ax.autoscale_view()
    ax.margins(0.02)
    ax.set_xlabel("経度", fontproperties=JAPANESE_FONT)
    ax.set_ylabel("緯度", fontproperties=JAPANESE_FONT)
    ax.set_title(
        "駅データ.jp 全国鉄道路線・駅",
        fontproperties=JAPANESE_FONT,
    )
    fig.text(
        0.5,
        0.015,
        "2本指上下スクロール: 拡大縮小  /  駅をクリック: 詳細表示",
        ha="center",
        fontproperties=JAPANESE_FONT,
    )
    fig.canvas.mpl_connect("scroll_event", zoom_at_cursor)
    fig.canvas.mpl_connect("pick_event", show_station)
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    return fig


def main() -> None:
    args = parse_args()
    data_dir = locate_data_dir(args.data_dir)
    stations, lines, joins = load_data(data_dir)
    fig = create_visualization(stations, lines, joins)

    if args.output is not None:
        fig.savefig(args.output, dpi=180, bbox_inches="tight")
        print(f"保存: {args.output}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
