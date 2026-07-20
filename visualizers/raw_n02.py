import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib import font_manager

from rail_data.paths import N02_DATASET_ID, N02_UTF8_ROOT


# DejaVu Sans 不含完整 CJK 字形，改用 macOS 內建繁中字型。
CJK_FONT = font_manager.FontProperties(fname="/System/Library/Fonts/STHeiti Medium.ttc")


def zoom_at_cursor(event):
    """使用觸控板雙指捲動或滑鼠滾輪，以游標位置為中心縮放。"""
    ax = event.inaxes
    if ax is None or event.xdata is None or event.ydata is None:
        return

    # macOS 觸控板可能回報小數或較大的捲動量，限制單次縮放幅度。
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


def main():
    railways = gpd.read_file(
        N02_UTF8_ROOT / f"{N02_DATASET_ID}_RailroadSection.geojson"
    )
    stations = gpd.read_file(
        N02_UTF8_ROOT / f"{N02_DATASET_ID}_Station.geojson"
    )

    print(stations.columns)
    print(stations.head())

    ax = railways.plot(figsize=(10, 10), linewidth=0.5)
    stations.plot(ax=ax, color="red", linewidth=1)
    ax.set_title("觸控板雙指上下滑動或滾輪縮放", fontproperties=CJK_FONT)
    ax.figure.canvas.mpl_connect("scroll_event", zoom_at_cursor)
    plt.show()


if __name__ == "__main__":
    main()
