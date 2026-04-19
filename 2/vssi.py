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
import ee
from osgeo import gdal

from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QFileDialog, QMessageBox, QTableWidgetItem
from qgis.core import QgsProject, QgsCoordinateReferenceSystem, QgsCoordinateTransform

from .resources import *
from .vssi_dialog import VegetationSeasonalStabilityIndexDialog

class VegetationSeasonalStabilityIndex:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = self.tr(u'&EcoLand-OS')
        self.first_start = None
    
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
        icon_path = ':/plugins/2/icon.png'
        self.add_action(icon_path, text=self.tr(u'VSSI Tool'), callback=self.run, parent=self.iface.mainWindow())
        self.first_start = True

    def unload(self):
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&EcoLand-OS'), action)
            self.iface.removeToolBarIcon(action)

    def run(self):
        if self.first_start == True:
            self.first_start = False
            self.dlg = VegetationSeasonalStabilityIndexDialog()
            
            self.dlg.btnRun.clicked.connect(self.run_gee_analysis)
            self.dlg.btnBrowse.clicked.connect(self.browse_local_folder)

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
        
        # Initialize Earth Engine
        try:
            # We initialize without a project string; it uses the default terminal auth
            ee.Initialize(project='')  ##### enter your project id here
        except Exception as e:
            QMessageBox.critical(None, "EE Error", f"Earth Engine failed to initialize. Please run 'earthengine authenticate' in your terminal.\n\nError: {e}")
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
            independents = ee.List(['constant', 't', 'cos1', 'sin1', 'cos2', 'sin2', 'cos3', 'sin3'])
            dependent = ee.String('NDVI')
            
            summaryFeatures = []

            for epoch in epochs:
                epochCol = baseModisCol.filterDate(epoch['start'], epoch['end'])

                def add_harmonics(image):
                    date = ee.Date(image.get('system:time_start'))
                    years = date.difference(ee.Date(epoch['start']), 'year')
                    timeRadians1 = years.multiply(2 * math.pi)
                    timeRadians2 = years.multiply(4 * math.pi)
                    timeRadians3 = years.multiply(6 * math.pi)
                    
                    return image \
                        .addBands(ee.Image.constant(1).rename('constant').float()) \
                        .addBands(ee.Image.constant(years).rename('t').float()) \
                        .addBands(ee.Image.constant(timeRadians1.cos()).rename('cos1').float()) \
                        .addBands(ee.Image.constant(timeRadians1.sin()).rename('sin1').float()) \
                        .addBands(ee.Image.constant(timeRadians2.cos()).rename('cos2').float()) \
                        .addBands(ee.Image.constant(timeRadians2.sin()).rename('sin2').float()) \
                        .addBands(ee.Image.constant(timeRadians3.cos()).rename('cos3').float()) \
                        .addBands(ee.Image.constant(timeRadians3.sin()).rename('sin3').float()) \
                        .set('t', years)

                withNDVI = epochCol.map(add_harmonics)
                harmonicRegression = withNDVI.select(independents.add(dependent)).reduce(ee.Reducer.linearRegression(independents.length(), 1))
                coef = harmonicRegression.select('coefficients').arrayProject([0]).arrayFlatten([independents])

                amp1 = coef.select('cos1').pow(2).add(coef.select('sin1').pow(2)).sqrt().rename('1_Amp_Annual')
                amp2 = coef.select('cos2').pow(2).add(coef.select('sin2').pow(2)).sqrt().rename('2_Amp_Biannual')
                amp3 = coef.select('cos3').pow(2).add(coef.select('sin3').pow(2)).sqrt().rename('3_Amp_Triannual')
                
                def calc_fitted(image):
                    base = image.select('constant').multiply(coef.select('constant')).add(image.select('t').multiply(coef.select('t')))
                    h1 = image.select('cos1').multiply(coef.select('cos1')).add(image.select('sin1').multiply(coef.select('sin1')))
                    h2 = image.select('cos2').multiply(coef.select('cos2')).add(image.select('sin2').multiply(coef.select('sin2')))
                    h3 = image.select('cos3').multiply(coef.select('cos3')).add(image.select('sin3').multiply(coef.select('sin3')))
                    return image.addBands(base.add(h1).add(h2).add(h3).rename('fitted'))

                fitted = withNDVI.map(calc_fitted)

                def calc_rmse(img):
                    return img.select('NDVI').subtract(img.select('fitted')).pow(2)

                rmse = fitted.map(calc_rmse).mean().sqrt().rename('4_RMSE_Instability')
                baseline = coef.select('constant').rename('5_Baseline_NDVI')
                trend = coef.select('t').rename('6_Trend_Slope')

                statsImage = ee.Image([amp1, amp2, amp3, rmse, baseline, trend])
                statsDict = statsImage.reduceRegion(reducer=ee.Reducer.mean(), geometry=roi, scale=250, maxPixels=1e9)
                statsDict = statsDict.set('Epoch', epoch['name'])
                summaryFeatures.append(statsDict)

            # PULL DATA FROM GOOGLE SERVERS TO QGIS
            # getInfo() is the magic Python command that downloads the EE math results
            final_data = ee.List(summaryFeatures).getInfo()

            for row in final_data:
                b0 = row.get('5_Baseline_NDVI')
                rmse = row.get('4_RMSE_Instability')
                a1 = row.get('1_Amp_Annual')
                a3 = row.get('3_Amp_Triannual')

                if b0 is None or rmse is None or a1 is None or a3 is None or a1 == 0:
                    row['7_VSSI'] = None
                else:
                    row['7_VSSI'] = round(b0 * (1 - rmse) * (1 - (a3 / a1)), 4)

            # POPULATE THE UI TABLE
            if final_data:
                headers = ['Epoch', '1_Amp_Annual', '2_Amp_Biannual', '3_Amp_Triannual', '4_RMSE_Instability', '5_Baseline_NDVI', '7_VSSI']
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
            self.dlg.btnRun.setText("Run Harmonic Regression")
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
            final_data = []

            for epoch in epochs:
                epoch_start_dt = np.datetime64(f"{epoch['start_year']}-01-01")
                epoch_files = [
                    (date_info, path) for date_info, path in dated_files
                    if epoch['start_year'] <= date_info[0] <= epoch['end_year']
                ]

                if not epoch_files:
                    final_data.append({
                        'Epoch': epoch['name'],
                        '1_Amp_Annual': None,
                        '2_Amp_Biannual': None,
                        '3_Amp_Triannual': None,
                        '4_RMSE_Instability': None,
                        '5_Baseline_NDVI': None,
                        '7_VSSI': None,
                    })
                    continue

                epoch_files.sort(key=lambda x: x[0])
                ndvi_arrays = []
                t_values = []
                raster_shape = None

                for (year, dt), tif_path in epoch_files:
                    ds = gdal.Open(tif_path)
                    if ds is None:
                        continue

                    arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
                    if raster_shape is None:
                        raster_shape = arr.shape
                    elif arr.shape != raster_shape:
                        continue

                    arr = np.where(arr == -28672, np.nan, arr * 0.0001)
                    ndvi_arrays.append(arr)

                    days_since_start = float((dt - epoch_start_dt) / np.timedelta64(1, 'D'))
                    t_values.append(days_since_start / 365.25)

                if not ndvi_arrays:
                    final_data.append({
                        'Epoch': epoch['name'],
                        '1_Amp_Annual': None,
                        '2_Amp_Biannual': None,
                        '3_Amp_Triannual': None,
                        '4_RMSE_Instability': None,
                        '5_Baseline_NDVI': None,
                        '7_VSSI': None,
                    })
                    continue

                ndvi_stack = np.stack(ndvi_arrays, axis=0)
                t = np.asarray(t_values, dtype=np.float32)

                X = np.column_stack([
                    np.ones_like(t),
                    t,
                    np.cos(2 * np.pi * t),
                    np.sin(2 * np.pi * t),
                    np.cos(4 * np.pi * t),
                    np.sin(4 * np.pi * t),
                    np.cos(6 * np.pi * t),
                    np.sin(6 * np.pi * t),
                ])

                T, H, W = ndvi_stack.shape
                Y = ndvi_stack.reshape(T, H * W)
                valid_cols = ~np.isnan(Y).any(axis=0)

                coef_map = np.full((8, H * W), np.nan, dtype=np.float32)
                rmse_map = np.full(H * W, np.nan, dtype=np.float32)

                if np.any(valid_cols):
                    coef_valid, _, _, _ = np.linalg.lstsq(X, Y[:, valid_cols], rcond=None)
                    coef_map[:, valid_cols] = coef_valid.astype(np.float32)

                    fitted_valid = X @ coef_valid
                    rmse_map[valid_cols] = np.sqrt(np.nanmean((Y[:, valid_cols] - fitted_valid) ** 2, axis=0)).astype(np.float32)

                amp1 = np.sqrt(coef_map[2] ** 2 + coef_map[3] ** 2)
                amp2 = np.sqrt(coef_map[4] ** 2 + coef_map[5] ** 2)
                amp3 = np.sqrt(coef_map[6] ** 2 + coef_map[7] ** 2)
                baseline = coef_map[0]
                ratio = np.full(H * W, np.nan, dtype=np.float32)
                valid_ratio = (~np.isnan(amp1)) & (~np.isnan(amp3)) & (amp1 != 0)
                ratio[valid_ratio] = amp3[valid_ratio] / amp1[valid_ratio]
                vssi = baseline * (1 - rmse_map) * (1 - ratio)

                row = {
                    'Epoch': epoch['name'],
                    '1_Amp_Annual': float(np.nanmean(amp1)) if np.any(~np.isnan(amp1)) else None,
                    '2_Amp_Biannual': float(np.nanmean(amp2)) if np.any(~np.isnan(amp2)) else None,
                    '3_Amp_Triannual': float(np.nanmean(amp3)) if np.any(~np.isnan(amp3)) else None,
                    '4_RMSE_Instability': float(np.nanmean(rmse_map)) if np.any(~np.isnan(rmse_map)) else None,
                    '5_Baseline_NDVI': float(np.nanmean(baseline)) if np.any(~np.isnan(baseline)) else None,
                    '7_VSSI': float(np.nanmean(vssi)) if np.any(~np.isnan(vssi)) else None,
                }
                final_data.append(row)

            headers = ['Epoch', '1_Amp_Annual', '2_Amp_Biannual', '3_Amp_Triannual', '4_RMSE_Instability', '5_Baseline_NDVI', '7_VSSI']
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
            self.dlg.btnRun.setText("Run Harmonic Regression")
            self.dlg.btnRun.setEnabled(True)
