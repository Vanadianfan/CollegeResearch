"""Parse and apply explicit post-topology corrections."""

from __future__ import annotations

from pathlib import Path

from .models import (
    AnchorRow,
    AppliedCorrection,
    GraphEdgeRow,
    LineComponentRow,
    NodeRow,
    SegmentRow,
    UnfoldMergeCorrection,
)


def load_corrections(path: Path) -> list[UnfoldMergeCorrection]:
    correction_path = path.expanduser().resolve()
    if not correction_path.is_file():
        raise FileNotFoundError(f"修正ファイルが見つかりません: {correction_path}")

    corrections: list[UnfoldMergeCorrection] = []
    for line_no, raw_line in enumerate(
        correction_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        content = raw_line.split("#", 1)[0].strip()
        if not content:
            continue
        fields = content.split()
        if fields[0] != "UM":
            raise ValueError(
                f"{correction_path}:{line_no}: 未対応の修正命令 {fields[0]!r}"
            )
        if len(fields) != 5:
            raise ValueError(
                f"{correction_path}:{line_no}: UM は "
                "<junction_node> <incoming_edge> <loop_edge> <outgoing_edge> "
                "の4引数が必要です。"
            )
        try:
            junction_node_id = int(fields[1])
        except ValueError as error:
            raise ValueError(
                f"{correction_path}:{line_no}: junction_node は整数で指定してください。"
            ) from error

        edge_refs: list[tuple[int, int]] = []
        for token in fields[2:]:
            if len(token) < 2 or token[-1] not in "+-" or not token[:-1].isdigit():
                raise ValueError(
                    f"{correction_path}:{line_no}: 辺は 12060+ または 12060- "
                    f"の形式で指定してください: {token!r}"
                )
            edge_refs.append((int(token[:-1]), int(token[-1] == "+")))
        if len({edge_id for edge_id, _ in edge_refs}) != len(edge_refs):
            raise ValueError(
                f"{correction_path}:{line_no}: 同じ graph_edge を重複指定できません。"
            )
        corrections.append(
            UnfoldMergeCorrection(
                line_no=line_no,
                junction_node_id=junction_node_id,
                edge_refs=tuple(edge_refs),
            )
        )
    return corrections

def apply_unfold_merge_corrections(
    corrections: list[UnfoldMergeCorrection],
    nodes: list[NodeRow],
    segments: list[SegmentRow],
    anchors: list[AnchorRow],
    line_components: list[LineComponentRow],
    graph_edges: list[GraphEdgeRow],
    *,
    allow_unavailable: bool,
) -> tuple[list[AppliedCorrection], list[UnfoldMergeCorrection]]:
    node_by_id = {node.id: node for node in nodes}
    segment_by_id = {segment.id: segment for segment in segments}
    component_by_id = {component.id: component for component in line_components}
    graph_edge_by_id = {edge.id: edge for edge in graph_edges}
    anchor_nodes = {anchor.node_id for anchor in anchors}
    applied: list[AppliedCorrection] = []
    skipped: list[UnfoldMergeCorrection] = []
    if allow_unavailable:
        # correction.txt uses IDs from the deterministic full build.  A
        # --line-name subset has a different ID space, so applying them there
        # could silently target the wrong topology.
        return applied, list(corrections)

    def edge_endpoints(edge: GraphEdgeRow, forward: int) -> tuple[int, int]:
        if forward:
            return edge.from_node_id, edge.to_node_id
        return edge.to_node_id, edge.from_node_id

    def directed_segment_refs(
        edge: GraphEdgeRow, forward: int
    ) -> list[tuple[int, int]]:
        if forward:
            return list(edge.segment_refs)
        return [
            (segment_id, 1 - segment_forward)
            for segment_id, segment_forward in reversed(edge.segment_refs)
        ]

    def segment_endpoints(segment_ref: tuple[int, int]) -> tuple[int, int]:
        segment_id, forward = segment_ref
        segment = segment_by_id[segment_id]
        if forward:
            return segment.from_node_id, segment.to_node_id
        return segment.to_node_id, segment.from_node_id

    def validate_atomic_path(
        segment_refs: list[tuple[int, int]],
        expected_start: int,
        expected_end: int,
        context: str,
    ) -> None:
        if not segment_refs:
            raise ValueError(f"{context}: graph_edge に atomic_segment がありません。")
        current = expected_start
        for segment_ref in segment_refs:
            start_node, end_node = segment_endpoints(segment_ref)
            if start_node != current:
                raise ValueError(
                    f"{context}: atomic_segment#{segment_ref[0]} が連続していません "
                    f"(expected={current}, actual={start_node})。"
                )
            current = end_node
        if current != expected_end:
            raise ValueError(
                f"{context}: atomic_segment 終点が一致しません "
                f"(expected={expected_end}, actual={current})。"
            )

    def rewire_traversal_endpoint(
        segment_ref: tuple[int, int],
        endpoint: str,
        old_node_id: int,
        new_node_id: int,
        context: str,
    ) -> None:
        segment_id, forward = segment_ref
        segment = segment_by_id[segment_id]
        if endpoint == "start":
            attribute = "from_node_id" if forward else "to_node_id"
        else:
            attribute = "to_node_id" if forward else "from_node_id"
        if getattr(segment, attribute) != old_node_id:
            raise ValueError(
                f"{context}: atomic_segment#{segment_id} の {endpoint} は "
                f"node#{old_node_id} ではありません。"
            )
        setattr(segment, attribute, new_node_id)

    for correction in corrections:
        context = f"correction.txt:{correction.line_no} UM"
        missing_edges = [
            edge_id
            for edge_id, _ in correction.edge_refs
            if edge_id not in graph_edge_by_id
        ]
        if correction.junction_node_id not in node_by_id or missing_edges:
            if allow_unavailable:
                skipped.append(correction)
                continue
            missing_text = ", ".join(
                f"graph_edge#{edge_id}" for edge_id in missing_edges
            )
            if correction.junction_node_id not in node_by_id:
                missing_text = (
                    f"network_node#{correction.junction_node_id}"
                    + (f", {missing_text}" if missing_text else "")
                )
            raise ValueError(f"{context}: 修正対象が見つかりません: {missing_text}")

        directed_edges = [
            (graph_edge_by_id[edge_id], forward)
            for edge_id, forward in correction.edge_refs
        ]
        component_ids = {edge.line_component_id for edge, _ in directed_edges}
        if len(component_ids) != 1:
            raise ValueError(
                f"{context}: 3辺は同じ rail_line_component ではありません。"
            )
        line_component_id = next(iter(component_ids))
        junction = node_by_id[correction.junction_node_id]
        if junction.line_component_id != line_component_id:
            raise ValueError(
                f"{context}: junction と graph_edge の line_component が異なります。"
            )
        if junction.id in anchor_nodes:
            raise ValueError(
                f"{context}: 駅アンカー node#{junction.id} は分割できません。"
            )

        directed_endpoints = [
            edge_endpoints(edge, forward) for edge, forward in directed_edges
        ]
        for (_, previous_end), (next_start, _) in zip(
            directed_endpoints, directed_endpoints[1:]
        ):
            if previous_end != next_start:
                raise ValueError(
                    f"{context}: 指定された有向 graph_edge が連続していません "
                    f"({previous_end} != {next_start})。"
                )
        incoming_edge, loop_edge, outgoing_edge = directed_edges
        incoming_endpoints, loop_endpoints, outgoing_endpoints = directed_endpoints
        if (
            incoming_endpoints[1] != junction.id
            or loop_endpoints != (junction.id, junction.id)
            or outgoing_endpoints[0] != junction.id
        ):
            raise ValueError(
                f"{context}: UM は incoming -> junction self-loop -> outgoing "
                "の形で指定してください。"
            )

        incoming_refs = directed_segment_refs(*incoming_edge)
        loop_refs = directed_segment_refs(*loop_edge)
        outgoing_refs = directed_segment_refs(*outgoing_edge)
        validate_atomic_path(
            incoming_refs, *incoming_endpoints, f"{context} incoming"
        )
        validate_atomic_path(loop_refs, *loop_endpoints, f"{context} loop")
        validate_atomic_path(
            outgoing_refs, *outgoing_endpoints, f"{context} outgoing"
        )

        correction_arm_ids = {
            incoming_refs[-1][0],
            loop_refs[0][0],
            loop_refs[-1][0],
            outgoing_refs[0][0],
        }
        incident_segment_ids = {
            segment.id
            for segment in segments
            if segment.from_node_id == junction.id or segment.to_node_id == junction.id
        }
        if len(correction_arm_ids) != 4 or incident_segment_ids != correction_arm_ids:
            raise ValueError(
                f"{context}: junction の4腕が指定経路と一致しません "
                f"(incident={sorted(incident_segment_ids)}, "
                f"specified={sorted(correction_arm_ids)})。"
            )

        split_node_id = max(node_by_id) + 1
        split_node = NodeRow(
            id=split_node_id,
            line_id=junction.line_id,
            lon=junction.lon,
            lat=junction.lat,
            topology_type="shape",
            creation_method="correction_unfold_merge",
            line_component_id=junction.line_component_id,
        )
        nodes.append(split_node)
        node_by_id[split_node_id] = split_node
        junction.topology_type = "shape"
        component_by_id[line_component_id].node_count += 1

        rewire_traversal_endpoint(
            loop_refs[-1], "end", junction.id, split_node_id, context
        )
        rewire_traversal_endpoint(
            outgoing_refs[0], "start", junction.id, split_node_id, context
        )

        merged_refs = [*incoming_refs, *loop_refs, *outgoing_refs]
        merged_start = incoming_endpoints[0]
        merged_end = outgoing_endpoints[1]
        validate_atomic_path(
            merged_refs, merged_start, merged_end, f"{context} merged"
        )

        for node_id in (junction.id, split_node_id):
            degree = sum(
                segment.from_node_id == node_id or segment.to_node_id == node_id
                for segment in segments
            )
            if degree != 2:
                raise ValueError(
                    f"{context}: 修正後の node#{node_id} degree={degree} (expected=2)。"
                )

        source_edge_ids = {edge.id for edge, _ in directed_edges}
        merged_edge_id = incoming_edge[0].id
        merged_edge = GraphEdgeRow(
            id=merged_edge_id,
            line_component_id=line_component_id,
            from_node_id=merged_start,
            to_node_id=merged_end,
            distance_m=sum(
                segment_by_id[segment_id].length_m
                for segment_id, _ in merged_refs
            ),
            segment_refs=merged_refs,
        )
        graph_edges[:] = [
            edge for edge in graph_edges if edge.id not in source_edge_ids
        ]
        graph_edges.append(merged_edge)
        graph_edges.sort(key=lambda edge: edge.id)
        for edge_id in source_edge_ids:
            graph_edge_by_id.pop(edge_id)
        graph_edge_by_id[merged_edge.id] = merged_edge
        applied.append(
            AppliedCorrection(
                line_no=correction.line_no,
                junction_node_id=junction.id,
                source_edge_refs=correction.edge_refs,
                merged_edge_id=merged_edge.id,
                split_node_id=split_node_id,
                distance_m=merged_edge.distance_m,
            )
        )

    return applied, skipped

