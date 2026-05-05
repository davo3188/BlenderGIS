# -*- coding:utf-8 -*-
import os, sys, json, re
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
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

PKG, SUBPKG = __package__.split('.', maxsplit=1)

TIMEOUT = 30

from ..core import settings
USER_AGENT = settings.user_agent


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _http_get(url):
	"""Perform a simple HTTP GET and return response bytes. Raises on error."""
	rq = Request(url, headers={'User-Agent': USER_AGENT})
	with urlopen(rq, timeout=TIMEOUT) as r:
		return r.read()


# ---------------------------------------------------------------------------
# WFS helpers
# ---------------------------------------------------------------------------

def _strip_ns(tag):
	"""Remove XML namespace prefix from a tag string."""
	return tag.split('}')[-1] if '}' in tag else tag


def _get_capabilities(base_url):
	"""
	Fetch and parse a WFS GetCapabilities document.

	Returns a list of dicts:
	  [{'name': str, 'title': str, 'crs': str or None}, ...]
	Raises URLError / HTTPError / ET.ParseError on failure.
	"""
	url = base_url.rstrip('?&') + '?SERVICE=WFS&REQUEST=GetCapabilities'
	data = _http_get(url)
	root = ET.fromstring(data)

	layers = []
	for elem in root.iter():
		if _strip_ns(elem.tag) == 'FeatureType':
			name = next(
				(c.text for c in elem
				 if _strip_ns(c.tag) == 'Name' and c.text),
				None)
			title = next(
				(c.text for c in elem
				 if _strip_ns(c.tag) == 'Title' and c.text),
				None)
			crs_text = next(
				(c.text for c in elem
				 if _strip_ns(c.tag) in
				    ('DefaultSRS', 'DefaultCRS', 'OtherSRS', 'OtherCRS')
				 and c.text),
				None)
			if name:
				layers.append({
					'name': name,
					'title': title or name,
					'crs': crs_text,
				})
	return layers


def _normalize_crs(crs_string):
	"""
	Normalize a WFS CRS string to 'EPSG:NNNN' format.
	Handles 'urn:ogc:def:crs:EPSG::4326', 'EPSG:4326', etc.
	Returns None if no EPSG code can be extracted.
	"""
	if not crs_string:
		return None
	m = re.search(r'EPSG[:\s]+(\d+)', crs_string, re.IGNORECASE)
	if m:
		return 'EPSG:' + m.group(1)
	return None


def _build_getfeature_url(base_url, layer_name, layer_crs,
                           max_features=1000, bbox=None):
	"""
	Build a WFS GetFeature URL requesting GeoJSON output.
	Sends both WFS 2.0 and WFS 1.x parameter names for maximum compatibility.
	"""
	params = {
		'SERVICE': 'WFS',
		'VERSION': '2.0.0',
		'REQUEST': 'GetFeature',
		'TYPENAMES': layer_name,      # WFS 2.0
		'TYPENAME': layer_name,       # WFS 1.x fallback
		'OUTPUTFORMAT': 'application/json',
		'COUNT': str(max_features),   # WFS 2.0
		'MAXFEATURES': str(max_features),  # WFS 1.x fallback
	}
	if bbox is not None and layer_crs:
		epsg_code = layer_crs.split(':')[-1]
		params['BBOX'] = '{},{},{},{},urn:ogc:def:crs:EPSG::{}'.format(
			bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax, epsg_code)
	return base_url.rstrip('?&') + '?' + urlencode(params)


# ---------------------------------------------------------------------------
# Geometry helpers (identical to io_import_geojson.py)
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
# Operator 1 — Service URL dialog
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_wfs_service_dialog(Operator):
	"""Enter WFS service base URL"""

	bl_idname = "importgis.wfs_service_dialog"
	bl_description = "Import features from a WFS (OGC Web Feature Service)"
	bl_label = "Import WFS"
	bl_options = {'INTERNAL'}

	serviceUrl: StringProperty(
		name="Service URL",
		description="Base URL of the WFS service (without query parameters)",
		default="")

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)

	def draw(self, context):
		layout = self.layout
		layout.prop(self, 'serviceUrl')

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def execute(self, context):
		url = self.serviceUrl.strip()
		if not url:
			self.report({'ERROR'}, "Please enter a WFS service URL")
			return {'CANCELLED'}
		bpy.ops.importgis.wfs_layer_dialog('INVOKE_DEFAULT', serviceUrl=url)
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator 2 — Layer selection dialog (fetches GetCapabilities)
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_wfs_layer_dialog(Operator):
	"""Fetch available layers from a WFS service and let the user choose one"""

	bl_idname = "importgis.wfs_layer_dialog"
	bl_description = "Select a WFS layer to import"
	bl_label = "Import WFS — Select Layer"
	bl_options = {'INTERNAL'}

	serviceUrl: StringProperty()

	# Cache for the capabilities result so listLayers() doesn't re-fetch
	# on every redraw. Stored as a class variable keyed by URL.
	_caps_cache = {}

	def listLayers(self, context):
		url = self.serviceUrl.strip()
		if not url:
			return [('NONE', '(no URL)', '')]
		if url not in IMPORTGIS_OT_wfs_layer_dialog._caps_cache:
			try:
				layers = _get_capabilities(url)
				IMPORTGIS_OT_wfs_layer_dialog._caps_cache[url] = layers
			except Exception as e:
				log.error("GetCapabilities failed: %s", e)
				return [('NONE', '(failed to fetch layers)', '')]
		layers = IMPORTGIS_OT_wfs_layer_dialog._caps_cache.get(url, [])
		if not layers:
			return [('NONE', '(no layers found)', '')]
		return [(l['name'], l['title'], l['name']) for l in layers]

	layerName: EnumProperty(
		name="Layer",
		description="WFS feature type to import",
		items=listLayers)

	def check(self, context):
		return True

	def invoke(self, context, event):
		# Pre-populate the cache before the dialog opens so the enum
		# is populated on first draw.
		url = self.serviceUrl.strip()
		if url and url not in IMPORTGIS_OT_wfs_layer_dialog._caps_cache:
			try:
				layers = _get_capabilities(url)
				IMPORTGIS_OT_wfs_layer_dialog._caps_cache[url] = layers
			except (URLError, HTTPError) as e:
				code = getattr(e, 'code', None)
				if code in (401, 403):
					self.report({'ERROR'},
						"Access denied ({}). This service requires authentication.".format(code))
				elif code == 404:
					self.report({'ERROR'}, "Service not found. Check the URL.")
				elif code is not None:
					self.report({'ERROR'},
						"HTTP error {}: {}".format(code, getattr(e, 'reason', '')))
				else:
					self.report({'ERROR'},
						"Cannot reach service. Check URL and connection.")
				return {'CANCELLED'}
			except ET.ParseError as e:
				self.report({'ERROR'},
					"Could not parse GetCapabilities response: {}".format(e))
				return {'CANCELLED'}
			except Exception as e:
				self.report({'ERROR'},
					"GetCapabilities failed: {}".format(e))
				return {'CANCELLED'}
		return context.window_manager.invoke_props_dialog(self)

	def draw(self, context):
		layout = self.layout
		layout.label(text="URL: " + self.serviceUrl[:60], icon='URL')
		layout.prop(self, 'layerName')

	def execute(self, context):
		if self.layerName == 'NONE':
			self.report({'ERROR'}, "No layer selected")
			return {'CANCELLED'}

		# Find the CRS for the selected layer from the cache
		url = self.serviceUrl.strip()
		caps = IMPORTGIS_OT_wfs_layer_dialog._caps_cache.get(url, [])
		layer_crs = ''
		for l in caps:
			if l['name'] == self.layerName:
				layer_crs = _normalize_crs(l['crs']) or ''
				break

		bpy.ops.importgis.wfs_import('INVOKE_DEFAULT',
			serviceUrl=url,
			layerName=self.layerName,
			layerCRS=layer_crs)
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator 3 — Import options and execute
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_wfs_import(Operator):
	"""Import options and geometry builder for a WFS layer"""

	bl_idname = "importgis.wfs_import"
	bl_description = "Import features from a WFS layer"
	bl_label = "Import WFS Layer"
	bl_options = {"UNDO"}

	serviceUrl: StringProperty()
	layerName:  StringProperty()
	layerCRS:   StringProperty()  # normalized 'EPSG:NNNN' or empty

	def listPredefCRS(self, context):
		return PredefCRS.getEnumItems()

	reprojection: BoolProperty(
		name="Override CRS",
		description=(
			"Override the CRS detected from GetCapabilities. "
			"Use when the service reports a wrong or missing CRS."),
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
		name="Max Features",
		description="Maximum number of features to download",
		default=1000,
		min=1,
		max=100000)

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

		layout.label(text="Engine: WFS → GeoJSON (urllib)", icon='INFO')

		# Read-only service info
		box = layout.box()
		box.label(text="Service: " + self.serviceUrl[:55], icon='URL')
		box.label(text="Layer: " + self.layerName, icon='MESH_DATA')
		if self.layerCRS:
			box.label(text="Detected CRS: " + self.layerCRS)

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

		fileName = self.layerName

		# --- Determine layer CRS ---
		if self.reprojection:
			layerCRS = self.layerCRSOverride
		elif self.layerCRS:
			layerCRS = self.layerCRS
		else:
			log.warning("No CRS from GetCapabilities; assuming EPSG:4326")
			layerCRS = "EPSG:4326"

		# --- GeoScene check ---
		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'}, "Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		# --- Build bbox filter in layer CRS if requested ---
		req_bbox = None
		if self.useBbox and geoscn.isGeoref:
			try:
				from ..core.proj import reprojBbox
				scene_bbox = getBBOX.fromScn(context.scene).to2D().toGeo(geoscn)
				if scene_bbox is not None:
					if geoscn.crs != layerCRS:
						req_bbox = reprojBbox(geoscn.crs, layerCRS, scene_bbox)
					else:
						req_bbox = scene_bbox
			except Exception as e:
				log.warning("Could not compute bbox filter: %s", e)
				req_bbox = None

		# --- Build GetFeature URL ---
		url = _build_getfeature_url(
			self.serviceUrl, self.layerName, layerCRS,
			max_features=self.maxFeatures,
			bbox=req_bbox)
		log.debug("WFS GetFeature URL: %s", url)

		# --- HTTP GET ---
		try:
			raw = _http_get(url)
		except HTTPError as e:
			code = e.code
			if code in (401, 403):
				self.report({'ERROR'},
					"Access denied ({}). This service requires authentication.".format(code))
			elif code == 404:
				self.report({'ERROR'}, "Service not found. Check the URL.")
			else:
				self.report({'ERROR'},
					"HTTP error {}: {}".format(code, getattr(e, 'reason', '')))
			return {'CANCELLED'}
		except URLError as e:
			self.report({'ERROR'},
				"Cannot reach service. Check URL and connection.")
			return {'CANCELLED'}

		# --- Check we got JSON not GML ---
		if raw.lstrip()[:1] == b'<':
			self.report({'ERROR'},
				"Server returned GML instead of GeoJSON. "
				"This WFS version may not support JSON output.")
			return {'CANCELLED'}

		# --- Parse JSON ---
		try:
			gj = json.loads(raw)
		except json.JSONDecodeError as e:
			self.report({'ERROR'}, "Cannot parse server response as JSON: {}".format(e))
			return {'CANCELLED'}

		# ===================================================================
		# From here: logic copied verbatim from io_import_geojson.py execute()
		# Differences:
		#   - layerCRS already determined above (not from file content)
		#   - fileName = self.layerName (not a filepath basename)
		#   - No file open() needed — gj is already parsed
		# ===================================================================

		if gj.get('type') != 'FeatureCollection':
			self.report({'ERROR'}, "Response is not a GeoJSON FeatureCollection")
			return {'CANCELLED'}

		features = gj.get('features', [])
		if not features:
			self.report({'ERROR'}, "No features returned by the service")
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

		log.info("WFS '%s': %d features", fileName, nbFeats)

		# --- Build mesh ---
		bm = bmesh.new()
		collection = None
		if self.separateObjects:
			collection = bpy.data.collections.new(fileName)
			context.scene.collection.children.link(collection)

		progress = -1
		null_geom_count = 0
		valid_feat_count = 0

		for feat_i, feature in enumerate(features):

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
			# Store properties from first feature as representative custom props
			if props_list:
				for key, val in props_list[0].items():
					if val is not None:
						obj[key[:63]] = val

		bm.free()

		t = perf_clock() - t0
		log.info("WFS import completed in %.3f seconds", t)

		if prefs.adjust3Dview:
			bbox.shift(-dx, -dy)
			adjust3Dview(context, bbox)

		return {'FINISHED'}


classes = [
	IMPORTGIS_OT_wfs_service_dialog,
	IMPORTGIS_OT_wfs_layer_dialog,
	IMPORTGIS_OT_wfs_import,
]

def register():
	for cls in classes:
		bpy.utils.register_class(cls)

def unregister():
	for cls in reversed(classes):
		bpy.utils.unregister_class(cls)
