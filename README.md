# FreedomNAS Core Engine

Il cuore universale di FreedomNAS - il motore di traduzione OS-agnostico che permette al middleware di funzionare su Debian, FreeBSD e altre distribuzioni.

## Concetto Chiave: JSON Translator Layer

Invece di avere codice specifico per ogni OS sparso nel codebase, FreedomNAS Core implementa un **singolo strato di traduzione** che converte azioni astratte in comandi OS-specifici.

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

## File Componenti

- **engine.py**: Il motore principale
- **config.py**: Gestione della configurazione persistente
- **os_profiles/**: Directory contente i profili JSON per ogni OS

## Uso Veloce

### Ottenere un Comando (senza eseguire)

```python
from freedomnas_core.engine import FreedomNASEngine

engine = FreedomNASEngine()

# Ottenere il comando nativo
cmd = engine.get_command("services", "restart", service_name="samba")
print(cmd)  # Output: systemctl restart samba (su Debian)
```

### Eseguire un Comando

```python
# Eseguire direttamente
stdout, stderr = engine.execute("services", "restart", service_name="samba")

# Con parametri customizzati
stdout, stderr = engine.execute(
    "packages", 
    "install", 
    package_name="vim",
    timeout=60
)
```

### Operazioni ZFS

```python
# Creare un pool ZFS
stdout, stderr = engine.execute(
    "zfs",
    "pool_create",
    pool_name="datapool",
    vdev_type="raidz2",
    ashift=12,
    compression="lz4",
    devices="/dev/sda /dev/sdb /dev/sdc"
)

# Lista pool
cmd = engine.get_command("zfs", "pool_list")
print(cmd)
```

### Gestire i Servizi

```python
# Avviare un servizio
engine.execute("services", "start", service_name="samba")

# Abilitare al boot
engine.execute("services", "enable", service_name="samba")

# Controllare lo stato
stdout, stderr = engine.execute("services", "status", service_name="samba")
```

## Architettura

### Singleton Pattern

L'engine implementa il pattern Singleton per garantire che esista una sola istanza:

```python
# Tutte queste sono la stessa istanza
engine1 = FreedomNASEngine()
engine2 = FreedomNASEngine()
assert engine1 is engine2  # True!
```

### Thread-Safe

L'inizializzazione usa lock per sicurezza in ambienti multi-thread.

### Auto-Detect OS

L'engine rileva automaticamente l'OS durante l'inizializzazione:

```python
# Auto-detect (Debian, FreeBSD, Arch, ecc.)
engine = FreedomNASEngine()

# O forza un OS per testing
engine = FreedomNASEngine(os_override="freebsd")
```

## Profili OS

Ogni distribuzione ha il suo profilo JSON con la mappatura dei comandi:

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

### Categorie Supportate

- **users**: Gestione utenti (create, delete, add_to_group, ecc.)
- **services**: Gestione servizi (start, stop, restart, enable, disable, ecc.)
- **network**: Configurazione rete (interface, bridge, IP, hostname, ecc.)
- **packages**: Gestione pacchetti (install, remove, upgrade, ecc.)
- **zfs**: Operazioni ZFS (pool, dataset, snapshot, ecc.)
- **samba**: Configurazione SMB/CIFS
- **nfs**: Configurazione NFS
- **btrfs**: Operazioni Btrfs filesystem
- **grub_btrfs**: Boot con snapshot Btrfs
- **system**: Informazioni e configurazioni di sistema

## Gestione della Configurazione

La classe `ConfigurationManager` gestisce la configurazione persistente in YAML:

```python
from freedomnas_core.config import ConfigurationManager

config = ConfigurationManager()

# Leggere configurazione
hostname = config.get_hostname()
users = config.get_users()

# Modificare configurazione
config.set_hostname("new-hostname")
config.add_user({
    "username": "john",
    "uid": 1001,
    "groups": ["sudo"]
})
config.save()  # Salva atomicamente
```

La configurazione è memorizzata in:
1. Dataset ZFS: `/mnt/zpool/.freedomnas/config.yaml` (primaria)
2. Backup: `/etc/freedomnas/config.yaml` (fallback)

## Sicurezza

### Protezione Injection

I comandi sono eseguiti senza shell, prevenendo injection attacks:

```python
# Sicuro: i parametri non sono interpretati dalla shell
engine.execute("users", "create", username="attacker'; rm -rf /")
# Esegue: useradd -m -s /bin/bash attacker'; rm -rf /
# (come username letterale, non come codice shell)
```

### File di Configurazione

La configurazione è salvata con permessi `0o600` (lettura/scrittura solo owner):

```
-rw------- 1 root root config.yaml
```

### Backup Atomico

Le operazioni di salvataggio sono atomiche:
1. Scrivi su file temporaneo
2. Backup del file esistente
3. Rename atomico

Se qualcosa fallisce, il backup viene ripristinato automaticamente.

## Integrazione WebUI

La WebUI usa lo stesso engine:

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

## Integrazione Installer

L'installer TUI usa lo stesso engine per eseguire operazioni di sistema:

```python
from freedomnas_core.engine import FreedomNASEngine

class DiskInstaller:
    def __init__(self):
        self.engine = FreedomNASEngine()
    
    def partition_disk(self, disk):
        # Usa engine per partizioni
        cmd = self.engine.get_command("disk", "partition", disk=disk)
        self.engine.execute("disk", "partition", disk=disk)
```

## Debug e Logging

L'engine usa Python logging standard:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("FreedomNAS.Core")

engine = FreedomNASEngine()
# Vedrai messaggi di debug nell'output
```

## Aggiungere Nuovi Profili OS

Vedi [CONTRIBUTING.md](CONTRIBUTING.md) per una guida dettagliata su come aggiungere supporto per una nuova distribuzione.

## Gestione Errori

L'engine fornisce eccezioni specifiche:

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
    print(f"OS non supportato: {e}")
except Exception as e:
    print(f"Errore sconosciuto: {e}")

try:
    engine.execute("services", "restart", service_name="samba")
except CommandTranslationError:
    print("Comando non disponibile su questo OS")
except CommandExecutionError as e:
    print(f"Esecuzione comando fallita: {e}")
```

## Roadmap

- [ ] Supporto per più distribuzioni (CentOS, Alpine, ecc.)
- [ ] Caching dei comandi
- [ ] Gestione permessi elevated (sudo/doas)
- [ ] Output parsing strutturato
- [ ] Integrazione con audit logging
- [ ] Profili custom per installazioni speciali
