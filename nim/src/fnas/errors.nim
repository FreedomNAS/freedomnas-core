type
  ErrorKind* = enum
    ekOsNotSupported = "os_not_supported"
    ekTranslation = "translation"
    ekExecution = "execution"
    ekEngine = "engine"
    ekKey = "key"
    ekValue = "value"
    ekOsError = "os_error"

  FnasError* = object of CatchableError
    kind*: ErrorKind

proc raiseFnas*(kind: ErrorKind, msg: string) {.noreturn.} =
  var e = newException(FnasError, msg)
  e.kind = kind
  raise e
