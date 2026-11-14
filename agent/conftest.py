"""Make the agent package importable when pytest runs from agent/."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
