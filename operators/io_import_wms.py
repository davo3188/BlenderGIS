# -*- coding:utf-8 -*-

import hashlib
import os
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import bpy
from bpy.props import StringProperty, EnumProperty, IntProperty
from bpy.types import Operator
import logging
log = logging.getLogger(__name__)

from ..geoscene import GeoScene, georefManagerLayout
from ..core import HAS_GDAL
from ..core.basemaps import MapService, GRIDS
from ..core.basemaps.servicesDefs import SOURCES
from ..core.proj import reprojBbox, reprojPt
from ..core import BBOX

from .utils import getBBOX, placeObj, adjust3Dview, showTextures, addTexture
from .utils import rasterExtentToMesh, geoRastUVmap

from ..core import settings
USER_AGENT = settings.user_agent

PKG, SUBPKG = __package__.split('.', maxsplit=1)

TIMEOUT = 30


# ---------------------------------------------------------------------------
# HTTP + XML helpers
# ---------------------------------------------------------------------------

def _http_get(url):
	rq = Request(url, headers={'User-Agent': USER_AGENT})
	with urlopen(rq, timeout=TIMEOUT) as r:
		return r.read()


def _strip_ns(tag):
	return tag.split('}')[-1] if '}' in tag else tag


def _get_wms_capabilities(base_url, version='1.1.1'):
	"""
	Fetch and parse a WMS GetCapabilities document.

	Returns {'layers': [...], 'formats': [...]}
	  layers: [{'name': str, 'title': str}, ...]
	  formats: ['image/png', 'image/jpeg', ...]
	Raises URLError / HTTPError / ET.ParseError on failure.
	"""
	url = (base_url.rstrip('?&') +
	       '?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=' + version)
	data = _http_get(url)
	root = ET.fromstring(data)

	# Collect GetMap formats
	formats = []
	for elem in root.iter():
		if _strip_ns(elem.tag) == 'GetMap':
			for child in elem:
				if _strip_ns(child.tag) == 'Format' and child.text:
					f = child.text.strip()
					if f not in formats:
						formats.append(f)

	# Collect layers (named layers only — skip unnamed group layers)
	layers = []
	seen = set()
	for elem in root.iter():
		if _strip_ns(elem.tag) != 'Layer':
			continue
		name_elem = next(
			(c for c in elem if _strip_ns(c.tag) == 'Name' and c.text), None)
		if name_elem is None:
			continue
		name = name_elem.text.strip()
		if not name or name in seen:
			continue
		seen.add(name)
		title_elem = next(
			(c for c in elem if _strip_ns(c.tag) == 'Title' and c.text), None)
		title = title_elem.text.strip() if title_elem is not None else name
		layers.append({'name': name, 'title': title})

	# Keep only recognised image formats; fall back to first three if none match
	img_formats = [f for f in formats
	               if f in ('image/png', 'image/jpeg', 'image/jpg', 'image/gif')]
	if not img_formats:
		img_formats = formats[:3] if formats else ['image/png']

	return {'layers': layers, 'formats': img_formats}


# ---------------------------------------------------------------------------
# Operator 1 — Service URL dialog
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_wms_service_dialog(Operator):
	"""Enter WMS service base URL"""

	bl_idname = "importgis.wms_service_dialog"
	bl_description = "Import imagery from a WMS (OGC Web Map Service)"
	bl_label = "Import WMS"
	bl_options = {'INTERNAL'}

	serviceUrl: StringProperty(
		name="WMS URL",
		description="Base URL of the WMS service (without query parameters)",
		default="")

	wmsVersion: EnumProperty(
		name="Version",
		description="WMS protocol version",
		items=[('1.1.1', '1.1.1', ''), ('1.3.0', '1.3.0', '')],
		default='1.1.1')

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self)

	def draw(self, context):
		layout = self.layout
		layout.prop(self, 'serviceUrl')
		layout.prop(self, 'wmsVersion')

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	def execute(self, context):
		url = self.serviceUrl.strip()
		if not url:
			self.report({'ERROR'}, "Please enter a WMS service URL")
			return {'CANCELLED'}
		bpy.ops.importgis.wms_import('INVOKE_DEFAULT',
			serviceUrl=url,
			wmsVersion=self.wmsVersion)
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator 2 — Layer/options dialog + import
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_wms_import(Operator):
	"""Fetch WMS layers, configure options, and import imagery"""

	bl_idname = "importgis.wms_import"
	bl_description = "Configure and execute WMS imagery import"
	bl_label = "Import WMS — Options"
	bl_options = {"UNDO"}

	serviceUrl:  StringProperty()
	wmsVersion:  StringProperty(default='1.1.1')

	# Last successfully fetched capabilities, stored at class level so
	# listLayers/listFormats can read them even when Blender calls the
	# callbacks on a fresh instance before invoke() has run.
	_last_caps = {}
	_last_url = ''

	# ---- dynamic enum callbacks ----

	def listLayers(self, context):
		caps = IMPORTGIS_OT_wms_import._last_caps
		layers = caps.get('layers', [])
		if not layers:
			return [('NONE', '(no layers)', '')]
		return [(l['name'], l['title'], l['name']) for l in layers]

	def listFormats(self, context):
		caps = IMPORTGIS_OT_wms_import._last_caps
		fmts = caps.get('formats', [])
		if not fmts:
			return [('image/png', 'PNG', '')]
		return [(f, f.split('/')[-1].upper(), '') for f in fmts]

	def listGrids(self, context):
		return [(k, k, v.get('CRS', k)) for k, v in GRIDS.items()]

	# ---- properties ----

	layerName: EnumProperty(
		name="Layer",
		description="WMS layer to import",
		items=listLayers)

	layerFormat: EnumProperty(
		name="Format",
		description="Image format for tile requests",
		items=listFormats)

	layerStyle: StringProperty(
		name="Style",
		description="WMS style name (leave blank for default)",
		default='')

	gridName: EnumProperty(
		name="Tile grid",
		description="Tile matrix / projection used for tile requests",
		items=listGrids)

	importMode: EnumProperty(
		name="Mode",
		items=[
			('PLANE', 'Flat plane',    'Create a new georeferenced plane mesh'),
			('DRAPE', 'Drape on mesh', 'Apply as texture on the active mesh object'),
		])

	zoomLevel: IntProperty(
		name="Zoom level",
		description="Tile zoom level (higher = more detail, more tiles)",
		default=14, min=0, max=22)

	def check(self, context):
		return True

	# ---- invoke: fetch capabilities then show dialog ----

	def invoke(self, context, event):
		url = self.serviceUrl.strip()
		if url != IMPORTGIS_OT_wms_import._last_url or not IMPORTGIS_OT_wms_import._last_caps:
			try:
				caps = _get_wms_capabilities(url, self.wmsVersion)
				IMPORTGIS_OT_wms_import._last_caps = caps
				IMPORTGIS_OT_wms_import._last_url = url
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
			except URLError:
				self.report({'ERROR'},
					"Cannot reach service. Check URL and connection.")
				return {'CANCELLED'}
			except ET.ParseError:
				self.report({'ERROR'},
					"Cannot parse WMS capabilities. Check URL and version.")
				return {'CANCELLED'}
			except Exception as e:
				self.report({'ERROR'}, "GetCapabilities failed: {}".format(e))
				return {'CANCELLED'}

		caps = IMPORTGIS_OT_wms_import._last_caps
		if not caps.get('layers'):
			self.report({'ERROR'}, "No layers found. Check URL and WMS version.")
			return {'CANCELLED'}

		return context.window_manager.invoke_props_dialog(self, width=420)

	# ---- draw ----

	def draw(self, context):
		layout = self.layout
		box = layout.box()
		box.label(text=self.serviceUrl[:65], icon='URL')
		layout.prop(self, 'layerName')
		layout.prop(self, 'layerFormat')
		layout.prop(self, 'layerStyle')
		layout.prop(self, 'gridName')
		layout.prop(self, 'importMode')
		layout.prop(self, 'zoomLevel', slider=True)
		if self.importMode == 'DRAPE':
			layout.label(text="Active object in the viewport will receive the texture",
			             icon='INFO')
		geoscn = GeoScene()
		if geoscn.isPartiallyGeoref:
			georefManagerLayout(self, context)

	@classmethod
	def poll(cls, context):
		return context.mode == 'OBJECT'

	# ---- execute ----

	def execute(self, context):

		# --- Validate cache folder ---
		prefs = context.preferences.addons[PKG].preferences
		cache_folder = prefs.cacheFolder
		if not cache_folder or not os.path.exists(cache_folder):
			self.report({'ERROR'},
				"Please define a valid cache folder in addon preferences")
			return {'CANCELLED'}
		if not os.access(cache_folder, os.W_OK):
			self.report({'ERROR'}, "Cache folder is not writable")
			return {'CANCELLED'}

		# --- Scene georef check ---
		geoscn = GeoScene()
		if geoscn.isBroken:
			self.report({'ERROR'},
				"Scene georef is broken, please fix it beforehand")
			return {'CANCELLED'}

		if self.layerName == 'NONE':
			self.report({'ERROR'}, "No layer selected")
			return {'CANCELLED'}

		# --- Build dynamic MapService source ---
		# MapService.__init__ looks up SOURCES[srckey], so we inject temporarily.
		fmt_short = self.layerFormat.split('/')[-1]      # 'png' or 'jpeg'
		crs_param_key = 'SRS' if self.wmsVersion == '1.1.1' else 'CRS'

		url_tpl = {
			'BASE_URL':  self.serviceUrl,
			'SERVICE':   'WMS',
			'VERSION':   self.wmsVersion,
			'REQUEST':   'GetMap',
			'LAYERS':    '{LAY}',
			'FORMAT':    self.layerFormat,
			'STYLES':    '{STYLE}',
			'BBOX':      '{BBOX}',
			'WIDTH':     '{WIDTH}',
			'HEIGHT':    '{HEIGHT}',
			'TRANSPARENT': 'TRUE',
		}
		url_tpl[crs_param_key] = '{CRS}'

		source = {
			'name':        'WMS Import',
			'description': '',
			'service':     'WMS',
			'grid':        self.gridName,
			'layers': {
				'LYR': {
					'urlKey':      self.layerName,
					'name':        self.layerName,
					'description': '',
					'format':      fmt_short,
					'style':       self.layerStyle,
					'zmin':        0,
					'zmax':        22,
				}
			},
			'urlTemplate': url_tpl,
			'referer':     self.serviceUrl,
		}

		# Stable srckey derived from URL so the tile cache file is reused across
		# imports of the same service.
		src_key = 'wms_' + hashlib.md5(self.serviceUrl.encode()).hexdigest()[:8]
		SOURCES[src_key] = source
		try:
			ms = MapService(src_key, cache_folder)
		finally:
			# MapService copies all source attrs onto itself in __init__,
			# so removing from SOURCES here is safe.
			del SOURCES[src_key]

		tm = ms.srcTms
		grid_crs = tm.CRS

		# --- GDAL check for cross-CRS reprojection ---
		scene_crs = geoscn.crs if geoscn.hasCRS else grid_crs
		needs_reproj = scene_crs != grid_crs
		if needs_reproj and not HAS_GDAL:
			self.report({'ERROR'},
				"GDAL is required when the scene CRS differs from the tile grid CRS. "
				"Install GDAL or choose a tile grid matching your scene CRS ({}).".format(scene_crs))
			return {'CANCELLED'}

		if self.importMode == 'DRAPE':
			return self._run_drape(context, geoscn, ms, tm, grid_crs, scene_crs, cache_folder)
		else:
			return self._run_plane(context, geoscn, ms, tm, grid_crs, scene_crs, cache_folder)

	# -----------------------------------------------------------------------
	# PLANE mode
	# -----------------------------------------------------------------------

	def _run_plane(self, context, geoscn, ms, tm, grid_crs, scene_crs, cache_folder):
		prefs = context.preferences.addons[PKG].preferences

		# Determine centre of the requested area in grid CRS.
		# If the scene is already georeffed, centre on the scene origin;
		# otherwise centre on (0, 0) in the grid CRS.
		if geoscn.isGeoref:
			dx0, dy0 = geoscn.getOriginPrj()
			if scene_crs != grid_crs:
				try:
					cx, cy = reprojPt(scene_crs, grid_crs, dx0, dy0)
				except Exception as e:
					self.report({'ERROR'}, "Reprojection of scene origin failed: {}".format(e))
					return {'CANCELLED'}
			else:
				cx, cy = dx0, dy0
		else:
			cx, cy = 0.0, 0.0

		# Build a bbox centred on (cx, cy) that covers ~1024 pixels at the
		# requested zoom level.
		res = tm.getRes(self.zoomLevel)
		half = 512 * res
		bbox = BBOX(cx - half, cy - half, cx + half, cy + half)

		# outCRS triggers GDAL reprojection inside getImage; pass None when
		# the raster is already in the scene CRS.
		out_crs = scene_crs if scene_crs != grid_crs else None

		# Fetch (blocking — same behaviour as WFS importer)
		context.window.cursor_set('WAIT')
		ms.start()
		try:
			rast = ms.getImage('LYR', bbox, self.zoomLevel,
			                   toDstGrid=False, outCRS=out_crs)
		finally:
			ms.stop()
			context.window.cursor_set('DEFAULT')

		if rast is None:
			self.report({'ERROR'}, "No image returned from WMS service")
			return {'CANCELLED'}

		# Save to a temp tif and load into Blender, then pack so the result
		# stays valid even if the temp file is deleted later.
		img_name = self.layerName.replace(':', '_').replace('/', '_') + '_wms'
		img_path = os.path.join(bpy.app.tempdir, img_name + '.tif')
		try:
			rast.save(img_path)
		except Exception as e:
			self.report({'ERROR'}, "Failed to save WMS image: {}".format(e))
			return {'CANCELLED'}

		try:
			bpyImg = bpy.data.images.load(img_path)
		except Exception as e:
			self.report({'ERROR'}, "Failed to load image into Blender: {}".format(e))
			return {'CANCELLED'}
		bpyImg.name = img_name
		bpyImg.pack()
		# geoRastUVmap() needs bpyImg on the raster object; add it at runtime.
		setattr(rast, 'bpyImg', bpyImg)

		# Set scene georef if not already set.
		# rast.center is in (out_crs or grid_crs) — i.e. the scene CRS.
		if not geoscn.isGeoref:
			try:
				geoscn.crs = scene_crs
			except Exception as e:
				self.report({'ERROR'}, "Cannot set scene CRS: {}".format(e))
				return {'CANCELLED'}
			dx, dy = rast.center.x, rast.center.y
			geoscn.setOriginPrj(dx, dy)
		else:
			dx, dy = geoscn.getOriginPrj()

		# Build flat quad mesh from the raster's geographic extent.
		name = img_name
		mesh = rasterExtentToMesh(name, rast, dx, dy, pxLoc='CORNER')
		obj = placeObj(mesh, name)

		# UV map and texture.
		uvLayer = mesh.uv_layers.new(name='wmsUV')
		geoRastUVmap(obj, uvLayer, rast, dx, dy)
		mat = bpy.data.materials.new(name + '_mat')
		obj.data.materials.append(mat)
		addTexture(mat, bpyImg, uvLayer)

		if prefs.adjust3Dview:
			adjust3Dview(context, getBBOX.fromObj(obj))
		if prefs.forceTexturedSolid:
			showTextures(context)

		return {'FINISHED'}

	# -----------------------------------------------------------------------
	# DRAPE mode
	# -----------------------------------------------------------------------

	def _run_drape(self, context, geoscn, ms, tm, grid_crs, scene_crs, cache_folder):
		prefs = context.preferences.addons[PKG].preferences

		obj = context.active_object
		if obj is None or obj.type != 'MESH':
			self.report({'ERROR'}, "Select a mesh object first")
			return {'CANCELLED'}
		if not geoscn.isGeoref:
			self.report({'ERROR'},
				"Scene must be georeferenced before draping")
			return {'CANCELLED'}

		dx, dy = geoscn.getOriginPrj()

		# Convert the mesh bounding box to scene CRS, then to grid CRS.
		obj_bbox = getBBOX.fromObj(obj).to2D().toGeo(geoscn)
		if obj_bbox is None:
			self.report({'ERROR'},
				"Cannot determine object geographic extent. "
				"Is the scene georeferenced?")
			return {'CANCELLED'}

		if scene_crs != grid_crs:
			try:
				req_bbox = reprojBbox(scene_crs, grid_crs, obj_bbox)
			except Exception as e:
				self.report({'ERROR'}, "Reprojection of mesh extent failed: {}".format(e))
				return {'CANCELLED'}
		else:
			req_bbox = obj_bbox

		# Choose zoom level that produces ~1024 px across the longest edge.
		w = abs(req_bbox.xmax - req_bbox.xmin)
		h = abs(req_bbox.ymax - req_bbox.ymin)
		if max(w, h) > 0:
			target_res = max(w, h) / 1024.0
			zoom = tm.getNearestZoom(target_res, rule='lower')
			if zoom is None:
				zoom = self.zoomLevel
			zoom = max(0, min(zoom, tm.nbLevels - 1))
		else:
			zoom = self.zoomLevel

		out_crs = scene_crs if scene_crs != grid_crs else None

		context.window.cursor_set('WAIT')
		ms.start()
		try:
			rast = ms.getImage('LYR', req_bbox, zoom,
			                   toDstGrid=False, outCRS=out_crs)
		finally:
			ms.stop()
			context.window.cursor_set('DEFAULT')

		if rast is None:
			self.report({'ERROR'}, "No image returned from WMS service")
			return {'CANCELLED'}

		img_name = self.layerName.replace(':', '_').replace('/', '_') + '_wms_drape'
		img_path = os.path.join(bpy.app.tempdir, img_name + '.tif')
		try:
			rast.save(img_path)
		except Exception as e:
			self.report({'ERROR'}, "Failed to save WMS image: {}".format(e))
			return {'CANCELLED'}

		try:
			bpyImg = bpy.data.images.load(img_path)
		except Exception as e:
			self.report({'ERROR'}, "Failed to load image into Blender: {}".format(e))
			return {'CANCELLED'}
		bpyImg.name = img_name
		bpyImg.pack()
		setattr(rast, 'bpyImg', bpyImg)

		# The raster is in scene_crs (outCRS=scene_crs or no reprojection
		# when they match).  geoRastUVmap expects vertex coords in scene CRS
		# and reprojects to raster CRS only when they differ — here they
		# don't, so reproj=None is correct.
		obj.select_set(True)
		context.view_layer.objects.active = obj
		mesh = obj.data
		uvLayer = mesh.uv_layers.new(name='wmsUV')
		uvLayer.active = True
		geoRastUVmap(obj, uvLayer, rast, dx, dy)

		mat = bpy.data.materials.new(img_name + '_mat')
		obj.data.materials.append(mat)
		addTexture(mat, bpyImg, uvLayer)

		if prefs.forceTexturedSolid:
			showTextures(context)

		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
	IMPORTGIS_OT_wms_service_dialog,
	IMPORTGIS_OT_wms_import,
]

def register():
	for cls in classes:
		bpy.utils.register_class(cls)

def unregister():
	for cls in reversed(classes):
		bpy.utils.unregister_class(cls)
