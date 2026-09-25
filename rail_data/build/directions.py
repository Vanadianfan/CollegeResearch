"""Infer left-running directions for strict parallel-track corridors."""

from __future__ import annotations

import math
from collections import defaultdict

from .models import (
    AnchorRow,
    GraphEdgeRow,
    NodeRow,
    ParallelDirectionAssignment,
    ParallelDirectionSkip,
    SegmentRow,
)


def assign_left_running_parallel_directions(
    nodes: list[NodeRow],
    segments: list[SegmentRow],
    anchors: list[AnchorRow],
    graph_edges: list[GraphEdgeRow],
) -> tuple[list[ParallelDirectionAssignment], list[ParallelDirectionSkip]]:
    """Make strict two-edge, degree-3 railway bubbles left-running.

    Each endpoint must have exactly one external edge, so the incoming travel
    direction is unambiguous.  The left branch is chosen from the local
    junction tangents; whole-path side area is used only when the tangents are
    too close or disagree between the two ends.
    """
    node_by_id = {node.id: node for node in nodes}
    segment_by_id = {segment.id: segment for segment in segments}
    anchor_nodes = {anchor.node_id for anchor in anchors}
    incident_edges: dict[int, list[GraphEdgeRow]] = defaultdict(list)
    edges_by_node_pair: dict[tuple[int, int, int], list[GraphEdgeRow]] = defaultdict(
        list
    )
    for edge in graph_edges:
        incident_edges[edge.from_node_id].append(edge)
        incident_edges[edge.to_node_id].append(edge)
        if edge.from_node_id == edge.to_node_id:
            continue
        node_a_id, node_b_id = sorted((edge.from_node_id, edge.to_node_id))
        edges_by_node_pair[
            (edge.line_component_id, node_a_id, node_b_id)
        ].append(edge)

    def directed_segment_refs(
        edge: GraphEdgeRow, start_node_id: int
    ) -> list[tuple[int, int]]:
        if edge.from_node_id == start_node_id:
            return list(edge.segment_refs)
        if edge.to_node_id == start_node_id:
            return [
                (segment_id, 1 - forward)
                for segment_id, forward in reversed(edge.segment_refs)
            ]
        raise ValueError(
            f"graph_edge#{edge.id} は node#{start_node_id} に接続していません。"
        )

    def segment_endpoints(segment_id: int, forward: int) -> tuple[int, int]:
        segment = segment_by_id[segment_id]
        if forward:
            return segment.from_node_id, segment.to_node_id
        return segment.to_node_id, segment.from_node_id

    def outgoing_unit_vector(edge: GraphEdgeRow, node_id: int) -> tuple[float, float]:
        current_node_id = node_id
        for segment_id, forward in directed_segment_refs(edge, node_id):
            start_node_id, end_node_id = segment_endpoints(segment_id, forward)
            if start_node_id != current_node_id:
                raise ValueError(
                    f"graph_edge#{edge.id} の atomic_segment#{segment_id} が "
                    f"node#{current_node_id} から連続していません。"
                )
            start = node_by_id[start_node_id]
            end = node_by_id[end_node_id]
            mean_latitude = math.radians((start.lat + end.lat) / 2)
            dx = (end.lon - start.lon) * math.cos(mean_latitude)
            dy = end.lat - start.lat
            length = math.hypot(dx, dy)
            if length > 1e-14:
                return dx / length, dy / length
            current_node_id = end_node_id
        raise ValueError(f"graph_edge#{edge.id} の接線方向を計算できません。")

    def signed_turn_angle(
        incoming: tuple[float, float], outgoing: tuple[float, float]
    ) -> float:
        cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
        dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
        return math.atan2(cross, dot)

    def left_side_score(edge: GraphEdgeRow, start_node_id: int) -> float:
        node_ids = [start_node_id]
        current_node_id = start_node_id
        for segment_id, forward in directed_segment_refs(edge, start_node_id):
            segment_start, segment_end = segment_endpoints(segment_id, forward)
            if segment_start != current_node_id:
                raise ValueError(
                    f"graph_edge#{edge.id} の面積計算用線列が連続していません。"
                )
            node_ids.append(segment_end)
            current_node_id = segment_end
        origin = node_by_id[start_node_id]
        cosine = math.cos(math.radians(origin.lat))
        points = [
            (
                (node_by_id[node_id].lon - origin.lon) * cosine,
                node_by_id[node_id].lat - origin.lat,
            )
            for node_id in node_ids
        ]
        signed_area = sum(
            start[0] * end[1] - end[0] * start[1]
            for start, end in zip(points, points[1:])
        ) / 2
        # Closing the path from B back to A produces negative signed area when
        # the A->B path lies on the left side of travel.
        return -signed_area

    def set_travel_direction(
        edge: GraphEdgeRow, start_node_id: int, end_node_id: int
    ) -> None:
        if (edge.from_node_id, edge.to_node_id) == (start_node_id, end_node_id):
            edge.direction = "forward"
        elif (edge.to_node_id, edge.from_node_id) == (start_node_id, end_node_id):
            edge.direction = "backward"
        else:
            raise ValueError(
                f"graph_edge#{edge.id} の端点が指定方向と一致しません。"
            )

    assignments: list[ParallelDirectionAssignment] = []
    skipped: list[ParallelDirectionSkip] = []
    for (line_component_id, node_a_id, node_b_id), parallel_edges in sorted(
        edges_by_node_pair.items()
    ):
        if len(parallel_edges) != 2:
            continue
        node_a = node_by_id[node_a_id]
        node_b = node_by_id[node_b_id]
        if node_a.topology_type != "junction" or node_b.topology_type != "junction":
            continue
        if len(incident_edges[node_a_id]) != 3 or len(incident_edges[node_b_id]) != 3:
            continue
        if node_a_id in anchor_nodes or node_b_id in anchor_nodes:
            continue

        parallel_edge_ids = {edge.id for edge in parallel_edges}
        external_a = [
            edge
            for edge in incident_edges[node_a_id]
            if edge.id not in parallel_edge_ids
        ]
        external_b = [
            edge
            for edge in incident_edges[node_b_id]
            if edge.id not in parallel_edge_ids
        ]
        sorted_edge_ids = sorted(parallel_edge_ids)
        if len(sorted_edge_ids) != 2:
            continue
        edge_ids = (sorted_edge_ids[0], sorted_edge_ids[1])
        if len(external_a) != 1 or len(external_b) != 1:
            skipped.append(
                ParallelDirectionSkip(
                    line_component_id,
                    node_a_id,
                    node_b_id,
                    edge_ids,
                    "每端不是恰好一條外部連線",
                )
            )
            continue
        if any(edge.direction != "both" for edge in parallel_edges):
            skipped.append(
                ParallelDirectionSkip(
                    line_component_id,
                    node_a_id,
                    node_b_id,
                    edge_ids,
                    "平行連線已有方向限制",
                )
            )
            continue

        outgoing_external_a = outgoing_unit_vector(external_a[0], node_a_id)
        outgoing_external_b = outgoing_unit_vector(external_b[0], node_b_id)
        incoming_a = (-outgoing_external_a[0], -outgoing_external_a[1])
        incoming_b = (-outgoing_external_b[0], -outgoing_external_b[1])
        angles_a = {
            edge.id: signed_turn_angle(
                incoming_a, outgoing_unit_vector(edge, node_a_id)
            )
            for edge in parallel_edges
        }
        angles_b = {
            edge.id: signed_turn_angle(
                incoming_b, outgoing_unit_vector(edge, node_b_id)
            )
            for edge in parallel_edges
        }
        separation_a = abs(
            math.atan2(
                math.sin(angles_a[edge_ids[0]] - angles_a[edge_ids[1]]),
                math.cos(angles_a[edge_ids[0]] - angles_a[edge_ids[1]]),
            )
        )
        separation_b = abs(
            math.atan2(
                math.sin(angles_b[edge_ids[0]] - angles_b[edge_ids[1]]),
                math.cos(angles_b[edge_ids[0]] - angles_b[edge_ids[1]]),
            )
        )
        a_to_b_edge_id = max(angles_a, key=angles_a.get)  # type: ignore[arg-type]
        b_to_a_edge_id = max(angles_b, key=angles_b.get)  # type: ignore[arg-type]
        assignment_method = "junction_tangent"
        if (
            min(separation_a, separation_b) < math.radians(0.01)
            or a_to_b_edge_id == b_to_a_edge_id
        ):
            side_scores = {
                edge.id: left_side_score(edge, node_a_id)
                for edge in parallel_edges
            }
            score_difference = abs(
                side_scores[edge_ids[0]] - side_scores[edge_ids[1]]
            )
            if score_difference < 1e-18:
                skipped.append(
                    ParallelDirectionSkip(
                        line_component_id,
                        node_a_id,
                        node_b_id,
                        edge_ids,
                        "端點切線與整體線形都無法區分左右",
                    )
                )
                continue
            a_to_b_edge_id = max(
                side_scores, key=side_scores.get  # type: ignore[arg-type]
            )
            b_to_a_edge_id = next(
                edge_id for edge_id in edge_ids if edge_id != a_to_b_edge_id
            )
            assignment_method = "path_side_area"

        edge_by_id = {edge.id: edge for edge in parallel_edges}
        set_travel_direction(edge_by_id[a_to_b_edge_id], node_a_id, node_b_id)
        set_travel_direction(edge_by_id[b_to_a_edge_id], node_b_id, node_a_id)
        assignments.append(
            ParallelDirectionAssignment(
                line_component_id=line_component_id,
                node_a_id=node_a_id,
                node_b_id=node_b_id,
                a_to_b_edge_id=a_to_b_edge_id,
                b_to_a_edge_id=b_to_a_edge_id,
                method=assignment_method,
            )
        )

    return assignments, skipped
