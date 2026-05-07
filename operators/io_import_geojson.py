# -*- coding:utf-8 -*-
import os, sys, json
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
from ..core.utils import perf_clock

from .utils import adjust3Dview, getBBOX

PKG, SUBPKG = __package__.split('.', maxsplit=1)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _coords_to_xy_zs(coords):
	"""
	Convert a list of GeoJSON coordinate positions to (xy_list, z_list).
	Each position is [x, y] or [x, y, z].
	"""
	xy = [(c[0], c[1]) for c in coords]
	zs = [c[2] if len(c) >= 3 else 0.0 for c in coords]
	return xy, zs


def _has_z(coords):
	"""Return True if any coordinate position in a flat list has a Z value."""
	return any(len(c) >= 3 for c in coords)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_geojson_file_dialog(Operator):
	"""Select a GeoJSON file, then open the import options dialog"""

	bl_idname = "importgis.geojson_file_dialog"
	bl_description = "Import GeoJSON vector layer (.geojson, .json)"
	bl_label = "Import GeoJSON"
	bl_options = {'INTERNAL'}

	filepath: StringProperty(
		name="File Path",
		maxlen=1024,
		subtype='FILE_PATH')

	filename_ext = ".geojson"

	filter_glob: StringProperty(
		default="*.geojson;*.json",
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
		bpy.ops.importgis.geojson_import('INVOKE_DEFAULT', filepath=self.filepath)
		return {'FINISHED'}


class IMPORTGIS_OT_geojson_import(Operator):
	"""Import options and geometry builder for a GeoJSON file"""

	bl_idname = "importgis.geojson_import"
	bl_description = "Import GeoJSON vector layer (.geojson, .json)"
	bl_label = "Import GeoJSON"
	bl_options = {"UNDO"}

	filepath: StringProperty()

	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	reprojection: BoolProperty(
		name="Specify CRS",
		description=(
			"Override the default CRS (EPSG:4326). "
			"Use when the file uses a non-standard CRS."),
		default=False)

	layerCRSOverride: EnumProperty(
		name="Layer CRS",
		description="Coordinate Reference System of this file",
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
		description="Create one Blender object per feature (can be slow for large files)",
		default=False)

	def _crs_layout(self, layout):
		row = layout.row(align=True)
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, 'layerCRSOverride', text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')

	def draw(self, context):
		layout = self.layout

		layout.label(text="Engine: JSON (native)", icon='INFO')

		layout.prop(self, 'elevSource')
		layout.prop(self, 'separateObjects')

		geoscn = GeoScene()
		if geoscn.isPartiallyGeoref:
			layout.prop(self, 'reprojection')
			if self.reprojection:
				self._crs_layout(layout)
			georefManagerLayout(self, context)
		else:
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

		fileName = os.path.splitext(os.path.basename(self.filepath))[0]

		# --- Load JSON ---
		try:
			with open(self.filepath, 'r', encoding='utf-8') as f:
				gj = json.load(f)
		except (OSError, json.JSONDecodeError) as e:
			self.report({'ERROR'}, "Cannot read file: {}".format(e))
			return {'CANCELLED'}

		if gj.get('type') != 'FeatureCollection':
			self.report({'ERROR'}, "File is not a GeoJSON FeatureCollection")
			return {'CANCELLED'}

		features = gj.get('features', [])
		if not features:
			self.report({'ERROR'}, "FeatureCollection contains no features")
			return {'CANCELLED'}

		nbFeats = len(features)

		# --- GeoScene check ---
		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		# --- Determine layer CRS ---
		# Try to detect from the legacy "crs" member when no override is requested.
		import re
		crs_member = gj.get('crs')
		detected_crs = None
		if crs_member and crs_member.get('type') == 'name':
			crs_name = crs_member.get('properties', {}).get('name', '')
			# handles both "EPSG:32632" and "urn:ogc:def:crs:EPSG::32632"
			m = re.search(r'EPSG[:\s]+(\d+)', crs_name, re.IGNORECASE)
			if m:
				detected_crs = 'EPSG:' + m.group(1)

		if self.reprojection:
			layerCRS = self.layerCRSOverride
		elif detected_crs:
			layerCRS = detected_crs
			# Show info so user knows CRS was auto-detected
			log.info("CRS auto-detected from file: %s", detected_crs)
		else:
			layerCRS = "EPSG:4326"

		if not geoscn.hasCRS:
			try:
				geoscn.crs = layerCRS
			except Exception as e:
				log.error("Cannot set scene CRS", exc_info=True)
				self.report({'ERROR'}, "Cannot set scene CRS, check logs")
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
				return {'CANCELLED'}
			if rprj.iproj == 'EPSGIO' and nbFeats > 100:
				self.report({'ERROR'},
					"Reprojection via EPSG.io is limited to 100 features. "
					"Please install GDAL or PyProj.")
				return {'CANCELLED'}

		# --- First pass: compute bounding box ---
		# Iterate all coordinate positions to find overall extent.
		xmin = xmax = ymin = ymax = None

		def _update_bbox(coords_flat):
			nonlocal xmin, xmax, ymin, ymax
			for c in coords_flat:
				x, y = c[0], c[1]
				if xmin is None:
					xmin = xmax = x
					ymin = ymax = y
				else:
					if x < xmin: xmin = x
					if x > xmax: xmax = x
					if y < ymin: ymin = y
					if y > ymax: ymax = y

		def _flat_coords(geom):
			"""Yield all leaf coordinate positions from any geometry type."""
			gt = geom.get('type', '')
			coords = geom.get('coordinates', [])
			if gt == 'Point':
				yield coords
			elif gt in ('LineString', 'MultiPoint'):
				yield from coords
			elif gt in ('Polygon', 'MultiLineString'):
				for ring in coords:
					yield from ring
			elif gt == 'MultiPolygon':
				for poly in coords:
					for ring in poly:
						yield from ring

		for feat in features:
			geom = feat.get('geometry')
			if geom is None:
				continue
			_update_bbox(list(_flat_coords(geom)))

		if xmin is None:
			self.report({'ERROR'}, "No valid geometry found in file")
			return {'CANCELLED'}

		raw_bbox = BBOX(xmin, ymin, xmax, ymax)
		if rprj is not None:
			bbox = rprj.bbox(raw_bbox)
		else:
			bbox = raw_bbox

		# --- Set or retrieve scene origin ---
		if not geoscn.isGeoref:
			dx, dy = bbox.center
			geoscn.setOriginPrj(dx, dy)
		else:
			dx, dy = geoscn.getOriginPrj()

		log.info("GeoJSON '%s': %d features", fileName, nbFeats)

		# --- Build mesh ---
		bm = bmesh.new()
		collection = None
		if self.separateObjects:
			collection = bpy.data.collections.new(fileName)
			context.scene.collection.children.link(collection)

		progress = -1
		null_geom_count = 0
		valid_feat_count = 0

		wm = context.window_manager
		wm.progress_begin(0, nbFeats)
		for feat_i, feature in enumerate(features):

			wm.progress_update(feat_i)
			pourcent = round(((feat_i + 1) * 100) / nbFeats)
			if pourcent in range(0, 110, 10) and pourcent != progress:
				progress = pourcent
				if pourcent == 100:
					print(str(pourcent) + '%')
				else:
					print(str(pourcent), end="%, ")
				sys.stdout.flush()

			geom = feature.get('geometry')
			if geom is None:
				null_geom_count += 1
				continue

			gt = geom.get('type', '')
			coords = geom.get('coordinates', [])

			# Normalise to a list of (single_type, coords_for_one_part) tuples
			# so the mesh-building block below is uniform.
			parts = []
			if gt == 'Point':
				parts = [('Point', coords)]
			elif gt == 'MultiPoint':
				parts = [('Point', c) for c in coords]
			elif gt == 'LineString':
				parts = [('LineString', coords)]
			elif gt == 'MultiLineString':
				parts = [('LineString', c) for c in coords]
			elif gt == 'Polygon':
				parts = [('Polygon', coords)]
			elif gt == 'MultiPolygon':
				parts = [('Polygon', c) for c in coords]
			else:
				log.warning("Feature %d: unsupported geometry type '%s', skipping", feat_i, gt)
				continue

			if not parts:
				null_geom_count += 1
				continue

			for part_type, part_coords in parts:

				if part_type == 'Point':
					if not part_coords:
						continue
					xy = [(part_coords[0], part_coords[1])]
					fhas_z = len(part_coords) >= 3
					zs = [part_coords[2] if fhas_z else 0.0]
					if rprj:
						xy = rprj.pts(xy)
					z = zs[0] if (self.elevSource == 'GEOM' and fhas_z) else 0.0
					bm.verts.new((xy[0][0] - dx, xy[0][1] - dy, z))

				elif part_type == 'LineString':
					if not part_coords:
						continue
					fhas_z = _has_z(part_coords)
					raw_xy, zs = _coords_to_xy_zs(part_coords)
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

				elif part_type == 'Polygon':
					# part_coords is a list of rings; ring 0 = exterior, 1+ = holes
					# GeoJSON exterior rings are CCW per RFC 7946; just drop closing pt
					for r_i, ring in enumerate(part_coords):
						if not ring or len(ring) < 4:
							continue
						fhas_z = _has_z(ring)
						raw_xy, zs = _coords_to_xy_zs(ring)
						if rprj:
							raw_xy = rprj.pts(raw_xy)
						use_z = self.elevSource == 'GEOM' and fhas_z
						pts3d = [(x - dx, y - dy, z if use_z else 0.0)
								 for (x, y), z in zip(raw_xy, zs)]
						# Drop the closing duplicate point
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
				if not bm.verts:
					bm.clear()
					continue
				feat_bbox = getBBOX.fromBmesh(bm)
				ox, oy, oz = feat_bbox.center
				oz = feat_bbox.zmin
				bmesh.ops.translate(bm, verts=bm.verts, vec=(-ox, -oy, -oz))

				mesh = bpy.data.meshes.new(fileName)
				bm.to_mesh(mesh)
				bm.clear()
				mesh.validate(verbose=False)

				obj = bpy.data.objects.new(fileName, mesh)
				collection.objects.link(obj)
				context.view_layer.objects.active = obj
				obj.select_set(True)
				obj.location = (ox, oy, oz)
				obj.show_wire = (len(mesh.edges) > 0 and len(mesh.polygons) == 0)

				props = feature.get('properties') or {}
				for key, val in props.items():
					if val is not None:
						obj[key[:63]] = val

		wm.progress_end()

		if null_geom_count:
			log.warning("%d features had null/unsupported geometry and were skipped", null_geom_count)
			self.report({'WARNING'},
				"{} features skipped (null/unsupported geometry)".format(null_geom_count))

		if valid_feat_count == 0:
			bm.free()
			self.report({'ERROR'}, "No valid geometry found in file")
			return {'CANCELLED'}

		# --- Write combined mesh (non-separate mode) ---
		if not self.separateObjects:
			if prefs.mergeDoubles:
				bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)

			mesh = bpy.data.meshes.new(fileName)
			bm.to_mesh(mesh)
			mesh.validate(verbose=False)

			obj = bpy.data.objects.new(fileName, mesh)
			context.scene.collection.objects.link(obj)
			context.view_layer.objects.active = obj
			obj.select_set(True)
			obj.show_wire = (len(mesh.edges) > 0 and len(mesh.polygons) == 0)
			bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY')

			props_list = []
			for feature in features:
				p = feature.get('properties') or {}
				props_list.append(p)
			# Store properties from first feature as representative custom props
			if props_list:
				for key, val in props_list[0].items():
					if val is not None:
						obj[key[:63]] = val

		bm.free()

		t = perf_clock() - t0
		log.info("GeoJSON import completed in %.3f seconds", t)

		if prefs.adjust3Dview:
			bbox.shift(-dx, -dy)
			adjust3Dview(context, bbox)

		return {'FINISHED'}


classes = [
	IMPORTGIS_OT_geojson_file_dialog,
	IMPORTGIS_OT_geojson_import,
]

def register():
	for cls in classes:
		bpy.utils.register_class(cls)

def unregister():
	for cls in reversed(classes):
		bpy.utils.unregister_class(cls)
