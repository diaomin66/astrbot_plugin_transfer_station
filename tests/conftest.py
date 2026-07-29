from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PACKAGE_NAME = ROOT.name


def load_plugin_module(module_name: str):
    return importlib.import_module(f"{PACKAGE_NAME}.{module_name}")
