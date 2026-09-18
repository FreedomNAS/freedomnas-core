## Scheduled ZFS snapshots with retention.
##
## Cron decides *when* — one entry per interval, from the profile — and this
## decides *what*: for every dataset with a policy, take a snapshot and then
## prune that interval down to the number of copies the policy keeps.
##
## The one rule that matters: only snapshots this scheduler created are ever
## destroyed. They carry a name of their own, so a snapshot taken by hand, by
## the update terminal (`pre-update-*`) or by anything else can never be
## pruned by a retention count someone lowered.

import std/[algorithm, json, strutils, tables, times]
import ./engine
import ./errors

const
  Prefix* = "freedomnas"
  Intervals* = ["hourly", "daily", "weekly", "monthly"]

proc snapshotName*(interval: string, asOf: DateTime): string =
  ## `freedomnas-hourly-20260910-030000`. The timestamp is last and
  ## fixed-width, so sorting these names alphabetically sorts them by age.
  Prefix & "-" & interval & "-" & asOf.format("yyyyMMdd-HHmmss")

proc parseNow*(nowIso: string): DateTime =
  ## Empty means "right now"; anything else is a `datetime.isoformat()`
  ## string, fractional seconds and all — only whole seconds matter here.
  if nowIso.len == 0:
    return times.now()
  parse(nowIso.split('.')[0], "yyyy-MM-dd'T'HH:mm:ss")

proc ours(eng: Engine, dataset, interval: string): seq[string] =
  ## Our snapshots of this dataset and interval, oldest first.
  var stdout: string
  try:
    (stdout, _) = eng.execute("zfs", "snapshot_list", initTable[string, string](),
                              initTable[string, string](), 30, false)
  except FnasError:
    return @[]

  let wanted = dataset & "@" & Prefix & "-" & interval & "-"
  for line in stdout.splitLines():
    let name = line.split('\t')[0]
    if name.startsWith(wanted):
      result.add name
  algorithm.sort(result)

proc prune(eng: Engine, dataset, interval: string, keep: int): seq[string] =
  let existing = ours(eng, dataset, interval)
  let overflow = max(0, existing.len - keep)
  for fullName in existing[0 ..< overflow]:
    let snap = fullName.split('@', 1)[1]
    try:
      discard eng.execute("zfs", "snapshot_destroy",
                          {"dataset": dataset, "snap_name": snap}.toTable,
                          initTable[string, string](), 30, true)
    except FnasError:
      continue
    result.add fullName

proc runOne(eng: Engine, dataset, interval: string, keep: int, asOf: DateTime): JsonNode =
  let name = snapshotName(interval, asOf)
  result = newJObject()
  result["dataset"] = %dataset
  result["interval"] = %interval
  result["created"] = newJNull()
  result["destroyed"] = newJArray()
  result["error"] = newJNull()

  try:
    discard eng.execute("zfs", "snapshot_create",
                        {"dataset": dataset, "snap_name": name}.toTable,
                        initTable[string, string](), 30, true)
  except FnasError as exc:
    result["error"] = %exc.msg
    return

  result["created"] = %(dataset & "@" & name)
  result["destroyed"] = %prune(eng, dataset, interval, keep)

proc policyKeep(policy: JsonNode, interval: string): int =
  let node = policy{interval}
  if node == nil:
    return 0
  case node.kind
  of JInt: node.getInt
  of JString:
    try: parseInt(node.getStr)
    except ValueError: 0
  else: 0

proc runInterval*(eng: Engine, policies: JsonNode, interval: string,
                  asOf: DateTime): JsonNode =
  ## Applies every policy that asks for this interval. Never raises past
  ## a single dataset's failure — only an unknown interval is refused.
  if interval notin Intervals:
    raiseFnas(ekValue, "Unknown interval: " & interval)

  result = newJArray()
  if policies == nil or policies.kind != JArray:
    return

  for policy in policies:
    let dataset = policy{"dataset"}.getStr("").strip()
    let keep = policyKeep(policy, interval)
    if dataset.len == 0 or keep <= 0:
      continue
    result.add runOne(eng, dataset, interval, keep, asOf)
