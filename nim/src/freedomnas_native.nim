import std/[json, locks, tables]
import nimpy
import fnas/[engine, errors, osdetect, pyformat, pyyaml, shlexutil, yamlvalue]
import fnas/config as fnasconfig
import fnas/snapshots as fnassnapshots
import fnas/pools as fnaspools

## Nim identifiers ignore case and underscores, so every exported name here
## would collide with the camelCase proc it wraps: the calls are qualified
## with their module to stop each wrapper calling itself.

type PyThreadState = pointer

proc PyEval_SaveThread(): PyThreadState {.importc, cdecl.}
proc PyEval_RestoreThread(ts: PyThreadState) {.importc, cdecl.}

template withoutGil(body: untyped): untyped =
  ## Long-running syscalls must not freeze the WebUI's other threads.
  let threadState = PyEval_SaveThread()
  try:
    body
  finally:
    PyEval_RestoreThread(threadState)

var
  enginesLock: Lock
  engines: Table[int, Engine]
  nextHandle: int

initLock(enginesLock)

proc errorPayload(kind: ErrorKind, message: string): string =
  $(%*{"error": {"kind": $kind, "message": message}})

template guarded(body: untyped): string =
  try:
    body
  except FnasError as exc:
    errorPayload(exc.kind, exc.msg)
  except CatchableError as exc:
    errorPayload(ekEngine, exc.msg)

proc lookup(handle: int): Engine =
  withLock enginesLock:
    if handle notin engines:
      raiseFnas(ekEngine, "Unknown engine handle: " & $handle)
    result = engines[handle]

proc parseValues(valuesJson: string): (Table[string, string], Table[string, string]) =
  let node = parseJson(valuesJson)
  for key, value in node{"str"}.getFields:
    result[0][key] = value.getStr
  for key, value in node{"raw"}.getFields:
    result[1][key] = value.getStr

proc applyValues(strValues, rawValues: Table[string, string]): Table[string, string] =
  for key, value in strValues:
    result[key] = pyformat.sanitize(value)
  for key, value in rawValues:
    result[key] = value

proc resolve_os(content: string): string {.exportpy.} =
  guarded:
    let resolved = osdetect.resolveDistro(content)
    $(%*{"profile": resolved.profile, "warnings": resolved.warnings})

proc detect_init(): string {.exportpy.} =
  osdetect.detectInitSystem()

proc sanitize_value(value: string): string {.exportpy.} =
  pyformat.sanitize(value)

proc has_shell_metachar(command: string): bool {.exportpy.} =
  engine.containsShellMetachar(command)

proc shlex_split(command: string): string {.exportpy.} =
  guarded:
    $(%*{"argv": shlexutil.split(command)})

proc format_template(tpl, valuesJson: string): string {.exportpy.} =
  guarded:
    let (strValues, rawValues) = parseValues(valuesJson)
    $(%*{"value": pyformat.format(tpl, applyValues(strValues, rawValues))})

proc engine_create(profileDir, osType, initOverride: string, targetMode: bool,
                   root, chrootPrefix: string): string {.exportpy.} =
  guarded:
    let eng = engine.newEngine(profileDir, osType, initOverride, targetMode,
                               root, chrootPrefix)
    var handle: int
    withLock enginesLock:
      handle = nextHandle
      inc nextHandle
      engines[handle] = eng
    $(%*{
      "handle": handle,
      "os_type": eng.osType,
      "init_system": eng.initSystem,
      "root": eng.root,
      "chroot_prefix": eng.chrootPrefix,
      "warnings": eng.warnings,
      "profile": eng.profile,
    })

proc engine_release(handle: int) {.exportpy.} =
  withLock enginesLock:
    engines.del(handle)

proc engine_command(handle: int, category, action, valuesJson: string): string {.exportpy.} =
  guarded:
    let
      (strValues, rawValues) = parseValues(valuesJson)
      command = lookup(handle).getCommand(category, action, strValues, rawValues)
    $(%*{"is_sequence": command.isSequence, "commands": command.commands})

proc engine_run(handle: int, category, action, valuesJson: string,
                timeout: int, check: bool): string {.exportpy.} =
  guarded:
    let
      (strValues, rawValues) = parseValues(valuesJson)
      eng = lookup(handle)
    var output, error: string
    withoutGil:
      (output, error) = eng.execute(category, action, strValues, rawValues,
                                    timeout, check)
    $(%*{"stdout": output, "stderr": error})

proc engine_run_with_stdin(handle: int, category, action, valuesJson,
                           stdinData: string, timeout: int): string {.exportpy.} =
  guarded:
    let
      (strValues, rawValues) = parseValues(valuesJson)
      eng = lookup(handle)
    var output, error: string
    withoutGil:
      (output, error) = eng.executeWithStdin(category, action, stdinData,
                                             strValues, rawValues, timeout)
    $(%*{"stdout": output, "stderr": error})

proc engine_value(handle: int, category, key: string): string {.exportpy.} =
  guarded:
    $(%*{"value": lookup(handle).getProfileValue(category, key)})

proc engine_section(handle: int, category: string): string {.exportpy.} =
  guarded:
    $(%*{"value": lookup(handle).getProfileSection(category)})

proc engine_info(handle: int): string {.exportpy.} =
  guarded:
    $(%*{"value": lookup(handle).getOsInfo()})

proc engine_categories(handle: int): string {.exportpy.} =
  guarded:
    $(%*{"value": lookup(handle).listCategories()})

proc engine_actions(handle: int, category: string): string {.exportpy.} =
  guarded:
    $(%*{"value": lookup(handle).listActions(category)})

proc engine_invocation(handle: int, command: string): string {.exportpy.} =
  guarded:
    let invocation = lookup(handle).resolveInvocation(command)
    $(%*{"argv": invocation.argv, "use_shell": invocation.useShell})

# ---------------------------------------------------------------------------
# Configuration — YAML reading and writing, merge, validation
# ---------------------------------------------------------------------------

proc config_defaults(): string {.exportpy.} =
  guarded:
    $(%*{"value": toJson(fnasconfig.defaults())})

proc config_parse(text: string): string {.exportpy.} =
  guarded:
    $(%*{"value": toJson(pyyaml.load(text))})

proc config_dump(valueJson: string): string {.exportpy.} =
  guarded:
    $(%*{"value": pyyaml.dump(fromJson(parseJson(valueJson)))})

proc config_merge_defaults(valueJson: string): string {.exportpy.} =
  guarded:
    $(%*{"value": toJson(fnasconfig.mergeWithDefaults(fromJson(parseJson(valueJson))))})

proc config_validate(valueJson: string): string {.exportpy.} =
  guarded:
    fnasconfig.validate(fromJson(parseJson(valueJson)))
    $(%*{"value": true})

proc config_header(valueJson, now: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnasconfig.header(fromJson(parseJson(valueJson)), now)})

# ---------------------------------------------------------------------------
# Scheduled snapshots
# ---------------------------------------------------------------------------

proc snapshot_run(handle: int, policiesJson, interval, nowIso: string): string {.exportpy.} =
  guarded:
    let
      policies = parseJson(policiesJson)
      asOf = fnassnapshots.parseNow(nowIso)
      eng = lookup(handle)
    var results: JsonNode
    withoutGil:
      results = fnassnapshots.runInterval(eng, policies, interval, asOf)
    $(%*{"value": results})

# ---------------------------------------------------------------------------
# Pool composition — cache and log devices
# ---------------------------------------------------------------------------

proc toStringSeq(node: JsonNode): seq[string] =
  for item in node.items:
    result.add item.getStr

proc pool_attach_devices(handle: int, poolName, cacheJson, logJson: string): string {.exportpy.} =
  guarded:
    let
      cache = toStringSeq(parseJson(cacheJson))
      logs = toStringSeq(parseJson(logJson))
      eng = lookup(handle)
    var res: JsonNode
    withoutGil:
      res = fnaspools.attachDevices(eng, poolName, cache, logs)
    $(%*{"value": res})

proc pool_detach_device(handle: int, poolName, device: string): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    withoutGil:
      fnaspools.detachDevice(eng, poolName, device)
    $(%*{"value": true})
