"""
backend/conftest.py

Pytest configuration and environment bootstrap for PELDRUN Core test suites.
Ensures backend directory is automatically registered in sys.path.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Register backend root directory in sys.path
backend_root = Path(__file__).resolve().parent
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))