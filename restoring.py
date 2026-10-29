"""
The restore lock: while a restored or reinitialized configuration is being
applied, `<state>/restoring.json` exists and the WebUI serves only the page
that follows it. The reconcile writes each step into it as it goes. A
restore that ends well removes it; one with a failed step keeps it,
finished, until someone has seen which step failed and chose to continue.
"""

import datetime
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

try:
    from freedomnas import paths
except ImportError:  # running from the repository, not from the package
    import paths  # type: ignore

LOCK_NAME = "restoring.json"

_WORDS = (
    ("storage.pools.", "Pool"),
    ("share_groups.", "Group share"),
    ("users.", "Account"),
    ("smb_shares.", "SMB share"),
    ("nfs_shares.", "NFS share"),
    ("features.", "Feature"),
    ("services.boot.", "Service"),
)
_NAMES = {
    "system.hostname": "Hostname",
    "system.timezone": "Time zone",
    "system.ssh": "SSH",
    "system.power_button": "Power button",
    "network.interfaces": "Network",
}


def lock_file(engine=None) -> Path:
    return paths.state_dir(engine) / LOCK_NAME


def step_label(path: str) -> str:
    """A reconcile change's path as the page names the step."""
    for prefix, word in _WORDS:
        if path.startswith(prefix):
            return f"{word} {path[len(prefix):]}"
    return _NAMES.get(path, path)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write(path: Path, data: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read(path: Path) -> Optional[Dict]:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def begin(path: Path, reason: str = "") -> None:
    """Put the lock in place before anything is applied (and before the WebUI starts)."""
    _write(path, {"reason": reason, "started": _now(), "steps": [], "finished": False, "failed": False})


def clear(path: Path) -> bool:
    """Take a finished lock away (the user chose to continue). False while the restore still runs."""
    data = read(path)
    if data is not None and not data.get("finished"):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


class Progress:
    """The steps of one reconcile, written into the lock as they run."""

    def __init__(self, path: Path):
        self.path = path
        self.data = read(path) or {"reason": "", "started": _now()}
        self.data.update(steps=[], finished=False, failed=False)

    def plan(self, labels: List[str]) -> None:
        self.data["steps"] = [{"label": label, "state": "waiting", "error": ""} for label in labels]
        _write(self.path, self.data)

    def add(self, label: str) -> int:
        """A step learned of while running (a module the reconcile installed); returns its index."""
        self.data["steps"].append({"label": label, "state": "waiting", "error": ""})
        _write(self.path, self.data)
        return len(self.data["steps"]) - 1

    def start(self, index: int) -> None:
        if index < len(self.data["steps"]):
            self.data["steps"][index]["state"] = "running"
            _write(self.path, self.data)

    def done(self, index: int, errors: List[str]) -> None:
        if index < len(self.data["steps"]):
            step = self.data["steps"][index]
            step["state"] = "failed" if errors else "done"
            step["error"] = "\n".join(errors)
            _write(self.path, self.data)

    def finish(self, errors: List[str]) -> None:
        """Remove the lock when everything went well, else keep it finished with what failed."""
        if not errors:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        self.data.update(finished=True, failed=True, errors=errors, ended=_now())
        _write(self.path, self.data)
