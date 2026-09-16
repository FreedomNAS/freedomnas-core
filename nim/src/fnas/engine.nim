import std/[json, options, sequtils, strutils, tables]
import ./errors
import ./exec
import ./pyformat
import ./profile as profileloader
import ./shlexutil

type
  Engine* = ref object
    osType*: string
    initSystem*: string
    profileDir*: string
    root*: string
    chrootPrefix*: string
    targetMode*: bool
    profile*: JsonNode
    warnings*: seq[string]

  Invocation* = object
    argv*: seq[string]
    command*: string
    useShell*: bool

  CommandTemplate* = object
    isSequence*: bool
    commands*: seq[string]

proc newEngine*(profileDir, osType, initOverride: string, targetMode: bool,
                root, chrootPrefixOverride: string): Engine =
  result = Engine(
    osType: osType,
    profileDir: profileDir,
    targetMode: targetMode,
    root: if root in ["", "/"]: "" else: root.strip(leading = false, chars = {'/'}),
  )
  let loaded = profileloader.load(profileDir, osType, initOverride, targetMode)
  result.profile = loaded.profile
  result.initSystem = loaded.initSystem
  result.warnings = loaded.warnings

  if result.root.len > 0:
    var prefixTemplate = chrootPrefixOverride
    if prefixTemplate.len == 0:
      prefixTemplate = result.profile{"chroot", "exec_prefix"}.getStr("")
    if prefixTemplate.len == 0:
      raiseFnas(ekTranslation,
        "No chroot.exec_prefix for '" & osType & "' — cannot execute " &
        "commands against target root " & result.root & ". Add it to " &
        "profiles/installer/installer-" & osType & ".json.")
    result.chrootPrefix = format(prefixTemplate, {"root": result.root}.toTable)

proc containsShellMetachar*(command: string): bool =
  for token in ["|", ">", "<", ";", "&", "$("]:
    if token in command:
      return true
  false

proc sectionKeys(node: JsonNode): seq[string] =
  if node != nil and node.kind == JObject:
    toSeq(node.keys)
  else:
    @[]

proc getCommand*(engine: Engine, category, action: string,
                 strValues, rawValues: Table[string, string]): CommandTemplate =
  let
    section = engine.profile{category}
    entry = if section != nil and section.kind == JObject: section{action} else: nil
  if entry == nil:
    raiseFnas(ekTranslation,
      "Command not found: " & category & "." & action &
      "\nAvailable actions in '" & category & "': " & sectionKeys(section).join(", "))

  var values: Table[string, string]
  for key, value in strValues:
    values[key] = sanitize(value)
  for key, value in rawValues:
    values[key] = value

  if entry.kind == JArray:
    result.isSequence = true
    for item in entry:
      result.commands.add format(item.getStr, values)
  else:
    result.commands.add format(entry.getStr, values)

proc getProfileValue*(engine: Engine, category, key: string): JsonNode =
  let section = engine.profile{category}
  let value = if section != nil and section.kind == JObject: section{key} else: nil
  if value == nil:
    raiseFnas(ekTranslation,
      "Profile value not found: " & category & "." & key &
      "\nAvailable keys in '" & category & "': " & sectionKeys(section).join(", "))
  value

proc getProfileSection*(engine: Engine, category: string): JsonNode =
  let section = engine.profile{category}
  if section != nil and section.kind == JObject: section else: newJObject()

proc getOsInfo*(engine: Engine): Table[string, string] =
  {
    "os_name": engine.profile{"os_name"}.getStr("Unknown"),
    "os_family": engine.profile{"os_family"}.getStr("Unknown"),
    "os_version": engine.profile{"os_version"}.getStr("Unknown"),
    "init_system": engine.initSystem,
    "detected_type": engine.osType,
  }.toTable

proc listCategories*(engine: Engine): seq[string] =
  for key in engine.profile.keys:
    if not key.startsWith("os_"):
      result.add key

proc listActions*(engine: Engine, category: string): seq[string] =
  sectionKeys(engine.profile{category})

proc resolveInvocation*(engine: Engine, command: string): Invocation =
  let needsShell = containsShellMetachar(command)
  result.command = command

  if engine.chrootPrefix.len == 0:
    if needsShell:
      result.useShell = true
      result.argv = @[command]
    else:
      result.argv = shlexutil.split(command)
    return

  let prefix = shlexutil.split(engine.chrootPrefix)
  result.argv =
    if needsShell: prefix & @["sh", "-c", command]
    else: prefix & shlexutil.split(command)

proc lastLines(text: string, count = 12): string =
  let lines = text.strip().splitLines()
  if lines.len <= count:
    lines.join("\n")
  else:
    (@["[...]"] & lines[^count .. ^1]).join("\n")

proc runSafe*(engine: Engine, command: string, timeout: int,
              check: bool): (string, string) =
  ## Everything that is not a timeout or a non-zero exit — a bad quote, a
  ## missing binary — reaches the caller as one execution error, the way
  ## the pure-Python engine's blanket `except Exception` does.
  try:
    let
      invocation = engine.resolveInvocation(command)
      argv = if invocation.useShell: @["/bin/sh", "-c", command] else: invocation.argv
      res = exec.run(argv, none(string), timeout)

    if res.timedOut:
      raiseFnas(ekExecution,
        "Command timed out after " & $timeout & "s: " & command)

    if check and res.exitCode != 0:
      raiseFnas(ekExecution,
        "Command failed with exit code " & $res.exitCode &
        "\nCommand: " & command &
        "\nStdout: " & lastLines(res.output) &
        "\nStderr: " & lastLines(res.error))

    result = (res.output.strip(), res.error.strip())
  except FnasError as exc:
    if exc.kind == ekExecution:
      raise
    raiseFnas(ekExecution,
      "Error executing command: " & command & "\nError: " & exc.msg)

proc execute*(engine: Engine, category, action: string,
              strValues, rawValues: Table[string, string],
              timeout: int, check: bool): (string, string) =
  let commands = engine.getCommand(category, action, strValues, rawValues)
  for command in commands.commands:
    result = engine.runSafe(command, timeout, check)

proc executeWithStdin*(engine: Engine, category, action, stdinData: string,
                       strValues, rawValues: Table[string, string],
                       timeout: int): (string, string) =
  let commands = engine.getCommand(category, action, strValues, rawValues)
  if commands.isSequence:
    raiseFnas(ekTranslation,
      category & "." & action & " is a command sequence — " &
      "execute_with_stdin requires a single command")

  let command = commands.commands[0]
  var argv = shlexutil.split(command)
  if engine.chrootPrefix.len > 0:
    argv = shlexutil.split(engine.chrootPrefix) & argv

  try:
    let res = exec.run(argv, some(stdinData), timeout)
    if res.timedOut:
      raiseFnas(ekExecution, "Command timed out after " & $timeout & "s: " & command)
    if res.exitCode != 0:
      raiseFnas(ekExecution,
        "Command failed with exit code " & $res.exitCode &
        "\nCommand: " & command &
        "\nStderr: " & res.error)
    result = (res.output.strip(), res.error.strip())
  except FnasError as exc:
    if exc.kind == ekExecution:
      raise
    raiseFnas(ekExecution,
      "Error executing command: " & command & "\nError: " & exc.msg)
