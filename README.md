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

- **engine.py**: The main engine
- **config.py**: Persistent configuration management
- **os_profiles/**: Directory containing the JSON profiles for each OS

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
- **services**: Service management (start, stop, restart, enable, disable, etc.)
- **network**: Network configuration (interface, bridge, IP, hostname, etc.)
- **packages**: Package management (install, remove, upgrade, etc.)
- **zfs**: ZFS operations (pool, dataset, snapshot, etc.)
- **samba**: SMB/CIFS configuration
- **nfs**: NFS configuration
- **btrfs**: Btrfs filesystem operations
- **grub_btrfs**: Boot with Btrfs snapshots
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
1. ZFS dataset: `/mnt/zpool/.freedomnas/config.yaml` (primary)
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

The TUI installer uses the same engine to run system operations:

```python
from freedomnas_core.engine import FreedomNASEngine

class DiskInstaller:
    def __init__(self):
        self.engine = FreedomNASEngine()
    
    def partition_disk(self, disk):
        # Use the engine for partitioning
        cmd = self.engine.get_command("disk", "partition", disk=disk)
        self.engine.execute("disk", "partition", disk=disk)
```

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
