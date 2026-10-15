"""
python -m freedomnas — FreedomNAS Core diagnostic CLI.

Commands:
  status        Print OS info, init system, and loaded profile
  check         Validate profiles directory and config file
  unlock-pools  Unlock ZFS-encrypted pools (USB zero-touch, TTY passphrase fallback)
  reconcile     Apply configuration via the ReconciliationEngine
  serve         Load and start all modules under the Orchestrator, run until stopped
  path          Print one of this edition's FreedomNAS directories
  version       Print package version
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading


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


def cmd_grow_root_pool(_args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas.grow import grow_root_pool
    except ImportError:
        from engine import get_engine  # type: ignore
        from grow import grow_root_pool  # type: ignore

    try:
        engine = get_engine()
    except Exception as exc:
        print(f"ERROR: could not initialise engine: {exc}", file=sys.stderr)
        return 1

    result = grow_root_pool(engine)
    if result["grown"]:
        print(f"Grew pool '{result['pool']}' to the end of {result['device']}")
        return 0
    print(f"Root pool not grown: {result['reason']}")
    return 0


def cmd_snapshot_run(args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas.config import ConfigurationManager
        from freedomnas.snapshots import SnapshotScheduler
    except ImportError:
        from engine import get_engine  # type: ignore
        from config import ConfigurationManager  # type: ignore
        from snapshots import SnapshotScheduler  # type: ignore

    try:
        engine = get_engine()
        config = ConfigurationManager()
    except Exception as exc:
        print(f"ERROR: could not initialise: {exc}", file=sys.stderr)
        return 1

    results = SnapshotScheduler(engine, config).run(args.interval)
    if not results:
        print(f"No dataset asks for {args.interval} snapshots.")
        return 0

    failed = 0
    for r in results:
        if r["error"]:
            failed += 1
            print(f"FAIL  {r['dataset']}: {r['error']}", file=sys.stderr)
            continue
        pruned = f", pruned {len(r['destroyed'])}" if r["destroyed"] else ""
        print(f"OK    {r['created']}{pruned}")
    return 1 if failed else 0


def cmd_shadow_sync(_args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas.config import ConfigurationManager
        from freedomnas import shadow
    except ImportError:
        from engine import get_engine  # type: ignore
        from config import ConfigurationManager  # type: ignore
        import shadow  # type: ignore

    try:
        engine = get_engine()
        config = ConfigurationManager()
    except Exception as exc:
        print(f"ERROR: could not initialise: {exc}", file=sys.stderr)
        return 1

    results = shadow.sync(engine, str(config.config_path))
    if not results:
        print("No other pool carries a .freedomnas dataset — nothing to mirror.")
        return 0

    failed = 0
    for r in results:
        if r["copied"]:
            print(f"OK    {r['dataset']}")
        else:
            failed += 1
            print(f"FAIL  {r['dataset']}: {r['error']}", file=sys.stderr)
    return 1 if failed else 0


def cmd_state_boot(_args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas import shadow
    except ImportError:
        from engine import get_engine  # type: ignore
        import shadow  # type: ignore

    try:
        result = shadow.boot(get_engine())
    except Exception as exc:
        print(f"ERROR: could not put the configuration in place: {exc}", file=sys.stderr)
        return 1
    print(f"action {result['action']}")
    print(f"internal {result['internal']}")
    if result["source"]:
        print(f"source {result['source']}")
    for pool in result["keep"]:
        print(f"keep {pool}")
    return 0


def cmd_ensure_tls(args) -> int:
    try:
        from freedomnas import tls
    except ImportError:
        import tls  # type: ignore

    try:
        created = tls.ensure_certificate(args.hostname)
    except Exception as exc:
        print(f"ERROR: could not create the TLS certificate: {exc}", file=sys.stderr)
        return 1

    if created:
        print(f"Created a self-signed certificate at {tls.cert_file()}")
    print(f"Certificate fingerprint (SHA-256): {tls.fingerprint()}")
    return 0


def cmd_path(args) -> int:
    try:
        from freedomnas import paths
    except ImportError:
        import paths  # type: ignore

    try:
        resolve = {
            "install-root": lambda: paths.INSTALL_ROOT,
            "config":       paths.config_dir,
            "state":        paths.state_dir,
            "tls":          paths.tls_dir,
            "keyslots":     paths.keyslot_dir,
            "setup-pending": lambda: paths.config_dir() / "setup-pending",
        }[args.name]
        print(resolve())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
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

    results = reconciler.reconcile(desired_path=args.desired, mode=args.mode, dry_run=args.dry_run)

    if args.dry_run:
        for result in results:
            print(f"  PLAN  {result.params['change']:<6} {result.action}")
        print(f"\n{len(results)} planned step(s), nothing applied.")
        return 0

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


def cmd_boot_ok(_args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas.config import ConfigurationManager
        from freedomnas.bootstate import confirm_boot
    except ImportError:
        from engine import get_engine  # type: ignore
        from config import ConfigurationManager  # type: ignore
        from bootstate import confirm_boot  # type: ignore

    try:
        engine = get_engine()
        config = ConfigurationManager()
    except Exception as exc:
        print(f"ERROR: could not initialise engine: {exc}", file=sys.stderr)
        return 1
    confirm_boot(engine, config)
    return 0


def cmd_serve(_args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas.config import ConfigurationManager
        from freedomnas.orchestrator import Orchestrator
        from freedomnas.module_loader import ModuleLoader, ModuleLoaderException
    except ImportError:
        from engine import get_engine  # type: ignore
        from config import ConfigurationManager  # type: ignore
        from orchestrator import Orchestrator  # type: ignore
        from module_loader import ModuleLoader, ModuleLoaderException  # type: ignore

    try:
        engine = get_engine()
        config = ConfigurationManager()
        orchestrator = Orchestrator(engine, config)
        loader = ModuleLoader(engine, config)
    except Exception as exc:
        print(f"ERROR: could not initialise: {exc}", file=sys.stderr)
        return 1

    try:
        registered = loader.load_all(orchestrator)
    except ModuleLoaderException as exc:
        print(f"ERROR: module discovery failed: {exc}", file=sys.stderr)
        return 1

    if not registered:
        print("WARNING: no modules registered", file=sys.stderr)
    else:
        print(f"Registered modules: {', '.join(registered)}")

    orchestrator.start_all()
    print("Orchestrator started. Press Ctrl+C to stop.")

    stop_event = threading.Event()

    def _handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    stop_event.wait()
    print("\nStopping...", file=sys.stderr)
    orchestrator.stop_all()
    return 0


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
        choices=["declarative", "additive", "disaster_recovery"],
        default="declarative",
        help="declarative (default): make the system match exactly, removals included; "
             "additive: add and change only. disaster_recovery is the old name of declarative",
    )
    reconcile_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the steps without applying any",
    )
    reconcile_parser.add_argument(
        "--desired",
        default=None,
        help="Path to a desired-state YAML file (default: use the config.yaml shadow copy)",
    )
    sub.add_parser(
        "grow-root-pool",
        help="Extend the root pool's partition and the pool into the free space after it",
    )
    snapshot_parser = sub.add_parser(
        "snapshot-run",
        help="Take and prune the scheduled snapshots for one interval (called from cron)",
    )
    snapshot_parser.add_argument(
        "--interval",
        choices=["hourly", "daily", "weekly", "monthly"],
        required=True,
        help="Which retention interval to apply",
    )

    sub.add_parser(
        "shadow-sync",
        help="Mirror the configuration onto every other pool's .freedomnas dataset",
    )

    sub.add_parser(
        "state-boot",
        help="Mount the root pool's .freedomnas and restore or merge from the data pools (called at boot)",
    )

    tls_parser = sub.add_parser(
        "ensure-tls",
        help="Create the WebUI's self-signed certificate if it is not there yet",
    )
    tls_parser.add_argument(
        "--hostname", default="",
        help="Name to put in the certificate (default: the system hostname)",
    )
    path_parser = sub.add_parser("path", help="Print one of this edition's FreedomNAS directories")
    path_parser.add_argument("name", choices=["install-root", "config", "state", "tls", "keyslots", "setup-pending"])
    sub.add_parser(
        "boot-ok",
        help="Confirm the boot: ends a new kernel's trial before an automatic rollback",
    )
    sub.add_parser("serve", help="Load and start all modules under the Orchestrator, run until stopped")
    sub.add_parser("version", help="Print package version")

    args = parser.parse_args()

    dispatch = {
        "status":       cmd_status,
        "check":        cmd_check,
        "unlock-pools": cmd_unlock_pools,
        "grow-root-pool": cmd_grow_root_pool,
        "reconcile":    cmd_reconcile,
        "ensure-tls":   cmd_ensure_tls,
        "snapshot-run": cmd_snapshot_run,
        "shadow-sync":  cmd_shadow_sync,
        "state-boot":   cmd_state_boot,
        "path":         cmd_path,
        "boot-ok":      cmd_boot_ok,
        "serve":        cmd_serve,
        "version":      cmd_version,
    }

    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        return 0

    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
