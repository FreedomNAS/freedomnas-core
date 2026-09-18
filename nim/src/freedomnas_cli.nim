## freedomnas — the native CLI.
##
## It answers what the engine and the configuration can answer without an
## interpreter: which edition and init system were detected, whether the
## profiles load, what config.yaml holds, and scheduled snapshots. The
## commands that still need the keyslots or the module runtime — reconcile,
## unlock-pools, serve — live in `python -m freedomnas`.

import std/[json, os, parseopt, strutils, tables, times]
import fnas/[engine, errors, osdetect, pyyaml, yamlvalue]
import fnas/config as fnasconfig
import fnas/snapshots as fnassnapshots
import fnas/shadow as fnasshadow

const
  ProfileCandidates = [
    "/usr/lib/freedomnas/profiles",
    "freedomnas-profiles",
    "../freedomnas-profiles",
    "/opt/freedomnas/lib/os_profiles",
  ]
  Usage = """
freedomnas — FreedomNAS core engine

Usage:
  freedomnas status  [--profile-dir DIR] [--os EDITION]
  freedomnas check   [--profile-dir DIR] [--os EDITION]
  freedomnas config  [show|check|path] [--config FILE]
  freedomnas snapshot-run <hourly|daily|weekly|monthly> [--profile-dir DIR] [--os EDITION] [--config FILE]
  freedomnas shadow-sync [--profile-dir DIR] [--os EDITION] [--config FILE]
  freedomnas version

Environment:
  FREEDOMNAS_PROFILES   profiles directory, overriding the search path
  FREEDOMNAS_CONFIG_FILE   config.yaml path, overriding the search path
"""

proc profileDir(explicit: string): string =
  if explicit.len > 0:
    return explicit
  let fromEnv = getEnv("FREEDOMNAS_PROFILES")
  if fromEnv.len > 0:
    return fromEnv
  for candidate in ProfileCandidates:
    if dirExists(candidate):
      return candidate
  raiseFnas(ekOsNotSupported,
    "Could not find OS profiles directory. Searched: " &
    ProfileCandidates.join(", "))

proc detectOs(): string =
  if hostOS == "freebsd":
    return "freebsd"
  if not fileExists("/etc/os-release"):
    raiseFnas(ekOsNotSupported,
      "/etc/os-release not found. Cannot detect Linux distribution.")
  osdetect.resolveDistro(readFile("/etc/os-release")).profile

proc openEngine(dir, osOverride: string): Engine =
  let osType = if osOverride.len > 0: osOverride else: detectOs()
  engine.newEngine(profileDir(dir), osType, "", false, "", "")

proc cmdStatus(dir, osOverride: string): int =
  let
    eng = openEngine(dir, osOverride)
    info = eng.getOsInfo()
  var node = newJObject()
  for key in ["os_name", "os_family", "os_version", "init_system", "detected_type"]:
    node[key] = newJString(info.getOrDefault(key))
  echo node.pretty(2)
  0

proc cmdCheck(dir, osOverride: string): int =
  let eng = openEngine(dir, osOverride)
  echo "  OK  Profile loaded: ", eng.getOsInfo()["os_name"],
       " (", eng.initSystem, ")"
  echo "  OK  Profiles directory: ", eng.profileDir
  echo "  OK  Categories: ", eng.listCategories().len
  if "services" notin eng.profile:
    echo "  !!  No 'services' section — service commands will fail"
    return 1
  0

proc configPath(explicit: string): string =
  if explicit.len > 0:
    return explicit
  let fromEnv = getEnv("FREEDOMNAS_CONFIG_FILE")
  if fromEnv.len > 0:
    return fromEnv
  fnasconfig.findConfigPath()

proc cmdConfig(action, explicitPath: string): int =
  let path = configPath(explicitPath)
  case action
  of "", "show":
    stdout.write dump(fnasconfig.loadFile(path))
    0
  of "path":
    echo path
    0
  of "check":
    let config = fnasconfig.loadFile(path)
    echo "  OK  Configuration: ", path
    let hostname = config.getPath("system.hostname")
    echo "  OK  Hostname: ", (if hostname == nil: "(unset)" else: hostname.strVal)
    let initialized = config.getPath("system.initialized")
    echo "  OK  Initialized: ",
      (if initialized != nil and initialized.kind == vBool and initialized.boolVal:
         "yes" else: "no")
    0
  else:
    stderr.writeLine "Unknown config action: " & action
    2

proc cmdSnapshotRun(dir, osOverride, explicitConfigPath, interval: string): int =
  if interval.len == 0:
    stderr.writeLine "Usage: freedomnas snapshot-run <hourly|daily|weekly|monthly>"
    return 2

  let
    eng = openEngine(dir, osOverride)
    config = fnasconfig.loadFile(configPath(explicitConfigPath))
    policies = toJson(config.getPath("snapshots.policies"))
    results = fnassnapshots.runInterval(eng, policies, interval, times.now())

  if results.len == 0:
    echo "No dataset asks for ", interval, " snapshots."
    return 0

  var failed = 0
  for r in results:
    if r["error"].kind != JNull:
      inc failed
      stderr.writeLine "FAIL  " & r["dataset"].getStr & ": " & r["error"].getStr
      continue
    let destroyed = r["destroyed"].len
    let pruned = if destroyed > 0: ", pruned " & $destroyed else: ""
    echo "OK  ", r["created"].getStr, pruned

  if failed > 0: 1 else: 0

proc cmdShadowSync(dir, osOverride, explicitConfigPath: string): int =
  let
    eng = openEngine(dir, osOverride)
    path = configPath(explicitConfigPath)
    results = fnasshadow.syncAll(eng, path)

  if results.len == 0:
    echo "Nothing to mirror."
    return 0

  var failed = 0
  for r in results:
    if r["error"].kind != JNull:
      inc failed
      stderr.writeLine "FAIL  " & r["dataset"].getStr & ": " & r["error"].getStr
      continue
    echo "OK  ", r["dataset"].getStr

  if failed > 0: 1 else: 0

proc main(): int =
  var
    command = ""
    action = ""
    dir = ""
    osOverride = ""
    configFile = ""
    parser = initOptParser()

  var pending = ""

  for kind, key, value in parser.getopt():
    case kind
    of cmdArgument:
      if pending.len > 0:
        case pending
        of "profile-dir": dir = key
        of "os": osOverride = key
        else: configFile = key
        pending = ""
      elif command.len == 0:
        command = key
      elif action.len == 0:
        action = key
    of cmdLongOption, cmdShortOption:
      case key
      of "profile-dir", "os", "config":
        # parseopt only fills `value` for --opt:v and --opt=v; the
        # space-separated form arrives as the next argument.
        if value.len > 0:
          case key
          of "profile-dir": dir = value
          of "os": osOverride = value
          else: configFile = value
        else:
          pending = key
      of "help", "h":
        echo Usage
        return 0
      else:
        stderr.writeLine "Unknown option: " & key
        return 2
    of cmdEnd: discard

  if pending.len > 0:
    stderr.writeLine "Missing value for --" & pending
    return 2

  try:
    case command
    of "status": result = cmdStatus(dir, osOverride)
    of "check": result = cmdCheck(dir, osOverride)
    of "config": result = cmdConfig(action, configFile)
    of "snapshot-run": result = cmdSnapshotRun(dir, osOverride, configFile, action)
    of "shadow-sync": result = cmdShadowSync(dir, osOverride, configFile)
    of "version": echo "freedomnas-core 4.0.0 (native)"
    of "": echo Usage; result = 2
    else:
      stderr.writeLine "Unknown command: " & command
      result = 2
  except FnasError as exc:
    stderr.writeLine "ERROR: " & exc.msg
    result = 1

when isMainModule:
  quit(main())
