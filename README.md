# FreedomNAS Core Engine

The universal heart of FreedomNAS — the OS-agnostic translation engine that lets the middleware run on Debian, FreeBSD, and other distributions.

## Key Concept: JSON Translator Layer

Instead of scattering OS-specific code throughout the codebase, FreedomNAS Core implements a **single translation layer** that converts abstract actions into OS-specific commands.

```
[WebUI/Installer] 
        ↓
engine.execute("services", "restart", service_name="samba")
        ↓
[FreedomNAS Engine - JSON Translator]
        ↓
Debian: systemctl restart samba
FreeBSD: service samba restart
Arch: systemctl restart samba
```

## Components

- **engine.py**: Backend selection — everything imports the engine from here
- **engine_py.py**: The engine in pure Python (default backend)
- **engine_native.py**: The engine in Nim, through a Python extension module
- **nim/**: Nim sources for the native engine and the `freedomnas` CLI
- **config.py**: Persistent configuration management — backend selection
- **config_py.py**: Configuration manager on PyYAML (default backend)
- **config_native.py**: Same manager with the YAML end in Nim
- **os_profiles/**: Directory containing the JSON profiles for each OS

### Two engines, one behaviour

The engine is being moved to Nim one piece at a time, so a build with no
Python runtime becomes possible at boot. Both implementations ship, and
`FREEDOMNAS_ENGINE` picks one:

| Value | Engine |
|-------|--------|
| `python` (default) | `engine_py.py` |
| `native` | `engine_native.py`, `ImportError` if the extension is missing |
| `auto` | native when built, Python otherwise |

`FREEDOMNAS_CONFIG` does the same for the configuration manager and, when
unset, follows `FREEDOMNAS_ENGINE` — so one variable moves the whole core
and the other pins the configuration back on its own.

They are not allowed to differ. `tests/test_engine_parity.py` runs every
command of every shipped profile through both engines and compares the
results, the exceptions and their messages; `tests/test_config_parity.py`
compares the **bytes** both managers write, because an appliance reads back
what the other one saved. Run the suite twice before changing either:

```bash
cd nim && nimble native && cd ..        # builds ../freedomnas_native.so
python3 -m pytest tests/ -q
FREEDOMNAS_ENGINE=native python3 -m pytest tests/ -q
```

Both `nimble native` and `nimble cli` build with `--tlsEmulation:off
--passC:"-ftls-model=global-dynamic"`. Without it, the first thread the
*host process* creates (not Nim — `asyncio.to_thread`, `threading.Thread`;
the WebUI uses this for `reconcile()`) to call into the native engine
segfaults, reliably, once the process has done enough allocator activity —
one scrypt-sized keyslot wrap (`keymgmt.py`, ~32 MiB) is enough on its own.
See [Core Engine](../freedomnas-docs/docs/architecture/engine.md#building-it)
for why.

`nimble cli` builds `nim/freedomnas`, the native CLI: `status`, `check`,
`config show|check|path`, `snapshot-run <interval>`, `shadow-sync` and
`version`, with no interpreter involved. What still needs Python is
everything built on the keyslots or the module runtime — `reconcile`,
`unlock-pools`, `serve` — through `python -m freedomnas`.

### More than two implementations, same pattern

Scheduled snapshots (`snapshots.py`), pool composition (`pools.py`), the
configuration's shadow copy (`shadow.py`), the reconciliation engine
(`reconcile.py`), encrypted-pool key management (`keymgmt.py`) and the
package manager (`package_manager.py`) follow the same split as the engine
and the configuration manager. Every native side but one calls the
engine's native handle directly — `engine._handle` — so none of those
makes sense without the native engine; all six are held to the
pure-Python behaviour by a parity test:

| Module | Backend switch | Pure Python | Native Python side | Nim logic | Parity test |
|--------|----------------|-------------|---------------------|-----------|-------------|
| Scheduled snapshots | `snapshots.py` (`FREEDOMNAS_SNAPSHOTS`) | `snapshots_py.py` | `snapshots_native.py` | `nim/src/fnas/snapshots.nim` | `tests/test_snapshots_parity.py` |
| Pool cache/log devices | `pools.py` (`FREEDOMNAS_POOLS`) | `pools_py.py` | `pools_native.py` | `nim/src/fnas/pools.nim` | `tests/test_pools_parity.py` |
| Configuration's shadow copy | `shadow.py` (`FREEDOMNAS_SHADOW`) | `shadow_py.py` | `shadow_native.py` | `nim/src/fnas/shadow.nim` | `tests/test_shadow_parity.py` |
| Reconciliation engine | `reconcile.py` (`FREEDOMNAS_RECONCILE`) | `reconcile_py.py` | `reconcile_native.py` | `nim/src/fnas/reconcile.nim` | `tests/test_reconcile_parity.py` |
| Encrypted-pool keyslots | `keymgmt.py` (`FREEDOMNAS_KEYMGMT`) | `keymgmt_py.py` | `keymgmt_native.py` | `nim/src/fnas/keymgmt.nim` | `tests/test_keymgmt_parity.py` |
| Package manager | `package_manager.py` (`FREEDOMNAS_PACKAGES`) | `package_manager_py.py` | `package_manager_native.py` | `nim/src/fnas/packages.nim` | `tests/test_package_manager_parity.py` |

Every one of these backend switches follows `FREEDOMNAS_ENGINE` when its
own variable is unset. Scheduled snapshots and the shadow copy are also
what make `freedomnas snapshot-run <interval>` and `freedomnas shadow-sync`
possible with no Python interpreter at all — reconcile and keymgmt have no
such native CLI command (both stay reachable only through
`python -m freedomnas reconcile` / `unlock-pools`, per the "still needs
Python" note above), but under `FREEDOMNAS_ENGINE=native` their own
logic — diff/apply, scrypt+AES-256-GCM wrapping, keyslot I/O, USB/TTY
unlock — runs in Nim too.

The package manager is the one exception to "every native side calls the
engine's handle": it never calls `engine.execute()` at all — MANIFEST.json
and category JSON reading is pure, read-only navigation — so it needs no
engine handle and works under either engine backend, or with none open
yet. Its parity test runs against the real `freedomnas-profiles/packages/`
tree rather than a throwaway fixture, for the same reason
`tests/test_package_manager.py`'s pure-Python baseline does: the manifest
and category shape is the contract being tested, not a stand-in for it.

Unlike the other three, `reconcile_native.py`'s public surface is narrower
than the reference: it implements `reconcile()` alone. `compute_diff` and
the `_do_*`/`_apply_*` helpers are pure-Python-only internals that nothing
outside `reconcile_py.py` calls directly (confirmed by grepping
freedomnas-webui and freedomnas-installer), so `tests/test_reconcile.py`
exercises them white-box against `reconcile_py` specifically, while
`tests/test_reconcile_parity.py` compares the two backends' `reconcile()`
output end to end.

`keymgmt.py` carries a stricter requirement than "behaves the same": a
keyslot file one backend writes has to open with the other, since a pool
created under one build and booted under the other is ordinary, not an
edge case. It wraps with two different, independent implementations of
the same public standards — Python's `hashlib.scrypt` + `cryptography`'s
`AESGCM` versus Nim's `nimcrypto` scrypt (RFC 7914) and AES-256-GCM
(NIST SP 800-38D) — cross-checked byte-for-byte in
`tests/test_keymgmt_parity.py`, including writing a keyslot with one
backend and reading it back with the other, and the TTY passphrase prompt
against a real pty.

## Quick Usage

### Getting a Command (without executing it)

```python
from freedomnas_core.engine import FreedomNASEngine

engine = FreedomNASEngine()

# Get the native command
cmd = engine.get_command("services", "restart", service_name="samba")
print(cmd)  # Output: systemctl restart samba (on Debian)
```

### Executing a Command

```python
# Execute directly
stdout, stderr = engine.execute("services", "restart", service_name="samba")

# With custom parameters
stdout, stderr = engine.execute(
    "packages", 
    "install", 
    package_name="vim",
    timeout=60
)
```

### ZFS Operations

```python
# Create a ZFS pool
stdout, stderr = engine.execute(
    "zfs",
    "pool_create",
    pool_name="datapool",
    vdev_type="raidz2",
    ashift=12,
    compression="lz4",
    devices="/dev/sda /dev/sdb /dev/sdc"
)

# List pools
cmd = engine.get_command("zfs", "pool_list")
print(cmd)
```

### Encrypted Pools (`keymgmt.py`)

Don't call `zfs.pool_create_encrypted`/`load_key_file` directly — use
`PoolProvisioner`/`PoolUnlocker`, which also manage the two independent
USB/passphrase keyslots (scrypt + AES-256-GCM) on top of the engine call:

```python
from freedomnas.keymgmt import PoolProvisioner, PoolUnlocker

PoolProvisioner(engine).create_encrypted_pool(
    pool_name="datapool",
    vdev_type="mirror",
    devices="/dev/sda /dev/sdb",
    master_passphrase="correct horse battery staple",  # >= 12 chars
    usb_devices=["/dev/sdc1"],   # already-mounted-and-writable USB stick(s)
)

# At boot: python -m freedomnas unlock-pools --tty /dev/console
locked = PoolUnlocker(engine).list_locked_pools()
```

See `ARCHITECTURE.md`'s "Encryption Key Management" section in the parent
repo for the full wrap/unwrap design and the TTY fail-open timeout.

### Managing Services

```python
# Start a service
engine.execute("services", "start", service_name="samba")

# Enable at boot
engine.execute("services", "enable", service_name="samba")

# Check status
stdout, stderr = engine.execute("services", "status", service_name="samba")
```

## Architecture

### Singleton Pattern

The engine implements the Singleton pattern to guarantee a single instance exists:

```python
# These are all the same instance
engine1 = FreedomNASEngine()
engine2 = FreedomNASEngine()
assert engine1 is engine2  # True!
```

### Thread-Safe

Initialization uses locking for safety in multi-threaded environments.

### Target Engines (installer only)

The singleton describes the machine the process runs on — right for the WebUI
and the modules, useless for an installer provisioning a *different* edition
into a chroot. `for_target()` returns a fresh, non-singleton engine bound to
another OS profile:

```python
# Install Void from a Debian live environment, into /mnt
target = FreedomNASEngine.for_target("void", root="/mnt")
target.execute("packages", "install", package_name="zfs")
# -> chroot /mnt xbps-install -y zfs
```

- `root=None` (or `"/"`) means "run on the host" — what the in-place install
  mode wants.
- With a `root`, every command is prefixed with the profile's
  `chroot.exec_prefix`, and a command needing a shell is handed to `sh -c`
  *inside* the chroot, never through the host's shell.
- The target's init system comes from its own profile declaration; a target
  engine never probes the running host for it.
- `get_engine()` and the singleton are untouched by any of this.

### Auto-Detect OS

The engine automatically detects the OS during initialization:

```python
# Auto-detect (Debian, FreeBSD, Arch, etc.)
engine = FreedomNASEngine()

# Or force a specific OS for testing
engine = FreedomNASEngine(os_override="freebsd")
```

## OS Profiles

Each distribution has its own JSON profile with the command mapping:

```json
{
  "os_name": "Debian",
  "os_family": "linux",
  "os_version": "12+",
  
  "services": {
    "start": "systemctl start {service_name}",
    "stop": "systemctl stop {service_name}",
    "restart": "systemctl restart {service_name}",
    ...
  },
  
  "packages": {
    "install": "apt install -y {package_name}",
    ...
  }
}
```

### Supported Categories

- **users**: User management (create, delete, add_to_group, etc.)
- **services**: Service management (start, stop, restart, enable, disable, etc.) — merged in from `init/{init_system}.json`, not the OS profile itself
- **network**: Network configuration (interface, bridge, IP, hostname, etc.)
- **packages**: Package management (install, remove, upgrade, etc.)
- **zfs**: ZFS operations (pool, dataset, snapshot, encrypted pools/keys, etc.)
- **media**: Removable-device list/mount/unmount, used by `keymgmt.py`'s USB zero-touch unlock
- **samba**: SMB/CIFS configuration
- **nfs**: NFS configuration
- **zfs_properties**: the ZFS properties each edition supports — a catalogue, not commands
- **system**: System information and configuration

## Configuration Management

The `ConfigurationManager` class handles persistent configuration in YAML:

```python
from freedomnas_core.config import ConfigurationManager

config = ConfigurationManager()

# Read configuration
hostname = config.get_hostname()
users = config.get_users()

# Modify configuration
config.set_hostname("new-hostname")
config.add_user({
    "username": "john",
    "uid": 1001,
    "groups": ["sudo"]
})
config.save()  # Saves atomically
```

The configuration is stored in:
1. ZFS dataset: `/etc/freedomnas/state/config.yaml` (primary — where `rpool/.freedomnas` mounts)
2. Backup: `/etc/freedomnas/config.yaml` (fallback)

## Security

### Injection Protection

Commands are executed without a shell, preventing injection attacks:

```python
# Safe: parameters are not interpreted by the shell
engine.execute("users", "create", username="attacker'; rm -rf /")
# Runs: useradd -m -s /bin/bash attacker'; rm -rf /
# (as a literal username, not as shell code)
```

### Configuration File

The configuration is saved with `0o600` permissions (owner read/write only):

```
-rw------- 1 root root config.yaml
```

### Atomic Backup

Save operations are atomic:
1. Write to a temporary file
2. Back up the existing file
3. Atomic rename

If anything fails, the backup is restored automatically.

## WebUI Integration

The WebUI uses the same engine:

```python
from freedomnas_core.engine import FreedomNASEngine

@app.post("/api/services/{service}/restart")
async def restart_service(service: str):
    engine = FreedomNASEngine()
    try:
        stdout, stderr = engine.execute(
            "services", 
            "restart", 
            service_name=service,
            timeout=30
        )
        return {"status": "success", "output": stdout}
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500
```

## Installer Integration

> Pre-built images from `freedomnas-image-builder` are the primary install
> path (see `IMAGE-BASED-INSTALLATION.md`). `freedomnas-installer` is the
> secondary one — for hardware that can't be flashed, console installs, and
> in-place installs onto a running system — and drives everything through
> this engine.

The installer runs *two* engines: the singleton for the machine it is
running on, and a target engine for the edition being installed:

```python
from freedomnas.engine import FreedomNASEngine, get_engine

host = get_engine()                                        # the live system
target = FreedomNASEngine.for_target("void", root="/mnt")  # what's being installed

# Partitioning uses the host's tools...
host.execute("partitioning", "create_esp", disk="/dev/sda", esp_size="512MiB")

# ...while anything inside the new system is transparently chrooted.
target.execute("packages", "install", package_name="zfs")
# -> chroot /mnt xbps-install -y zfs
```

`for_target(root=...)` reads the chroot invocation from the profile's
`chroot.exec_prefix`, so even that is not hardcoded. See the **Target
Engines** section above.

## Debugging and Logging

The engine uses standard Python logging:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("FreedomNAS.Core")

engine = FreedomNASEngine()
# You'll see debug messages in the output
```

## Adding New OS Profiles

See [CONTRIBUTING.md](CONTRIBUTING.md) for a detailed guide on adding support for a new distribution.

## Error Handling

The engine provides specific exceptions:

```python
from freedomnas_core.engine import (
    FreedomNASEngineException,
    OSNotSupportedException,
    CommandTranslationError,
    CommandExecutionError
)

try:
    engine = FreedomNASEngine()
except OSNotSupportedException as e:
    print(f"Unsupported OS: {e}")
except Exception as e:
    print(f"Unknown error: {e}")

try:
    engine.execute("services", "restart", service_name="samba")
except CommandTranslationError:
    print("Command not available on this OS")
except CommandExecutionError as e:
    print(f"Command execution failed: {e}")
```

## Roadmap

- [ ] Support for more distributions (CentOS, Alpine, etc.)
- [ ] Command caching
- [ ] Elevated permission handling (sudo/doas)
- [ ] Structured output parsing
- [ ] Audit logging integration
- [ ] Custom profiles for special installations
