## FreedomNAS ZFS Encryption Key Management.
##
## Manages independent unlock factors ("keyslots") for ZFS-native-encrypted
## pools, analogous to LUKS2/systemd-cryptenroll keyslots:
##
##   - At pool-creation time a random raw ZFS key is generated once and
##     never stored in the clear anywhere.
##   - Two independent wrapped copies of that raw key are kept in a keyslot
##     file outside the encrypted pool (on the boot device, so it is
##     readable before the pool is unlocked): one unwrapped by the byte
##     content of a "freedomnas.key" file distributed to one or more USB
##     sticks (zero-touch boot), one unwrapped by an operator-chosen Master
##     Passphrase (TTY fallback). Each slot uses its own salt/nonce and
##     scrypt KDF, so compromising one factor does not reveal the other or
##     the pool key.
##   - Both unlock paths converge on the same engine action
##     (zfs.load_key_file): the raw key is written to a 0600 tmpfs file
##     just long enough for `zfs load-key -L file://...` to read it, then
##     the file is shredded.
##
## The wrapping (scrypt + AES-256-GCM, RFC 7914 / NIST SP 800-38D) is
## cross-checked byte-for-byte against Python's hashlib.scrypt and
## cryptography's AESGCM: a keyslot written by one engine must open with
## the other. One divergence from keymgmt.py is accepted rather than
## chased: a malformed (corrupted) keyslot's exact error text may differ,
## since Nim's hex decoder does not reproduce CPython's ValueError wording
## character for character. It still raises, and unlock still fails safe.

import std/[json, monotimes, options, os, posix, strutils, tables, times]
import posix/termios
import nimcrypto/[bcmode, rijndael, scrypt]
import nimcrypto/sysrand as ncsysrand
import ./engine
import ./errors

const
  KeyslotDir* = "/etc/freedomnas/keyslots"
  RuntimeKeyDir* = "/run/freedomnas"
  UsbKeyFilename* = "freedomnas.key"
  UsbMountpoint* = "/run/freedomnas/usbkey"
  ScryptN = 1 shl 15
  ScryptR = 8
  ScryptP = 1
  KeyLen* = 32
  SaltLen = 16
  NonceLen = 12
  GcmTagLen = 16

# ---------------------------------------------------------------------------
# Byte/hex helpers
# ---------------------------------------------------------------------------

proc genRandomBytes(n: int): seq[byte] =
  ## randomBytes reports failure by returning fewer bytes, never by raising
  ## — and on FreeBSD it needs /dev/urandom, which a chroot or jail without
  ## devfs does not have. Ignoring that would leave a zero-filled pool key.
  result = newSeq[byte](n)
  if n > 0 and ncsysrand.randomBytes(result) != n:
    raiseFnas(ekOsError, "Could not read " & $n & " random bytes from the system")

proc toBytes(s: string): seq[byte] =
  result = newSeq[byte](s.len)
  for i in 0 ..< s.len: result[i] = byte(s[i])

proc toHexStr*(data: openArray[byte]): string =
  const digits = "0123456789abcdef"
  result = newString(data.len * 2)
  for i, b in data:
    result[i*2] = digits[int(b) shr 4]
    result[i*2 + 1] = digits[int(b) and 0xF]

proc fromHexStrict*(s: string): seq[byte] =
  if s.len mod 2 != 0:
    raise newException(ValueError, "non-hexadecimal number found in fromhex() arg")
  result = newSeq[byte](s.len div 2)
  for i in 0 ..< result.len:
    var v = 0
    for j in 0 .. 1:
      let c = s[i*2 + j]
      let digit =
        case c
        of '0'..'9': ord(c) - ord('0')
        of 'a'..'f': ord(c) - ord('a') + 10
        of 'A'..'F': ord(c) - ord('A') + 10
        else: raise newException(ValueError, "non-hexadecimal number found in fromhex() arg")
      v = v * 16 + digit
    result[i] = byte(v)

# ---------------------------------------------------------------------------
# Key wrapping primitives
# ---------------------------------------------------------------------------

proc deriveWrappingKey(secret, salt: seq[byte]): seq[byte] =
  let (xyvLen, bLen) = scryptCalc(ScryptN, ScryptR, ScryptP)
  var xyv = newSeq[uint32](xyvLen)
  var b = newSeq[byte](bLen)
  result = newSeq[byte](KeyLen)
  discard scrypt(secret, salt, ScryptN, ScryptR, ScryptP, xyv, b, result)

proc wrapKey*(rawKey, secret: seq[byte]): JsonNode =
  ## Wrap rawKey so it can only be recovered with the given secret.
  let salt = genRandomBytes(SaltLen)
  let nonce = genRandomBytes(NonceLen)
  let wrappingKey = deriveWrappingKey(secret, salt)

  var ctx: GCM[aes256]
  ctx.init(wrappingKey, nonce, [])
  var ciphertext = newSeq[byte](rawKey.len)
  var tag: array[GcmTagLen, byte]
  ctx.encrypt(rawKey, ciphertext, tag)
  ctx.clear()

  result = newJObject()
  result["salt"] = %toHexStr(salt)
  result["nonce"] = %toHexStr(nonce)
  result["ciphertext"] = %toHexStr(ciphertext & @tag)

proc unwrapKey*(slot: JsonNode, secret: seq[byte]): seq[byte] =
  ## Recover the raw key from a slot. Raises on any mismatch (kind ekKeyMgmt).
  if slot == nil or slot.kind != JObject:
    raiseFnas(ekKeyMgmt, "Malformed keyslot data")

  let saltNode = slot{"salt"}
  let nonceNode = slot{"nonce"}
  let ctNode = slot{"ciphertext"}
  if saltNode == nil or nonceNode == nil or ctNode == nil or
     saltNode.kind != JString or nonceNode.kind != JString or ctNode.kind != JString:
    raiseFnas(ekKeyMgmt, "Malformed keyslot data")

  var salt, nonce, combined: seq[byte]
  try:
    salt = fromHexStrict(saltNode.getStr)
    nonce = fromHexStrict(nonceNode.getStr)
    combined = fromHexStrict(ctNode.getStr)
  except ValueError as exc:
    raiseFnas(ekKeyMgmt, "Malformed keyslot data: " & exc.msg)

  if combined.len < GcmTagLen:
    raiseFnas(ekKeyMgmt, "Wrong secret or corrupted keyslot")

  let wrappingKey = deriveWrappingKey(secret, salt)
  let ciphertext = combined[0 ..< combined.len - GcmTagLen]
  let tag = combined[combined.len - GcmTagLen ..< combined.len]

  var ctx: GCM[aes256]
  ctx.init(wrappingKey, nonce, [])
  var plaintext = newSeq[byte](ciphertext.len)
  let ok = ctx.decrypt(ciphertext, plaintext, tag)
  ctx.clear()

  if not ok:
    raiseFnas(ekKeyMgmt, "Wrong secret or corrupted keyslot")
  plaintext

# ---------------------------------------------------------------------------
# Keyslot persistence — /etc/freedomnas/keyslots/<pool>.json
# ---------------------------------------------------------------------------

type KeyslotStore* = object
  poolName: string
  path: string

proc newKeyslotStore*(poolName: string, baseDir = ""): KeyslotStore =
  let dir = if baseDir.len > 0: baseDir else: KeyslotDir
  KeyslotStore(poolName: poolName, path: dir / (poolName & ".json"))

proc storeExists*(store: KeyslotStore): bool = fileExists(store.path)

proc storeLoad(store: KeyslotStore): JsonNode =
  parseJson(readFile(store.path))

proc storeSave(store: KeyslotStore, data: JsonNode) =
  let parent = parentDir(store.path)
  if parent.len > 0:
    createDir(parent)
    setFilePermissions(parent, {fpUserRead, fpUserWrite, fpUserExec})
  let tmp = store.path & ".tmp"
  writeFile(tmp, data.pretty(2))
  setFilePermissions(tmp, {fpUserRead, fpUserWrite})
  moveFile(tmp, store.path)

proc getSlot*(store: KeyslotStore, name: string): JsonNode =
  if not store.storeExists(): return nil
  let data = store.storeLoad()
  let slots = data{"slots"}
  if slots == nil or slots.kind != JObject: return nil
  slots{name}

proc setSlot*(store: KeyslotStore, name: string, slot: JsonNode) =
  var data: JsonNode
  let now = times.now().format("yyyy-MM-dd'T'HH:mm:ss")
  if store.storeExists():
    data = store.storeLoad()
  else:
    data = newJObject()
    data["pool"] = %store.poolName
    data["created_at"] = %now
    data["slots"] = newJObject()
  if data{"slots"} == nil or data["slots"].kind != JObject:
    data["slots"] = newJObject()
  data["slots"][name] = slot
  data["updated_at"] = %now
  store.storeSave(data)

# ---------------------------------------------------------------------------
# Transient tmpfs keyfile helpers, shared by unlock and provisioning
# ---------------------------------------------------------------------------

proc writeTransientKeyfile(poolName: string, rawKey: seq[byte], baseDir = ""): string =
  let dir = if baseDir.len > 0: baseDir else: RuntimeKeyDir
  createDir(dir)
  setFilePermissions(dir, {fpUserRead, fpUserWrite, fpUserExec})
  result = dir / (poolName & ".key")
  let fd = posix.open(result.cstring, O_WRONLY or O_CREAT or O_TRUNC, 0o600.Mode)
  if fd < 0:
    raiseFnas(ekKeyMgmt, "Could not open keyfile: " & result)
  try:
    if rawKey.len > 0:
      discard posix.write(fd, unsafeAddr rawKey[0], rawKey.len)
  finally:
    discard posix.close(fd)

proc shred(path: string) =
  try:
    let size = getFileSize(path)
    if size > 0:
      let junk = genRandomBytes(int(size))
      let fd = posix.open(path.cstring, O_WRONLY)
      if fd >= 0:
        discard posix.write(fd, unsafeAddr junk[0], junk.len)
        discard posix.fsync(fd)
        discard posix.close(fd)
    removeFile(path)
  except OSError, IOError:
    discard

# ---------------------------------------------------------------------------
# Unlock at boot: discovery, USB zero-touch
# ---------------------------------------------------------------------------

proc listLockedPools*(eng: Engine): seq[string] =
  ## Imported pools whose root dataset is keystatus=unavailable.
  var stdout: string
  try:
    (stdout, _) = eng.execute("zfs", "pool_list", initTable[string, string](),
                              initTable[string, string](), 30, false)
  except FnasError:
    return @[]

  for line in stdout.splitLines():
    let fields = line.splitWhitespace()
    if fields.len == 0: continue
    let poolName = fields[0]
    var status: string
    try:
      (status, _) = eng.execute("zfs", "pool_keystatus", {"pool_name": poolName}.toTable,
                                initTable[string, string](), 30, false)
    except FnasError:
      continue
    if status.strip() == "unavailable":
      result.add poolName

proc listRemovableDevices(eng: Engine): seq[string] =
  var stdout: string
  try:
    (stdout, _) = eng.execute("media", "list_removable", initTable[string, string](),
                              initTable[string, string](), 30, false)
  except FnasError:
    return @[]

  for line in stdout.splitLines():
    let fields = line.splitWhitespace()
    if fields.len < 3: continue
    if fields[1] == "1" and fields[2] == "part":
      result.add "/dev/" & fields[0]

proc readUsbKey(eng: Engine, device, mountpoint: string): seq[byte] =
  ## Empty seq means "no key" — a 0-byte freedomnas.key is not a realistic
  ## input to distinguish from "absent".
  let mp = if mountpoint.len > 0: mountpoint else: UsbMountpoint
  createDir(mp)
  setFilePermissions(mp, {fpUserRead, fpUserWrite, fpUserExec})
  try:
    discard eng.execute("media", "mount_ro", {"device": device, "mountpoint": mp}.toTable,
                        initTable[string, string](), 30, true)
  except FnasError:
    return @[]

  try:
    let keyPath = mp / UsbKeyFilename
    if not fileExists(keyPath):
      return @[]
    toBytes(readFile(keyPath))
  finally:
    try:
      discard eng.execute("media", "unmount", {"mountpoint": mp}.toTable,
                          initTable[string, string](), 30, false)
    except FnasError:
      discard

proc loadRawKey(eng: Engine, poolName: string, rawKey: seq[byte], runtimeKeyDir: string): bool =
  let keyfile = writeTransientKeyfile(poolName, rawKey, runtimeKeyDir)
  try:
    discard eng.execute("zfs", "load_key_file",
                        {"pool_name": poolName, "keyfile_path": keyfile}.toTable,
                        initTable[string, string](), 30, true)
    # Only this pool's datasets: `zfs mount -a` would also mount every
    # other imported pool, including one belonging to another system.
    discard eng.execute("zfs", "mount_pool", {"pool_name": poolName}.toTable,
                        initTable[string, string](), 30, false)
    result = true
  except FnasError:
    result = false
  finally:
    shred(keyfile)

proc tryUsbUnlock*(eng: Engine, poolName: string,
                   keyslotDir = "", usbMountpoint = "", runtimeKeyDir = ""): bool =
  ## Scan removable media for freedomnas.key and unlock poolName if it matches.
  let store = newKeyslotStore(poolName, keyslotDir)
  let slot = store.getSlot("usb")
  if slot == nil:
    return false

  for device in listRemovableDevices(eng):
    let keyBytes = readUsbKey(eng, device, usbMountpoint)
    if keyBytes.len == 0: continue
    var rawKey: seq[byte]
    try:
      rawKey = unwrapKey(slot, keyBytes)
    except FnasError:
      continue
    return loadRawKey(eng, poolName, rawKey, runtimeKeyDir)

  false

# ---------------------------------------------------------------------------
# TTY passphrase fallback
# ---------------------------------------------------------------------------

proc readLineNoEcho(fdIn, fdOut: cint): Option[string] =
  ## Read one line from an explicit tty fd with local echo disabled.
  ## None means EOF (tty closed/detached) before anything was read.
  var oldSettings, newSettings: Termios
  discard tcGetAttr(fdIn, addr oldSettings)
  newSettings = oldSettings
  newSettings.c_lflag = newSettings.c_lflag and not ECHO
  discard tcSetAttr(fdIn, TCSANOW, addr newSettings)

  var line = ""
  var buf: array[1, char]
  var eof = false
  while true:
    let n = posix.read(fdIn, addr buf[0], 1)
    if n <= 0:
      eof = true
      break
    if buf[0] == '\n':
      break
    line.add buf[0]

  discard tcSetAttr(fdIn, TCSANOW, addr oldSettings)
  let newline = "\n"
  discard posix.write(fdOut, newline.cstring, newline.len)

  if eof and line.len == 0:
    none(string)
  else:
    some(line)

proc tryTtyUnlock*(eng: Engine, poolName, ttyPath: string, timeout: int,
                   keyslotDir = "", runtimeKeyDir = ""): bool =
  ## Prompt for the Master Passphrase on ttyPath, retrying until success or
  ## until `timeout` seconds elapse without a successful attempt.
  ##
  ## Fails open: on timeout, returns false rather than blocking forever —
  ## the pool stays locked until unlocked manually.
  let store = newKeyslotStore(poolName, keyslotDir)
  let slot = store.getSlot("passphrase")
  if slot == nil:
    return false

  let fdIn = posix.open(ttyPath.cstring, O_RDONLY)
  if fdIn < 0:
    return false
  let fdOut = posix.open(ttyPath.cstring, O_WRONLY)
  if fdOut < 0:
    discard posix.close(fdIn)
    return false

  defer:
    discard posix.close(fdIn)
    discard posix.close(fdOut)

  let deadline = getMonoTime() + initDuration(seconds = timeout)
  while getMonoTime() < deadline:
    let remaining = max(0'i64, (deadline - getMonoTime()).inSeconds)
    let prompt = "\nFreedomNAS: pool '" & poolName &
                "' is encrypted and no USB key was found.\n" &
                "Enter Master Passphrase to unlock (giving up in " & $remaining & "s): "
    discard posix.write(fdOut, prompt.cstring, prompt.len)

    let lineOpt = readLineNoEcho(fdIn, fdOut)
    if lineOpt.isNone:
      break
    let passphrase = lineOpt.get
    if passphrase.len == 0:
      continue

    var rawKey: seq[byte]
    try:
      rawKey = unwrapKey(slot, toBytes(passphrase))
    except FnasError:
      let wrong = "Incorrect passphrase.\n"
      discard posix.write(fdOut, wrong.cstring, wrong.len)
      continue

    return loadRawKey(eng, poolName, rawKey, runtimeKeyDir)

  let timedOut = "\nFreedomNAS: timed out waiting for the Master Passphrase for '" &
                poolName & "'.\n" &
                "Continuing boot with this pool locked -- unlock it later from the WebUI or CLI.\n"
  discard posix.write(fdOut, timedOut.cstring, timedOut.len)
  false

# ---------------------------------------------------------------------------
# Provisioning at pool-creation time
# ---------------------------------------------------------------------------

proc writeUsbKey(eng: Engine, device: string, keyBytes: seq[byte], mountpoint: string) =
  let mp = if mountpoint.len > 0: mountpoint else: UsbMountpoint
  createDir(mp)
  setFilePermissions(mp, {fpUserRead, fpUserWrite, fpUserExec})
  discard eng.execute("media", "mount_rw", {"device": device, "mountpoint": mp}.toTable,
                      initTable[string, string](), 30, true)
  try:
    let keyPath = mp / UsbKeyFilename
    let fd = posix.open(keyPath.cstring, O_WRONLY or O_CREAT or O_TRUNC, 0o600.Mode)
    if fd < 0:
      raiseFnas(ekKeyMgmt, "Could not open " & keyPath)
    try:
      if keyBytes.len > 0:
        discard posix.write(fd, unsafeAddr keyBytes[0], keyBytes.len)
    finally:
      discard posix.close(fd)
  finally:
    discard eng.execute("media", "unmount", {"mountpoint": mp}.toTable,
                        initTable[string, string](), 30, false)

proc createEncryptedPool*(eng: Engine, poolName, vdevType, devices, masterPassphrase: string,
                          usbDevices: seq[string], ashift: int, compression: string,
                          keyslotDir = "", runtimeKeyDir = "", usbMountpoint = "") =
  ## Create poolName with ZFS native encryption, then provision two
  ## independent unlock keyslots: one wrapped with masterPassphrase, one
  ## wrapped with a freshly generated random USB key that is written as
  ## freedomnas.key onto every device in usbDevices.
  let rawKey = genRandomBytes(KeyLen)
  let usbKeyBytes = genRandomBytes(KeyLen)

  let keyfile = writeTransientKeyfile(poolName, rawKey, runtimeKeyDir)
  try:
    discard eng.execute("zfs", "pool_create_encrypted",
                        {"pool_name": poolName, "vdev_type": vdevType, "devices": devices,
                         "compression": compression, "keyfile_path": keyfile}.toTable,
                        {"ashift": $ashift}.toTable, 30, true)
  finally:
    shred(keyfile)

  let store = newKeyslotStore(poolName, keyslotDir)
  store.setSlot("passphrase", wrapKey(rawKey, toBytes(masterPassphrase)))
  store.setSlot("usb", wrapKey(rawKey, usbKeyBytes))

  for device in usbDevices:
    writeUsbKey(eng, device, usbKeyBytes, usbMountpoint)
