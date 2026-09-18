import sys
from pathlib import Path

REPO = Path(__file__).parent.parent

# Test this checkout, never an installed copy. Every module imports its
# siblings as `freedomnas.<name>` first and falls back to `<name>`; on a
# machine with freedomnas-core installed (/usr/lib/freedomnas/core via its
# .pth) the first form finds the *installed* modules, so the code under test
# would catch exception classes from another version than the one the tests
# raise — and failures would come and go with what happens to be installed.
sys.path[:] = [
    p for p in sys.path
    if not (Path(p or ".") / "freedomnas" / "__init__.py").is_file()
]
for name in [m for m in sys.modules if m == "freedomnas" or m.startswith("freedomnas.")]:
    del sys.modules[name]

sys.path.insert(0, str(REPO))
