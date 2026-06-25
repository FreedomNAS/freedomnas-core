"""
FreedomNAS Core Engine - Universal OS Abstraction Layer

This module implements the JSON Translator Layer, allowing FreedomNAS middleware
to execute OS-native commands in a unified way across different operating systems.

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
    SUPPORTED_OS = ["debian", "freebsd", "arch"]
    
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
            self.profile = self._load_profile()
            
            self.__class__._initialized = True
            logger.info(f"FreedomNAS Engine initialized for {self.os_type}")
    
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
            # Try to detect specific Linux distribution
            try:
                with open("/etc/os-release") as f:
                    content = f.read().lower()
                    
                if "debian" in content:
                    return "debian"
                elif "ubuntu" in content:
                    # Ubuntu is based on Debian, use the same profile
                    logger.warning("Ubuntu detected, using Debian profile")
                    return "debian"
                elif "arch" in content:
                    return "arch"
                elif "fedora" in content or "rhel" in content or "centos" in content:
                    raise OSNotSupportedException(
                        "RHEL-based systems not yet supported. "
                        "Please use Debian or FreeBSD Edition."
                    )
                else:
                    raise OSNotSupportedException(
                        f"Unknown Linux distribution. "
                        f"Please use Debian-based or FreeBSD Edition."
                    )
            except FileNotFoundError:
                raise OSNotSupportedException(
                    "/etc/os-release not found. Cannot detect Linux distribution."
                )
        
        elif system == "FreeBSD":
            return "freebsd"
        
        else:
            raise OSNotSupportedException(
                f"Operating system '{system}' is not supported by FreedomNAS. "
                f"Supported systems: Linux (Debian), FreeBSD"
            )
    
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
    
    def _load_profile(self) -> Dict[str, Any]:
        """
        Load the OS-specific command profile.
        
        Returns:
            Dict: OS profile with command templates
        
        Raises:
            OSNotSupportedException: If the profile file doesn't exist
        """
        profile_path = self.profile_dir / f"{self.os_type}.json"
        
        if not profile_path.exists():
            raise OSNotSupportedException(
                f"OS profile not found: {profile_path}\n"
                f"Supported profiles: {', '.join(f'{os}.json' for os in self.SUPPORTED_OS)}"
            )
        
        try:
            with open(profile_path, 'r') as f:
                profile = json.load(f)
            logger.debug(f"Loaded profile: {profile_path}")
            return profile
        except json.JSONDecodeError as e:
            raise FreedomNASEngineException(
                f"Invalid JSON in profile {profile_path}: {e}"
            )
        except Exception as e:
            raise FreedomNASEngineException(
                f"Error loading profile {profile_path}: {e}"
            )
    
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
        try:
            result = subprocess.run(
                shlex.split(command),
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
            if self._contains_shell_metachar(command):
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            else:
                result = subprocess.run(
                    shlex.split(command),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )

            if check and result.returncode != 0:
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
                f"Error executing command: {command}\n"
                f"Error: {e}"
            )
    
    def get_os_info(self) -> Dict[str, str]:
        """
        Get information about the detected OS.
        
        Returns:
            Dict with OS metadata from the profile
        """
        return {
            "os_name": self.profile.get("os_name", "Unknown"),
            "os_family": self.profile.get("os_family", "Unknown"),
            "os_version": self.profile.get("os_version", "Unknown"),
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
