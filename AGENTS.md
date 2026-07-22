# Project Instructions

## Purpose

This project converts Japan MLIT N02-24 railway GML and S12-25 passenger data
into a compact, topology-preserving SQLite network and provides an interactive verifier.
The build entry point is `python -m rail_data.build.main`. Build
implementation and `correction.txt` live under `rail_data/build/`; the
downstream read-only data contract lives under `rail_data/schema/`.
Visualization entry points live under `visualizers/`.
Downloaded source datasets live under `raw_data/`. Generated
`rail_network.sqlite` remains in the project root. Shared path constants are
defined in `rail_data/paths.py`.
Initialize a fresh checkout with `python3 setup.py`; it creates `.venv`,
installs `requirements.txt`, and downloads the public raw datasets.

## Data semantics that must be preserved

- Read the UTF-8 N02 GML/XML. Preserve `Station -> RailroadSection`
  `xlink:href`; do not reconstruct topology from GeoJSON/Shapefile or by
  coordinate-only dissolve.
- A source `Station` is an overlapping line, not a point. Physical track is
  stored once as ordered `atomic_segment` rows.
- Preserve route/operator-specific `station` rows. `station_group` / N02
  `groupCode` is membership only; it is not proof of transfer connectivity.
- `station_group.passengers` is the S12-25 2024 daily boarding-plus-alighting
  group total. Sum only distinct `duplicate2024=1`, `dataEorN2024=1` primary
  observations; exclude `duplicate2024=2`. If any primary observation is
  missing/nonpublic, or only a duplicate reference is found, store `NULL`
  rather than a partial total. It is not a count of unique people.
- `station.source_id` is the source Station `gml:id`; keep source codes as
  `TEXT` so leading zeros survive.
- `network_node` owns coordinates. Junction status is derived from topology;
  station evaluation uses `station_anchor`.
- Keep only research-core tables. Do not reintroduce raw
  `railroad_section`/provenance tables unless explicitly requested.
- Distances use GRS80 geodesic metres. A station connection must satisfy
  `distance_m = from_station_offset_m + gap_length_m + to_station_offset_m`.

## Current network rules

- SQLite schema version is 8. In `station_connection`, keep
  `to_station_offset_m` immediately after `from_station_offset_m`, followed by
  `gap_length_m`.
- `graph_edge.direction` is enforced during routing. Strict degree-3 bubbles
  with exactly two parallel edges are made one-way using left-hand running.
- `station_connection` stores both ordered directions as separate
  `direction='forward'` rows. Query distance by the indexed pair
  `(from_anchor_id, to_anchor_id)`; never assume the reverse distance is equal.
- Apply `correction.txt` after raw topology compression and before calculating
  station connections. `UM 382288 12053- 12054+ 12047-` unfolds the Yurikamome
  loop and merges the forced path. `SM 287467 3528+3521 3526+3529` splits a
  false Tokaido junction into two disconnected ordinary paths and merges each
  edge pair. Full-build IDs are not valid in `--line-name` subset builds.
- N02-24 source labels `stationCode=003484` and `005146` are repaired from the
  corrupted XML values to `茗荷谷` and `螢田` during parsing.

## Build and verification workflow

- A normal build atomically replaces the requested DB. During agent testing,
  write to `/private/tmp`; do not overwrite `rail_network.sqlite` unless the
  user explicitly asks.
- Every build must run the independent passenger reverse validator after the
  temporary DB is populated and before atomic replacement. It must reject a
  `NULL` group when complete primary S12 data can be reconstructed, as well as
  any non-NULL value that cannot be independently reproduced. The standalone
  entry point is `python -m rail_data.build.passenger_validation`.
- Run:

  ```bash
  .venv/bin/python -m rail_data.build.main --output /private/tmp/rail-network-test.sqlite
  .venv/bin/python -m visualizers.network_db /private/tmp/rail-network-test.sqlite --check-only
  ```

- Preserve unrelated user files and changes. Treat the four known unmatched
  stations (敦賀, 放出, 鴫野, and 箕面船場阪大前) as warnings unless their source
  matching is deliberately fixed.

## Visualizer contract

- Show stations as solid nodes, non-station junctions as hollow nodes, group
  labels near member stations, and edge distances.
- Hovering a station highlights every displayed member of its group by color
  only; do not enlarge nodes.
- Hovering coincident, locally parallel graph edges highlights all matching
  edges and lists every distinct displayed rail line. Treat this as geometric
  overlap, not proof that the physical track is shared.
- The page UI is Japanese. Prefer macOS Japanese fonts (`Hiragino Sans`,
  `Hiragino Kaku Gothic ProN`) and show detail-card fields on separate lines.
- Station details include `station.source_id`. Preserve trackpad/touch pinch
  zoom and drag-to-pan behavior.
