## FreedomNAS Reconciliation Engine.
##
## Applies configuration from config.yaml to bring a freshly installed OS to
## the desired state.
##
## Three modes:
## - hybrid            : diff desired vs current, apply minimal changes;
##                        users absent from desired are never deleted
## - declarative        : treat current as empty, apply full desired state;
##                        users and shares missing from desired are deleted
## - disaster_recovery  : restore from config snapshot in fixed order
##                        (hostname -> timezone -> pools -> users -> services
##                        -> shares)

import std/[algorithm, json, sequtils, sets, strutils, tables]
import ./engine
import ./errors
import ./pyyaml
import ./yamlvalue

const
  Modes = ["hybrid", "declarative", "disaster_recovery"]
  FeatureServiceMap = {
    "samba-cifs": ("samba", "smb_service"),
    "nfs": ("nfs", "nfs_service"),
  }.toTable

# ---------------------------------------------------------------------------
# Change / ActionResult builders
# ---------------------------------------------------------------------------

proc change(path: string, oldValue, newValue: JsonNode, changeType: string): JsonNode =
  result = newJObject()
  result["path"] = %path
  result["old_value"] = if oldValue == nil: newJNull() else: oldValue
  result["new_value"] = if newValue == nil: newJNull() else: newValue
  result["change_type"] = %changeType

proc actionResult(action: string, params: JsonNode, state: string, error = ""): JsonNode =
  result = newJObject()
  result["action"] = %action
  result["params"] = params
  result["target_state"] = %state
  result["error"] = if error.len == 0: newJNull() else: %error

proc truthy(node: JsonNode): bool =
  if node == nil: return false
  case node.kind
  of JNull: false
  of JString: node.getStr.len > 0
  of JBool: node.getBool
  of JInt: node.getInt != 0
  of JFloat: node.getFloat != 0.0
  of JArray: node.elems.len > 0
  of JObject: node.fields.len > 0

proc byKey(arr: JsonNode, key: string): OrderedTable[string, JsonNode] =
  result = initOrderedTable[string, JsonNode]()
  if arr == nil or arr.kind != JArray: return
  for item in arr.items:
    if item.kind == JObject and item.hasKey(key) and item[key].kind == JString:
      result[item[key].getStr] = item

# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------

proc scalarChange(desired, current: JsonNode, section, key, path: string): JsonNode =
  let dVal = desired{section, key}
  let cVal = current{section, key}
  if not truthy(dVal) or dVal == cVal: return nil
  change(path, cVal, dVal, (if truthy(cVal): "modify" else: "add"))

proc diffShares(desired, current: JsonNode, key, pathPrefix: string,
                changes: var seq[JsonNode]) =
  let dItems = byKey(desired{key}, "name")
  let cItems = byKey(current{key}, "name")
  for name, cfg in dItems.pairs:
    if name notin cItems:
      changes.add change(pathPrefix & "." & name, nil, cfg, "add")
    elif cfg != cItems[name]:
      changes.add change(pathPrefix & "." & name, cItems[name], cfg, "modify")
  for name, cfg in cItems.pairs:
    if name notin dItems:
      changes.add change(pathPrefix & "." & name, cfg, nil, "remove")

proc computeDiff*(desired, current: JsonNode, mode: string): seq[JsonNode] =
  result = @[]

  let hostname = scalarChange(desired, current, "system", "hostname", "system.hostname")
  if hostname != nil: result.add hostname
  let timezone = scalarChange(desired, current, "system", "timezone", "system.timezone")
  if timezone != nil: result.add timezone

  let dUsers = byKey(desired{"users"}, "username")
  let cUsers = byKey(current{"users"}, "username")
  for username, userCfg in dUsers.pairs:
    if username notin cUsers:
      result.add change("users." & username, nil, userCfg, "add")
    elif userCfg != cUsers[username]:
      result.add change("users." & username, cUsers[username], userCfg, "modify")
  if mode == "declarative":
    for username, oldCfg in cUsers.pairs:
      if username notin dUsers:
        result.add change("users." & username, oldCfg, nil, "remove")

  let dPools = byKey(desired{"storage", "pools"}, "name")
  let cPools = byKey(current{"storage", "pools"}, "name")
  for name, poolCfg in dPools.pairs:
    if name notin cPools:
      result.add change("storage.pools." & name, nil, poolCfg, "add")

  diffShares(desired, current, "smb_shares", "smb_shares", result)
  diffShares(desired, current, "nfs_shares", "nfs_shares", result)

proc loadDesired*(path: string): JsonNode =
  var text: string
  try:
    text = readFile(path)
  except IOError as exc:
    raiseFnas(ekOsError, exc.msg & ": '" & path & "'")
  let val = pyyaml.load(text)
  if val == nil: newJObject() else: toJson(val)

# ---------------------------------------------------------------------------
# Atomic engine calls
# ---------------------------------------------------------------------------

proc runAction(eng: Engine, action: string, params: JsonNode,
               category, act: string, strValues: Table[string, string]): JsonNode =
  try:
    discard eng.execute(category, act, strValues, initTable[string, string](), 30, true)
    actionResult(action, params, "applied")
  except FnasError as exc:
    actionResult(action, params, "failed", exc.msg)

proc doHostname(eng: Engine, hostname: string): JsonNode =
  runAction(eng, "system.hostname_set", %*{"hostname": hostname},
           "system", "hostname_set", {"hostname": hostname}.toTable)

proc doTimezone(eng: Engine, timezone: string): JsonNode =
  runAction(eng, "system.timezone_set", %*{"timezone": timezone},
           "system", "timezone_set", {"timezone": timezone}.toTable)

proc poolIsImported(eng: Engine, poolName: string): bool =
  var stdout: string
  try:
    (stdout, _) = eng.execute("zfs", "pool_list", initTable[string, string](),
                              initTable[string, string](), 30, false)
  except FnasError:
    return false
  for line in stdout.splitLines():
    let fields = line.splitWhitespace()
    if fields.len > 0 and fields[0] == poolName:
      return true
  false

proc doPoolImport(eng: Engine, pool: JsonNode): JsonNode =
  let poolName = pool{"name"}.getStr("")
  let params = %*{"pool_name": poolName}
  if poolIsImported(eng, poolName):
    return actionResult("zfs.pool_import", params, "skipped", "already imported")
  runAction(eng, "zfs.pool_import", params, "zfs", "pool_import",
           {"pool_name": poolName}.toTable)

proc userExists(eng: Engine, username: string): bool =
  var stdout: string
  try:
    (stdout, _) = eng.execute("users", "get_user_info",
                              {"username": username}.toTable,
                              initTable[string, string](), 30, false)
  except FnasError:
    return false
  stdout.strip().len > 0

proc joinStrItems(node: JsonNode, sep: string): string =
  if node == nil or node.kind != JArray: return ""
  var parts: seq[string]
  for item in node.items:
    if item.kind == JString: parts.add item.getStr
  parts.join(sep)

proc addOptional(strValues, rawValues: var Table[string, string], key: string,
                 node: JsonNode, default: string) =
  ## Mirrors `user.get(key, default)` passed straight to `engine.execute`:
  ## a string value is sanitized like any other kwarg, anything else (an
  ## int uid/gid, or the default) is not — the pure-Python engine only
  ## sanitizes kwargs it receives as `str`.
  if node == nil or node.kind == JNull:
    rawValues[key] = default
  elif node.kind == JString:
    strValues[key] = node.getStr
  elif node.kind == JInt:
    rawValues[key] = $node.getInt
  else:
    rawValues[key] = $node

proc doUserCreate(eng: Engine, user: JsonNode): JsonNode =
  let username = user{"username"}.getStr("")
  let groups = joinStrItems(user{"groups"}, ",")
  let params = %*{"username": username, "groups": groups}
  if userExists(eng, username):
    return actionResult("users.create", params, "skipped", "user already exists")

  var strValues = {"username": username, "groups": groups}.toTable
  var rawValues = initTable[string, string]()
  addOptional(strValues, rawValues, "uid", user{"uid"}, "")
  addOptional(strValues, rawValues, "gid", user{"gid"}, "")
  addOptional(strValues, rawValues, "shell", user{"shell"}, "/bin/bash")

  try:
    discard eng.execute("users", "create", strValues, rawValues, 30, true)
    actionResult("users.create", params, "applied")
  except FnasError as exc:
    actionResult("users.create", params, "failed", exc.msg)

proc doUserModify(eng: Engine, user, oldUser: JsonNode): seq[JsonNode] =
  let username = user{"username"}.getStr("")
  var desiredGroups, currentGroups: HashSet[string]
  if user{"groups"} != nil and user{"groups"}.kind == JArray:
    for g in user{"groups"}.items:
      if g.kind == JString: desiredGroups.incl g.getStr
  if oldUser{"groups"} != nil and oldUser{"groups"}.kind == JArray:
    for g in oldUser{"groups"}.items:
      if g.kind == JString: currentGroups.incl g.getStr

  for group in sorted(toSeq(desiredGroups - currentGroups)):
    let params = %*{"username": username, "group": group}
    result.add runAction(eng, "users.add_to_group", params, "users", "add_to_group",
                         {"username": username, "group": group}.toTable)

  for group in sorted(toSeq(currentGroups - desiredGroups)):
    let params = %*{"username": username, "group": group}
    result.add runAction(eng, "users.remove_from_group", params, "users", "remove_from_group",
                         {"username": username, "group": group}.toTable)

  if result.len == 0:
    result.add actionResult("users.modify", %*{"username": username}, "skipped",
                            "no group membership changes detected")

proc doUserDelete(eng: Engine, username: string): JsonNode =
  runAction(eng, "users.delete", %*{"username": username}, "users", "delete",
           {"username": username}.toTable)

proc doSmbShareCreate(eng: Engine, share: JsonNode): JsonNode =
  let
    name = share{"name"}.getStr("")
    path = share{"path"}.getStr("")
    comment = share{"comment"}.getStr("")
    acl = (if share{"acl"} != nil and share{"acl"}.kind == JString: share{"acl"}.getStr
           else: "Everyone:F")
  runAction(eng, "samba.create_share", %*{"share_name": name, "path": path},
           "samba", "create_share",
           {"share_name": name, "path": path, "comment": comment, "acl": acl}.toTable)

proc doSmbShareDelete(eng: Engine, shareName: string): JsonNode =
  runAction(eng, "samba.delete_share", %*{"share_name": shareName},
           "samba", "delete_share", {"share_name": shareName}.toTable)

proc doNfsShareCreate(eng: Engine, share: JsonNode): JsonNode =
  let
    name = share{"name"}.getStr("")
    path = share{"path"}.getStr("")
    clients = (if share{"clients"} != nil and share{"clients"}.kind == JString:
                share{"clients"}.getStr else: "*")
    options = (if share{"options"} != nil and share{"options"}.kind == JString:
                share{"options"}.getStr else: "ro,sync,no_subtree_check")
    params = %*{"name": name, "path": path, "clients": clients}
  try:
    discard eng.execute("nfs", "export_add",
                        {"path": path, "clients": clients, "options": options}.toTable,
                        initTable[string, string](), 30, true)
    discard eng.execute("nfs", "exportfs_reload", initTable[string, string](),
                        initTable[string, string](), 30, true)
    actionResult("nfs.export_add", params, "applied")
  except FnasError as exc:
    actionResult("nfs.export_add", params, "failed", exc.msg)

proc doNfsShareDelete(eng: Engine, shareName: string, nfsShares: JsonNode): JsonNode =
  let params = %*{"name": shareName}
  var path = ""
  var found = false
  if nfsShares != nil and nfsShares.kind == JArray:
    for share in nfsShares.items:
      if share.kind == JObject and share{"name"}.getStr("") == shareName:
        let p = share{"path"}
        if p != nil and p.kind == JString and p.getStr.len > 0:
          path = p.getStr
          found = true
        break

  if not found:
    return actionResult("nfs.export_remove", params, "skipped",
                        "No path recorded for NFS share '" & shareName & "'")

  try:
    discard eng.execute("nfs", "export_remove", {"path": path}.toTable,
                        initTable[string, string](), 30, true)
    discard eng.execute("nfs", "exportfs_reload", initTable[string, string](),
                        initTable[string, string](), 30, true)
    actionResult("nfs.export_remove", params, "applied")
  except FnasError as exc:
    actionResult("nfs.export_remove", params, "failed", exc.msg)

proc doServiceEnable(eng: Engine, featureId: string): seq[JsonNode] =
  if featureId notin FeatureServiceMap:
    return @[actionResult("services.enable." & featureId, %*{"feature_id": featureId},
                          "skipped", "No service mapping for feature '" & featureId & "'")]

  let (category, key) = FeatureServiceMap[featureId]
  var serviceName: string
  try:
    serviceName = eng.getProfileValue(category, key).getStr
  except FnasError as exc:
    return @[actionResult("services.enable." & featureId, %*{"feature_id": featureId},
                          "skipped", exc.msg)]

  for action in ["enable", "start"]:
    let params = %*{"service_name": serviceName}
    result.add runAction(eng, "services." & action, params, "services", action,
                         {"service_name": serviceName}.toTable)

# ---------------------------------------------------------------------------
# Applying changes / disaster recovery
# ---------------------------------------------------------------------------

proc applyChanges(eng: Engine, changes: seq[JsonNode], nfsShares: JsonNode): seq[JsonNode] =
  for c in changes:
    let path = c["path"].getStr
    let changeType = c["change_type"].getStr

    if path == "system.hostname":
      result.add doHostname(eng, c["new_value"].getStr)
    elif path == "system.timezone":
      result.add doTimezone(eng, c["new_value"].getStr)
    elif path.startsWith("users."):
      if changeType == "add":
        result.add doUserCreate(eng, c["new_value"])
      elif changeType == "modify":
        result.add doUserModify(eng, c["new_value"], c["old_value"])
      elif changeType == "remove":
        result.add doUserDelete(eng, path.split(".", 1)[1])
    elif path.startsWith("storage.pools."):
      if changeType == "add":
        result.add doPoolImport(eng, c["new_value"])
    elif path.startsWith("smb_shares."):
      if changeType in ["add", "modify"]:
        result.add doSmbShareCreate(eng, c["new_value"])
      elif changeType == "remove":
        result.add doSmbShareDelete(eng, path.split(".", 1)[1])
    elif path.startsWith("nfs_shares."):
      if changeType in ["add", "modify"]:
        result.add doNfsShareCreate(eng, c["new_value"])
      elif changeType == "remove":
        result.add doNfsShareDelete(eng, path.split(".", 1)[1], nfsShares)

proc applyDisasterRecovery(eng: Engine, state, nfsShares: JsonNode): seq[JsonNode] =
  let hostname = state{"system", "hostname"}
  if truthy(hostname):
    result.add doHostname(eng, hostname.getStr)

  let timezone = state{"system", "timezone"}
  if truthy(timezone):
    result.add doTimezone(eng, timezone.getStr)

  let pools = state{"storage", "pools"}
  if pools != nil and pools.kind == JArray:
    for pool in pools.items:
      result.add doPoolImport(eng, pool)

  let users = state{"users"}
  if users != nil and users.kind == JArray:
    for user in users.items:
      result.add doUserCreate(eng, user)

  let features = state{"installed_features"}
  if features != nil and features.kind == JArray:
    for feature in features.items:
      if truthy(feature{"enabled"}):
        result.add doServiceEnable(eng, feature{"id"}.getStr(""))

  let smbShares = state{"smb_shares"}
  if smbShares != nil and smbShares.kind == JArray:
    for share in smbShares.items:
      result.add doSmbShareCreate(eng, share)

  let nfsSharesState = state{"nfs_shares"}
  if nfsSharesState != nil and nfsSharesState.kind == JArray:
    for share in nfsSharesState.items:
      result.add doNfsShareCreate(eng, share)

proc reconcileRun*(eng: Engine, desiredPath, mode: string,
                   state, nfsShares: JsonNode): JsonNode =
  if mode notin Modes:
    raiseFnas(ekValue, "'" & mode & "' is not a valid ReconcileMode")

  var results: seq[JsonNode]
  if mode == "disaster_recovery":
    results = applyDisasterRecovery(eng, state, nfsShares)
  else:
    let desired = if desiredPath.len > 0: loadDesired(desiredPath) else: state
    let current = if mode == "declarative": newJObject() else: state
    let changes = computeDiff(desired, current, mode)
    results = applyChanges(eng, changes, nfsShares)

  result = newJArray()
  for r in results: result.add r
