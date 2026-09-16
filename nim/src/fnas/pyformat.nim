import std/tables
import ./errors

const Dangerous = {';', '|', '&', '$', '`', '\\', '\n', '\r'}

proc sanitize*(value: string): string =
  result = newStringOfCap(value.len)
  for c in value:
    if c notin Dangerous:
      result.add c

proc format*(tpl: string, values: Table[string, string]): string =
  ## str.format() restricted to named fields: what the JSON profiles use.
  result = newStringOfCap(tpl.len)
  var i = 0
  while i < tpl.len:
    let c = tpl[i]
    if c == '{':
      if i + 1 < tpl.len and tpl[i + 1] == '{':
        result.add '{'
        i += 2
        continue
      var j = i + 1
      while j < tpl.len and tpl[j] != '}':
        inc j
      if j >= tpl.len:
        raiseFnas(ekTranslation, "expected '}' before end of string: " & tpl)
      let field = tpl[i + 1 ..< j]
      if field.len == 0:
        raiseFnas(ekTranslation, "positional fields are not supported: " & tpl)
      for bad in [':', '!', '[', '.']:
        if bad in field:
          raiseFnas(ekTranslation,
            "unsupported format field '" & field & "' in: " & tpl)
      if field notin values:
        raiseFnas(ekKey, field)
      result.add values[field]
      i = j + 1
    elif c == '}':
      if i + 1 < tpl.len and tpl[i + 1] == '}':
        result.add '}'
        i += 2
        continue
      raiseFnas(ekTranslation, "single '}' encountered in format string: " & tpl)
    else:
      result.add c
      inc i
