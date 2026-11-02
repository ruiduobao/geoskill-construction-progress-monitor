# geoskill-construction-progress-monitor

> Monitor construction progress from multi-temporal satellite imagery. Classifies project stages (clearing, earthwork, foundation, structure, finishing, completed) using spectral indices and detects stagnation. Use when tracking infrastructure projects, auditing construction timelines, or generating progress reports from remote sensing data.

See [SKILL.md](SKILL.md) for full usage, inputs and outputs.

## Quick start

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

## License

MIT
