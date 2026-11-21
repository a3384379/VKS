"""Make the shared connector implementation available in source deployments."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages/vks-connect/src"))
