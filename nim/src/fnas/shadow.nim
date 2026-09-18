## The configuration's shadow copy on every pool that carries one.
##
## `config.yaml` is a backup of the system state, not the source of truth —
## and a backup that lives only on the OS disk is no backup at all. When the
## disk that holds the root pool dies, what survives is the data pool, and
## that is where the appliance's identity has to be waiting: hostname,
## users, shares, snapshot policies.
##
## So every pool gets a `<pool>/.freedomnas` dataset, and the configuration
## is mirrored into each of them. `firstboot.sh` looks for exactly that
## dataset when it comes up, which is what makes a reinstall onto a blank
## disk find its old self on the data pool.
##
## All the copies mount at the same place (/etc/freedomnas/state), only one
## at a time — which is why mirroring mounts them somewhere else for a
## moment, the same way the initramfs mounts the root dataset at /sysroot.

import std/[json, os, strutils, tables, tempfiles]
import ./engine
import ./errors

const
  DatasetName* = ".freedomnas"
  StateDir* = "/etc/freedomnas/state"
  ConfigName* = "config.yaml"

proc pools(eng: Engine): seq[string] =
  var stdout: string
  try:
    (stdout, _) = eng.execute("zfs", "pool_list", initTable[string, string](),
                              initTable[string, string](), 30, false)
  except FnasError:
    return @[]
  for line in stdout.splitLines():
    let fields = line.splitWhitespace()
    if fields.len > 0:
      result.add fields[0]

proc datasetExists(eng: Engine, dataset: string): bool =
  var stdout: string
  try:
    (stdout, _) = eng.execute("zfs", "dataset_at_path",
                              {"path": dataset}.toTable, initTable[string, string](),
                              30, false)
  except FnasError:
    return false
  stdout.strip() == dataset

proc activeDataset*(eng: Engine): string =
  ## "" when none — the `.freedomnas` currently mounted at StateDir, if any.
  var stdout: string
  try:
    (stdout, _) = eng.execute("zfs", "dataset_at_path",
                              {"path": StateDir}.toTable, initTable[string, string](),
                              30, false)
  except FnasError:
    return ""
  let name = stdout.strip()
  if name.endsWith("/" & DatasetName): name else: ""

proc ensureDataset*(eng: Engine, pool: string): bool =
  ## Give a pool its shadow dataset. True if it had to be created.
  ##
  ## Called when a pool is created, so that a data pool is ready to hold the
  ## configuration from the moment it exists.
  let dataset = pool & "/" & DatasetName
  if datasetExists(eng, dataset):
    return false
  discard eng.execute("zfs", "create_state_dataset", {"pool_name": pool}.toTable,
                      initTable[string, string](), 30, true)
  true

proc copyInto(eng: Engine, dataset, sourcePath: string): JsonNode =
  result = newJObject()
  result["dataset"] = %dataset
  result["copied"] = %false
  result["error"] = newJNull()

  let mountpoint = createTempDir("freedomnas-shadow.", "")
  try:
    discard eng.execute("zfs", "mount_dataset_at",
                        {"dataset": dataset, "mountpoint": mountpoint}.toTable,
                        initTable[string, string](), 30, true)
  except FnasError as exc:
    result["error"] = %("could not mount: " & exc.msg)
    try: removeDir(mountpoint)
    except OSError: discard
    return

  try:
    copyFileWithPermissions(sourcePath, mountpoint / ConfigName)
    result["copied"] = %true
  except OSError as exc:
    result["error"] = %exc.msg

  try:
    discard eng.execute("zfs", "unmount_at", {"mountpoint": mountpoint}.toTable,
                        initTable[string, string](), 30, false)
  except FnasError:
    discard
  try: removeDir(mountpoint)
  except OSError: discard

proc syncAll*(eng: Engine, configPath: string): JsonNode =
  ## Copy the live configuration into every other pool's shadow dataset.
  ##
  ## The pool the configuration is already served from is skipped: it is the
  ## source. Never raises — a pool that cannot be mirrored is reported and
  ## the others still get their copy.
  result = newJArray()
  if not fileExists(configPath):
    return

  let active = activeDataset(eng)
  for pool in pools(eng):
    let dataset = pool & "/" & DatasetName
    if dataset == active or not datasetExists(eng, dataset):
      continue
    result.add copyInto(eng, dataset, configPath)
