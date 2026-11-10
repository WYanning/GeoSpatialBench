# City vector-data contract

## Roles and fields

| Config role | Geometry after exploding multipart features | Minimum filtered features | Use |
|---|---|---:|---|
| `pois` | Point | 750 | Direction, distance, nearest neighbor, density |
| `roads` | LineString | 1,250 | Topology, orientation, order and network questions |
| `buildings` | Polygon | 650 | Topology, boundary distance and perimeter |
| `landuse` | Polygon | 25 | Region density and editing regions |

These counts are necessary for the locked QA sampler, not a guarantee that every relation is available. At least 25 candidate density regions must cover three or more supplied POIs. Vertical-order QA needs 2D-intersecting roads with usable numeric `layer` values, including nonzero values. Enlarge the extent when candidates are insufficient. Development Editing still uses this shared input check, even when fewer editing tasks are requested.

Each role specifies `path` and, for a multilevel format such as GeoPackage, `layer`. Formats supported by GeoPandas include GeoPackage, GeoJSON and Shapefile. Use a stable `id_field`; without it, source row positions are used and depend on file ordering. Multipart features are exploded; generation preserves a component suffix where needed. Wrong geometry types and empty geometries are filtered; invalid geometries are rejected and must be repaired in the source.

Roads must expose an ordinal numeric layer, mapped through `field_map.layer`. `bridge` and `tunnel` are optional flags. **Layer is vertical stacking order, not elevation in metres.** Missing numeric layer cells default to zero; an entirely zero layer column fails the full input check. Non-numeric non-null values are rejected. Do not fabricate bridge/tunnel/layer metadata to make the check pass.

```json
"roads": {
  "path": "../local_data/roads.geojson",
  "id_field": "osm_id",
  "field_map": {"layer": "z_order", "bridge": "is_bridge", "tunnel": "is_tunnel"},
  "attribute_fields": ["name", "type"]
}
```

`attribute_fields` are retained in canonical provenance and diagnostic attribute modes. The public default GeoJSON QA mode sends geometry and anonymous IDs only, which makes O06 information-insufficient. Its excluded-subset score is reported separately. The map renderer explicitly shows the O06 layer values.

For a single mixed GeoJSON, several roles may point to the same file. Use a verified classification field to distinguish polygon roles:

```json
"buildings": {"path":"../local_data/city.geojson", "attribute_filter":{"role":["building"]}},
"landuse": {"path":"../local_data/city.geojson", "attribute_filter":{"role":["landuse","park"]}}
```

## Coordinates and filtering

Input layers must have a declared CRS, or an explicit `source_crs` in the role config. `target_crs` must be projected with metre axes. Longitude/latitude or foot-based projections are rejected for metric calculations. `init-config` retains a metre-based source CRS; otherwise it estimates a UTM zone from up to 1,000 POIs. Review that estimate against your full city extent. You can always pass `--target-crs` explicitly.

If `read_bbox` is used, it is interpreted in the **source CRS**, per role. For differing layer CRSs, specify separate role-level `read_bbox` values. WGS84 boundaries cannot be applied directly to a projected source. Default strict filter intervals are roads (30, 1200) m, buildings (50, 30000) m² and regions (20000, 3000000) m². These are editable city parameters and appear in the config.

QA uses projected coordinates. Editing recentres each scene into a local metric frame. The local frame is not EPSG:4326 GeoJSON; coordinate units are stated in the prompt. Do not reproject the editing coordinates as if they were longitude/latitude.

## What gets constructed

The 28 relations comprise T9 + D5 + M8 + O6. Deferred relations D03, D07, O07 and O09 are retained only in the registry for transparency. QA uses observed geometries and source-derived controlled constructions. Editing uses a separate miner and scene pool, rejects already-satisfied targets during mining, and checks a feasible reference edit. Reference edits are not necessarily unique.

The QA full build performs independent geometry/oracle recomputation, serialization checks, MCQ balance and prompt leakage audits. A full Editing build requires each reference to satisfy its constraints and all 28 quotas to be complete. Geometry mixes are influenced by available source candidates and relation-specific quotas. No global F0 cap is imposed in the portable miner, avoiding exhaustive searches after a cap is reached; record the resulting mix when comparing cities.

The bundled Stockholm scenes form a fixed evaluation case. Newly generated scenes have their own IDs and reference answers; evaluate them with matching predictions.

The ready-to-run `config/stockholm.example.json` uses the bundled four-layer vector subset and the `STODEMO` prefix for a new build. Its source GeoPackage is documented under `examples/stockholm/vector/`. The source selection excludes closed roads because the current sampler's first-to-last direction definitions require distinct endpoints; when preparing another city, provide enough open road segments for these constructions.
