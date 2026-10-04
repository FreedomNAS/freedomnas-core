"""Pool layout words and the status tree the WebUI shows."""

import json

import pytest

from pools import parse_properties, parse_status, vdev_spec


def test_simple_mirror():
    assert vdev_spec([{"type": "mirror", "devices": ["/dev/a", "/dev/b"]}]) == \
        ["mirror", "/dev/a", "/dev/b"]


def test_every_class():
    words = vdev_spec([
        {"class": "data", "type": "raidz2", "devices": ["/dev/a", "/dev/b", "/dev/c"]},
        {"class": "data", "type": "raidz2", "devices": ["/dev/d", "/dev/e", "/dev/f"]},
        {"class": "special", "type": "mirror", "devices": ["/dev/g", "/dev/h"]},
        {"class": "dedup", "type": "", "devices": ["/dev/i"]},
        {"class": "spare", "type": "mirror", "devices": ["/dev/j", "/dev/k"]},
    ])
    assert words == ["raidz2", "/dev/a", "/dev/b", "/dev/c",
                     "raidz2", "/dev/d", "/dev/e", "/dev/f",
                     "special", "mirror", "/dev/g", "/dev/h",
                     "dedup", "/dev/i", "spare", "/dev/j", "/dev/k"]


@pytest.mark.parametrize("groups", [
    [],
    [{"type": "raidz2", "devices": ["/dev/a", "/dev/b"]}],
    [{"type": "mirror", "devices": ["/dev/a"]}],
    [{"class": "special", "type": "raidz1", "devices": ["/dev/a", "/dev/b"]}],
    [{"class": "cache", "devices": ["/dev/a"]}],
    [{"type": "stripe", "devices": ["/dev/a"]}],
    [{"devices": ["/dev/a"]}, {"devices": ["/dev/a"]}],
    [{"devices": []}],
])
def test_refused(groups):
    with pytest.raises(ValueError):
        vdev_spec(groups)


STATUS = {"pools": {"tank": {
    "name": "tank", "state": "DEGRADED", "error_count": 0,
    "status": "One or more devices has been taken offline by the administrator.\n\tSufficient replicas exist.\n",
    "action": "Online the device using 'zpool online'.\n",
    "scan_stats": {"function": "RESILVER", "state": "SCANNING", "to_examine": 200,
                   "issued": 50, "errors": 0, "start_time": 1, "scrub_pause": 0},
    "vdevs": {"tank": {"name": "tank", "vdev_type": "root", "vdevs": {
        "mirror-0": {"name": "mirror-0", "vdev_type": "mirror", "state": "DEGRADED",
                     "total_space": 1000, "vdevs": {
                         "/dev/a": {"name": "/dev/a", "path": "/dev/a", "vdev_type": "disk",
                                    "state": "ONLINE", "checksum_errors": 2},
                         "/dev/b": {"name": "/dev/b", "path": "/dev/b", "vdev_type": "disk",
                                    "state": "OFFLINE"}}}}}},
    "logs": {"/dev/c": {"name": "/dev/c", "path": "/dev/c", "vdev_type": "disk", "state": "ONLINE"}},
    "spares": {"/dev/d": {"name": "/dev/d", "path": "/dev/d", "vdev_type": "disk", "state": "AVAIL"}},
}}}


def test_status_tree():
    st = parse_status(json.dumps(STATUS), "tank")
    assert st["state"] == "DEGRADED"
    assert "\n" not in st["status"] and st["status"].startswith("One or more devices")
    assert st["scan"]["percent"] == 25.0 and st["scan"]["function"] == "RESILVER"
    assert [g["role"] for g in st["groups"]] == ["data", "log", "spare"]
    mirror = st["groups"][0]["vdevs"][0]
    assert mirror["type"] == "mirror" and mirror["depth"] == 0 and mirror["size"] == 1000
    assert [c["state"] for c in mirror["children"]] == ["ONLINE", "OFFLINE"]
    assert mirror["children"][0]["checksum_errors"] == 2
    assert mirror["children"][0]["parent_type"] == "mirror"


def test_status_unknown_pool_or_garbage():
    assert parse_status(json.dumps(STATUS), "other") is None
    assert parse_status("not json", "tank") is None


def test_healthy_pool_without_scan():
    healthy = {"pools": {"p": {"name": "p", "state": "ONLINE",
                               "vdevs": {"p": {"name": "p", "vdevs": {}}}}}}
    st = parse_status(json.dumps(healthy), "p")
    assert st["scan"] is None and st["status"] == "" and st["groups"] == [{"role": "data", "vdevs": []}]


def test_properties():
    props = parse_properties("autotrim\toff\tdefault\ncomment\thello world\tlocal\n")
    assert props == {"autotrim": {"value": "off", "source": "default"},
                     "comment": {"value": "hello world", "source": "local"}}
