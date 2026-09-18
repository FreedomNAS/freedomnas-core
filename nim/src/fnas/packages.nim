## FreedomNAS Package Manager — load and query OS-specific package
## definitions from freedomnas-profiles/packages/.
##
## Read-only: MANIFEST.json lists the categories, each category's own JSON
## file holds its per-edition packages/services/pkgbuilds. Resolving the
## default packages directory stays in Python — same reasoning as the
## profile search paths in engine_native.py: it depends on where the
## *Python module* lives, which has no Nim equivalent.

import std/[json, os, tables]
import ./errors

type PackageManager* = ref object
  packagesDir*: string
  manifest*: JsonNode
  categories: Table[string, JsonNode]

proc newPackageManager*(packagesDir: string): PackageManager =
  if not dirExists(packagesDir):
    raiseFnas(ekOsError, "Packages directory not found: " & packagesDir)
  let manifestPath = packagesDir / "MANIFEST.json"
  if not fileExists(manifestPath):
    raiseFnas(ekOsError, "MANIFEST.json not found: " & manifestPath)
  result = PackageManager(
    packagesDir: packagesDir,
    manifest: parseJson(readFile(manifestPath)),
    categories: initTable[string, JsonNode](),
  )

proc categoriesList(pm: PackageManager): JsonNode =
  let c = pm.manifest{"categories"}
  if c != nil and c.kind == JArray: c else: newJArray()

proc getCategories*(pm: PackageManager): JsonNode =
  ## All available categories with metadata.
  categoriesList(pm)

proc findCategoryInfo(pm: PackageManager, categoryId: string): JsonNode =
  for cat in categoriesList(pm):
    let id = cat{"id"}
    if id != nil and id.kind == JString and id.getStr == categoryId:
      return cat
  nil

proc getCategoryInfo*(pm: PackageManager, categoryId: string): JsonNode =
  let info = findCategoryInfo(pm, categoryId)
  if info == nil:
    raiseFnas(ekValue, "Category not found: " & categoryId)
  info

proc loadCategory(pm: PackageManager, categoryId: string): JsonNode =
  if pm.categories.hasKey(categoryId):
    return pm.categories[categoryId]

  let catInfo = findCategoryInfo(pm, categoryId)
  if catInfo == nil:
    raiseFnas(ekValue, "Category not found in manifest: " & categoryId)

  let fileNode = catInfo{"file"}
  if fileNode == nil or fileNode.kind != JString:
    raiseFnas(ekOsError, "Category '" & categoryId & "' has no 'file' entry in the manifest")

  let catPath = pm.packagesDir / fileNode.getStr
  if not fileExists(catPath):
    raiseFnas(ekOsError, "Category file not found: " & catPath)

  let data = parseJson(readFile(catPath))
  pm.categories[categoryId] = data
  data

proc jsonStrings(node: JsonNode): seq[string] =
  if node != nil and node.kind == JArray:
    for item in node.items:
      if item.kind == JString: result.add item.getStr

proc editionEntry(pm: PackageManager, categoryId, edition: string): JsonNode =
  let category = loadCategory(pm, categoryId)
  let entry = category{edition}
  if entry == nil:
    raiseFnas(ekValue, "Edition '" & edition & "' not supported in category '" & categoryId & "'")
  entry

proc editionValues(pm: PackageManager, categoryId, edition, key,
                   initSystem: string): JsonNode =
  ## The category's own `key` list for `edition`, plus its
  ## `init.<initSystem>.<key>` entries when an init system is given.
  let entry = editionEntry(pm, categoryId, edition)
  result = newJArray()
  let values = entry{key}
  if values != nil and values.kind == JArray:
    for v in values: result.add v
  if initSystem.len > 0:
    let initValues = entry{"init", initSystem, key}
    if initValues != nil and initValues.kind == JArray:
      for v in initValues: result.add v

proc isSupportedEdition(pm: PackageManager, edition: string): bool =
  let supported = pm.manifest{"supported_editions"}
  if supported == nil or supported.kind != JArray: return false
  for e in supported:
    if e.kind == JString and e.getStr == edition: return true
  false

proc getPackagesForOs*(pm: PackageManager, categoryId, edition: string,
                       initSystem = ""): seq[string] =
  if not isSupportedEdition(pm, edition):
    raiseFnas(ekValue, "Unsupported edition: " & edition)
  jsonStrings(editionValues(pm, categoryId, edition, "packages", initSystem))

proc getPkgbuildsForOs*(pm: PackageManager, categoryId, edition: string,
                        initSystem = ""): JsonNode =
  ## The `{name, git}` PKGBUILD repositories a category builds after its
  ## packages, as a JSON array.
  editionValues(pm, categoryId, edition, "pkgbuilds", initSystem)

proc getServicesForOs*(pm: PackageManager, categoryId, edition: string,
                       initSystem = ""): seq[string] =
  jsonStrings(editionValues(pm, categoryId, edition, "services", initSystem))

proc getEarlyServicesForOs*(pm: PackageManager, categoryId, edition: string,
                            initSystem = ""): seq[string] =
  jsonStrings(editionValues(pm, categoryId, edition, "early_services", initSystem))

proc getConfigPaths*(pm: PackageManager, categoryId, edition: string): seq[string] =
  jsonStrings(editionEntry(pm, categoryId, edition){"config_paths"})

proc getRequiredCategories*(pm: PackageManager): seq[string] =
  for cat in categoriesList(pm):
    let req = cat{"required"}
    let id = cat{"id"}
    if req != nil and req.kind == JBool and req.getBool and id != nil and id.kind == JString:
      result.add id.getStr

proc getOptionalCategories*(pm: PackageManager): seq[string] =
  for cat in categoriesList(pm):
    let req = cat{"required"}
    let id = cat{"id"}
    let isRequired = req != nil and req.kind == JBool and req.getBool
    if not isRequired and id != nil and id.kind == JString:
      result.add id.getStr

proc validateCategory*(pm: PackageManager, categoryId: string): bool =
  try:
    discard loadCategory(pm, categoryId)
    true
  except FnasError:
    false

proc validateEdition*(pm: PackageManager, edition: string): bool =
  isSupportedEdition(pm, edition)

proc getAllPackagesForOs*(pm: PackageManager, edition: string,
                          categories: seq[string]): JsonNode =
  ## `categories` — the categories to include; the caller resolves the
  ## "all optional categories" default, mirroring how the Python side does.
  result = newJObject()
  for categoryId in categories:
    var packages = newJArray()
    for pkg in getPackagesForOs(pm, categoryId, edition):
      packages.add %pkg
    result[categoryId] = packages
