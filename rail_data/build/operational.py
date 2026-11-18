"""Build and persist the ekidata operational railway network and regional tags."""

from __future__ import annotations

import csv
import heapq
import math
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from rail_data.paths import PROJECT_ROOT
from shared.params import DEFAULT_PARAMS

EKIDATA_DIR = PROJECT_ROOT / "raw_data" / "駅データ.jp"
LINE_CSV = EKIDATA_DIR / "line20260618free.csv"
STATION_CSV = EKIDATA_DIR / "station20260713free.csv"
JOIN_CSV = EKIDATA_DIR / "join20260618.csv"

TOKYO_PREFS = frozenset({"11", "12", "13", "14"})
KINKI_PREFS = frozenset({"26", "27", "28", "29"})

# Paid limited-express-only overlay lines in ekidata to exclude from regular operational graph
EXCLUDED_LINE_CODES = frozenset({"11328"})  # JR成田エクスプレス


def normalize_name(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)
    for old, new in (
        ("ヶ", "ケ"),
        ("ヵ", "カ"),
        ("ノ", "之"),
        ("ッツ", "ツ"),
        (" ", ""),
        ("　", ""),
    ):
        s = s.replace(old, new)
    return s


JR_OPERATORS: frozenset[str] = frozenset(
    {
        "北海道旅客鉄道",
        "東日本旅客鉄道",
        "東海旅客鉄道",
        "西日本旅客鉄道",
        "四国旅客鉄道",
        "九州旅客鉄道",
    }
)

_RAW_OP_PREFIX_MAP: dict[str, tuple[str, ...]] = {
    "JR": tuple(JR_OPERATORS),
    "東京メトロ": ("東京地下鉄",),
    "都営": ("東京都",),
    "都電": ("東京都",),
    "大阪メトロ": ("大阪市高速電気軌道",),
    "東急": ("東急電鉄",),
    "東武": ("東武鉄道",),
    "西武": ("西武鉄道",),
    "京王": ("京王電鉄",),
    "小田急": ("小田急電鉄",),
    "京急": ("京浜急行電鉄",),
    "京成": ("京成電鉄",),
    "新京成": ("新京成電鉄",),
    "相鉄": ("相模鉄道",),
    "名鉄": ("名古屋鉄道",),
    "近鉄": ("近畿日本鉄道",),
    "南海": ("南海電気鉄道",),
    "阪急": ("阪急電鉄",),
    "阪神": ("阪神電気鉄道",),
    "京阪": ("京阪電気鉄道",),
    "西鉄": ("西日本鉄道",),
    "広電": ("広島電鉄",),
    "伊予鉄道": ("伊予鉄道",),
    "伊予鉄": ("伊予鉄道",),
    "富山地鉄": ("富山地方鉄道",),
    "東京モノレール": ("東京モノレール",),
    "大阪モノレール": ("大阪モノレール",),
    "多摩モノレール": ("多摩都市モノレール",),
    "千葉都市モノレール": ("千葉都市モノレール",),
    "沖縄都市モノレール": ("沖縄都市モノレール",),
    "湘南モノレール": ("湘南モノレール",),
    "神鉄": ("神戸電鉄",),
    "神戸市営": ("神戸市",),
    "京都市営": ("京都市",),
    "福岡市営": ("福岡市",),
    "札幌市営": ("札幌市",),
    "仙台市営": ("仙台市",),
    "名古屋市営": ("名古屋市",),
    "横浜市営": ("横浜市",),
}

OP_PREFIX_OPERATORS: dict[str, frozenset[str]] = {
    normalize_name(k): frozenset(v) for k, v in _RAW_OP_PREFIX_MAP.items()
}
_SORTED_OP_PREFIXES: tuple[str, ...] = tuple(
    sorted(OP_PREFIX_OPERATORS.keys(), key=len, reverse=True)
)


def extract_n02_line_stems(norm_n02_name: str) -> tuple[frozenset[str], bool]:
    """Return canonical route stems and whether N02 uses generic '本線'."""
    no_paren = re.sub(r"\([^)]*\)", "", norm_n02_name)
    candidates = [norm_n02_name, no_paren]
    candidates.extend(re.findall(r"\(([^)]+)\)", norm_n02_name))

    expanded: list[str] = []
    for cand in candidates:
        if not cand:
            continue
        expanded.append(cand)
        stripped = re.sub(r"^\d+号線", "", cand)
        if stripped:
            expanded.append(stripped)

    stems: set[str] = set()
    is_generic_honsen = False
    for cand in expanded:
        if cand == "本線":
            is_generic_honsen = True
        else:
            stems.add(cand)
            if cand.endswith("本線") and len(cand) > 3:
                stems.add(cand[:-2] + "線")
    return frozenset(stems), is_generic_honsen


def extract_op_line_signature(
    norm_op_name: str,
) -> tuple[frozenset[str], bool, frozenset[str]]:
    """Return (route stems, ends_with_bare_honsen, hinted N02 operator_names)."""
    outer = re.sub(r"\([^)]*\)|【[^】]*】", "", norm_op_name)
    bases: list[str] = [outer] if outer else []
    for inner in re.findall(r"\(([^)]+)\)", norm_op_name):
        if inner.endswith("線") or inner.endswith("ライン"):
            bases.append(inner)

    op_hints: set[str] = set()
    for pfx in _SORTED_OP_PREFIXES:
        if norm_op_name.startswith(pfx) or any(b.startswith(pfx) for b in bases):
            op_hints.update(OP_PREFIX_OPERATORS[pfx])
    if "・JR" in norm_op_name:
        op_hints.update(JR_OPERATORS)

    stems: set[str] = set()
    is_bare_honsen = False
    for base in bases:
        stems.add(base)
        for pfx in _SORTED_OP_PREFIXES:
            if base.startswith(pfx) and len(base) > len(pfx):
                rest = base[len(pfx) :]
                if rest == "本線":
                    is_bare_honsen = True
                elif not rest.startswith("・"):
                    stems.add(rest)

    extra: set[str] = set()
    for s in stems:
        if s.endswith("本線") and len(s) > 3:
            extra.add(s[:-2] + "線")
    stems.update(extra)
    if "本線" in stems:
        stems.discard("本線")
        is_bare_honsen = True
    return frozenset(stems), is_bare_honsen, frozenset(op_hints)


def fast_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    rad = math.pi / 180.0
    lat_mid = (lat1 + lat2) * 0.5 * rad
    dx = (lon2 - lon1) * rad * 6378137.0 * math.cos(lat_mid)
    dy = (lat2 - lat1) * rad * 6378137.0
    return math.hypot(dx, dy)


def classify_pref_region(pref_code: str) -> str | None:
    normalized = pref_code.strip().zfill(2) if pref_code and pref_code.strip() else ""
    if normalized in TOKYO_PREFS:
        return "tokyo"
    if normalized in KINKI_PREFS:
        return "kinki"
    return None


def infer_operator_name(line_name: str, matched_ops: Counter[str]) -> str:
    if matched_ops:
        return matched_ops.most_common(1)[0][0]
    if line_name.startswith("JR"):
        return "JR"
    for sep in ("・", " "):
        if sep in line_name:
            return line_name.split(sep, 1)[0]
    return line_name


def build_and_write_operational_network(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    """
    Construct and populate:
      - operational_line
      - operational_line_stop (including native ekidata pref_code and region)
      - operational_edge
      - operational_edge_has_graph_edge
      - station.region
    """
    if not (LINE_CSV.is_file() and STATION_CSV.is_file() and JOIN_CSV.is_file()):
        raise FileNotFoundError(
            f"ekidata CSV ファイルが見つかりません: {EKIDATA_DIR}"
        )

    cur = connection.cursor()

    # 1. Load N02 lines, station groups, primary anchors, network nodes, and graph edges
    n02_lines: dict[int, tuple[str, str, str, str, str]] = {}
    n02_line_stems: dict[int, tuple[frozenset[str], bool]] = {}
    for lid, rtype, ptype, name, op in cur.execute(
        "SELECT id, railway_type_code, provider_type_code, name, operator_name FROM rail_line"
    ):
        norm_lname = normalize_name(name)
        n02_lines[lid] = (rtype, ptype, name, op, norm_lname)
        n02_line_stems[lid] = extract_n02_line_stems(norm_lname)

    n02_groups: dict[int, list[object]] = {}
    group_anchors: dict[int, list[int]] = defaultdict(list)
    anchor_info: dict[int, tuple[int, int, int, str, str, int, float, float, str]] = {}
    for row in cur.execute(
        """
        WITH ranked_anchor AS (
            SELECT
                a.id AS anchor_id,
                a.node_id,
                sc.station_id,
                ROW_NUMBER() OVER (
                    PARTITION BY sc.station_id
                    ORDER BY a.is_primary DESC, sc.component_no, a.anchor_no
                ) AS rank_no
            FROM station_anchor AS a
            JOIN station_component AS sc ON sc.id = a.station_component_id
        )
        SELECT
            ra.anchor_id, s.id, s.group_id, g.group_code, g.display_name,
            s.name, s.line_id, l.name, l.operator_name, ra.node_id, n.lon, n.lat
        FROM ranked_anchor AS ra
        JOIN station AS s ON s.id = ra.station_id
        JOIN station_group AS g ON g.id = s.group_id
        JOIN rail_line AS l ON l.id = s.line_id
        JOIN network_node AS n ON n.id = ra.node_id
        WHERE ra.rank_no = 1
        ORDER BY ra.anchor_id
        """
    ):
        aid, sid, gid, gcode, gname, sname, lid, lname, opname, nid, lon, lat = row
        anchor_info[aid] = (sid, gid, lid, lname, opname, nid, lon, lat, sname)
        group_anchors[gid].append(aid)
        if gid not in n02_groups:
            n02_groups[gid] = [
                gcode,
                gname,
                [lon],
                [lat],
                {normalize_name(gname), normalize_name(sname)},
            ]
        else:
            n02_groups[gid][2].append(lon)  # type: ignore[attr-defined]
            n02_groups[gid][3].append(lat)  # type: ignore[attr-defined]
            n02_groups[gid][4].add(normalize_name(sname))  # type: ignore[attr-defined]

    group_by_norm_name: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    all_group_coords: list[tuple[int, float, float]] = []
    for gid, info in n02_groups.items():
        lons: list[float] = info[2]  # type: ignore[assignment]
        lats: list[float] = info[3]  # type: ignore[assignment]
        nset: set[str] = info[4]  # type: ignore[assignment]
        mlon = sum(lons) / len(lons)
        mlat = sum(lats) / len(lats)
        info[2] = mlon
        info[3] = mlat
        all_group_coords.append((gid, mlon, mlat))
        for nm in nset:
            group_by_norm_name[nm].append((gid, mlon, mlat))

    node_info: dict[int, tuple[int, float, float, str]] = {}
    nodes_by_line_name_op: dict[tuple[str, str], list[tuple[int, int, float, float, str]]] = (
        defaultdict(list)
    )
    nodes_by_op: dict[str, list[tuple[int, int, float, float, str]]] = defaultdict(list)
    for nid, lid, lon, lat, ttype in cur.execute(
        "SELECT id, line_id, lon, lat, topology_type FROM network_node"
    ):
        if lid not in n02_lines:
            continue
        node_info[nid] = (lid, lon, lat, ttype)
        _, _, lname, opname, _ = n02_lines[lid]
        entry = (nid, lid, lon, lat, ttype)
        nodes_by_line_name_op[(lname, opname)].append(entry)
        nodes_by_op[opname].append(entry)

    # Build graph_edge adjacency on network_node
    # ge_adj[u] -> list of (v, dist_m, ge_id | None, forward, line_id)
    ge_adj: dict[int, list[tuple[int, float, int | None, int, int]]] = defaultdict(list)
    ge_distance: dict[int, float] = {}
    ge_line_id: dict[int, int] = {}
    for ge_id, fn, tn, dm, dir_code, lid in cur.execute(
        """
        SELECT ge.id, ge.from_node_id, ge.to_node_id, ge.distance_m, ge.direction, rlc.line_id
        FROM graph_edge AS ge
        JOIN rail_line_component AS rlc ON rlc.id = ge.line_component_id
        WHERE ge.edge_kind = 'rail'
        """
    ):
        if lid not in n02_lines:
            continue
        ge_distance[ge_id] = dm
        ge_line_id[ge_id] = lid
        if dir_code in ("both", "forward"):
            ge_adj[fn].append((tn, dm, ge_id, 1, lid))
        if dir_code in ("both", "backward"):
            ge_adj[tn].append((fn, dm, ge_id, 0, lid))

    linked_node_pairs: set[tuple[int, int]] = set()

    def add_virtual_link(u: int, v: int, pen: float, l1: int, l2: int) -> None:
        key = (min(u, v), max(u, v))
        if key in linked_node_pairs:
            return
        linked_node_pairs.add(key)
        ge_adj[u].append((v, pen, None, 1, l2))
        ge_adj[v].append((u, pen, None, 1, l1))

    # Link identical coordinates across split/connecting N02 lines
    nodes_by_coord: dict[tuple[float, float], list[int]] = defaultdict(list)
    for nid, (lid, lon, lat, _ttype) in node_info.items():
        nodes_by_coord[(round(lon, 6), round(lat, 6))].append(nid)
    for nids in nodes_by_coord.values():
        if len(nids) > 1:
            for i in range(len(nids)):
                for j in range(i + 1, len(nids)):
                    u, v = nids[i], nids[j]
                    l1, l2 = node_info[u][0], node_info[v][0]
                    pen = 5.0 if n02_lines[l1][3] == n02_lines[l2][3] else 40.0
                    add_virtual_link(u, v, pen, l1, l2)

    # Link split N02 lines sharing (line.name, line.operator_name) within 100 m
    for nlist in nodes_by_line_name_op.values():
        lids = {x[1] for x in nlist}
        if len(lids) > 1:
            terms = [x for x in nlist if x[4] == "terminal"]
            for u, l1, lon1, lat1, _ in terms:
                for v, l2, lon2, lat2, _ in nlist:
                    if l1 == l2:
                        continue
                    if abs(lon1 - lon2) > 0.002 or abs(lat1 - lat2) > 0.002:
                        continue
                    d = fast_distance_m(lon1, lat1, lon2, lat2)
                    if d <= 100.0:
                        add_virtual_link(u, v, d + 2.0, l1, l2)

    # Link same-operator branch terminals near trunk nodes (e.g. Meitetsu Biwajima Junction)
    for nlist in nodes_by_op.values():
        lids = {x[1] for x in nlist}
        if len(lids) > 1:
            terms = [x for x in nlist if x[4] == "terminal"]
            for u, l1, lon1, lat1, _ in terms:
                for v, l2, lon2, lat2, _ in nlist:
                    if l1 == l2:
                        continue
                    if abs(lon1 - lon2) > 0.0012 or abs(lat1 - lat2) > 0.0012:
                        continue
                    d = fast_distance_m(lon1, lat1, lon2, lat2)
                    if d <= 60.0:
                        add_virtual_link(u, v, d + 150.0, l1, l2)

    # Link anchors within the same station_group with realistic transfer penalty
    for gid, aids in group_anchors.items():
        if len(aids) > 1:
            for i in range(len(aids)):
                for j in range(i + 1, len(aids)):
                    n1, l1 = anchor_info[aids[i]][5], anchor_info[aids[i]][2]
                    n2, l2 = anchor_info[aids[j]][5], anchor_info[aids[j]][2]
                    if n1 == n2:
                        continue
                    pen = 800.0 if n02_lines[l1][3] == n02_lines[l2][3] else 2500.0
                    ge_adj[n1].append((n2, pen, None, 1, l2))
                    ge_adj[n2].append((n1, pen, None, 1, l1))

    # Fallback bridge across nearby split station_groups (never beats real graph_edge paths)
    for i in range(len(all_group_coords)):
        g1, lon1, lat1 = all_group_coords[i]
        for j in range(i + 1, len(all_group_coords)):
            g2, lon2, lat2 = all_group_coords[j]
            if abs(lon1 - lon2) > 0.0035 or abs(lat1 - lat2) > 0.0035:
                continue
            d = fast_distance_m(lon1, lat1, lon2, lat2)
            if d <= 250.0 and group_anchors[g1] and group_anchors[g2]:
                n1, l1 = anchor_info[group_anchors[g1][0]][5], anchor_info[group_anchors[g1][0]][2]
                n2, l2 = anchor_info[group_anchors[g2][0]][5], anchor_info[group_anchors[g2][0]][2]
                if n1 == n2:
                    continue
                ge_adj[n1].append((n2, d + 3500.0, None, 1, l2))
                ge_adj[n2].append((n1, d + 3500.0, None, 1, l1))

    # 2. Load ekidata lines, stations, and joins
    eki_lines: dict[str, dict[str, str]] = {}
    with LINE_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["e_status"] != "0":
                continue
            if "新幹線" in r["line_name"] or r["line_cd"] in EXCLUDED_LINE_CODES:
                continue
            eki_lines[r["line_cd"]] = r

    eki_stations: dict[str, dict[str, str]] = {}
    stations_by_line: dict[str, list[dict[str, str]]] = defaultdict(list)
    with STATION_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["e_status"] != "0" or r["line_cd"] not in eki_lines:
                continue
            eki_stations[r["station_cd"]] = r
            stations_by_line[r["line_cd"]].append(r)

    # Map each ekidata station_cd to ranked candidate N02 station_groups
    eki_to_candidates: dict[str, list[tuple[int, float]]] = {}
    for scd, r in eki_stations.items():
        lon, lat = float(r["lon"]), float(r["lat"])
        nm = normalize_name(r["station_name"])
        cands: list[tuple[int, float]] = []
        seen_gids: set[int] = set()
        for gid, glon, glat in group_by_norm_name.get(nm, ()):
            if not group_anchors[gid] or gid in seen_gids:
                continue
            d = fast_distance_m(lon, lat, glon, glat)
            if d <= 1500.0:
                cands.append((gid, d))
                seen_gids.add(gid)
        if not cands:
            best_gid: int | None = None
            best_d = 1e9
            for gid, glon, glat in all_group_coords:
                if abs(lon - glon) > 0.01 or abs(lat - glat) > 0.01:
                    continue
                d = fast_distance_m(lon, lat, glon, glat)
                if d < best_d and d <= 600.0 and group_anchors[gid]:
                    best_d = d
                    best_gid = gid
            if best_gid is not None:
                cands.append((best_gid, best_d))
        if cands:
            cands.sort(key=lambda x: x[1])
            eki_to_candidates[scd] = cands

    joins_by_line: dict[str, list[tuple[str, str]]] = defaultdict(list)
    with JOIN_CSV.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lcd = r["line_cd"]
            if lcd not in eki_lines:
                continue
            s1, s2 = r["station_cd1"], r["station_cd2"]
            if s1 in eki_stations and s2 in eki_stations:
                joins_by_line[lcd].append((s1, s2))

    # Helper to find directed graph_edge path between two N02 station_groups
    node_to_group: dict[int, int] = {
        anchor_info[aid][5]: anchor_info[aid][1] for aid in anchor_info
    }

    def line_match_tier(
        lid: int,
        op_sig: tuple[frozenset[str], bool, frozenset[str]],
        preferred_op: str | None,
    ) -> int:
        """
        Return line affinity tier:
          3: exact canonical line name + operator match
          2: exact canonical line name without conflicting operator hint
          1: matching operator (different/alias line name)
          0: different operator and line name
        """
        op_stems, op_is_honsen, op_hints = op_sig
        nstems, n_is_honsen = n02_line_stems[lid]
        opname = n02_lines[lid][3]
        op_matches = (opname in op_hints) or (
            not op_hints and preferred_op is not None and opname == preferred_op
        )
        stem_hit = bool(op_stems & nstems) or (
            n_is_honsen and op_is_honsen and op_matches
        )
        if not stem_hit and op_matches:
            stem_hit = any(
                ns.startswith(os) and len(os) >= 4
                for os in op_stems
                for ns in nstems
            )
        if stem_hit:
            if op_matches or (preferred_op is not None and opname == preferred_op):
                return 3
            if not op_hints:
                return 2
        if op_matches or (preferred_op is not None and opname == preferred_op):
            return 1
        return 0

    def select_best_anchor(
        gid: int,
        op_sig: tuple[frozenset[str], bool, frozenset[str]],
        preferred_op: str | None,
    ) -> int:
        aids = group_anchors[gid]
        if len(aids) == 1:
            return aids[0]
        best_aid = aids[0]
        best_tier = -1
        for aid in aids:
            lid = anchor_info[aid][2]
            tier = line_match_tier(lid, op_sig, preferred_op)
            if tier > best_tier:
                best_tier = tier
                best_aid = aid
        return best_aid

    def find_directed_ge_path(
        cand_from: list[tuple[int, float]],
        cand_to: list[tuple[int, float]],
        op_sig: tuple[frozenset[str], bool, frozenset[str]],
        preferred_op: str | None,
    ) -> tuple[float, int, int, int, int, list[tuple[int, int]], int]:
        g_from_rep = cand_from[0][0]
        g_to_rep = cand_to[0][0]
        lon1, lat1 = float(n02_groups[g_from_rep][2]), float(n02_groups[g_from_rep][3])  # type: ignore[arg-type]
        lon2, lat2 = float(n02_groups[g_to_rep][2]), float(n02_groups[g_to_rep][3])  # type: ignore[arg-type]
        straight_d = fast_distance_m(lon1, lat1, lon2, lat2)

        if len(cand_from) == 1 and len(cand_to) == 1 and g_from_rep == g_to_rep:
            aid = select_best_anchor(g_from_rep, op_sig, preferred_op)
            return max(100.0, straight_d), aid, aid, g_from_rep, g_from_rep, [], 0

        all_lons = [float(n02_groups[g][2]) for g, _ in cand_from + cand_to]  # type: ignore[arg-type]
        all_lats = [float(n02_groups[g][3]) for g, _ in cand_from + cand_to]  # type: ignore[arg-type]
        min_lon = min(all_lons) - 0.18
        max_lon = max(all_lons) + 0.18
        min_lat = min(all_lats) - 0.18
        max_lat = max(all_lats) + 0.18
        max_cost = max(60000.0, straight_d * 5.0)
        fallback_cap = min(max_cost, max(8000.0, straight_d * 6.0))

        def endpoint_penalties(cands: list[tuple[int, float]]) -> list[tuple[int, int, int, float]]:
            raw: list[tuple[int, int, int, int, float]] = []
            max_tier = 0
            for gid, gd in cands:
                for aid in group_anchors[gid]:
                    lid = anchor_info[aid][2]
                    tier = line_match_tier(lid, op_sig, preferred_op)
                    if tier > max_tier:
                        max_tier = tier
                    raw.append((gid, aid, anchor_info[aid][5], tier, gd))
            out: list[tuple[int, int, int, float]] = []
            for gid, aid, nid, tier, gd in raw:
                if max_tier >= 2:
                    if tier >= 2:
                        pen = 0.0
                    elif tier == 1:
                        pen = 1500.0
                    else:
                        pen = 3000.0
                elif max_tier == 1:
                    pen = 0.0 if tier >= 1 else 2500.0
                else:
                    pen = 0.0
                out.append((gid, aid, nid, pen + gd * 0.05))
            return out

        starts = endpoint_penalties(cand_from)
        targets = endpoint_penalties(cand_to)
        target_by_nid: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
        for gid, aid, nid, pen in targets:
            target_by_nid[nid].append((gid, aid, pen))

        TARGET_NODE = -1
        dist: dict[tuple[int, int], float] = {}
        parent: dict[tuple[int, int], tuple[int, int, int | None, int]] = {}
        # heap entry: (cost, node_id, has_ge, start_aid, start_gid, end_aid, end_gid, end_nid)
        heap: list[tuple[float, int, int, int, int, int, int, int]] = []
        for sgid, said, snid, spen in starts:
            key = (snid, 0)
            if spen < dist.get(key, 1e18):
                dist[key] = spen
                heapq.heappush(heap, (spen, snid, 0, said, sgid, -1, -1, -1))

        def build_result(
            u_end: int,
            end_has_ge: int,
            s_aid: int,
            s_gid: int,
            e_aid: int,
            e_gid: int,
        ) -> tuple[float, int, int, int, int, list[tuple[int, int]], int]:
            ge_refs: list[tuple[int, int]] = []
            visited_groups: set[int] = set()
            curr = u_end
            curr_ge = end_has_ge
            while (curr, curr_ge) in parent:
                prev, prev_ge, ge_id, fwd = parent[(curr, curr_ge)]
                if ge_id is not None:
                    ge_refs.append((ge_id, fwd))
                if curr in node_to_group:
                    visited_groups.add(node_to_group[curr])
                curr, curr_ge = prev, prev_ge
            ge_refs.reverse()
            visited_groups.discard(s_gid)
            visited_groups.discard(e_gid)
            real_dist = sum(ge_distance[ge_id] for ge_id, _ in ge_refs)
            if real_dist <= 0.0:
                lon_a, lat_a = float(n02_groups[s_gid][2]), float(n02_groups[s_gid][3])  # type: ignore[arg-type]
                lon_b, lat_b = float(n02_groups[e_gid][2]), float(n02_groups[e_gid][3])  # type: ignore[arg-type]
                real_dist = max(100.0, fast_distance_m(lon_a, lat_a, lon_b, lat_b))
            return real_dist, s_aid, e_aid, s_gid, e_gid, ge_refs, len(visited_groups)

        fallback_res: tuple[float, int, int, int, int, list[tuple[int, int]], int] | None = None
        tier_mult = (2.2, 1.0, 0.50, 0.40)

        while heap:
            d, u, has_ge, s_aid, s_gid, e_aid, e_gid, end_nid = heapq.heappop(heap)
            if fallback_res is not None and d > fallback_cap:
                return fallback_res
            if u == TARGET_NODE:
                res = build_result(end_nid, has_ge, s_aid, s_gid, e_aid, e_gid)
                if has_ge == 1:
                    return res
                if fallback_res is None:
                    fallback_res = res
                continue

            if d > dist.get((u, has_ge), 1e18):
                continue

            if u in target_by_nid:
                for tgid, taid, tpen in target_by_nid[u]:
                    total_d = d + tpen
                    if total_d <= max_cost:
                        heapq.heappush(
                            heap,
                            (total_d, TARGET_NODE, has_ge, s_aid, s_gid, taid, tgid, u),
                        )

            for v, w, ge_id, fwd, lid in ge_adj.get(u, ()):
                _, vlon, vlat, _ = node_info[v]
                if not (min_lon <= vlon <= max_lon and min_lat <= vlat <= max_lat):
                    continue
                mult = 1.0
                if ge_id is not None:
                    mult = tier_mult[line_match_tier(lid, op_sig, preferred_op)]
                next_has_ge = 1 if (has_ge or ge_id is not None) else 0
                nd = d + max(w, 0.01) * mult
                limit = fallback_cap if fallback_res is not None else max_cost
                if nd < dist.get((v, next_has_ge), 1e18) and nd <= limit:
                    dist[(v, next_has_ge)] = nd
                    parent[(v, next_has_ge)] = (u, has_ge, ge_id, fwd)
                    heapq.heappush(
                        heap, (nd, v, next_has_ge, s_aid, s_gid, -1, -1, -1)
                    )

        if fallback_res is not None:
            return fallback_res

        aid_from = select_best_anchor(g_from_rep, op_sig, preferred_op)
        aid_to = select_best_anchor(g_to_rep, op_sig, preferred_op)
        return max(100.0, straight_d), aid_from, aid_to, g_from_rep, g_to_rep, [], 0

    # 3. Build operational_line, operational_line_stop, operational_edge, operational_edge_has_graph_edge
    op_lines_rows: list[tuple[object, ...]] = []
    op_stops_rows: list[tuple[object, ...]] = []
    op_edges_rows: list[tuple[object, ...]] = []
    op_edge_ge_rows: list[tuple[int, int, int, int, int]] = []

    next_op_edge_id = 1
    next_op_edge_ge_id = 1
    train_speed_mps = DEFAULT_PARAMS.train_speed_mps
    stop_penalty_s = DEFAULT_PARAMS.train_stop_penalty_s

    for op_line_id, lcd in enumerate(sorted(eki_lines.keys(), key=int), start=1):
        lmeta = eki_lines[lcd]
        line_name = lmeta["line_name"]
        norm_op_lname = normalize_name(line_name)
        op_sig = extract_op_line_signature(norm_op_lname)
        raw_stops = sorted(
            stations_by_line.get(lcd, []),
            key=lambda r: (int(r["e_sort"] or 0), int(r["station_cd"])),
        )
        mapped_stops = [r for r in raw_stops if r["station_cd"] in eki_to_candidates]
        if not mapped_stops:
            continue

        # Infer primary N02 operator for this line
        op_counter: Counter[str] = Counter()
        for r in mapped_stops:
            for gid, _ in eki_to_candidates[r["station_cd"]]:
                for aid in group_anchors[gid]:
                    lid = anchor_info[aid][2]
                    opname = n02_lines[lid][3]
                    tier = line_match_tier(lid, op_sig, None)
                    if tier >= 2:
                        op_counter[opname] += 6
                    elif tier == 1:
                        op_counter[opname] += 4
                    else:
                        op_counter[opname] += 1
        preferred_op = op_counter.most_common(1)[0][0] if op_counter else None
        operator_name = infer_operator_name(line_name, op_counter)

        # Contract any unmapped intermediate stations in join pairs
        raw_pairs = joins_by_line.get(lcd, [])
        unmapped_adj: dict[str, list[str]] = defaultdict(list)
        for s1, s2 in raw_pairs:
            unmapped_adj[s1].append(s2)
            unmapped_adj[s2].append(s1)

        effective_pairs: list[tuple[str, str]] = []
        seen_unordered: set[tuple[str, str]] = set()
        for s1, s2 in raw_pairs:
            if s1 in eki_to_candidates and s2 in eki_to_candidates:
                key = (min(s1, s2), max(s1, s2))
                if key not in seen_unordered:
                    seen_unordered.add(key)
                    effective_pairs.append((s1, s2))
            elif s1 in eki_to_candidates and s2 not in eki_to_candidates:
                for s3 in unmapped_adj[s2]:
                    if s3 != s1 and s3 in eki_to_candidates:
                        key = (min(s1, s3), max(s1, s3))
                        if key not in seen_unordered:
                            seen_unordered.add(key)
                            effective_pairs.append((s1, s3))

        # Check if loop line (number of unique mapped stops == number of effective joins)
        stop_deg: Counter[str] = Counter()
        for s1, s2 in effective_pairs:
            stop_deg[s1] += 1
            stop_deg[s2] += 1
        is_loop = int(
            len(mapped_stops) >= 3
            and len(effective_pairs) >= len(mapped_stops)
            and all(stop_deg[r["station_cd"]] >= 2 for r in mapped_stops)
        )

        # Pass 1: resolve directed paths over candidate N02 station_groups
        pair_results: list[
            tuple[
                str,
                str,
                tuple[float, int, int, int, int, list[tuple[int, int]], int],
                tuple[float, int, int, int, int, list[tuple[int, int]], int],
            ]
        ] = []
        stop_group_votes: dict[str, Counter[int]] = defaultdict(Counter)
        for s1, s2 in effective_pairs:
            c1, c2 = eki_to_candidates[s1], eki_to_candidates[s2]
            fwd_res = find_directed_ge_path(c1, c2, op_sig, preferred_op)
            bwd_res = find_directed_ge_path(c2, c1, op_sig, preferred_op)
            pair_results.append((s1, s2, fwd_res, bwd_res))
            _, _, _, g1_f, g2_f, ge_f, _ = fwd_res
            _, _, _, g2_b, g1_b, ge_b, _ = bwd_res
            stop_group_votes[s1][g1_f] += 4 if ge_f else 1
            stop_group_votes[s2][g2_f] += 4 if ge_f else 1
            stop_group_votes[s2][g2_b] += 2 if ge_b else 1
            stop_group_votes[s1][g1_b] += 2 if ge_b else 1

        chosen_group: dict[str, int] = {}
        for r in mapped_stops:
            scd = r["station_cd"]
            if stop_group_votes[scd]:
                chosen_group[scd] = stop_group_votes[scd].most_common(1)[0][0]
            else:
                chosen_group[scd] = eki_to_candidates[scd][0][0]

        # Pass 2: enforce per-stop group consistency whenever a multi-group stop voted differently
        stop_anchor_votes: dict[str, Counter[int]] = defaultdict(Counter)
        pair_dist_lookup: dict[tuple[str, str], float] = {}
        line_total_distance_m = 0.0
        seq_no = 0
        has_skipped = False

        for s1, s2, fwd_res, bwd_res in pair_results:
            cg1, cg2 = chosen_group[s1], chosen_group[s2]
            dist_fwd, a1_fwd, a2_fwd, g1_f, g2_f, ge_fwd, skip_fwd = fwd_res
            if g1_f != cg1 or g2_f != cg2:
                dist_fwd, a1_fwd, a2_fwd, _, _, ge_fwd, skip_fwd = find_directed_ge_path(
                    [(cg1, 0.0)], [(cg2, 0.0)], op_sig, preferred_op
                )
            dist_bwd, a2_bwd, a1_bwd, g2_b, g1_b, ge_bwd, skip_bwd = bwd_res
            if g2_b != cg2 or g1_b != cg1:
                dist_bwd, a2_bwd, a1_bwd, _, _, ge_bwd, skip_bwd = find_directed_ge_path(
                    [(cg2, 0.0)], [(cg1, 0.0)], op_sig, preferred_op
                )

            stop_anchor_votes[s1][a1_fwd] += 2
            stop_anchor_votes[s2][a2_fwd] += 2
            stop_anchor_votes[s2][a2_bwd] += 1
            stop_anchor_votes[s1][a1_bwd] += 1
            pair_dist_lookup[(s1, s2)] = dist_fwd
            pair_dist_lookup[(s2, s1)] = dist_bwd

            line_total_distance_m += dist_fwd
            if skip_fwd > 0 or skip_bwd > 0:
                has_skipped = True

            for from_scd, to_scd, dm, ge_refs, skipped in (
                (s1, s2, dist_fwd, ge_fwd, skip_fwd),
                (s2, s1, dist_bwd, ge_bwd, skip_bwd),
            ):
                cost_s = round((dm / train_speed_mps) + stop_penalty_s, 2)
                op_edges_rows.append(
                    (
                        next_op_edge_id,
                        op_line_id,
                        seq_no,
                        int(from_scd),
                        int(to_scd),
                        "forward",
                        round(dm, 3),
                        cost_s,
                        skipped,
                    )
                )
                for ge_seq, (ge_id, fwd) in enumerate(ge_refs):
                    op_edge_ge_rows.append(
                        (next_op_edge_ge_id, next_op_edge_id, ge_seq, ge_id, fwd)
                    )
                    next_op_edge_ge_id += 1
                next_op_edge_id += 1
                seq_no += 1

        # Build operational_line_stop rows
        cum_dist = 0.0
        prev_scd: str | None = None
        for idx, r in enumerate(mapped_stops, start=1):
            scd = r["station_cd"]
            if prev_scd is not None and (prev_scd, scd) in pair_dist_lookup:
                cum_dist += pair_dist_lookup[(prev_scd, scd)]
            prev_scd = scd
            gid = chosen_group[scd]
            if stop_anchor_votes[scd]:
                chosen_aid = stop_anchor_votes[scd].most_common(1)[0][0]
            else:
                chosen_aid = select_best_anchor(gid, op_sig, preferred_op)
            pref_code = (r.get("pref_cd") or "").strip().zfill(2)
            stop_region = classify_pref_region(pref_code)
            op_stops_rows.append(
                (
                    int(scd),
                    op_line_id,
                    idx,
                    chosen_aid,
                    r["station_name"],
                    pref_code,
                    stop_region,
                    round(cum_dist, 3),
                    0.0,
                    0.0,
                    None,
                    None,
                )
            )

        service_kind = "rapid_or_through" if has_skipped else "local"
        op_lines_rows.append(
            (
                op_line_id,
                lcd,
                line_name,
                operator_name,
                lmeta.get("line_color_c") or None,
                service_kind,
                is_loop,
                len(mapped_stops),
                round(line_total_distance_m, 3),
            )
        )

    connection.executemany(
        "INSERT INTO operational_line VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        op_lines_rows,
    )
    connection.executemany(
        "INSERT INTO operational_line_stop VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        op_stops_rows,
    )
    connection.executemany(
        "INSERT INTO operational_edge VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        op_edges_rows,
    )
    connection.executemany(
        "INSERT INTO operational_edge_has_graph_edge VALUES (?, ?, ?, ?, ?)",
        op_edge_ge_rows,
    )

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

    return {
        "operational_line": len(op_lines_rows),
        "operational_line_stop": len(op_stops_rows),
        "operational_edge": len(op_edges_rows),
        "operational_edge_has_graph_edge": len(op_edge_ge_rows),
    }
