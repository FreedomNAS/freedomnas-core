"""What each core module's health() reports, from what its commands print."""

import importlib.util
import json
from pathlib import Path

import pytest

from engine import CommandExecutionError, CommandTranslationError

MODULES = Path(__file__).resolve().parent.parent / "modules"


def load(name, cls):
    spec = importlib.util.spec_from_file_location(f"health_{name}", MODULES / name / "module.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, cls)


class FakeEngine:
    def __init__(self, outputs=None, values=None):
        self.outputs = outputs or {}
        self.values = values or {}

    def execute(self, category, action, **kwargs):
        out = self.outputs.get((category, action), "")
        if callable(out):
            out = out(**kwargs)
        if isinstance(out, Exception):
            raise out
        return out, ""

    def get_profile_value(self, category, key):
        try:
            return self.values[(category, key)]
        except KeyError:
            raise CommandTranslationError(f"{category}.{key}")


class FakeConfig:
    def __init__(self, users=(), features=(), topgrade=False, smb=(), nfs=()):
        self.users = [{"username": u} for u in users]
        self.features = list(features)
        self.topgrade = topgrade
        self.smb = [{"name": n} for n in smb]
        self.nfs = [{"path": p} for p in nfs]

    def get_smb_shares(self):
        return self.smb

    def get_nfs_shares(self):
        return self.nfs

    def get_users(self):
        return self.users

    def get_interfaces(self):
        return []

    def get_installed_features(self):
        return self.features

    def is_topgrade_configured(self):
        if isinstance(self.topgrade, Exception):
            raise self.topgrade
        return self.topgrade


def module(name, cls, engine, config=None):
    mod = load(name, cls)(engine, config or FakeConfig())
    mod.start()
    return mod


IP_ADDR = json.dumps([
    {"ifname": "lo", "operstate": "UNKNOWN", "flags": ["LOOPBACK", "UP", "LOWER_UP"]},
    {"ifname": "enp3s0", "operstate": "UP", "flags": ["BROADCAST", "UP", "LOWER_UP"]},
    {"ifname": "enp4s0", "operstate": "DOWN", "flags": ["BROADCAST"]},
])


def test_network_reads_the_json_linux_profiles_print():
    engine = FakeEngine({("network", "list_interfaces"): IP_ADDR})
    assert module("network", "NetworkModule", engine).health() == (True, "enp3s0 up")


def test_network_with_only_loopback_up_is_unhealthy():
    only_lo = json.dumps([{"ifname": "lo", "operstate": "UNKNOWN"},
                          {"ifname": "eth0", "operstate": "DOWN"}])
    engine = FakeEngine({("network", "list_interfaces"): only_lo})
    assert module("network", "NetworkModule", engine).health() == (False, "no interface is up")


def test_network_reads_freebsds_bare_names():
    engine = FakeEngine({("network", "list_interfaces"): "em0 lo0\n"})
    assert module("network", "NetworkModule", engine).health() == (True, "em0 up")


def test_network_health_check_does_not_raise_on_a_real_answer():
    engine = FakeEngine({("network", "list_interfaces"): IP_ADDR})
    assert module("network", "NetworkModule", engine).health_check() is True


def test_shares_names_the_service_that_stopped():
    engine = FakeEngine(
        {("services", "status"): lambda service_name: (
            CommandExecutionError("inactive") if service_name == "nfs-server" else "")},
        {("samba", "smb_service"): "smbd", ("nfs", "nfs_service"): "nfs-server"},
    )
    config = FakeConfig(features=[{"id": "samba-cifs", "enabled": True}, {"id": "nfs", "enabled": True}])
    assert module("shares", "SharesModule", engine, config).health() == (False, "no shares, nfs-server not running")


def test_shares_without_share_features_is_healthy():
    assert module("shares", "SharesModule", FakeEngine()).health() == (True, "no shares, no share services enabled")


def test_shares_restored_without_features_are_seen_and_their_services_checked():
    engine = FakeEngine({}, {("samba", "smb_service"): "smbd", ("nfs", "nfs_service"): "nfs-server"})
    config = FakeConfig(smb=["documenti", "foto"], nfs=["/tank/documenti"])
    assert module("shares", "SharesModule", engine, config).health() == (
        True, "2 SMB, 1 NFS shares, smbd, nfs-server running")


def test_users_names_the_account_that_is_missing():
    engine = FakeEngine({("users", "get_user_info"): lambda username, **_: (
        "sarah:x:1000" if username == "sarah" else "")})
    config = FakeConfig(users=["sarah", "mario"])
    assert module("users", "UsersModule", engine, config).health() == (
        False, "mario missing from the system")


@pytest.mark.parametrize("topgrade, report", [
    (True, (True, "topgrade configured")),
    (False, (True, "topgrade not configured yet")),
])
def test_updates_is_healthy_whether_or_not_configured(topgrade, report):
    assert module("updates", "UpdatesModule", FakeEngine(), FakeConfig(topgrade=topgrade)).health() == report
