version       = "4.0.0"
author        = "FreedomNAS Team"
description   = "FreedomNAS core engine — native implementation"
license       = "MPL-2.0"
srcDir        = "src"
bin           = @["freedomnas_cli"]

requires "nim >= 2.0.0"
requires "nimpy >= 0.2.0"
requires "yaml >= 2.1.0"
requires "nimcrypto >= 0.7.3"

const ThreadFlags =
  # --tlsEmulation:off: Nim's emulated TLS only registers threads Nim
  # itself creates. Every thread that calls into this .so is one Python
  # created (asyncio.to_thread, threading.Thread — see WebUI callers of
  # engine/reconcile/keymgmt), so a foreign thread's first string-argument
  # call reads from an unregistered (nil) slot and segfaults with emulated
  # TLS.
  #
  # -ftls-model=global-dynamic: native TLS alone still is not enough. GCC's
  # default model for a shared library's `__thread` variables (usually
  # initial-exec) assumes a static TLS block reserved once at load time;
  # after enough allocator activity on the main thread — one scrypt-sized
  # (~32 MiB) keyslot wrap is enough, reliably, see keymgmt.nim — a later
  # foreign thread's access to that block segfaults the same way. Forcing
  # global-dynamic makes every access go through __tls_get_addr() properly
  # instead of a cached static offset, at a cost that is irrelevant here.
  "--threads:on --tlsEmulation:off --passC:\"-ftls-model=global-dynamic\""

task native, "Build the Python extension module into the package root":
  exec "nim c --app:lib -d:release " & ThreadFlags &
       " --out:../freedomnas_native.so src/freedomnas_native.nim"

task cli, "Build the freedomnas CLI":
  exec "nim c -d:release " & ThreadFlags & " --out:freedomnas src/freedomnas_cli.nim"
