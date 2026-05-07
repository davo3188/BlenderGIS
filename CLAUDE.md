# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

BlenderGIS is a Blender addon (v2.2.14, requires Blender 2.83+, compatible through 5.x) that adds GIS functionality to Blender: importing shapefiles, georasters, OpenStreetMap data, downloading DEM elevation, displaying web basemaps, and managing scene georeferencing. It is pure Python with no build system.

## Installation / Development

There is no build step. Development workflow:

1. Symlink or copy the repo folder into Blender's addon directory:
   - Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\BlenderGIS\`
2. In Blender: Edit > Preferences > Add-ons > search "BlenderGIS" > enable
3. To reload after code changes: use Blender's "Reload Scripts" (F8 or from the Info menu) or the [Blender Development addon](https://github.com/JacquesLucke/blender_development)
4. Logs are written to `~/.bgis/bgis.log`

There are no tests, no linter config, and no CI setup in this repo.

## Architecture

### Entry Point

`__init__.py` is the Blender addon entry point. Its `register()` / `unregister()` functions wire everything together: icons, preferences, menus, operators, and the keyboard shortcut (NUMPAD_ASTERIX opens the map viewer). Module-level boolean flags near the top (e.g., `IMPORT_SHP = True`) control which operators are registered.

### Top-Level Files

- `prefs.py` — Addon preferences UI and stored settings (CRS definitions, DEM/Overpass server lists, projection/image engine selection, OSM filter tags)
- `geoscene.py` — `GeoScene` class that manages the georeferencing state of the active scene. Maintains synchronization between WGS84 lat/lon, projected CRS coordinates, and the Blender scene origin. All operators that move or set the scene origin go through this.

### `core/` — Geospatial Engine

| Subpackage | Purpose |
|---|---|
| `proj/` | Coordinate reprojection. `SRS` parses spatial reference systems; `Reproj` transforms between them using GDAL, PyProj, EPSG.io, or a built-in engine selected automatically. `_get_reproj(src, dst)` is a module-level `@lru_cache(32)` factory — always use it via `reprojPt/reprojPts/reprojBbox` rather than instantiating `Reproj()` in a loop |
| `georaster/` | Reading and writing georeferenced raster images (GeoTIFF, etc.) |
| `basemaps/` | Web map tile fetching, tile matrix grids, GeoPackage tile cache |
| `maths/` | Interpolation algorithms (Akima, fill-nodata, k-means) |
| `utils/` | Geometry primitives (`XY`, `BBOX`), gradients, colors |
| `core/lib/` | Vendored libraries: `shapefile.py` (shapefile I/O), `Tyf/` (TIFF encoder), `imageio/` (multi-format image I/O) |
| `checkdeps.py` | Detects optional dependencies at import time: `HAS_GDAL`, `HAS_PYPROJ`, `HAS_PIL`, `HAS_IMGIO` |
| `settings.py` | Reads/writes `core/settings.json`; auto-selects best available projection and image engine |
| `errors.py` | Custom exceptions: `OverlapError`, `ReprojError`, `ApiKeyError` |

### `operators/` — Blender Operators

Each file implements one Blender operator (UI action):

| File | What it does |
|---|---|
| `io_import_shp.py` | Import ESRI Shapefile — **primary reference for new vector importers** |
| `io_import_gpkg.py` | Import GeoPackage (.gpkg) — GDAL/OGR path + SQLite3 fallback with custom WKB parser |
| `io_import_geojson.py` | Import GeoJSON (.geojson/.json) — stdlib `json` only, no dependencies |
| `io_import_wfs.py` | Import from WFS service — 3-operator chain, GetCapabilities, GetFeature→GeoJSON |
| `io_import_wms.py` | Import WMS imagery — flat georeferenced plane or drape texture on active mesh |
| `io_import_arcgis_rest.py` | Import from ArcGIS REST Feature Service — 3-operator chain, service metadata→layer picker→paginated GeoJSON fetch |
| `io_import_osm.py` | Import OpenStreetMap XML |
| `io_import_georaster.py` | Import georeferenced raster (DEM or texture) |
| `io_import_asc.py` | Import ESRI ASCII Grid |
| `io_export_shp.py` | Export to Shapefile |
| `io_get_dem.py` | Download DEM from OpenTopography / GMRT |
| `view3d_mapviewer.py` | Interactive web map viewer inside the 3D viewport |
| `io_import_custom_basemap.py` | Add a custom WMS/WMTS source to the map viewer — fetches GetCapabilities, persists in addon prefs |
| `nodes_terrain_analysis_builder.py` | Build terrain-analysis shader node tree |
| `nodes_terrain_analysis_reclassify.py` | Reclassify terrain via shader nodes |
| `add_camera_georef.py` | Set up camera for georeferenced rendering |
| `add_camera_exif.py` | Import geotagged photos as cameras |
| `mesh_delaunay_voronoi.py` | Delaunay triangulation / Voronoi diagram |
| `mesh_earth_sphere.py` | Convert lon/lat mesh to sphere |
| `object_drop.py` | Drop objects onto a terrain mesh |

### `operators/utils/` — Shared Operator Utilities

| File | What it does |
|---|---|
| `bgis_utils.py` | `getBBOX`, `adjust3Dview`, `DropToGround`, `placeObj`, etc. |
| `http.py` | `http_get(url)` and `format_http_error(e)` — **single shared HTTP helper used by all web-service operators**. Do not define `_http_get()` locally in new operators; import from here. |
| `georaster_utils.py` | Raster mesh/UV helpers |
| `delaunay_voronoi.py` | Triangulation algorithms |

### Dependency Strategy

Optional libraries (GDAL, PyProj, PIL) are detected at startup by `core/checkdeps.py`. `core/settings.py` picks the best available engine. Code that needs them checks the `HAS_*` flags and falls back gracefully. The `core/lib/` vendored libraries are always available.

## Key Conventions

- **Blender operator pattern**: Classes inherit from `bpy.types.Operator` with `bl_idname`, `bl_label`, `execute()`, and optionally `invoke()` / `draw()`. Operators that need a file dialog use `bpy.types.IMPORT_OT_` / `EXPORT_OT_` base classes.
- **Georeferencing state** lives in Blender scene custom properties, accessed via `GeoScene(bpy.context.scene)`. Always use `GeoScene` methods rather than touching scene properties directly.
- **CRS strings** follow EPSG codes (`"EPSG:4326"`, `"EPSG:3857"`) or Proj4 strings. The `SRS` class in `core/proj/` normalizes them.
- **OpenTopography DEM downloads** require an API key since 2022 — users must register and enter the key in addon preferences.

---

## Current Development Goal — New Format & Service Importers

The active task is implementing the ESRI File Geodatabase importer. All web-service importers are complete.

### Completed
- `operators/io_import_gpkg.py` — GeoPackage (.gpkg) ✓ registered
- `operators/io_import_geojson.py` — GeoJSON (.geojson/.json) ✓ registered
- `operators/io_import_wfs.py` — WFS service ✓ registered
- `operators/io_import_wms.py` — WMS imagery (flat plane / drape on mesh) ✓ registered
- `operators/io_import_custom_basemap.py` — Custom WMS/WMTS source for map viewer ✓ registered
- `operators/io_import_arcgis_rest.py` — ArcGIS REST Feature Service ✓ registered (`ARCGIS_REST = True`)

### Remaining
- `operators/io_import_gdb.py` — ESRI File Geodatabase (.gdb) importer

All formats must be registered in `__init__.py` following the exact same pattern used by `IMPORT_SHP`.

---

## Implementation Rules for New Importers

### 1. Mandatory reference — read this first
Before writing any code, always read `operators/io_import_shp.py` in full.
It is the canonical pattern for vector import in this codebase. Every structural decision (operator class layout, mesh construction, georeferencing, error handling) must mirror that file unless there is an explicit technical reason to deviate.

### 2. GDAL/OGR is the only permitted read backend
Both GPKG and GDB must be read exclusively via `osgeo.ogr`.
- Always guard with `HAS_GDAL` from `core/checkdeps.py`
- If GDAL is unavailable, call `self.report({'ERROR'}, ...)` and return `{'CANCELLED'}`
- Do not use fiona, geopandas, or any library not already present in Blender's Python

### 3. GDAL driver names (exact strings required)
```python
# GeoPackage
ogr.GetDriverByName("GPKG")

# ESRI File Geodatabase — requires GDAL built with OpenFileGDB or FileGDB driver
ogr.GetDriverByName("OpenFileGDB")   # preferred, read-only, no external dependency
ogr.GetDriverByName("FileGDB")       # fallback, requires Esri FileGDB API SDK
```
Always try `OpenFileGDB` first for .gdb; fall back to `FileGDB`; if neither is available report a clear error.

### 4. Multi-layer handling
Both formats are multi-layer containers. The operator must:
- On `invoke()`: open the datasource, enumerate layers via `ds.GetLayerCount()`, populate an `EnumProperty` with layer names
- Show a dialog (via `invoke()` calling `context.window_manager.invoke_props_dialog(self)`) so the user can select which layer to import
- On `execute()`: open the selected layer by name via `ds.GetLayerByName(self.layer_name)`

### 5. Geometry type support
Handle all OGR geometry types that `io_import_shp.py` already handles:
`wkbPoint`, `wkbMultiPoint`, `wkbLineString`, `wkbMultiLineString`, `wkbPolygon`, `wkbMultiPolygon`
Also handle their Z variants: `wkbPoint25D`, `wkbPolygon25D`, etc.
Unknown or unsupported geometry types must trigger a warning, not a crash.

### 6. CRS and georeferencing
- Read the layer CRS via `layer.GetSpatialRef()`
- Use `core/proj/srs.py SRS` class to parse it
- Use `GeoScene` from `geoscene.py` to set or verify the scene origin
- Reproject coordinates to the scene CRS using `core/proj/reproj.py Reproj` if they differ
- Logic is identical to what `io_import_shp.py` does — copy that logic directly

### 7. Attribute / field import
- Read feature fields via `feature.GetField(field_name)`
- Store them as Blender object custom properties (same approach as `io_import_shp.py`)
- Field names longer than 63 characters must be truncated (Blender property name limit)

### 8. Object and collection naming
- Name each Blender object after the layer name
- Place imported objects in a Blender collection named after the source filename (without extension)
- Create the collection if it does not exist

### 9. Registration in `__init__.py`
Add the new operators following the exact same pattern as `IMPORT_SHP`. Current flags already present:
```python
IMPORT_GPKG = True
IMPORT_GEOJSON = True
IMPORT_WFS = True
IMPORT_WMS = True
CUSTOM_BASEMAP = True
ARCGIS_REST = True
# IMPORT_GDB = True  ← pending
```
Each flag has a corresponding conditional import, `register()`, `unregister()`, and menu entry in `VIEW3D_MT_menu_gis_import.draw()`. Menu entries reuse the `"shp"` icon.

### 10. Error handling requirements
Every operator must handle these failure cases explicitly:
- File not found or unreadable
- GDAL driver not available
- Layer has zero features
- All features have null geometry
- CRS is undefined or unrecognizable
- Reprojection failure

Use `self.report({'WARNING'}, ...)` for non-fatal issues and `self.report({'ERROR'}, ...)` + `return {'CANCELLED'}` for fatal ones.

---

## Development Sequence

**Current status:** All web-service importers complete. Next: ESRI File Geodatabase.

1. Read `operators/io_import_shp.py` completely ✓
2. Read `core/checkdeps.py` to understand `HAS_GDAL` ✓
3. Read `core/proj/reproj.py` to understand `Reproj` ✓
4. Read the relevant section of `__init__.py` to understand operator registration ✓
5. Implement `operators/io_import_gpkg.py` ✓ DONE
6. Register GPKG in `__init__.py` and test in Blender ✓ DONE
7. Implement `operators/io_import_geojson.py` ✓ DONE
8. Register GeoJSON in `__init__.py` ✓ DONE
9. Implement `operators/io_import_wfs.py` ✓ DONE
10. Register WFS in `__init__.py` ✓ DONE
11. Implement `operators/io_import_wms.py` ✓ DONE
12. Register WMS in `__init__.py` ✓ DONE
13. Implement `operators/io_import_custom_basemap.py` ✓ DONE
14. Register custom basemap in `__init__.py` under GIS > Web geodata ✓ DONE
15. Implement `operators/io_import_arcgis_rest.py` ✓ DONE
16. Register ArcGIS REST in `__init__.py` ✓ DONE
17. Implement `operators/io_import_gdb.py`
18. Register GDB in `__init__.py` and test in Blender


