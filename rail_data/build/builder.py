"""Transform parsed N02 features into the in-memory database model."""

from __future__ import annotations

import heapq
import math
from collections import Counter, defaultdict, deque

from .corrections import apply_corrections
from .directions import assign_left_running_parallel_directions
from .geometry import (
    add_split,
    coord_key,
    curve_signature,
    grid_cell_for_point,
    grid_cells_for_segment,
    midpoint_along,
    part_signature,
    point_parameter,
    polyline_length,
    segment_length,
    split_priority,
)
from .models import (
    AnchorRow,
    ComponentDraft,
    ConnectionRow,
    Correction,
    Coord,
    GraphEdgeRow,
    ImportIssue,
    LineComponentRow,
    NodeRow,
    RawSection,
    RawSegment,
    RawStation,
    RouteKey,
    SegmentRow,
    StationGroupRow,
    StationRow,
)


def choose_station_sections(
    station: RawStation,
    curves: dict[str, list[list[Coord]]],
    section_by_id: dict[str, RawSection],
    sections_by_route_signature: dict[
        tuple[RouteKey, tuple[tuple[Coord, ...], ...]], list[RawSection]
    ],
) -> list[RawSection]:
    station_parts = curves.get(station.curve_id, [])
    signature = curve_signature(station_parts)
    direct = [
        section_by_id[section_id]
        for section_id in station.section_refs
        if section_id in section_by_id
        and section_by_id[section_id].route_key == station.route_key
        and curve_signature(curves.get(section_by_id[section_id].curve_id, []))
        == signature
    ]
    if direct:
        return direct
    return sections_by_route_signature.get((station.route_key, signature), [])

def build_database_model(
    curves: dict[str, list[list[Coord]]],
    raw_sections: list[RawSection],
    raw_stations: list[RawStation],
    selected_line_names: set[str] | None,
    point_tolerance: float,
    corrections: list[Correction],
) -> dict[str, object]:
    issues: list[ImportIssue] = []
    selected_sections = [
        section
        for section in raw_sections
        if selected_line_names is None or section.route_key.name in selected_line_names
    ]
    route_keys = sorted({section.route_key for section in selected_sections})
    line_id_by_key = {key: index for index, key in enumerate(route_keys, start=1)}
    selected_route_keys = set(route_keys)

    section_by_id = {section.source_id: section for section in selected_sections}
    sections_by_route_signature: dict[
        tuple[RouteKey, tuple[tuple[Coord, ...], ...]], list[RawSection]
    ] = defaultdict(list)
    for section in selected_sections:
        parts = curves.get(section.curve_id, [])
        sections_by_route_signature[(section.route_key, curve_signature(parts))].append(
            section
        )

    selected_stations = [
        station for station in raw_stations if station.route_key in selected_route_keys
    ]
    group_codes = sorted({station.group_code for station in selected_stations})
    group_id_by_code = {code: index for index, code in enumerate(group_codes, start=1)}
    group_name_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for station in selected_stations:
        group_name_counts[station.group_code][station.name] += 1

    station_rows: list[StationRow] = []
    component_drafts: list[ComponentDraft] = []
    components_by_section_part: dict[tuple[str, int], list[ComponentDraft]] = (
        defaultdict(list)
    )
    next_component_id = 1

    for station_id, raw_station in enumerate(selected_stations, start=1):
        parts = curves.get(raw_station.curve_id, [])
        matched_sections = choose_station_sections(
            raw_station, curves, section_by_id, sections_by_route_signature
        )
        matched_part_lookup: list[tuple[RawSection, int, bool] | None] = []
        for station_part in parts:
            signature = part_signature(station_part)
            found: tuple[RawSection, int, bool] | None = None
            for section in matched_sections:
                for part_no, section_part in enumerate(
                    curves.get(section.curve_id, [])
                ):
                    if part_signature(section_part) != signature:
                        continue
                    section_forward = tuple(map(coord_key, station_part)) == tuple(
                        map(coord_key, section_part)
                    )
                    found = (section, part_no, section_forward)
                    break
                if found is not None:
                    break
            matched_part_lookup.append(found)

        geometry_length = sum(polyline_length(part) for part in parts)
        if not parts:
            geometry_status = "invalid"
        elif len(parts) == 1:
            geometry_status = "simple"
        else:
            geometry_status = "multi_component"
        station_rows.append(
            StationRow(
                id=station_id,
                source_id=raw_station.source_id,
                station_code=raw_station.station_code,
                group_id=group_id_by_code[raw_station.group_code],
                line_id=line_id_by_key[raw_station.route_key],
                name=raw_station.name,
                geometry_length_m=geometry_length,
                geometry_status=geometry_status,
            )
        )

        for part_no, points in enumerate(parts):
            midpoint, _, _, position_m = midpoint_along(points)
            matched = (
                matched_part_lookup[part_no]
                if part_no < len(matched_part_lookup)
                else None
            )
            component = ComponentDraft(
                id=next_component_id,
                station_id=station_id,
                line_id=line_id_by_key[raw_station.route_key],
                component_no=part_no,
                points=points,
                length_m=polyline_length(points),
                midpoint=midpoint,
                position_m=position_m,
                geometry_class="simple_path",
                anchor_status="ready" if matched is not None else "manual_required",
                matched_section_id=None if matched is None else matched[0].source_id,
                matched_part_no=None if matched is None else matched[1],
                section_forward=True if matched is None else matched[2],
            )
            component_drafts.append(component)
            if matched is not None:
                components_by_section_part[(matched[0].source_id, matched[1])].append(
                    component
                )
            else:
                issues.append(
                    ImportIssue(
                        "station",
                        "warning",
                        "station_component",
                        next_component_id,
                        "STATION_SECTION_NOT_MATCHED",
                        f"駅曲線を同じ路線の RailroadSection に対応付けできません: {raw_station.name}",
                        {
                            "station_id": station_id,
                            "source_id": raw_station.source_id,
                            "station_code": raw_station.station_code,
                            "group_code": raw_station.group_code,
                            "station_name": raw_station.name,
                            "line_name": raw_station.route_key.name,
                            "operator_name": raw_station.route_key.operator_name,
                        },
                    )
                )
            next_component_id += 1

    raw_segments: list[RawSegment] = []
    raw_segment_by_id: dict[int, RawSegment] = {}
    raw_ids_by_section_part: dict[tuple[str, int], list[int]] = defaultdict(list)
    raw_ids_by_line: dict[int, list[int]] = defaultdict(list)

    for section in selected_sections:
        line_id = line_id_by_key[section.route_key]
        for part_no, points in enumerate(curves.get(section.curve_id, [])):
            for start, end in zip(points, points[1:]):
                if coord_key(start) == coord_key(end):
                    issues.append(
                        ImportIssue(
                            "topology",
                            "warning",
                            None,
                            None,
                            "ZERO_LENGTH_SOURCE_SEGMENT",
                            "原始曲線に長さ0の線分があります。",
                            {"section": section.source_id, "part": part_no},
                        )
                    )
                    continue
                raw_id = len(raw_segments) + 1
                raw_segment = RawSegment(
                    raw_id,
                    line_id,
                    coord_key(start),
                    coord_key(end),
                )
                raw_segments.append(raw_segment)
                raw_segment_by_id[raw_id] = raw_segment
                raw_ids_by_section_part[(section.source_id, part_no)].append(raw_id)
                raw_ids_by_line[line_id].append(raw_id)

    for component in component_drafts:
        if component.matched_section_id is None or component.matched_part_no is None:
            continue
        raw_ids = raw_ids_by_section_part[
            (component.matched_section_id, component.matched_part_no)
        ]
        component.raw_segment_ids = (
            raw_ids if component.section_forward else list(reversed(raw_ids))
        )
        _, station_segment_index, station_t, _ = midpoint_along(component.points)
        if component.section_forward:
            raw_index = station_segment_index
            raw_t = station_t
        else:
            raw_index = len(raw_ids) - 1 - station_segment_index
            raw_t = 1 - station_t
        if 0 <= raw_index < len(raw_ids):
            add_split(
                raw_segment_by_id[raw_ids[raw_index]],
                raw_t,
                component.midpoint,
                "station_anchor",
            )

    for line_id, raw_ids in raw_ids_by_line.items():
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        section_endpoints: set[Coord] = set()
        for raw_id in raw_ids:
            raw = raw_segment_by_id[raw_id]
            for cell in grid_cells_for_segment(raw.start, raw.end):
                grid[cell].append(raw_id)
        for (section_id, part_no), part_raw_ids in raw_ids_by_section_part.items():
            if (
                not part_raw_ids
                or raw_segment_by_id[part_raw_ids[0]].line_id != line_id
            ):
                continue
            section_endpoints.add(raw_segment_by_id[part_raw_ids[0]].start)
            section_endpoints.add(raw_segment_by_id[part_raw_ids[-1]].end)

        for point in section_endpoints:
            for raw_id in grid.get(grid_cell_for_point(point), []):
                raw = raw_segment_by_id[raw_id]
                t, distance = point_parameter(point, raw.start, raw.end)
                if distance <= point_tolerance and 1e-10 < t < 1 - 1e-10:
                    add_split(raw, t, point, "split")

    nodes: list[NodeRow] = []
    node_by_id: dict[int, NodeRow] = {}
    node_id_by_key: dict[tuple[int, Coord], int] = {}
    segments: list[SegmentRow] = []
    segment_by_id: dict[int, SegmentRow] = {}
    segment_id_by_key: dict[tuple[int, int, int], int] = {}
    raw_piece_refs: dict[int, list[tuple[int, int]]] = defaultdict(list)

    def get_node(line_id: int, point: Coord, method: str) -> int:
        key = (line_id, coord_key(point))
        existing = node_id_by_key.get(key)
        if existing is not None:
            node = node_by_id[existing]
            if split_priority(method) > split_priority(node.creation_method):
                node.creation_method = method
            return existing
        node_id = len(nodes) + 1
        node = NodeRow(node_id, line_id, key[1][0], key[1][1], creation_method=method)
        nodes.append(node)
        node_by_id[node_id] = node
        node_id_by_key[key] = node_id
        return node_id

    for raw in raw_segments:
        split_map: dict[float, tuple[Coord, str]] = {
            0.0: (raw.start, "source_vertex"),
            1.0: (raw.end, "source_vertex"),
        }
        for t, point, method in raw.splits:
            rounded_t = round(t, 12)
            current = split_map.get(rounded_t)
            if current is None or split_priority(method) > split_priority(current[1]):
                split_map[rounded_t] = (point, method)
        ordered = sorted((t, point, method) for t, (point, method) in split_map.items())
        for (_, start, start_method), (_, end, end_method) in zip(ordered, ordered[1:]):
            start_node = get_node(raw.line_id, start, start_method)
            end_node = get_node(raw.line_id, end, end_method)
            if start_node == end_node:
                continue
            canonical_start, canonical_end = sorted((start_node, end_node))
            segment_key = (raw.line_id, canonical_start, canonical_end)
            segment_id = segment_id_by_key.get(segment_key)
            if segment_id is None:
                length_m = segment_length(start, end)
                if length_m <= 0:
                    continue
                segment_id = len(segments) + 1
                segment = SegmentRow(
                    segment_id,
                    raw.line_id,
                    canonical_start,
                    canonical_end,
                    length_m,
                )
                segments.append(segment)
                segment_by_id[segment_id] = segment
                segment_id_by_key[segment_key] = segment_id
            segment = segment_by_id[segment_id]
            forward = int(
                segment.from_node_id == start_node and segment.to_node_id == end_node
            )
            raw_piece_refs[raw.id].append((segment_id, forward))

    component_segment_sets: dict[int, set[int]] = defaultdict(set)
    for component in component_drafts:
        refs: list[tuple[int, int]] = []
        if component.section_forward:
            for raw_id in component.raw_segment_ids:
                refs.extend(raw_piece_refs[raw_id])
        else:
            for raw_id in component.raw_segment_ids:
                refs.extend(
                    (segment_id, 1 - forward)
                    for segment_id, forward in reversed(raw_piece_refs[raw_id])
                )
        for segment_id, _forward in refs:
            component_segment_sets[component.id].add(segment_id)

    anchors: list[AnchorRow] = []
    anchor_by_id: dict[int, AnchorRow] = {}
    for component in component_drafts:
        node_id = node_id_by_key.get((component.line_id, coord_key(component.midpoint)))
        if node_id is None or component.anchor_status != "ready":
            continue
        anchor = AnchorRow(
            id=len(anchors) + 1,
            station_component_id=component.id,
            node_id=node_id,
            anchor_no=0,
            method="half_arc",
            position_m=component.position_m,
            is_primary=1,
            status="confirmed",
        )
        anchors.append(anchor)
        anchor_by_id[anchor.id] = anchor

    adjacency_by_line: dict[int, dict[int, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    segments_by_line: dict[int, list[int]] = defaultdict(list)
    for segment in segments:
        adjacency_by_line[segment.line_id][segment.from_node_id].append(segment.id)
        adjacency_by_line[segment.line_id][segment.to_node_id].append(segment.id)
        segments_by_line[segment.line_id].append(segment.id)

    line_components: list[LineComponentRow] = []
    segments_by_component: dict[int, list[int]] = defaultdict(list)
    nodes_by_component: dict[int, set[int]] = defaultdict(set)
    for line_id in sorted(adjacency_by_line):
        adjacency = adjacency_by_line[line_id]
        unseen = set(adjacency)
        component_no = 0
        while unseen:
            start = min(unseen)
            queue = deque([start])
            component_nodes: set[int] = set()
            component_segments: set[int] = set()
            while queue:
                node_id = queue.popleft()
                if node_id in component_nodes:
                    continue
                component_nodes.add(node_id)
                unseen.discard(node_id)
                for segment_id in adjacency[node_id]:
                    component_segments.add(segment_id)
                    segment = segment_by_id[segment_id]
                    other = (
                        segment.to_node_id
                        if segment.from_node_id == node_id
                        else segment.from_node_id
                    )
                    if other not in component_nodes:
                        queue.append(other)
            component_id = len(line_components) + 1
            line_components.append(
                LineComponentRow(
                    component_id,
                    line_id,
                    component_no,
                    len(component_nodes),
                    len(component_segments),
                    "ok",
                )
            )
            for node_id in component_nodes:
                node_by_id[node_id].line_component_id = component_id
            for segment_id in component_segments:
                segment_by_id[segment_id].line_component_id = component_id
            nodes_by_component[component_id] = component_nodes
            segments_by_component[component_id] = sorted(component_segments)
            component_no += 1

    for line_id, adjacency in adjacency_by_line.items():
        for node_id, incident in adjacency.items():
            degree = len(set(incident))
            node_by_id[node_id].topology_type = (
                "isolated"
                if degree == 0
                else "terminal"
                if degree == 1
                else "shape"
                if degree == 2
                else "junction"
            )

    anchor_nodes = {anchor.node_id for anchor in anchors}
    graph_edges: list[GraphEdgeRow] = []
    graph_edge_by_id: dict[int, GraphEdgeRow] = {}
    for line_component in line_components:
        component_id = line_component.id
        component_nodes = nodes_by_component[component_id]
        component_segment_ids = set(segments_by_component[component_id])
        adjacency = adjacency_by_line[line_component.line_id]
        important_nodes = {
            node_id
            for node_id in component_nodes
            if node_id in anchor_nodes or len(set(adjacency[node_id])) != 2
        }
        if not important_nodes and component_nodes:
            important_nodes.add(min(component_nodes))

        visited_segments: set[int] = set()
        for start_node in sorted(important_nodes):
            for first_segment_id in sorted(adjacency[start_node]):
                if (
                    first_segment_id not in component_segment_ids
                    or first_segment_id in visited_segments
                ):
                    continue
                path_refs: list[tuple[int, int]] = []
                current_node = start_node
                current_segment_id = first_segment_id
                while True:
                    if current_segment_id in visited_segments:
                        break
                    visited_segments.add(current_segment_id)
                    segment = segment_by_id[current_segment_id]
                    forward = int(segment.from_node_id == current_node)
                    path_refs.append((current_segment_id, forward))
                    next_node = segment.to_node_id if forward else segment.from_node_id
                    if next_node in important_nodes:
                        end_node = next_node
                        break
                    candidates = [
                        segment_id
                        for segment_id in adjacency[next_node]
                        if segment_id != current_segment_id
                        and segment_id in component_segment_ids
                    ]
                    if not candidates:
                        end_node = next_node
                        break
                    current_node = next_node
                    current_segment_id = candidates[0]
                if not path_refs:
                    continue
                edge_id = len(graph_edges) + 1
                distance_m = sum(
                    segment_by_id[segment_id].length_m for segment_id, _ in path_refs
                )
                graph_edge = GraphEdgeRow(
                    edge_id,
                    component_id,
                    start_node,
                    end_node,
                    distance_m,
                    path_refs,
                )
                graph_edges.append(graph_edge)
                graph_edge_by_id[edge_id] = graph_edge

        missing_segments = component_segment_ids - visited_segments
        for segment_id in sorted(missing_segments):
            segment = segment_by_id[segment_id]
            edge_id = len(graph_edges) + 1
            graph_edge = GraphEdgeRow(
                edge_id,
                component_id,
                segment.from_node_id,
                segment.to_node_id,
                segment.length_m,
                [(segment_id, 1)],
            )
            graph_edges.append(graph_edge)
            graph_edge_by_id[edge_id] = graph_edge
            issues.append(
                ImportIssue(
                    "topology",
                    "warning",
                    "graph_edge",
                    edge_id,
                    "UNCOMPRESSED_SEGMENT_FALLBACK",
                    "通常の連鎖圧縮で回収できなかった線分を1辺として保存しました。",
                )
            )

    applied_corrections, skipped_corrections = apply_corrections(
        corrections,
        nodes,
        segments,
        anchors,
        line_components,
        graph_edges,
        allow_unavailable=selected_line_names is not None,
    )
    graph_edge_by_id = {edge.id: edge for edge in graph_edges}
    parallel_direction_assignments, parallel_direction_skips = (
        assign_left_running_parallel_directions(
            nodes,
            segments,
            anchors,
            graph_edges,
        )
    )

    graph_adjacency: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for edge in graph_edges:
        if edge.direction in ("both", "forward"):
            graph_adjacency[edge.from_node_id].append(
                (edge.id, edge.to_node_id, 1)
            )
        if edge.direction in ("both", "backward"):
            graph_adjacency[edge.to_node_id].append(
                (edge.id, edge.from_node_id, 0)
            )

    anchors_at_node: dict[int, list[AnchorRow]] = defaultdict(list)
    component_by_anchor: dict[int, int] = {}
    for anchor in anchors:
        anchors_at_node[anchor.node_id].append(anchor)
        component_by_anchor[anchor.id] = anchor.station_component_id

    def atomic_refs_for_edge_path(
        edge_refs: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        for edge_id, edge_forward in edge_refs:
            refs = graph_edge_by_id[edge_id].segment_refs
            if edge_forward:
                result.extend(refs)
            else:
                result.extend(
                    (segment_id, 1 - forward) for segment_id, forward in reversed(refs)
                )
        return result

    def make_connection(
        start_anchor: AnchorRow,
        target_anchor: AnchorRow,
        line_component_id: int,
        edge_path: list[tuple[int, int]],
    ) -> ConnectionRow:
        atomic_refs = atomic_refs_for_edge_path(edge_path)
        total_distance = sum(
            segment_by_id[segment_id].length_m for segment_id, _ in atomic_refs
        )
        from_members = component_segment_sets[component_by_anchor[start_anchor.id]]
        to_members = component_segment_sets[component_by_anchor[target_anchor.id]]
        prefix_count = 0
        from_offset = 0.0
        for segment_id, _ in atomic_refs:
            if segment_id not in from_members:
                break
            from_offset += segment_by_id[segment_id].length_m
            prefix_count += 1
        to_offset = 0.0
        for segment_id, _ in reversed(atomic_refs[prefix_count:]):
            if segment_id not in to_members:
                break
            to_offset += segment_by_id[segment_id].length_m
        gap_length = max(0.0, total_distance - from_offset - to_offset)
        return ConnectionRow(
            id=0,
            line_component_id=line_component_id,
            from_anchor_id=start_anchor.id,
            to_anchor_id=target_anchor.id,
            from_station_offset_m=from_offset,
            to_station_offset_m=to_offset,
            gap_length_m=gap_length,
            distance_m=total_distance,
            edge_refs=edge_path,
        )

    # Keep both ordered directions.  This makes a directional distance lookup
    # an indexed equality query on (from_anchor_id, to_anchor_id), even when
    # the two directions use different sides of a separated track or tunnel.
    connections_by_ordered_pair: dict[tuple[int, int], ConnectionRow] = {}
    for start_anchor in anchors:
        start_component_id = node_by_id[start_anchor.node_id].line_component_id
        if start_component_id is None:
            continue
        queue: list[tuple[float, int, int, list[tuple[int, int]]]] = [
            (0.0, 0, start_anchor.node_id, [])
        ]
        best_distance = {start_anchor.node_id: 0.0}
        settled_nodes: set[int] = set()
        serial = 0

        while queue:
            distance, _serial, current_node, path = heapq.heappop(queue)
            if current_node in settled_nodes:
                continue
            settled_nodes.add(current_node)

            if current_node != start_anchor.node_id:
                target_anchors = [
                    anchor
                    for anchor in anchors_at_node.get(current_node, [])
                    if anchor.id != start_anchor.id
                ]
                if target_anchors:
                    for target_anchor in target_anchors:
                        pair = (start_anchor.id, target_anchor.id)
                        connection = make_connection(
                            start_anchor,
                            target_anchor,
                            int(start_component_id),
                            path,
                        )
                        previous = connections_by_ordered_pair.get(pair)
                        if (
                            previous is None
                            or connection.distance_m < previous.distance_m - 1e-6
                        ):
                            connections_by_ordered_pair[pair] = connection
                    # A station is a terminal for adjacency discovery: never
                    # continue through it to a farther station.
                    continue

            for edge_id, next_node, edge_forward in graph_adjacency[current_node]:
                edge = graph_edge_by_id[edge_id]
                if edge.line_component_id != start_component_id:
                    continue
                next_distance = distance + edge.distance_m
                if next_distance >= best_distance.get(next_node, math.inf) - 1e-9:
                    continue
                best_distance[next_node] = next_distance
                serial += 1
                heapq.heappush(
                    queue,
                    (
                        next_distance,
                        serial,
                        next_node,
                        [*path, (edge_id, edge_forward)],
                    ),
                )

    connections = []
    for connection_id, pair in enumerate(
        sorted(connections_by_ordered_pair), start=1
    ):
        connection = connections_by_ordered_pair[pair]
        connection.id = connection_id
        connections.append(connection)

    group_rows: list[StationGroupRow] = []
    station_count_by_group = Counter(row.group_id for row in station_rows)
    for group_code in group_codes:
        group_id = group_id_by_code[group_code]
        counts = group_name_counts[group_code]
        display_name = counts.most_common(1)[0][0] if counts else group_code
        group_rows.append(
            StationGroupRow(
                id=group_id,
                group_code=group_code,
                display_name=display_name,
                station_count=station_count_by_group[group_id],
            )
        )

    return {
        "route_keys": route_keys,
        "group_rows": group_rows,
        "station_rows": station_rows,
        "components": component_drafts,
        "nodes": nodes,
        "segments": segments,
        "anchors": anchors,
        "line_components": line_components,
        "graph_edges": graph_edges,
        "connections": connections,
        "issues": issues,
        "applied_corrections": applied_corrections,
        "skipped_corrections": skipped_corrections,
        "parallel_direction_assignments": parallel_direction_assignments,
        "parallel_direction_skips": parallel_direction_skips,
    }
