# -*- coding: utf-8 -*-
import os
from qgis.PyQt import uic
from qgis.PyQt import QtWidgets

# This loads your .ui file so that PyQt can build your plugin window
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'vssi_dialog_base.ui'))

class VegetationSeasonalStabilityIndexDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None):
        """Constructor."""
        super(VegetationSeasonalStabilityIndexDialog, self).__init__(parent)
        self.setupUi(self)