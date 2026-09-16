## Reading and writing config.yaml the way PyYAML does it.
##
## Loading resolves plain scalars with PyYAML's implicit rules (so `yes` is
## a bool and `12` an int, exactly as safe_load sees them) and keeps quoted
## scalars as strings. Parsing itself is NimYAML's, so hand-edited files
## with comments, flow style or block scalars still load.
##
## Dumping is a port of PyYAML's emitter for the subset a configuration
## uses: block style, indent 2, width 80, ASCII output, keys in insertion
## order. Byte-for-byte agreement with `yaml.dump(..., default_flow_style=
## False, sort_keys=False)` is what tests/test_config_parity.py checks, and
## it is what lets either engine rewrite a file the other one wrote.

import std/[strutils, tables, unicode]
import yaml/[data, parser, stream]
import std/streams
import ./errors
import ./yamlvalue

const
  BestWidth = 80
  BestIndent = 2
  NullWords = ["", "~", "null", "Null", "NULL"]
  BoolWords = ["yes", "Yes", "YES", "no", "No", "NO",
               "true", "True", "TRUE", "false", "False", "FALSE",
               "on", "On", "ON", "off", "Off", "OFF"]
  TrueWords = ["yes", "Yes", "YES", "true", "True", "TRUE", "on", "On", "ON"]

# ---------------------------------------------------------------------------
# Implicit resolution — PyYAML's rules, hand-written to avoid a regex library
# ---------------------------------------------------------------------------

proc allDigitsOrUnderscore(s: string, digits: set[char]): bool =
  if s.len == 0:
    return false
  for c in s:
    if c notin digits and c != '_':
      return false
  true

proc looksLikeInt(s: string): bool =
  var body = s
  if body.len > 0 and body[0] in {'-', '+'}:
    body = body[1 .. ^1]
  if body.len == 0:
    return false
  if body.startsWith("0b"):
    return allDigitsOrUnderscore(body[2 .. ^1], {'0', '1'})
  if body.startsWith("0x"):
    return allDigitsOrUnderscore(body[2 .. ^1], {'0' .. '9', 'a' .. 'f', 'A' .. 'F'})
  if body == "0":
    return true
  if body[0] == '0':
    return allDigitsOrUnderscore(body[1 .. ^1], {'0' .. '7'})
  if body[0] notin {'1' .. '9'}:
    return false
  if ':' in body:
    let parts = body.split(':')
    if not allDigitsOrUnderscore(parts[0], {'0' .. '9'}):
      return false
    for part in parts[1 .. ^1]:
      if part.len notin 1 .. 2 or not allDigitsOrUnderscore(part, {'0' .. '9'}):
        return false
      if part.len == 2 and part[0] notin {'0' .. '5'}:
        return false
    return true
  allDigitsOrUnderscore(body, {'0' .. '9'})

proc looksLikeFloat(s: string): bool =
  var body = s
  if body.len > 0 and body[0] in {'-', '+'}:
    body = body[1 .. ^1]
  if body in [".inf", ".Inf", ".INF"]:
    return true
  if s in [".nan", ".NaN", ".NAN"]:
    return true
  if ':' in body:
    let parts = body.split(':')
    if not allDigitsOrUnderscore(parts[0], {'0' .. '9'}):
      return false
    for part in parts[1 .. ^2]:
      if part.len notin 1 .. 2 or not allDigitsOrUnderscore(part, {'0' .. '9'}):
        return false
    let last = parts[^1]
    let dot = last.find('.')
    if dot < 0:
      return false
    return allDigitsOrUnderscore(last[0 ..< dot], {'0' .. '9'}) and
           (last.len == dot + 1 or allDigitsOrUnderscore(last[dot + 1 .. ^1], {'0' .. '9'}))

  if '.' notin body:
    return false

  var mantissa = body
  let ePos = body.find({'e', 'E'})
  if ePos >= 0:
    mantissa = body[0 ..< ePos]
    var exponent = body[ePos + 1 .. ^1]
    if exponent.len > 0 and exponent[0] in {'-', '+'}:
      exponent = exponent[1 .. ^1]
    if not allDigitsOrUnderscore(exponent, {'0' .. '9'}):
      return false

  let dot = mantissa.find('.')
  if dot < 0:
    return false
  let
    before = mantissa[0 ..< dot]
    after = mantissa[dot + 1 .. ^1]
  if before.len == 0:
    return allDigitsOrUnderscore(after, {'0' .. '9'})
  if not allDigitsOrUnderscore(before, {'0' .. '9'}):
    return false
  after.len == 0 or allDigitsOrUnderscore(after, {'0' .. '9'})

proc isDigits(s: string): bool =
  s.len > 0 and s.allCharsInSet({'0' .. '9'})

proc looksLikeTimestamp(s: string): bool =
  ## ymd, or ymd + separator + time, with optional fraction and zone.
  if s.len < 8:
    return false
  let datePart = s.split({'T', 't', ' ', '\t'}, maxsplit = 1)
  let ymd = datePart[0].split('-')
  if ymd.len != 3 or ymd[0].len != 4 or not isDigits(ymd[0]):
    return false
  if not isDigits(ymd[1]) or ymd[1].len notin 1 .. 2:
    return false
  if not isDigits(ymd[2]) or ymd[2].len notin 1 .. 2:
    return false
  if datePart.len == 1:
    return s.len == 10 and ymd[1].len == 2 and ymd[2].len == 2

  var rest = datePart[1].strip(trailing = false, chars = {' ', '\t'})
  if rest.len == 0:
    return false
  for zoneSep in ["Z", "+", "-"]:
    discard zoneSep
  var timePart = rest
  let zoneStart = rest.find({'Z', 'z', '+'}, start = 1)
  if zoneStart > 0:
    timePart = rest[0 ..< zoneStart]
  timePart = timePart.strip()
  let hms = timePart.split(':')
  if hms.len != 3:
    return false
  if not isDigits(hms[0]) or hms[0].len notin 1 .. 2: return false
  if not isDigits(hms[1]) or hms[1].len != 2: return false
  var seconds = hms[2]
  let dot = seconds.find('.')
  if dot >= 0:
    let fraction = seconds[dot + 1 .. ^1]
    if fraction.len > 0 and not isDigits(fraction):
      return false
    seconds = seconds[0 ..< dot]
  isDigits(seconds) and seconds.len == 2

proc resolvePlain*(content: string): Val =
  if content in NullWords:
    return newNull()
  if content in BoolWords:
    return newBool(content in TrueWords)
  if looksLikeInt(content):
    var text = content.replace("_", "")
    try:
      if text.len > 1 and text[0] == '+':
        text = text[1 .. ^1]
      if ':' in text:
        # Sexagesimal: PyYAML reads 12:30 as 750.
        var
          negative = text[0] == '-'
          digits = text.strip(chars = {'-', '+'}).split(':')
          total: BiggestInt = 0
        for part in digits:
          total = total * 60 + parseBiggestInt(part)
        return newInt(if negative: -total else: total)
      if text.toLowerAscii.contains("0b"):
        return newInt(parseBinInt(text.replace("0b", "")))
      if text.toLowerAscii.contains("0x"):
        return newInt(parseHexInt(text.replace("0x", "")))
      if text.len > 1 and text[0] == '0':
        return newInt(parseOctInt(text))
      return newInt(parseBiggestInt(text))
    except ValueError:
      return newStr(content)
  if looksLikeFloat(content):
    try:
      let text = content.replace("_", "")
      if ':' in text:
        var
          negative = text[0] == '-'
          total = 0.0
        for part in text.strip(chars = {'-', '+'}).split(':'):
          total = total * 60 + parseFloat(part)
        return newFloat(if negative: -total else: total)
      return newFloat(parseFloat(text))
    except ValueError:
      return newStr(content)
  if looksLikeTimestamp(content):
    return newTimestamp(content)
  newStr(content)

proc resolvesToStr*(content: string): bool =
  ## Would writing this text plain read back as the same string?
  resolvePlain(content).kind == vStr

# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

proc buildValue(s: YamlStream, first: Event): Val =
  case first.kind
  of yamlScalar:
    result =
      if first.scalarStyle == ssPlain:
        resolvePlain(first.scalarContent)
      else:
        newStr(first.scalarContent)
  of yamlStartSeq:
    result = newSeq()
    while true:
      let event = s.next()
      if event.kind == yamlEndSeq:
        break
      result.items.add buildValue(s, event)
  of yamlStartMap:
    result = newMap()
    while true:
      let event = s.next()
      if event.kind == yamlEndMap:
        break
      if event.kind != yamlScalar:
        raiseFnas(ekConfig, "Only string keys are supported in config.yaml")
      let value = s.next()
      result.pairs.add (event.scalarContent, buildValue(s, value))
  of yamlAlias:
    raiseFnas(ekConfig, "YAML anchors and aliases are not supported in config.yaml")
  else:
    raiseFnas(ekConfig, "Unexpected YAML event: " & $first.kind)

proc load*(text: string): Val =
  ## safe_load() of a single document; an empty document yields nil.
  var
    yamlParser = initYamlParser()
    stream = yamlParser.parse(newStringStream(text))
  try:
    for event in stream.items():
      case event.kind
      of yamlStartStream, yamlStartDoc: discard
      of yamlEndDoc, yamlEndStream: discard
      else:
        return buildValue(stream, event)
  except YamlParserError as exc:
    raiseFnas(ekConfig, exc.msg)
  except YamlStreamError as exc:
    raiseFnas(ekConfig, exc.msg)
  nil


# ---------------------------------------------------------------------------
# Dumping — a port of PyYAML's emitter for block style, ASCII, width 80
# ---------------------------------------------------------------------------

type
  ScalarAnalysis = object
    empty: bool
    multiline: bool
    allowBlockPlain: bool
    allowSingleQuoted: bool

  Emitter = object
    output: string
    column: int
    whitespace: bool
    indention: bool

const
  WhitespaceChars = {'\0', ' ', '\t', '\r', '\n'}
  EscapeReplacements = {
    '\0': "\\0", '\a': "\\a", '\b': "\\b", '\t': "\\t", '\n': "\\n",
    '\v': "\\v", '\f': "\\f", '\r': "\\r", '\e': "\\e",
    '"': "\\\"", '\\': "\\\\",
  }.toTable

proc analyzeScalar(scalar: string): ScalarAnalysis =
  if scalar.len == 0:
    return ScalarAnalysis(empty: true, multiline: false,
                          allowBlockPlain: true, allowSingleQuoted: true)

  var
    blockIndicators = false
    lineBreaks = false
    specialCharacters = false
    leadingSpace = false
    leadingBreak = false
    trailingSpace = false
    trailingBreak = false
    breakSpace = false
    spaceBreak = false
    previousSpace = false
    previousBreak = false
    precededByWhitespace = true

  if scalar.startsWith("---") or scalar.startsWith("..."):
    blockIndicators = true

  var followedByWhitespace = scalar.len == 1 or scalar[1] in WhitespaceChars

  for index, ch in scalar:
    if index == 0:
      if ch in {'#', ',', '[', ']', '{', '}', '&', '*', '!', '|', '>',
                '\'', '"', '%', '@', '`'}:
        blockIndicators = true
      if ch in {'?', ':'} and followedByWhitespace:
        blockIndicators = true
      if ch == '-' and followedByWhitespace:
        blockIndicators = true
    else:
      if ch == ':' and followedByWhitespace:
        blockIndicators = true
      if ch == '#' and precededByWhitespace:
        blockIndicators = true

    if ch == '\n':
      lineBreaks = true
    if not (ch == '\n' or (ch >= '\x20' and ch <= '\x7E')):
      specialCharacters = true

    if ch == ' ':
      if index == 0: leadingSpace = true
      if index == scalar.len - 1: trailingSpace = true
      if previousBreak: breakSpace = true
      previousSpace = true
      previousBreak = false
    elif ch == '\n':
      if index == 0: leadingBreak = true
      if index == scalar.len - 1: trailingBreak = true
      if previousSpace: spaceBreak = true
      previousSpace = false
      previousBreak = true
    else:
      previousSpace = false
      previousBreak = false

    precededByWhitespace = ch in WhitespaceChars
    followedByWhitespace = index + 2 >= scalar.len or scalar[index + 2] in WhitespaceChars

  result = ScalarAnalysis(empty: false, multiline: lineBreaks,
                          allowBlockPlain: true, allowSingleQuoted: true)

  if leadingSpace or leadingBreak or trailingSpace or trailingBreak:
    result.allowBlockPlain = false
  if breakSpace:
    result.allowBlockPlain = false
    result.allowSingleQuoted = false
  if spaceBreak or specialCharacters:
    result.allowBlockPlain = false
    result.allowSingleQuoted = false
  if lineBreaks:
    result.allowBlockPlain = false
  if blockIndicators:
    result.allowBlockPlain = false

proc write(e: var Emitter, data: string) =
  if data.len == 0:
    return
  e.output.add data
  e.column += data.runeLen
  e.whitespace = data[^1] == ' '
  e.indention = e.indention and data[^1] == ' '

proc writeLineBreak(e: var Emitter) =
  e.output.add "\n"
  e.column = 0
  e.whitespace = true
  e.indention = true

proc writeIndent(e: var Emitter, indent: int) =
  if not e.indention or e.column > indent or (e.column == indent and not e.whitespace):
    e.writeLineBreak()
  if e.column < indent:
    e.output.add spaces(indent - e.column)
    e.column = indent
    e.whitespace = true

proc writeBreakRun(e: var Emitter, run: string, indent: int) =
  if run[0] == '\n':
    e.writeLineBreak()
  for _ in run:
    e.writeLineBreak()
  e.writeIndent(indent)

proc writePlain(e: var Emitter, text: string, indent: int,
                leadingSpace: bool, split: bool) =
  if leadingSpace:
    e.write " "
  if text.len == 0:
    return

  var
    spacesRun = false
    breaks = false
    start = 0
    stop = 0
  while stop <= text.len:
    let ch = if stop < text.len: text[stop] else: '\0'
    if spacesRun:
      if stop == text.len or ch != ' ':
        if start + 1 == stop and e.column > BestWidth and split and
           start != 0 and stop != text.len:
          e.writeIndent(indent)
        else:
          e.write text[start ..< stop]
        start = stop
    elif breaks:
      if stop == text.len or ch != '\n':
        e.writeBreakRun(text[start ..< stop], indent)
        start = stop
    else:
      if stop == text.len or ch in {' ', '\n'}:
        e.write text[start ..< stop]
        start = stop
    if stop < text.len:
      spacesRun = ch == ' '
      breaks = ch == '\n'
    inc stop

proc writeSingleQuoted(e: var Emitter, text: string, indent: int,
                       leadingSpace: bool, split: bool) =
  if leadingSpace:
    e.write " "
  e.write "'"

  var
    spacesRun = false
    breaks = false
    start = 0
    stop = 0
  while stop <= text.len:
    let ch = if stop < text.len: text[stop] else: '\0'
    if spacesRun:
      if stop == text.len or ch != ' ':
        if start + 1 == stop and e.column > BestWidth and split and
           start != 0 and stop != text.len:
          e.writeIndent(indent)
        else:
          e.write text[start ..< stop]
        start = stop
    elif breaks:
      if stop == text.len or ch != '\n':
        e.writeBreakRun(text[start ..< stop], indent)
        start = stop
    else:
      if stop == text.len or ch in {' ', '\n', '\''}:
        if start < stop:
          e.write text[start ..< stop]
          start = stop
    if ch == '\'' and stop < text.len:
      e.write "''"
      start = stop + 1
    if stop < text.len:
      spacesRun = ch == ' '
      breaks = ch == '\n'
    inc stop

  e.write "'"

proc escapeRune(r: Rune): string =
  let code = int32(r)
  if code <= 0xFF:
    "\\x" & toHex(code, 2)
  elif code <= 0xFFFF:
    "\\u" & toHex(code, 4)
  else:
    "\\U" & toHex(code, 8)

proc writeDoubleQuoted(e: var Emitter, text: string, indent: int,
                       leadingSpace: bool, split: bool) =
  if leadingSpace:
    e.write " "
  e.write "\""

  let runes = text.toRunes
  var
    start = 0
    stop = 0
  while stop <= runes.len:
    var data = ""
    var escaped = false
    if stop < runes.len:
      let
        r = runes[stop]
        code = int32(r)
      if code == ord('"') or code == ord('\\') or code < 0x20 or code > 0x7E:
        escaped = true
        if code < 0x100 and EscapeReplacements.hasKey(char(code)):
          data = EscapeReplacements[char(code)]
        else:
          data = escapeRune(r)
    else:
      escaped = true

    if escaped:
      if start < stop:
        e.write $runes[start ..< stop]
        start = stop
      if stop < runes.len:
        e.write data
        start = stop + 1

    if split and 0 < stop and stop < runes.len - 1 and
       runes[stop] == Rune(' ') and e.column > BestWidth and start < stop:
      e.write $runes[start ..< stop]
      start = stop
      e.write "\\"
      e.writeIndent(indent)
      if runes[start] == Rune(' '):
        e.write "\\"
    inc stop

  e.write "\""

proc floatRepr(value: float): string =
  ## PyYAML writes repr(float), with `.0` inserted before a bare exponent.
  if value != value:
    return ".nan"
  if value == Inf:
    return ".inf"
  if value == NegInf:
    return "-.inf"
  result = $value
  if '.' notin result and 'e' in result:
    let e = result.find('e')
    result = result[0 ..< e] & ".0" & result[e .. ^1]

proc plainText(v: Val): string =
  case v.kind
  of vNull: "null"
  of vBool: (if v.boolVal: "true" else: "false")
  of vInt: $v.intVal
  of vFloat: floatRepr(v.floatVal)
  of vTimestamp, vStr: v.strVal
  else: ""

proc writeScalar(e: var Emitter, v: Val, indent: int,
                 leadingSpace: bool, split: bool) =
  let text = plainText(v)
  if v.kind != vStr:
    e.writePlain(text, indent, leadingSpace, split)
    return

  let analysis = analyzeScalar(text)
  if not analysis.empty and analysis.allowBlockPlain and resolvesToStr(text):
    e.writePlain(text, indent, leadingSpace, split)
  elif analysis.allowSingleQuoted:
    e.writeSingleQuoted(text, indent, leadingSpace, split)
  else:
    e.writeDoubleQuoted(text, indent, leadingSpace, split)

proc isEmptyCollection(v: Val): bool =
  (v.kind == vSeq and v.items.len == 0) or (v.kind == vMap and v.pairs.len == 0)

proc emitNode(e: var Emitter, v: Val, indent: int, sameLine: bool)

proc emitMapping(e: var Emitter, v: Val, indent: int, sameLine: bool) =
  for i, (key, value) in v.pairs:
    if i > 0 or not sameLine:
      e.writeIndent(indent)

    # PyYAML only writes `key:` for a simple key: short, non-empty and on
    # one line. Anything else gets the explicit `? key` / `: value` form.
    let analysis = analyzeScalar(key)
    if key.runeLen < 128 and not analysis.empty and not analysis.multiline:
      e.writeScalar(newStr(key), indent, leadingSpace = false, split = false)
    else:
      e.write "?"
      e.writeScalar(newStr(key), indent + BestIndent, leadingSpace = true, split = true)
      e.writeIndent(indent)
    e.write ":"
    if value.kind in {vSeq, vMap} and not isEmptyCollection(value):
      if value.kind == vMap:
        e.emitNode(value, indent + BestIndent, sameLine = false)
      else:
        e.emitNode(value, indent, sameLine = false)
    else:
      e.emitNode(value, indent + BestIndent, sameLine = true)

proc emitSequence(e: var Emitter, v: Val, indent: int, sameLine: bool) =
  for i, item in v.items:
    if i > 0 or not sameLine:
      e.writeIndent(indent)
    e.write "-"
    e.emitNode(item, indent + BestIndent, sameLine = true)

proc emitNode(e: var Emitter, v: Val, indent: int, sameLine: bool) =
  case v.kind
  of vSeq:
    if v.items.len == 0:
      e.writePlain("[]", indent, leadingSpace = sameLine, split = true)
    else:
      if sameLine:
        e.write " "
      e.emitSequence(v, indent, sameLine)
  of vMap:
    if v.pairs.len == 0:
      e.writePlain("{}", indent, leadingSpace = sameLine, split = true)
    else:
      if sameLine:
        e.write " "
      e.emitMapping(v, indent, sameLine)
  else:
    e.writeScalar(v, indent, leadingSpace = sameLine, split = true)

proc dump*(v: Val): string =
  ## yaml.dump(value, default_flow_style=False, sort_keys=False)
  var e = Emitter(output: "", column: 0, whitespace: true, indention: true)
  case v.kind
  of vMap:
    if v.pairs.len == 0:
      e.write "{}"
    else:
      e.emitMapping(v, 0, sameLine = true)
  of vSeq:
    if v.items.len == 0:
      e.write "[]"
    else:
      e.emitSequence(v, 0, sameLine = true)
  else:
    e.writeScalar(v, 0, leadingSpace = false, split = true)
  e.output & "\n"
