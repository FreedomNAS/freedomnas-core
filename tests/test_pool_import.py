"""Importing an existing pool: look first without changing it, refuse what would mount over this system."""

import pytest

import pool_import
from engine import CommandExecutionError
from pool_import import ImportProblem

LISTING = """   pool: naspool
     id: 11200611346329343249
  state: ONLINE
 status: The pool was last accessed by another system.
 action: The pool can be imported using its name or numeric identifier and
\tthe '-f' flag.
    see: https://openzfs.github.io/openzfs-docs/msg/ZFS-8000-EY
 config:

\tnaspool     ONLINE
\t  sdb2      ONLINE

   pool: tank
     id: 42
  state: ONLINE
 action: The pool can be imported using its name or numeric identifier.
 config:

\ttank        ONLINE
\t  sdc       ONLINE

   pool: broken
     id: 7
  state: UNAVAIL
 status: One or more devices are missing from the system.
 action: The pool cannot be imported.
"""


def tree(pool, rows):
    """rows: (rel, mountpoint, source, canmount[, extra props])"""
    lines = []
    for row in rows:
        rel, mp, source, canmount = row[:4]
        extra = row[4] if len(row) > 4 else {}
        name = f"{pool}/{rel}" if rel else pool
        lines.append(f"{name}\tmountpoint\t{mp}\t{source}")
        lines.append(f"{name}\tcanmount\t{canmount}\tdefault")
        lines.append(f"{name}\tused\t1000\t-")
        for prop in ("encryption", "encryptionroot", "keyformat", "keystatus"):
            lines.append(f"{name}\t{prop}\t{extra.get(prop, 'off' if prop == 'encryption' else '-')}\t-")
    return "\n".join(lines)


DATA_POOL = [
    ("", "/tank", "default", "on"),
    ("media", "/tank/media", "inherited from tank", "on"),
    ("backup", "/srv/backup", "local", "on"),
]


class FakeEngine:
    def __init__(self, rows=DATA_POOL, listing=LISTING, failing=(), wrong_key=False):
        self.rows = rows
        self.listing = listing
        self.failing = set(failing)
        self.wrong_key = wrong_key
        self.calls = []
        self.unlocked = set()

    def execute(self, category, action, timeout=30, check=True, **kw):
        self.calls.append((action, kw))
        if action in self.failing:
            raise CommandExecutionError(f"{action} failed")
        if action == "pool_importable":
            return self.listing, ""
        if action == "dataset_tree":
            rows = []
            for row in self.rows:
                extra = dict(row[4]) if len(row) > 4 else {}
                if extra.get("keystatus") == "unavailable" and row[0] in self.unlocked:
                    extra["keystatus"] = "available"
                rows.append((*row[:4], extra))
            return tree(kw["pool_name"], rows), ""
        return "", ""

    def execute_with_stdin(self, category, action, stdin, timeout=30, **kw):
        self.calls.append((action, kw))
        if self.wrong_key:
            raise CommandExecutionError("Incorrect key provided")
        self.unlocked.add(kw["dataset"].split("/", 1)[1] if "/" in kw["dataset"] else "")
        return "", ""

    def actions(self):
        return [a for a, _ in self.calls]


def free(_path):
    return None


def test_the_listing_is_read_pool_by_pool():
    pools = pool_import.parse_importable(LISTING)
    assert [p["name"] for p in pools] == ["naspool", "tank", "broken"]
    nas, tank, broken = pools
    assert nas["id"] == "11200611346329343249" and nas["foreign"] and nas["importable"]
    assert "'-f' flag" in nas["action"]
    assert not tank["foreign"] and tank["importable"]
    assert not broken["importable"]


def test_nothing_to_import_is_an_empty_list():
    assert pool_import.parse_importable("no pools available to import") == []


def test_mountpoints_follow_a_new_name_and_a_new_root():
    datasets = pool_import.parse_tree(tree("tank", DATA_POOL), "tank")
    same = pool_import.project_mountpoints(datasets, "tank")
    assert same == {"": "/tank", "media": "/tank/media", "backup": "/srv/backup"}
    renamed = pool_import.project_mountpoints(datasets, "vault")
    assert renamed[""] == "/vault" and renamed["media"] == "/vault/media"
    moved = pool_import.project_mountpoints(datasets, "tank", "/mnt/data")
    assert moved == {"": "/mnt/data", "media": "/mnt/data/media", "backup": "/srv/backup"}


def test_a_child_of_an_unmounted_dataset_is_unmounted_too():
    rows = [("", "none", "local", "on"), ("a", "none", "inherited from p", "on")]
    datasets = pool_import.parse_tree(tree("p", rows), "p")
    assert pool_import.project_mountpoints(datasets, "p") == {"": "none", "a": "none"}


@pytest.mark.parametrize("rows", [
    [("", "none", "local", "on"), ("ROOT", "none", "local", "off"),
     ("ROOT/debian", "/", "local", "noauto")],
    [("", "/boot", "local", "on")],
    [("", "/", "local", "on")],
])
def test_another_systems_root_is_refused(rows):
    datasets = pool_import.parse_tree(tree("rpool", rows), "rpool")
    assert pool_import.os_root_reasons(datasets)


def test_a_freedomnas_state_dataset_is_not_a_system_root():
    rows = DATA_POOL + [(".freedomnas", "/etc/freedomnas/state", "local", "noauto")]
    datasets = pool_import.parse_tree(tree("tank", rows), "tank")
    assert pool_import.os_root_reasons(datasets) == []


def test_an_inherited_system_path_is_a_conflict_a_new_root_resolves():
    rows = [("", "/", "local", "off"), ("var", "/var", "inherited from p", "on")]
    datasets = pool_import.parse_tree(tree("p", rows), "p")
    assert pool_import.os_root_reasons(datasets) == []
    here = pool_import.project_mountpoints(datasets, "p")
    assert pool_import.mount_conflicts(datasets, here, free)[0]["reason"] == "a system directory"
    moved = pool_import.project_mountpoints(datasets, "p", "/mnt/p")
    assert pool_import.mount_conflicts(datasets, moved, free) == []


def test_an_occupied_directory_is_a_conflict():
    datasets = pool_import.parse_tree(tree("tank", DATA_POOL), "tank")
    mps = pool_import.project_mountpoints(datasets, "tank")
    busy = lambda p: "the directory is not empty" if p == "/tank/media" else None
    [conflict] = pool_import.mount_conflicts(datasets, mps, busy)
    assert conflict == {"dataset": "media", "mountpoint": "/tank/media",
                        "reason": "the directory is not empty"}


@pytest.mark.parametrize("path", ["relative", "/usr/local/x", "/", "/mnt/a b", "/mnt/../etc"])
def test_a_bad_mountpoint_is_refused(path):
    assert pool_import.mountpoint_problem(path)


def test_shares_follow_the_pool_to_its_new_mountpoint():
    datasets = pool_import.parse_tree(tree("tank", DATA_POOL), "tank")
    old = pool_import.project_mountpoints(datasets, "tank")
    new = pool_import.project_mountpoints(datasets, "tank", "/mnt/data")
    config = {"shares": {
        "smb": [{"name": "media", "path": "/tank/media/films", "acl": "Everyone:R,carla:F",
                 "valid_users": ["carla", "bruno"]},
                {"name": "elsewhere", "path": "/home/x", "acl": "Everyone:R"}],
        "nfs": [{"name": "bk", "path": "/srv/backup", "allowed_hosts": "10.0.0.0/24"}],
    }}
    shares = pool_import.foreign_shares(config, old, new)
    [smb] = shares["smb"]
    assert smb["path"] == "/mnt/data/media/films" and smb["original_path"] == "/tank/media/films"
    assert smb["users"] == ["carla", "bruno"]
    assert [s["path"] for s in shares["nfs"]] == ["/srv/backup"]


def test_foreign_users_keep_uid_and_mark_admins():
    users = pool_import.foreign_users({"users": [
        {"username": "carla", "uid": 1005, "groups": ["wheel", "users"]},
        {"username": "bruno", "groups": []},
    ]})
    assert users[0]["uid"] == 1005 and users[0]["admin"]
    assert users[1]["uid"] is None and not users[1]["admin"]


def test_a_pool_of_another_system_needs_the_confirmation():
    engine = FakeEngine()
    with pytest.raises(ImportProblem, match="another system"):
        pool_import.inspect(engine, "11200611346329343249", occupied=free)
    assert "pool_import_inspect" not in engine.actions()


def test_inspecting_imports_read_only_and_always_exports_again():
    engine = FakeEngine()
    report = pool_import.inspect(engine, "42", occupied=free)
    assert engine.actions()[-1] == "pool_export"
    [(_, inspect_kw)] = [c for c in engine.calls if c[0] == "pool_import_inspect"]
    assert inspect_kw["pool_id"] == "42" and inspect_kw["force"] == ""
    assert inspect_kw["pool_name"].startswith("fnasimport")
    assert report["refused"] == [] and report["conflicts"] == []
    assert report["pool"]["final_name"] == "tank" and not report["freedomnas"]
    assert [d["mountpoint"] for d in report["datasets"]] == ["/tank", "/tank/media", "/srv/backup"]


def test_an_export_follows_even_a_failed_survey():
    engine = FakeEngine(failing={"dataset_tree"})
    with pytest.raises(CommandExecutionError):
        pool_import.inspect(engine, "42", occupied=free)
    assert engine.actions()[-1] == "pool_export"


ENCRYPTED = [
    ("", "/tank", "default", "on", {"encryption": "aes-256-gcm", "encryptionroot": "T",
                                    "keyformat": "passphrase", "keystatus": "unavailable"}),
]


def encrypted_rows(pool):
    return [(r[0], r[1], r[2], r[3], {**r[4], "encryptionroot": pool}) for r in ENCRYPTED]


def test_a_passphrase_unlocks_and_a_wrong_one_is_reported():
    class Enc(FakeEngine):
        def execute(self, category, action, timeout=30, check=True, **kw):
            if action == "dataset_tree":
                self.rows = encrypted_rows(kw["pool_name"])
            return super().execute(category, action, timeout, check, **kw)

    ok = Enc()
    report = pool_import.inspect(ok, "42", passphrases={"": "correct horse"}, occupied=free)
    assert report["encryption_roots"][0]["unlocked"]
    bad = Enc(wrong_key=True)
    report = pool_import.inspect(bad, "42", passphrases={"": "nope"}, occupied=free)
    assert report["encryption_roots"][0]["error"] == "wrong passphrase"
    missing = Enc()
    report = pool_import.inspect(missing, "42", occupied=free)
    assert report["encryption_roots"][0]["error"] == "passphrase required"


def test_importing_moves_the_mountpoint_and_mounts():
    engine = FakeEngine()
    result = pool_import.import_pool(engine, "42", "tank", root_mountpoint="/mnt/data")
    assert result["pool"] == "tank"
    [(_, setkw)] = [c for c in engine.calls if c[0] == "dataset_set"]
    assert setkw == {"property": "mountpoint", "value": "/mnt/data", "dataset": "tank"}
    assert engine.actions()[-1] == "mount_pool_auto"


def test_an_unchanged_mountpoint_is_not_set():
    engine = FakeEngine()
    pool_import.import_pool(engine, "42", "tank", root_mountpoint="/tank")
    assert "dataset_set" not in engine.actions()


def test_a_failed_mount_exports_the_pool_again():
    engine = FakeEngine(failing={"mount_pool_auto"})
    with pytest.raises(ImportProblem):
        pool_import.import_pool(engine, "42", "tank")
    assert engine.actions()[-1] == "pool_export"


def test_a_system_root_found_at_import_is_exported_again():
    engine = FakeEngine(rows=[("", "/", "local", "on")])
    with pytest.raises(ImportProblem, match="root"):
        pool_import.import_pool(engine, "42", "rpool")
    assert engine.actions()[-1] == "pool_export"


def test_a_failed_import_gives_the_pool_its_mountpoint_back():
    engine = FakeEngine(failing={"mount_pool_auto"})
    with pytest.raises(ImportProblem):
        pool_import.import_pool(engine, "42", "tank", root_mountpoint="/mnt/data")
    assert engine.actions()[-3:] == ["mount_pool_auto", "dataset_inherit", "pool_export"]


def test_an_explicit_mountpoint_is_set_back_as_it_was():
    rows = [("", "/data/tank", "local", "on")]
    engine = FakeEngine(rows=rows, failing={"mount_pool_auto"})
    with pytest.raises(ImportProblem):
        pool_import.import_pool(engine, "42", "tank", root_mountpoint="/mnt/data")
    sets = [kw["value"] for a, kw in engine.calls if a == "dataset_set"]
    assert sets == ["/mnt/data", "/data/tank"]
