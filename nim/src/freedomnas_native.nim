import std/[json, locks, tables]
import nimpy
import fnas/[engine, errors, osdetect, pyformat, pyyaml, shlexutil, yamlvalue]
import fnas/config as fnasconfig
import fnas/snapshots as fnassnapshots
import fnas/pools as fnaspools
import fnas/shadow as fnasshadow
import fnas/reconcile as fnasreconcile
import fnas/keymgmt as fnaskeymgmt
import fnas/packages as fnaspackages

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

  pkgManagersLock: Lock
  pkgManagers: Table[int, PackageManager]
  nextPkgHandle: int

initLock(enginesLock)
initLock(pkgManagersLock)

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

proc lookupPkg(handle: int): PackageManager =
  withLock pkgManagersLock:
    if handle notin pkgManagers:
      raiseFnas(ekEngine, "Unknown package manager handle: " & $handle)
    result = pkgManagers[handle]

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

# ---------------------------------------------------------------------------
# The configuration's shadow copy
# ---------------------------------------------------------------------------

proc shadow_active_dataset(handle: int): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    var active: string
    withoutGil:
      active = fnasshadow.activeDataset(eng)
    $(%*{"value": active})

proc shadow_ensure_dataset(handle: int, pool: string): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    var created: bool
    withoutGil:
      created = fnasshadow.ensureDataset(eng, pool)
    $(%*{"value": created})

proc shadow_sync(handle: int, configPath: string): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    var results: JsonNode
    withoutGil:
      results = fnasshadow.syncAll(eng, configPath)
    $(%*{"value": results})

# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

proc reconcile_run(handle: int, desiredPath, mode, stateJson,
                   nfsSharesJson: string): string {.exportpy.} =
  guarded:
    let
      state = parseJson(stateJson)
      nfsShares = parseJson(nfsSharesJson)
      eng = lookup(handle)
    var results: JsonNode
    withoutGil:
      results = fnasreconcile.reconcileRun(eng, desiredPath, mode, state, nfsShares)
    $(%*{"value": results})

# ---------------------------------------------------------------------------
# Encryption key management
# ---------------------------------------------------------------------------

proc keymgmt_wrap_key(rawKeyHex, secretHex: string): string {.exportpy.} =
  guarded:
    let
      rawKey = fnaskeymgmt.fromHexStrict(rawKeyHex)
      secret = fnaskeymgmt.fromHexStrict(secretHex)
    $(%*{"value": fnaskeymgmt.wrapKey(rawKey, secret)})

proc keymgmt_unwrap_key(slotJson, secretHex: string): string {.exportpy.} =
  guarded:
    let
      slot = parseJson(slotJson)
      secret = fnaskeymgmt.fromHexStrict(secretHex)
      rawKey = fnaskeymgmt.unwrapKey(slot, secret)
    $(%*{"value": fnaskeymgmt.toHexStr(rawKey)})

proc keymgmt_store_exists(poolName, keyslotDir: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaskeymgmt.newKeyslotStore(poolName, keyslotDir).storeExists()})

proc keymgmt_store_get_slot(poolName, keyslotDir, name: string): string {.exportpy.} =
  guarded:
    let slot = fnaskeymgmt.newKeyslotStore(poolName, keyslotDir).getSlot(name)
    $(%*{"value": (if slot == nil: newJNull() else: slot)})

proc keymgmt_store_set_slot(poolName, keyslotDir, name, slotJson: string): string {.exportpy.} =
  guarded:
    fnaskeymgmt.newKeyslotStore(poolName, keyslotDir).setSlot(name, parseJson(slotJson))
    $(%*{"value": true})

proc keymgmt_list_locked_pools(handle: int): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    var pools: seq[string]
    withoutGil:
      pools = fnaskeymgmt.listLockedPools(eng)
    $(%*{"value": pools})

proc keymgmt_try_usb_unlock(handle: int, poolName, keyslotDir, usbMountpoint,
                            runtimeKeyDir: string): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    var ok: bool
    withoutGil:
      ok = fnaskeymgmt.tryUsbUnlock(eng, poolName, keyslotDir, usbMountpoint, runtimeKeyDir)
    $(%*{"value": ok})

proc keymgmt_try_tty_unlock(handle: int, poolName, ttyPath: string, timeout: int,
                            keyslotDir, runtimeKeyDir: string): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    var ok: bool
    withoutGil:
      ok = fnaskeymgmt.tryTtyUnlock(eng, poolName, ttyPath, timeout, keyslotDir, runtimeKeyDir)
    $(%*{"value": ok})

proc keymgmt_create_encrypted_pool(handle: int, poolName, vdevType, devices, masterPassphrase,
                                   usbDevicesJson: string, ashift: int, compression, keyslotDir,
                                   runtimeKeyDir, usbMountpoint: string): string {.exportpy.} =
  guarded:
    let eng = lookup(handle)
    var usbDevices: seq[string]
    for item in parseJson(usbDevicesJson).items:
      usbDevices.add item.getStr
    withoutGil:
      fnaskeymgmt.createEncryptedPool(eng, poolName, vdevType, devices, masterPassphrase,
                                      usbDevices, ashift, compression,
                                      keyslotDir, runtimeKeyDir, usbMountpoint)
    $(%*{"value": true})

# ---------------------------------------------------------------------------
# Package manager — reads freedomnas-profiles/packages/
# ---------------------------------------------------------------------------

proc package_manager_create(packagesDir: string): string {.exportpy.} =
  guarded:
    let pm = fnaspackages.newPackageManager(packagesDir)
    var handle: int
    withLock pkgManagersLock:
      handle = nextPkgHandle
      inc nextPkgHandle
      pkgManagers[handle] = pm
    $(%*{"handle": handle})

proc package_manager_release(handle: int) {.exportpy.} =
  withLock pkgManagersLock:
    pkgManagers.del(handle)

proc package_manager_categories(handle: int): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getCategories(lookupPkg(handle))})

proc package_manager_category_info(handle: int, categoryId: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getCategoryInfo(lookupPkg(handle), categoryId)})

proc package_manager_packages(handle: int, categoryId, edition, initSystem: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getPackagesForOs(lookupPkg(handle), categoryId, edition, initSystem)})

proc package_manager_pkgbuilds(handle: int, categoryId, edition, initSystem: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getPkgbuildsForOs(lookupPkg(handle), categoryId, edition, initSystem)})

proc package_manager_services(handle: int, categoryId, edition, initSystem: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getServicesForOs(lookupPkg(handle), categoryId, edition, initSystem)})

proc package_manager_early_services(handle: int, categoryId, edition, initSystem: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getEarlyServicesForOs(lookupPkg(handle), categoryId, edition, initSystem)})

proc package_manager_config_paths(handle: int, categoryId, edition: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getConfigPaths(lookupPkg(handle), categoryId, edition)})

proc package_manager_required_categories(handle: int): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getRequiredCategories(lookupPkg(handle))})

proc package_manager_optional_categories(handle: int): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.getOptionalCategories(lookupPkg(handle))})

proc package_manager_validate_category(handle: int, categoryId: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.validateCategory(lookupPkg(handle), categoryId)})

proc package_manager_validate_edition(handle: int, edition: string): string {.exportpy.} =
  guarded:
    $(%*{"value": fnaspackages.validateEdition(lookupPkg(handle), edition)})
