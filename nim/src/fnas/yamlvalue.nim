## The value model config.yaml is read into and written from.
##
## Mappings keep insertion order: PyYAML dumps with sort_keys=False, so key
## order is part of the file and has to survive a load/save round trip.
##
## Timestamps are their own kind because PyYAML's safe_load turns an
## unquoted `2026-07-18 10:30:00` into a datetime and dumps it back
## unquoted — a plain string would come back quoted and change the file.

import std/[json, sequtils]

type
  ValKind* = enum
    vNull, vBool, vInt, vFloat, vStr, vTimestamp, vSeq, vMap

  Val* = ref object
    case kind*: ValKind
    of vNull: discard
    of vBool: boolVal*: bool
    of vInt: intVal*: BiggestInt
    of vFloat: floatVal*: float
    of vStr, vTimestamp: strVal*: string
    of vSeq: items*: seq[Val]
    of vMap: pairs*: seq[(string, Val)]

  ConfigValueError* = object of CatchableError

const TimestampMarker* = "__fnas_timestamp__"

proc newNull*(): Val = Val(kind: vNull)
proc newBool*(v: bool): Val = Val(kind: vBool, boolVal: v)
proc newInt*(v: BiggestInt): Val = Val(kind: vInt, intVal: v)
proc newFloat*(v: float): Val = Val(kind: vFloat, floatVal: v)
proc newStr*(v: string): Val = Val(kind: vStr, strVal: v)
proc newTimestamp*(v: string): Val = Val(kind: vTimestamp, strVal: v)
proc newSeq*(items: seq[Val] = @[]): Val = Val(kind: vSeq, items: items)
proc newMap*(): Val = Val(kind: vMap, pairs: @[])

proc contains*(v: Val, key: string): bool =
  v.kind == vMap and v.pairs.anyIt(it[0] == key)

proc `[]`*(v: Val, key: string): Val =
  for (k, value) in v.pairs:
    if k == key:
      return value
  raise newException(KeyError, "No key '" & key & "' in mapping")

proc get*(v: Val, key: string): Val =
  ## nil when absent, so callers can test without catching.
  if v == nil or v.kind != vMap:
    return nil
  for (k, value) in v.pairs:
    if k == key:
      return value
  nil

proc `[]=`*(v: Val, key: string, value: Val) =
  for i in 0 ..< v.pairs.len:
    if v.pairs[i][0] == key:
      v.pairs[i] = (key, value)
      return
  v.pairs.add (key, value)

proc delete*(v: Val, key: string) =
  for i in 0 ..< v.pairs.len:
    if v.pairs[i][0] == key:
      v.pairs.delete(i)
      return

proc copyVal*(v: Val): Val =
  if v == nil:
    return nil
  case v.kind
  of vNull: result = newNull()
  of vBool: result = newBool(v.boolVal)
  of vInt: result = newInt(v.intVal)
  of vFloat: result = newFloat(v.floatVal)
  of vStr: result = newStr(v.strVal)
  of vTimestamp: result = newTimestamp(v.strVal)
  of vSeq:
    var copied: seq[Val]
    for item in v.items:
      copied.add copyVal(item)
    result = newSeq(copied)
  of vMap:
    result = newMap()
    for (k, value) in v.pairs:
      result.pairs.add (k, copyVal(value))

proc `==`*(a, b: Val): bool =
  if a.isNil or b.isNil:
    return a.isNil and b.isNil
  if a.kind != b.kind:
    return false
  case a.kind
  of vNull: true
  of vBool: a.boolVal == b.boolVal
  of vInt: a.intVal == b.intVal
  of vFloat: a.floatVal == b.floatVal
  of vStr, vTimestamp: a.strVal == b.strVal
  of vSeq:
    a.items.len == b.items.len and (block:
      for i in 0 ..< a.items.len:
        if a.items[i] != b.items[i]: return false
      true)
  of vMap:
    a.pairs.len == b.pairs.len and (block:
      for i in 0 ..< a.pairs.len:
        if a.pairs[i][0] != b.pairs[i][0]: return false
        if a.pairs[i][1] != b.pairs[i][1]: return false
      true)

# ---------------------------------------------------------------------------
# JSON bridge
#
# JSON has no timestamp, so one crosses as {"__fnas_timestamp__": "..."} and
# config_native.py turns it back into a datetime — the object PyYAML would
# have produced.
# ---------------------------------------------------------------------------

proc toJson*(v: Val): JsonNode =
  if v == nil:
    return newJNull()
  case v.kind
  of vNull: result = newJNull()
  of vBool: result = newJBool(v.boolVal)
  of vInt: result = newJInt(v.intVal)
  of vFloat: result = newJFloat(v.floatVal)
  of vStr: result = newJString(v.strVal)
  of vTimestamp:
    result = newJObject()
    result[TimestampMarker] = newJString(v.strVal)
  of vSeq:
    result = newJArray()
    for item in v.items:
      result.add toJson(item)
  of vMap:
    result = newJObject()
    for (k, value) in v.pairs:
      result[k] = toJson(value)

proc fromJson*(node: JsonNode): Val =
  if node == nil:
    return newNull()
  case node.kind
  of JNull: result = newNull()
  of JBool: result = newBool(node.getBool)
  of JInt: result = newInt(node.getBiggestInt)
  of JFloat: result = newFloat(node.getFloat)
  of JString: result = newStr(node.getStr)
  of JArray:
    var items: seq[Val]
    for item in node.items:
      items.add fromJson(item)
    result = newSeq(items)
  of JObject:
    if node.len == 1 and node.hasKey(TimestampMarker):
      return newTimestamp(node[TimestampMarker].getStr)
    result = newMap()
    for key, value in node.pairs:
      result.pairs.add (key, fromJson(value))

proc typeName*(v: Val): string =
  ## The Python type name, for messages that quote it.
  if v == nil: return "NoneType"
  case v.kind
  of vNull: "NoneType"
  of vBool: "bool"
  of vInt: "int"
  of vFloat: "float"
  of vStr: "str"
  of vTimestamp: "datetime"
  of vSeq: "list"
  of vMap: "dict"
