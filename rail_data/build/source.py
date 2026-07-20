"""Locate and parse the authoritative UTF-8 N02 GML/XML source."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from rail_data.paths import N02_GML_ROOT, N02_XML_PATH, N02_ZIP_PATH

from .models import Coord, RawSection, RawStation, RouteKey


GML_NS = "http://www.opengis.net/gml/3.2"
KSJ_NS = "http://nlftp.mlit.go.jp/ksj/schemas/ksj-app"
XLINK_NS = "http://www.w3.org/1999/xlink"
GML_ID = f"{{{GML_NS}}}id"
XLINK_HREF = f"{{{XLINK_NS}}}href"
KSJ = f"{{{KSJ_NS}}}"

# The official N02-24 UTF-8 XML contains two corrupted station labels.  Match
# both stationCode and the bad source value so explicit inputs from other N02
# releases are not changed accidentally.
N02_24_STATION_NAME_FIXES = {
    ("003484", "??J"): "茗荷谷",
    ("005146", "壓c"): "螢田",
}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(element: ET.Element, name: str) -> str:
    child = element.find(f"{KSJ}{name}")
    return "" if child is None or child.text is None else child.text.strip()


def child_refs(element: ET.Element, name: str) -> list[str]:
    refs = []
    for child in element.findall(f"{KSJ}{name}"):
        href = child.get(XLINK_HREF, "").strip()
        if href:
            refs.append(href.removeprefix("#"))
    return refs


def route_key(element: ET.Element) -> RouteKey:
    return RouteKey(
        child_text(element, "railwayType"),
        child_text(element, "serviceProviderType"),
        child_text(element, "railwayLineName"),
        child_text(element, "operationCompany"),
    )


def locate_input(explicit_path: Path | None) -> Path:
    if explicit_path is not None:
        path = explicit_path.expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"入力が見つかりません: {path}")
        return path

    if N02_XML_PATH.is_file():
        return N02_XML_PATH
    if N02_GML_ROOT.is_dir():
        return N02_GML_ROOT
    if N02_ZIP_PATH.is_file():
        return N02_ZIP_PATH
    raise FileNotFoundError(
        f"{N02_XML_PATH} または {N02_ZIP_PATH} を検出できません。"
        "python3 setup.py を実行するか、--input で指定してください。"
    )


@contextmanager
def open_n02_xml(path: Path) -> Iterator[BinaryIO]:
    if path.is_dir():
        xml_paths = sorted(path.glob("UTF-8/N02-*.xml")) or sorted(
            path.rglob("N02-*.xml")
        )
        if not xml_paths:
            raise FileNotFoundError(f"{path} に N02 XML がありません。")
        with xml_paths[-1].open("rb") as stream:
            yield stream
        return

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if "UTF-8" in PurePosixPath(name).parts
                and PurePosixPath(name).name.startswith("N02-")
                and name.endswith(".xml")
            )
            if not names:
                raise FileNotFoundError(f"{path} に UTF-8 の N02 XML がありません。")
            with archive.open(names[-1]) as stream:
                yield stream
        return

    with path.open("rb") as stream:
        yield stream


def parse_pos_list(text: str | None) -> list[Coord]:
    values = [] if text is None else text.split()
    if len(values) % 2:
        raise ValueError(f"posList の数値数が奇数です: {len(values)}")
    result = []
    for index in range(0, len(values), 2):
        lat = float(values[index])
        lon = float(values[index + 1])
        result.append((lon, lat))
    return result


def parse_n02(
    input_path: Path,
) -> tuple[dict[str, list[list[Coord]]], list[RawSection], list[RawStation]]:
    curves: dict[str, list[list[Coord]]] = {}
    sections: list[RawSection] = []
    stations: list[RawStation] = []

    with open_n02_xml(input_path) as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            name = local_name(element.tag)
            if name == "Curve":
                curve_id = element.get(GML_ID, "")
                parts = [
                    parse_pos_list(pos_list.text)
                    for pos_list in element.findall(f".//{{{GML_NS}}}posList")
                ]
                curves[curve_id] = [part for part in parts if len(part) >= 2]
                element.clear()
            elif name == "RailroadSection":
                location = child_refs(element, "location")
                sections.append(
                    RawSection(
                        source_id=element.get(GML_ID, ""),
                        curve_id=location[0] if location else "",
                        route_key=route_key(element),
                    )
                )
                element.clear()
            elif name == "Station":
                location = child_refs(element, "location")
                station_code = child_text(element, "stationCode")
                station_name = child_text(element, "stationName")
                stations.append(
                    RawStation(
                        source_id=element.get(GML_ID, ""),
                        curve_id=location[0] if location else "",
                        route_key=route_key(element),
                        name=N02_24_STATION_NAME_FIXES.get(
                            (station_code, station_name), station_name
                        ),
                        station_code=station_code,
                        group_code=child_text(element, "groupCode"),
                        section_refs=child_refs(element, "railroadSection"),
                    )
                )
                element.clear()

    if not curves or not sections or not stations:
        raise ValueError(
            f"N02 解析結果が不正です: curves={len(curves)}, "
            f"sections={len(sections)}, stations={len(stations)}"
        )
    return curves, sections, stations
