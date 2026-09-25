# 鐵路網研究工具

```bash
# 建立 .venv、安裝依賴並下載生資料
python3 setup.py

# 啟用虛擬環境
source .venv/bin/activate

# 建立 SQLite 鐵路網
python -m rail_data.build.main

# 下載、整理並檢查2020年250m人口資料
python scripts/download_population_data.py
python -m population_data.build
python -m visualizers.population_mesh --open
```
