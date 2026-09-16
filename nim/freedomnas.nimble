version       = "4.0.0"
author        = "FreedomNAS Team"
description   = "FreedomNAS core engine — native implementation"
license       = "MPL-2.0"
srcDir        = "src"
bin           = @["freedomnas_cli"]

requires "nim >= 2.0.0"
requires "nimpy >= 0.2.0"

task native, "Build the Python extension module into the package root":
  exec "nim c --app:lib -d:release --threads:on " &
       "--out:../freedomnas_native.so src/freedomnas_native.nim"

task cli, "Build the freedomnas CLI":
  exec "nim c -d:release --threads:on --out:freedomnas src/freedomnas_cli.nim"
