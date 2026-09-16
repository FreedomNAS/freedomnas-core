## The configuration store: defaults, merge, validation, atomic save.
##
## Key order is part of the file, so the defaults below are written in the
## same order as config.py's DEFAULT_CONFIG — a fresh config.yaml has to
## come out identical whichever engine wrote it.

import std/[os, strutils]
import ./errors
import ./pyyaml
import ./yamlvalue

const
  DefaultConfigPath* = "/etc/freedomnas/state/config.yaml"
  BackupConfigPath* = "/etc/freedomnas/config.yaml"
  SnapshotIntervals* = ["hourly", "daily", "weekly", "monthly"]
  RequiredSections = ["system", "network", "users", "shares", "storage", "updates"]

proc pair(key: string, value: Val): (string, Val) = (key, value)

proc defaults*(): Val =
  result = newMap()

  var system = newMap()
  system.pairs.add pair("hostname", newStr("freedomnas"))
  system.pairs.add pair("timezone", newStr("UTC"))
  system.pairs.add pair("language", newStr("en_US.UTF-8"))
  system.pairs.add pair("initialized", newBool(false))
  system.pairs.add pair("initialized_at", newNull())
  system.pairs.add pair("initialized_by", newNull())
  system.pairs.add pair("edition", newNull())
  system.pairs.add pair("root_filesystem", newNull())
  result.pairs.add pair("system", system)

  var network = newMap()
  network.pairs.add pair("interfaces", newSeq())
  network.pairs.add pair("interfaces_mapping", newMap())
  result.pairs.add pair("network", network)

  result.pairs.add pair("users", newSeq())

  var shares = newMap()
  shares.pairs.add pair("smb", newSeq())
  shares.pairs.add pair("nfs", newSeq())
  result.pairs.add pair("shares", shares)

  var storage = newMap()
  storage.pairs.add pair("pools", newSeq())
  storage.pairs.add pair("boot_pool", newNull())
  result.pairs.add pair("storage", storage)

  var snapshots = newMap()
  snapshots.pairs.add pair("root", newSeq())
  snapshots.pairs.add pair("boot", newSeq())
  snapshots.pairs.add pair("policies", newSeq())
  result.pairs.add pair("snapshots", snapshots)

  var topgrade = newMap()
  topgrade.pairs.add pair("configured", newBool(false))
  topgrade.pairs.add pair("auto_snapshot", newBool(true))
  topgrade.pairs.add pair("enabled_steps", newSeq())
  var updates = newMap()
  updates.pairs.add pair("topgrade", topgrade)
  result.pairs.add pair("updates", updates)

  var reporting = newMap()
  reporting.pairs.add pair("enabled", newBool(false))
  result.pairs.add pair("reporting", reporting)

  result.pairs.add pair("installed_features", newSeq())

proc deepMerge(base, override: Val): Val =
  result = base
  for (key, value) in override.pairs:
    let existing = result.get(key)
    if existing != nil and existing.kind == vMap and value.kind == vMap:
      result[key] = deepMerge(existing, value)
    else:
      result[key] = copyVal(value)

proc mergeWithDefaults*(loaded: Val): Val =
  let merged = defaults()
  if loaded == nil:
    return merged
  if loaded.kind != vMap:
    # config.py reaches the same dead end through a Python AttributeError
    # on the non-dict it tried to merge; only the wording differs.
    raiseFnas(ekConfig,
      "Error loading configuration: the top level of config.yaml must be a " &
      "mapping, got " & typeName(loaded))
  deepMerge(merged, loaded)

proc validate*(config: Val) =
  if config == nil or config.kind != vMap:
    raiseFnas(ekConfig, "Missing required configuration section: 'system'")

  for section in RequiredSections:
    if config.get(section) == nil:
      raiseFnas(ekConfig, "Missing required configuration section: '" & section & "'")

  for (key, expected) in [("users", vSeq), ("shares", vMap), ("storage", vMap),
                          ("system", vMap), ("network", vMap), ("updates", vMap)]:
    let value = config.get(key)
    if value == nil or value.kind != expected:
      raiseFnas(ekConfig,
        "Configuration section '" & key & "' must be " &
        (if expected == vSeq: "list" else: "dict") &
        ", got " & typeName(value))

  proc requireKind(section, key: string, kind: ValKind, message: string) =
    let value = config[section].get(key)
    if value == nil or value.kind != kind:
      raiseFnas(ekConfig, message)

  requireKind("shares", "smb", vSeq, "shares.smb must be a list")
  requireKind("shares", "nfs", vSeq, "shares.nfs must be a list")
  requireKind("storage", "pools", vSeq, "storage.pools must be a list")
  requireKind("network", "interfaces", vSeq, "network.interfaces must be a list")
  requireKind("network", "interfaces_mapping", vMap,
              "network.interfaces_mapping must be a dict")
  requireKind("system", "initialized", vBool, "system.initialized must be a boolean")

proc getPath*(config: Val, key: string): Val =
  ## Dot-notation lookup; nil when any step is missing.
  result = config
  for part in key.split('.'):
    if result == nil or result.kind != vMap:
      return nil
    result = result.get(part)

proc setPath*(config: Val, key: string, value: Val) =
  var node = config
  let parts = key.split('.')
  for part in parts[0 ..< parts.len - 1]:
    var child = node.get(part)
    if child == nil or child.kind != vMap:
      child = newMap()
      node[part] = child
    node = child
  node[parts[^1]] = value

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

proc header*(config: Val, now: string): string =
  let
    editionVal = config.getPath("system.edition")
    edition = if editionVal == nil or editionVal.kind != vStr: "not set"
              else: editionVal.strVal
    hostnameVal = config.getPath("system.hostname")
    hostname = if hostnameVal == nil: "" else: hostnameVal.strVal
    initializedVal = config.getPath("system.initialized")
    initialized = if initializedVal != nil and initializedVal.kind == vBool and
                     initializedVal.boolVal: "yes" else: "no"
  "# FreedomNAS Configuration\n" &
  "# Last modified: " & now & "\n" &
  "# Edition: " & edition & "\n" &
  "# Hostname: " & hostname & "\n" &
  "# Initialized: " & initialized & "\n" &
  "# DO NOT EDIT MANUALLY unless you know what you're doing!\n" &
  "# Use the WebUI or API to make configuration changes.\n" &
  "# Manual edits may be overwritten by system updates.\n" &
  "---\n"

proc findConfigPath*(): string =
  if fileExists(DefaultConfigPath):
    return DefaultConfigPath
  if fileExists(BackupConfigPath):
    return BackupConfigPath
  DefaultConfigPath

proc loadFile*(path: string): Val =
  ## Defaults when the file is absent, merged and validated otherwise.
  if not fileExists(path):
    result = defaults()
    validate(result)
    return
  var text: string
  try:
    text = readFile(path)
  except IOError as exc:
    raiseFnas(ekConfig, "Error loading configuration: " & exc.msg)
  let loaded = pyyaml.load(text)
  result = mergeWithDefaults(loaded)
  validate(result)

proc saveFile*(config: Val, path, now: string) =
  ## Write, back up the old file, then rename into place — and put the
  ## backup back if anything fails, exactly as config.py does.
  validate(config)
  let
    parent = parentDir(path)
    tempPath = path.changeFileExt("yaml.tmp")
    backupPath = path.changeFileExt("yaml.backup")
  try:
    if parent.len > 0:
      createDir(parent)
    writeFile(tempPath, header(config, now) & dump(config))
    setFilePermissions(tempPath, {fpUserRead, fpUserWrite})
    if fileExists(path):
      moveFile(path, backupPath)
    moveFile(tempPath, path)
  except CatchableError as exc:
    if fileExists(backupPath) and not fileExists(path):
      moveFile(backupPath, path)
    raiseFnas(ekConfig, "Error saving configuration: " & exc.msg)
