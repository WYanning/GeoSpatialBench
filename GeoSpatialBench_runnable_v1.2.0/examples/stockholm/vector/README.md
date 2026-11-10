# Stockholm source-vector example

`stockholm_sample.gpkg` is a compact source-data example for the portable generator. Open it directly with QGIS, ArcGIS or GeoPandas. It has four layers in **EPSG:3006** (SWEREF 99 TM; coordinate units are metres):

| Layer | Features | Geometry | Fields |
|---|---:|---|---|
| `pois` | 1,800 | Point | `source_id`, `name`, `fclass` |
| `roads` | 2,400 | LineString | above, plus `osm_layer`, `bridge`, `tunnel` |
| `buildings` | 1,400 | Polygon | `source_id`, `name`, `fclass` |
| `landuse` | 60 | Polygon | `source_id`, `name`, `fclass` |

Road `osm_layer` is ordinal vertical stacking, not elevation in metres. The sample retains 213 roads with nonzero layer and actual intersecting pairs with unequal layers. Missing bridge/tunnel flags have been normalized to `F`; true flags use `T`.

From the repository root:

```sh
python -m pip install -r requirements.txt
python scripts/benchmark.py check --config config/stockholm.example.json
python scripts/benchmark.py build --config config/stockholm.example.json
python scripts/benchmark.py prompts --case outputs/stockholm_new_build
```

The default config uses this bundled file and writes a **new** benchmark to `outputs/stockholm_new_build/`. That directory must be empty before the first build. Source geometry is selected from observed vectors and reprojected without simplification or shape clipping. Direction/order construction requires distinct road endpoints, so closed roads are omitted from the sample. Dense regions and their observed POIs are retained for density tasks; both members of selected unequal-layer road pairs are preserved. Other features use deterministic sampling.

This is a four-role source subset for generating a benchmark. It is not a complete city extract. Generated scenes have their own IDs and require corresponding predictions. The fixed case and its example predictions are supplied separately in the parent directory.

## Read with Python

```python
import geopandas as gpd
roads = gpd.read_file("examples/stockholm/vector/stockholm_sample.gpkg", layer="roads")
print(roads.crs, len(roads))
```

Source: Geofabrik Sweden OpenStreetMap release dated **2026-07-20**. Geometry: **© OpenStreetMap contributors**. The database is available under [ODbL 1.0](https://www.openstreetmap.org/copyright); see the repository's `LICENSE-DATA.md`. Source-file hashes, layer bounds, selection seeds and the sample checksum are recorded in `data_manifest.json`. Do not apply the software MIT license to the OSM-derived database.
