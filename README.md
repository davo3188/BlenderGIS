Blender GIS
==========
Blender minimum version required : v2.83 — tested through Blender 5.x

Note : Since 2022, the OpenTopography web service requires an API key. Please register to opentopography.org and request a key. This service is still free.


[Wiki](https://github.com/domlysz/BlenderGIS/wiki/Home) - [FAQ](https://github.com/domlysz/BlenderGIS/wiki/FAQ) - [Quick start guide](https://github.com/domlysz/BlenderGIS/wiki/Quick-start) - [Flowchart](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/flowchart.jpg)
--------------------

## Functionalities overview

**GIS datafile import :** Import in Blender most common GIS data formats: Shapefile vector, GeoPackage (.gpkg), GeoJSON, ESRI ASCII Grid, raster image, GeoTIFF DEM, OpenStreetMap XML.

There are a lot of possibilities to create a 3D terrain from geographic data with BlenderGIS, check the [Flowchart](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/flowchart.jpg) to have an overview.

Exemple : import vector contour lines, create faces by triangulation and put a topographic raster texture.

![](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/Blender28x/gif/bgis_demo_delaunay.gif)

**Grab geodata directly from the web :** display dynamics web maps inside Blender 3d view, requests for OpenStreetMap data (buildings, roads ...), get true elevation data from the NASA SRTM mission.

![](https://raw.githubusercontent.com/wiki/domlysz/blenderGIS/Blender28x/gif/bgis_demo_webdata.gif)

**OGC and REST web service support :** Import vector features from any WFS endpoint or ArcGIS REST Feature Service (FeatureServer / MapServer), fetch imagery from a WMS server as a flat georeferenced plane or draped on an existing terrain mesh, and add custom WMS/WMTS basemaps to the live 3D map viewer (GetCapabilities is parsed automatically to populate layer and TileMatrixSet pickers). ArcGIS REST supports token authentication and automatic pagination for large datasets.

**And more :** Manage georeferencing informations of a scene, compute a terrain mesh by Delaunay triangulation, drop objects on a terrain mesh, make terrain analysis using shader nodes, setup new cameras from geotagged photos, setup a camera to render with Blender a new georeferenced raster.
