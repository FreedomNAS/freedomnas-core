"""
python -m freedomnas — FreedomNAS Core diagnostic CLI.

Commands:
  status        Print OS info, init system, and loaded profile
  check         Validate profiles directory and config file
  unlock-pools  Unlock ZFS-encrypted pools (USB zero-touch, TTY passphrase fallback)
  reconcile     Apply configuration via the ReconciliationEngine
  version       Print package version
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


def cmd_unlock_pools(args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas.keymgmt import PoolUnlocker
    except ImportError:
        from engine import get_engine  # type: ignore
        from keymgmt import PoolUnlocker  # type: ignore

    try:
        engine = get_engine()
    except Exception as exc:
        print(f"ERROR: could not initialise engine: {exc}", file=sys.stderr)
        return 1

    unlocker = PoolUnlocker(engine)
    locked = unlocker.list_locked_pools()
    if not locked:
        print("No locked ZFS pools found.")
        return 0

    failed = []
    for pool in locked:
        print(f"Pool '{pool}' is locked -- trying USB key...", file=sys.stderr)
        if unlocker.try_usb_unlock(pool):
            print(f"  OK  unlocked '{pool}' via USB key")
            continue

        if args.tty:
            print(f"  no USB key found -- prompting on {args.tty}", file=sys.stderr)
            if unlocker.try_tty_unlock(pool, args.tty, timeout=args.tty_timeout):
                print(f"  OK  unlocked '{pool}' via passphrase")
                continue

        print(f"  FAIL  '{pool}' remains locked", file=sys.stderr)
        failed.append(pool)

    if failed:
        print(f"\n{len(failed)} pool(s) still locked: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def cmd_reconcile(args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas.config import ConfigurationManager
        from freedomnas.reconcile import ReconciliationEngine
    except ImportError:
        from engine import get_engine  # type: ignore
        from config import ConfigurationManager  # type: ignore
        from reconcile import ReconciliationEngine  # type: ignore

    try:
        engine = get_engine()
        config = ConfigurationManager()
        reconciler = ReconciliationEngine(engine, config)
    except Exception as exc:
        print(f"ERROR: could not initialise reconciler: {exc}", file=sys.stderr)
        return 1

    results = reconciler.reconcile(desired_path=args.desired, mode=args.mode)

    failed = 0
    for result in results:
        marker = {"applied": "OK", "skipped": "SKIP", "failed": "FAIL"}.get(result.target_state, "??")
        line = f"  {marker}  {result.action} {result.params}"
        if result.error:
            line += f" -- {result.error}"
        print(line, file=sys.stderr if result.target_state == "failed" else sys.stdout)
        if result.target_state == "failed":
            failed += 1

    print(f"\n{len(results)} action(s), {failed} failure(s).")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m freedomnas",
        description="FreedomNAS Core diagnostic CLI",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="Print OS info and loaded profile")
    sub.add_parser("check", help="Validate profiles directory and config file")
    unlock_parser = sub.add_parser(
        "unlock-pools",
        help="Unlock ZFS-encrypted pools (USB zero-touch, TTY passphrase fallback)",
    )
    unlock_parser.add_argument(
        "--tty",
        default=None,
        help="TTY device to prompt on if no USB key is found (e.g. /dev/console). "
             "Omit to skip the passphrase fallback entirely.",
    )
    unlock_parser.add_argument(
        "--tty-timeout",
        type=int,
        default=300,
        help="Seconds to wait for a passphrase before giving up and continuing boot (default: 300)",
    )
    reconcile_parser = sub.add_parser("reconcile", help="Apply configuration via the ReconciliationEngine")
    reconcile_parser.add_argument(
        "--mode",
        choices=["hybrid", "declarative", "disaster_recovery"],
        default="disaster_recovery",
        help="Reconciliation mode (default: disaster_recovery, for first-boot use)",
    )
    reconcile_parser.add_argument(
        "--desired",
        default=None,
        help="Path to a desired-state YAML file (default: use the config.yaml shadow copy)",
    )
    sub.add_parser("version", help="Print package version")

    args = parser.parse_args()

    dispatch = {
        "status":       cmd_status,
        "check":        cmd_check,
        "unlock-pools": cmd_unlock_pools,
        "reconcile":    cmd_reconcile,
        "version":      cmd_version,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return 0

    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
