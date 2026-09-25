"""Parse and apply explicit post-topology corrections."""

from __future__ import annotations

from pathlib import Path

from .models import (
    AnchorRow,
    AppliedCorrection,
    AppliedCorrectionResult,
    AppliedSplitMergeCorrection,
    Correction,
    GraphEdgeRow,
    LineComponentRow,
    NodeRow,
    SegmentRow,
    SplitMergeCorrection,
    UnfoldMergeCorrection,
)


def load_corrections(path: Path) -> list[Correction]:
    correction_path = path.expanduser().resolve()
    if not correction_path.is_file():
        raise FileNotFoundError(f"修正ファイルが見つかりません: {correction_path}")

    corrections: list[Correction] = []
    for line_no, raw_line in enumerate(
        correction_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        content = raw_line.split("#", 1)[0].strip()
        if not content:
            continue
        fields = content.split()
        command = fields[0]
        if command not in {"UM", "SM"}:
            raise ValueError(
                f"{correction_path}:{line_no}: 未対応の修正命令 {command!r}"
            )
        expected_fields = 5 if command == "UM" else 4
        if len(fields) != expected_fields:
            syntax = (
                "<junction_node> <incoming_edge> <loop_edge> <outgoing_edge>"
                if command == "UM"
                else "<junction_node> <edge_a>+<edge_b> <edge_c>+<edge_d>"
            )
            raise ValueError(
                f"{correction_path}:{line_no}: {command} は {syntax} "
                f"の{expected_fields - 1}引数が必要です。"
            )
        try:
            junction_node_id = int(fields[1])
        except ValueError as error:
            raise ValueError(
                f"{correction_path}:{line_no}: junction_node は整数で"
                "指定してください。"
            ) from error

        if command == "SM":
            edge_pairs: list[tuple[int, int]] = []
            for token in fields[2:]:
                pair = token.split("+")
                if len(pair) != 2 or not all(part.isdigit() for part in pair):
                    raise ValueError(
                        f"{correction_path}:{line_no}: SM の辺ペアは "
                        f"123+456 の形式で指定してください: {token!r}"
                    )
                edge_pairs.append((int(pair[0]), int(pair[1])))
            edge_ids = [edge_id for pair in edge_pairs for edge_id in pair]
            if len(set(edge_ids)) != 4:
                raise ValueError(
                    f"{correction_path}:{line_no}: SM では異なる4本の "
                    "graph_edge を指定してください。"
                )
            corrections.append(
                SplitMergeCorrection(
                    line_no=line_no,
                    junction_node_id=junction_node_id,
                    edge_pairs=(edge_pairs[0], edge_pairs[1]),
                )
            )
            continue

        edge_refs: list[tuple[int, int]] = []
        for token in fields[2:]:
            if len(token) < 2 or token[-1] not in "+-" or not token[:-1].isdigit():
                raise ValueError(
                    f"{correction_path}:{line_no}: 辺は 123+ または 123- "
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


def apply_corrections(
    corrections: list[Correction],
    nodes: list[NodeRow],
    segments: list[SegmentRow],
    anchors: list[AnchorRow],
    line_components: list[LineComponentRow],
    graph_edges: list[GraphEdgeRow],
    *,
    allow_unavailable: bool,
) -> tuple[list[AppliedCorrectionResult], list[Correction]]:
    node_by_id = {node.id: node for node in nodes}
    segment_by_id = {segment.id: segment for segment in segments}
    component_by_id = {component.id: component for component in line_components}
    graph_edge_by_id = {edge.id: edge for edge in graph_edges}
    anchor_nodes = {anchor.node_id for anchor in anchors}
    applied: list[AppliedCorrectionResult] = []
    skipped: list[Correction] = []
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

    def expand_directed_edge(
        edge: GraphEdgeRow,
        forward: int,
        context: str,
    ) -> tuple[list[tuple[int, int]], int, int]:
        start_node, end_node = edge_endpoints(edge, forward)
        refs = directed_segment_refs(edge, forward)
        validate_atomic_path(refs, start_node, end_node, context)
        return refs, start_node, end_node

    def require_targets(
        junction_node_id: int,
        source_edge_ids: set[int],
        context: str,
    ) -> tuple[NodeRow, list[GraphEdgeRow]]:
        missing_edges = sorted(source_edge_ids - graph_edge_by_id.keys())
        if junction_node_id not in node_by_id or missing_edges:
            missing_text = ", ".join(
                f"graph_edge#{edge_id}" for edge_id in missing_edges
            )
            if junction_node_id not in node_by_id:
                missing_text = (
                    f"network_node#{junction_node_id}"
                    + (f", {missing_text}" if missing_text else "")
                )
            raise ValueError(
                f"{context}: 修正対象が見つかりません: {missing_text}"
            )
        return node_by_id[junction_node_id], [
            graph_edge_by_id[edge_id] for edge_id in source_edge_ids
        ]

    def require_split_junction(
        junction: NodeRow,
        source_edges: list[GraphEdgeRow],
        context: str,
    ) -> int:
        component_ids = {edge.line_component_id for edge in source_edges}
        if len(component_ids) != 1:
            raise ValueError(
                f"{context}: 指定辺は同じ rail_line_component ではありません。"
            )
        line_component_id = next(iter(component_ids))
        if junction.line_component_id != line_component_id:
            raise ValueError(
                f"{context}: junction と graph_edge の line_component が異なります。"
            )
        if junction.topology_type != "junction":
            raise ValueError(
                f"{context}: node#{junction.id} は junction ではありません "
                f"(actual={junction.topology_type})。"
            )
        if junction.id in anchor_nodes:
            raise ValueError(
                f"{context}: 駅アンカー node#{junction.id} は分割できません。"
            )
        return line_component_id

    def validate_junction_arms(
        junction: NodeRow,
        correction_arm_ids: set[int],
        context: str,
        target_description: str,
    ) -> None:
        incident_segment_ids = {
            segment.id
            for segment in segments
            if segment.from_node_id == junction.id or segment.to_node_id == junction.id
        }
        if len(correction_arm_ids) != 4 or incident_segment_ids != correction_arm_ids:
            raise ValueError(
                f"{context}: junction の4腕が{target_description}と一致しません "
                f"(incident={sorted(incident_segment_ids)}, "
                f"specified={sorted(correction_arm_ids)})。"
            )

    def split_rewire_merge(
        junction: NodeRow,
        line_component_id: int,
        source_edge_ids: set[int],
        rewires: list[tuple[tuple[int, int], str]],
        merged_paths: list[
            tuple[int, list[tuple[int, int]], int, int, str]
        ],
        creation_method: str,
        context: str,
    ) -> tuple[int, list[GraphEdgeRow]]:
        """Apply the mutation shared by validated UM and SM rewrites."""
        merged_edge_ids = [edge_id for edge_id, *_ in merged_paths]
        if (
            len(set(merged_edge_ids)) != len(merged_edge_ids)
            or not set(merged_edge_ids) <= source_edge_ids
        ):
            raise ValueError(
                f"{context}: 統合後の graph_edge ID は修正対象から"
                "重複なく選んでください。"
            )

        split_node_id = max(node_by_id) + 1
        split_node = NodeRow(
            id=split_node_id,
            line_id=junction.line_id,
            lon=junction.lon,
            lat=junction.lat,
            topology_type="shape",
            creation_method=creation_method,
            line_component_id=junction.line_component_id,
        )
        nodes.append(split_node)
        node_by_id[split_node_id] = split_node
        junction.topology_type = "shape"
        component_by_id[line_component_id].node_count += 1

        for segment_ref, endpoint in rewires:
            rewire_traversal_endpoint(
                segment_ref,
                endpoint,
                junction.id,
                split_node_id,
                context,
            )

        merged_edges: list[GraphEdgeRow] = []
        for edge_id, refs, start_node, end_node, path_context in merged_paths:
            validate_atomic_path(refs, start_node, end_node, path_context)
            merged_edges.append(
                GraphEdgeRow(
                    id=edge_id,
                    line_component_id=line_component_id,
                    from_node_id=start_node,
                    to_node_id=end_node,
                    distance_m=sum(
                        segment_by_id[segment_id].length_m
                        for segment_id, _ in refs
                    ),
                    segment_refs=refs,
                )
            )

        for node_id in (junction.id, split_node_id):
            degree = sum(
                segment.from_node_id == node_id or segment.to_node_id == node_id
                for segment in segments
            )
            if degree != 2:
                raise ValueError(
                    f"{context}: 修正後の node#{node_id} degree={degree} "
                    "(expected=2)。"
                )

        graph_edges[:] = [
            edge for edge in graph_edges if edge.id not in source_edge_ids
        ]
        graph_edges.extend(merged_edges)
        graph_edges.sort(key=lambda edge: edge.id)
        for edge_id in source_edge_ids:
            graph_edge_by_id.pop(edge_id)
        for edge in merged_edges:
            graph_edge_by_id[edge.id] = edge
        return split_node_id, merged_edges

    for correction in corrections:
        if isinstance(correction, SplitMergeCorrection):
            context = f"correction.txt:{correction.line_no} SM"
            source_edge_ids = {
                edge_id
                for edge_pair in correction.edge_pairs
                for edge_id in edge_pair
            }
            junction, source_edges = require_targets(
                correction.junction_node_id,
                source_edge_ids,
                context,
            )
            line_component_id = require_split_junction(
                junction,
                source_edges,
                context,
            )

            incident_edge_ids = {
                edge.id
                for edge in graph_edges
                if edge.from_node_id == junction.id or edge.to_node_id == junction.id
            }
            if incident_edge_ids != source_edge_ids:
                raise ValueError(
                    f"{context}: junction の4辺が指定ペアと一致しません "
                    f"(incident={sorted(incident_edge_ids)}, "
                    f"specified={sorted(source_edge_ids)})。"
                )

            directed_pairs: list[
                tuple[
                    tuple[GraphEdgeRow, int],
                    tuple[GraphEdgeRow, int],
                ]
            ] = []
            for first_id, second_id in correction.edge_pairs:
                first = graph_edge_by_id[first_id]
                second = graph_edge_by_id[second_id]
                if first.from_node_id == junction.id == first.to_node_id:
                    raise ValueError(
                        f"{context}: graph_edge#{first.id} は self-loop のため "
                        "SM に使用できません。"
                    )
                if second.from_node_id == junction.id == second.to_node_id:
                    raise ValueError(
                        f"{context}: graph_edge#{second.id} は self-loop のため "
                        "SM に使用できません。"
                    )
                if junction.id not in (first.from_node_id, first.to_node_id):
                    raise ValueError(
                        f"{context}: graph_edge#{first.id} は junction に"
                        "接続していません。"
                    )
                if junction.id not in (second.from_node_id, second.to_node_id):
                    raise ValueError(
                        f"{context}: graph_edge#{second.id} は junction に"
                        "接続していません。"
                    )
                first_toward_junction = int(first.to_node_id == junction.id)
                second_away_from_junction = int(second.from_node_id == junction.id)
                directed_pairs.append(
                    (
                        (first, first_toward_junction),
                        (second, second_away_from_junction),
                    )
                )

            pair_paths: list[tuple[list[tuple[int, int]], int, int]] = []
            split_rewires: list[tuple[tuple[int, int], str]] = []
            correction_arm_ids: set[int] = set()
            for pair_no, (incoming, outgoing) in enumerate(directed_pairs, start=1):
                incoming_refs, incoming_start, incoming_end = expand_directed_edge(
                    *incoming,
                    f"{context} pair#{pair_no} incoming",
                )
                outgoing_refs, outgoing_start, outgoing_end = expand_directed_edge(
                    *outgoing,
                    f"{context} pair#{pair_no} outgoing",
                )
                if incoming_end != junction.id or outgoing_start != junction.id:
                    raise ValueError(
                        f"{context}: pair#{pair_no} は junction を通る"
                        "連続経路ではありません。"
                    )
                correction_arm_ids.update(
                    (incoming_refs[-1][0], outgoing_refs[0][0])
                )
                if pair_no == 2:
                    split_rewires.extend(
                        [
                            (incoming_refs[-1], "end"),
                            (outgoing_refs[0], "start"),
                        ]
                    )
                pair_paths.append(
                    (
                        [*incoming_refs, *outgoing_refs],
                        incoming_start,
                        outgoing_end,
                    )
                )

            validate_junction_arms(
                junction,
                correction_arm_ids,
                context,
                "指定ペア",
            )

            merged_paths: list[
                tuple[int, list[tuple[int, int]], int, int, str]
            ] = []
            for pair_no, ((first_id, second_id), pair_path) in enumerate(
                zip(correction.edge_pairs, pair_paths), start=1
            ):
                refs, start_node, end_node = pair_path
                merged_paths.append(
                    (
                        min(first_id, second_id),
                        refs,
                        start_node,
                        end_node,
                        f"{context} pair#{pair_no} merged",
                    )
                )
            split_node_id, merged_edges = split_rewire_merge(
                junction,
                line_component_id,
                source_edge_ids,
                split_rewires,
                merged_paths,
                "correction_split_merge",
                context,
            )
            applied.append(
                AppliedSplitMergeCorrection(
                    line_no=correction.line_no,
                    junction_node_id=junction.id,
                    source_edge_pairs=correction.edge_pairs,
                    merged_edge_ids=(merged_edges[0].id, merged_edges[1].id),
                    split_node_id=split_node_id,
                    distance_ms=(
                        merged_edges[0].distance_m,
                        merged_edges[1].distance_m,
                    ),
                )
            )
            continue

        context = f"correction.txt:{correction.line_no} UM"
        source_edge_ids = {edge_id for edge_id, _ in correction.edge_refs}
        junction, source_edges = require_targets(
            correction.junction_node_id,
            source_edge_ids,
            context,
        )
        line_component_id = require_split_junction(
            junction,
            source_edges,
            context,
        )
        directed_edges = [
            (graph_edge_by_id[edge_id], forward)
            for edge_id, forward in correction.edge_refs
        ]
        expanded_edges = [
            expand_directed_edge(edge, forward, f"{context} {label}")
            for (edge, forward), label in zip(
                directed_edges,
                ("incoming", "loop", "outgoing"),
            )
        ]
        incoming_edge = directed_edges[0]
        (
            (incoming_refs, incoming_start, incoming_end),
            (loop_refs, loop_start, loop_end),
            (outgoing_refs, outgoing_start, outgoing_end),
        ) = expanded_edges
        if (
            incoming_end != junction.id
            or (loop_start, loop_end) != (junction.id, junction.id)
            or outgoing_start != junction.id
        ):
            raise ValueError(
                f"{context}: UM は incoming -> junction self-loop -> outgoing "
                "の形で指定してください。"
            )

        correction_arm_ids = {
            incoming_refs[-1][0],
            loop_refs[0][0],
            loop_refs[-1][0],
            outgoing_refs[0][0],
        }
        validate_junction_arms(
            junction,
            correction_arm_ids,
            context,
            "指定経路",
        )

        merged_refs = [*incoming_refs, *loop_refs, *outgoing_refs]
        split_node_id, merged_edges = split_rewire_merge(
            junction,
            line_component_id,
            source_edge_ids,
            [(loop_refs[-1], "end"), (outgoing_refs[0], "start")],
            [
                (
                    incoming_edge[0].id,
                    merged_refs,
                    incoming_start,
                    outgoing_end,
                    f"{context} merged",
                )
            ],
            "correction_unfold_merge",
            context,
        )
        merged_edge = merged_edges[0]
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
