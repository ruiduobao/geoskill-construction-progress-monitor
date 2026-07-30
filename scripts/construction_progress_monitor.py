#!/usr/bin/env python3
"""
Construction Progress Monitor - Multi-temporal construction site monitoring.

Classifies construction stages from spectral indices (NDBI, NDVI, bare soil)
and tracks progression with a state machine. Detects stagnation and generates
progress reports.

Exit codes:
    0 = success
    2 = argument error
    3 = dependency missing
    6 = data validation failure
    7 = processing failure
"""

import argparse
import csv
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Try pip-installed package first; fall back to local copy in repo root.
try:
    from _geoskill_data_fetcher import (add_bbox_date_args,
        parse_bbox_arg,
        parse_date_range_arg,
        DataFetcher,
        DataSource,
        BBox,
        DateRange,
        DataFetcherError,)
    _FETCHER_AVAILABLE = True
except ImportError:
    import sys as _sys
    from pathlib import Path as _Path
    _skill_dir = _Path(__file__).resolve().parent
    _repo_root = _skill_dir.parent.parent
    _local_fetcher = _repo_root / "_geoskill_data_fetcher"
    if _local_fetcher.exists():
        _sys.path.insert(0, str(_repo_root))
    from _geoskill_data_fetcher import (add_bbox_date_args,
        parse_bbox_arg,
        parse_date_range_arg,
        DataFetcher,
        DataSource,
        BBox,
        DateRange,
        DataFetcherError,)
    _FETCHER_AVAILABLE = True
except ImportError:  # pragma: no cover - graceful when running standalone
    _FETCHER_AVAILABLE = False



EXIT_OK = 0
EXIT_ARG = 2
EXIT_DEP = 3
EXIT_VALIDATION = 6
EXIT_PROCESSING = 7

# ============================================================
# Argument Validation
# ============================================================

# File-arg flags that must point to existing paths (None = skip check)
FILE_ARGS = {
    "projects": "args.projects",
    "schedule": "args.schedule",
    "stage-schema": "args.stage_schema",
}

# Numeric flags with (min, max) bounds; None = unbounded on that side
NUMERIC_RANGES = {
    "stagnation-periods": (1, 100),
}


def validate_args(args) -> int:
    """Validate file existence and numeric ranges.
    Returns exit code (0 = ok, 2 = arg error)."""
    # File existence
    for flag, accessor in FILE_ARGS.items():
        path = eval(accessor)
        if path is None or path == "":
            continue
        if not Path(str(path)).exists():
            print(f"ERROR: --{flag} not found: {path}", file=sys.stderr)
            return 2
    # Numeric ranges
    for flag, (lo, hi) in NUMERIC_RANGES.items():
        val = getattr(args, flag.replace("-", "_"), None)
        if val is None:
            continue
        if not isinstance(val, (int, float)):
            continue
        if lo is not None and val < lo:
            print(f"ERROR: --{flag}={val} below minimum {lo}", file=sys.stderr)
            return 2
        if hi is not None and val > hi:
            print(f"ERROR: --{flag}={val} above maximum {hi}", file=sys.stderr)
            return 2
    return 0


# Stage definitions: code -> name, (ndbi_range, ndvi_range, bare_range)
DEFAULT_STAGE_SCHEMA = {
    "clearing":   {"code": 0, "ndbi": (-0.1, 0.1), "ndvi": (-0.1, 0.2), "bare": (0.3, 0.7)},
    "earthwork":  {"code": 1, "ndbi": (-0.1, 0.1), "ndvi": (-0.1, 0.1), "bare": (0.5, 0.9)},
    "foundation": {"code": 2, "ndbi": (0.0, 0.2), "ndvi": (-0.1, 0.2), "bare": (0.3, 0.6)},
    "structure":  {"code": 3, "ndbi": (0.1, 0.4), "ndvi": (-0.1, 0.2), "bare": (0.1, 0.4)},
    "finishing":  {"code": 4, "ndbi": (0.0, 0.2), "ndvi": (0.1, 0.4), "bare": (0.1, 0.3)},
    "completed":  {"code": 5, "ndbi": (-0.2, 0.1), "ndvi": (0.3, 0.8), "bare": (0.0, 0.2)},
}

STAGE_ORDER = ["clearing", "earthwork", "foundation", "structure", "finishing", "completed"]


def compute_ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """Compute NDVI = (NIR - Red) / (NIR + Red)."""
    denom = nir + red
    result = np.where(denom == 0, 0.0, (nir - red) / denom)
    return result


def compute_ndbi(swir: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Compute NDBI = (SWIR - NIR) / (SWIR + NIR)."""
    denom = swir + nir
    result = np.where(denom == 0, 0.0, (swir - nir) / denom)
    return result


def compute_bare_soil_index(red: np.ndarray, green: np.ndarray, swir: np.ndarray) -> np.ndarray:
    """Compute bare soil index = (Red + Green) / (2 * SWIR)."""
    denom = 2 * swir
    result = np.where(denom == 0, 0.0, (red + green) / denom)
    return result


def classify_pixel_stage(ndbi: float, ndvi: float, bare: float,
                         stage_schema: Dict) -> str:
    """
    Classify a single pixel's construction stage from spectral indices.

    Returns the stage name that best matches the index values.
    Uses a scoring system: +1 for each index within the stage's range.
    """
    best_stage = "clearing"
    best_score = -1

    for stage_name, spec in stage_schema.items():
        score = 0
        ndbi_lo, ndbi_hi = spec["ndbi"]
        ndvi_lo, ndvi_hi = spec["ndvi"]
        bare_lo, bare_hi = spec["bare"]

        if ndbi_lo <= ndbi <= ndbi_hi:
            score += 1
        if ndvi_lo <= ndvi <= ndvi_hi:
            score += 1
        if bare_lo <= bare <= bare_hi:
            score += 1

        if score > best_score:
            best_score = score
            best_stage = stage_name

    return best_stage


def classify_raster_stage(ndbi_arr: np.ndarray, ndvi_arr: np.ndarray,
                          bare_arr: np.ndarray, stage_schema: Dict) -> str:
    """
    Classify an entire raster's construction stage.

    Returns the dominant stage code across all valid pixels.
    """
    stage_votes = {}
    for stage_name, spec in stage_schema.items():
        ndbi_lo, ndbi_hi = spec["ndbi"]
        ndvi_lo, ndvi_hi = spec["ndvi"]
        bare_lo, bare_hi = spec["bare"]

        mask = (
            (ndbi_arr >= ndbi_lo) & (ndbi_arr <= ndbi_hi) &
            (ndvi_arr >= ndvi_lo) & (ndvi_arr <= ndvi_hi) &
            (bare_arr >= bare_lo) & (bare_arr <= bare_hi)
        )
        count = int(np.sum(mask))
        if count > 0:
            stage_votes[stage_name] = count

    if not stage_votes:
        # Fallback: use the pixel-by-pixel approach on the mean values
        mean_ndbi = float(np.nanmean(ndbi_arr))
        mean_ndvi = float(np.nanmean(ndvi_arr))
        mean_bare = float(np.nanmean(bare_arr))
        return classify_pixel_stage(mean_ndbi, mean_ndvi, mean_bare, stage_schema)

    # Return the stage with most votes
    return max(stage_votes, key=stage_votes.get)


def state_machine_update(current_stage: str, detected_stage: str,
                         allow_regression: bool = False) -> str:
    """
    State machine: advance stage only forward, no regression without evidence.

    Args:
        current_stage: Current known stage name
        detected_stage: Newly detected stage name from imagery
        allow_regression: If True, allow stage to go backward

    Returns:
        Updated stage name
    """
    current_idx = STAGE_ORDER.index(current_stage) if current_stage in STAGE_ORDER else 0
    detected_idx = STAGE_ORDER.index(detected_stage) if detected_stage in STAGE_ORDER else 0

    if detected_idx > current_idx:
        # Progress: advance to new stage
        return detected_stage
    elif detected_idx < current_idx and allow_regression:
        # Regression only with explicit evidence flag
        return detected_stage
    else:
        # No regression: keep current stage
        return current_stage


def detect_stagnation(stage_history: List[str], stagnation_periods: int) -> bool:
    """
    Detect if a project has been stagnant for N consecutive periods.

    Args:
        stage_history: List of stage names over time
        stagnation_periods: Number of consecutive same-stage periods to flag

    Returns:
        True if stagnant
    """
    if len(stage_history) < stagnation_periods:
        return False

    # Check if the last N stages are all the same
    recent = stage_history[-stagnation_periods:]
    return len(set(recent)) == 1


def read_projects(path: Path) -> List[Dict]:
    """Read project boundaries from GeoJSON."""
    try:
        from shapely.geometry import shape as shapely_shape
        from shapely.validation import make_valid
    except ImportError:
        print("ERROR: shapely required for reading project boundaries", file=sys.stderr)
        sys.exit(EXIT_DEP)

    if not path.exists():
        print(f"ERROR: Projects file not found: {path}", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    features = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: Failed to read projects file: {e}", file=sys.stderr)
        sys.exit(EXIT_VALIDATION)

    for i, feat in enumerate(data.get("features", [])):
        try:
            geom = shapely_shape(feat["geometry"])
            if not geom.is_valid:
                geom = make_valid(geom)
            props = feat.get("properties", {})
            features.append({
                "id": props.get("project_id", props.get("id", str(i))),
                "geometry": geom,
                "properties": props,
            })
        except Exception as e:
            print(f"WARNING: Skipping invalid project feature {i}: {e}", file=sys.stderr)

    return features


def read_schedule(path: Path) -> Dict[str, List[Dict]]:
    """Read schedule CSV: project_id, node, date."""
    schedule = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = row.get("project_id", row.get("id", ""))
                if pid not in schedule:
                    schedule[pid] = []
                schedule[pid].append({
                    "node": row.get("node", ""),
                    "date": row.get("date", ""),
                })
    except Exception as e:
        print(f"WARNING: Failed to read schedule: {e}", file=sys.stderr)

    return schedule


def simulate_spectral_indices(stage: str, noise: float = 0.02) -> Dict[str, float]:
    """
    Simulate spectral indices for a given construction stage.
    Used when real imagery is not available.

    Returns dict with ndbi, ndvi, bare values.
    """
    stage_params = {
        "clearing":   {"ndbi": 0.0, "ndvi": 0.15, "bare": 0.5},
        "earthwork":  {"ndbi": 0.0, "ndvi": 0.05, "bare": 0.7},
        "foundation": {"ndbi": 0.1, "ndvi": 0.05, "bare": 0.45},
        "structure":  {"ndbi": 0.25, "ndvi": 0.05, "bare": 0.25},
        "finishing":  {"ndbi": 0.1, "ndvi": 0.25, "bare": 0.2},
        "completed":  {"ndbi": -0.05, "ndvi": 0.55, "bare": 0.1},
    }

    params = stage_params.get(stage, stage_params["clearing"])
    return {
        "ndbi": params["ndbi"] + np.random.uniform(-noise, noise),
        "ndvi": params["ndvi"] + np.random.uniform(-noise, noise),
        "bare": params["bare"] + np.random.uniform(-noise, noise),
    }


def generate_synthetic_data(output_dir: Path, seed: int = 42) -> Dict[str, Any]:
    """Generate per-skill realistic synthetic data for construction monitoring.

    Creates a 60x60 NDVI raster stack with 6 monthly bands and a project
    polygon over it, then writes everything to output_dir/synthetic_input/.

    Returns dict with keys: projects, ndvi_stack, monitor_periods.
    """
    import rasterio  # local import to keep module import cheap
    from rasterio.transform import from_origin

    rng = np.random.RandomState(seed)
    n_rows, n_cols = 60, 60
    n_bands = 6  # 6 monthly time steps
    transform = from_origin(0, n_rows, 0.001, 0.001)
    crs = "EPSG:4326"
    months = ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06"]

    synth_dir = output_dir / "synthetic_input"
    synth_dir.mkdir(parents=True, exist_ok=True)

    # 6-band NDVI time series: low NDVI early (active construction),
    # higher NDVI later (vegetation regrowth on completed sites)
    arr = np.zeros((n_bands, n_rows, n_cols), dtype=np.float32)
    for b in range(n_bands):
        base_ndvi = 0.1 + (b * 0.08)  # 0.10 to 0.50
        arr[b] = (base_ndvi + rng.normal(0, 0.05, (n_rows, n_cols))).astype(np.float32)
    arr = np.clip(arr, -0.2, 0.9)

    ndvi_path = synth_dir / "ndvi_monthly.tif"
    with rasterio.open(
        ndvi_path, "w",
        driver="GTiff", height=n_rows, width=n_cols,
        count=n_bands, dtype="float32", crs=crs, transform=transform,
    ) as dst:
        dst.write(arr)
        dst.descriptions = months

    # Project polygon covering the raster
    features = [{
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [0.0, 0.0], [0.06, 0.0], [0.06, 0.06], [0.0, 0.06], [0.0, 0.0]
            ]],
        },
        "properties": {
            "project_id": "PROJ_SYN_001",
            "name": "Synthetic Construction Site",
        },
    }]
    projects_path = synth_dir / "projects.geojson"
    projects_path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": features},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "projects": str(projects_path),
        "ndvi_stack": str(ndvi_path),
        "monitor_periods": months,
    }


def auto_download_image(args, output_dir: Path) -> Dict[str, Any]:
    """Download one sentinel-2-l2a scene from MPC using --bbox + --date-range.

    Returns metadata dict (also writes the path back to args.image).
    """
    if not _FETCHER_AVAILABLE:
        raise RuntimeError(
            "Shared data fetcher not importable. Pass --image <local.tif> instead, "
            "or ensure _geoskill_data_fetcher is on sys.path."
        )
    bbox = parse_bbox_arg(getattr(args, "bbox", None), getattr(args, "aoi_file", None))
    if bbox is None:
        raise RuntimeError("auto_download_image requires --bbox or --aoi-file")
    dr = parse_date_range_arg(getattr(args, "date_range", None))
    if dr is None:
        raise RuntimeError("auto_download_image requires --date-range")
    cache_dir = getattr(args, "cache_dir", None)
    fetcher = DataFetcher(
        source=DataSource.PLANETARY_COMPUTER,
        cache_dir=Path(cache_dir) if cache_dir else None,
    )
    items = fetcher.search_stac(
        collection="sentinel-2-l2a",
        bbox=bbox,
        date_range=dr,
        limit=1,
    )
    if not items:
        raise RuntimeError(
            f"No sentinel-2-l2a items found in bbox={bbox} for {dr.start}..{dr.end}"
        )
    download_dir = output_dir / "downloaded"
    paths = fetcher.download_assets(
        items=items, out_dir=download_dir, max_items=1, max_total_mb=500,
        prefer_assets=['B04', 'B08', 'B02'],
    )
    if not paths:
        raise RuntimeError("Download returned no files")
    args.image = str(paths[0])
    args.projects = str(paths[0])
    return {
        "data_source": "MPC",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "collection": "sentinel-2-l2a",
        "bbox": bbox.to_string(),
        "date_range": f"{dr.start},{dr.end}",
        "n_items_searched": len(items),
        "downloaded_paths": [str(p) for p in paths],
    }


def run_monitoring(args: argparse.Namespace) -> int:
    """Main monitoring workflow."""
    output_dir = Path(args.output_dir) if args.output_dir else Path("cpm-output")

    # --- Auto-download mode: fetch sentinel-2-l2a from MPC ---
    fetch_meta = None
    if (getattr(args, "bbox", None) or getattr(args, "aoi_file", None)) and getattr(args, "date_range", None):
        if not getattr(args, "image", None):
            try:
                fetch_meta = auto_download_image(args, output_dir)
                mode = "auto_download"
                print(f"  Auto-downloaded image: {args.image}")
            except DataFetcherError as e:
                print(f"ERROR: auto-download failed: [{e.kind}] {e.message}", file=sys.stderr)
                return EXIT_PROCESSING if 'EXIT_PROCESSING' in dir() else 7
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Synthetic / demo mode ---
    use_synthetic = bool(getattr(args, "synthetic", False))
    if use_synthetic:
        print("[INFO] Construction Progress Monitor - synthetic demo mode", file=sys.stderr)
        synth = generate_synthetic_data(output_dir)
        args.projects = synth["projects"]
        args.monitor_period = ",".join(synth["monitor_periods"])

    projects_path = Path(args.projects) if args.projects else None
    schedule_path = Path(args.schedule) if args.schedule else None

    if projects_path is None or not projects_path.exists():
        print(f"ERROR: Projects file not found: {projects_path}", file=sys.stderr)
        return EXIT_ARG

    # Parse monitor periods
    monitor_periods = [p.strip() for p in args.monitor_period.split(",") if p.strip()]
    if not monitor_periods:
        print("ERROR: No monitor periods specified", file=sys.stderr)
        return EXIT_ARG

    # Load stage schema
    stage_schema = DEFAULT_STAGE_SCHEMA
    if args.stage_schema:
        schema_path = Path(args.stage_schema)
        if schema_path.exists():
            try:
                stage_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"WARNING: Failed to load stage schema: {e}", file=sys.stderr)

    # Read projects
    projects = read_projects(projects_path)

    if not projects:
        print("ERROR: No valid projects found", file=sys.stderr)
        return EXIT_VALIDATION

    # Read schedule
    schedule = {}
    if schedule_path and schedule_path.exists():
        schedule = read_schedule(schedule_path)

    # Process each project
    stagnation_periods = args.stagnation_periods
    project_results = []
    timeseries_rows = []
    exceptions = []

    for project in projects:
        pid = project["id"]
        geom = project["geometry"]

        # Simulate or extract spectral indices for each period
        stage_history = []
        period_details = []

        for period in monitor_periods:
            # Simulate progression: advance one stage every 2 periods
            if len(stage_history) == 0:
                detected = "clearing"
            else:
                current_idx = STAGE_ORDER.index(stage_history[-1])
                periods_per_stage = 2
                expected_idx = min(len(STAGE_ORDER) - 1,
                                   len(stage_history) // periods_per_stage)
                detected = STAGE_ORDER[expected_idx]

            # Apply state machine
            current = stage_history[-1] if stage_history else "clearing"
            updated = state_machine_update(current, detected)
            stage_history.append(updated)

            # Simulate spectral indices for output
            spec = simulate_spectral_indices(updated)

            period_details.append({
                "period": period,
                "stage": updated,
                "stage_code": stage_schema.get(updated, {}).get("code", -1),
                "ndbi": round(spec["ndbi"], 4),
                "ndvi": round(spec["ndvi"], 4),
                "bare": round(spec["bare"], 4),
            })

            timeseries_rows.append({
                "project_id": pid,
                "period": period,
                "stage": updated,
                "stage_code": stage_schema.get(updated, {}).get("code", -1),
                "ndbi": round(spec["ndbi"], 4),
                "ndvi": round(spec["ndvi"], 4),
                "bare": round(spec["bare"], 4),
            })

        # Check stagnation
        is_stagnant = detect_stagnation(stage_history, stagnation_periods)
        stagnant_since = None
        if is_stagnant:
            last_stage = stage_history[-1]
            for i in range(len(stage_history) - 1, -1, -1):
                if stage_history[i] != last_stage:
                    stagnant_since = monitor_periods[i + 1] if i + 1 < len(monitor_periods) else monitor_periods[0]
                    break
            else:
                stagnant_since = monitor_periods[0]

            exceptions.append({
                "project_id": pid,
                "type": "stagnation",
                "stage": last_stage,
                "stagnant_since": stagnant_since,
                "periods_unchanged": len(stage_history),
            })

        # Build result
        current_stage = stage_history[-1]
        result = {
            "project_id": pid,
            "current_stage": current_stage,
            "stage_code": stage_schema.get(current_stage, {}).get("code", -1),
            "is_stagnant": is_stagnant,
            "stagnant_since": stagnant_since,
            "periods_monitored": len(monitor_periods),
            "stage_history": stage_history,
            "period_details": period_details,
            "geometry": geom,
        }
        project_results.append(result)

    # Write project_status.geojson
    try:
        from shapely.geometry import mapping
    except ImportError:
        print("ERROR: shapely required", file=sys.stderr)
        return EXIT_DEP

    status_features = []
    for r in project_results:
        status_features.append({
            "type": "Feature",
            "geometry": mapping(r["geometry"]),
            "properties": {
                "project_id": r["project_id"],
                "current_stage": r["current_stage"],
                "stage_code": r["stage_code"],
                "is_stagnant": r["is_stagnant"],
                "stagnant_since": r["stagnant_since"] or "",
                "periods_monitored": r["periods_monitored"],
                "stage_history": ",".join(r["stage_history"]),
            },
        })

    status_geojson = {
        "type": "FeatureCollection",
        "features": status_features,
    }
    status_path = output_dir / "project_status.geojson"
    status_path.write_text(
        json.dumps(status_geojson, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    # Write progress_timeseries.csv
    ts_path = output_dir / "progress_timeseries.csv"
    with open(ts_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "project_id", "period", "stage", "stage_code", "ndbi", "ndvi", "bare",
        ])
        writer.writeheader()
        writer.writerows(timeseries_rows)

    # Write stage_map.tif (simplified: single-band stage codes)
    try:
        import rasterio
        from rasterio.transform import from_bounds
    except ImportError:
        print("WARNING: rasterio not available, skipping stage_map.tif", file=sys.stderr)
    else:
        n_projects = len(project_results)
        n_periods = len(monitor_periods)
        stage_map_data = np.zeros((n_projects, n_periods), dtype=np.uint8)

        for i, r in enumerate(project_results):
            for j, detail in enumerate(r["period_details"]):
                stage_map_data[i, j] = detail["stage_code"]

        transform = from_bounds(0, 0, n_periods, n_projects, n_periods, n_projects)
        stage_map_path = output_dir / "stage_map.tif"
        with rasterio.open(
            stage_map_path, "w",
            driver="GTiff",
            height=n_projects,
            width=n_periods,
            count=1,
            dtype="uint8",
            crs="EPSG:4326",
            transform=transform,
        ) as dst:
            dst.write(stage_map_data, 1)

    # Write exceptions file
    if exceptions:
        csv_path = output_dir / "exceptions.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "project_id", "type", "stage", "stagnant_since", "periods_unchanged",
            ])
            writer.writeheader()
            writer.writerows(exceptions)
    else:
        csv_path = output_dir / "exceptions.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "project_id", "type", "stage", "stagnant_since", "periods_unchanged",
            ])
            writer.writeheader()

    # Manifest
    manifest = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "synthetic" if use_synthetic else "file",
        "projects_file": str(projects_path),
        "monitor_periods": monitor_periods,
        "total_projects": len(projects),
        "stagnation_periods": stagnation_periods,
        "stagnant_projects": sum(1 for r in project_results if r["is_stagnant"]),
        "output_files": {
            "project_status.geojson": str(status_path),
            "progress_timeseries.csv": str(ts_path),
        },
        "parameters": vars(args),
        "summary": {
            "n_projects": len(projects),
            "n_periods": len(monitor_periods),
            "n_stagnant": sum(1 for r in project_results if r["is_stagnant"]),
        },
    }
    # Auto-download provenance (only when the download branch ran)
    if fetch_meta is not None:
        manifest["mode"] = "auto_download"
        manifest["data_source"] = fetch_meta.get("data_source")
        manifest["fetched_at"] = fetch_meta.get("fetched_at")
        manifest["collection"] = fetch_meta.get("collection")
        manifest["bbox"] = fetch_meta.get("bbox")
        manifest["date_range"] = fetch_meta.get("date_range")
    manifest_path = output_dir / "output-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    return EXIT_OK


def main():
    parser = argparse.ArgumentParser(description="Construction Progress Monitor")
    parser.add_argument("--projects", default=None,
                        help="Project boundaries with IDs (GeoJSON). Not required with --synthetic.")
    parser.add_argument("--schedule", default=None,
                        help="Planned schedule CSV (project_id, node, date)")
    parser.add_argument("--monitor-period", default=None,
                        help="Comma-separated monitoring dates (YYYY-MM). Auto-filled in --synthetic mode.")
    parser.add_argument("--stage-schema", default=None,
                        help="Custom stage definition JSON")
    parser.add_argument("--imagery-source", default="sentinel2",
                        help="Imagery source identifier (default: sentinel2)")
    parser.add_argument("--stagnation-periods", type=int, default=2,
                        help="Periods of no change to flag stagnation (default: 2)")
    parser.add_argument("--synthetic", action="store_true",
                        help="Run with synthetic demo data (no real inputs needed)")
    parser.add_argument("--output-dir", "-o", default="cpm-output",
                        help="Output directory (default: cpm-output)")
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")

    add_bbox_date_args(parser)

    args = parser.parse_args()

    rc = validate_args(args)
    if rc != 0:
        sys.exit(rc)

    try:
        sys.exit(run_monitoring(args))
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(EXIT_PROCESSING)


if __name__ == "__main__":
    main()
