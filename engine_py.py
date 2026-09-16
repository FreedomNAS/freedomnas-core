"""
FreedomNAS Core Engine - Universal OS Abstraction Layer (pure Python)

This module implements the JSON Translator Layer, allowing FreedomNAS middleware
to execute OS-native commands in a unified way across different operating systems.

Kept as the reference implementation the native engine is verified against,
and as the fallback when FREEDOMNAS_ENGINE=python. See engine.py.

The engine:
1. Detects the running OS (Linux/Debian, FreeBSD, etc.)
2. Loads the appropriate JSON profile with OS-specific commands
3. Translates abstract actions to native commands
4. Executes commands securely without shell injection
"""

import json
import platform
import subprocess
import shlex
import os
import logging
import threading
from pathlib import Path
from typing import Union, List, Dict, Any, Tuple, Optional

# Configure logging
logger = logging.getLogger("FreedomNAS.Core")


def _last_lines(text: str, count: int = 12) -> str:
    """
    The tail of a failed command's output, for the exception message.

    A package manager writes hundreds of lines and puts the reason it gave up
    in the last few; the whole thing goes to the log, this goes to the human.
    """
    lines = text.strip().splitlines()
    if len(lines) <= count:
        return "\n".join(lines)
    return "\n".join(["[...]"] + lines[-count:])


class FreedomNASEngineException(Exception):
    """Base exception for FreedomNAS engine errors"""
    pass


class OSNotSupportedException(FreedomNASEngineException):
    """Raised when the detected OS is not supported"""
    pass


class CommandTranslationError(FreedomNASEngineException):
    """Raised when a command cannot be translated for the current OS"""
    pass


class CommandExecutionError(FreedomNASEngineException):
    """Raised when a command execution fails"""
    pass


class FreedomNASEngine:
    """
    Universal OS abstraction engine for FreedomNAS.
    
    Translates abstract operations into OS-specific commands using JSON profiles.
    Ensures all system calls are safe and os-agnostic.
    
    Implemented as a singleton to ensure only one engine instance exists.
    Thread-safe initialization using locks.
    
    Example:
        engine = FreedomNASEngine()
        
        # Get a command without executing it
        cmd = engine.get_command("services", "restart", service_name="samba")
        print(cmd)  # systemctl restart samba (on Debian)
        
        # Execute a command safely
        stdout, stderr = engine.execute("services", "restart", service_name="samba")
    """
    
    # Supported OS families
    SUPPORTED_OS = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]

    # /etc/os-release ID (or ID_LIKE) -> profile name. Several ids can map to
    # one profile when a derivative is close enough to reuse it wholesale.
    _DISTRO_IDS = {
        "debian": "debian",
        "ubuntu": "debian",
        "arch": "arch",
        "artix": "artix",
        "void": "void",
        "gentoo": "gentoo",
    }

    # Recognised but deliberately refused, so the error can say why.
    _KNOWN_UNSUPPORTED = ("fedora", "rhel", "centos", "rocky", "almalinux")

    # Profile subdirectories that are never treated as module fragments:
    # "os" holds shared OS-level metadata, "init" holds per-init-system service
    # commands, "packages" holds distro package-group manifests (unrelated to
    # engine command profiles).
    _RESERVED_PROFILE_DIRS = {"os", "init", "packages"}
    
    # Singleton pattern variables
    _instance: Optional['FreedomNASEngine'] = None
    _lock: threading.Lock = threading.Lock()
    _initialized: bool = False
    
    def __new__(cls, profile_dir: Optional[str] = None, os_override: Optional[str] = None):
        """
        Implement singleton pattern with thread safety.
        
        Returns:
            FreedomNASEngine: The single instance of the engine
        """
        if cls._instance is None:
            with cls._lock:
                # Double-check pattern
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, profile_dir: Optional[str] = None, os_override: Optional[str] = None):
        """
        Initialize the FreedomNAS Engine.
        
        Only initializes once due to singleton pattern.
        Subsequent calls do nothing.
        
        Args:
            profile_dir: Custom directory for OS profiles. Defaults to lib/os_profiles/
            os_override: Force a specific OS (for testing). Defaults to None (auto-detect)
        
        Raises:
            OSNotSupportedException: If the detected OS is not supported
        """
        # Only initialize once
        if self._initialized:
            return
        
        with self._lock:
            # Double-check pattern
            if self._initialized:
                return
            
            self.os_type = os_override or self._detect_os()
            self.profile_dir = Path(profile_dir or self._get_default_profile_dir())
            self.root = None
            self.chroot_prefix = None
            self._target_mode = False
            self._init_override = None
            self.profile, self.init_system = self._load_profile()

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
        """
        Build a non-singleton engine bound to a *target* OS instead of the
        running one.

        The singleton returned by get_engine() always describes the machine
        the process is running on — correct for the WebUI and the modules,
        useless for an installer that provisions a different edition into a
        chroot. This constructor bypasses the singleton entirely: the
        returned engine loads `os_type`'s profile and, when `root` is given,
        prefixes every command it executes with the chroot invocation taken
        from the profile's `chroot.exec_prefix`.

        Args:
            os_type: Target edition ("debian", "freebsd", "void", ...)
            root: Target root directory. None or "/" means "run on the host",
                  which is what the in-place install mode wants.
            profile_dir: Profiles directory (defaults to the usual search path)
            init_system: Target init system. Defaults to the target profile's
                         declared `init_system` — a target engine never probes
                         the running host for it.
            chroot_prefix: Override for the profile's `chroot.exec_prefix`

        Returns:
            FreedomNASEngine: a fresh instance, not the singleton

        Raises:
            OSNotSupportedException: If no profile exists for os_type
            CommandTranslationError: If root is set but no chroot.exec_prefix
                                     is available
        """
        engine = object.__new__(cls)
        engine._initialized = True
        engine._target_mode = True
        engine._init_override = init_system
        engine.os_type = os_type
        engine.profile_dir = Path(profile_dir or engine._get_default_profile_dir())
        engine.root = None if root in (None, "", "/") else root.rstrip("/")
        engine.chroot_prefix = None
        engine.profile, engine.init_system = engine._load_profile()

        if engine.root is not None:
            template = chroot_prefix or engine.profile.get("chroot", {}).get("exec_prefix")
            if not template:
                raise CommandTranslationError(
                    f"No chroot.exec_prefix for '{os_type}' — cannot execute "
                    f"commands against target root {engine.root}. Add it to "
                    f"profiles/installer/installer-{os_type}.json."
                )
            engine.chroot_prefix = template.format(root=engine.root)

        logger.info(
            f"FreedomNAS Engine (target) initialized for {os_type} "
            f"(init: {engine.init_system}, root: {engine.root or '/'})"
        )
        return engine

    def _detect_os(self) -> str:
        """
        Detect the running operating system.
        
        Returns:
            str: OS identifier ("debian", "freebsd", "arch", etc.)
        
        Raises:
            OSNotSupportedException: If the OS is not supported
        """
        system = platform.system()

        if system == "Linux":
            try:
                with open("/etc/os-release") as f:
                    content = f.read()
            except FileNotFoundError:
                raise OSNotSupportedException(
                    "/etc/os-release not found. Cannot detect Linux distribution."
                )

            fields = self._parse_os_release(content)
            distro_id = fields.get("id", "")

            # Exact ID first: a derivative that has its own profile (Artix)
            # must never fall through to its parent (Arch) via ID_LIKE.
            if distro_id in self._DISTRO_IDS:
                profile = self._DISTRO_IDS[distro_id]
                if profile != distro_id:
                    logger.warning(
                        f"{distro_id} detected, using the {profile} profile"
                    )
                return profile

            # Then the declared parent, for a derivative we don't know about.
            for parent in fields.get("id_like", "").split():
                if parent in self._DISTRO_IDS:
                    profile = self._DISTRO_IDS[parent]
                    logger.warning(
                        f"Unknown distribution '{distro_id}', using the "
                        f"{profile} profile it declares itself compatible with"
                    )
                    return profile

            if distro_id in self._KNOWN_UNSUPPORTED:
                raise OSNotSupportedException(
                    "RHEL-based systems not yet supported. "
                    "Please use Debian or FreeBSD Edition."
                )
            raise OSNotSupportedException(
                f"Unknown Linux distribution '{distro_id or content[:40]}'. "
                f"Supported: {', '.join(sorted(self._DISTRO_IDS))}."
            )

        elif system == "FreeBSD":
            return "freebsd"

        else:
            raise OSNotSupportedException(
                f"Operating system '{system}' is not supported by FreedomNAS. "
                f"Supported systems: Linux, FreeBSD"
            )

    @staticmethod
    def _parse_os_release(content: str) -> Dict[str, str]:
        """
        Parse /etc/os-release into a lowercased key -> value dict.

        Matching on the ID field rather than searching the whole file for a
        substring matters once several editions are supported: Ubuntu's file
        mentions Debian, Artix's may declare ID_LIKE=arch, and a substring
        search would hand either of them the wrong profile.
        """
        fields = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields[key.strip().lower()] = value.strip().strip('"\'').lower()
        return fields
    
    def _get_default_profile_dir(self) -> str:
        """
        Get the default directory for OS profiles.

        Returns:
            str: Path to the os_profiles directory
        """
        # Try multiple locations to support both installer and webui
        possible_paths = [
            Path("/usr/lib/freedomnas/profiles"),                   # system install (spec §3.5)
            Path(__file__).parent.parent / "freedomnas-profiles",   # dev sibling repo
            Path(__file__).parent.parent / "os_profiles",           # dev local
            Path(__file__).parent / "os_profiles",                  # dev local
            Path("/opt/freedomnas/lib/os_profiles"),                 # alternative
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
        """
        Detect the running init system.

        Checks filesystem markers in the order defined by spec §4:
        a) /run/systemd/system  → systemd
        b) /sbin/openrc or /usr/sbin/openrc  → openrc
        c) /sbin/runit or /run/runit  → runit
        d) /usr/bin/s6-svscan  → s6
        e) /usr/bin/dinit  → dinit
        f) /etc/rc.d + FreeBSD  → bsd_rc
        g) fallback: "init_system" field from OS profile

        Returns:
            str: init system identifier (e.g. "systemd", "openrc", ...)
        """
        import platform as _platform

        if Path("/run/systemd/system").exists():
            return "systemd"
        if Path("/sbin/openrc").exists() or Path("/usr/sbin/openrc").exists():
            return "openrc"
        if Path("/sbin/runit").exists() or Path("/run/runit").exists():
            return "runit"
        if Path("/usr/bin/s6-svscan").exists():
            return "s6"
        if Path("/usr/bin/dinit").exists():
            return "dinit"
        if Path("/etc/rc.d").exists() and _platform.system() == "FreeBSD":
            return "bsd_rc"

        # Fallback: read from OS profile if present
        declared = self.profile.get("init_system")
        if declared:
            logger.debug(f"Init system not detected at runtime; using profile declaration: {declared}")
            return declared

        logger.warning("Could not detect init system; defaulting to 'systemd'")
        return "systemd"

    def _load_init_profile(self, init_system: str) -> Optional[Dict[str, Any]]:
        """
        Load the init profile for the given init system.

        Searches for {profile_dir}/init/{init_system}.json.

        Returns:
            dict with the init profile, or None if not found.
        """
        init_path = self.profile_dir / "init" / f"init-{init_system}.json"
        if not init_path.exists():
            logger.debug(f"Init profile not found: {init_path}")
            return None
        try:
            with open(init_path, "r") as f:
                init_profile = json.load(f)
            logger.debug(f"Loaded init profile: {init_path}")
            return init_profile
        except json.JSONDecodeError as exc:
            raise FreedomNASEngineException(
                f"Invalid JSON in init profile {init_path}: {exc}"
            )

    def _merge_module_profiles(self, profile: Dict[str, Any]) -> None:
        """
        Scan every module's profile subdirectory (e.g. profiles/storage/) and
        merge in {dirname}/{dirname}-{os_type}.json when present.

        Each module fragment contributes its own top-level category key(s),
        so there's no overlap to resolve — this is a plain dict.update() per
        fragment. Reserved directories (_RESERVED_PROFILE_DIRS) are skipped;
        a directory with no fragment for the current OS is silently skipped
        too (e.g. a community module that doesn't support this OS).
        """
        for entry in sorted(self.profile_dir.iterdir()):
            if not entry.is_dir() or entry.name in self._RESERVED_PROFILE_DIRS:
                continue
            fragment_path = entry / f"{entry.name}-{self.os_type}.json"
            if not fragment_path.exists():
                continue
            try:
                with open(fragment_path, "r") as f:
                    fragment = json.load(f)
            except json.JSONDecodeError as exc:
                raise FreedomNASEngineException(
                    f"Invalid JSON in profile fragment {fragment_path}: {exc}"
                )
            profile.update(fragment)
            logger.debug(f"Merged module profile fragment: {fragment_path}")

    def _load_profile(self) -> Tuple[Dict[str, Any], str]:
        """
        Load and merge the OS profile with every module's profile fragment,
        then with the appropriate init profile.

        Steps:
        1. Load the shared OS profile (profiles/os/os-{os_type}.json)
        2. Merge in each module's profiles/{module}/{module}-{os_type}.json
        3. Detect the running init system
        4. Load init profile (e.g. init/init-systemd.json)
        5. Merge: init profile's "services" replaces OS profile's "services"
        6. Backward compat: if no init profile found and OS profile has
           "services", keep it with a deprecation warning.

        Returns:
            Tuple[dict, str]: (merged profile dict, init_system name)

        Raises:
            OSNotSupportedException: If the OS profile file does not exist.
        """
        profile_path = self.profile_dir / "os" / f"os-{self.os_type}.json"

        if not profile_path.exists():
            raise OSNotSupportedException(
                f"OS profile not found: {profile_path}\n"
                f"Supported profiles: {', '.join(f'os-{os}.json' for os in self.SUPPORTED_OS)}"
            )

        try:
            with open(profile_path, "r") as f:
                profile = json.load(f)
            logger.debug(f"Loaded OS profile: {profile_path}")
        except json.JSONDecodeError as exc:
            raise FreedomNASEngineException(
                f"Invalid JSON in profile {profile_path}: {exc}"
            )
        except Exception as exc:
            raise FreedomNASEngineException(
                f"Error loading profile {profile_path}: {exc}"
            )

        self._merge_module_profiles(profile)

        # Detect init system (may read profile.get("init_system") as fallback).
        # A target engine never probes the running host's filesystem — the
        # target's own init system is whatever its profile declares.
        if self._init_override:
            init_system = self._init_override
        elif self._target_mode:
            init_system = profile.get("init_system", "systemd")
        else:
            init_system = self._detect_init_system()

        # Load and merge init profile
        init_profile = self._load_init_profile(init_system)

        if init_profile is not None:
            profile["services"] = init_profile["services"]
            logger.info(f"Merged init profile '{init_system}' into OS profile")
        elif "services" in profile:
            # Backward compatibility: OS profile still has services section
            logger.warning(
                f"WARNING: OS profile '{self.os_type}' contains a 'services' section. "
                "This is deprecated. Move service commands to "
                f"init/init-{init_system}.json"
            )
        else:
            logger.warning(
                f"No init profile found for '{init_system}' and OS profile has no "
                "'services' section. engine.execute('services', ...) will fail."
            )

        return profile, init_system
    
    @staticmethod
    def _contains_shell_metachar(command: str) -> bool:
        """Return True if the command string contains shell metacharacters."""
        return any(tok in command for tok in ('|', '>', '<', ';', '&', '$('))

    def _sanitize_kwargs(self, kwargs: dict) -> dict:
        """
        Strip shell-injection characters from all template variable values.

        This is a belt-and-suspenders measure for commands routed through
        shell=True.  Template variables for paths, IPs, and service names
        should never contain these characters legitimately.
        """
        dangerous = frozenset(';|&$`\\\n\r')
        sanitized = {}
        for key, value in kwargs.items():
            if isinstance(value, str):
                sanitized[key] = ''.join(c for c in value if c not in dangerous)
            else:
                sanitized[key] = value
        return sanitized

    def get_command(
        self,
        category: str,
        action: str,
        **kwargs
    ) -> Union[str, List[str]]:
        """
        Retrieve and format a command without executing it.
        
        Args:
            category: Command category (e.g., "services", "zfs", "network")
            action: Action within the category (e.g., "restart", "create", "up")
            **kwargs: Template variables to format into the command
        
        Returns:
            Union[str, List[str]]: Formatted command(s). Can be a single command
                                  or a list of commands that should be executed sequentially.
        
        Raises:
            CommandTranslationError: If the command cannot be found in the profile
        
        Example:
            cmd = engine.get_command("zfs", "pool_create",
                                    pool_name="datapool",
                                    vdev_type="raidz2",
                                    ashift=12,
                                    compression="lz4",
                                    devices="/dev/sda /dev/sdb /dev/sdc")
        """
        try:
            command_template = self.profile[category][action]
        except KeyError:
            available_actions = list(self.profile.get(category, {}).keys())
            raise CommandTranslationError(
                f"Command not found: {category}.{action}\n"
                f"Available actions in '{category}': {', '.join(available_actions)}"
            )

        safe_kwargs = self._sanitize_kwargs(kwargs)

        # Handle both single commands and command sequences
        if isinstance(command_template, list):
            return [cmd.format(**safe_kwargs) for cmd in command_template]
        else:
            return command_template.format(**safe_kwargs)
    
    def execute(
        self,
        category: str,
        action: str,
        timeout: int = 30,
        check: bool = True,
        **kwargs
    ) -> Tuple[str, str]:
        """
        Execute a command translated for the current OS.
        
        Args:
            category: Command category
            action: Action within the category
            timeout: Command timeout in seconds (default: 30)
            check: If True, raise exception on non-zero return code (default: True)
            **kwargs: Template variables for the command
        
        Returns:
            Tuple[str, str]: (stdout, stderr) as strings
        
        Raises:
            CommandTranslationError: If the command cannot be translated
            CommandExecutionError: If the command fails and check=True
            subprocess.TimeoutExpired: If the command exceeds the timeout
        
        Example:
            stdout, stderr = engine.execute("services", "start", service_name="samba")
            if stderr:
                logger.warning(f"Command warning: {stderr}")
        """
        try:
            command_template = self.get_command(category, action, **kwargs)
        except CommandTranslationError:
            raise
        
        # Handle command sequences (list of commands)
        if isinstance(command_template, list):
            last_result = ("", "")
            for i, cmd in enumerate(command_template):
                logger.debug(f"Executing command {i+1}/{len(command_template)}: {cmd}")
                result = self._run_safe(cmd, timeout, check)
                last_result = result
            return last_result
        
        # Execute single command
        logger.debug(f"Executing command: {command_template}")
        return self._run_safe(command_template, timeout, check)

    def execute_with_stdin(
        self,
        category: str,
        action: str,
        stdin_data: str,
        timeout: int = 30,
        **kwargs
    ) -> Tuple[str, str]:
        """
        Execute a command, piping data to its stdin.

        Used for operations where sensitive data (e.g. passwords) must be
        passed via stdin rather than the command string, so they never appear
        in ps output or logs.  Always runs with shell=False.

        Args:
            category: Command category
            action: Action within the category
            stdin_data: String to pipe to the process stdin
            timeout: Command timeout in seconds (default: 30)
            **kwargs: Template variables for the command

        Returns:
            Tuple[str, str]: (stdout, stderr)

        Raises:
            CommandTranslationError: If the command is a sequence (not allowed here)
            CommandExecutionError: If the command fails
        """
        command = self.get_command(category, action, **kwargs)

        if isinstance(command, list):
            raise CommandTranslationError(
                f"{category}.{action} is a command sequence — "
                "execute_with_stdin requires a single command"
            )

        logger.debug(f"Executing command with stdin: {command}")
        argv = shlex.split(command)
        if self.chroot_prefix:
            argv = shlex.split(self.chroot_prefix) + argv
        try:
            result = subprocess.run(
                argv,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                raise CommandExecutionError(
                    f"Command failed with exit code {result.returncode}\n"
                    f"Command: {command}\n"
                    f"Stderr: {result.stderr}"
                )
            return result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            raise CommandExecutionError(
                f"Command timed out after {timeout}s: {command}"
            )
        except CommandExecutionError:
            raise
        except Exception as e:
            raise CommandExecutionError(
                f"Error executing command: {command}\nError: {e}"
            )

    def get_profile_value(self, category: str, key: str) -> str:
        """
        Return a non-command value from the profile (e.g. config_file paths,
        service names like smb_service or nfs_service).

        Args:
            category: Profile category (e.g. "samba", "nfs")
            key: Key within the category (e.g. "config_file", "smb_service")

        Returns:
            str: The value from the profile

        Raises:
            CommandTranslationError: If the category or key does not exist
        """
        try:
            return self.profile[category][key]
        except KeyError:
            available = list(self.profile.get(category, {}).keys())
            raise CommandTranslationError(
                f"Profile value not found: {category}.{key}\n"
                f"Available keys in '{category}': {', '.join(available)}"
            )

    def get_profile_section(self, category: str) -> Dict[str, Any]:
        """
        A whole data section of the profile, copied.

        `get_profile_value` answers for one key; some sections are catalogues
        rather than single values — `zfs_properties` describes every tunable
        the edition supports — and the caller wants all of it.
        """
        section = self.profile.get(category, {})
        return dict(section) if isinstance(section, dict) else {}

    def _resolve_invocation(
        self,
        command: str
    ) -> Tuple[Union[str, List[str]], bool]:
        """
        Turn a formatted command string into the (argv, shell) pair to run.

        Without a chroot prefix this is the historical behaviour: commands
        containing shell metacharacters run via shell=True, everything else
        via shlex.split + shell=False.

        With a chroot prefix (a target engine, see for_target()) nothing ever
        goes through shell=True — the prefix argv is prepended and a command
        that needs a shell is handed to `sh -c` inside the chroot as a single
        argument, so quoting inside the command can never escape into the
        host's shell.
        """
        needs_shell = self._contains_shell_metachar(command)

        if not self.chroot_prefix:
            if needs_shell:
                return command, True
            return shlex.split(command), False

        prefix = shlex.split(self.chroot_prefix)
        if needs_shell:
            return prefix + ["sh", "-c", command], False
        return prefix + shlex.split(command), False

    def _run_safe(
        self,
        command: str,
        timeout: int = 30,
        check: bool = True
    ) -> Tuple[str, str]:
        """
        Execute a command safely.

        Commands without shell metacharacters run via shlex.split + shell=False.
        Commands that contain pipes, redirects, or && run via shell=True so the
        shell can interpret those operators correctly.  Template variables are
        sanitized by _sanitize_kwargs before they reach this method, limiting
        injection risk to the JSON profile author.

        Standard input is /dev/null, never the caller's terminal: a command
        that tries to prompt gets EOF and fails instead of blocking forever
        on a question nobody can see.

        Args:
            command: Fully-formatted command string to execute
            timeout: Timeout in seconds
            check: If True, raise on non-zero exit code

        Returns:
            Tuple[str, str]: (stdout, stderr)

        Raises:
            CommandExecutionError: If the command fails
        """
        try:
            argv, use_shell = self._resolve_invocation(command)
            result = subprocess.run(
                argv,
                shell=use_shell,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )

            if check and result.returncode != 0:
                logger.error(
                    "Command failed with exit code %s: %s\nstdout:\n%s\nstderr:\n%s",
                    result.returncode, command, result.stdout, result.stderr,
                )
                raise CommandExecutionError(
                    f"Command failed with exit code {result.returncode}\n"
                    f"Command: {command}\n"
                    f"Stdout: {_last_lines(result.stdout)}\n"
                    f"Stderr: {_last_lines(result.stderr)}"
                )

            return result.stdout.strip(), result.stderr.strip()

        except subprocess.TimeoutExpired:
            raise CommandExecutionError(
                f"Command timed out after {timeout}s: {command}"
            )
        except CommandExecutionError:
            raise
        except Exception as e:
            raise CommandExecutionError(
                f"Error executing command: {command}\n"
                f"Error: {e}"
            )
    
    def get_os_info(self) -> Dict[str, str]:
        """
        Get information about the detected OS and init system.

        Returns:
            Dict with OS metadata from the profile, including init_system.
        """
        return {
            "os_name": self.profile.get("os_name", "Unknown"),
            "os_family": self.profile.get("os_family", "Unknown"),
            "os_version": self.profile.get("os_version", "Unknown"),
            "init_system": self.init_system,
            "detected_type": self.os_type,
        }
    
    def list_categories(self) -> List[str]:
        """
        List all available command categories in the profile.
        
        Returns:
            List of category names
        """
        return [
            cat for cat in self.profile.keys()
            if not cat.startswith("os_")
        ]
    
    def list_actions(self, category: str) -> List[str]:
        """
        List all available actions in a category.
        
        Args:
            category: Category name
        
        Returns:
            List of action names
        """
        if category not in self.profile:
            return []
        return list(self.profile[category].keys())
    
    def __repr__(self) -> str:
        """String representation"""
        os_info = self.get_os_info()
        return (
            f"FreedomNASEngine(os={os_info['os_name']}, "
            f"type={self.os_type}, "
            f"profile_dir={self.profile_dir})"
        )


def get_engine(profile_dir: Optional[str] = None) -> FreedomNASEngine:
    """
    Convenience function to get or create the singleton engine instance.
    
    This is useful for modules that need a consistent engine instance
    without having to pass it around everywhere.
    
    Since FreedomNASEngine implements singleton pattern internally,
    this simply creates/returns the single instance.
    
    Args:
        profile_dir: Custom directory for OS profiles (only used on first call)
    
    Returns:
        FreedomNASEngine: The singleton engine instance
    
    Example:
        # First call: creates the engine
        engine = get_engine()
        
        # Subsequent calls: returns the same instance
        same_engine = get_engine()
        assert engine is same_engine  # True
    """
    return FreedomNASEngine(profile_dir)
