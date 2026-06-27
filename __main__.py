"""
python -m freedomnas — FreedomNAS Core diagnostic CLI.

Commands:
  status   Print OS info, init system, and loaded profile
  check    Validate profiles directory and config file
  version  Print package version
"""

from __future__ import annotations

import argparse
import json
import sys


def cmd_version(_args) -> int:
    try:
        from importlib.metadata import version
        print(version("freedomnas-core"))
    except Exception:
        print("unknown")
    return 0


def cmd_status(_args) -> int:
    try:
        from freedomnas.engine import get_engine
    except ImportError:
        from engine import get_engine  # type: ignore

    try:
        engine = get_engine()
    except Exception as exc:
        print(f"ERROR: could not initialise engine: {exc}", file=sys.stderr)
        return 1

    info = engine.get_os_info()
    print(json.dumps(info, indent=2))
    return 0


def cmd_check(_args) -> int:
    errors: list[str] = []

    # Verify profiles directory
    try:
        from freedomnas.engine import get_engine
    except ImportError:
        from engine import get_engine  # type: ignore

    try:
        engine = get_engine()
        info = engine.get_os_info()
        print(f"  OK  Profile loaded: {info.get('os_name')} ({info.get('init_system')})")
    except Exception as exc:
        errors.append(f"Profile: {exc}")
        print(f"  FAIL  Profile: {exc}", file=sys.stderr)

    # Verify config file
    try:
        from freedomnas.config import ConfigurationManager
    except ImportError:
        from config import ConfigurationManager  # type: ignore

    try:
        config = ConfigurationManager()
        print(
            f"  OK  Config loaded: hostname={config.get_hostname()}, "
            f"initialized={config.is_initialized()}"
        )
    except Exception as exc:
        errors.append(f"Config: {exc}")
        print(f"  FAIL  Config: {exc}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} error(s) found.", file=sys.stderr)
        return 1

    print("\nAll checks passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m freedomnas",
        description="FreedomNAS Core diagnostic CLI",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Print OS info and loaded profile")
    sub.add_parser("check", help="Validate profiles directory and config file")
    sub.add_parser("version", help="Print package version")

    args = parser.parse_args()

    dispatch = {
        "status":  cmd_status,
        "check":   cmd_check,
        "version": cmd_version,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return 0

    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
