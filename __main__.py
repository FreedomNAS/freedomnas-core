"""
python -m freedomnas — FreedomNAS Core diagnostic CLI.

Commands:
  status        Print OS info, init system, and loaded profile
  check         Validate profiles directory and config file
  unlock-pools  Unlock ZFS-encrypted pools (USB zero-touch, TTY passphrase fallback)
  reconcile     Apply configuration via the ReconciliationEngine
  serve         Check the modules until stopped: retried, then blocked (orchestrator.py)
  path          Print one of this edition's FreedomNAS directories
  version       Print package version
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import List


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


def _mirror_saves(engine, config) -> None:
    try:
        from freedomnas import shadow
    except ImportError:
        import shadow  # type: ignore
    config.on_save.append(shadow.mirror_after_save(engine))


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
        _mirror_saves(engine, config)
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
    if result.get("reason"):
        print(f"reason {result['reason']}")
    if result.get("network"):
        print(f"network {result['network']}")
    for pool in result["keep"]:
        print(f"keep {pool}")
    return 0


def cmd_restore_offline(args) -> int:
    try:
        from freedomnas.engine import get_engine
        from freedomnas import offline_restore
    except ImportError:
        from engine import get_engine  # type: ignore
        import offline_restore  # type: ignore

    try:
        result = offline_restore.restore(get_engine(), Path(args.source))
    except Exception as exc:
        print(f"ERROR: could not restore from {args.source}: {exc}", file=sys.stderr)
        return 1
    for name in result["identity"]:
        print(f"identity {name}")
    failed = 0
    for r in result["results"]:
        if r.target_state == "failed":
            failed += 1
            print(f"FAIL  {r.action} {r.params}: {r.error}", file=sys.stderr)
        else:
            print(f"{r.target_state.upper():<8}{r.action} {r.params}" + (f": {r.error}" if r.error else ""))
    return 1 if failed else 0


def _configured_hostname() -> str:
    try:
        from freedomnas.config import ConfigurationManager
    except ImportError:
        from config import ConfigurationManager  # type: ignore
    try:
        config = ConfigurationManager()
        return config.get_hostname() if config.is_initialized() else ""
    except Exception:
        return ""


def cmd_ensure_tls(args) -> int:
    try:
        from freedomnas import tls
        from freedomnas.engine import get_engine
    except ImportError:
        import tls  # type: ignore
        from engine import get_engine  # type: ignore

    try:
        addresses = tls.addresses(get_engine())
    except Exception as exc:
        print(f"WARNING: could not read the network addresses: {exc}", file=sys.stderr)
        addresses = []
    hostname = args.hostname or _configured_hostname()
    try:
        issued = tls.ensure_certificate(hostname, addresses, not args.names_only)
    except Exception as exc:
        print(f"ERROR: could not create the TLS certificate: {exc}", file=sys.stderr)
        return 1

    if issued:
        print(f"Issued the server certificate at {tls.cert_file()}")
    print(f"Certificate authority fingerprint (SHA-256): {tls.ca_fingerprint()}")
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
            "run":          paths.run_dir,
            "setup-pending": lambda: paths.config_dir() / "setup-pending",
            "restoring":    lambda: paths.state_dir() / "restoring.json",
        }[args.name]
        print(resolve())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_restore_begin(args) -> int:
    try:
        from freedomnas import restoring
    except ImportError:
        import restoring  # type: ignore
    try:
        restoring.begin(restoring.lock_file(), args.reason)
    except OSError as exc:
        print(f"ERROR: could not write the restore lock: {exc}", file=sys.stderr)
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
        _mirror_saves(engine, config)
        reconciler = ReconciliationEngine(engine, config)
    except Exception as exc:
        print(f"ERROR: could not initialise reconciler: {exc}", file=sys.stderr)
        return 1

    if args.progress and not args.dry_run:
        try:
            from freedomnas import restoring
        except ImportError:
            import restoring  # type: ignore
        reconciler.progress = restoring.Progress(restoring.lock_file(engine))

    results = reconciler.reconcile(desired_path=args.desired, mode=args.mode, dry_run=args.dry_run)

    if args.dry_run:
        for result in results:
            print(f"  PLAN  {result.params['change']:<6} {result.action}")
        print(f"\n{len(results)} planned step(s), nothing applied.")
        return 0

    failed = 0
    for result in results:
        marker = {"applied": "OK", "skipped": "SKIP", "failed": "FAIL", "warning": "WARN"}.get(result.target_state, "??")
        line = f"  {marker}  {result.action} {result.params}"
        if result.error:
            line += f" -- {result.error}"
        print(line, file=sys.stderr if result.target_state == "failed" else sys.stdout)
        if result.target_state == "failed":
            failed += 1

    warnings = sum(r.target_state == "warning" for r in results)
    print(f"\n{len(results)} action(s), {failed} failure(s)" + (f", {warnings} warning(s)." if warnings else "."))
    if reconciler.progress:
        reconciler.progress.finish([f"{r.action}: {r.error}" for r in results if r.target_state == "failed"])
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
        _mirror_saves(engine, config)
    except Exception as exc:
        print(f"ERROR: could not initialise engine: {exc}", file=sys.stderr)
        return 1
    confirm_boot(engine, config)
    try:
        from freedomnas import bootenv
    except ImportError:
        import bootenv  # type: ignore
    try:
        bootenv.confirm_boot(engine)
    except Exception as exc:
        print(f"WARNING: could not confirm the boot environment: {exc}", file=sys.stderr)
    return 0


SERVE_RESTART = 75


def _module_dirs(loader) -> List[str]:
    try:
        return sorted(str(m["_dir"]) for m in loader.discover())
    except Exception:
        return []


def _mtime(path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def cmd_serve(_args) -> int:
    """
    Check the modules until stopped (see orchestrator.py). Exits with
    SERVE_RESTART when the installed modules change, so supervisor.sh starts
    it again with the new set; a second serve refuses to start.
    """
    import fcntl
    import time
    try:
        from freedomnas.engine import get_engine
        from freedomnas.config import ConfigurationManager
        from freedomnas.orchestrator import Orchestrator
        from freedomnas.module_loader import ModuleLoader, ModuleLoaderException
        from freedomnas import paths
    except ImportError:
        from engine import get_engine  # type: ignore
        from config import ConfigurationManager  # type: ignore
        from orchestrator import Orchestrator  # type: ignore
        from module_loader import ModuleLoader, ModuleLoaderException  # type: ignore
        import paths  # type: ignore

    try:
        engine = get_engine()
        config = ConfigurationManager()
        _mirror_saves(engine, config)
        run = paths.run_dir(engine)
        (run / "module-restart").mkdir(parents=True, exist_ok=True)
        orchestrator = Orchestrator(engine, config, run_dir=run)
        loader = ModuleLoader(engine, config)
    except Exception as exc:
        print(f"ERROR: could not initialise: {exc}", file=sys.stderr)
        return 1

    lock = open(run / "serve.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("ERROR: another serve is already checking the modules", file=sys.stderr)
        return 1

    try:
        registered = loader.load_all(orchestrator)
    except ModuleLoaderException as exc:
        print(f"ERROR: module discovery failed: {exc}", file=sys.stderr)
        return 1
    for module_id, reason in sorted(loader.failed.items()):
        print(f"ERROR: module {module_id} not loaded: {reason}", file=sys.stderr)
    print(f"Checking modules: {', '.join(registered) or 'none'}")

    installed = _module_dirs(loader)
    config_mtime = _mtime(config.config_path)
    orchestrator.keep_blocked()
    orchestrator.start_all()

    stop_event = threading.Event()

    def _handle_signal(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    code = 0
    last_write = 0.0
    while not stop_event.is_set():
        if _module_dirs(loader) != installed:
            print("The installed modules changed: starting again", file=sys.stderr)
            code = SERVE_RESTART
            break
        mtime = _mtime(config.config_path)
        if mtime != config_mtime:
            config_mtime = mtime
            try:
                config.load()
            except Exception as exc:
                print(f"WARNING: could not read the configuration again: {exc}", file=sys.stderr)
        changed = orchestrator.take_restart_requests() + orchestrator.check_due()
        if changed or time.monotonic() - last_write >= 10:
            try:
                orchestrator.write()
                last_write = time.monotonic()
            except OSError as exc:
                print(f"WARNING: could not write the modules' health: {exc}", file=sys.stderr)
        stop_event.wait(1)

    orchestrator.stop_all()
    return code


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
        "--progress",
        action="store_true",
        help="Write each step into the restore lock (<state>/restoring.json) for the WebUI's restore page",
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
        help="Create FreedomNAS's certificate authority and (re)issue the WebUI's certificate for this host's names and addresses",
    )
    tls_parser.add_argument(
        "--hostname", default="",
        help="Name to put in the certificate (default: the configured hostname, else the system's)",
    )
    tls_parser.add_argument(
        "--names-only", action="store_true",
        help="Issue the certificate again only for a missing name, not for a new address",
    )
    path_parser = sub.add_parser("path", help="Print one of this edition's FreedomNAS directories")
    path_parser.add_argument("name", choices=["install-root", "config", "state", "tls", "keyslots", "setup-pending",
                                              "restoring", "run"])
    offline_parser = sub.add_parser(
        "restore-offline",
        help="Before the first boot (an installer's chroot): put back the identity and install the modules "
             "of the configuration copied into SOURCE",
    )
    offline_parser.add_argument("--from", dest="source", required=True,
                                help="Directory holding config.yaml and identity/, as a data pool's .freedomnas")
    begin_parser = sub.add_parser(
        "restore-begin",
        help="Put the restore lock in place: the WebUI serves only the restore page until it is gone",
    )
    begin_parser.add_argument("--reason", default="", help="Why (restored, reinitialized, interrupted)")
    sub.add_parser(
        "boot-ok",
        help="Confirm the boot: ends a new kernel's trial before an automatic rollback",
    )
    sub.add_parser("serve", help="Check the modules until stopped: retried, then blocked")
    sub.add_parser("version", help="Print package version")

    args = parser.parse_args()

    dispatch = {
        "status":       cmd_status,
        "check":        cmd_check,
        "unlock-pools": cmd_unlock_pools,
        "grow-root-pool": cmd_grow_root_pool,
        "reconcile":    cmd_reconcile,
        "restore-begin": cmd_restore_begin,
        "restore-offline": cmd_restore_offline,
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
