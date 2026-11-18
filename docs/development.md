# 鐵路網研究工具

```bash
# 建立 .venv、安裝依賴並下載生資料
python3 setup.py

# 啟用虛擬環境
source .venv/bin/activate

# 建立 SQLite 鐵路網、計算中心性（operational_line_stop 全國／地域別）、檢驗並可視化
python -m rail_data.build.main
python -m centrality.calculate
python -m visualizers.network_db --check-only
python -m visualizers.network_db --open

# OAT 參數感度分析全組合（{全國, tokyo, kinki} × {cutoff, unbounded}，輸出至 network_sensitivity/）
python -m centrality.sensitivity --force

# 指定區域或截斷模式（各參數皆為選用；--region all 僅算全國）
python -m centrality.sensitivity --region tokyo --cutoff --force
python -m centrality.sensitivity --region kinki --unbounded --force
python -m centrality.sensitivity --region all --cutoff --force

# 下載、整理並檢查2020年250m人口資料
python scripts/download_population_data.py
python -m population_data.build
python -m visualizers.population_mesh --open
```
