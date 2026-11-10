# Prompts, inference and scoring

## QA

Gold and canonical files are evaluator inputs; `messages` are the model inputs. Source IDs, provenance, answers and oracle records are excluded. MCQ task wording differs from open-ended wording for intersection partitions, strict containment, numerical intervals and several ordering questions. The emitter uses relation-specific wording and the fixed gold option order.

Run each question form separately:

```sh
python scripts/run_api.py --prompts outputs/city/prompts/qa_open_geojson_exact.jsonl --out outputs/city/predictions/qa_open.jsonl --model YOUR_MODEL --base-url YOUR_API_ROOT
python scripts/run_api.py --prompts outputs/city/prompts/qa_mcq4_geojson_exact.jsonl --out outputs/city/predictions/qa_mcq4.jsonl --model YOUR_MODEL --base-url YOUR_API_ROOT
python scripts/benchmark.py evaluate-qa --items outputs/city/qa/items.jsonl --gold outputs/city/qa/gold_mcq4.jsonl --predictions outputs/city/predictions/qa_mcq4.jsonl --form mcq4 --out outputs/city/reports/qa_mcq4.json
```

Prediction rows require a unique `item_id` and `prediction` string. The endpoint runner adds prompt ID, model and form. External local inference systems can emit the same minimal schema:

```json
{"item_id":"CITY-L1-T01-001","prediction":"equals"}
```

Current scoring version is `complete_semantic_final_answer_v3_20261014`. Numeric answers require an unambiguous final number; MCQ requires an explicit final option. Nonnumeric answers must be complete terminal answers, optionally introduced by `Answer:` or `Final answer:`. Negations and alternative lists do not match a gold token merely by containing it. Metric tolerances are read from gold: distance/perimeter 1 m, circular bearing 1°, density 0.001. Content correctness and assessed format compliance are reported separately; format compliance is only assessed for numeric open answers and MCQ. `--scoring legacy` explicitly selects the former nonnumeric extraction for historical compatibility and records its version.

The full denominator is always the gold-set size. By-relation and family statistics, missing-output counts and information-sufficient statistics are included. Geometry-only `geojson_exact` does not expose O06's layer attributes; its sufficient-subset denominator is 675 for a full city. The package's annotated maps include layer cards, so both `map_annotated` and `hybrid_geojson_exact` expose this information and use all 700 items for the sufficient subset. This applies to this package's offline renderer, not arbitrary historical image inputs.

## Structured editing

```sh
python scripts/run_api.py --prompts outputs/city/prompts/editing_geojson_exact.jsonl --out outputs/city/predictions/editing.jsonl --model YOUR_MODEL --base-url YOUR_API_ROOT
python scripts/benchmark.py evaluate-editing --tasks outputs/city/editing/tasks.jsonl --predictions outputs/city/predictions/editing.jsonl --out outputs/city/reports/editing.json
```

Model output is one transform JSON object. Examples:

```json
{"op":"translate","dx":12.5,"dy":-8.0}
{"op":"rotate","angle_deg":45}
{"op":"scale","factor":1.2}
{"op":"snap","to":"F1"}
{"op":"attribute_edit","layer":2}
```

Translation is in task metres. Rotation is counterclockwise about F0's centroid; scale is also about that centroid. Snap uses the named reference geometry. Only F0 is changed by the executor. Prediction rows require `task_id` and either `prediction` containing the model transform JSON, or a parsed `transform` object. An archival `prediction_wkt` adapter is also accepted for legacy exports; this is an evaluator adapter, not the transform-JSON model output protocol.

The final prediction must be parseable, nonempty, geometrically valid and satisfy every declared constraint. Geometric edits must change F0. O06 allows unchanged geometry but requires the predicted layer attribute. Reference `context.predicted_F0_layer` is used **only** to validate a reference edit; it is removed for model scoring. It cannot substitute for a missing model attribute. Directional rules and T04/O08 contact tolerances follow the task constraints.

Public Editing prompts contain anonymous feature IDs and task constraints, retaining the required layer information. QA prompts use fixed option order. Reference answers and source identifiers are not sent to the model.

The runner allows one corrective retry for undecodable/invalid or unchanged structured output. It reconstructs the initial geometry from the public scene already in the user message, and does not inspect gold or report constraint satisfaction. The evaluator scores the final recorded attempt. Transport retries are separate from this corrective retry. Resume requires identical prompt hashes, model, API root, API format and generation parameters.

## Offline images and raster editing

`map_annotated` sends the image, question/instruction and necessary constraints, withholding vector coordinates. `hybrid_geojson_exact` sends the matching image plus the structured input. Both use the same frozen scene geometry as GeoJSON. Preview prompts cover 28 items per task with `--per-relation 1`; use full prompts for full runs. A preview prediction file can be scored against the full gold with `--allow-partial`, but its score will include all missing outputs as failures. For a true preview-only denominator, construct matching subsets of both items/tasks and gold.

`rendering.py` fixes the plot rectangle and metres-per-pixel mapping. Frame sidecars document the affine conversion; the evaluator recomputes it from the initial scene instead of trusting a supplied prediction transform. PNGs are 1536×1536, x rightwards and y downwards. Pixel outputs use a single GeoJSON geometry object for F0 and, when required, a layer:

Frame sidecars hash both the visible initial scene and the PNG. Prompt preparation verifies those hashes and binds the image/frame hashes into each prompt; inference rejects altered images or metadata. Resume also binds all input image hashes. Frames depend only on initial geometry, so reference edits may legitimately move outside the displayed canvas. Finite off-canvas pixel coordinates are accepted and inverse-projected normally; reference diagnostics report this coverage instead of silently claiming all gold is visible.

```json
{"geometry":{"type":"Point","coordinates":[750,720]}}
```

```sh
python scripts/benchmark.py evaluate-editing --tasks outputs/city/editing/tasks.jsonl --predictions outputs/city/predictions/pixel_edits.jsonl --raster --out outputs/city/reports/pixel_editing.json
```

The evaluator maps pixels back to task metres. Raster success requires a valid changed edit satisfying the strict constraints, or an edit within 4 pixels Hausdorff distance of the reference. Required attributes must still match; O06 may keep geometry unchanged. `strict_CSR` remains available per item and is distinct from `raster_tolerant_success`. The simple pixel runner retries parse/validity failures, but does not detect unchanged geometry from pixels before scoring; label this difference if comparing retry policies.

Image/Together inputs use the `offline_metric_v1` metric renderer. This input condition differs from the paper's OSM-basemap condition. Record the renderer, source data, seed, CRS and software versions with model results.

## Running APIs

Set `BENCHMARK_API_KEY` using the syntax of your shell. `--key-env` selects another environment variable. `--api-format chat` appends `/chat/completions` to your API root; `--api-format responses` appends `/responses`. Pass the API root including `/v1` if your provider uses it. Models and roots are user-selected; the code does not assume provider-specific model availability or reasoning settings. `--temperature` is optional because some endpoints omit it. Record provider-specific settings with the experiment if extending the runner.

The runner sends only `messages` and optional image bytes. No gold file is loaded. `--dry-run` validates requests without a key or network call. Errors are written separately; missing predictions stay missing until a resumed run succeeds or are explicitly counted as failures by the evaluator.
