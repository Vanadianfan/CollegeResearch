"""Japanese standard regional-mesh geometry utilities."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MeshBounds:
    west_lon: float
    south_lat: float
    east_lon: float
    north_lat: float

    @property
    def center_lon(self) -> float:
        return (self.west_lon + self.east_lon) / 2

    @property
    def center_lat(self) -> float:
        return (self.south_lat + self.north_lat) / 2


def _quadrant_offset(digit: int) -> tuple[int, int]:
    if digit not in (1, 2, 3, 4):
        raise ValueError(f"mesh quadrant must be 1..4: {digit}")
    return ((digit - 1) % 2, (digit - 1) // 2)


def fifth_mesh_bounds(mesh_code: str) -> MeshBounds:
    """Return JGD2011 lon/lat bounds for a 10-digit fifth (250 m) mesh."""

    if len(mesh_code) != 10 or not mesh_code.isdigit():
        raise ValueError(f"fifth mesh code must contain 10 digits: {mesh_code!r}")

    primary_lat = int(mesh_code[0:2]) * (2.0 / 3.0)
    primary_lon = 100.0 + int(mesh_code[2:4])

    second_lat = int(mesh_code[4])
    second_lon = int(mesh_code[5])
    if not (0 <= second_lat <= 7 and 0 <= second_lon <= 7):
        raise ValueError(f"invalid second-mesh digits: {mesh_code!r}")

    third_lat = int(mesh_code[6])
    third_lon = int(mesh_code[7])
    if not (0 <= third_lat <= 9 and 0 <= third_lon <= 9):
        raise ValueError(f"invalid third-mesh digits: {mesh_code!r}")

    south = primary_lat + second_lat * (5.0 / 60.0)
    west = primary_lon + second_lon * (7.5 / 60.0)
    south += third_lat * (30.0 / 3600.0)
    west += third_lon * (45.0 / 3600.0)
    height = 30.0 / 3600.0
    width = 45.0 / 3600.0

    for digit in (int(mesh_code[8]), int(mesh_code[9])):
        x_offset, y_offset = _quadrant_offset(digit)
        height /= 2
        width /= 2
        south += y_offset * height
        west += x_offset * width

    return MeshBounds(
        west_lon=west,
        south_lat=south,
        east_lon=west + width,
        north_lat=south + height,
    )
