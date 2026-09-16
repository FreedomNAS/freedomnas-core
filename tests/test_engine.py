"""Tests for FreedomNASEngine — the OS-agnostic command translation layer."""

import builtins
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from engine import (
    BACKEND,
    CommandExecutionError,
    CommandTranslationError,
    FreedomNASEngine,
    FreedomNASEngineException,
    OSNotSupportedException,
    get_engine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROFILES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles")


def os_release_open(os_release_content: str):
    """
    Return an open() side-effect that fakes /etc/os-release but passes every
    other path through to the real filesystem (so JSON profile loading works).
    """
    _real_open = builtins.open

    def _open(path, *args, **kwargs):
        if str(path) == "/etc/os-release":
            return StringIO(os_release_content)
        return _real_open(path, *args, **kwargs)

    return _open


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the engine singleton between tests."""
    FreedomNASEngine._instance = None
    FreedomNASEngine._initialized = False
    yield
    FreedomNASEngine._instance = None
    FreedomNASEngine._initialized = False


# These tests reach inside the pure-Python engine: they patch subprocess.run
# or builtins.open and assert on how it calls them. The native engine runs
# commands in Nim, where those patches do not apply — and would execute the
# real command instead of a mock. The same behaviour is verified against the
# Python engine, backend to backend, in test_engine_parity.py.
mocks_python_internals = pytest.mark.skipif(
    BACKEND == "native",
    reason="patches Python internals the native engine does not use; "
           "covered by test_engine_parity.py",
)


def make_engine(os_override: str = "debian") -> FreedomNASEngine:
    return FreedomNASEngine(profile_dir=PROFILES_DIR, os_override=os_override)


# A non-target engine merges the *running host's* init profile, detected at
# runtime — so a `services` command legitimately differs between a systemd
# build host and an OpenRC one for the same call. Record the expectation per
# init system and pick it at assert time, rather than assuming the host runs
# systemd. Values are what profiles/init/init-<name>.json defines.
SERVICE_START_BY_INIT = {
    "systemd": "systemctl start samba",
    "openrc":  "rc-service samba start",
    "runit":   "sv start samba",
    "s6":      "s6-svc -u /run/service/samba",
    "dinit":   "dinitctl start samba",
    "bsd_rc":  "service samba start",
}


def expected_service_start(engine: FreedomNASEngine) -> str:
    """The `services.start` command for the init system this host detected."""
    expected = SERVICE_START_BY_INIT.get(engine.init_system)
    if expected is None:
        pytest.fail(
            f"No expected services.start recorded for init system "
            f"'{engine.init_system}' — add one to SERVICE_START_BY_INIT to "
            f"match profiles/init/init-{engine.init_system}.json"
        )
    return expected


# ---------------------------------------------------------------------------
# OS Detection
# ---------------------------------------------------------------------------

class TestOSDetection:
    def test_detects_debian(self):
        with patch("builtins.open", side_effect=os_release_open("ID=debian\nVERSION_ID=12")), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "debian"

    def test_detects_ubuntu_as_debian(self):
        with patch("builtins.open", side_effect=os_release_open("ID=ubuntu\nVERSION_ID=22.04")), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "debian"

    def test_detects_arch(self):
        with patch("builtins.open", side_effect=os_release_open("ID=arch\nNAME=\"Arch Linux\"")), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "arch"

    def test_detects_gentoo(self):
        with patch("builtins.open", side_effect=os_release_open('ID=gentoo\nNAME="Gentoo"')), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "gentoo"

    def test_detects_void(self):
        with patch("builtins.open", side_effect=os_release_open('ID=void\nNAME="void"')), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "void"

    def test_detects_artix_not_arch(self):
        """
        Artix declares ID_LIKE=arch but has its own profile (OpenRC, not
        systemd). The exact ID has to win, or it silently gets Arch's.
        """
        with patch("builtins.open", side_effect=os_release_open(
                'ID=artix\nID_LIKE=arch\nNAME="Artix Linux"')), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "artix"

    def test_unknown_derivative_falls_back_to_id_like(self):
        with patch("builtins.open", side_effect=os_release_open(
                'ID=linuxmint\nID_LIKE="ubuntu debian"')), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "debian"

    def test_quoted_id_is_unquoted(self):
        with patch("builtins.open", side_effect=os_release_open("ID='gentoo'")), \
             patch("platform.system", return_value="Linux"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "gentoo"

    def test_detects_freebsd(self):
        with patch("platform.system", return_value="FreeBSD"):
            engine = FreedomNASEngine(profile_dir=PROFILES_DIR)
        assert engine.os_type == "freebsd"

    def test_unsupported_os_raises(self):
        with patch("platform.system", return_value="Windows"):
            with pytest.raises(OSNotSupportedException):
                FreedomNASEngine(profile_dir=PROFILES_DIR)

    def test_unsupported_linux_distro_raises(self):
        with patch("builtins.open", side_effect=os_release_open("ID=fedora")), \
             patch("platform.system", return_value="Linux"):
            with pytest.raises(OSNotSupportedException):
                FreedomNASEngine(profile_dir=PROFILES_DIR)

    def test_missing_os_release_raises(self):
        _real_open = builtins.open

        def _raise_for_os_release(path, *args, **kwargs):
            if str(path) == "/etc/os-release":
                raise FileNotFoundError
            return _real_open(path, *args, **kwargs)

        with patch("platform.system", return_value="Linux"), \
             patch("builtins.open", side_effect=_raise_for_os_release):
            with pytest.raises(OSNotSupportedException):
                FreedomNASEngine(profile_dir=PROFILES_DIR)


# ---------------------------------------------------------------------------
# Profile Loading
# ---------------------------------------------------------------------------

class TestProfileLoading:
    def test_loads_debian_profile(self):
        engine = make_engine("debian")
        assert engine.profile["os_name"] == "Debian"
        assert engine.profile["os_family"] == "linux"

    def test_loads_freebsd_profile(self):
        engine = make_engine("freebsd")
        assert engine.profile["os_name"] == "FreeBSD"
        assert engine.profile["os_family"] == "bsd"

    def test_loads_arch_profile(self):
        engine = make_engine("arch")
        assert engine.profile["os_name"] == "Arch Linux"

    def test_missing_profile_raises(self):
        with pytest.raises(OSNotSupportedException):
            FreedomNASEngine(profile_dir="/nonexistent/path", os_override="debian")

    @mocks_python_internals
    def test_invalid_json_raises(self):
        with patch("builtins.open", mock_open(read_data="not json {")), \
             patch("pathlib.Path.exists", return_value=True):
            with pytest.raises(FreedomNASEngineException):
                FreedomNASEngine(profile_dir=PROFILES_DIR, os_override="debian")


# ---------------------------------------------------------------------------
# get_command
# ---------------------------------------------------------------------------

class TestGetCommand:
    def test_returns_string_for_single_command(self):
        engine = make_engine("debian")
        cmd = engine.get_command("services", "start", service_name="samba")
        assert cmd == expected_service_start(engine)

    def test_returns_list_for_multi_step_command(self):
        engine = make_engine("debian")
        cmds = engine.get_command(
            "network", "rename_interface", old_name="eth0", new_name="fnas_lan"
        )
        assert isinstance(cmds, list)
        assert len(cmds) == 3
        assert cmds[0] == "ip link set eth0 down"
        assert cmds[1] == "ip link set eth0 name fnas_lan"
        assert cmds[2] == "ip link set fnas_lan up"

    def test_freebsd_multi_step_set_static_ip(self):
        engine = make_engine("freebsd")
        cmds = engine.get_command(
            "network", "set_static_ip",
            logical_name="fnas_lan", ip="192.168.1.50",
            netmask="255.255.255.0", gateway="192.168.1.1",
        )
        assert isinstance(cmds, list)
        assert len(cmds) == 2

    def test_unknown_category_raises(self):
        engine = make_engine("debian")
        with pytest.raises(CommandTranslationError) as exc_info:
            engine.get_command("nonexistent", "action")
        assert "nonexistent" in str(exc_info.value)

    def test_unknown_action_raises(self):
        engine = make_engine("debian")
        with pytest.raises(CommandTranslationError) as exc_info:
            engine.get_command("services", "fly")
        assert "fly" in str(exc_info.value)
        assert "services" in str(exc_info.value)

    def test_kwargs_are_sanitized(self):
        engine = make_engine("debian")
        cmd = engine.get_command(
            "services", "start",
            service_name="samba; rm -rf /"
        )
        # _sanitize_kwargs strips shell metacharacters — `;` is removed.
        # The remaining text is harmless because without `;` there is no
        # second shell statement when the command runs via shlex (shell=False).
        assert ";" not in cmd

    def test_injection_chars_stripped(self):
        engine = make_engine("debian")
        cmd = engine.get_command(
            "users", "delete",
            username="bob|evil"
        )
        assert "|" not in cmd


# ---------------------------------------------------------------------------
# execute — routing logic
# ---------------------------------------------------------------------------

@mocks_python_internals
class TestExecuteRouting:
    def _make_result(self, stdout="ok", stderr="", returncode=0):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    def test_simple_command_uses_list_form(self):
        engine = make_engine("debian")
        mock_result = self._make_result()
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            engine.execute("services", "start", service_name="samba")
            args, kwargs = mock_run.call_args
            # shell=False: first arg is a list
            assert isinstance(args[0], list)
            assert kwargs.get("shell") is None or kwargs.get("shell") is False

    def test_pipe_command_uses_shell_true(self):
        engine = make_engine("debian")
        mock_result = self._make_result(stdout="vim/2:9.0.1-1 [upgradable]")
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            engine.execute("packages", "list_updatable")
            args, kwargs = mock_run.call_args
            # shell=True: first arg is a string
            assert isinstance(args[0], str)
            assert kwargs.get("shell") is True

    def test_multi_step_command_runs_all_steps(self):
        engine = make_engine("debian")
        mock_result = self._make_result()
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            engine.execute(
                "network", "rename_interface",
                old_name="eth0", new_name="fnas_lan",
            )
            assert mock_run.call_count == 3

    def test_returns_stdout_stderr_tuple(self):
        engine = make_engine("debian")
        mock_result = self._make_result(stdout="active", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            stdout, stderr = engine.execute("services", "status", service_name="samba")
        assert stdout == "active"
        assert stderr == ""

    def test_nonzero_exit_raises_by_default(self):
        engine = make_engine("debian")
        mock_result = self._make_result(stdout="", stderr="unit not found", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(CommandExecutionError):
                engine.execute("services", "status", service_name="nope")

    def test_nonzero_exit_no_raise_when_check_false(self):
        engine = make_engine("debian")
        mock_result = self._make_result(stdout="", stderr="not found", returncode=1)
        with patch("subprocess.run", return_value=mock_result):
            stdout, stderr = engine.execute(
                "services", "status", service_name="nope", check=False
            )
        assert stderr == "not found"

    def test_timeout_raises_command_execution_error(self):
        engine = make_engine("debian")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            with pytest.raises(CommandExecutionError, match="timed out"):
                engine.execute("services", "start", service_name="samba")


# ---------------------------------------------------------------------------
# Shell metachar detection
# ---------------------------------------------------------------------------

class TestShellMetacharDetection:
    def test_pipe_detected(self):
        assert FreedomNASEngine._contains_shell_metachar("cmd | grep foo")

    def test_redirect_detected(self):
        assert FreedomNASEngine._contains_shell_metachar("cmd > /dev/null")

    def test_stderr_redirect_detected(self):
        assert FreedomNASEngine._contains_shell_metachar("cmd 2>/dev/null")

    def test_ampersand_detected(self):
        assert FreedomNASEngine._contains_shell_metachar("cmd && other")

    def test_subshell_detected(self):
        assert FreedomNASEngine._contains_shell_metachar("cmd $(other)")

    def test_plain_command_not_detected(self):
        assert not FreedomNASEngine._contains_shell_metachar("systemctl start samba")

    def test_path_with_slash_not_detected(self):
        assert not FreedomNASEngine._contains_shell_metachar("zpool import /dev/sda")


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------

class TestIntrospection:
    def test_list_categories_excludes_os_keys(self):
        engine = make_engine("debian")
        cats = engine.list_categories()
        assert "services" in cats
        assert "zfs" in cats
        assert "os_name" not in cats
        assert "os_family" not in cats

    def test_list_actions_for_services(self):
        engine = make_engine("debian")
        actions = engine.list_actions("services")
        assert "start" in actions
        assert "stop" in actions
        assert "restart" in actions

    def test_list_actions_unknown_category_returns_empty(self):
        engine = make_engine("debian")
        assert engine.list_actions("nonexistent") == []

    def test_get_os_info(self):
        engine = make_engine("debian")
        info = engine.get_os_info()
        assert info["os_name"] == "Debian"
        assert info["os_family"] == "linux"
        assert info["detected_type"] == "debian"


# ---------------------------------------------------------------------------
# Singleton behaviour
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_two_calls_return_same_instance(self):
        e1 = make_engine("debian")
        e2 = FreedomNASEngine(profile_dir=PROFILES_DIR, os_override="debian")
        assert e1 is e2

    def test_repr_contains_os_name(self):
        engine = make_engine("debian")
        assert "Debian" in repr(engine)

    def test_get_engine_convenience(self):
        """
        get_engine() takes no os_override: it describes the machine the tests
        are running on. That only resolves on a host the engine supports, so
        on any other dev box (Gentoo, Fedora, ...) there is nothing to assert
        rather than something to fail.
        """
        try:
            engine = get_engine(profile_dir=PROFILES_DIR)
        except OSNotSupportedException as exc:
            pytest.skip(f"host OS unsupported by the engine: {exc}")
        assert isinstance(engine, FreedomNASEngine)
        assert engine.init_system in SERVICE_START_BY_INIT


# ---------------------------------------------------------------------------
# Target engines (installer)
# ---------------------------------------------------------------------------

class TestForTarget:
    """FreedomNASEngine.for_target() — a non-singleton engine for another OS."""

    def test_returns_a_fresh_instance(self):
        singleton = make_engine("debian")
        target = FreedomNASEngine.for_target("debian", profile_dir=PROFILES_DIR)
        assert target is not singleton

    def test_loads_the_target_profile_not_the_hosts(self):
        make_engine("debian")
        target = FreedomNASEngine.for_target("freebsd", profile_dir=PROFILES_DIR)
        assert target.os_type == "freebsd"
        assert target.get_os_info()["os_name"] == "FreeBSD"

    def test_init_system_comes_from_the_target_profile(self):
        """A target engine must never probe the running host for its init."""
        target = FreedomNASEngine.for_target("freebsd", profile_dir=PROFILES_DIR)
        assert target.init_system == "bsd_rc"
        assert "sysrc" in target.get_command("services", "enable", service_name="sshd")

    def test_init_system_can_be_overridden(self):
        target = FreedomNASEngine.for_target(
            "debian", profile_dir=PROFILES_DIR, init_system="openrc"
        )
        assert target.init_system == "openrc"

    def test_no_root_means_no_chroot_prefix(self):
        target = FreedomNASEngine.for_target("debian", profile_dir=PROFILES_DIR)
        assert target.chroot_prefix is None
        argv, use_shell = target._resolve_invocation("apt-get update")
        assert argv == ["apt-get", "update"]
        assert use_shell is False

    def test_root_slash_is_treated_as_no_chroot(self):
        target = FreedomNASEngine.for_target("debian", root="/", profile_dir=PROFILES_DIR)
        assert target.root is None
        assert target.chroot_prefix is None

    def test_root_prefixes_every_command(self):
        target = FreedomNASEngine.for_target("debian", root="/mnt", profile_dir=PROFILES_DIR)
        assert target.chroot_prefix == "chroot /mnt"
        argv, use_shell = target._resolve_invocation("apt-get update")
        assert argv == ["chroot", "/mnt", "apt-get", "update"]
        assert use_shell is False

    def test_shell_commands_are_wrapped_not_shelled_out(self):
        """
        A command needing a shell runs `sh -c` *inside* the chroot, as a single
        argument — never through the host's shell, where its quoting could
        escape.
        """
        target = FreedomNASEngine.for_target("debian", root="/mnt", profile_dir=PROFILES_DIR)
        argv, use_shell = target._resolve_invocation("echo 'a:b' | chpasswd")
        assert argv == ["chroot", "/mnt", "sh", "-c", "echo 'a:b' | chpasswd"]
        assert use_shell is False

    def test_trailing_slash_is_stripped(self):
        target = FreedomNASEngine.for_target("debian", root="/mnt/", profile_dir=PROFILES_DIR)
        assert target.chroot_prefix == "chroot /mnt"

    def test_chroot_prefix_can_be_overridden(self):
        target = FreedomNASEngine.for_target(
            "debian", root="/mnt", profile_dir=PROFILES_DIR,
            chroot_prefix="systemd-nspawn -D {root}",
        )
        assert target.chroot_prefix == "systemd-nspawn -D /mnt"

    def test_host_singleton_is_untouched_by_a_target_engine(self):
        host = make_engine("debian")
        FreedomNASEngine.for_target("freebsd", root="/mnt", profile_dir=PROFILES_DIR)
        assert host.chroot_prefix is None
        assert host.os_type == "debian"
        assert get_engine(profile_dir=PROFILES_DIR) is host

    def test_gentoo_target_uses_openrc_and_portage(self):
        target = FreedomNASEngine.for_target("gentoo", root="/mnt", profile_dir=PROFILES_DIR)
        assert target.init_system == "openrc"
        assert target.get_command("services", "enable", service_name="sshd") == (
            "rc-update add sshd default"
        )
        assert target.get_command("packages", "install", package_name="sys-fs/zfs") == (
            "emerge --quiet --noreplace sys-fs/zfs"
        )
        argv, use_shell = target._resolve_invocation(
            target.get_command("packages", "install", package_name="sys-fs/zfs")
        )
        assert argv[:2] == ["chroot", "/mnt"]
        assert use_shell is False

    def test_unknown_target_raises(self):
        with pytest.raises(OSNotSupportedException):
            FreedomNASEngine.for_target("plan9", profile_dir=PROFILES_DIR)

    def test_installer_fragment_is_merged(self):
        """profiles/installer/ is picked up by the ordinary fragment scan."""
        target = FreedomNASEngine.for_target("debian", profile_dir=PROFILES_DIR)
        assert "partitioning" in target.profile
        assert "rootpool" in target.profile
        assert target.get_command(
            "partitioning", "create_esp", disk="/dev/sda", esp_size="512MiB"
        ) == "sgdisk -n 1:0:+512MiB -t 1:EF00 /dev/sda"
