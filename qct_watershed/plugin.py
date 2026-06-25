# -*- coding: utf-8 -*-
"""
QCT Watershed Delineation – part of the QCivilTools suite.
Registers under  QCivilTools > Watershed Delineation  in the QGIS menu bar.
Author: Dat Vu  |  https://github.com/datmast-cmd
"""
import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication
from .qct_menu_helper import get_or_create_qct_menu, remove_action_from_qct_menu


class QCTWatershedPlugin:
    def __init__(self, iface):
        self.iface      = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.dialog     = None
        self.action     = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icons', 'watershed.png')
        icon = (QIcon(icon_path) if os.path.exists(icon_path)
                else QgsApplication.getThemeIcon('/mActionRunModel.svg'))
        self.action = QAction(icon, "Watershed Delineation", self.iface.mainWindow())
        self.action.setToolTip("QCivilTools – Watershed Delineation (WhiteboxTools)")
        self.action.triggered.connect(self.run)
        get_or_create_qct_menu(self.iface).addAction(self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        remove_action_from_qct_menu(self.iface, self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.action:
            self.action.deleteLater()

    def run(self):
        from .dialog import WatershedDelineationDialog
        if self.dialog is None:
            self.dialog = WatershedDelineationDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
