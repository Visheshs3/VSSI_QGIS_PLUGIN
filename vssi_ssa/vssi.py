# -*- coding: utf-8 -*-
"""
Vegetation Seasonal Stability Index - QGIS Plugin
"""
import os
import math
import json
import re
import glob
import numpy as np
import pandas as pd
import ee
from osgeo import gdal
from pyts.decomposition import SingularSpectrumAnalysis

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox, QTableWidgetItem
from qgis.core import QgsProject, QgsCoordinateReferenceSystem, QgsCoordinateTransform

from .resources import *
from .vssi_dialog import VegetationSeasonalStabilityIndexDialog

class VegetationSeasonalStabilityIndex:
    """QGIS Plugin Implementation."""

    SETTINGS_KEY_PROJECT_ID = 'vssi_plugin/gee_project_id'

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = self.tr(u'&EcoLand-OS')
        self.first_start = None
        self.ee_initialized = False
    
    def tr(self, message):
        return QCoreApplication.translate('VegetationSeasonalStabilityIndex', message)

    def add_action(self, icon_path, text, callback, enabled_flag=True, add_to_menu=True, add_to_toolbar=True, status_tip=None, whats_this=None, parent=None):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        icon_path = ':/plugins/vssi_harmonic_regression/icon.png'
        self.add_action(icon_path, text=self.tr(u'VSSI Tool'), callback=self.run, parent=self.iface.mainWindow())
        self.first_start = True

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&EcoLand-OS'), action)
            self.iface.removeToolBarIcon(action)

    def _load_saved_project_id(self):
        """Load the saved GEE project ID from QSettings."""
        return QSettings().value(self.SETTINGS_KEY_PROJECT_ID, '', type=str)

    def _save_project_id(self, project_id):
        """Persist the GEE project ID to QSettings."""
        QSettings().setValue(self.SETTINGS_KEY_PROJECT_ID, project_id)

    def authenticate_ee(self):
        """Run the Earth Engine browser-based authentication flow."""
        project_id = self.dlg.lineProjectId.text().strip()
        if not project_id:
            QMessageBox.warning(None, "Project ID Required",
                                "Please enter your Google Earth Engine project ID before authenticating.\n\n"
                                "You can find or create a project at:\nhttps://console.cloud.google.com/")
            return

        self.dlg.labelAuthStatus.setText("Authenticating...")
        self.dlg.btnAuthenticate.setEnabled(False)
        QCoreApplication.processEvents()

        try:
            ee.Authenticate()
            ee.Initialize(project=project_id)
            self.ee_initialized = True
            self._save_project_id(project_id)
            self.dlg.labelAuthStatus.setText("Authenticated ✓")
            self.dlg.labelAuthStatus.setStyleSheet("color: green; font-weight: bold;")
        except Exception as e:
            self.ee_initialized = False
            self.dlg.labelAuthStatus.setText("Authentication failed")
            self.dlg.labelAuthStatus.setStyleSheet("color: red;")
            QMessageBox.critical(None, "Authentication Error",
                                 f"Earth Engine authentication failed:\n{e}")
        finally:
            self.dlg.btnAuthenticate.setEnabled(True)

    def initialize_ee(self):
        """Try to initialize EE with saved credentials (no browser prompt)."""
        if self.ee_initialized:
            return True

        project_id = self.dlg.lineProjectId.text().strip()
        if not project_id:
            project_id = self._load_saved_project_id()

        if not project_id:
            return False

        try:
            ee.Initialize(project=project_id)
            self.ee_initialized = True
            self._save_project_id(project_id)
            self.dlg.labelAuthStatus.setText("Authenticated ✓")
            self.dlg.labelAuthStatus.setStyleSheet("color: green; font-weight: bold;")
            return True
        except Exception:
            self.ee_initialized = False
            return False

    def run(self):
        if self.first_start == True:
            self.first_start = False
            self.dlg = VegetationSeasonalStabilityIndexDialog()
            
            self.dlg.btnRun.clicked.connect(self.run_gee_analysis)
            self.dlg.btnBrowse.clicked.connect(self.browse_local_folder)
            self.dlg.btnAuthenticate.clicked.connect(self.authenticate_ee)

            # Restore saved project ID
            saved_id = self._load_saved_project_id()
            if saved_id:
                self.dlg.lineProjectId.setText(saved_id)
                # Try silent initialization with saved credentials
                if self.initialize_ee():
                    self.dlg.labelAuthStatus.setText("Authenticated ✓")
                    self.dlg.labelAuthStatus.setStyleSheet("color: green; font-weight: bold;")

        self.dlg.comboRoi.clear()
        layers = QgsProject.instance().mapLayers().values()
        for layer in layers:
            if layer.type() == layer.VectorLayer:
                self.dlg.comboRoi.addItem(layer.name(), layer.id())

        self.dlg.show()
        self.dlg.exec_()

    def browse_local_folder(self):
        folder = QFileDialog.getExistingDirectory(None, "Select NDVI folder")
        if folder:
            self.dlg.lineLocalPath.setText(folder)

    def run_gee_analysis(self):
        if self.dlg.comboMode.currentText() == "Local Files":
            self.run_local_analysis()
            return
        self.run_gee_mode()

    def run_gee_mode(self):
        """The core Earth Engine logic, translated to Python"""
        
        # Initialize Earth Engine using the integrated auth system
        if not self.initialize_ee():
            QMessageBox.warning(None, "EE Authentication Required",
                                "Earth Engine is not authenticated.\n\n"
                                "Please enter your GEE Project ID in the "
                                "'Earth Engine Authentication' section and click 'Authenticate'.")
            return

        # Grab inputs from UI
        start_year = self.dlg.spinStartYear.value()
        end_year = self.dlg.spinEndYear.value()
        interval = self.dlg.spinInterval.value()
        layer_id = self.dlg.comboRoi.currentData()
        
        # Setup QGIS ROI -> EE Geometry
        if not layer_id:
            # No layer selected? Use your default JavaScript coordinates!
            print("No QGIS layer found. Using default fallback ROI.")
            roi = ee.Geometry.Point([81.7770, 17.4430]).buffer(15000)
        else:
            # Convert actual QGIS polygon features to EE Geometry (not bounding box)
            layer = QgsProject.instance().mapLayer(layer_id)
            crsSrc = layer.crs()
            crsDest = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(crsSrc, crsDest, QgsProject.instance())

            ee_features = []
            for feat in layer.getFeatures():
                geom = feat.geometry()
                geom.transform(transform)
                geojson = json.loads(geom.asJson())
                ee_features.append(ee.Feature(ee.Geometry(geojson)))

            if ee_features:
                roi_fc = ee.FeatureCollection(ee_features)
                roi = roi_fc.geometry()
            else:
                QMessageBox.warning(None, "ROI Error", "Selected ROI layer has no valid features. Using fallback ROI.")
                roi = ee.Geometry.Point([81.7770, 17.4430]).buffer(15000)

        # Generate Epochs
        epochs = []
        for year in range(start_year, end_year + 1, interval):
            epoch_end = min(year + interval - 1, end_year)
            epochs.append({'name': f'{year}-{epoch_end}', 'start': f'{year}-01-01', 'end': f'{epoch_end}-12-31'})

        # Notify user processing has started (this can take a minute)
        self.dlg.btnRun.setText("Processing... Please Wait")
        self.dlg.btnRun.setEnabled(False)
        QCoreApplication.processEvents() # Keeps QGIS from freezing

        try:
            # TRANSLATED EARTH ENGINE MATH

            # Build 2003 MODIS forest baseline mask (LC_Type1 classes 1-5 = forest)
            land_cover_2003 = ee.ImageCollection('MODIS/061/MCD12Q1') \
                .filterDate('2003-01-01', '2003-12-31').first().select('LC_Type1')
            forest_mask_2003 = land_cover_2003.gte(1).And(land_cover_2003.lte(5))

            def maskMODIS(image):
                qa = image.select('SummaryQA')
                # Pixel must have good QA AND must have been a forest in 2003
                mask = qa.lte(1).And(forest_mask_2003)
                ndvi = image.select('NDVI').multiply(0.0001).rename('NDVI')
                return image.updateMask(mask).addBands(ndvi, None, True).copyProperties(image, ["system:time_start"])

            baseModisCol = ee.ImageCollection("MODIS/061/MOD13Q1").filterBounds(roi).map(maskMODIS)

            # 6. Extract spatially-averaged NDVI time series from GEE
            full_col = baseModisCol.filterDate(f'{start_year}-01-01', f'{end_year}-12-31')

            def extract_mean_ndvi(image):
                mean_dict = image.select('NDVI').reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=roi,
                    scale=250,
                    maxPixels=1e9
                )
                return ee.Feature(None, {
                    'NDVI': mean_dict.get('NDVI'),
                    'date': ee.Date(image.get('system:time_start')).format('YYYY-MM-dd')
                })

            ndvi_fc = full_col.map(extract_mean_ndvi)
            # getInfo() downloads the time series from Google servers
            ndvi_list = ndvi_fc.getInfo()

            # 7. Build pandas time series from GEE results
            records = []
            for f in ndvi_list['features']:
                props = f['properties']
                if props.get('NDVI') is not None:
                    records.append({'date': props['date'], 'NDVI': props['NDVI']})

            if not records:
                QMessageBox.warning(None, "Data Error", "No valid NDVI data returned from GEE.")
                return

            df = pd.DataFrame(records)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').set_index('date')

            # MODIS MOD13Q1 is a 16-day composite.
            # Resample to 16 days to ensure strict mathematical regularity.
            ndvi_ts = df['NDVI'].resample('16D').mean().interpolate(method='linear')

            if len(ndvi_ts) < 4:
                QMessageBox.warning(None, "Data Error", "Insufficient NDVI data for SSA decomposition.")
                return

            # 8. Singular Spectrum Analysis
            # For a 16-day interval, one year ≈ 23 observations (365/16 ≈ 22.8).
            # Setting the window size to 23 forces the SVD to isolate the annual cycle.
            window_size = min(23, len(ndvi_ts) // 2)
            ssa = SingularSpectrumAnalysis(window_size=window_size, groups=None)

            X = ndvi_ts.values.reshape(1, -1)
            X_ssa = ssa.fit_transform(X)

            # Extract components: eigenvector 1 = trend, eigenvectors 2&3 = seasonality
            trend = X_ssa[0, 0]
            seasonality = X_ssa[0, 1] + X_ssa[0, 2]

            # Build a components DataFrame for easy epoch slicing
            components_df = pd.DataFrame({
                'NDVI': ndvi_ts.values,
                'Trend': trend,
                'Seasonality': seasonality,
            }, index=ndvi_ts.index)

            # 9. Calculate epoch-wise VSSI = Var(seasonality) / Var(total NDVI)
            final_data = []
            for epoch in epochs:
                mask = (components_df.index >= pd.to_datetime(epoch['start'])) & \
                       (components_df.index <= pd.to_datetime(epoch['end']))
                epoch_data = components_df.loc[mask]

                if len(epoch_data) > 0:
                    var_seasonal = float(np.var(epoch_data['Seasonality']))
                    var_total = float(np.var(epoch_data['NDVI']))
                    vssi = round(var_seasonal / var_total, 4) if var_total > 0 else np.nan
                    row = {
                        'Epoch': epoch['name'],
                        '1_Trend_Mean': round(float(np.mean(epoch_data['Trend'])), 4),
                        '2_Var_Seasonal': round(var_seasonal, 6),
                        '3_Var_Total': round(var_total, 6),
                        '4_VSSI': vssi,
                    }
                else:
                    row = {
                        'Epoch': epoch['name'],
                        '1_Trend_Mean': None,
                        '2_Var_Seasonal': None,
                        '3_Var_Total': None,
                        '4_VSSI': None,
                    }
                final_data.append(row)

            # 10. POPULATE THE UI TABLE
            headers = ['Epoch', '1_Trend_Mean', '2_Var_Seasonal', '3_Var_Total', '4_VSSI']
            self.dlg.tableResults.setColumnCount(len(headers))
            self.dlg.tableResults.setRowCount(len(final_data))
            self.dlg.tableResults.setHorizontalHeaderLabels(headers)

            for row_idx, row_data in enumerate(final_data):
                for col_idx, key in enumerate(headers):
                    val = row_data.get(key, "N/A")
                    # Format numbers nicely if they aren't the Epoch string
                    if isinstance(val, str):
                        display_text = val
                    elif val is None:
                        display_text = "N/A"
                    else:
                        display_text = f"{val:.4f}"
                    self.dlg.tableResults.setItem(row_idx, col_idx, QTableWidgetItem(display_text))

            self.dlg.tableResults.resizeColumnsToContents()

        except Exception as e:
            QMessageBox.critical(None, "Processing Error", f"An error occurred during GEE processing:\n{str(e)}")
        finally:
            # Reset button state
            self.dlg.btnRun.setText("Run SSA")
            self.dlg.btnRun.setEnabled(True)

    def run_local_analysis(self):
        ndvi_folder = self.dlg.lineLocalPath.text().strip()
        if not ndvi_folder or not os.path.isdir(ndvi_folder):
            QMessageBox.warning(None, "Input Error", "Please select a valid NDVI folder.")
            return

        tif_files = sorted(glob.glob(os.path.join(ndvi_folder, '*.tif')))
        if not tif_files:
            QMessageBox.warning(None, "Input Error", "No .tif files found in the selected folder.")
            return

        date_pattern = re.compile(r'(\d{4}-\d{2}-\d{2})')
        dated_files = []
        for tif_path in tif_files:
            match = date_pattern.search(os.path.basename(tif_path))
            if not match:
                continue
            date_str = match.group(1)
            try:
                dt = np.datetime64(date_str)
            except Exception:
                continue
            year = int(date_str[0:4])
            dated_files.append(((year, dt), tif_path))

        if not dated_files:
            QMessageBox.warning(None, "Input Error", "No NDVI files matched date pattern YYYY-MM-DD.")
            return

        start_year = self.dlg.spinStartYear.value()
        end_year = self.dlg.spinEndYear.value()
        interval = self.dlg.spinInterval.value()

        epochs = []
        for year in range(start_year, end_year + 1, interval):
            epoch_end = min(year + interval - 1, end_year)
            epochs.append({'name': f'{year}-{epoch_end}', 'start_year': year, 'end_year': epoch_end})

        self.dlg.btnRun.setText("Processing... Please Wait")
        self.dlg.btnRun.setEnabled(False)
        QCoreApplication.processEvents()

        try:
            # 1. Read all rasters and compute spatial mean NDVI per date
            records = []
            for (year, dt), tif_path in dated_files:
                if not (start_year <= year <= end_year):
                    continue

                ds = gdal.Open(tif_path)
                if ds is None:
                    continue

                arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
                arr = np.where(arr == -28672, np.nan, arr * 0.0001)
                mean_ndvi = float(np.nanmean(arr))
                if not np.isnan(mean_ndvi):
                    records.append({'date': str(dt), 'NDVI': mean_ndvi})

            if not records:
                QMessageBox.warning(None, "Data Error", "No valid NDVI data found in the selected files.")
                return

            # 2. Build pandas time series
            df = pd.DataFrame(records)
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').set_index('date')

            # Resample to 16-day composites to ensure regularity and interpolate gaps
            ndvi_ts = df['NDVI'].resample('16D').mean().interpolate(method='linear')

            if len(ndvi_ts) < 4:
                QMessageBox.warning(None, "Data Error", "Insufficient NDVI data for SSA decomposition.")
                return

            # 3. Singular Spectrum Analysis on the full time series
            # Window size of 23 ≈ one year of 16-day composites
            window_size = min(23, len(ndvi_ts) // 2)
            ssa = SingularSpectrumAnalysis(window_size=window_size, groups=None)

            X = ndvi_ts.values.reshape(1, -1)
            X_ssa = ssa.fit_transform(X)

            # Extract components: eigenvector 1 = trend, eigenvectors 2&3 = seasonality
            trend = X_ssa[0, 0]
            seasonality = X_ssa[0, 1] + X_ssa[0, 2]

            components_df = pd.DataFrame({
                'NDVI': ndvi_ts.values,
                'Trend': trend,
                'Seasonality': seasonality,
            }, index=ndvi_ts.index)

            # 4. Calculate epoch-wise VSSI = Var(seasonality) / Var(total NDVI)
            final_data = []
            for epoch in epochs:
                start_dt = pd.to_datetime(f"{epoch['start_year']}-01-01")
                end_dt = pd.to_datetime(f"{epoch['end_year']}-12-31")
                mask = (components_df.index >= start_dt) & (components_df.index <= end_dt)
                epoch_data = components_df.loc[mask]

                if len(epoch_data) > 0:
                    var_seasonal = float(np.var(epoch_data['Seasonality']))
                    var_total = float(np.var(epoch_data['NDVI']))
                    vssi = round(var_seasonal / var_total, 4) if var_total > 0 else np.nan
                    row = {
                        'Epoch': epoch['name'],
                        '1_Trend_Mean': round(float(np.mean(epoch_data['Trend'])), 4),
                        '2_Var_Seasonal': round(var_seasonal, 6),
                        '3_Var_Total': round(var_total, 6),
                        '4_VSSI': vssi,
                    }
                else:
                    row = {
                        'Epoch': epoch['name'],
                        '1_Trend_Mean': None,
                        '2_Var_Seasonal': None,
                        '3_Var_Total': None,
                        '4_VSSI': None,
                    }
                final_data.append(row)

            # 5. Populate the UI table
            headers = ['Epoch', '1_Trend_Mean', '2_Var_Seasonal', '3_Var_Total', '4_VSSI']
            self.dlg.tableResults.setColumnCount(len(headers))
            self.dlg.tableResults.setRowCount(len(final_data))
            self.dlg.tableResults.setHorizontalHeaderLabels(headers)

            for row_idx, row_data in enumerate(final_data):
                for col_idx, key in enumerate(headers):
                    val = row_data.get(key, "N/A")
                    if isinstance(val, str):
                        display_text = val
                    elif val is None or (isinstance(val, float) and np.isnan(val)):
                        display_text = "N/A"
                    else:
                        display_text = f"{val:.4f}"
                    self.dlg.tableResults.setItem(row_idx, col_idx, QTableWidgetItem(display_text))

            self.dlg.tableResults.resizeColumnsToContents()

        except Exception as e:
            QMessageBox.critical(None, "Processing Error", f"An error occurred during local processing:\n{str(e)}")
        finally:
            self.dlg.btnRun.setText("Run SSA")
            self.dlg.btnRun.setEnabled(True)
