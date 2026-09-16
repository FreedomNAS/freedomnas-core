"""
FreedomNAS Configuration Manager — YAML handled by Nim.

The configuration is a plain Python dict here exactly as before, and every
accessor is inherited unchanged from the pure-Python manager. What moves to
Nim is the part that touches the file: parsing, merging with the defaults,
validation, the header, and writing the YAML back out.

The written bytes have to be identical either way — an appliance that saved
its configuration with one engine is read, and rewritten, by the other, and
by whatever copy of the file the shadow datasets carry. That is asserted in
tests/test_config_parity.py, down to line folding and quoting.

Selected by FREEDOMNAS_CONFIG=native; see config.py.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any, Dict

try:
    from freedomnas import freedomnas_native as _native
except ImportError:
    import freedomnas_native as _native  # type: ignore[no-redef]

try:
    from freedomnas.config_py import ConfigurationException, ConfigurationManager as _BaseManager
except ImportError:
    from config_py import (  # type: ignore[no-redef]
        ConfigurationException,
        ConfigurationManager as _BaseManager,
    )

logger = logging.getLogger("FreedomNAS.Config")

TIMESTAMP_MARKER = "__fnas_timestamp__"


def _unwrap(payload: str) -> Any:
    result = json.loads(payload)
    error = result.get("error")
    if error is not None:
        raise ConfigurationException(error.get("message", ""))
    return result["value"]


def _encode(value: Any) -> Any:
    """Python values as the native side expects them, timestamps marked."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return {TIMESTAMP_MARKER: str(value)}
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    """The inverse: a marked timestamp becomes the datetime PyYAML returns."""
    if isinstance(value, dict):
        if len(value) == 1 and TIMESTAMP_MARKER in value:
            return _parse_timestamp(value[TIMESTAMP_MARKER])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _parse_timestamp(text: str):
    try:
        if len(text) == 10:
            return datetime.date.fromisoformat(text)
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return text


def _to_native(value: Any) -> str:
    return json.dumps(_encode(value))


class ConfigurationManager(_BaseManager):
    """Same manager; the YAML end of it runs in Nim."""

    def load(self) -> None:
        if not self.config_path.exists():
            logger.info("No configuration file found. Using defaults.")
            self.config = _decode(_unwrap(_native.config_defaults()))
            self._validate_config(self.config)
            return

        try:
            text = self.config_path.read_text()
        except Exception as e:
            raise ConfigurationException(f"Error loading configuration: {e}")

        loaded = _decode(_unwrap(_native.config_parse(text))) or {}
        self.config = self._merge_with_defaults(loaded)
        self._validate_config(self.config)
        logger.info(f"Loaded configuration from: {self.config_path}")

    def save(self) -> None:
        try:
            self._validate_config(self.config)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            temp_path = self.config_path.with_suffix(".yaml.tmp")
            payload = _to_native(self.config)
            header = self._get_config_header()
            body = _unwrap(_native.config_dump(payload))

            with open(temp_path, "w") as f:
                f.write(header)
                f.write(body)

            temp_path.chmod(0o600)
            logger.debug(f"Wrote temporary config to: {temp_path}")

            backup_path = self.config_path.with_suffix(".yaml.backup")
            if self.config_path.exists():
                self.config_path.rename(backup_path)
                logger.info(f"Created backup: {backup_path}")

            temp_path.rename(self.config_path)
            logger.info(f"Saved configuration to: {self.config_path}")

        except ConfigurationException:
            raise
        except Exception as e:
            backup_path = self.config_path.with_suffix(".yaml.backup")
            if backup_path.exists() and not self.config_path.exists():
                backup_path.rename(self.config_path)
                logger.warning(f"Save failed, restored backup from: {backup_path}")
            raise ConfigurationException(f"Error saving configuration: {e}")

    def _merge_with_defaults(self, loaded_config: Dict[str, Any]) -> Dict[str, Any]:
        return _decode(_unwrap(_native.config_merge_defaults(_to_native(loaded_config))))

    def _validate_config(self, config: Dict[str, Any]) -> bool:
        _unwrap(_native.config_validate(_to_native(config)))
        logger.debug("Configuration validation passed")
        return True

    def _get_config_header(self) -> str:
        return _unwrap(_native.config_header(
            _to_native(self.config), datetime.datetime.now().isoformat()
        ))
