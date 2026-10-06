"""Delegated permissions, events, iostat and ARC parsing."""

import pytest

import delegation
import monitoring
from delegation import DelegationProblem

ALLOW = """---- Permissions on tank/data ------------------------------------------
Permission sets:
\t@basic hold,send
Create time permissions:
\tdestroy
Local permissions:
\tgroup wheel @basic
Local+Descendent permissions:
\tuser sarah create,mount,snapshot
\teveryone mount
Descendent permissions:
\tuser anna send
"""


def test_parse_allow():
    d = delegation.parse(ALLOW)
    assert [(x["who"], x["name"], x["scope"]) for x in d] == [
        ("set", "@basic", "both"), ("create", "", "both"), ("group", "wheel", "local"),
        ("user", "sarah", "both"), ("everyone", "", "both"), ("user", "anna", "descendent")]
    assert d[3]["perms"] == ["create", "mount", "snapshot"]


def test_arguments():
    assert delegation.arguments({"who": "user", "names": "sarah,anna", "perms": ["create", "mount"],
                                 "scope": "local"}) == {
        "allow_scope": "-l", "allow_who": "-u sarah,anna", "perms": "create,mount"}
    assert delegation.arguments({"who": "set", "set": "@basic", "perms": "send,hold"})["allow_who"] == "-s @basic"
    assert delegation.arguments({"who": "create", "perms": "destroy", "scope": "local"})["allow_scope"] == ""
    assert delegation.arguments({"who": "everyone", "perms": ""}, revoking=True)["perms"] == ""
    assert delegation.arguments({"who": "group", "names": ["wheel"], "perms": "load-key,send:raw"})["perms"] == "load-key,send:raw"


@pytest.mark.parametrize("request_", [
    {"who": "user", "names": "", "perms": "mount"},
    {"who": "user", "names": "-r", "perms": "mount"},
    {"who": "user", "names": "a b", "perms": "mount"},
    {"who": "user", "names": "sarah", "perms": "-r"},
    {"who": "user", "names": "sarah", "perms": "Mount"},
    {"who": "user", "names": "sarah", "perms": ""},
    {"who": "set", "set": "basic", "perms": "mount"},
    {"who": "root", "perms": "mount"},
    {"who": "user", "names": "sarah", "perms": "mount", "scope": "all"},
])
def test_arguments_refused(request_):
    with pytest.raises(DelegationProblem):
        delegation.arguments(request_)


EVENTS = """Oct  4 2026 17:47:44.332778965\tsysevent.fs.zfs.pool_create
        class = "sysevent.fs.zfs.pool_create"
        pool = "tank"

Oct  4 2026 17:48:00.000000000\tereport.fs.zfs.checksum
        pool = "tank"
        vdev_path = "/dev/sda1"
"""


def test_events_newest_first():
    ev = monitoring.parse_events(EVENTS)
    assert [e["short"] for e in ev] == ["checksum", "pool_create"]
    assert ev[0]["error"] and not ev[1]["error"]
    assert ev[0]["pool"] == "tank" and ev[0]["fields"]["vdev_path"] == "/dev/sda1"
    assert len(monitoring.parse_events(EVENTS, limit=1)) == 1


def test_iostat():
    rows = monitoring.parse_iostat(
        "tank\t100\t900\t5\t6\t5000\t6000\nmirror-0\t100\t900\t5\t6\t5000\t6000\n"
        "/dev/sda1\t-\t-\t2\t3\t2000\t3000\n", "tank")
    assert [r["name"] for r in rows] == ["tank", "mirror-0", "/dev/sda1"]
    assert rows[0]["read_bytes"] == 5000 and rows[2]["alloc"] is None


def test_arcstats_linux_and_freebsd():
    linux = "13 1 0x01 147 39984 1 2\nname                            type data\nhits                            4    900\nmisses                          4    100\nsize                            4    2048\nc                               4    4096\nc_max                           4    8192\n"
    freebsd = "kstat.zfs.misc.arcstats.hits: 900\nkstat.zfs.misc.arcstats.misses: 100\nkstat.zfs.misc.arcstats.size: 2048\n"
    for text in (linux, freebsd):
        s = monitoring.arc_summary(monitoring.parse_arcstats(text))
        assert s["size"] == 2048 and s["hit_ratio"] == 90.0 and s["l2_hit_ratio"] is None
    assert monitoring.arc_summary({}) is None
