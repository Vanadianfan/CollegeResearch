"""Shared project and data paths used by builders and visualizers."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_ROOT = PROJECT_ROOT / "raw_data"
