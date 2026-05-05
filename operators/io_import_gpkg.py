# -*- coding:utf-8 -*-
import os, sys, struct, sqlite3
import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty
from bpy.types import Operator
import bmesh
import logging
log = logging.getLogger(__name__)

from ..geoscene import GeoScene, georefManagerLayout
from ..prefs import PredefCRS
from ..core import BBOX
from ..core.proj import Reproj
from ..core.checkdeps import HAS_GDAL
from ..core.utils import perf_clock

from .utils import adjust3Dview, getBBOX

PKG, SUBPKG = __package__.split('.', maxsplit=1)


# ---------------------------------------------------------------------------
# GDAL-only helpers (used exclusively by the OGR path)
# ---------------------------------------------------------------------------

def _crs_from_osr(osr_srs):
	"""Return a CRS string from an osr.SpatialReference: 'AUTH:CODE' or proj4 fallback."""
	auth_name = osr_srs.GetAuthorityName(None)
	auth_code = osr_srs.GetAuthorityCode(None)
	if auth_name and auth_code:
		return "{}:{}".format(auth_name, auth_code)
	return osr_srs.ExportToProj4()


def _ring_pts(ring):
	"""Return (xy_list, z_list) from an OGR LinearRing or LineString geometry."""
	n = ring.GetPointCount()
	xy = [(ring.GetX(i), ring.GetY(i)) for i in range(n)]
	zs = [ring.GetZ(i) for i in range(n)]
	return xy, zs


# ---------------------------------------------------------------------------
# SQLite3 / WKB helpers (used exclusively by the fallback path)
# ---------------------------------------------------------------------------

# Envelope size in bytes for each GPKG envelope type (flags bits 2-4).
# type 0=none(0 bytes), 1=XY(32), 2=XYZ(48), 3=XYM(48), 4=XYZM(64)
# WKB starts at byte 8 (fixed GPKG header) + envelope size.
_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def _read_wkb(data, pos):
	"""
	Parse one ISO WKB geometry starting at byte offset pos inside data.

	Returns (flat_type, has_z, parts, new_pos).

	flat_type  -- 1=Point  2=LineString  3=Polygon  4-6=Multi* (outer type only)
	has_z      -- True if any Z coordinates are present
	parts      -- list of dicts; Multi* types are decomposed into single-type parts:
	               {'type': 1, 'xy': [(x,y)],   'zs': [z]}
	               {'type': 2, 'xy': [(x,y),...], 'zs': [z,...]}
	               {'type': 3, 'rings': [{'xy': [(x,y),...], 'zs': [z,...]}, ...]}
	"""
	byte_order = data[pos]; pos += 1
	endian = '<' if byte_order == 1 else '>'

	geom_type = struct.unpack_from(endian + 'I', data, pos)[0]; pos += 4

	# Decode ISO WKB type + Z flag.
	# ISO WKB: 1-7 = 2D, 1001-1007 = Z, 2001-2007 = M, 3001-3007 = ZM.
	# Defensive: also handle EWKB-style 0x80000000 Z flag.
	has_z = False
	if geom_type > 3000:                  # ISO ZM
		flat = geom_type - 3000; has_z = True
	elif geom_type > 2000:                # ISO M (no Z)
		flat = geom_type - 2000
	elif geom_type > 1000:                # ISO Z
		flat = geom_type - 1000; has_z = True
	elif geom_type & 0x80000000:          # EWKB Z flag (defensive)
		flat = geom_type & 0x1FFFFFFF; has_z = True
	else:
		flat = geom_type

	fmt_xy  = endian + 'dd'
	fmt_xyz = endian + 'ddd'
	step    = 24 if has_z else 16   # bytes per coordinate tuple

	parts = []

	if flat == 1:  # Point
		vals = struct.unpack_from(fmt_xyz if has_z else fmt_xy, data, pos); pos += step
		parts.append({'type': 1,
					  'xy': [(vals[0], vals[1])],
					  'zs': [vals[2] if has_z else 0.0]})

	elif flat == 2:  # LineString
		n = struct.unpack_from(endian + 'I', data, pos)[0]; pos += 4
		xy, zs = [], []
		for _ in range(n):
			vals = struct.unpack_from(fmt_xyz if has_z else fmt_xy, data, pos); pos += step
			xy.append((vals[0], vals[1]))
			zs.append(vals[2] if has_z else 0.0)
		parts.append({'type': 2, 'xy': xy, 'zs': zs})

	elif flat == 3:  # Polygon
		n_rings = struct.unpack_from(endian + 'I', data, pos)[0]; pos += 4
		rings = []
		for _ in range(n_rings):
			n_pts = struct.unpack_from(endian + 'I', data, pos)[0]; pos += 4
			xy, zs = [], []
			for _ in range(n_pts):
				vals = struct.unpack_from(fmt_xyz if has_z else fmt_xy, data, pos); pos += step
				xy.append((vals[0], vals[1]))
				zs.append(vals[2] if has_z else 0.0)
			rings.append({'xy': xy, 'zs': zs})
		parts.append({'type': 3, 'rings': rings})

	elif flat in (4, 5, 6):  # Multi*: recurse into each sub-geometry
		n_geoms = struct.unpack_from(endian + 'I', data, pos)[0]; pos += 4
		for _ in range(n_geoms):
			sub_flat, sub_has_z, sub_parts, pos = _read_wkb(data, pos)
			# Propagate Z: if any sub declares Z, the whole feature is treated as Z
			has_z = has_z or sub_has_z
			parts.extend(sub_parts)

	# flat > 6 (GeometryCollection etc.): unsupported — return empty parts

	return flat, has_z, parts, pos


def _parse_gpkg_wkb(blob):
	"""
	Strip the GPKG geometry blob header and parse the embedded ISO WKB.

	Returns (flat_type, has_z, parts) on success, or None for empty/invalid blobs.
	parts has the same structure as documented in _read_wkb().
	"""
	try:
		if len(blob) < 8 or blob[0] != 0x47 or blob[1] != 0x50:   # magic 'GP'
			return None
		flags        = blob[3]
		# Step 1: try flags-based offset (spec-compliant files)
		envelope_type = (flags >> 2) & 0x07                        # bits 2-4
		wkb_start = 8 + _ENVELOPE_BYTES.get(envelope_type, 0)

		# Step 2: validate; if invalid, scan forward for real WKB start.
		# Handles QGIS files that write non-standard envelope sizes.
		if wkb_start >= len(blob) or blob[wkb_start] not in (0, 1):
			wkb_start = None
			for i in range(8, min(len(blob) - 5, 200)):
				if blob[i] not in (0, 1):
					continue
				endian_test = '<' if blob[i] == 1 else '>'
				gt_test = struct.unpack_from(endian_test + 'I', blob, i + 1)[0]
				if gt_test > 3000:            gt_flat = gt_test - 3000
				elif gt_test > 2000:          gt_flat = gt_test - 2000
				elif gt_test > 1000:          gt_flat = gt_test - 1000
				elif gt_test & 0x80000000:    gt_flat = gt_test & 0x1FFFFFFF
				else:                         gt_flat = gt_test
				if 1 <= gt_flat <= 6:
					wkb_start = i
					break
			if wkb_start is None:
				log.warning("Cannot find valid WKB start in GPKG blob")
				return None
		if len(blob) <= wkb_start:
			return None
		flat, has_z, parts, _ = _read_wkb(blob, wkb_start)
		return (flat, has_z, parts)
	except (struct.error, IndexError, TypeError) as e:
		log.debug('_parse_gpkg_wkb failed (%s) on blob starting with %r', type(e).__name__, bytes(blob[:16]))
		return None


def _list_layers_sqlite(filepath):
	"""
	Return a list of feature layer names from a GPKG file using sqlite3.
	Works without GDAL; faster than opening an OGR datasource for the dialog callback.
	"""
	try:
		con = sqlite3.connect(filepath)
		rows = con.execute(
			"SELECT table_name FROM gpkg_contents "
			"WHERE data_type = 'features' ORDER BY table_name"
		).fetchall()
		con.close()
		return [r[0] for r in rows]
	except Exception as e:
		log.warning("Cannot list GPKG layers via SQLite3: %s", e)
		return []


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_gpkg_file_dialog(Operator):
	"""Select a GeoPackage file, then open the layer and options dialog"""

	bl_idname = "importgis.gpkg_file_dialog"
	bl_description = "Import GeoPackage vector layer (.gpkg)"
	bl_label = "Import GPKG"
	bl_options = {'INTERNAL'}

	filepath: StringProperty(
		name="File Path",
		maxlen=1024,
		subtype='FILE_PATH')

	filename_ext = ".gpkg"

	filter_glob: StringProperty(
		default="*.gpkg",
		options={'HIDDEN'})

	def invoke(self, context, event):
		context.window_manager.fileselect_add(self)
		return {'RUNNING_MODAL'}

	def draw(self, context):
		layout = self.layout
		layout.label(text="Options available after")
		layout.label(text="selecting a file")

	def execute(self, context):
		if not os.path.exists(self.filepath):
			self.report({'ERROR'}, "Invalid filepath")
			return {'CANCELLED'}
		bpy.ops.importgis.gpkg_import('INVOKE_DEFAULT', filepath=self.filepath)
		return {'FINISHED'}


class IMPORTGIS_OT_gpkg_import(Operator):
	"""Layer selection, options, and import for a GeoPackage file"""

	bl_idname = "importgis.gpkg_import"
	bl_description = "Import GeoPackage vector layer (.gpkg)"
	bl_label = "Import GPKG"
	bl_options = {"UNDO"}

	filepath: StringProperty()

	# Force dialog redraw when properties change
	def check(self, context):
		return True

	def listLayers(self, context):
		"""Always uses sqlite3 — no GDAL required, no datasource left open."""
		if not self.filepath or not os.path.exists(self.filepath):
			return [('NONE', '(no layers)', '')]
		names = _list_layers_sqlite(self.filepath)
		return [(n, n, '') for n in names] if names else [('NONE', '(no layers found)', '')]

	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	layerName: EnumProperty(
		name="Layer",
		description="Select the layer to import",
		items=listLayers)

	reprojection: BoolProperty(
		name="Specify layer CRS",
		description="Override the CRS detected from the layer metadata",
		default=False)

	layerCRSOverride: EnumProperty(
		name="Layer CRS",
		description="Coordinate Reference System to use for this layer",
		items=listPredefCRS)

	elevSource: EnumProperty(
		name="Elevation source",
		description="Source of vertex Z values",
		items=[
			('NONE', 'None', "Flat geometry, Z set to zero"),
			('GEOM', 'Geometry', "Use Z values from geometry when present"),
		],
		default='GEOM')

	separateObjects: BoolProperty(
		name="Separate objects",
		description="Create one Blender object per feature (can be slow for large layers)",
		default=False)

	def _crs_layout(self, layout):
		row = layout.row(align=True)
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, 'layerCRSOverride', text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')

	def draw(self, context):
		layout = self.layout

		engine = "GDAL (full)" if HAS_GDAL else "SQLite3 (basic, no reprojection)"
		layout.label(text="Engine: " + engine, icon='INFO')

		layout.prop(self, 'layerName')
		layout.prop(self, 'elevSource')
		layout.prop(self, 'separateObjects')

		geoscn = GeoScene()
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self._crs_layout(layout)
			georefManagerLayout(self, context)
		else:
			# Scene not yet georef'd: CRS auto-detected from layer, but user can override
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self._crs_layout(layout)

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def execute(self, context):

		prefs = bpy.context.preferences.addons[PKG].preferences

		w = context.window
		w.cursor_set('WAIT')
		t0 = perf_clock()

		bpy.ops.object.select_all(action='DESELECT')

		gpkgName = os.path.splitext(os.path.basename(self.filepath))[0]

		# ===================================================================
		# GDAL / OGR path  (unchanged from original implementation)
		# ===================================================================
		if HAS_GDAL:

			from osgeo import ogr

			# --- Open datasource ---
			ds = ogr.Open(self.filepath, 0)
			if ds is None:
				self.report({'ERROR'}, "Cannot open: {}".format(self.filepath))
				return {'CANCELLED'}

			if self.layerName == 'NONE':
				self.report({'ERROR'}, "No layer selected")
				ds = None
				return {'CANCELLED'}

			layer = ds.GetLayerByName(self.layerName)
			if layer is None:
				self.report({'ERROR'}, "Layer '{}' not found".format(self.layerName))
				ds = None
				return {'CANCELLED'}

			nbFeats = layer.GetFeatureCount()
			if nbFeats == 0:
				self.report({'ERROR'}, "Layer '{}' contains no features".format(self.layerName))
				ds = None
				return {'CANCELLED'}

			# --- GeoScene check ---
			geoscn = GeoScene()
			if geoscn.isBroken:
				self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
				ds = None
				return {'CANCELLED'}

			# --- Determine layer CRS ---
			osr_srs = layer.GetSpatialRef()

			if geoscn.isGeoref:
				if self.reprojection:
					layerCRS = self.layerCRSOverride
				elif osr_srs is not None:
					layerCRS = _crs_from_osr(osr_srs)
				else:
					log.warning("Layer has no CRS; assuming scene CRS (no reprojection)")
					layerCRS = geoscn.crs
			else:
				# Scene not yet georef'd
				if osr_srs is not None and not self.reprojection:
					layerCRS = _crs_from_osr(osr_srs)
				else:
					layerCRS = self.layerCRSOverride

			if not geoscn.hasCRS:
				try:
					geoscn.crs = layerCRS
				except Exception as e:
					log.error("Cannot set scene CRS", exc_info=True)
					self.report({'ERROR'}, "Cannot set scene CRS, check logs")
					ds = None
					return {'CANCELLED'}

			# --- Init reprojector if needed ---
			rprj = None
			if geoscn.crs != layerCRS:
				log.info("Reprojecting from %s to %s", layerCRS, geoscn.crs)
				try:
					rprj = Reproj(layerCRS, geoscn.crs)
				except Exception as e:
					log.error("Reprojection init failed", exc_info=True)
					self.report({'ERROR'}, "Cannot initialize reprojection, check logs")
					ds = None
					return {'CANCELLED'}
				if rprj.iproj == 'EPSGIO' and nbFeats > 100:
					self.report({'ERROR'},
						"Reprojection via EPSG.io is limited to 100 features. "
						"Please install GDAL or PyProj.")
					ds = None
					return {'CANCELLED'}

			# --- Compute bounding box ---
			# OGR GetExtent() returns (xmin, xmax, ymin, ymax)
			ext = layer.GetExtent()
			bbox = BBOX(ext[0], ext[2], ext[1], ext[3])
			if rprj is not None:
				bbox = rprj.bbox(bbox)

			# --- Set or retrieve scene origin ---
			if not geoscn.isGeoref:
				dx, dy = bbox.center
				geoscn.setOriginPrj(dx, dy)
			else:
				dx, dy = geoscn.getOriginPrj()

			# --- Geometry type info ---
			raw_layer_type = layer.GetGeomType()
			layer_flat_type = ogr.GT_Flatten(raw_layer_type)
			log.info("Layer '%s': %d features, geometry type: %s",
					 self.layerName, nbFeats, ogr.GeometryTypeToName(raw_layer_type))

			POINT_TYPES = {ogr.wkbPoint, ogr.wkbMultiPoint}
			LINE_TYPES  = {ogr.wkbLineString, ogr.wkbMultiLineString}
			POLY_TYPES  = {ogr.wkbPolygon, ogr.wkbMultiPolygon}
			SUPPORTED   = POINT_TYPES | LINE_TYPES | POLY_TYPES

			if layer_flat_type not in SUPPORTED and layer_flat_type != ogr.wkbUnknown:
				self.report({'ERROR'},
					"Unsupported geometry type: {}".format(ogr.GeometryTypeToName(layer_flat_type)))
				ds = None
				return {'CANCELLED'}

			# --- Field definitions ---
			layer_defn = layer.GetLayerDefn()
			field_count = layer_defn.GetFieldCount()
			field_names = [layer_defn.GetFieldDefn(i).GetName() for i in range(field_count)]

			# --- Build mesh ---
			bm = bmesh.new()
			collection = None
			if self.separateObjects:
				collection = bpy.data.collections.new(self.layerName)
				context.scene.collection.children.link(collection)

			progress = -1
			null_geom_count = 0
			valid_feat_count = 0

			layer.ResetReading()
			for feat_i, feature in enumerate(layer):

				pourcent = round(((feat_i + 1) * 100) / nbFeats)
				if pourcent in range(0, 110, 10) and pourcent != progress:
					progress = pourcent
					if pourcent == 100:
						print(str(pourcent) + '%')
					else:
						print(str(pourcent), end="%, ")
					sys.stdout.flush()

				geom = feature.GetGeometryRef()
				if geom is None:
					null_geom_count += 1
					continue

				ftype_raw = geom.GetGeometryType()
				ftype = ogr.GT_Flatten(ftype_raw)
				fhas_z = ogr.GT_HasZ(ftype_raw)

				# Decompose Multi* into individual sub-geometries
				if ftype == ogr.wkbMultiPoint:
					sub_geoms = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
					sub_ftype = ogr.wkbPoint
				elif ftype == ogr.wkbMultiLineString:
					sub_geoms = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
					sub_ftype = ogr.wkbLineString
				elif ftype == ogr.wkbMultiPolygon:
					sub_geoms = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
					sub_ftype = ogr.wkbPolygon
				elif ftype in (ogr.wkbPoint, ogr.wkbLineString, ogr.wkbPolygon):
					sub_geoms = [geom]
					sub_ftype = ftype
				else:
					log.warning("Feature %d: unsupported geometry type %s, skipping",
								feat_i, ogr.GeometryTypeToName(ftype))
					continue

				for sub in sub_geoms:

					if sub_ftype == ogr.wkbPoint:
						xy = [(sub.GetX(), sub.GetY())]
						zs = [sub.GetZ()]
						if rprj:
							xy = rprj.pts(xy)
						z = zs[0] if (self.elevSource == 'GEOM' and fhas_z) else 0.0
						bm.verts.new((xy[0][0] - dx, xy[0][1] - dy, z))

					elif sub_ftype == ogr.wkbLineString:
						raw_xy, zs = _ring_pts(sub)
						if not raw_xy:
							continue
						if rprj:
							raw_xy = rprj.pts(raw_xy)
						use_z = self.elevSource == 'GEOM' and fhas_z
						pts3d = [(x - dx, y - dy, z if use_z else 0.0)
								 for (x, y), z in zip(raw_xy, zs)]
						verts = [bm.verts.new(p) for p in pts3d]
						for i in range(len(verts) - 1):
							try:
								bm.edges.new([verts[i], verts[i + 1]])
							except ValueError:
								log.warning("Feature %d: duplicate edge %d-%d, skipping", feat_i, i, i + 1)

					elif sub_ftype == ogr.wkbPolygon:
						# Ring 0 = exterior ring; rings 1+ = holes (holes not supported, matches SHP behaviour)
						for r_i in range(sub.GetGeometryCount()):
							ring = sub.GetGeometryRef(r_i)
							raw_xy, zs = _ring_pts(ring)
							# OGR rings are closed (last pt == first); need ≥4 pts for a valid triangle
							if len(raw_xy) < 4:
								continue
							if rprj:
								raw_xy = rprj.pts(raw_xy)
							use_z = self.elevSource == 'GEOM' and fhas_z
							pts3d = [(x - dx, y - dy, z if use_z else 0.0)
									 for (x, y), z in zip(raw_xy, zs)]
							# WKB/OGC exterior rings are CCW; just drop the closing duplicate point
							pts3d.pop()
							if len(pts3d) < 3:
								continue
							verts = [bm.verts.new(p) for p in pts3d]
							try:
								face = bm.faces.new(verts)
								face.normal_update()
							except ValueError:
								log.warning("Feature %d ring %d: degenerate face, skipping", feat_i, r_i)

				valid_feat_count += 1

				# --- Per-feature object (separateObjects mode) ---
				if self.separateObjects:
					feat_bbox = getBBOX.fromBmesh(bm)
					ox, oy, oz = feat_bbox.center
					oz = feat_bbox.zmin
					bmesh.ops.translate(bm, verts=bm.verts, vec=(-ox, -oy, -oz))

					mesh = bpy.data.meshes.new(self.layerName)
					bm.to_mesh(mesh)
					bm.clear()
					mesh.validate(verbose=False)

					obj = bpy.data.objects.new(self.layerName, mesh)
					collection.objects.link(obj)
					context.view_layer.objects.active = obj
					obj.select_set(True)
					obj.location = (ox, oy, oz)
					obj.show_wire = (len(mesh.edges) > 0 and len(mesh.polygons) == 0)

					for field_i in range(field_count):
						prop_name = field_names[field_i][:63]
						val = feature.GetField(field_i)
						if val is not None:
							obj[prop_name] = val

			# Close datasource
			ds = None

			if null_geom_count:
				log.warning("%d features had null geometry and were skipped", null_geom_count)
				self.report({'WARNING'}, "{} features skipped (null geometry)".format(null_geom_count))

			if valid_feat_count == 0:
				bm.free()
				self.report({'ERROR'}, "No valid geometry found in layer '{}'".format(self.layerName))
				return {'CANCELLED'}

			# --- Write combined mesh (non-separate mode) ---
			if not self.separateObjects:
				if prefs.mergeDoubles:
					bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)

				mesh = bpy.data.meshes.new(self.layerName)
				bm.to_mesh(mesh)
				mesh.validate(verbose=False)

				obj = bpy.data.objects.new(self.layerName, mesh)
				context.scene.collection.objects.link(obj)
				context.view_layer.objects.active = obj
				obj.select_set(True)
				bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')
				obj.show_wire = (len(mesh.edges) > 0 and len(mesh.polygons) == 0)

			bm.free()

			t = perf_clock() - t0
			log.info("GPKG import completed in %.3f seconds", t)

			if prefs.adjust3Dview:
				bbox.shift(-dx, -dy)
				adjust3Dview(context, bbox)

			return {'FINISHED'}

		# ===================================================================
		# SQLite3 fallback path  (no GDAL required; no reprojection)
		# ===================================================================

		con = sqlite3.connect(self.filepath)

		# --- Geometry column ---
		row = con.execute(
			"SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?",
			(self.layerName,)
		).fetchone()
		if row is None:
			con.close()
			self.report({'ERROR'}, "No geometry column found for layer '{}'".format(self.layerName))
			return {'CANCELLED'}
		geom_col = row[0]

		# --- Layer CRS from gpkg tables ---
		row = con.execute(
			"SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
			(self.layerName,)
		).fetchone()
		srs_id = row[0] if row else None

		layerCRS = None
		if srs_id is not None:
			row = con.execute(
				"SELECT organization, organization_coordsys_id "
				"FROM gpkg_spatial_ref_sys WHERE srs_id = ?",
				(srs_id,)
			).fetchone()
			if row and row[0] and row[1] is not None:
				layerCRS = "{}:{}".format(row[0].upper(), row[1])

		if layerCRS is None:
			log.warning("Cannot determine CRS for layer '%s'", self.layerName)

		# --- Feature count ---
		safe_layer = self.layerName.replace('"', '""')
		safe_geom  = geom_col.replace('"', '""')

		nbFeats = con.execute(
			'SELECT COUNT(*) FROM "{}"'.format(safe_layer)
		).fetchone()[0]
		if nbFeats == 0:
			con.close()
			self.report({'ERROR'}, "Layer '{}' contains no features".format(self.layerName))
			return {'CANCELLED'}

		# --- GeoScene check ---
		geoscn = GeoScene()
		if geoscn.isBroken:
			con.close()
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		# Resolve final CRS: auto-detected wins; user override applies when reprojection=True
		if layerCRS is None:
			layerCRS = geoscn.crs if geoscn.hasCRS else self.layerCRSOverride
		elif self.reprojection:
			layerCRS = self.layerCRSOverride

		if not geoscn.hasCRS:
			try:
				geoscn.crs = layerCRS
			except Exception as e:
				log.error("Cannot set scene CRS", exc_info=True)
				con.close()
				self.report({'ERROR'}, "Cannot set scene CRS, check logs")
				return {'CANCELLED'}

		# --- CRS mismatch: warn and continue without reprojection ---
		if layerCRS and geoscn.crs != layerCRS:
			log.warning("Layer CRS %s != scene CRS %s; SQLite3 path imports without reprojection",
						layerCRS, geoscn.crs)
			self.report({'WARNING'},
				"Layer CRS ({}) differs from scene CRS ({}). "
				"Coordinates imported without reprojection — install GDAL for full CRS support.".format(
					layerCRS, geoscn.crs))

		# --- Bounding box from gpkg_contents ---
		ext_row = con.execute(
			"SELECT min_x, min_y, max_x, max_y FROM gpkg_contents WHERE table_name = ?",
			(self.layerName,)
		).fetchone()

		if ext_row and all(v is not None for v in ext_row):
			bbox = BBOX(ext_row[0], ext_row[1], ext_row[2], ext_row[3])
		else:
			bbox = None
			log.warning("No extent in gpkg_contents for '%s'; origin set to (0,0) and "
						"3D view adjustment skipped", self.layerName)

		# --- Set or retrieve scene origin ---
		if not geoscn.isGeoref:
			dx, dy = bbox.center if bbox is not None else (0.0, 0.0)
			geoscn.setOriginPrj(dx, dy)
		else:
			dx, dy = geoscn.getOriginPrj()

		# --- Field columns (all non-geometry columns) ---
		cols_info = con.execute(
			'PRAGMA table_info("{}")'.format(safe_layer)
		).fetchall()
		# cols_info rows: (cid, name, type, notnull, dflt_value, pk)
		field_cols = [c[1] for c in cols_info if c[1].lower() != geom_col.lower()]
		field_count = len(field_cols)

		# --- Feature query ---
		geom_q   = '"{}"'.format(safe_geom)
		fields_q = ', '.join('"{}"'.format(f) for f in field_cols)
		query    = ('SELECT {}, {} FROM "{}"'.format(geom_q, fields_q, safe_layer)
					if fields_q else
					'SELECT {} FROM "{}"'.format(geom_q, safe_layer))

		log.info("Layer '%s' (SQLite3): %d features", self.layerName, nbFeats)

		# --- Build mesh ---
		bm = bmesh.new()
		collection = None
		if self.separateObjects:
			collection = bpy.data.collections.new(self.layerName)
			context.scene.collection.children.link(collection)

		progress = -1
		null_geom_count = 0
		valid_feat_count = 0

		for feat_i, row in enumerate(con.execute(query)):

			pourcent = round(((feat_i + 1) * 100) / nbFeats)
			if pourcent in range(0, 110, 10) and pourcent != progress:
				progress = pourcent
				if pourcent == 100:
					print(str(pourcent) + '%')
				else:
					print(str(pourcent), end="%, ")
				sys.stdout.flush()

			blob = row[0]
			if not blob:
				null_geom_count += 1
				continue

			parsed = _parse_gpkg_wkb(bytes(blob))
			if parsed is None:
				null_geom_count += 1
				continue

			flat_type, has_z, parts = parsed

			if not parts:
				log.warning("Feature %d: unsupported or empty geometry (type %d), skipping",
							feat_i, flat_type)
				null_geom_count += 1
				continue

			for part in parts:
				pt = part['type']

				if pt == 1:  # Point
					xy, zs = part['xy'], part['zs']
					z = zs[0] if (self.elevSource == 'GEOM' and has_z) else 0.0
					bm.verts.new((xy[0][0] - dx, xy[0][1] - dy, z))

				elif pt == 2:  # LineString
					raw_xy, zs = part['xy'], part['zs']
					if not raw_xy:
						continue
					use_z = self.elevSource == 'GEOM' and has_z
					pts3d = [(x - dx, y - dy, z if use_z else 0.0)
							 for (x, y), z in zip(raw_xy, zs)]
					verts = [bm.verts.new(p) for p in pts3d]
					for i in range(len(verts) - 1):
						try:
							bm.edges.new([verts[i], verts[i + 1]])
						except ValueError:
							log.warning("Feature %d: duplicate edge %d-%d, skipping", feat_i, i, i + 1)

				elif pt == 3:  # Polygon
					for r_i, ring in enumerate(part['rings']):
						raw_xy, zs = ring['xy'], ring['zs']
						# WKB rings are closed (last pt == first); need ≥4 pts for a valid triangle
						if len(raw_xy) < 4:
							continue
						use_z = self.elevSource == 'GEOM' and has_z
						pts3d = [(x - dx, y - dy, z if use_z else 0.0)
								 for (x, y), z in zip(raw_xy, zs)]
						# WKB/OGC exterior rings are CCW; just drop the closing duplicate point
						pts3d.pop()
						if len(pts3d) < 3:
							continue
						verts = [bm.verts.new(p) for p in pts3d]
						try:
							face = bm.faces.new(verts)
							face.normal_update()
						except ValueError:
							log.warning("Feature %d ring %d: degenerate face, skipping", feat_i, r_i)

				else:
					log.warning("Feature %d: unsupported part type %d, skipping", feat_i, pt)

			valid_feat_count += 1

			# --- Per-feature object (separateObjects mode) ---
			if self.separateObjects:
				if not bm.verts:
					# Feature produced no geometry (e.g. all rings degenerate)
					bm.clear()
					continue
				feat_bbox = getBBOX.fromBmesh(bm)
				ox, oy, oz = feat_bbox.center
				oz = feat_bbox.zmin
				bmesh.ops.translate(bm, verts=bm.verts, vec=(-ox, -oy, -oz))

				mesh = bpy.data.meshes.new(self.layerName)
				bm.to_mesh(mesh)
				bm.clear()
				mesh.validate(verbose=False)

				obj = bpy.data.objects.new(self.layerName, mesh)
				collection.objects.link(obj)
				context.view_layer.objects.active = obj
				obj.select_set(True)
				obj.location = (ox, oy, oz)
				obj.show_wire = (len(mesh.edges) > 0 and len(mesh.polygons) == 0)

				for j in range(field_count):
					prop_name = field_cols[j][:63]
					val = row[j + 1]  # row[0] is the geometry blob
					if val is not None:
						obj[prop_name] = val

		con.close()

		if null_geom_count:
			log.warning("%d features had null/invalid geometry and were skipped", null_geom_count)
			self.report({'WARNING'},
				"{} features skipped (null/invalid geometry)".format(null_geom_count))

		if valid_feat_count == 0:
			bm.free()
			self.report({'ERROR'}, "No valid geometry found in layer '{}'".format(self.layerName))
			return {'CANCELLED'}

		# --- Write combined mesh (non-separate mode) ---
		if not self.separateObjects:
			if prefs.mergeDoubles:
				bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)

			mesh = bpy.data.meshes.new(self.layerName)
			bm.to_mesh(mesh)
			mesh.validate(verbose=False)

			obj = bpy.data.objects.new(self.layerName, mesh)
			context.scene.collection.objects.link(obj)
			context.view_layer.objects.active = obj
			obj.select_set(True)
			bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')
			obj.show_wire = (len(mesh.edges) > 0 and len(mesh.polygons) == 0)

		bm.free()

		t = perf_clock() - t0
		log.info("GPKG import completed in %.3f seconds (SQLite3 path)", t)

		if prefs.adjust3Dview and bbox is not None:
			bbox.shift(-dx, -dy)
			adjust3Dview(context, bbox)

		return {'FINISHED'}


classes = [
	IMPORTGIS_OT_gpkg_file_dialog,
	IMPORTGIS_OT_gpkg_import,
]

def register():
	for cls in classes:
		try:
			bpy.utils.register_class(cls)
		except ValueError:
			bpy.utils.unregister_class(cls)
			bpy.utils.register_class(cls)

def unregister():
	for cls in classes:
		bpy.utils.unregister_class(cls)
