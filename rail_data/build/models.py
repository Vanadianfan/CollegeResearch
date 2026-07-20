"""Intermediate models owned exclusively by the N02 build pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


Coord = tuple[float, float]


@dataclass(frozen=True, order=True)
class RouteKey:
    railway_type_code: str
    provider_type_code: str
    name: str
    operator_name: str


@dataclass
class RawSection:
    source_id: str
    curve_id: str
    route_key: RouteKey


@dataclass
class RawStation:
    source_id: str
    curve_id: str
    route_key: RouteKey
    name: str
    station_code: str
    group_code: str
    section_refs: list[str]


@dataclass(frozen=True)
class RawPassengerStation:
    source_id: str
    curve_id: str
    route_key: RouteKey
    name: str
    station_code: str
    group_code: str
    duplicate_code: str
    data_status_code: str
    passengers: int | None


@dataclass
class StationGroupRow:
    id: int
    group_code: str
    display_name: str
    station_count: int
    passengers: int | None = None


@dataclass
class StationRow:
    id: int
    source_id: str
    station_code: str
    group_id: int
    line_id: int
    name: str
    geometry_length_m: float
    geometry_status: str


@dataclass
class ComponentDraft:
    id: int
    station_id: int
    line_id: int
    component_no: int
    points: list[Coord]
    length_m: float
    midpoint: Coord
    position_m: float
    geometry_class: str
    anchor_status: str
    matched_section_id: str | None = None
    matched_part_no: int | None = None
    section_forward: bool = True
    raw_segment_ids: list[int] = field(default_factory=list)


@dataclass
class RawSegment:
    id: int
    line_id: int
    start: Coord
    end: Coord
    splits: list[tuple[float, Coord, str]] = field(default_factory=list)


@dataclass
class NodeRow:
    id: int
    line_id: int
    lon: float
    lat: float
    topology_type: str = "shape"
    creation_method: str = "source_vertex"
    line_component_id: int | None = None


@dataclass
class SegmentRow:
    id: int
    line_id: int
    from_node_id: int
    to_node_id: int
    length_m: float
    build_status: str = "ok"
    line_component_id: int | None = None


@dataclass
class AnchorRow:
    id: int
    station_component_id: int
    node_id: int
    anchor_no: int
    method: str
    position_m: float
    is_primary: int
    status: str


@dataclass
class LineComponentRow:
    id: int
    line_id: int
    component_no: int
    node_count: int
    segment_count: int
    build_status: str


@dataclass
class GraphEdgeRow:
    id: int
    line_component_id: int
    from_node_id: int
    to_node_id: int
    distance_m: float
    segment_refs: list[tuple[int, int]]
    direction: str = "both"


@dataclass
class ConnectionRow:
    id: int
    line_component_id: int
    from_anchor_id: int
    to_anchor_id: int
    from_station_offset_m: float
    to_station_offset_m: float
    gap_length_m: float
    distance_m: float
    edge_refs: list[tuple[int, int]]
    direction: str = "forward"


@dataclass
class ImportIssue:
    stage: str
    severity: str
    entity_table: str | None
    entity_id: int | None
    issue_code: str
    message: str
    details: dict[str, object] | None = None


@dataclass(frozen=True)
class PassengerAggregationSummary:
    source_record_count: int
    selected_group_count: int
    available_group_count: int
    incomplete_group_count: int
    missing_primary_group_count: int
    no_station_group_count: int
    no_source_record_group_count: int
    mapped_by_station_code: int
    mapped_by_geometry: int
    mapped_by_group_code: int
    unmatched_active_record_count: int
    ignored_duplicate_record_count: int
    ignored_no_station_record_count: int


@dataclass(frozen=True)
class UnfoldMergeCorrection:
    line_no: int
    junction_node_id: int
    edge_refs: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class AppliedCorrection:
    line_no: int
    junction_node_id: int
    source_edge_refs: tuple[tuple[int, int], ...]
    merged_edge_id: int
    split_node_id: int
    distance_m: float


@dataclass(frozen=True)
class ParallelDirectionAssignment:
    line_component_id: int
    node_a_id: int
    node_b_id: int
    a_to_b_edge_id: int
    b_to_a_edge_id: int
    method: str


@dataclass(frozen=True)
class ParallelDirectionSkip:
    line_component_id: int
    node_a_id: int
    node_b_id: int
    edge_ids: tuple[int, int]
    reason: str
