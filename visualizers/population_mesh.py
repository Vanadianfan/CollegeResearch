"""Validate and visualize the processed 2020 250 m population mesh database."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from population_data.mesh import fifth_mesh_bounds
from population_data.schema import (
    POPULATION_SCHEMA_VERSION,
    PopulationSchemaMismatchError,
    connect_database,
)
from rail_data.paths import POPULATION_DB_PATH, PROJECT_ROOT


@dataclass(slots=True)
class CheckResult:
    level: str
    message: str
    details: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="2020年250m人口メッシュDBを検査し、HTMLで地図表示します。"
    )
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        default=POPULATION_DB_PATH,
    )
    parser.add_argument(
        "--primary-mesh",
        action="append",
        help="表示する一次メッシュ4桁。複数指定可（既定: 5339、なければ最多地域）",
    )
    parser.add_argument(
        "--rail-db",
        type=Path,
        default=PROJECT_ROOT / "rail_network.sqlite",
        help="重ねて表示する鉄道路網DB。存在しない場合は人口だけ表示",
    )
    parser.add_argument("--output", type=Path, help="HTML 出力先")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--open", action="store_true")
    return parser.parse_args()


def validate_database(connection: sqlite3.Connection) -> list[CheckResult]:
    results: list[CheckResult] = []
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    results.append(
        CheckResult(
            "OK" if version == POPULATION_SCHEMA_VERSION else "ERROR",
            f"schema version={version}"
            if version == POPULATION_SCHEMA_VERSION
            else f"schema version={version}（期待値 {POPULATION_SCHEMA_VERSION}）",
        )
    )
    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    expected_metadata = {
        "census_year": "2020",
        "datum": "JGD2011",
        "mesh_size_m": "250",
        "stats_id": "T001142",
        "population_field": "T001142001",
    }
    mismatches = [
        f"{key}={metadata.get(key)!r} (expected={value!r})"
        for key, value in expected_metadata.items()
        if metadata.get(key) != value
    ]
    results.append(
        CheckResult(
            "ERROR" if mismatches else "OK",
            "メタデータ不一致" if mismatches else "資料定義: 2020 / JGD2011 / 250m / T001142001",
            mismatches,
        )
    )
    row_count, population_sum, primary_count = connection.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(population), 0),
               COUNT(DISTINCT primary_mesh_code)
        FROM population_mesh
        """
    ).fetchone()
    results.append(
        CheckResult(
            "ERROR" if row_count == 0 else "OK",
            f"人口メッシュ {row_count:,} 件 / 一次メッシュ {primary_count:,} 件 / "
            f"人口合計 {population_sum:,}",
        )
    )
    invalid = connection.execute(
        """
        SELECT mesh_code FROM population_mesh
        WHERE length(mesh_code) != 10
           OR mesh_code GLOB '*[^0-9]*'
           OR primary_mesh_code != substr(mesh_code, 1, 4)
           OR population < 0
        LIMIT 10
        """
    ).fetchall()
    results.append(
        CheckResult(
            "ERROR" if invalid else "OK",
            "コード・人口値が不正" if invalid else "メッシュコードと人口値の形式が正常",
            [row[0] for row in invalid],
        )
    )

    geometry_errors: list[str] = []
    for row in connection.execute(
        """
        SELECT mesh_code, west_lon, south_lat, east_lon, north_lat
        FROM population_mesh
        """
    ):
        bounds = fifth_mesh_bounds(row[0])
        expected = (
            bounds.west_lon,
            bounds.south_lat,
            bounds.east_lon,
            bounds.north_lat,
        )
        if any(
            not math.isclose(value, target, abs_tol=1e-12)
            for value, target in zip(row[1:], expected)
        ):
            geometry_errors.append(row[0])
            if len(geometry_errors) >= 10:
                break
    results.append(
        CheckResult(
            "ERROR" if geometry_errors else "OK",
            "網格邊界計算錯誤" if geometry_errors else "250m網格邊界與 KEY_CODE 一致",
            geometry_errors,
        )
    )
    return results


def print_results(results: list[CheckResult]) -> None:
    for result in results:
        print(f"[{result.level}] {result.message}")
        for detail in result.details:
            print(f"       - {detail}")


def choose_primary_meshes(
    connection: sqlite3.Connection,
    requested: list[str] | None,
) -> list[str]:
    if requested:
        codes = sorted(set(requested))
        if any(len(code) != 4 or not code.isdigit() for code in codes):
            raise ValueError("--primary-mesh は4桁で指定してください。")
    else:
        tokyo = connection.execute(
            "SELECT 1 FROM population_mesh WHERE primary_mesh_code='5339' LIMIT 1"
        ).fetchone()
        if tokyo:
            codes = ["5339"]
        else:
            row = connection.execute(
                """
                SELECT primary_mesh_code
                FROM population_mesh
                GROUP BY primary_mesh_code
                ORDER BY COUNT(*) DESC, primary_mesh_code
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise ValueError("表示できる人口メッシュがありません。")
            codes = [row[0]]
    missing = [
        code
        for code in codes
        if connection.execute(
            "SELECT 1 FROM population_mesh WHERE primary_mesh_code=? LIMIT 1",
            (code,),
        ).fetchone()
        is None
    ]
    if missing:
        raise ValueError(f"DB に一次メッシュがありません: {', '.join(missing)}")
    return codes


def load_rail_overlay(
    rail_database: Path,
    bounds: tuple[float, float, float, float],
) -> tuple[list[list[object]], list[list[object]]]:
    path = rail_database.expanduser().resolve()
    if not path.is_file():
        return [], []
    west, south, east, north = bounds
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "atomic_segment",
            "network_node",
            "rail_line",
            "station_anchor",
            "station_component",
            "station",
            "station_group",
        }
        if not required <= tables:
            return [], []
        rail_segments = [
            list(row)
            for row in connection.execute(
                """
                SELECT n1.lon, n1.lat, n2.lon, n2.lat, rl.name
                FROM atomic_segment AS segment
                JOIN network_node AS n1 ON n1.id=segment.from_node_id
                JOIN network_node AS n2 ON n2.id=segment.to_node_id
                JOIN rail_line AS rl ON rl.id=segment.line_id
                WHERE MIN(n1.lon, n2.lon) <= ?
                  AND MAX(n1.lon, n2.lon) >= ?
                  AND MIN(n1.lat, n2.lat) <= ?
                  AND MAX(n1.lat, n2.lat) >= ?
                """,
                (east, west, north, south),
            )
        ]
        stations = [
            list(row)
            for row in connection.execute(
                """
                SELECT DISTINCT n.lon, n.lat, sg.display_name
                FROM station_anchor AS anchor
                JOIN network_node AS n ON n.id=anchor.node_id
                JOIN station_component AS component
                  ON component.id=anchor.station_component_id
                JOIN station AS station ON station.id=component.station_id
                JOIN station_group AS sg ON sg.id=station.group_id
                WHERE n.lon BETWEEN ? AND ? AND n.lat BETWEEN ? AND ?
                """,
                (west, east, south, north),
            )
        ]
        return rail_segments, stations
    finally:
        connection.close()


def html_document(payload: dict[str, object]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>250m人口メッシュ検証</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; width: 100%; height: 100%; overflow: hidden; }}
  body {{ background: #101418; color: #f2f4f6; font-family: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif; }}
  canvas {{ display: block; width: 100%; height: 100%; cursor: grab; touch-action: none; }}
  canvas.dragging {{ cursor: grabbing; }}
  .panel {{ position: absolute; z-index: 2; background: rgba(16,20,24,.90); border: 1px solid #46505a; border-radius: 8px; padding: 10px 12px; line-height: 1.55; backdrop-filter: blur(6px); pointer-events: none; }}
  #summary {{ top: 12px; left: 12px; }}
  #detail {{ right: 12px; bottom: 12px; min-width: 230px; }}
  .title {{ font-weight: 600; margin-bottom: 4px; }}
  .muted {{ color: #aeb8c2; }}
  .legend {{ display: flex; align-items: center; gap: 7px; margin-top: 7px; }}
  .bar {{ width: 130px; height: 9px; border-radius: 5px; background: linear-gradient(90deg, rgba(255,184,73,.15), rgba(255,184,73,.98)); }}
  .dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #ff647c; margin-right: 5px; }}
</style>
</head>
<body>
<canvas id="map" aria-label="2020年250m人口メッシュと鉄道路線"></canvas>
<div id="summary" class="panel"></div>
<div id="detail" class="panel"><span class="muted">メッシュにカーソルを合わせてください</span></div>
<script>
const source = {data};
const canvas = document.getElementById('map');
const ctx = canvas.getContext('2d');
const summary = document.getElementById('summary');
const detail = document.getElementById('detail');
const cellWidth = 1 / 320;
const cellHeight = 1 / 480;
function meshBounds(code) {{
  let south = Number(code.slice(0,2)) * 2/3;
  let west = 100 + Number(code.slice(2,4));
  south += Number(code[4]) * 5/60 + Number(code[6]) * 30/3600;
  west += Number(code[5]) * 7.5/60 + Number(code[7]) * 45/3600;
  let h = 30/3600, w = 45/3600;
  for (const ch of code.slice(8,10)) {{
    const q = Number(ch) - 1; h /= 2; w /= 2;
    west += (q % 2) * w; south += Math.floor(q / 2) * h;
  }}
  return [west, south, west+w, south+h];
}}
const meshes = source.meshes.map(m => {{ const b=meshBounds(m[0]); return {{code:m[0], population:m[1], disclosure:m[2], target:m[3], aggregated:m[4], b}}; }});
const index = new Map();
for (const m of meshes) {{
  const primary = m.code.slice(0,4);
  const baseWest = 100 + Number(primary.slice(2,4));
  const baseSouth = Number(primary.slice(0,2)) * 2/3;
  const x = Math.round((m.b[0]-baseWest)/cellWidth);
  const y = Math.round((m.b[1]-baseSouth)/cellHeight);
  index.set(`${{primary}}:${{x}}:${{y}}`, m);
}}
const maxPopulation = Math.max(1, ...meshes.map(m => m.population));
const totalPopulation = meshes.reduce((sum,m) => sum+m.population, 0);
let dpr=1, width=0, height=0;
let centerLon=(source.bounds[0]+source.bounds[2])/2;
let centerLat=(source.bounds[1]+source.bounds[3])/2;
let scale=1;
let dragging=false, lastX=0, lastY=0;
let touches=new Map(), pinchDistance=0;
function resize(first=false) {{
  dpr=window.devicePixelRatio||1; width=innerWidth; height=innerHeight;
  canvas.width=Math.round(width*dpr); canvas.height=Math.round(height*dpr);
  canvas.style.width=width+'px'; canvas.style.height=height+'px';
  if (first) {{
    const lonSpan=Math.max(.001,source.bounds[2]-source.bounds[0]);
    const latSpan=Math.max(.001,source.bounds[3]-source.bounds[1]);
    scale=.92*Math.min(width/lonSpan,height/latSpan);
  }}
  draw();
}}
function project(lon,lat) {{ return [(lon-centerLon)*scale+width/2,(centerLat-lat)*scale+height/2]; }}
function unproject(x,y) {{ return [(x-width/2)/scale+centerLon,centerLat-(y-height/2)/scale]; }}
function draw() {{
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,width,height);
  ctx.fillStyle='#101418'; ctx.fillRect(0,0,width,height);
  const showGrid=scale*cellWidth>5;
  for (const m of meshes) {{
    const p1=project(m.b[0],m.b[3]), p2=project(m.b[2],m.b[1]);
    if(p2[0]<0||p1[0]>width||p2[1]<0||p1[1]>height) continue;
    const t=Math.log1p(m.population)/Math.log1p(maxPopulation);
    ctx.fillStyle=`rgba(255,184,73,${{.10+.88*t}})`;
    ctx.fillRect(p1[0],p1[1],Math.max(1,p2[0]-p1[0]),Math.max(1,p2[1]-p1[1]));
    if(showGrid) {{ ctx.strokeStyle='rgba(230,235,240,.20)'; ctx.lineWidth=.5; ctx.strokeRect(p1[0],p1[1],p2[0]-p1[0],p2[1]-p1[1]); }}
  }}
  ctx.strokeStyle='rgba(88,205,229,.78)'; ctx.lineWidth=1;
  ctx.beginPath();
  for(const r of source.rails) {{ const a=project(r[0],r[1]), b=project(r[2],r[3]); ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]); }}
  ctx.stroke();
  ctx.fillStyle='#ff647c';
  for(const s of source.stations) {{ const p=project(s[0],s[1]); if(p[0]<-3||p[0]>width+3||p[1]<-3||p[1]>height+3)continue; ctx.beginPath();ctx.arc(p[0],p[1],2.4,0,Math.PI*2);ctx.fill(); }}
}}
function meshAt(x,y) {{
  const [lon,lat]=unproject(x,y);
  for(const primary of source.primary) {{
    const west=100+Number(primary.slice(2,4)); const south=Number(primary.slice(0,2))*2/3;
    if(lon<west||lon>=west+1||lat<south||lat>=south+2/3)continue;
    const ix=Math.floor((lon-west)/cellWidth+1e-9), iy=Math.floor((lat-south)/cellHeight+1e-9);
    return index.get(`${{primary}}:${{ix}}:${{iy}}`)||null;
  }}
  return null;
}}
function showDetail(event) {{
  if(dragging)return; const rect=canvas.getBoundingClientRect(); const m=meshAt(event.clientX-rect.left,event.clientY-rect.top);
  if(!m) {{ detail.innerHTML='<span class="muted">人口記録のないメッシュです</span>'; return; }}
  detail.innerHTML=`<div class="title">250mメッシュ ${{m.code}}</div><div>人口（総数）: ${{m.population.toLocaleString('ja-JP')}} 人</div><div>秘匿処理: ${{m.disclosure}}</div><div>合算先: ${{m.target||'なし'}}</div><div>合算対象: ${{m.aggregated||'なし'}}</div>`;
}}
canvas.addEventListener('mousedown',e=>{{dragging=true;lastX=e.clientX;lastY=e.clientY;canvas.classList.add('dragging');}});
addEventListener('mouseup',()=>{{dragging=false;canvas.classList.remove('dragging');}});
addEventListener('mousemove',e=>{{if(dragging){{centerLon-=(e.clientX-lastX)/scale;centerLat+=(e.clientY-lastY)/scale;lastX=e.clientX;lastY=e.clientY;draw();}}else showDetail(e);}});
canvas.addEventListener('wheel',e=>{{e.preventDefault();const rect=canvas.getBoundingClientRect();const x=e.clientX-rect.left,y=e.clientY-rect.top;const before=unproject(x,y);const factor=Math.exp(-Math.max(-100,Math.min(100,e.deltaY))*.0025);scale=Math.max(80,Math.min(2e7,scale*factor));const after=unproject(x,y);centerLon+=before[0]-after[0];centerLat+=before[1]-after[1];draw();}},{{passive:false}});
canvas.addEventListener('touchstart',e=>{{e.preventDefault();for(const t of e.changedTouches)touches.set(t.identifier,[t.clientX,t.clientY]);if(touches.size===1){{const p=[...touches.values()][0];lastX=p[0];lastY=p[1];}}if(touches.size===2){{const p=[...touches.values()];pinchDistance=Math.hypot(p[0][0]-p[1][0],p[0][1]-p[1][1]);}}}},{{passive:false}});
canvas.addEventListener('touchmove',e=>{{e.preventDefault();for(const t of e.changedTouches)touches.set(t.identifier,[t.clientX,t.clientY]);const p=[...touches.values()];if(p.length===1){{centerLon-=(p[0][0]-lastX)/scale;centerLat+=(p[0][1]-lastY)/scale;lastX=p[0][0];lastY=p[0][1];}}else if(p.length===2){{const dist=Math.hypot(p[0][0]-p[1][0],p[0][1]-p[1][1]);const mid=[(p[0][0]+p[1][0])/2,(p[0][1]+p[1][1])/2];const before=unproject(mid[0],mid[1]);scale*=dist/pinchDistance;pinchDistance=dist;const after=unproject(mid[0],mid[1]);centerLon+=before[0]-after[0];centerLat+=before[1]-after[1];}}draw();}},{{passive:false}});
function endTouch(e){{for(const t of e.changedTouches)touches.delete(t.identifier);pinchDistance=0;}}
canvas.addEventListener('touchend',endTouch);canvas.addEventListener('touchcancel',endTouch);
summary.innerHTML=`<div class="title">2020年 250m人口メッシュ</div><div>一次メッシュ: ${{source.primary.map(c=>'M'+c).join(', ')}}</div><div>表示: ${{meshes.length.toLocaleString('ja-JP')}} メッシュ / ${{totalPopulation.toLocaleString('ja-JP')}} 人</div><div><span class="dot"></span>駅 ${{source.stations.length.toLocaleString('ja-JP')}} 件　<span style="color:#58cde5">━</span> 鉄道</div><div class="legend"><span>0</span><span class="bar"></span><span>${{maxPopulation.toLocaleString('ja-JP')}}</span></div><div class="muted">ドラッグ: 移動　ピンチ／スクロール: 拡大縮小</div>`;
addEventListener('resize',()=>resize(false));resize(true);
</script>
</body>
</html>
"""


def create_visualizer(
    connection: sqlite3.Connection,
    primary_codes: list[str],
    rail_database: Path,
    output_path: Path,
) -> None:
    placeholders = ",".join("?" for _ in primary_codes)
    rows = connection.execute(
        f"""
        SELECT mesh_code, population, disclosure_status,
               aggregation_target_mesh_code, aggregated_mesh_codes,
               west_lon, south_lat, east_lon, north_lat
        FROM population_mesh
        WHERE primary_mesh_code IN ({placeholders})
        ORDER BY mesh_code
        """,
        primary_codes,
    ).fetchall()
    if not rows:
        raise ValueError("表示対象の人口メッシュがありません。")
    bounds = (
        min(row[5] for row in rows),
        min(row[6] for row in rows),
        max(row[7] for row in rows),
        max(row[8] for row in rows),
    )
    rails, stations = load_rail_overlay(rail_database, bounds)
    payload = {
        "primary": primary_codes,
        "bounds": bounds,
        "meshes": [list(row[:5]) for row in rows],
        "rails": rails,
        "stations": stations,
    }
    output = output_path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_document(payload), encoding="utf-8")
    print(f"HTML 出力: {output}")
    print(f"  population meshes: {len(rows):,}")
    print(f"  rail segments: {len(rails):,}")
    print(f"  station anchors: {len(stations):,}")


def main() -> int:
    args = parse_args()
    try:
        connection = connect_database(args.database)
    except (OSError, PopulationSchemaMismatchError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}")
        return 1
    try:
        results = validate_database(connection)
        print_results(results)
        failed = any(result.level == "ERROR" for result in results)
        if args.strict:
            failed = failed or any(result.level == "WARN" for result in results)
        if failed:
            return 1
        if args.check_only:
            return 0
        primary_codes = choose_primary_meshes(connection, args.primary_mesh)
        database_path = args.database.expanduser().resolve()
        output = args.output or database_path.with_name(
            f"{database_path.stem}_visualizer.html"
        )
        create_visualizer(connection, primary_codes, args.rail_db, output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}")
        return 1
    finally:
        connection.close()
    if args.open:
        webbrowser.open(output.expanduser().resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
