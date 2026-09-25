"""Coordinate normalization, indexing, and GRS80 distance utilities."""

from __future__ import annotations

import math
from typing import Iterator

from pyproj import Geod

from .models import Coord, RawSegment


COORDINATE_DIGITS = 8
DEFAULT_POINT_ON_SEGMENT_TOLERANCE = 5e-8
GRID_SIZE_DEGREES = 0.01
GEOD = Geod(ellps="GRS80")


def coord_key(point: Coord) -> Coord:
    return (round(point[0], COORDINATE_DIGITS), round(point[1], COORDINATE_DIGITS))


def part_signature(points: list[Coord]) -> tuple[Coord, ...]:
    forward = tuple(coord_key(point) for point in points)
    backward = tuple(reversed(forward))
    return min(forward, backward)


def curve_signature(parts: list[list[Coord]]) -> tuple[tuple[Coord, ...], ...]:
    return tuple(sorted(part_signature(part) for part in parts))


def segment_length(start: Coord, end: Coord) -> float:
    return float(GEOD.inv(start[0], start[1], end[0], end[1])[2])


def polyline_length(points: list[Coord]) -> float:
    return sum(segment_length(start, end) for start, end in zip(points, points[1:]))


def midpoint_along(points: list[Coord]) -> tuple[Coord, int, float, float]:
    lengths = [segment_length(start, end) for start, end in zip(points, points[1:])]
    total = sum(lengths)
    target = total / 2
    accumulated = 0.0
    for index, length in enumerate(lengths):
        if accumulated + length >= target or index == len(lengths) - 1:
            ratio = 0.0 if length == 0 else (target - accumulated) / length
            start = points[index]
            end = points[index + 1]
            midpoint = (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
            return coord_key(midpoint), index, ratio, target
        accumulated += length
    raise AssertionError("midpoint_along reached an unreachable state")


def point_parameter(point: Coord, start: Coord, end: Coord) -> tuple[float, float]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    norm = dx * dx + dy * dy
    if norm == 0:
        return 0.0, math.dist(point, start)
    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / norm
    projected = (start[0] + t * dx, start[1] + t * dy)
    return t, math.dist(point, projected)


def grid_cells_for_segment(start: Coord, end: Coord) -> Iterator[tuple[int, int]]:
    min_x = math.floor(min(start[0], end[0]) / GRID_SIZE_DEGREES)
    max_x = math.floor(max(start[0], end[0]) / GRID_SIZE_DEGREES)
    min_y = math.floor(min(start[1], end[1]) / GRID_SIZE_DEGREES)
    max_y = math.floor(max(start[1], end[1]) / GRID_SIZE_DEGREES)
    for cell_x in range(min_x, max_x + 1):
        for cell_y in range(min_y, max_y + 1):
            yield (cell_x, cell_y)


def grid_cell_for_point(point: Coord) -> tuple[int, int]:
    return (
        math.floor(point[0] / GRID_SIZE_DEGREES),
        math.floor(point[1] / GRID_SIZE_DEGREES),
    )


def add_split(raw_segment: RawSegment, t: float, point: Coord, method: str) -> None:
    if t <= 1e-12 or t >= 1 - 1e-12:
        return
    raw_segment.splits.append((t, coord_key(point), method))


def split_priority(method: str) -> int:
    return {"source_vertex": 0, "split": 1, "station_anchor": 2}.get(method, 0)

