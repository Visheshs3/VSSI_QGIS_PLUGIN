EcoLand-OS (VSSI Tool) - QGIS Plugin

Overview
--------
EcoLand-OS calculates Vegetation Seasonal Stability Index (VSSI) from:

1) Google Earth Engine MODIS time series, or
2) Local NDVI GeoTIFF time series.

The implemented VSSI formula is:
    VSSI = β0 × (1 - RMSE) × (1 - A3/A1)


Requirements
------------
- QGIS 3.x
- Python dependencies available in your QGIS Python environment:
  - numpy
  - earthengine-api (for GEE mode)
  - osgeo.gdal


Installation (Local Plugin)
---------------------------
1. Copy this plugin folder into your QGIS profile plugin directory.
2. Open QGIS -> Plugins -> Manage and Install Plugins.
3. Enable "EcoLand-OS".


Earth Engine Setup (GEE Mode)
-----------------------------
1. Authenticate once on your system:
      earthengine authenticate

2. If your Earth Engine account requires a Cloud project, set environment
   variable EE_PROJECT_ID before launching QGIS.

   Example (Linux):
      export EE_PROJECT_ID="your-gcp-project-id"
      qgis

   The plugin automatically reads EE_PROJECT_ID. If not set, it initializes
   Earth Engine with your default authenticated context.


How to Use
----------
1. Add a polygon ROI layer in QGIS (optional; plugin has fallback ROI).
2. Open EcoLand-OS -> VSSI Tool.
3. Select mode:
   - GEE: computes from MODIS/061/MOD13Q1.
   - Local Files: computes from NDVI .tif files in a folder.
4. Set Start Year, End Year, and Interval.
5. Click "Run Harmonic Regression".


Local Files Input Format
------------------------
- Folder must contain .tif files.
- Each filename must include a date string in format YYYY-MM-DD.
  Example: NDVI_2021-06-15.tif
- Raster should be single-band NDVI with scale factor 0.0001.
- NoData value expected by the plugin: -28672.
- Rasters in one run should have the same width/height.


Output Columns
--------------
- Epoch
- 1_Amp_Annual (A1)
- 2_Amp_Biannual
- 3_Amp_Triannual (A3)
- 4_RMSE_Instability (RMSE)
- 5_Baseline_NDVI (β0)
- 7_VSSI


Troubleshooting
---------------
- "Earth Engine failed to initialize":
  Re-run authentication and/or set EE_PROJECT_ID, then restart QGIS.
- "No NDVI files matched date pattern YYYY-MM-DD":
  Rename files to include valid dates.
- Empty or N/A results:
  Check NoData coverage, raster consistency, and selected year range.


Publishing Notes
----------------
- Keep metadata.txt updated (version, homepage, repository, tracker).
- Increment version before each release.
- Ensure icon.png and description are final before publishing to the QGIS
  plugin repository.
