---
name: construction-progress-monitor
description: >
  Monitor construction progress from multi-temporal satellite imagery.
  Classifies project stages (clearing, earthwork, foundation, structure,
  finishing, completed) using spectral indices and detects stagnation.
  Use when tracking infrastructure projects, auditing construction
  timelines, or generating progress reports from remote sensing data.
---

# Construction Progress Monitor

Monitors construction site evolution from multi-temporal imagery using
spectral indices and a state machine to classify project stages.

## Trigger

Use when the user wants to:
- Track construction progress from satellite imagery
- Classify project stages (clearing → earthwork → foundation → structure → finishing → completed)
- Detect stagnant projects (no change for N periods)
- Generate progress reports for infrastructure projects
- Audit construction timelines against planned schedules

## CLI Usage

```bash
# Basic monitoring with project boundaries and schedule
python scripts/construction_progress_monitor.py \
  --projects projects.geojson \
  --schedule schedule.csv \
  --monitor-period 2024-01,2024-04,2024-07,2024-10

# With custom stage schema and stagnation threshold
python scripts/construction_progress_monitor.py \
  --projects projects.geojson \
  --schedule schedule.csv \
  --monitor-period 2024-01,2024-04,2024-07,2024-10 \
  --stage-schema stages.json \
  --stagnation-periods 2 \
  --imagery-source sentinel2

# Specify output directory
python scripts/construction_progress_monitor.py \
  --projects projects.geojson \
  --schedule schedule.csv \
  --monitor-period 2024-01,2024-04 \
  --output-dir ./cpm-output
```

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `--projects` | required | Project boundaries with IDs (GeoJSON) |
| `--schedule` | required | Planned schedule CSV (project_id, node, date) |
| `--monitor-period` | required | Comma-separated monitoring dates (YYYY-MM) |
| `--stage-schema` | built-in | Custom stage definition JSON |
| `--imagery-source` | sentinel2 | Imagery source identifier |
| `--stagnation-periods` | 2 | Periods of no change to flag stagnation |
| `--output-dir` | ./cpm-output | Output directory |

## Output

| File | Description |
|---|---|
| `project_status.geojson` | Per-project current stage and metrics |
| `progress_timeseries.csv` | Stage timeline per project |
| `stage_map.tif` | Raster stage classification map |
| `exceptions.xlsx` | Stagnation and anomaly report |

## Construction Stages

| Stage | Code | Key Indicators |
|---|---|---|
| clearing | 0 | High bare soil, low NDVI, low NDBI |
| earthwork | 1 | Dominant bare soil, minimal vegetation |
| foundation | 2 | Mixed bare soil and built material |
| structure | 3 | High NDBI, low NDVI, built materials |
| finishing | 4 | Moderate NDBI, increasing NDVI |
| completed | 5 | Low NDBI, high NDVI, stable |

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | Argument error |
| 3 | Dependency missing |
| 6 | Data validation failure |
| 7 | Processing failure |


## 数据下载

本 skill 可自动从 Microsoft Planetary Computer 下载数据 (无需 API key):

```bash
python construction_progress_monitor.py --bbox 116,39,117,40 --date-range 2024-06-01,2024-06-30 --output-dir <tmp>
```

- `--bbox W,S,E,N`: WGS-84 边界框 (西, 南, 东, 北)
- `--date-range START,END`: 日期范围 (YYYY-MM-DD,YYYY-MM-DD)
- `--aoi-file <path.geojson>`: 替代 --bbox 的 GeoJSON 多边形
- `--cache-dir <path>`: 缓存目录 (默认 ~/.geoskill_cache)

当用户只给 `--bbox + --date-range` (没有 `--image`) 时，skill 自动下载数据。
当用户给 `--image` 时，走原文件路径 (向后兼容)。
