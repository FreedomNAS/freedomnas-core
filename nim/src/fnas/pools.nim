## Pool composition: the devices that are not where the data lives.
##
## A cache device (L2ARC) and a log device (SLOG) are attachments, not part
## of the pool's redundancy. That has two consequences this module is built
## around:
##
##   * they can be added to a pool that already exists, so the same code
##     serves "choose an SSD while creating the pool" and "add one later";
##   * losing one loses no data, so attaching them must never be able to
##     take the pool down with it — a cache that fails to attach is
##     reported, and the pool it was meant for is left exactly as it was.

import std/[json, sequtils, strutils, tables]
import ./engine
import ./errors

const Roles = [("cache", "pool_add_cache"), ("log", "pool_add_log")].toTable

proc attachDevices*(eng: Engine, poolName: string,
                    cache, log: seq[string]): JsonNode =
  ## Attach cache and/or log devices to an existing pool.
  ##
  ## Returns what went in and what did not. Never raises: the pool is
  ## already there, and a cache device that will not attach is a smaller
  ## problem than an exception unwinding the caller.
  result = newJObject()
  result["cache"] = newJArray()
  result["log"] = newJArray()
  result["errors"] = newJArray()

  for (role, devices) in [("cache", cache), ("log", log)]:
    let filtered = devices.filterIt(it.len > 0)
    if filtered.len == 0:
      continue
    try:
      discard eng.execute("zfs", Roles[role],
                          {"pool_name": poolName, "devices": filtered.join(" ")}.toTable,
                          initTable[string, string](), 30, true)
    except FnasError as exc:
      var failure = newJObject()
      failure["role"] = %role
      failure["devices"] = %filtered
      failure["error"] = %exc.msg
      result["errors"].add failure
      continue
    result[role] = %filtered

proc detachDevice*(eng: Engine, poolName, device: string) =
  ## Remove one attached device. Raises if ZFS refuses.
  ##
  ## `zpool remove` takes hot spares, cache, log and even some data vdevs —
  ## the refusal to unmake a pool's redundancy comes from ZFS itself, which
  ## is the right place for it to come from.
  discard eng.execute("zfs", "pool_remove_device",
                      {"pool_name": poolName, "device": device}.toTable,
                      initTable[string, string](), 30, true)
