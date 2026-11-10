# GeoSpatialBench 1.2.0

[English](README.md) · [论文](https://arxiv.org/abs/2610.12881) · [数据要求](docs/DATA_CONTRACT.md) · [输入与评分协议](docs/PROTOCOL.md)

GeoSpatialBench 用于评测模型的空间关系理解和约束空间编辑能力，包含 **28 类空间关系**，支持 **GeoJSON、Image、Together** 三种输入，以及 **开放 QA、四选一 QA、Editing** 三种任务。

## 包含什么

- **Stockholm 固定题库**：700 个 QA 场景和 700 个独立 Editing 任务，每类关系各 25 题；附标准答案、公开提示和 Qwen3.5-9B 的 GeoJSON 示例预测。
- **源矢量样本**：一个 EPSG:3006 GeoPackage，包含 1,800 个 POI、2,400 条道路、1,400 栋建筑和 60 个区域。
- **构建与图像工具**：生成题库、公开提示和 1536×1536 离线地图，地图包含对象标签、米制坐标、北向和所需道路 layer 信息。
- **模型运行与评分工具**：支持兼容 Chat Completions 或 Responses 格式的接口，提供断点续跑、原始响应保存和自动评分。
- **配置与依赖**：城市配置模板、Python 3.13 依赖锁定文件、空间关系定义和输出格式说明。

## 快速开始

安装 Python 3.13，解压后进入本目录，运行：

```sh
python quickstart.py
```

也可以指定安装源：

```sh
python quickstart.py --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

启动器自动创建 `.venv` 并安装依赖，运行随包示例、生成新的城市题库，并准备三种输入的预览。预览包含每类一题，共 28 QA 场景和 28 Editing 任务。此命令不需要 API key，不调用模型接口。

终端最后显示 `PASS`。`outputs/latest_quickstart.json` 指向输出目录，目录内包含题库、提示、图片及结果。已有所需依赖时，可运行 `python quickstart.py --skip-install`。

PowerShell 也可运行 `powershell -ExecutionPolicy Bypass -File quickstart.ps1`。

## 评测自己的模型

以下示例使用 Windows PowerShell；Linux/macOS 将 `.\.venv\Scripts\python.exe` 替换为 `.venv/bin/python`。

先替换模型名和 API 根地址，检查请求格式：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py run --model YOUR_MODEL --base-url YOUR_API_ROOT --mode all --out outputs/my_model_preview --dry-run
```

设置 API key 后，用相同模型和地址执行推理与评分：

```powershell
$env:BENCHMARK_API_KEY = "YOUR_API_KEY"
.\.venv\Scripts\python.exe scripts/benchmark.py run --model YOUR_MODEL --base-url YOUR_API_ROOT --mode all --out outputs/my_model_preview --resume
```

默认使用 Stockholm 固定题库，每类一题。三种输入使用相同题目，各任务的评分分母为 28，全部九个条件共 252 个模型请求。只测 GeoJSON 时使用 `--mode geojson_exact`。

正式评测全部 700 题时，使用新的输出目录并增加 `--per-relation 0`：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py run --model YOUR_MODEL --base-url YOUR_API_ROOT --mode all --per-relation 0 --out outputs/my_model_full
```

全部九个条件共 6300 个模型请求。接口采用 Responses 格式时增加 `--api-format responses`。中断后保持输入、模型和参数一致，增加 `--resume` 继续。

| 输出路径 | 内容 |
|---|---|
| `summary.json` | 各输入与任务的最终分数 |
| `reports/` | 总体、关系分组和逐题评分 |
| `predictions/` | 原始响应、重试记录和模型设置 |
| `prompts/` | 模型输入 |
| `case/` | 使用的题目、标准答案和图片 |

模型接口只接收公开提示和图片。标准答案与参考编辑用于本地评分。

## 运行随包结果示例

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py example
```

此命令对随包预测评分，不调用模型。输出为 `outputs/stockholm_example/`。

| Stockholm / Qwen3.5-9B / GeoJSON | 正确数 | 分数 |
|---|---:|---:|
| 开放 QA | 309/700 | 44.14% |
| 四选一 QA | 316/700 | 45.14% |
| Editing 严格 CSR | 266/700 | 38.00% |

## 生成题库

使用随包 Stockholm 源矢量数据：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py check --config config/stockholm.example.json
.\.venv\Scripts\python.exe scripts/benchmark.py build --config config/stockholm.example.json
.\.venv\Scripts\python.exe scripts/benchmark.py prompts --case outputs/stockholm_new_build
```

输出包含 700 QA 场景、700 Editing 任务、标准答案和公开提示。新题目使用 `STODEMO` 前缀，需要生成对应的模型预测。

自己的城市数据需要 POI、道路、建筑、用地/区域四个逻辑图层，道路还需数字竖向 layer 字段。可复制 `config/city.example.json` 修改，或从多图层 GeoPackage 创建配置：

```powershell
.\.venv\Scripts\python.exe scripts/benchmark.py inspect local_data/city.gpkg
.\.venv\Scripts\python.exe scripts/benchmark.py init-config --data local_data/city.gpkg --city "My City" --id-prefix CITY --out config/my_city.json
.\.venv\Scripts\python.exe scripts/benchmark.py check --config config/my_city.json
.\.venv\Scripts\python.exe scripts/benchmark.py build --config config/my_city.json
```

检查配置中的图层名、字段映射、过滤条件、`output_dir` 和米制投影 CRS。相对路径以配置文件所在目录为基准，输出目录须为空。使用自己的题库时，为模型命令增加 `--case outputs/city`，路径应与配置的实际输出一致。

## 输入与评分

`geojson_exact` 为几何文本输入，`map_annotated` 为离线地图输入，`hybrid_geojson_exact` 为图像与几何文本组合输入。Image Editing 输出像素几何，GeoJSON/Together Editing 输出几何变换 JSON；具体格式见 [PROTOCOL.md](docs/PROTOCOL.md)。

Image/Together 使用 `offline_metric_v1` 离线米制地图。其图像与像素坐标协议区别于论文中的 OSM 底图条件；随包模型结果为单城 GeoJSON 示例。

## 目录

| 路径 | 内容 |
|---|---|
| `quickstart.py`、`quickstart.ps1` | 启动入口 |
| `scripts/` | 构建、提示、图像、模型接口和评分代码 |
| `config/` | 城市配置模板 |
| `registry/` | 空间关系定义 |
| `examples/stockholm/` | 固定题库、标准答案、提示、示例预测和源矢量样本 |
| `docs/` | 数据要求、空间关系和评分协议 |
| `requirements*.txt` | Python 依赖 |
| `RELEASE_MANIFEST.json`、`SHA256SUMS.txt` | 包文件清单与校验值 |

代码使用 [MIT](LICENSE)；OSM 派生几何署名为 **© OpenStreetMap contributors**，采用 [ODbL](LICENSE-DATA.md)。论文引用见 `CITATION.cff` 和 `citation.bib`。
