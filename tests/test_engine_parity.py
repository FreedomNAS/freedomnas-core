"""
The native engine must answer exactly like the pure-Python one.

Both implementations are imported side by side and asked the same questions:
every command in every shipped profile, the nastiest template values we can
think of, and real command execution against a throwaway profile. What is
compared is not just the result but the failure too — exception class and
message text — because callers across the WebUI and the installer branch on
both.

`for_target()` is used almost everywhere on purpose: it is the one
constructor that is not a singleton, so two engines can exist at once.
"""

import json
import string
from pathlib import Path

import pytest

pytest.importorskip("freedomnas_native", reason="native engine not built")

import engine_native
import engine_py

PROFILES_DIR = str(Path(__file__).parent.parent.parent / "freedomnas-profiles")

EDITIONS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]

NASTY_VALUES = [
    "plain",
    "with space",
    "semi;colon",
    "pipe|char",
    "amp&and",
    "dollar$sign",
    "back`tick`",
    "back\\slash",
    "new\nline",
    "carriage\rreturn",
    "quote'single",
    'quote"double',
    "sub$(shell)",
    "rm -rf / --no-preserve-root",
    "",
]

COMMAND_STRINGS = [
    "systemctl start samba",
    "zpool import /dev/sda",
    "apt-get update | tee /tmp/log",
    "echo 'a:b' | chpasswd",
    "sh -c \"echo hi\"",
    "cmd > /dev/null",
    "cmd 2>/dev/null",
    "cmd && other",
    "cmd $(other)",
    "a  b   c",
    "quoted 'single string' and \"double string\"",
    "escaped\\ space",
]

OS_RELEASE_SAMPLES = [
    "ID=debian\nVERSION_ID=12",
    "ID=ubuntu\nVERSION_ID=22.04",
    "ID=arch",
    'ID=artix\nID_LIKE=arch\nNAME="Artix Linux"',
    "ID=void",
    "ID='gentoo'",
    'ID=linuxmint\nID_LIKE="ubuntu debian"',
    "ID=fedora",
    "ID=plan9",
    "",
    "# only a comment\n",
    "NAME=nothing\n",
    'ID="debian"\n\n  \nPRETTY_NAME="Debian GNU/Linux"',
]


def pair(os_type, **kwargs):
    return (
        engine_py.FreedomNASEngine.for_target(os_type, profile_dir=PROFILES_DIR, **kwargs),
        engine_native.FreedomNASEngine.for_target(os_type, profile_dir=PROFILES_DIR, **kwargs),
    )


def outcome(fn, *args, **kwargs):
    """Result or (exception class, message) — so failures are compared too."""
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:
        return ("raised", type(exc).__name__, str(exc))


def placeholders(template):
    texts = template if isinstance(template, list) else [template]
    names = []
    for text in texts:
        for _, field, _, _ in string.Formatter().parse(text):
            if field and field not in names:
                names.append(field)
    return names


def command_entries(profile):
    for category, section in profile.items():
        if not isinstance(section, dict):
            continue
        for action, template in section.items():
            if isinstance(template, str):
                yield category, action, template
            elif isinstance(template, list) and all(isinstance(i, str) for i in template):
                yield category, action, template


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", NASTY_VALUES)
def test_sanitize_matches(value):
    py = engine_py.FreedomNASEngine._sanitize_kwargs(None, {"v": value})
    native = engine_native.FreedomNASEngine._sanitize_kwargs(None, {"v": value})
    assert py == native


@pytest.mark.parametrize("value", [12, 1.5, True, None, ["a", "b"]])
def test_sanitize_leaves_non_strings_alone(value):
    py = engine_py.FreedomNASEngine._sanitize_kwargs(None, {"v": value})
    native = engine_native.FreedomNASEngine._sanitize_kwargs(None, {"v": value})
    assert py == native


@pytest.mark.parametrize("command", COMMAND_STRINGS)
def test_metachar_detection_matches(command):
    assert (engine_py.FreedomNASEngine._contains_shell_metachar(command)
            == engine_native.FreedomNASEngine._contains_shell_metachar(command))


@pytest.mark.parametrize("content", OS_RELEASE_SAMPLES)
def test_os_release_parsing_matches(content):
    assert (engine_py.FreedomNASEngine._parse_os_release(content)
            == engine_native.FreedomNASEngine._parse_os_release(content))


@pytest.mark.parametrize("content", OS_RELEASE_SAMPLES)
def test_distro_resolution_matches(content, monkeypatch):
    def resolve(module):
        engine = object.__new__(module.FreedomNASEngine)
        monkeypatch.setattr(module.platform, "system", lambda: "Linux")
        with monkeypatch.context() as m:
            m.setattr("builtins.open", lambda *a, **k: _FakeFile(content))
            return outcome(engine._detect_os)

    assert resolve(engine_py) == resolve(engine_native)


class _FakeFile:
    def __init__(self, content):
        self.content = content

    def read(self):
        return self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("os_type", EDITIONS)
def test_profile_is_identical(os_type):
    py, native = pair(os_type)
    assert json.dumps(py.profile, sort_keys=True) == json.dumps(native.profile, sort_keys=True)
    assert py.init_system == native.init_system
    assert py.list_categories() == native.list_categories()
    assert py.get_os_info() == native.get_os_info()


@pytest.mark.parametrize("os_type", EDITIONS)
def test_chroot_prefix_matches(os_type):
    for root in [None, "/", "/mnt", "/mnt/", "/target/root/"]:
        py, native = pair(os_type, root=root)
        assert py.root == native.root
        assert py.chroot_prefix == native.chroot_prefix


def test_unknown_edition_fails_the_same_way():
    assert (outcome(engine_py.FreedomNASEngine.for_target, "plan9", profile_dir=PROFILES_DIR)
            == outcome(engine_native.FreedomNASEngine.for_target, "plan9", profile_dir=PROFILES_DIR))


def test_missing_profile_dir_fails_the_same_way():
    assert (outcome(engine_py.FreedomNASEngine.for_target, "debian", profile_dir="/nonexistent")
            == outcome(engine_native.FreedomNASEngine.for_target, "debian", profile_dir="/nonexistent"))


# ---------------------------------------------------------------------------
# Every command in every shipped profile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("os_type", EDITIONS)
def test_every_profile_command_translates_identically(os_type):
    py, native = pair(os_type)
    checked = 0
    for category, action, template in command_entries(py.profile):
        for index, filler in enumerate(NASTY_VALUES):
            kwargs = {name: f"{filler}{index}{name}" for name in placeholders(template)}
            expected = outcome(py.get_command, category, action, **kwargs)
            got = outcome(native.get_command, category, action, **kwargs)
            assert got == expected, f"{os_type} {category}.{action} with {kwargs}"
            checked += 1
    assert checked > 100


@pytest.mark.parametrize("os_type", EDITIONS)
def test_unknown_commands_fail_identically(os_type):
    py, native = pair(os_type)
    for category, action in [("services", "fly"), ("nonexistent", "action"),
                             ("zfs", ""), ("", "")]:
        assert (outcome(py.get_command, category, action)
                == outcome(native.get_command, category, action))


@pytest.mark.parametrize("os_type", EDITIONS)
def test_missing_template_variable_fails_identically(os_type):
    py, native = pair(os_type)
    for category, action, template in command_entries(py.profile):
        if not placeholders(template):
            continue
        assert (outcome(py.get_command, category, action)
                == outcome(native.get_command, category, action))


@pytest.mark.parametrize("os_type", EDITIONS)
def test_profile_values_and_sections_match(os_type):
    py, native = pair(os_type)
    for category, section in py.profile.items():
        assert py.get_profile_section(category) == native.get_profile_section(category)
        if not isinstance(section, dict):
            continue
        assert py.list_actions(category) == native.list_actions(category)
        for key in section:
            assert (outcome(py.get_profile_value, category, key)
                    == outcome(native.get_profile_value, category, key))
    assert (outcome(py.get_profile_value, "samba", "nope")
            == outcome(native.get_profile_value, "samba", "nope"))


def test_known_divergence_on_a_non_dict_category():
    """
    The one place the two engines differ, recorded rather than hidden.

    `os_name` is a string, not a section. The Python engine indexes it and
    dies with AttributeError; the native engine reports no actions. Nothing
    in FreedomNAS asks a scalar for its actions, and a crash is not a
    behaviour worth reproducing — but it is a difference, so it is asserted
    here and will fail loudly if either side changes.
    """
    py, native = pair("debian")
    assert native.list_actions("os_name") == []
    with pytest.raises(AttributeError):
        py.list_actions("os_name")


@pytest.mark.parametrize("os_type", EDITIONS)
def test_invocation_resolution_matches(os_type):
    for root in [None, "/mnt"]:
        py, native = pair(os_type, root=root)
        for command in COMMAND_STRINGS:
            assert (outcome(py._resolve_invocation, command)
                    == outcome(native._resolve_invocation, command)), command


# ---------------------------------------------------------------------------
# Real execution, against a throwaway profile
# ---------------------------------------------------------------------------

EXEC_PROFILE = {
    "os_name": "Parity",
    "os_family": "linux",
    "os_version": "1",
    "init_system": "systemd",
    "t": {
        "plain": "/bin/echo hello",
        "with_arg": "/bin/echo {name}",
        "piped": "/bin/echo one two | /usr/bin/tr ' ' '-'",
        "to_stderr": "/bin/sh -c 'echo oops >&2'",
        "fails": "/bin/false",
        "fails_loud": "/bin/sh -c 'echo out; echo err >&2; exit 3'",
        "many_lines": "/bin/sh -c 'for i in $(seq 1 40); do echo line$i; done; exit 1'",
        "slow": "/bin/sleep 5",
        "napping": "/bin/sleep 2",
        "sequence": ["/bin/echo first", "/bin/echo second"],
        "missing_binary": "/bin/definitely-not-here",
        "reads_stdin": "/bin/cat",
        "trailing_space": "/bin/echo '  padded  '",
    },
}


@pytest.fixture(scope="module")
def exec_profiles(tmp_path_factory):
    root = tmp_path_factory.mktemp("parity-profiles")
    (root / "os").mkdir()
    (root / "os" / "os-parity.json").write_text(json.dumps(EXEC_PROFILE))
    return str(root)


def exec_pair(exec_profiles, **kwargs):
    return (
        engine_py.FreedomNASEngine.for_target("parity", profile_dir=exec_profiles, **kwargs),
        engine_native.FreedomNASEngine.for_target("parity", profile_dir=exec_profiles, **kwargs),
    )


@pytest.mark.parametrize("action", [
    "plain", "piped", "to_stderr", "fails", "fails_loud", "many_lines",
    "sequence", "missing_binary", "trailing_space",
])
def test_execution_matches(exec_profiles, action):
    py, native = exec_pair(exec_profiles)
    assert outcome(py.execute, "t", action) == outcome(native.execute, "t", action)


@pytest.mark.parametrize("action", ["fails", "fails_loud", "many_lines"])
def test_check_false_matches(exec_profiles, action):
    py, native = exec_pair(exec_profiles)
    assert (outcome(py.execute, "t", action, check=False)
            == outcome(native.execute, "t", action, check=False))


def test_argument_is_passed_and_sanitized(exec_profiles):
    py, native = exec_pair(exec_profiles)
    for value in NASTY_VALUES:
        assert (outcome(py.execute, "t", "with_arg", name=value)
                == outcome(native.execute, "t", "with_arg", name=value)), value


def test_timeout_matches(exec_profiles):
    py, native = exec_pair(exec_profiles)
    assert (outcome(py.execute, "t", "slow", timeout=1)
            == outcome(native.execute, "t", "slow", timeout=1))


def test_stdin_matches(exec_profiles):
    py, native = exec_pair(exec_profiles)
    for data in ["secret\n", "", "line1\nline2\n", "unicode àèì\n"]:
        assert (outcome(py.execute_with_stdin, "t", "reads_stdin", data)
                == outcome(native.execute_with_stdin, "t", "reads_stdin", data))


def test_stdin_refuses_a_sequence(exec_profiles):
    py, native = exec_pair(exec_profiles)
    assert (outcome(py.execute_with_stdin, "t", "sequence", "data")
            == outcome(native.execute_with_stdin, "t", "sequence", "data"))


def test_native_execution_releases_the_gil(exec_profiles):
    """
    The WebUI keeps serving while a command runs.

    A native call that held the GIL would freeze every other Python thread
    for as long as the command takes — a `zpool create` would make the
    whole interface stop answering. The counter thread below has to keep
    moving while the engine waits on `sleep`.
    """
    import threading
    import time

    _, native = exec_pair(exec_profiles)
    ticks = 0
    running = True

    def count():
        nonlocal ticks
        while running:
            ticks += 1
            time.sleep(0.01)

    counter = threading.Thread(target=count, daemon=True)
    counter.start()
    native.execute("t", "napping", timeout=10)
    running = False
    counter.join()

    assert ticks > 100, f"only {ticks} ticks — the GIL was held during execution"


def test_stdin_is_not_the_terminal(exec_profiles):
    """A command that reads stdin gets EOF, not a hung terminal."""
    py, native = exec_pair(exec_profiles)
    assert outcome(py.execute, "t", "reads_stdin") == outcome(native.execute, "t", "reads_stdin")
