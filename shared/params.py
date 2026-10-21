"""shared/params.py - 鐵路網拓撲與時空間阻抗模型之參數定義"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TIME_CUTOFF_S: float = 7200.0  # 預設時間截斷閾值: 120分鐘 (7200秒)


@dataclass(frozen=True)
class NetworkParams:
    """鐵路網拓撲與時空間阻抗參數設定"""
    name: str = "baseline"
    description: str = "基準モデル（600m徒歩・180秒固定乗換ペナルティ）"
    max_transfer_distance_m: float = 600.0                   # 轉乘上限距離 (m)
    fixed_transfer_penalty_s: float = 180.0                  # 固定轉乘懲罰時間 (s)
    circuity_factor: float = 1.2                             # 站內/街區繞行係數
    walk_speed_mps: float = 80.0 / 60.0                      # 步行速度 (80 m/min ~= 1.333 m/s)
    train_speed_mps: float = 50.0 * 1000.0 / 3600.0          # 一般列車巡航速度 (50 km/h ~= 13.889 m/s)
    shinkansen_speed_mps: float = 200.0 * 1000.0 / 3600.0    # 新幹線巡航速度 (200 km/h ~= 55.556 m/s)
    train_stop_penalty_s: float = 30.0                       # 停站與加減速懲罰 (s)
    time_cutoff_s: float | None = DEFAULT_TIME_CUTOFF_S      # 時間截斷閾值 (s)，None 表示無時間截斷


DEFAULT_PARAMS = NetworkParams()
