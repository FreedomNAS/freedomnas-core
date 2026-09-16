import ./errors

const Whitespace = {' ', '\t', '\r', '\n'}

proc split*(s: string): seq[string] =
  ## shlex.split() in POSIX mode, which is how the engine turns a formatted
  ## profile command into argv.
  var
    token = ""
    started = false
    i = 0
  while i < s.len:
    let c = s[i]
    if c in Whitespace:
      if started:
        result.add token
        token = ""
        started = false
      inc i
    elif c == '\'':
      started = true
      inc i
      var closed = false
      while i < s.len:
        if s[i] == '\'':
          closed = true
          inc i
          break
        token.add s[i]
        inc i
      if not closed:
        raiseFnas(ekValue, "No closing quotation")
    elif c == '"':
      started = true
      inc i
      var closed = false
      while i < s.len:
        if s[i] == '"':
          closed = true
          inc i
          break
        if s[i] == '\\' and i + 1 < s.len and s[i + 1] in {'"', '\\'}:
          token.add s[i + 1]
          i += 2
          continue
        token.add s[i]
        inc i
      if not closed:
        raiseFnas(ekValue, "No closing quotation")
    elif c == '\\':
      if i + 1 >= s.len:
        raiseFnas(ekValue, "No escaped character")
      started = true
      token.add s[i + 1]
      i += 2
    else:
      started = true
      token.add c
      inc i
  if started:
    result.add token
