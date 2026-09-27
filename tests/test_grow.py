"""The root pool grows into a larger disk once, and never takes the boot with it."""

from grow import CommandExecutionError, MIN_GROWTH_BYTES, grow_root_pool

GIB = 1024 ** 3
PART_START = 513 * 1024 * 1024
PART_SIZE = 9 * GIB


class FakeEngine:
    def __init__(self, disk_size, leaves="\t/dev/vda2\t9G\t1G\t8G\t-\t-\t0%\t10%\t-\tONLINE\t-",
                 root="rpool/ROOT/debian", failing=()):
        self.disk_size = disk_size
        self.leaves = leaves
        self.root = root
        self.failing = set(failing)
        self.calls = []

    def execute(self, category, action, timeout=30, **kwargs):
        self.calls.append(action)
        if action in self.failing:
            raise CommandExecutionError(f"{action} failed")
        return {
            "dataset_at_path": self.root,
            "pool_leaf_devices": "rpool\t9G\t1G\t8G\t-\t-\t0%\t10%\t1.00x\tONLINE\t-\n" + self.leaves,
            "partition_of": f"/dev/vda 2 {PART_START} {PART_SIZE}",
            "disk_size": str(self.disk_size),
        }.get(action, ""), ""


def test_a_larger_disk_is_taken():
    engine = FakeEngine(disk_size=PART_START + PART_SIZE + 100 * GIB)
    result = grow_root_pool(engine)

    assert result["grown"] and result["pool"] == "rpool" and result["device"] == "/dev/vda2"
    assert engine.calls[-2:] == ["partition_grow", "pool_expand"]


def test_a_partition_at_the_end_is_left_alone():
    engine = FakeEngine(disk_size=PART_START + PART_SIZE + MIN_GROWTH_BYTES - 1)
    result = grow_root_pool(engine)

    assert not result["grown"]
    assert "partition_grow" not in engine.calls


def test_a_mirror_is_left_alone():
    mirror = "\tmirror-0\t9G\t-\n\t/dev/sda2\t9G\t-\n\t/dev/sdb2\t9G\t-"
    engine = FakeEngine(disk_size=100 * GIB, leaves=mirror)
    result = grow_root_pool(engine)

    assert not result["grown"] and "more than one device" in result["reason"]
    assert "partition_of" not in engine.calls


def test_a_root_off_zfs_is_left_alone():
    engine = FakeEngine(disk_size=100 * GIB, failing={"dataset_at_path"})
    result = grow_root_pool(engine)

    assert not result["grown"]
    assert engine.calls == ["dataset_at_path"]


def test_a_failing_resize_is_reported_not_raised():
    engine = FakeEngine(disk_size=100 * GIB, failing={"partition_grow"})
    result = grow_root_pool(engine)

    assert not result["grown"] and "partition_grow failed" in result["reason"]
    assert "pool_expand" not in engine.calls
