"""Immutable records that exactly mirror persisted SQLite table columns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


SCHEMA_VERSION = 8


@dataclass(frozen=True, slots=True)
class RailLineRecord:
    id: int
    railway_type_code: str
    provider_type_code: str
    name: str
    operator_name: str


@dataclass(frozen=True, slots=True)
class RailLineComponentRecord:
    id: int
    line_id: int
    component_no: int
    node_count: int
    segment_count: int
    build_status: str


@dataclass(frozen=True, slots=True)
class StationGroupRecord:
    id: int
    group_code: str
    display_name: str
    station_count: int
    passengers: int | None


@dataclass(frozen=True, slots=True)
class StationRecord:
    id: int
    source_id: str
    station_code: str
    group_id: int
    line_id: int
    name: str
    geometry_length_m: float
    geometry_status: str


@dataclass(frozen=True, slots=True)
class StationComponentRecord:
    id: int
    station_id: int
    component_no: int
    length_m: float
    geometry_class: str
    midpoint_lon: float | None
    midpoint_lat: float | None
    anchor_status: str


@dataclass(frozen=True, slots=True)
class NetworkNodeRecord:
    id: int
    line_id: int
    line_component_id: int | None
    lon: float
    lat: float
    topology_type: str
    creation_method: str


@dataclass(frozen=True, slots=True)
class AtomicSegmentRecord:
    id: int
    line_id: int
    line_component_id: int | None
    from_node_id: int
    to_node_id: int
    length_m: float
    build_status: str


@dataclass(frozen=True, slots=True)
class StationAnchorRecord:
    id: int
    station_component_id: int
    node_id: int
    anchor_no: int
    method: str
    position_m: float | None
    is_primary: int
    status: str


@dataclass(frozen=True, slots=True)
class GraphEdgeRecord:
    id: int
    line_component_id: int
    edge_kind: str
    from_node_id: int
    to_node_id: int
    direction: str
    distance_m: float
    cost_s: float | None
    status: str
    source_method: str


@dataclass(frozen=True, slots=True)
class GraphEdgeHasAtomicSegmentRecord:
    id: int
    graph_edge_id: int
    atomic_segment_id: int
    sequence_no: int
    forward: int


@dataclass(frozen=True, slots=True)
class StationConnectionRecord:
    id: int
    line_component_id: int
    from_anchor_id: int
    to_anchor_id: int
    direction: str
    from_station_offset_m: float
    to_station_offset_m: float
    gap_length_m: float
    distance_m: float
    path_status: str


@dataclass(frozen=True, slots=True)
class StationConnectionHasGraphEdgeRecord:
    id: int
    station_connection_id: int
    graph_edge_id: int
    sequence_no: int
    forward: int


DatabaseRecord: TypeAlias = (
    RailLineRecord
    | RailLineComponentRecord
    | StationGroupRecord
    | StationRecord
    | StationComponentRecord
    | NetworkNodeRecord
    | AtomicSegmentRecord
    | StationAnchorRecord
    | GraphEdgeRecord
    | GraphEdgeHasAtomicSegmentRecord
    | StationConnectionRecord
    | StationConnectionHasGraphEdgeRecord
)

TABLE_RECORD_TYPES: dict[str, type[DatabaseRecord]] = {
    "rail_line": RailLineRecord,
    "rail_line_component": RailLineComponentRecord,
    "station_group": StationGroupRecord,
    "station": StationRecord,
    "station_component": StationComponentRecord,
    "network_node": NetworkNodeRecord,
    "atomic_segment": AtomicSegmentRecord,
    "station_anchor": StationAnchorRecord,
    "graph_edge": GraphEdgeRecord,
    "graph_edge_has_atomic_segment": GraphEdgeHasAtomicSegmentRecord,
    "station_connection": StationConnectionRecord,
    "station_connection_has_graph_edge": StationConnectionHasGraphEdgeRecord,
}

REQUIRED_TABLES = frozenset(TABLE_RECORD_TYPES)
