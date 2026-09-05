import sys
from pathlib import Path

# Make engine.py and config.py importable from tests/ without installation.
sys.path.insert(0, str(Path(__file__).parent.parent))
