# -*- coding:utf-8 -*-
import os, sys, json
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty
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
from .utils.http import http_get, format_http_error

PKG, SUBPKG = __package__.split('.', maxsplit=1)


# ---------------------------------------------------------------------------
# ArcGIS REST helpers
# ---------------------------------------------------------------------------

def _check_arcgis_error(data):
	"""Raise RuntimeError with human-readable message for ArcGIS error responses."""
	err = data.get('error')
	if err is None:
		return
	code = err.get('code', 0)
	message = err.get('message', 'Unknown error')
	if code == 499:
		raise RuntimeError(
			"Token required. This service requires authentication. "
			"Provide a valid ArcGIS token.")
	elif code == 498:
		raise RuntimeError(
			"Token invalid or expired. Check your ArcGIS token.")
	elif code == 400:
		raise RuntimeError(
			"Bad request (400): {}".format(message))
	else:
		raise RuntimeError(
			"ArcGIS error {}: {}".format(code, message))


def _fetch_layers(base_url, token=''):
	"""
	Fetch service metadata and return only Feature Layer entries.

	Returns a list of dicts: [{'id': int, 'name': str}, ...]
	Raises RuntimeError for ArcGIS error responses.
	Raises URLError / HTTPError on network failure.
	"""
	url = base_url.rstrip('/') + '?f=json'
	if token:
		url += '&token=' + token
	raw = http_get(url)
	data = json.loads(raw)
	_check_arcgis_error(data)
	return [
		{'id': l['id'], 'name': l['name']}
		for l in data.get('layers', [])
		if l.get('type') == 'Feature Layer'
	]


def _build_query_url(service_url, layer_id, token='',
                     result_offset=0, max_features=1000,
                     bbox=None, bbox_crs='EPSG:4326'):
	params = {
		'where': '1=1',
		'outFields': '*',
		'f': 'geojson',
		'resultOffset': str(result_offset),
		'resultRecordCount': str(max_features),
	}
	if bbox is not None:
		params['geometry'] = '{},{},{},{}'.format(
			bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax)
		params['geometryType'] = 'esriGeometryEnvelope'
		params['inSR'] = bbox_crs.split(':')[-1]
		params['spatialRel'] = 'esriSpatialRelIntersects'
	if token:
		params['token'] = token
	return '{}/{}/query?{}'.format(
		service_url.rstrip('/'), layer_id, urlencode(params))


# ---------------------------------------------------------------------------
# Geometry helpers (identical to io_import_geojson.py)
# ---------------------------------------------------------------------------

def _coords_to_xy_zs(coords):
	xy = [(c[0], c[1]) for c in coords]
	zs = [c[2] if len(c) >= 3 else 0.0 for c in coords]
	return xy, zs


def _has_z(coords):
	return any(len(c) >= 3 for c in coords)


# ---------------------------------------------------------------------------
# Operator 1 — Service URL dialog
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_arcgis_rest_service_dialog(Operator):
	"""Enter ArcGIS REST FeatureServer or MapServer base URL"""

	bl_idname = "importgis.arcgis_rest_service_dialog"
	bl_description = "Import features from an ArcGIS REST Feature Service"
	bl_label = "Import ArcGIS REST Feature Service"
	bl_options = {'INTERNAL'}

	serviceUrl: StringProperty(
		name="FeatureServer URL",
		description="Base URL ending in /FeatureServer or /MapServer (no layer ID)",
		default="")

	token: StringProperty(
		name="Token (optional)",
		description="Leave empty for public services",
		default="")

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self, width=500)

	def draw(self, context):
		layout = self.layout
		layout.label(text="ArcGIS REST Feature Service", icon='URL')
		layout.prop(self, 'serviceUrl')
		layout.prop(self, 'token')
		layout.label(
			text="Example: .../FeatureServer or .../MapServer",
			icon='INFO')

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def execute(self, context):
		url = self.serviceUrl.strip().rstrip('/')
		if not url:
			self.report({'ERROR'}, "Enter a service URL")
			return {'CANCELLED'}
		try:
			bpy.ops.importgis.arcgis_rest_layer_dialog('INVOKE_DEFAULT',
				serviceUrl=url, token=self.token)
		except RuntimeError:
			return {'CANCELLED'}
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator 2 — Layer selection dialog
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_arcgis_rest_layer_dialog(Operator):
	"""Fetch available Feature Layers and let the user choose one"""

	bl_idname = "importgis.arcgis_rest_layer_dialog"
	bl_description = "Select an ArcGIS REST Feature Layer to import"
	bl_label = "Import ArcGIS REST — Select Layer"
	bl_options = {'INTERNAL'}

	serviceUrl: StringProperty()
	token: StringProperty()

	# Cache populated in invoke() before dialog opens so listLayers()
	# does not re-fetch on every enum redraw.
	_last_layers = []  # list of {'id': int, 'name': str}
	_last_url = ''

	def listLayers(self, context):
		layers = IMPORTGIS_OT_arcgis_rest_layer_dialog._last_layers
		if not layers:
			return [('NONE', '(no layers)', '')]
		return [(str(l['id']), l['name'], 'ID: {}'.format(l['id']))
		        for l in layers]

	layerId: EnumProperty(
		name="Layer",
		description="Feature Layer to import",
		items=listLayers)

	def check(self, context):
		return True

	def invoke(self, context, event):
		try:
			layers = _fetch_layers(self.serviceUrl, self.token)
			IMPORTGIS_OT_arcgis_rest_layer_dialog._last_layers = layers
			IMPORTGIS_OT_arcgis_rest_layer_dialog._last_url = self.serviceUrl
			if not layers:
				self.report({'ERROR'},
					"No Feature Layers found in this service")
				return {'CANCELLED'}
		except RuntimeError as e:
			self.report({'ERROR'}, str(e))
			return {'CANCELLED'}
		except (URLError, HTTPError) as e:
			self.report({'ERROR'}, format_http_error(e))
			return {'CANCELLED'}
		except json.JSONDecodeError as e:
			self.report({'ERROR'},
				"Cannot parse service metadata: {}".format(e))
			return {'CANCELLED'}
		return context.window_manager.invoke_props_dialog(self, width=400)

	def draw(self, context):
		layout = self.layout
		layout.label(text="URL: " + self.serviceUrl[:60], icon='URL')
		layout.prop(self, 'layerId')

	def execute(self, context):
		if self.layerId == 'NONE':
			self.report({'ERROR'}, "Select a layer")
			return {'CANCELLED'}
		bpy.ops.importgis.arcgis_rest_import('INVOKE_DEFAULT',
			serviceUrl=self.serviceUrl,
			token=self.token,
			layerId=self.layerId)
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator 3 — Import options and execute
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_arcgis_rest_import(Operator):
	"""Import options and geometry builder for an ArcGIS REST Feature Layer"""

	bl_idname = "importgis.arcgis_rest_import"
	bl_description = "Import features from an ArcGIS REST Feature Layer"
	bl_label = "Import ArcGIS REST Layer"
	bl_options = {"UNDO"}

	serviceUrl: StringProperty()
	token: StringProperty()
	layerId: StringProperty()

	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	reprojection: BoolProperty(
		name="Specify CRS",
		description="Override default WGS84 CRS",
		default=False)

	layerCRSOverride: EnumProperty(
		name="Layer CRS",
		description="Coordinate Reference System of this layer",
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

	maxFeatures: IntProperty(
		name="Max features per page",
		description="Maximum number of features to request per page",
		default=1000,
		min=1,
		max=5000)

	useBbox: BoolProperty(
		name="Filter by scene extent",
		description=(
			"Only download features within the current scene "
			"georeferenced bounding box"),
		default=False)

	def check(self, context):
		return True

	def _crs_layout(self, layout):
		row = layout.row(align=True)
		split = row.split(factor=0.35, align=True)
		split.label(text='CRS:')
		split.prop(self, 'layerCRSOverride', text='')
		row.operator("bgis.add_predef_crs", text='', icon='ADD')

	def draw(self, context):
		layout = self.layout

		layout.label(text="Engine: ArcGIS REST → GeoJSON", icon='INFO')

		box = layout.box()
		box.label(text="Service: " + self.serviceUrl[:55], icon='URL')
		# Look up layer name for display
		layer_id_int = int(self.layerId) if self.layerId.isdigit() else -1
		layer_name = next(
			(l['name'] for l in IMPORTGIS_OT_arcgis_rest_layer_dialog._last_layers
			 if l['id'] == layer_id_int),
			'Layer ' + self.layerId)
		box.label(text="Layer: {} (ID {})".format(layer_name, self.layerId),
		          icon='MESH_DATA')

		layout.prop(self, 'maxFeatures')
		layout.prop(self, 'useBbox')
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
		return context.window_manager.invoke_props_dialog(self, width=420)

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def execute(self, context):

		prefs = bpy.context.preferences.addons[PKG].preferences

		w = context.window
		w.cursor_set('WAIT')
		t0 = perf_clock()

		bpy.ops.object.select_all(action='DESELECT')

		# --- Resolve layer name for object/collection naming ---
		layer_id_int = int(self.layerId) if self.layerId.isdigit() else -1
		fileName = next(
			(l['name'] for l in IMPORTGIS_OT_arcgis_rest_layer_dialog._last_layers
			 if l['id'] == layer_id_int),
			'Layer_' + self.layerId)

		# --- Determine layer CRS (always WGS84 unless overridden) ---
		layerCRS = self.layerCRSOverride if self.reprojection else 'EPSG:4326'

		# --- GeoScene check ---
		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		# --- Build spatial filter bbox in EPSG:4326 ---
		query_bbox = None
		if self.useBbox and geoscn.isGeoref:
			try:
				scene_bbox = getBBOX.fromScn(context.scene).to2D().toGeo(geoscn)
				if geoscn.crs != 'EPSG:4326':
					rprj_to_wgs = Reproj(geoscn.crs, 'EPSG:4326')
					query_bbox = rprj_to_wgs.bbox(scene_bbox)
				else:
					query_bbox = scene_bbox
			except Exception as e:
				log.warning("Could not compute bbox filter: %s", e)
				query_bbox = None

		# --- Paginated feature fetch ---
		all_features = []
		offset = 0
		page = 0
		while True:
			url = _build_query_url(
				self.serviceUrl, self.layerId, self.token,
				result_offset=offset,
				max_features=self.maxFeatures,
				bbox=query_bbox,
				bbox_crs='EPSG:4326')
			log.debug("ArcGIS REST query URL: %s", url)

			try:
				raw = http_get(url)
			except (URLError, HTTPError) as e:
				self.report({'ERROR'}, format_http_error(e))
				return {'CANCELLED'}

			try:
				gj = json.loads(raw)
			except json.JSONDecodeError as e:
				self.report({'ERROR'}, "Invalid JSON response: {}".format(e))
				return {'CANCELLED'}

			try:
				_check_arcgis_error(gj)
			except RuntimeError as e:
				self.report({'ERROR'}, str(e))
				return {'CANCELLED'}

			page_features = gj.get('features', [])
			all_features.extend(page_features)

			page += 1
			print("Page {}: {} features (total so far: {})".format(
				page, len(page_features), len(all_features)))
			sys.stdout.flush()

			if not gj.get('exceededTransferLimit', False):
				break
			if not page_features:
				# Safety: server says more data but returned nothing — stop.
				log.warning("exceededTransferLimit but empty page at offset %d; stopping", offset)
				break
			offset += self.maxFeatures

		if not all_features:
			self.report({'ERROR'}, "No features returned by service")
			return {'CANCELLED'}

		# Wrap accumulated pages into a single FeatureCollection for the mesh builder.
		gj = {'type': 'FeatureCollection', 'features': all_features}

		# ===================================================================
		# From here: logic copied verbatim from io_import_geojson.py execute()
		# Differences:
		#   - layerCRS already determined above (hardcoded EPSG:4326 or override)
		#   - fileName = layer name resolved from _last_layers
		#   - No file open() needed — gj is already assembled
		# ===================================================================

		if gj.get('type') != 'FeatureCollection':
			self.report({'ERROR'}, "Response is not a GeoJSON FeatureCollection")
			return {'CANCELLED'}

		features = gj.get('features', [])
		if not features:
			self.report({'ERROR'}, "No features returned by service")
			return {'CANCELLED'}

		nbFeats = len(features)

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
			self.report({'ERROR'}, "No valid geometry found in response")
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

		log.info("ArcGIS REST '%s': %d features", fileName, nbFeats)

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
			self.report({'ERROR'}, "No valid geometry found in response")
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
			if props_list:
				for key, val in props_list[0].items():
					if val is not None:
						obj[key[:63]] = val

		bm.free()

		t = perf_clock() - t0
		log.info("ArcGIS REST import completed in %.3f seconds", t)

		if prefs.adjust3Dview:
			bbox.shift(-dx, -dy)
			adjust3Dview(context, bbox)

		return {'FINISHED'}


classes = [
	IMPORTGIS_OT_arcgis_rest_service_dialog,
	IMPORTGIS_OT_arcgis_rest_layer_dialog,
	IMPORTGIS_OT_arcgis_rest_import,
]

def register():
	for cls in classes:
		bpy.utils.register_class(cls)

def unregister():
	for cls in reversed(classes):
		bpy.utils.unregister_class(cls)
