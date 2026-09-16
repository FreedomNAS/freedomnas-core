import std/[options, os, osproc, posix, streams, times]
import ./errors

type ExecResult* = object
  exitCode*: int
  output*: string
  error*: string
  timedOut*: bool

proc setNonBlocking(fd: cint) =
  discard fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) or O_NONBLOCK)

proc drain(fd: cint, sink: var string): bool =
  ## Returns false once the pipe has reached EOF.
  var buf: array[8192, char]
  while true:
    let n = read(fd, addr buf[0], buf.len)
    if n > 0:
      let start = sink.len
      sink.setLen(start + n)
      copyMem(addr sink[start], addr buf[0], n)
    elif n == 0:
      return false
    else:
      return errno in [EAGAIN, EWOULDBLOCK, EINTR]

proc startOrRaise(argv: seq[string]): Process =
  ## Python reports a failed exec as OSError; its text is part of the
  ## message callers see, so it is reproduced here.
  try:
    result = startProcess(argv[0], args = argv[1 .. ^1], options = {poUsePath})
  except OSError as exc:
    raiseFnas(ekOsError, "[Errno " & $exc.errorCode & "] " &
      osErrorMsg(OSErrorCode(exc.errorCode)) & ": '" & argv[0] & "'")

proc run*(argv: seq[string], stdinData: Option[string], timeoutSec: int): ExecResult =
  var process = startOrRaise(argv)
  defer: process.close()

  if stdinData.isSome:
    process.inputStream.write(stdinData.get)
  process.inputStream.close()

  let
    outFd = process.outputHandle.cint
    errFd = process.errorHandle.cint
  setNonBlocking(outFd)
  setNonBlocking(errFd)

  var
    fds: array[2, TPollfd]
    open = [true, true]
    deadline = epochTime() + timeoutSec.float

  while open[0] or open[1]:
    let remaining = deadline - epochTime()
    if remaining <= 0:
      process.kill()
      discard process.waitForExit()
      result.timedOut = true
      return

    fds[0] = TPollfd(fd: outFd, events: POLLIN, revents: 0)
    fds[1] = TPollfd(fd: errFd, events: POLLIN, revents: 0)
    let ready = poll(addr fds[0], 2, cint(remaining * 1000))
    if ready < 0:
      if errno == EINTR:
        continue
      break
    if ready == 0:
      continue

    if open[0] and (fds[0].revents and (POLLIN or POLLHUP or POLLERR)) != 0:
      open[0] = drain(outFd, result.output)
    if open[1] and (fds[1].revents and (POLLIN or POLLHUP or POLLERR)) != 0:
      open[1] = drain(errFd, result.error)

  let waited = process.waitForExit(int((deadline - epochTime()) * 1000))
  if waited == -1:
    process.kill()
    discard process.waitForExit()
    result.timedOut = true
    return
  result.exitCode = waited
