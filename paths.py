"""
Where FreedomNAS keeps its files.

Two kinds of location, found two different ways:

  * The program itself. core, profiles, webui, installer and modules share
    one root — /usr/lib/freedomnas on Linux, /usr/local/lib/freedomnas on
    FreeBSD — and this file sits at <root>/core/freedomnas/paths.py, so the
    root is found from here. It cannot come from a profile: the profiles
    are what it is needed to find.

  * The configuration. Where an edition keeps it is data, not code: the
    OS profile's `paths.config_dir` (/etc/freedomnas on Linux,
    /usr/local/etc/freedomnas on FreeBSD), read through the engine that
    loaded that profile.

In a development tree the checkouts sit side by side instead, and the
fallbacks below find them there.
"""

from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).resolve().parent

INSTALL_ROOT = _HERE.parent.parent
"""<root> of an installed system: this file is <root>/core/freedomnas/paths.py."""


def _first_dir(candidates: List[Path]) -> Optional[Path]:
    for path in candidates:
        if path.is_dir():
            return path
    return None


def profiles_candidates() -> List[Path]:
    return [
        INSTALL_ROOT / "profiles",
        _HERE.parent / "freedomnas-profiles",
    ]


def profiles_dir() -> Optional[Path]:
    """The freedomnas-profiles tree, installed or checked out beside this one."""
    return _first_dir([p for p in profiles_candidates() if (p / "os").is_dir()])


def modules_candidates() -> List[Path]:
    return [
        _HERE / "modules",
        INSTALL_ROOT / "modules",
    ]


def _engine():
    try:
        from freedomnas.engine import get_engine
    except ImportError:  # running from the repository, not from the package
        from engine import get_engine  # type: ignore
    return get_engine()


def config_dir(engine=None) -> Path:
    """The edition's configuration directory, from its OS profile."""
    engine = engine or _engine()
    return Path(engine.profile["paths"]["config_dir"])


def state_dir(engine=None) -> Path:
    """Where the `.freedomnas` dataset mounts: the live config.yaml lives here."""
    return config_dir(engine) / "state"


def shared_dir(engine=None) -> Path:
    """The mountpoint a data pool's `.freedomnas` carries: never the state's own."""
    return config_dir(engine) / "shared"


def tls_dir(engine=None) -> Path:
    return config_dir(engine) / "tls"


def run_dir(engine=None) -> Path:
    """Runtime state that lasts until the next boot (the modules' health)."""
    engine = engine or _engine()
    return Path(engine.profile["paths"]["run_dir"])


def keyslot_dir(engine=None) -> Path:
    return config_dir(engine) / "keyslots"
