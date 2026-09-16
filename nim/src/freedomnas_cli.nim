## freedomnas — the native CLI.
##
## Phase 1 covers what the engine alone can answer: which edition and init
## system were detected, and whether the profiles load. The commands that
## need config.yaml or the keyslots (reconcile, unlock-pools, serve) still
## live in `python -m freedomnas`.

import std/[json, os, parseopt, strutils, tables]
import fnas/[engine, errors, osdetect]

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
  freedomnas status [--profile-dir DIR] [--os EDITION]
  freedomnas check  [--profile-dir DIR] [--os EDITION]
  freedomnas version

Environment:
  FREEDOMNAS_PROFILES   profiles directory, overriding the search path
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

proc main(): int =
  var
    command = ""
    dir = ""
    osOverride = ""
    parser = initOptParser()

  var pending = ""

  for kind, key, value in parser.getopt():
    case kind
    of cmdArgument:
      if pending.len > 0:
        if pending == "profile-dir": dir = key else: osOverride = key
        pending = ""
      elif command.len == 0:
        command = key
    of cmdLongOption, cmdShortOption:
      case key
      of "profile-dir", "os":
        # parseopt only fills `value` for --opt:v and --opt=v; the
        # space-separated form arrives as the next argument.
        if value.len > 0:
          if key == "profile-dir": dir = value else: osOverride = value
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
