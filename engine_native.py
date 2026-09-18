"""
FreedomNAS Core Engine — the same OS abstraction layer, backed by Nim.

Every command translation, profile merge and syscall happens in
freedomnas_native (nim/src/), compiled to a Python extension module. This
file is the thin Python side of that boundary: it keeps the class shape the
rest of FreedomNAS imports — singleton, attributes, exception types — and
translates calls into the native module's JSON protocol.

Two things stay on this side deliberately:

  - the profile search paths, so a development checkout behaves the way it
    always has;
  - reading /etc/os-release, so the detection tests that patch open() keep
    testing the detection logic (which lives in Nim) rather than the file.

Selected by FREEDOMNAS_ENGINE=native; see engine.py.
"""

from __future__ import annotations

import json
import logging
import platform
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

try:
    from freedomnas.engine_py import (
        CommandExecutionError,
        CommandTranslationError,
        FreedomNASEngineException,
        OSNotSupportedException,
    )
except ImportError:
    from engine_py import (  # type: ignore[no-redef]
        CommandExecutionError,
        CommandTranslationError,
        FreedomNASEngineException,
        OSNotSupportedException,
    )

logger = logging.getLogger("FreedomNAS.Core")

_ERROR_CLASSES = {
    "os_not_supported": OSNotSupportedException,
    "translation": CommandTranslationError,
    "execution": CommandExecutionError,
    "engine": FreedomNASEngineException,
    "key": KeyError,
    "value": ValueError,
    "os_error": OSError,
}


def _unwrap(payload: str) -> dict:
    """Decode a native reply, raising the exception its error kind names."""
    result = json.loads(payload)
    error = result.get("error")
    if error is None:
        return result
    kind = error.get("kind", "engine")
    message = error.get("message", "")
    raise _ERROR_CLASSES.get(kind, FreedomNASEngineException)(message)


def _values(kwargs: dict) -> str:
    """
    Split template variables the way the pure-Python engine treats them:
    strings get their shell metacharacters stripped, anything else is
    stringified by the formatter untouched.
    """
    return json.dumps({
        "str": {k: v for k, v in kwargs.items() if isinstance(v, str)},
        "raw": {k: str(v) for k, v in kwargs.items() if not isinstance(v, str)},
    })


class FreedomNASEngine:
    """Native-backed engine; same public API as the pure-Python one."""

    SUPPORTED_OS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]

    _KNOWN_UNSUPPORTED = ("fedora", "rhel", "centos", "rocky", "almalinux")

    _instance: Optional["FreedomNASEngine"] = None
    _lock: threading.Lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls, profile_dir: Optional[str] = None, os_override: Optional[str] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, profile_dir: Optional[str] = None, os_override: Optional[str] = None):
        if self._initialized:
            return

        self.os_type = os_override or self._detect_os()
        self.profile_dir = Path(profile_dir or self._get_default_profile_dir())
        self.root = None
        self.chroot_prefix = None
        self._target_mode = False
        self._init_override = None
        self._handle = None
        self._open(init_system=None, chroot_prefix=None)

        self.__class__._initialized = True
        logger.info(
            f"FreedomNAS Engine initialized for {self.os_type} "
            f"(init: {self.init_system})"
        )

    @classmethod
    def for_target(
        cls,
        os_type: str,
        root: Optional[str] = None,
        profile_dir: Optional[str] = None,
        init_system: Optional[str] = None,
        chroot_prefix: Optional[str] = None,
    ) -> "FreedomNASEngine":
        engine = object.__new__(cls)
        engine._initialized = True
        engine._target_mode = True
        engine._init_override = init_system
        engine.os_type = os_type
        engine.profile_dir = Path(profile_dir or engine._get_default_profile_dir())
        engine.root = None if root in (None, "", "/") else root.rstrip("/")
        engine.chroot_prefix = None
        engine._handle = None
        engine._open(init_system=init_system, chroot_prefix=chroot_prefix)

        logger.info(
            f"FreedomNAS Engine (target) initialized for {os_type} "
            f"(init: {engine.init_system}, root: {engine.root or '/'})"
        )
        return engine

    # ------------------------------------------------------------------
    # Native handle
    # ------------------------------------------------------------------

    def _open(self, init_system: Optional[str], chroot_prefix: Optional[str]) -> None:
        created = _unwrap(_native.engine_create(
            str(self.profile_dir),
            self.os_type,
            init_system or "",
            self._target_mode,
            self.root or "",
            chroot_prefix or "",
        ))
        self._handle = created["handle"]
        self.init_system = created["init_system"]
        self.profile: Dict[str, Any] = created["profile"]
        self.chroot_prefix = created["chroot_prefix"] or None
        for warning in created["warnings"]:
            logger.info(warning)

    def __del__(self):
        handle = getattr(self, "_handle", None)
        if handle is not None:
            try:
                _native.engine_release(handle)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def _detect_os(self) -> str:
        system = platform.system()

        if system == "Linux":
            try:
                with open("/etc/os-release") as f:
                    content = f.read()
            except FileNotFoundError:
                raise OSNotSupportedException(
                    "/etc/os-release not found. Cannot detect Linux distribution."
                )
            resolved = _unwrap(_native.resolve_os(content))
            for warning in resolved["warnings"]:
                logger.warning(warning)
            return resolved["profile"]

        elif system == "FreeBSD":
            return "freebsd"

        else:
            raise OSNotSupportedException(
                f"Operating system '{system}' is not supported by FreedomNAS. "
                f"Supported systems: Linux, FreeBSD"
            )

    @staticmethod
    def _parse_os_release(content: str) -> Dict[str, str]:
        fields = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields[key.strip().lower()] = value.strip().strip('"\'').lower()
        return fields

    def _get_default_profile_dir(self) -> str:
        possible_paths = [
            Path("/usr/lib/freedomnas/profiles"),
            Path(__file__).parent.parent / "freedomnas-profiles",
            Path(__file__).parent.parent / "os_profiles",
            Path(__file__).parent / "os_profiles",
            Path("/opt/freedomnas/lib/os_profiles"),
        ]

        for path in possible_paths:
            if path.exists() and path.is_dir():
                logger.debug(f"Using OS profiles directory: {path}")
                return str(path)

        raise OSNotSupportedException(
            f"Could not find OS profiles directory. "
            f"Searched: {', '.join(str(p) for p in possible_paths)}"
        )

    def _detect_init_system(self) -> str:
        detected = _native.detect_init()
        if detected:
            return detected
        declared = self.profile.get("init_system")
        if declared:
            return declared
        logger.warning("Could not detect init system; defaulting to 'systemd'")
        return "systemd"

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @staticmethod
    def _contains_shell_metachar(command: str) -> bool:
        return _native.has_shell_metachar(command)

    def _sanitize_kwargs(self, kwargs: dict) -> dict:
        return {
            key: _native.sanitize_value(value) if isinstance(value, str) else value
            for key, value in kwargs.items()
        }

    def get_command(self, category: str, action: str, **kwargs) -> Union[str, List[str]]:
        result = _unwrap(_native.engine_command(
            self._handle, category, action, _values(kwargs)
        ))
        if result["is_sequence"]:
            return result["commands"]
        return result["commands"][0]

    def execute(
        self,
        category: str,
        action: str,
        timeout: int = 30,
        check: bool = True,
        **kwargs,
    ) -> Tuple[str, str]:
        result = _unwrap(_native.engine_run(
            self._handle, category, action, _values(kwargs), timeout, check
        ))
        return result["stdout"], result["stderr"]

    def execute_with_stdin(
        self,
        category: str,
        action: str,
        stdin_data: str,
        timeout: int = 30,
        **kwargs,
    ) -> Tuple[str, str]:
        result = _unwrap(_native.engine_run_with_stdin(
            self._handle, category, action, _values(kwargs), stdin_data, timeout
        ))
        return result["stdout"], result["stderr"]

    def get_profile_value(self, category: str, key: str) -> str:
        return _unwrap(_native.engine_value(self._handle, category, key))["value"]

    def get_profile_section(self, category: str) -> Dict[str, Any]:
        return _unwrap(_native.engine_section(self._handle, category))["value"]

    def _resolve_invocation(self, command: str) -> Tuple[Union[str, List[str]], bool]:
        result = _unwrap(_native.engine_invocation(self._handle, command))
        if result["use_shell"]:
            return command, True
        return result["argv"], False

    def _run_safe(self, command: str, timeout: int = 30, check: bool = True) -> Tuple[str, str]:
        raise NotImplementedError(
            "_run_safe is internal to the pure-Python engine; the native engine "
            "runs commands inside freedomnas_native"
        )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_os_info(self) -> Dict[str, str]:
        return _unwrap(_native.engine_info(self._handle))["value"]

    def list_categories(self) -> List[str]:
        return _unwrap(_native.engine_categories(self._handle))["value"]

    def list_actions(self, category: str) -> List[str]:
        return _unwrap(_native.engine_actions(self._handle, category))["value"]

    def __repr__(self) -> str:
        os_info = self.get_os_info()
        return (
            f"FreedomNASEngine(os={os_info['os_name']}, "
            f"type={self.os_type}, "
            f"profile_dir={self.profile_dir})"
        )


def native_handle(engine) -> int:
    """
    The native handle of an engine, for the modules whose logic runs in Nim.

    They drive the engine from inside the extension, so a pure-Python engine
    — FREEDOMNAS_ENGINE=python, or one built from engine_py directly — cannot
    stand in for it. Say so, instead of an AttributeError on `_handle`.
    """
    handle = getattr(engine, "_handle", None)
    if handle is None:
        raise TypeError(
            f"{type(engine).__module__}.{type(engine).__name__} is not the native "
            "engine; the native modules need FREEDOMNAS_ENGINE=native"
        )
    return handle


def get_engine(profile_dir: Optional[str] = None) -> FreedomNASEngine:
    return FreedomNASEngine(profile_dir)
