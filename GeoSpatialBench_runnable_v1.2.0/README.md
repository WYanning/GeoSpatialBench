# GeoSpatialBench 1.2.0

[中文说明](README_zh.md) · [Paper](https://arxiv.org/abs/2610.12881) · [Data requirements](docs/DATA_CONTRACT.md) · [Input and scoring protocol](docs/PROTOCOL.md)

GeoSpatialBench evaluates spatial-relation understanding and constrained spatial editing across **28 relations**, three input modalities (**GeoJSON, Image, Together**) and three task forms (**Open QA, four-option MCQ, Editing**).

## Contents

- A fixed Stockholm case with 700 QA scenes and 700 independent Editing tasks, reference answers, public prompts and Qwen3.5-9B GeoJSON predictions.
- A four-layer EPSG:3006 GeoPackage with 1,800 POIs, 2,400 roads, 1,400 buildings and 60 regions.
- City configuration templates and tools for building QA, Editing and public prompts.
- A 1536×1536 offline map renderer with feature labels, metric coordinates, north and road-layer information.
- Chat Completions-compatible and Responses-format inference, resume support, raw responses and automatic scoring.
- Locked Python dependencies, data requirements, relation definitions and output schemas.

## Quick start

Install Python 3.13, extract the package and run from its root:

```sh
python quickstart.py
```

An optional pip mirror can be selected with `--index-url https://pypi.tuna.tsinghua.edu.cn/simple`. The launcher creates `.venv`, installs dependencies, scores the included example, builds a new city case and prepares all three modalities. The preview contains 28 QA scenes and 28 Editing tasks. It needs no API key or model request. The final message is `PASS`; `outputs/latest_quickstart.json` points to the output directory. Use `--skip-install` when dependencies are already installed.

## Evaluate a model

Use `.venv/Scripts/python.exe` on Windows or `.venv/bin/python` on Linux/macOS. Replace the model and API-root placeholders:

```sh
.venv/bin/python scripts/benchmark.py run --model YOUR_MODEL --base-url YOUR_API_ROOT --mode all --out outputs/my_model_preview --dry-run
```

Set `BENCHMARK_API_KEY` in your environment, then run the same command without `--dry-run` and with `--resume`. The default uses one scene per relation, shared across modalities, with a denominator of 28 per task. All nine conditions require 252 model requests. Use `--mode geojson_exact` for GeoJSON only, or `--api-format responses` for a Responses endpoint.

For all 700 scenes per task, use a new directory and `--per-relation 0`:

```sh
.venv/bin/python scripts/benchmark.py run --model YOUR_MODEL --base-url YOUR_API_ROOT --mode all --per-relation 0 --out outputs/my_model_full
```

All nine full conditions require 6300 requests. Resume interrupted runs with unchanged data, model and parameters. `summary.json` contains final scores; `reports/` contains per-item scores; `predictions/` contains raw responses and retry records. Public inputs are in `prompts/`, and selected scenes, references and images are in `case/`.

## Score the included example

```sh
.venv/bin/python scripts/benchmark.py example
```

This scores included predictions locally and writes `outputs/stockholm_example/`.

| Qwen3.5-9B / Stockholm / GeoJSON | Correct / total | Score |
|---|---:|---:|
| Open QA | 309/700 | 44.14% |
| MCQ | 316/700 | 45.14% |
| Editing, strict CSR | 266/700 | 38.00% |

## Build a case

```sh
.venv/bin/python scripts/benchmark.py check --config config/stockholm.example.json
.venv/bin/python scripts/benchmark.py build --config config/stockholm.example.json
.venv/bin/python scripts/benchmark.py prompts --case outputs/stockholm_new_build
```

This produces 700 QA scenes, 700 Editing tasks, reference answers and public prompts. New scenes use `STODEMO` IDs and require matching model predictions.

For another city, provide POIs, roads, buildings and land-use/region polygons, including a numeric road-layer field. Edit `config/city.example.json` or create a config from a GeoPackage:

```sh
.venv/bin/python scripts/benchmark.py inspect local_data/city.gpkg
.venv/bin/python scripts/benchmark.py init-config --data local_data/city.gpkg --city "My City" --id-prefix CITY --out config/my_city.json
.venv/bin/python scripts/benchmark.py check --config config/my_city.json
.venv/bin/python scripts/benchmark.py build --config config/my_city.json
```

Review layer names, field mappings, filters, a metre-based projected CRS and `output_dir`. Relative paths are resolved against the config directory. Build outputs must be empty. Add `--case outputs/city` to the model command using the configured output path.

## Modalities and scoring

`geojson_exact` supplies geometry text; `map_annotated` supplies an offline map; `hybrid_geojson_exact` supplies both. Image Editing returns pixel geometry, while GeoJSON/Together Editing returns transform JSON. See [PROTOCOL.md](docs/PROTOCOL.md) for schemas and scoring.

Image/Together use `offline_metric_v1`. Its inputs and pixel frame differ from the paper's OSM-basemap condition. Included model results cover the single-city GeoJSON case.

## Files and licensing

`scripts/` contains runtime code, `config/` contains city templates, `registry/` defines relations, and `examples/stockholm/` contains the fixed case, references, prompts, predictions and source vectors. `docs/` describes data and scoring. `requirements*.txt` specifies dependencies; `RELEASE_MANIFEST.json` and `SHA256SUMS.txt` support package integrity checks.

Code uses [MIT](LICENSE). OSM-derived geometry is attributed to **© OpenStreetMap contributors** under [ODbL](LICENSE-DATA.md). Paper citation files are `CITATION.cff` and `citation.bib`.
