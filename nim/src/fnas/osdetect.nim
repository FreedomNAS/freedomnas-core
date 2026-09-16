import std/[algorithm, os, sequtils, strutils, tables]
import ./errors

const
  SupportedOs* = ["debian", "freebsd", "arch", "artix", "void", "gentoo"]
  DistroIds* = {
    "debian": "debian",
    "ubuntu": "debian",
    "arch": "arch",
    "artix": "artix",
    "void": "void",
    "gentoo": "gentoo",
  }.toTable
  KnownUnsupported* = ["fedora", "rhel", "centos", "rocky", "almalinux"]

type Resolved* = object
  profile*: string
  warnings*: seq[string]

proc parseOsRelease*(content: string): Table[string, string] =
  for rawLine in content.splitLines():
    let line = rawLine.strip()
    if line.len == 0 or line.startsWith("#") or '=' notin line:
      continue
    let parts = line.split('=', 1)
    result[parts[0].strip().toLowerAscii()] =
      parts[1].strip().strip(chars = {'"', '\''}).toLowerAscii()

proc resolveDistro*(content: string): Resolved =
  let
    fields = parseOsRelease(content)
    distroId = fields.getOrDefault("id", "")

  if distroId in DistroIds:
    let profile = DistroIds[distroId]
    if profile != distroId:
      result.warnings.add(distroId & " detected, using the " & profile & " profile")
    result.profile = profile
    return

  for parent in fields.getOrDefault("id_like", "").splitWhitespace():
    if parent in DistroIds:
      let profile = DistroIds[parent]
      result.warnings.add("Unknown distribution '" & distroId &
        "', using the " & profile & " profile it declares itself compatible with")
      result.profile = profile
      return

  if distroId in KnownUnsupported:
    raiseFnas(ekOsNotSupported,
      "RHEL-based systems not yet supported. Please use Debian or FreeBSD Edition.")

  let shown = if distroId.len > 0: distroId
              else: content[0 ..< min(40, content.len)]
  raiseFnas(ekOsNotSupported,
    "Unknown Linux distribution '" & shown & "'. Supported: " &
    sorted(toSeq(DistroIds.keys)).join(", ") & ".")

proc pathExists(path: string): bool =
  fileExists(path) or dirExists(path) or symlinkExists(path)

proc detectInitSystem*(): string =
  if pathExists("/run/systemd/system"): return "systemd"
  if pathExists("/sbin/openrc") or pathExists("/usr/sbin/openrc"): return "openrc"
  if pathExists("/sbin/runit") or pathExists("/run/runit"): return "runit"
  if pathExists("/usr/bin/s6-svscan"): return "s6"
  if pathExists("/usr/bin/dinit"): return "dinit"
  if pathExists("/etc/rc.d") and hostOS == "freebsd": return "bsd_rc"
  return ""
