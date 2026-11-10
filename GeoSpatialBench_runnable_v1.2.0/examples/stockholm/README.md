# Stockholm example

The fixed case contains 700 QA scenes and 700 independent Editing tasks, each covering 28 relations × 25 instances. QA uses EPSG:3006 projected coordinates; Editing uses scene-local metres.

| Directory | Contents |
|---|---|
| `qa/` | Canonical scenes and Open/MCQ reference answers |
| `editing/` | Initial scenes, constraints and reference edits |
| `prompts/` | Public GeoJSON inputs |
| `predictions/` | Qwen3.5-9B GeoJSON example predictions |
| `vector/` | Four-layer source-vector sample |

Run from the package root using its Python environment:

```sh
python scripts/benchmark.py example
```

Results are written to `outputs/stockholm_example/`. Expected counts are Open QA 309/700, MCQ 316/700 and Editing strict CSR 266/700. The command needs no API key or model requests.

`config/stockholm.example.json` uses the source vectors to generate a new case with `STODEMO` IDs. Predictions must match the corresponding case; included predictions apply to the fixed `STO` scenes.

Geometry: **© OpenStreetMap contributors**, [ODbL 1.0](../../LICENSE-DATA.md).
