import std/[algorithm, json, os, strutils]
import ./errors
import ./osdetect

const ReservedProfileDirs = ["os", "init", "packages"]

type LoadedProfile* = object
  profile*: JsonNode
  initSystem*: string
  warnings*: seq[string]

proc readJson(path, context: string): JsonNode =
  try:
    result = parseJson(readFile(path))
  except JsonParsingError as exc:
    raiseFnas(ekEngine, "Invalid JSON in " & context & " " & path & ": " & exc.msg)
  except IOError as exc:
    raiseFnas(ekEngine, "Error loading profile " & path & ": " & exc.msg)

proc mergeFragments(profileDir, osType: string, profile: JsonNode) =
  var dirs: seq[string]
  for kind, path in walkDir(profileDir):
    if kind in {pcDir, pcLinkToDir}:
      dirs.add lastPathPart(path)
  sort(dirs)
  for name in dirs:
    if name in ReservedProfileDirs:
      continue
    let fragmentPath = profileDir / name / (name & "-" & osType & ".json")
    if not fileExists(fragmentPath):
      continue
    let fragment = readJson(fragmentPath, "profile fragment")
    for key, value in fragment:
      profile[key] = value

proc load*(profileDir, osType, initOverride: string, targetMode: bool): LoadedProfile =
  let profilePath = profileDir / "os" / ("os-" & osType & ".json")
  if not fileExists(profilePath):
    var supported: seq[string]
    for name in SupportedOs:
      supported.add "os-" & name & ".json"
    raiseFnas(ekOsNotSupported,
      "OS profile not found: " & profilePath & "\nSupported profiles: " &
      supported.join(", "))

  result.profile = readJson(profilePath, "profile")
  mergeFragments(profileDir, osType, result.profile)

  if initOverride.len > 0:
    result.initSystem = initOverride
  elif targetMode:
    result.initSystem = result.profile{"init_system"}.getStr("systemd")
  else:
    result.initSystem = detectInitSystem()
    if result.initSystem.len == 0:
      let declared = result.profile{"init_system"}.getStr("")
      if declared.len > 0:
        result.initSystem = declared
      else:
        result.warnings.add "Could not detect init system; defaulting to 'systemd'"
        result.initSystem = "systemd"

  let initPath = profileDir / "init" / ("init-" & result.initSystem & ".json")
  if fileExists(initPath):
    let initProfile = readJson(initPath, "init profile")
    if "services" notin initProfile:
      raiseFnas(ekKey, "services")
    result.profile["services"] = initProfile["services"]
    result.warnings.add "Merged init profile '" & result.initSystem & "' into OS profile"
  elif "services" in result.profile:
    result.warnings.add "WARNING: OS profile '" & osType &
      "' contains a 'services' section. This is deprecated. Move service " &
      "commands to init/init-" & result.initSystem & ".json"
  else:
    result.warnings.add "No init profile found for '" & result.initSystem &
      "' and OS profile has no 'services' section. " &
      "engine.execute('services', ...) will fail."
