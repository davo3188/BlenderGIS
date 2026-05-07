# -*- coding:utf-8 -*-

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

import bpy
from bpy.props import StringProperty, EnumProperty, IntProperty
from bpy.types import Operator
import logging
log = logging.getLogger(__name__)

from ..core.basemaps import GRIDS
from ..core.basemaps.servicesDefs import SOURCES

from .utils.http import http_get, format_http_error

PKG, SUBPKG = __package__.split('.', maxsplit=1)


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


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
	data = http_get(url)
	root = ET.fromstring(data)

	formats = []
	for elem in root.iter():
		if _strip_ns(elem.tag) == 'GetMap':
			for child in elem:
				if _strip_ns(child.tag) == 'Format' and child.text:
					f = child.text.strip()
					if f not in formats:
						formats.append(f)

	layers = []
	seen = set()
	for elem in root.iter():
		if _strip_ns(elem.tag) != 'Layer':
			continue
		name_elem = next(
			(c for c in elem
			 if _strip_ns(c.tag) in ('Name', 'Identifier') and c.text),
			None)
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

	img_formats = [f for f in formats
	               if f in ('image/png', 'image/jpeg', 'image/jpg', 'image/gif')]
	if not img_formats:
		img_formats = formats[:3] if formats else ['image/png']

	# WMTS TileMatrixSet identifiers (skipped silently for WMS responses,
	# which have no <TileMatrixSet> elements)
	matrix_sets = []
	seen_ms = set()
	for elem in root.iter():
		if _strip_ns(elem.tag) == 'TileMatrixSet':
			ident = next(
				(c.text for c in elem
				 if _strip_ns(c.tag) == 'Identifier' and c.text),
				None)
			if ident and ident not in seen_ms:
				seen_ms.add(ident)
				matrix_sets.append(ident)

	return {'layers': layers, 'formats': img_formats, 'matrix_sets': matrix_sets}


# ---------------------------------------------------------------------------
# Operator 1 — URL entry dialog
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_custom_basemap_url(Operator):
	"""Add a custom WMS or WMTS basemap to the map viewer"""

	bl_idname = "importgis.custom_basemap_url"
	bl_label = "Add custom basemap (WMS/WMTS)"
	bl_options = {'INTERNAL'}

	serviceType: EnumProperty(
		name="Service type",
		items=[('WMS',  'WMS',  'OGC Web Map Service'),
		       ('WMTS', 'WMTS', 'OGC Web Map Tile Service')],
		default='WMS')

	baseUrl: StringProperty(
		name="Service URL",
		description="Base URL of the WMS/WMTS service")

	wmsVersion: EnumProperty(
		name="WMS version",
		items=[('1.1.1', '1.1.1', ''), ('1.3.0', '1.3.0', '')],
		default='1.1.1')

	def check(self, context):
		return True

	def invoke(self, context, event):
		return context.window_manager.invoke_props_dialog(self, width=500)

	def draw(self, context):
		layout = self.layout
		layout.prop(self, 'serviceType')
		layout.prop(self, 'baseUrl')
		if self.serviceType == 'WMS':
			layout.prop(self, 'wmsVersion')

	def execute(self, context):
		if not self.baseUrl.strip():
			self.report({'ERROR'}, "Enter a service URL")
			return {'CANCELLED'}
		bpy.ops.importgis.custom_basemap_config('INVOKE_DEFAULT',
			serviceType=self.serviceType,
			baseUrl=self.baseUrl.strip(),
			wmsVersion=self.wmsVersion)
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator 3 — Fetch / retry GetCapabilities (button inside the config dialog)
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_custom_basemap_fetch(Operator):
	"""(Re)fetch the GetCapabilities response from the service URL"""

	bl_idname = "importgis.custom_basemap_fetch"
	bl_label = "Fetch layers"
	bl_options = {'INTERNAL'}

	baseUrl: StringProperty()
	wmsVersion: StringProperty(default='1.1.1')

	def execute(self, context):
		try:
			caps = _get_wms_capabilities(self.baseUrl, self.wmsVersion)
			IMPORTGIS_OT_custom_basemap_config._last_caps = caps
			IMPORTGIS_OT_custom_basemap_config._fetch_error = ''
			self.report({'INFO'},
				"Found {} layers".format(len(caps.get('layers', []))))
		except HTTPError as e:
			code = e.code
			if code in (401, 403):
				msg = "Access denied ({})".format(code)
			elif code == 404:
				msg = "Service not found"
			else:
				msg = "HTTP error {}".format(code)
			IMPORTGIS_OT_custom_basemap_config._fetch_error = msg
			self.report({'ERROR'}, msg)
			return {'CANCELLED'}
		except Exception as e:
			msg = str(e)
			IMPORTGIS_OT_custom_basemap_config._fetch_error = msg
			self.report({'ERROR'}, msg)
			return {'CANCELLED'}
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator 2 — Configuration dialog (layer / options / open viewer)
# ---------------------------------------------------------------------------

class IMPORTGIS_OT_custom_basemap_config(Operator):
	"""Choose layer/options for a custom basemap and open the map viewer"""

	bl_idname = "importgis.custom_basemap_config"
	bl_label = "Configure custom basemap"
	bl_options = {'INTERNAL'}

	serviceType: StringProperty()
	baseUrl:     StringProperty()
	wmsVersion:  StringProperty(default='1.1.1')

	gridName: EnumProperty(
		name="Tile grid",
		items=[(k, k, v.get('CRS', k)) for k, v in GRIDS.items()])

	layerStyle: StringProperty(name="Style", default='')
	sourceName: StringProperty(name="Source name")
	zmin: IntProperty(name="Min zoom", default=0,  min=0, max=22)
	zmax: IntProperty(name="Max zoom", default=20, min=0, max=22)

	# Class-level cache shared with the fetch operator.
	# Populated in invoke() and by IMPORTGIS_OT_custom_basemap_fetch.execute().
	_last_caps = {}
	_fetch_error = ''

	def listLayers(self, context):
		layers = IMPORTGIS_OT_custom_basemap_config._last_caps.get('layers', [])
		if not layers:
			return [('NONE', '(fetch layers first)', '')]
		return [(l['name'], l['title'], l['name']) for l in layers]

	def listFormats(self, context):
		fmts = IMPORTGIS_OT_custom_basemap_config._last_caps.get('formats', [])
		if not fmts:
			return [('png', 'PNG', '')]
		return [(f.split('/')[-1], f.split('/')[-1].upper(), f) for f in fmts]

	def listMatrixSets(self, context):
		ms = IMPORTGIS_OT_custom_basemap_config._last_caps.get('matrix_sets', [])
		if not ms:
			return [('NONE', '(fetch layers first)', '')]
		return [(m, m, '') for m in ms]

	layerName:   EnumProperty(name="Layer",  items=listLayers)
	layerFormat: EnumProperty(name="Format", items=listFormats)
	matrixSet:   EnumProperty(
		name="TileMatrixSet ID",
		description="Tile matrix set identifier from WMTS capabilities",
		items=listMatrixSets)

	def check(self, context):
		return True

	def invoke(self, context, event):
		# Reset cache for this URL
		IMPORTGIS_OT_custom_basemap_config._last_caps = {}
		IMPORTGIS_OT_custom_basemap_config._fetch_error = ''

		# Auto-fetch capabilities immediately so the dialog opens populated
		try:
			caps = _get_wms_capabilities(self.baseUrl, self.wmsVersion)
			IMPORTGIS_OT_custom_basemap_config._last_caps = caps
			n = len(caps.get('layers', []))
			self.report({'INFO'}, "Found {} layers".format(n))
		except HTTPError as e:
			code = e.code
			if code in (401, 403):
				msg = "Access denied ({}). Service requires authentication.".format(code)
			elif code == 404:
				msg = "Service not found. Check the URL."
			else:
				msg = "HTTP error {}: {}".format(code, getattr(e, 'reason', ''))
			IMPORTGIS_OT_custom_basemap_config._fetch_error = msg
			self.report({'WARNING'}, msg)
		except URLError:
			msg = "Cannot reach service. Check URL and connection."
			IMPORTGIS_OT_custom_basemap_config._fetch_error = msg
			self.report({'WARNING'}, msg)
		except ET.ParseError:
			msg = "Cannot parse capabilities response. Check URL."
			IMPORTGIS_OT_custom_basemap_config._fetch_error = msg
			self.report({'WARNING'}, msg)
		except Exception as e:
			msg = "GetCapabilities failed: {}".format(e)
			IMPORTGIS_OT_custom_basemap_config._fetch_error = msg
			self.report({'WARNING'}, msg)

		return context.window_manager.invoke_props_dialog(self, width=460)

	def draw(self, context):
		layout = self.layout

		err  = IMPORTGIS_OT_custom_basemap_config._fetch_error
		caps = IMPORTGIS_OT_custom_basemap_config._last_caps
		if err:
			box = layout.box()
			box.label(text=err, icon='ERROR')
			op = box.operator("importgis.custom_basemap_fetch",
			                  text="Retry fetch", icon='FILE_REFRESH')
			op.baseUrl    = self.baseUrl
			op.wmsVersion = self.wmsVersion
		elif caps.get('layers'):
			layout.label(
				text="Found {} layers".format(len(caps['layers'])),
				icon='CHECKMARK')
		else:
			op = layout.operator("importgis.custom_basemap_fetch",
			                     text="Fetch layers", icon='URL')
			op.baseUrl    = self.baseUrl
			op.wmsVersion = self.wmsVersion

		layout.separator()
		layout.prop(self, 'sourceName')
		layout.prop(self, 'layerName')
		layout.prop(self, 'layerFormat')
		layout.prop(self, 'layerStyle')
		layout.prop(self, 'gridName')
		if self.serviceType == 'WMTS':
			layout.prop(self, 'matrixSet')
		row = layout.row()
		row.prop(self, 'zmin')
		row.prop(self, 'zmax')

	def execute(self, context):
		if self.layerName == 'NONE':
			self.report({'ERROR'}, "Fetch layers first")
			return {'CANCELLED'}
		if not self.sourceName.strip():
			# Auto-name from the URL's host
			self.sourceName = urlparse(self.baseUrl).netloc or self.baseUrl[:30]
		if self.serviceType == 'WMTS' and self.matrixSet == 'NONE':
			self.report({'ERROR'}, "Fetch layers first to get TileMatrixSet list")
			return {'CANCELLED'}

		# Build source entry reusing prefs.py helpers.
		# Imported lazily to avoid any chance of circular import at module load.
		from ..prefs import _build_source_entry, _entry_to_sources_dict

		# _build_source_entry reads op.layerId and op.sourceDesc.
		# Set them as plain Python attributes — they don't need to be
		# Blender properties.
		self.layerId    = self.layerName
		self.sourceDesc = ''

		key = 'custom_' + hashlib.md5(os.urandom(16)).hexdigest()[:8]
		entry = _build_source_entry(self, key)
		SOURCES[key] = _entry_to_sources_dict(entry)

		# Persist to prefs JSON so the source survives Blender restart.
		try:
			addon_pkg = __package__.split('.')[0]
			prefs = bpy.context.preferences.addons[addon_pkg].preferences
			saved = json.loads(prefs.customBasemapsJson)
			saved.append(entry)
			prefs.customBasemapsJson = json.dumps(saved)
		except Exception as e:
			log.warning("Could not persist custom basemap: {}".format(e))
			# Not fatal — source is in SOURCES for this session

		# Open the map viewer with the new source pre-selected.
		bpy.ops.view3d.map_start('INVOKE_DEFAULT',
			src=key, lay='LYR', grd=self.gridName)
		return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
	IMPORTGIS_OT_custom_basemap_url,
	IMPORTGIS_OT_custom_basemap_fetch,
	IMPORTGIS_OT_custom_basemap_config,
]


def register():
	for cls in classes:
		bpy.utils.register_class(cls)


def unregister():
	for cls in reversed(classes):
		bpy.utils.unregister_class(cls)
