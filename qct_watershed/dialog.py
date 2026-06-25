# -*- coding: utf-8 -*-
"""
QCT Watershed Delineation Dialog — Two-phase UI  v1.0.0
Author : Dat Vu | https://github.com/datmast-cmd
"""
import os
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QGroupBox, QPushButton, QLabel, QLineEdit, QFileDialog,
    QProgressBar, QTextEdit, QTabWidget, QWidget, QDoubleSpinBox,
    QCheckBox, QMessageBox, QComboBox, QTextBrowser, QFrame,
    QSpinBox, QSizePolicy, QScrollArea, QSplitter,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QTimer
from qgis.PyQt.QtGui import QFont, QTextCursor

from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer,
    QgsMapLayerProxyModel, QgsLineSymbol,
    QgsPointXY, QgsGeometry, QgsFeature, QgsField,
    QgsFields, QgsWkbTypes, QgsCoordinateReferenceSystem,
    QgsVectorFileWriter, QgsCoordinateTransformContext,
)
from qgis.gui import QgsMapLayerComboBox, QgsMapToolEmitPoint
from qgis.PyQt.QtCore import QVariant

try:
    from .qct_style import DIALOG_STYLE, make_header, PRIMARY
except ImportError:
    DIALOG_STYLE = ""; make_header = None; PRIMARY = "#2c5f8a"

from .processor import WatershedProcessor


# ────────────────────────────────────────────────── worker thread ──
class WorkerThread(QThread):
    log_signal      = pyqtSignal(str, str)
    progress_signal = pyqtSignal(int)
    finished_signal = pyqtSignal(bool, str, object)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn     = fn
        self.args   = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
            if len(result) == 3:
                ok, msg, extra = result
            else:
                ok, msg = result
                extra   = None
            self.finished_signal.emit(ok, msg, extra)
        except Exception as e:
            import traceback
            self.finished_signal.emit(False, traceback.format_exc(), None)


# ─────────────────────────────────────────────────────────── dialog ──
class WatershedDelineationDialog(QDialog):

    # ── Output layer definitions ──────────────────────────────────────────
    PHASE1_OUTPUTS = [
        ("WBT_Filled_DEM.tif",           "Filled DEM",              "raster", False),
        ("WBT_D8_Pointer.tif",           "D8 Flow Direction",       "raster", False),
        ("WBT_D8_FlowAccumu.tif",        "D8 Flow Accumulation",    "raster", False),
        ("WBT_ExtractStreams.tif",        "Stream Network (raster)", "raster", False),
        ("WBT_ExtractStreams_vector.shp", "Stream Network (vector)", "vector", False),
    ]
    PHASE2_OUTPUTS = [
        ("outlet_snapped.shp",           "Outlet Snapped",            "vector", False),
        ("WBT_Watershed.tif",            "Watershed (raster)",        "raster", False),
        ("WBT_Watershed_Boundary.shp",   "Watershed Boundary",        "vector", False),
        ("WBT_UnnestBasins.tif",         "UnnestBasins (numbered)",   "raster", False),
        ("WBT_LongestFlowPath.shp",      "Longest Flow Path",         "vector", False),
        ("WBT_Subbasins.tif",            "Subbasins (raster)",        "raster", False),
        ("WBT_Subbasins_Info.shp",       "★ Subbasins Info",          "vector", False),
        ("WBT_AllDEM_Subbasins.shp",     "★ All-DEM Subbasins",       "vector", False),
    ]

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface         = iface
        self.processor     = WatershedProcessor()
        self.worker        = None
        self._out_checks   = {}   # fname -> QCheckBox
        self._phase1_done  = False
        self._output_dir   = ""
        self._streams_path = None
        self._dem_crs      = None   # QgsCoordinateReferenceSystem of input DEM
        self._map_tool     = None   # QgsMapToolEmitPoint for outlet picking
        self._prev_map_tool = None  # restore after picking
        self._outlet_layer = None   # scratch memory layer for clicked outlets
        self._outlet_points = []    # list of QgsPointXY
        self._loaded_layer_ids = []  # QGIS layer IDs added by this plugin session

        self.setWindowTitle("QCivilTools – Watershed Delineation (WhiteboxTools)")
        self.setMinimumWidth(820)
        self.setMinimumHeight(900)
        self._build_ui()

    # ══════════════════════════════════════════════════════════ build UI ══
    def _build_ui(self):
        ml = QVBoxLayout(self)
        ml.setContentsMargins(6, 6, 6, 6)
        ml.setSpacing(4)

        # ── Splitter: left = tabs + controls, right = help ────────
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Left panel
        left = QWidget()
        ll   = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_phase1_tab(), "Phase 1 — DEM & Streams")
        self.tabs.addTab(self._build_phase2_tab(), "Phase 2 — Watershed & Subbasins")
        self.tabs.addTab(self._build_log_tab(),    "Log")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        ll.addWidget(self.tabs)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)
        ll.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready — configure Phase 1 inputs and click Run Phase 1.")
        ll.addWidget(self.status_label)

        # Button row
        btn = QHBoxLayout()
        self.p1_btn = QPushButton("▶  Run Phase 1  (DEM → Streams)")
        self.p1_btn.clicked.connect(self.on_run_phase1)

        self.p2_btn = QPushButton("▶  Run Phase 2  (→ Watershed / Subbasins)")
        self.p2_btn.setEnabled(False)
        self.p2_btn.clicked.connect(self.on_run_phase2)

        self.cancel_btn = QPushButton("✕  Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.on_cancel)

        self.load_btn = QPushButton("🗺  Load Selected Outputs")
        self.load_btn.setEnabled(False)
        self.load_btn.clicked.connect(self.on_load_layers)

        btn.addWidget(self.p1_btn)
        btn.addWidget(self.p2_btn)
        btn.addWidget(self.cancel_btn)
        btn.addWidget(self.load_btn)
        ll.addLayout(btn)

        splitter.addWidget(left)
        splitter.addWidget(self._build_help_panel())
        splitter.setSizes([580, 280])
        ml.addWidget(splitter)
        # Initialise help for tab 0
        self._on_tab_changed(0)

    def _build_help_panel(self):
        """Right-hand guide panel — content switches with active tab."""
        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(4, 0, 0, 0)
        pl.setSpacing(0)
        self.help_browser = QTextBrowser()
        self.help_browser.setOpenExternalLinks(True)
        pl.addWidget(self.help_browser)
        return panel

    def _on_tab_changed(self, idx):
        if not hasattr(self, "help_browser"):
            return
        if idx == 0:
            self.help_browser.setHtml(self._help_html_phase1())
        elif idx == 1:
            self.help_browser.setHtml(self._help_html_phase2())
        else:
            self.help_browser.setHtml(self._help_html_log())
        return """
        <style>
          body{font-family:"Segoe UI",Arial,sans-serif;font-size:10px;color:#1c2833;margin:6px;}
          h2{color:#1a5276;font-size:12px;margin:4px 0 2px;}
          h3{color:#1a5276;font-size:10px;margin:8px 0 2px;
             border-bottom:1px solid #d5d8dc;padding-bottom:2px;}
          .step{background:#eaf4fb;border-left:3px solid #1a5276;
                margin:3px 0;padding:5px 8px;border-radius:0 3px 3px 0;}
          .note{background:#fef9e7;border-left:3px solid #d68910;
                margin:3px 0;padding:5px 8px;border-radius:0 3px 3px 0;}
          .ok{background:#eafaf1;border-left:3px solid #1e8449;
              margin:3px 0;padding:5px 8px;border-radius:0 3px 3px 0;}
          code{background:#eaecee;padding:1px 4px;border-radius:2px;font-size:9px;}
          p{margin:3px 0;}
        </style>

        <h2>🌊 Watershed Delineation</h2>
        <p>Two-phase workflow using <b>WhiteboxTools</b> to delineate watersheds and subbasins from a DEM.</p>

        <h3>Phase 1 — DEM &amp; Stream Network</h3>
        <div class="step">
        <b>Step 1</b> — Fill Depressions (<code>FillDepressionsWangAndLiu</code>)<br>
        <b>Step 2</b> — D8 Flow Direction (<code>D8Pointer</code>)<br>
        <b>Step 3</b> — Flow Accumulation (<code>D8FlowAccumulation</code>)<br>
        <b>Step 4</b> — Extract Streams (<code>ExtractStreams</code>) — threshold from area or cell count<br>
        <b>Step 5</b> — Vectorize streams → loaded to map
        </div>
        <div class="note">After Phase 1, inspect the stream network on the map.<br>
        Place outlet point(s) exactly on a stream cell, then run Phase 2.</div>

        <h3>Phase 2 — With Outlet</h3>
        <div class="step">
        <b>Step 6</b> — Snap Pour Points (<code>JensonSnapPourPoints</code>)<br>
        <b>Step 7</b> — Delineate Watershed (<code>Watershed</code>)<br>
        <b>Step 8</b> — Vectorize Watershed boundary<br>
        <b>Step 9</b> — UnnestBasins (optional)<br>
        <b>Step 10</b> — Longest Flow Path — whole watershed (optional)<br>
        <b>Step 11</b> — Subbasins raster (masked to watershed)<br>
        <b>Step 12</b> — <b>WBT_Subbasins_Info.shp</b> — per-subbasin attributes<br>
        <b>Step 13</b> — <b>WBT_AllDEM_Subbasins.shp</b> — full DEM subbasins
        </div>

        <h3>Phase 2 — No Outlet</h3>
        <div class="step">
        Skips steps 6–10. Runs <b>Subbasins</b> across the full DEM extent.<br>
        Produces <b>WBT_AllDEM_Subbasins.shp</b> only.
        </div>

        <h3>Output attributes (Subbasins shapefiles)</h3>
        <div class="ok">
        <code>SB_ID</code> — subbasin ID<br>
        <code>AREA_M2</code>, <code>AREA_HA</code> — area<br>
        <code>LFP_LEN</code> — longest flow path length (m)<br>
        <code>LFP_UP</code>, <code>LFP_DN</code> — upstream / downstream elevation<br>
        <code>LFP_SLP</code> — average slope (%)<br>
        <code>SLP_EA</code> — equal-area slope % (Taylor-Schwartz)
        </div>

        <h3>Tips</h3>
        <div class="note">
        • DEM must be in a <b>projected CRS</b> (e.g. NZTM2000 EPSG:2193).<br>
        • Lower stream threshold = denser network = more subbasins.<br>
        • Snap distance must be &gt; DEM cell size.<br>
        • WhiteboxTools must be installed — leave path blank to auto-detect.
        </div>
        """

    # ────────────────────────── per-tab help content ──────────────────────
    _HELP_CSS = """
        <style>
          body{font-family:"Segoe UI",Arial,sans-serif;font-size:10px;color:#1c2833;margin:6px;}
          h2{color:#1a5276;font-size:12px;margin:4px 0 2px;}
          h3{color:#1a5276;font-size:10px;margin:8px 0 2px;
             border-bottom:1px solid #d5d8dc;padding-bottom:2px;}
          .step{background:#eaf4fb;border-left:3px solid #1a5276;
                margin:3px 0;padding:5px 8px;border-radius:0 3px 3px 0;}
          .note{background:#fef9e7;border-left:3px solid #d68910;
                margin:3px 0;padding:5px 8px;border-radius:0 3px 3px 0;}
          .ok{background:#eafaf1;border-left:3px solid #1e8449;
              margin:3px 0;padding:5px 8px;border-radius:0 3px 3px 0;}
          .dl{background:#f0eafb;border-left:3px solid #7d3c98;
              margin:3px 0;padding:5px 8px;border-radius:0 3px 3px 0;}
          code{background:#eaecee;padding:1px 4px;border-radius:2px;font-size:9px;}
          a{color:#1a5276;}
          p{margin:3px 0;}
          li{margin:2px 0;}
        </style>"""

    def _help_html_phase1(self):
        return self._HELP_CSS + """
        <h2>🌊 Phase 1 — DEM &amp; Stream Network</h2>
        <p>Preprocesses your DEM and extracts a stream network using WhiteboxTools.</p>

        <h3>Steps</h3>
        <div class="step">
          <b>Step 1</b> — Fill Depressions <code>FillDepressionsWangAndLiu</code><br>
          <b>Step 2</b> — D8 Flow Direction <code>D8Pointer</code><br>
          <b>Step 3</b> — Flow Accumulation <code>D8FlowAccumulation</code><br>
          <b>Step 4</b> — Extract Streams <code>ExtractStreams</code> — threshold from area or cell count<br>
          <b>Step 5</b> — Vectorize streams → <i>outputs saved to disk</i>
        </div>

        <div class="note">
          ⚠️ <b>Stream network is NOT loaded automatically.</b><br>
          After Phase 1, click <b>"🗺 Load Selected Outputs"</b> to add layers to the map.<br>
          Then place outlet point(s) on the stream and run Phase 2.
        </div>

        <h3>WhiteboxTools — Install &amp; Setup</h3>
        <div class="dl">
          <b>Option A — Bundled ZIP (recommended)</b><br>
          Download WBT from the plugin repository:<br>
          <a href="https://github.com/QCivilTools/QCivilTools-Watershed-delineation/blob/main/WhiteboxTools.zip">
          📦 WhiteboxTools.zip (plugin repo)</a><br>
          Extract to a folder and point the plugin to <code>whitebox_tools.exe</code>.
        </div>
        <div class="dl">
          <b>Option B — Official release</b><br>
          <a href="https://github.com/jblindsay/whitebox-tools/releases">
          github.com/jblindsay/whitebox-tools/releases</a><br>
          Download the binary for your OS and extract.
        </div>
        <div class="dl">
          <b>Option C — QGIS Plugin</b><br>
          Install <b>WhiteboxTools for Processing</b> from the QGIS Plugin Repository
          (Plugins → Manage → search "WhiteboxTools").<br>
          This adds WBT as a QGIS Processing provider; the plugin will detect it automatically.
        </div>

        <h3>WBT Path</h3>
        <div class="step">
          Click <b>Browse Folder…</b> and select the folder where you extracted WhiteboxTools.<br>
          The plugin will automatically find <code>whitebox_tools.exe</code> (Windows) or
          <code>whitebox_tools</code> (Linux/macOS) inside that folder.<br><br>
          Leave blank to auto-detect from:<br>
          • System <code>PATH</code><br>
          • <code>C:\\WBT\\</code><br>
          • <code>C:\\whitebox_tools\\</code><br>
          • <code>C:\\WhiteboxTools_win_amd64\\WBT\\</code><br>
          • <code>~/WBT/</code> (Linux/macOS)<br>
          • QGIS Processing provider setting
        </div>

        <h3>QGIS Version Compatibility</h3>
        <div class="ok">
          ✅ QGIS 3.16+ (LTR) — fully supported<br>
          ✅ QGIS 3.22, 3.28, 3.34, 3.36, 3.40, 3.44 — tested<br>
          ✅ QGIS 3.x on Windows, Linux, macOS<br>
          ⚠️ QGIS 2.x — not supported
        </div>

        <h3>DEM Tips</h3>
        <div class="note">
          • DEM must be in a <b>projected CRS</b> (e.g. NZTM2000 EPSG:2193).<br>
          • All output files will share the DEM's CRS automatically.<br>
          • Lower stream threshold = denser network = more subbasins.<br>
          • Typical: 50 ha on a 1 m DEM ≈ 500,000 cells.
        </div>"""

    def _help_html_phase2(self):
        return self._HELP_CSS + """
        <h2>🎯 Phase 2 — Watershed &amp; Subbasins</h2>
        <p>Delineates watershed and subbasins from your outlet point(s).</p>

        <h3>Adding Outlet Points</h3>
        <div class="step">
          <b>Method A — Click on Map</b> (recommended)<br>
          Click <b>"📍 Pick Outlets on Map"</b> — then click directly on the stream
          network in the QGIS canvas. Each click adds a point.<br>
          Multi-point: keep clicking to add more outlets. Click
          <b>"✅ Done Picking"</b> when finished.<br><br>
          <b>Method B — Existing layer</b><br>
          Select a point layer from the map or browse to a .shp file.
        </div>

        <div class="note">
          ⚠️ Outlet points <b>must lie on or very near a stream cell</b>.<br>
          Use the snap distance to pull them onto the nearest stream.
        </div>

        <h3>Steps (with outlet)</h3>
        <div class="step">
          <b>Step 6</b> — Snap Pour Points <code>JensonSnapPourPoints</code><br>
          <b>Step 7</b> — Delineate Watershed <code>Watershed</code><br>
          <b>Step 8</b> — Vectorize Watershed boundary<br>
          <b>Step 9</b> — UnnestBasins (optional)<br>
          <b>Step 10</b> — Longest Flow Path (optional)<br>
          <b>Step 11</b> — Subbasins raster (masked to watershed)<br>
          <b>Step 12</b> ★ — <b>WBT_Subbasins_Info.shp</b> — per-subbasin attributes<br>
          <b>Step 13</b> ★ — <b>WBT_AllDEM_Subbasins.shp</b> — full DEM subbasins
        </div>

        <h3>Steps (no outlet)</h3>
        <div class="step">
          Steps 6–10 skipped. Produces <b>WBT_AllDEM_Subbasins.shp</b> only.
        </div>

        <h3>Output Attributes</h3>
        <div class="ok">
          <code>SB_ID</code> — subbasin ID<br>
          <code>AREA_M2</code>, <code>AREA_HA</code> — area<br>
          <code>LFP_LEN</code> — longest flow path (m)<br>
          <code>LFP_UP</code>, <code>LFP_DN</code> — upstream/downstream elevation<br>
          <code>LFP_SLP</code> — average slope (%)<br>
          <code>SLP_EA</code> — equal-area slope (Taylor-Schwartz)
        </div>

        <h3>CRS</h3>
        <div class="ok">
          ✅ All output layers are written and loaded with the <b>same CRS as the input DEM</b>.<br>
          Outlet points are automatically reprojected to match the DEM if needed.
        </div>

        <h3>Addons &amp; More Tools</h3>
        <div class="dl">
          <a href="https://github.com/QCivilTools/QCivilTools-Watershed-delineation">
          🔗 QCivilTools Watershed Delineation — Addons &amp; extras</a>
        </div>"""

    def _help_html_log(self):
        return self._HELP_CSS + """
        <h2>📋 Log</h2>
        <p>All WBT commands and processing messages are shown here.</p>
        <div class="step">
          <b>✅ SUCCESS</b> — step completed<br>
          <b>▶ STEP</b> — processing step started<br>
          <b>⚠️ WARNING</b> — non-fatal issue<br>
          <b>❌ ERROR</b> — processing stopped
        </div>
        <div class="note">
          If a step fails, copy the error text and check:<br>
          • WBT executable path is correct<br>
          • DEM is in a projected CRS<br>
          • Output directory is writable<br>
          • WhiteboxTools version ≥ 2.3
        </div>

        <h3>🔗 Plugin Repository</h3>
        <div class="dl">
          <a href="https://github.com/QCivilTools/QCivilTools-Watershed-delineation">
          github.com/QCivilTools/QCivilTools-Watershed-delineation</a><br>
          Source code, releases, addons, and WhiteboxTools bundled ZIP.
        </div>
        <div class="dl">
          📦 <a href="https://github.com/QCivilTools/QCivilTools-Watershed-delineation/blob/main/WhiteboxTools.zip">
          Download WhiteboxTools.zip</a> — bundled WBT binary (Windows/Linux/macOS)
        </div>

        <h3>🐛 Issues &amp; Feedback</h3>
        <div class="step">
          <a href="https://github.com/QCivilTools/QCivilTools-Watershed-delineation/issues">
          Report a bug or request a feature</a>
        </div>

        <h3>👤 Author</h3>
        <div class="ok">
          <b>Dat Vu</b><br>
          📧 <a href="mailto:datmast@gmail.com">datmast@gmail.com</a><br>
          🔗 <a href="https://github.com/QCivilTools">github.com/QCivilTools</a>
        </div>

        <h3>📦 Related Plugins</h3>
        <div class="step">
          <a href="https://github.com/QCivilTools/QCivilTools-HEC-RAS-manager">QCT HEC-RAS Manager</a>
            — HEC-RAS 2D workflow inside QGIS<br>
          <b>QCT Watershed Delineation</b> — this plugin<br>
          QCT 3D Civil Tool — surfaces, alignments, earthworks<br>
          QCT Coordinate Converter — NZGD49 → NZTM2000/NZVD2016<br>
          QCT PENZD Exporter — point layer → PENZD CSV
        </div>

        <h3>ℹ️ Version Info</h3>
        <div class="step">
          v1.0.0 — Initial release<br>
          QGIS ≥ 3.16 · Python ≥ 3.9 · WhiteboxTools ≥ 2.3
        </div>"""

    def _btn_style(self, colour):
        return ""  # use native Qt style

    # ══════════════════════════════════════════════════ Phase 1 tab ══
    def _build_phase1_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay   = QVBoxLayout(inner)
        lay.setSpacing(8)

        # Banner
        banner = QLabel(
            "PHASE 1 — Steps 1–5\n"
            "Fill DEM  →  D8 Direction  →  Flow Accumulation  →  Extract Streams  →  Vectorize\n"
            "After Phase 1, the stream network is added to your map so you can accurately\n"
            "place outlet points on the stream, then proceed to Phase 2.")
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "background:#e8f4fd;border:1px solid #2c5f8a;border-radius:4px;"
            "padding:8px;color:#1a3a5c;font-size:10px;")
        lay.addWidget(banner)

        # ── DEM input ──────────────────────────────────────────────────────
        dem_g = QGroupBox("DEM Raster  (must be in a projected CRS)")
        dg    = QVBoxLayout(dem_g)

        src_row = QHBoxLayout()
        self.dem_src_map  = QCheckBox("From map layers")
        self.dem_src_file = QCheckBox("From file")
        self.dem_src_map.setChecked(True)
        self.dem_src_map.toggled.connect(self._toggle_dem)
        self.dem_src_file.toggled.connect(lambda v: self.dem_src_map.setChecked(not v))
        src_row.addWidget(self.dem_src_map); src_row.addWidget(self.dem_src_file); src_row.addStretch()
        dg.addLayout(src_row)

        self.dem_combo = QgsMapLayerComboBox()
        self.dem_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.dem_combo.setShowCrs(True)
        dg.addWidget(self.dem_combo)

        self.dem_file_w = QWidget()
        dfr = QHBoxLayout(self.dem_file_w); dfr.setContentsMargins(0,0,0,0)
        self.dem_edit = QLineEdit(); self.dem_edit.setPlaceholderText("Path to DEM .tif …")
        db = QPushButton("Browse…")
        db.clicked.connect(lambda: self._browse_file(self.dem_edit, "Raster (*.tif *.tiff *.img *.asc)"))
        dfr.addWidget(self.dem_edit); dfr.addWidget(db)
        dg.addWidget(self.dem_file_w)
        self.dem_file_w.setVisible(False)
        lay.addWidget(dem_g)

        # ── Output directory ───────────────────────────────────────────────
        dir_g = QGroupBox("Output Directory")
        dr    = QHBoxLayout(dir_g)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText("Folder where all output files are saved…")
        dirbtn = QPushButton("Browse…"); dirbtn.clicked.connect(self._browse_output_dir)
        dr.addWidget(self.output_dir_edit); dr.addWidget(dirbtn)
        lay.addWidget(dir_g)

        # ── WBT executable ─────────────────────────────────────────────────
        wbt_g = QGroupBox("WhiteboxTools Executable")
        wg    = QVBoxLayout(wbt_g)
        wr    = QHBoxLayout()
        self.wbt_edit = QLineEdit()
        self.wbt_edit.setPlaceholderText("Leave blank to auto-detect, or browse to WBT folder…")
        wbtn  = QPushButton("Browse Folder…")
        wbtn.clicked.connect(self._browse_wbt_folder)
        wr.addWidget(self.wbt_edit); wr.addWidget(wbtn)
        wg.addLayout(wr)
        note = QLabel(
            "ℹ️  Select the folder containing <b>whitebox_tools.exe</b> — "
            "the plugin will find the executable automatically. "
            "Leave blank to auto-detect from PATH and common locations.")
        note.setOpenExternalLinks(True); note.setWordWrap(True)
        wg.addWidget(note)
        lay.addWidget(wbt_g)

        # ── Phase 1 Parameters ─────────────────────────────────────────────
        par_g = QGroupBox("⚙️  Phase 1 Parameters")
        pf    = QFormLayout(par_g)

        # Min catchment area
        self.use_area_check = QCheckBox("Use minimum catchment area (ha) instead of cell count")
        self.use_area_check.toggled.connect(self._toggle_area_mode)
        pf.addRow("Stream extraction:", self.use_area_check)

        self.min_area_spin = QDoubleSpinBox()
        self.min_area_spin.setRange(0.01, 999999); self.min_area_spin.setValue(50.0)
        self.min_area_spin.setDecimals(2); self.min_area_spin.setSuffix(" ha")
        self.min_area_spin.setEnabled(False)
        pf.addRow("Min catchment area:", self.min_area_spin)

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(1, 99999999); self.threshold_spin.setValue(10000)
        self.threshold_spin.setDecimals(0); self.threshold_spin.setSingleStep(1000)
        self.threshold_spin.setSuffix(" cells")
        pf.addRow("OR cell threshold:", self.threshold_spin)

        area_note = QLabel(
            "💡 Lower threshold = denser streams (more subbasins).   "
            "Example: 50 ha, 1 m DEM → 500,000 cells.")
        area_note.setStyleSheet("")
        pf.addRow("", area_note)

        # Fill depressions
        self.fix_flat_check = QCheckBox("Fix flat areas?")
        self.fix_flat_check.setChecked(True)
        pf.addRow("Fill depressions:", self.fix_flat_check)

        self.flat_spin = QDoubleSpinBox()
        self.flat_spin.setRange(0, 1); self.flat_spin.setValue(0.001)
        self.flat_spin.setDecimals(4); self.flat_spin.setSingleStep(0.001)
        pf.addRow("Flat increment (z units):", self.flat_spin)

        # D8 / Accum
        self.esri_check = QCheckBox("Use ESRI pointer scheme")
        pf.addRow("D8 Pointer:", self.esri_check)

        self.log_check = QCheckBox("Log-transform flow accumulation output?")
        pf.addRow("Flow Accumulation:", self.log_check)

        lay.addWidget(par_g)

        # ── Phase 1 Output Layers ──────────────────────────────────────────
        lay.addWidget(self._build_outputs_group("Phase 1 Outputs — Add to map after run",
                                                self.PHASE1_OUTPUTS, key_set=None))

        lay.addStretch()
        scroll.setWidget(inner)
        return scroll

    def _toggle_dem(self, map_mode):
        self.dem_src_file.setChecked(not map_mode)
        self.dem_combo.setVisible(map_mode)
        self.dem_file_w.setVisible(not map_mode)

    def _toggle_area_mode(self, use_area):
        self.min_area_spin.setEnabled(use_area)
        self.threshold_spin.setEnabled(not use_area)

    # ══════════════════════════════════════════════════ Phase 2 tab ══
    def _build_phase2_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay   = QVBoxLayout(inner)
        lay.setSpacing(8)

        # Banner
        self.p2_banner = QLabel(
            "⏳  Run Phase 1 first. After the stream network appears on the map,\n"
            "    place outlet point(s) on the stream, then come back here to run Phase 2.")
        self.p2_banner.setWordWrap(True)
        
        lay.addWidget(self.p2_banner)

        # ── No-outlet option ───────────────────────────────────────────────
        mode_g = QGroupBox("Delineation Mode")
        mg     = QVBoxLayout(mode_g)

        self.no_outlet_check = QCheckBox(
            "No outlet point — generate all subbasins across the entire DEM")
        self.no_outlet_check.setStyleSheet("")
        self.no_outlet_check.toggled.connect(self._on_no_outlet_toggled)
        mg.addWidget(self.no_outlet_check)

        mode_note = QLabel(
            "☑  No outlet: skips steps 6–10 (snap/watershed/unnest/LFP).\n"
            "       Produces WBT_AllDEM_Subbasins.shp for the entire DEM extent.\n\n"
            "☐  With outlet: delineates watershed to your pour point(s) first,\n"
            "       then creates WBT_Subbasins_Info.shp masked to the watershed\n"
            "       AND WBT_AllDEM_Subbasins.shp for the full DEM.")
        mode_note.setStyleSheet("")
        mg.addWidget(mode_note)
        lay.addWidget(mode_g)

        # ── Outlet input (hidden when no_outlet_check is ticked) ──────────
        self.outlet_section = QWidget()
        ol = QVBoxLayout(self.outlet_section); ol.setContentsMargins(0,0,0,0); ol.setSpacing(6)

        out_g = QGroupBox("Outlet Point Layer  (place points on the stream network)")
        og    = QVBoxLayout(out_g)

        # ── Map-click picking row ──────────────────────────────────────────
        pick_row = QHBoxLayout()
        self.pick_btn = QPushButton("📍 Pick Outlets on Map")
        self.pick_btn.setToolTip("Click on the stream network in the QGIS canvas to add outlet points")
        self.pick_btn.clicked.connect(self._start_outlet_picking)
        self.done_pick_btn = QPushButton("✅ Done Picking")
        self.done_pick_btn.setEnabled(False)
        self.done_pick_btn.clicked.connect(self._stop_outlet_picking)
        self.clear_outlets_btn = QPushButton("🗑 Clear Points")
        self.clear_outlets_btn.clicked.connect(self._clear_outlet_points)
        pick_row.addWidget(self.pick_btn)
        pick_row.addWidget(self.done_pick_btn)
        pick_row.addWidget(self.clear_outlets_btn)
        pick_row.addStretch()
        og.addLayout(pick_row)

        self.outlet_point_label = QLabel("No outlet points picked yet.")
        self.outlet_point_label.setStyleSheet("color:#666;font-size:9px;")
        og.addWidget(self.outlet_point_label)

        pick_note = QLabel(
            "💡 Click <b>Pick Outlets on Map</b>, then click directly on the stream network.\n"
            "   Multiple clicks = multiple outlets. Click <b>Done Picking</b> when finished.\n"
            "   OR select an existing point layer below.")
        pick_note.setWordWrap(True)
        pick_note.setStyleSheet("font-size:9px;color:#444;background:#f0f4f8;"
                                "border:1px solid #ccd;border-radius:3px;padding:5px;")
        og.addWidget(pick_note)

        osrc_row = QHBoxLayout()
        self.out_src_map  = QCheckBox("From map layers")
        self.out_src_file = QCheckBox("From file")
        self.out_src_map.setChecked(True)
        self.out_src_map.toggled.connect(self._toggle_outlet)
        self.out_src_file.toggled.connect(lambda v: self.out_src_map.setChecked(not v))
        osrc_row.addWidget(self.out_src_map); osrc_row.addWidget(self.out_src_file); osrc_row.addStretch()
        og.addLayout(osrc_row)

        self.outlet_combo = QgsMapLayerComboBox()
        self.outlet_combo.setFilters(QgsMapLayerProxyModel.PointLayer)
        self.outlet_combo.setShowCrs(True)
        og.addWidget(self.outlet_combo)

        self.outlet_file_w = QWidget()
        ofr = QHBoxLayout(self.outlet_file_w); ofr.setContentsMargins(0,0,0,0)
        self.outlet_edit = QLineEdit(); self.outlet_edit.setPlaceholderText("Path to outlet .shp …")
        ob = QPushButton("Browse…")
        ob.clicked.connect(lambda: self._browse_file(self.outlet_edit, "Shapefiles (*.shp)"))
        ofr.addWidget(self.outlet_edit); ofr.addWidget(ob)
        og.addWidget(self.outlet_file_w)
        self.outlet_file_w.setVisible(False)
        ol.addWidget(out_g)

        # ── Snap distance ──────────────────────────────────────────────────
        snap_g = QGroupBox("Outlet Snapping  (Step 6)")
        sf     = QFormLayout(snap_g)
        self.snap_spin = QDoubleSpinBox()
        self.snap_spin.setRange(1, 10000); self.snap_spin.setValue(50)
        self.snap_spin.setDecimals(1); self.snap_spin.setSuffix(" map units")
        sf.addRow("Max snap distance:", self.snap_spin)
        snap_note = QLabel("💡 Must be larger than DEM cell size. Moves outlet onto nearest stream cell.")
        snap_note.setStyleSheet("")
        sf.addRow("", snap_note)
        ol.addWidget(snap_g)

        # ── Optional steps ─────────────────────────────────────────────────
        opt_g = QGroupBox("Optional Steps")
        of    = QFormLayout(opt_g)
        self.run_unnest_check = QCheckBox("Run UnnestBasins (complete watershed per outlet)")
        self.run_unnest_check.setChecked(True)
        of.addRow("", self.run_unnest_check)
        self.run_lfp_check = QCheckBox("Run LongestFlowPath for whole watershed")
        self.run_lfp_check.setChecked(True)
        of.addRow("", self.run_lfp_check)
        ol.addWidget(opt_g)

        lay.addWidget(self.outlet_section)

        # ── Phase 2 Parameters (ESRI pointer — shared with phase1) ────────
        p2par_g = QGroupBox("⚙️  Phase 2 Parameters")
        p2f     = QFormLayout(p2par_g)
        self.p2_esri_note = QLabel(
            "D8 pointer scheme is shared with Phase 1 setting above.\n"
            "Re-run Phase 1 first if you change the scheme.")
        self.p2_esri_note.setStyleSheet("")
        p2f.addRow("", self.p2_esri_note)
        lay.addWidget(p2par_g)

        # ── Phase 2 Output Layers ──────────────────────────────────────────
        KEY = {"WBT_ExtractStreams_vector.shp", "outlet_snapped.shp",
               "WBT_Watershed_Boundary.shp", "WBT_LongestFlowPath.shp",
               "WBT_Subbasins_Info.shp", "WBT_AllDEM_Subbasins.shp"}
        self.p2_outputs_group = self._build_outputs_group(
            "Phase 2 Outputs — Add to map after run",
            self.PHASE2_OUTPUTS, key_set=KEY)
        lay.addWidget(self.p2_outputs_group)

        # ── Status ─────────────────────────────────────────────────────────
        self.p2_status = QLabel("Phase 1 not yet run.")
        self.p2_status.setStyleSheet("")
        lay.addWidget(self.p2_status)

        lay.addStretch()
        scroll.setWidget(inner)
        return scroll

    def _on_no_outlet_toggled(self, checked):
        """Show/hide outlet section and update Phase 2 output checkboxes."""
        self.outlet_section.setVisible(not checked)
        # When no-outlet, grey out outlet-only outputs
        OUTLET_ONLY = {"outlet_snapped.shp", "WBT_Watershed.tif",
                       "WBT_Watershed_Boundary.shp", "WBT_UnnestBasins.tif",
                       "WBT_LongestFlowPath.shp", "WBT_Subbasins.tif",
                       "WBT_Subbasins_Info.shp"}
        for fname, chk in self._out_checks.items():
            if fname in OUTLET_ONLY:
                chk.setEnabled(not checked)
                if checked:
                    chk.setChecked(False)
        # Update button label
        if checked:
            self.p2_btn.setText("▶  Run Phase 2  (Full-DEM Subbasins, no outlet)")
        else:
            self.p2_btn.setText("▶  Run Phase 2  (→ Watershed / Subbasins)")

    def _toggle_outlet(self, map_mode):
        self.out_src_file.setChecked(not map_mode)
        self.outlet_combo.setVisible(map_mode)
        self.outlet_file_w.setVisible(not map_mode)

    # ══════════════════════════════════════════════ Map-click outlet ══
    def _start_outlet_picking(self):
        """Activate QgsMapToolEmitPoint so user can click outlets on canvas."""
        canvas = self.iface.mapCanvas()
        self._prev_map_tool = canvas.mapTool()
        self._map_tool = QgsMapToolEmitPoint(canvas)
        self._map_tool.canvasClicked.connect(self._on_canvas_clicked)
        canvas.setMapTool(self._map_tool)
        self.pick_btn.setEnabled(False)
        self.done_pick_btn.setEnabled(True)
        self.outlet_point_label.setText(
            "🖱️  Click on the stream network to add outlet points… (click Done Picking when finished)")
        self.outlet_point_label.setStyleSheet("color:#1a5276;font-size:9px;font-weight:bold;")
        # Minimise dialog so canvas is accessible
        self.showMinimized()

    def _on_canvas_clicked(self, point, button):
        """Receive a canvas click, confirm save, and ask to add more or finish."""
        from qgis.PyQt.QtCore import Qt
        from qgis.PyQt.QtWidgets import QPushButton as _QPB
        if button != Qt.LeftButton:
            return

        # Temporarily disconnect to avoid double-fire during dialog
        self._map_tool.canvasClicked.disconnect(self._on_canvas_clicked)

        n_before = len(self._outlet_points)
        msg = QMessageBox(self)
        msg.setWindowTitle("Outlet Point")
        msg.setText(
            f"Point at:\n  X: {point.x():.3f}\n  Y: {point.y():.3f}\n\n"
            f"Points saved so far: {n_before}")
        msg.setInformativeText(
            "Yes  — save this point and finish picking\n"
            "Add  — save this point and pick another\n"
            "No   — discard this point and pick again")
        btn_yes = msg.addButton("Yes — Save && Finish", QMessageBox.AcceptRole)
        btn_add = msg.addButton("Add — Save && Continue", QMessageBox.ActionRole)
        btn_no  = msg.addButton("No — Discard && Continue", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_add)
        msg.exec_()
        clicked = msg.clickedButton()

        if clicked == btn_yes:
            # Save point and finish
            self._outlet_points.append(point)
            self._update_outlet_scratch_layer()
            self._stop_outlet_picking()

        elif clicked == btn_add:
            # Save point and keep picking
            self._outlet_points.append(point)
            n = len(self._outlet_points)
            self.outlet_point_label.setText(
                f"📍 {n} point{'s' if n != 1 else ''} saved — click map to add more")
            self.outlet_point_label.setStyleSheet("color:#1e8449;font-size:9px;font-weight:bold;")
            self._update_outlet_scratch_layer()
            self._map_tool.canvasClicked.connect(self._on_canvas_clicked)

        else:  # No — discard and keep picking
            n = len(self._outlet_points)
            self.outlet_point_label.setText(
                f"Point discarded — click again to pick ({n} saved so far)")
            self.outlet_point_label.setStyleSheet("color:#b7770d;font-size:9px;font-weight:bold;")
            self._map_tool.canvasClicked.connect(self._on_canvas_clicked)

    def _update_outlet_scratch_layer(self):
        """Keep a visible scratch layer on the map showing picked outlets."""
        from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY
        # Remove old scratch layer
        if self._outlet_layer and self._outlet_layer.id():
            QgsProject.instance().removeMapLayer(self._outlet_layer.id())
            self._outlet_layer = None

        crs_str = self._dem_crs.authid() if self._dem_crs and self._dem_crs.isValid() else \
                  self.iface.mapCanvas().mapSettings().destinationCrs().authid()

        lyr = QgsVectorLayer(f"Point?crs={crs_str}", "Outlet Points (picked)", "memory")
        pr  = lyr.dataProvider()
        pr.addAttributes([QgsField("id", QVariant.Int)])
        lyr.updateFields()
        feats = []
        for i, pt in enumerate(self._outlet_points, 1):
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPointXY(pt))
            f.setAttributes([i])
            feats.append(f)
        pr.addFeatures(feats)
        lyr.updateExtents()
        QgsProject.instance().addMapLayer(lyr)
        self._outlet_layer = lyr
        # Track it so it gets unloaded with other layers
        if lyr.id() not in self._loaded_layer_ids:
            self._loaded_layer_ids.append(lyr.id())

    def _stop_outlet_picking(self):
        """Deactivate map tool and restore previous tool."""
        # Safely disconnect signal — may already be disconnected if called from Cancel
        try:
            self._map_tool.canvasClicked.disconnect(self._on_canvas_clicked)
        except Exception:
            pass
        canvas = self.iface.mapCanvas()
        if self._prev_map_tool:
            canvas.setMapTool(self._prev_map_tool)
        else:
            canvas.unsetMapTool(self._map_tool)
        self._map_tool = None
        self.pick_btn.setEnabled(True)
        self.done_pick_btn.setEnabled(False)
        n = len(self._outlet_points)
        if n == 0:
            self.outlet_point_label.setText("No outlet points picked.")
            self.outlet_point_label.setStyleSheet("color:#666;font-size:9px;")
        else:
            self.outlet_point_label.setText(
                f"✅ {n} outlet point{'s' if n != 1 else ''} ready for Phase 2.")
            self.outlet_point_label.setStyleSheet("color:#1e8449;font-size:9px;font-weight:bold;")
        self.showNormal()
        self.raise_()

    def _clear_outlet_points(self):
        self._outlet_points.clear()
        if self._outlet_layer and self._outlet_layer.id():
            QgsProject.instance().removeMapLayer(self._outlet_layer.id())
            self._outlet_layer = None
        self.outlet_point_label.setText("Outlet points cleared.")
        self.outlet_point_label.setStyleSheet("color:#666;font-size:9px;")

    def _save_picked_outlets_to_shp(self, output_dir):
        """Write picked outlet points to a shapefile; return path or ''."""
        if not self._outlet_points:
            return ""
        crs_str = self._dem_crs.authid() if self._dem_crs and self._dem_crs.isValid() else \
                  self.iface.mapCanvas().mapSettings().destinationCrs().authid()
        lyr = QgsVectorLayer(f"Point?crs={crs_str}", "outlet_picked", "memory")
        pr  = lyr.dataProvider()
        pr.addAttributes([QgsField("id", QVariant.Int)])
        lyr.updateFields()
        feats = []
        for i, pt in enumerate(self._outlet_points, 1):
            f = QgsFeature()
            f.setGeometry(QgsGeometry.fromPointXY(pt))
            f.setAttributes([i])
            feats.append(f)
        pr.addFeatures(feats)
        out_path = os.path.join(output_dir, "outlet_picked.shp")
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "ESRI Shapefile"
        opts.fileEncoding = "UTF-8"
        err, _, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
            lyr, out_path, QgsCoordinateTransformContext(), opts)
        if err == QgsVectorFileWriter.NoError and os.path.exists(out_path):
            return out_path
        # Fallback for older QGIS
        try:
            err2 = QgsVectorFileWriter.writeAsVectorFormat(
                lyr, out_path, "UTF-8", lyr.crs(), "ESRI Shapefile")
            if err2 == QgsVectorFileWriter.NoError and os.path.exists(out_path):
                return out_path
        except Exception:
            pass
        return ""

    # ══════════════════════════════════════════════ CRS helper ══
    def _get_dem_crs(self):
        """Return QgsCoordinateReferenceSystem of the currently selected DEM."""
        try:
            if self.dem_src_map.isChecked():
                lyr = self.dem_combo.currentLayer()
                if lyr:
                    return lyr.crs()
            else:
                path = self.dem_edit.text().strip()
                if path and os.path.exists(path):
                    lyr = QgsRasterLayer(path, "tmp_dem_crs_check")
                    if lyr.isValid():
                        return lyr.crs()
        except Exception:
            pass
        return None

    # ══════════════════════════════════════════════ Outputs group ══
    def _build_outputs_group(self, title, outputs, key_set=None):
        """Build a QGroupBox with output layer checkboxes + quick-select buttons."""
        g    = QGroupBox(title)
        vlay = QVBoxLayout(g)

        # Quick-select row
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Quick select:"))
        for lbl, state in [("✅ All", True), ("☐ None", False), ("⭐ Key", None)]:
            btn = QPushButton(lbl)
            btn.setStyleSheet("font-size:9px;padding:2px 8px;")
            fnames = [f for f,_,_,_ in outputs]
            _state = state
            _key   = key_set
            btn.clicked.connect(lambda _, s=_state, fn=fnames, k=_key:
                                 self._quick_select_group(s, fn, k))
            sel_row.addWidget(btn)
        sel_row.addStretch()
        vlay.addLayout(sel_row)

        # Checkboxes grid
        grid = QGridLayout(); grid.setSpacing(4)
        r, c = 0, 0
        for fname, display, ftype, default in outputs:
            icon  = "🟦" if ftype == "raster" else "🟩"
            star  = " ⭐" if "★" in display else ""
            label = display.replace("★ ", "")
            chk   = QCheckBox(f"{icon}  {label}{star}")
            chk.setChecked(default)
            chk.setToolTip(fname)
            chk.setStyleSheet("font-size:10px;")
            self._out_checks[fname] = chk
            grid.addWidget(chk, r, c)
            c += 1
            if c > 1: c = 0; r += 1
        vlay.addLayout(grid)

        legend = QLabel("🟦 Raster    🟩 Vector    ⭐ Recommended key output")
        legend.setStyleSheet("")
        vlay.addWidget(legend)
        return g

    def _quick_select_group(self, state, fnames, key_set):
        for fname in fnames:
            chk = self._out_checks.get(fname)
            if chk and chk.isEnabled():
                if state is None:
                    chk.setChecked(key_set is not None and fname in key_set)
                else:
                    chk.setChecked(bool(state))

    # ══════════════════════════════════════════════════ Steps tab ══
    def _build_steps_tab(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        tb  = QTextBrowser()
        tb.setHtml(self._steps_html())
        tb.setOpenExternalLinks(True)
        lay.addWidget(tb)
        return w

    def _steps_html(self):
        return """
        <style>
          body{font-family:Arial,sans-serif;font-size:11px;}
          h3{color:#2c5f8a;margin:6px 0 2px;}
          .ph{background:#2c5f8a;color:white;padding:4px 10px;
              border-radius:4px;font-weight:bold;margin:8px 0 4px;}
          .s{background:#f0f4f8;border-left:4px solid #2c5f8a;
             margin:3px 0;padding:6px 10px;border-radius:0 4px 4px 0;}
          .n{background:#edfaed;border-left:4px solid #27ae60;
             margin:3px 0;padding:6px 10px;border-radius:0 4px 4px 0;}
          .skip{background:#fef9e7;border-left:4px solid #f0c040;
                margin:3px 0;padding:6px 10px;border-radius:0 4px 4px 0;}
          .num{color:#2c5f8a;font-weight:bold;}
          .nnum{color:#27ae60;font-weight:bold;}
          .snum{color:#b8860b;font-weight:bold;}
          .t{color:#c0392b;font-weight:bold;font-family:monospace;}
          .io{color:#666;font-size:10px;}
          .f{color:#27ae60;font-family:monospace;font-size:10px;line-height:1.7;}
          .auth{color:#999;font-size:10px;}
        </style>
        <h3>QCivilTools – Watershed Delineation (WhiteboxTools) — Two-Phase Workflow</h3>
        <p class="auth">Author: Dat Vu | https://github.com/datmast-cmd | v1.0.0</p>

        <div class="ph">⚡ PHASE 1 — DEM Preprocessing &amp; Stream Network (Steps 1–5)</div>
        <div class="s"><span class="num">Step 1</span> — Fill Depressions<br>
          <span class="t">FillDepressionsWangAndLiu</span> → WBT_Filled_DEM.tif</div>
        <div class="s"><span class="num">Step 2</span> — D8 Flow Direction<br>
          <span class="t">D8Pointer</span> → WBT_D8_Pointer.tif</div>
        <div class="s"><span class="num">Step 3</span> — D8 Flow Accumulation<br>
          <span class="t">D8FlowAccumulation</span> → WBT_D8_FlowAccumu.tif</div>
        <div class="s"><span class="num">Step 4 ★</span> — Extract Streams<br>
          <span class="t">ExtractStreams</span> → WBT_ExtractStreams.tif<br>
          <span class="io">Threshold from min catchment area (ha) or cell count</span></div>
        <div class="s"><span class="num">Step 5</span> — Vectorize Stream Network<br>
          <span class="t">RasterStreamsToVector</span> → WBT_ExtractStreams_vector.shp<br>
          <span class="io">→ Stream network loaded to map. <b>Place outlet points on the stream then run Phase 2.</b></span></div>

        <div class="ph">🎯 PHASE 2 — WITH OUTLET (Steps 6–13)</div>
        <div class="s"><span class="num">Step 6</span> — Snap Pour Points<br>
          <span class="t">JensonSnapPourPoints</span> → outlet_snapped.shp</div>
        <div class="s"><span class="num">Step 7</span> — Delineate Watershed<br>
          <span class="t">Watershed</span> → WBT_Watershed.tif</div>
        <div class="s"><span class="num">Step 8</span> — Vectorize Watershed<br>
          <span class="t">RasterToVectorPolygons</span> → WBT_Watershed_Boundary.shp</div>
        <div class="s"><span class="num">Step 9</span> — Unnested Basins (optional)<br>
          <span class="t">UnnestBasins</span> → WBT_UnnestBasins_1.tif…</div>
        <div class="s"><span class="num">Step 10</span> — Longest Flow Path — whole watershed (optional)<br>
          <span class="t">LongestFlowpath</span> → WBT_LongestFlowPath.shp</div>
        <div class="n"><span class="nnum">Step 11 ★</span> — Subbasins inside Watershed<br>
          <span class="t">Subbasins + mask</span> → WBT_Subbasins.tif</div>
        <div class="n"><span class="nnum">Step 12 ★</span> — Subbasin Info Shapefile (watershed-masked)<br>
          → <b>WBT_Subbasins_Info.shp</b></div>
        <div class="n"><span class="nnum">Step 13 ★</span> — All-DEM Subbasins<br>
          → <b>WBT_AllDEM_Subbasins.shp</b></div>

        <div class="ph">🗺️  PHASE 2 — NO OUTLET (Steps 11 &amp; 13 only)</div>
        <div class="skip"><span class="snum">Steps 6–10</span> — Skipped (no pour point)</div>
        <div class="n"><span class="nnum">Step 11 ★</span> — Subbasins (full DEM)<br>
          <span class="t">Subbasins</span> → used internally</div>
        <div class="skip"><span class="snum">Step 12</span> — Skipped (no watershed boundary)</div>
        <div class="n"><span class="nnum">Step 13 ★</span> — All-DEM Subbasins<br>
          → <b>WBT_AllDEM_Subbasins.shp</b><br>
          <span class="io">All subbasins across the full DEM with AREA, LFP_LEN, slope attributes.</span></div>
        """

    # ════════════════════════════════════════════════════ Log tab ══
    def _build_log_tab(self):
        w   = QWidget()
        lay = QVBoxLayout(w)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(
            "QTextEdit{background:#1e1e1e;color:#d4d4d4;"
            "font-family:monospace;font-size:10px;}")
        lay.addWidget(self.log_text)
        clr = QPushButton("Clear Log")
        clr.clicked.connect(self.log_text.clear)
        lay.addWidget(clr)
        return w

    # ════════════════════════════════════════════════════ Helpers ══
    def _browse_file(self, edit, filt):
        p, _ = QFileDialog.getOpenFileName(self, "Select File", "", filt)
        if p: edit.setText(p)

    def _browse_wbt_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select WhiteboxTools Folder")
        if folder:
            self.wbt_edit.setText(folder)

    def _browse_output_dir(self):
        p = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if p: self.output_dir_edit.setText(p)

    def _clean_source_path(self, source: str) -> str:
        if not source: return ""
        for sep in ("|", "?"):
            if sep in source:
                source = source.split(sep)[0]
        return source.strip()

    def _get_dem_path(self):
        if self.dem_src_map.isChecked():
            lyr = self.dem_combo.currentLayer()
            return self._clean_source_path(lyr.source()) if lyr else ""
        return self.dem_edit.text().strip()

    def _get_outlet_path(self):
        if self.out_src_map.isChecked():
            lyr = self.outlet_combo.currentLayer()
            if not lyr: return ""
            src = self._clean_source_path(lyr.source())
            if not src or not os.path.exists(src):
                return self._save_layer_to_temp(lyr)
            return src
        return self.outlet_edit.text().strip()

    def _save_layer_to_temp(self, lyr):
        import tempfile
        out_dir = self._get_output_dir() or self._output_dir
        if not out_dir:
            QMessageBox.warning(self, "Output Dir Required",
                "Please set an output directory before selecting a memory layer as outlet.")
            return ""
        tmp_path = os.path.join(out_dir, "_tmp_outlet_input.shp")
        try:
            from qgis.core import QgsVectorFileWriter, QgsCoordinateTransformContext
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "ESRI Shapefile"; opts.fileEncoding = "UTF-8"
            err, msg, _, _ = QgsVectorFileWriter.writeAsVectorFormatV3(
                lyr, tmp_path, QgsCoordinateTransformContext(), opts)
            if err == QgsVectorFileWriter.NoError and os.path.exists(tmp_path):
                return tmp_path
            err2 = QgsVectorFileWriter.writeAsVectorFormat(
                lyr, tmp_path, "UTF-8", lyr.crs(), "ESRI Shapefile")
            if err2 == QgsVectorFileWriter.NoError and os.path.exists(tmp_path):
                return tmp_path
        except Exception as e:
            self._log(f"Could not save scratch layer: {e}", "WARNING")
        QMessageBox.warning(self, "Layer Save Failed",
            "Could not save the scratch/memory outlet layer to disk.\n"
            "Please save your outlet layer as a shapefile first.")
        return ""

    def _get_output_dir(self):
        return self.output_dir_edit.text().strip()

    def _get_wbt_path(self):
        """Return path to whitebox_tools exe. If user gave a folder, find exe inside it."""
        import platform
        val = self.wbt_edit.text().strip()
        if not val:
            return None
        # If it's already pointing to the exe file, return as-is
        if os.path.isfile(val):
            return val
        # Treat as folder — look for the exe inside
        if os.path.isdir(val):
            exe_name = "whitebox_tools.exe" if platform.system() == "Windows" else "whitebox_tools"
            # Check folder itself, then one level deep (e.g. WBT/WBT/whitebox_tools.exe)
            for candidate in [
                os.path.join(val, exe_name),
                os.path.join(val, "WBT", exe_name),
            ]:
                if os.path.isfile(candidate):
                    return candidate
            self.append_log(
                f"WBT folder selected but '{exe_name}' not found inside '{val}'. "
                "Check the folder contains the WhiteboxTools binary.", "WARNING")
        return val  # pass through and let processor handle / error

    def _set_busy(self, busy):
        self.p1_btn.setEnabled(not busy)
        self.p2_btn.setEnabled(not busy and self._phase1_done)
        self.cancel_btn.setEnabled(busy)
        self.load_btn.setEnabled(not busy)

    # ═══════════════════════════════════════════════════ Actions ══
    def on_run_phase1(self):
        dem_path   = self._get_dem_path()
        output_dir = self._get_output_dir()

        if not dem_path or not os.path.exists(dem_path):
            QMessageBox.warning(self, "Input Error", "Please select a valid DEM raster.")
            return
        if not output_dir:
            QMessageBox.warning(self, "Input Error", "Please select an output directory.")
            return

        # Unload all layers from previous run before re-running
        self._unload_tracked_layers()

        os.makedirs(output_dir, exist_ok=True)
        self._output_dir = output_dir
        self._dem_crs    = self._get_dem_crs()   # capture CRS for all output loading

        params = {
            "dem_path":              dem_path,
            "output_dir":            output_dir,
            "wbt_path":              self._get_wbt_path(),
            "fix_flat":              self.fix_flat_check.isChecked(),
            "flat_increment":        self.flat_spin.value(),
            "esri_pointer":          self.esri_check.isChecked(),
            "log_transform":         self.log_check.isChecked(),
            "channel_threshold":     self.threshold_spin.value(),
            "min_catchment_area_ha": self.min_area_spin.value()
                                     if self.use_area_check.isChecked() else 0,
        }

        self.log_text.clear()
        self.progress_bar.setValue(0)
        self._set_busy(True)
        self.status_label.setText("Phase 1 running…")

        proc = self.processor
        proc.log_callback = None; proc.progress_callback = None

        self.worker = WorkerThread(proc.run_phase1, params)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.finished_signal.connect(self.on_phase1_finished)
        proc.log_callback      = lambda msg, lvl="INFO": self.worker.log_signal.emit(msg, lvl)
        proc.progress_callback = lambda pct: self.worker.progress_signal.emit(pct)
        self.worker.start()

    def on_phase1_finished(self, success, message, extra):
        self._set_busy(False)
        if success:
            self._phase1_done  = True
            self._streams_path = extra
            self.p2_btn.setEnabled(True)
            self.load_btn.setEnabled(True)
            self.progress_bar.setValue(38)
            self.status_label.setText("✅ Phase 1 done — click 'Load Selected Outputs' to view streams, then run Phase 2.")
            self.p2_banner.setText(
                "✅  Phase 1 complete! Outputs saved to disk.\n"
                "    Click '🗺 Load Selected Outputs' to add the stream network to the map.\n"
                "    Then:\n"
                "    • Use '📍 Pick Outlets on Map' to click outlet points on the stream, OR\n"
                "    • Select an existing outlet layer\n"
                "    • Tick 'No outlet point' to run full-DEM subbasins directly.")
            self.p2_status.setText("Phase 1 ✅ complete.")
            QMessageBox.information(self, "Phase 1 Complete",
                "Phase 1 outputs saved.\n\n"
                "Next steps:\n"
                "  1. Tick outputs to load, click '🗺 Load Selected Outputs'\n"
                "  2. Inspect the stream network on the map\n"
                "  3. In Phase 2 tab: pick outlet points or select existing layer\n"
                "  4. Click Run Phase 2")
        else:
            self.status_label.setText("❌ Phase 1 error.")
            QMessageBox.critical(self, "Phase 1 Error", message)

    def on_run_phase2(self):
        if not self._phase1_done:
            QMessageBox.warning(self, "Phase 1 Required",
                "Please run Phase 1 first to generate the stream network.")
            return

        # Unload Phase 2 layers from any previous run
        self._unload_tracked_layers(phase2_only=True)

        no_outlet  = self.no_outlet_check.isChecked()
        output_dir = self._get_output_dir() or self._output_dir

        if not no_outlet:
            # Priority: picked map points > layer/file selection
            if self._outlet_points:
                outlet_path = self._save_picked_outlets_to_shp(output_dir)
                if not outlet_path:
                    QMessageBox.warning(self, "Outlet Save Failed",
                        "Could not save picked outlet points to shapefile.\n"
                        "Please check the output directory is writable.")
                    return
            else:
                outlet_path = self._get_outlet_path()
                if not outlet_path or not os.path.exists(outlet_path):
                    QMessageBox.warning(self, "Input Error",
                        "No outlet points found.\n\n"
                        "Either:\n"
                        "  • Use '📍 Pick Outlets on Map' to click on the stream, OR\n"
                        "  • Select an existing outlet point layer,\n"
                        "  • OR tick 'No outlet point' to generate all subbasins.")
                    return
                if os.path.basename(outlet_path).lower().startswith("outlet_snapped"):
                    reply = QMessageBox.question(
                        self, "Check Outlet Layer",
                        "The selected outlet layer appears to be 'outlet_snapped' — "
                        "the output from a previous run.\n\n"
                        "WBT needs your ORIGINAL outlet points (before snapping).\n\n"
                        "Continue anyway?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if reply == QMessageBox.No:
                        return
        else:
            outlet_path = ""

        params = {
            "outlet_path":      outlet_path,
            "output_dir":       output_dir,
            "wbt_path":         self._get_wbt_path(),
            "esri_pointer":     self.esri_check.isChecked(),
            "snap_distance":    self.snap_spin.value(),
            "run_unnest":       self.run_unnest_check.isChecked(),
            "run_longest_flow": self.run_lfp_check.isChecked(),
            "no_outlet":        no_outlet,
        }

        mode_lbl = "Full-DEM subbasins (no outlet)" if no_outlet else "outlet → watershed → subbasins"
        self._set_busy(True)
        self.status_label.setText(f"Phase 2 running — {mode_lbl}…")

        proc = self.processor
        proc.log_callback = None; proc.progress_callback = None

        self.worker = WorkerThread(proc.run_phase2_wbt, params)
        self.worker.log_signal.connect(self.append_log)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.finished_signal.connect(self._on_phase2_wbt_done)
        proc.log_callback      = lambda msg, lvl="INFO": self.worker.log_signal.emit(msg, lvl)
        proc.progress_callback = lambda pct: self.worker.progress_signal.emit(pct)
        self.worker.start()

    def _on_phase2_wbt_done(self, success, message, ctx):
        if not success:
            self._set_busy(False)
            self.status_label.setText("❌ Phase 2 WBT error.")
            QMessageBox.critical(self, "Phase 2 Error", message)
            return
        self.status_label.setText("Phase 2: building subbasin shapefiles (steps 12–13)…")
        self.progress_bar.setValue(85)
        self._phase2_ctx = ctx
        QTimer.singleShot(0, self._run_phase2_qgis_deferred)

    def _run_phase2_qgis_deferred(self):
        ctx  = getattr(self, "_phase2_ctx", None)
        proc = self.processor
        proc.log_callback      = self.append_log
        proc.progress_callback = self.progress_bar.setValue
        try:
            ok, msg = proc.run_phase2_qgis(ctx)
        except Exception as e:
            import traceback
            ok  = False
            msg = traceback.format_exc()
            self.append_log(msg, "ERROR")
        self._set_busy(False)
        if ok:
            self.progress_bar.setValue(100)
            self.status_label.setText("✅ Phase 2 complete!")
            self.load_btn.setEnabled(True)
            QMessageBox.information(self, "Complete", msg)
        else:
            self.status_label.setText("❌ Phase 2 QGIS error.")
            QMessageBox.critical(self, "Phase 2 Error", msg)

    def on_cancel(self):
        if self.worker and self.worker.isRunning():
            self.processor.cancel_requested = True
            self.worker.wait(3000)
        self._set_busy(False)
        self.status_label.setText("Cancelled.")

    def _style_stream_layer(self, lyr):
        try:
            sym = QgsLineSymbol.createSimple({"color": "0,114,189", "width": "0.5"})
            lyr.renderer().setSymbol(sym)
        except Exception: pass

    def closeEvent(self, event):
        """Clean up map tool, ask to unload layers, reset all state."""
        # Restore map tool
        if self._map_tool:
            canvas = self.iface.mapCanvas()
            if self._prev_map_tool:
                canvas.setMapTool(self._prev_map_tool)
            else:
                canvas.unsetMapTool(self._map_tool)
            self._map_tool = None

        # Ask about loaded layers
        n_loaded = len([lid for lid in self._loaded_layer_ids
                        if QgsProject.instance().mapLayer(lid)])
        if n_loaded > 0:
            reply = QMessageBox.question(
                self, "Unload Layers?",
                f"QCT Watershed has {n_loaded} layer(s) loaded on the map.\n\n"
                "Remove them from the QGIS project?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._unload_tracked_layers()

        # Reset all state
        self._phase1_done   = False
        self._output_dir    = ""
        self._streams_path  = None
        self._dem_crs       = None
        self._outlet_points.clear()
        self._loaded_layer_ids.clear()
        self.log_text.clear()
        self.progress_bar.setValue(0)
        self.status_label.setText("Ready — configure Phase 1 inputs and click Run Phase 1.")
        self.p2_btn.setEnabled(False)
        self.load_btn.setEnabled(False)

        super().closeEvent(event)

    # ══════════════════════════════════════════ Layer tracking ══
    def _unload_tracked_layers(self, phase2_only=False):
        """Remove plugin-loaded layers from QGIS. If phase2_only, keep Phase 1 layers."""
        if not self._loaded_layer_ids:
            return
        if phase2_only:
            p2_fnames = {f for f, _, _, _ in self.PHASE2_OUTPUTS}
            p2_fnames.add("outlet_picked.shp")
            keep = []
            for lid in self._loaded_layer_ids:
                lyr = QgsProject.instance().mapLayer(lid)
                if lyr is None:
                    continue  # already gone
                name = lyr.name()
                # Unload if it matches a Phase 2 output name
                is_p2 = any(fname.replace(".shp", "").replace(".tif", "").lower() in name.lower()
                            for fname in p2_fnames)
                if is_p2:
                    QgsProject.instance().removeMapLayer(lid)
                else:
                    keep.append(lid)
            self._loaded_layer_ids = keep
        else:
            for lid in self._loaded_layer_ids:
                if QgsProject.instance().mapLayer(lid):
                    QgsProject.instance().removeMapLayer(lid)
            self._loaded_layer_ids.clear()
        # Also remove outlet scratch layer
        if self._outlet_layer and self._outlet_layer.id():
            if QgsProject.instance().mapLayer(self._outlet_layer.id()):
                QgsProject.instance().removeMapLayer(self._outlet_layer.id())
            self._outlet_layer = None
        self.iface.mapCanvas().refresh()

    def on_load_layers(self):
        """Load outputs for the currently active tab only (Phase 1 or Phase 2)."""
        out = self._get_output_dir() or self._output_dir
        if not out: return
        tab = self.tabs.currentIndex()
        if tab == 0:
            outputs = self.PHASE1_OUTPUTS
        else:
            outputs = self.PHASE2_OUTPUTS
        self._load_output_list(outputs, out)

    def _load_output_list(self, outputs, out):
        loaded = skipped = 0
        dem_crs = self._dem_crs
        for fname, display, ftype, _ in outputs:
            chk = self._out_checks.get(fname)
            if chk and not chk.isChecked(): skipped += 1; continue
            if fname == "WBT_UnnestBasins.tif":
                for i in range(1, 30):
                    fp = os.path.join(out, f"WBT_UnnestBasins_{i}.tif")
                    if not os.path.exists(fp): break
                    lyr = QgsRasterLayer(fp, f"UnnestBasins_{i}")
                    if lyr.isValid():
                        if dem_crs and dem_crs.isValid():
                            lyr.setCrs(dem_crs)
                        QgsProject.instance().addMapLayer(lyr)
                        self._loaded_layer_ids.append(lyr.id())
                        loaded += 1
                continue
            fpath = os.path.join(out, fname)
            if not os.path.exists(fpath): continue
            if ftype == "raster":
                lyr = QgsRasterLayer(fpath, display)
            else:
                lyr = QgsVectorLayer(fpath, display, "ogr")
            if lyr.isValid():
                if dem_crs and dem_crs.isValid():
                    lyr.setCrs(dem_crs)
                if ftype == "vector" and "stream" in display.lower():
                    self._style_stream_layer(lyr)
                QgsProject.instance().addMapLayer(lyr)
                self._loaded_layer_ids.append(lyr.id())
                loaded += 1
        self.iface.mapCanvas().refresh()
        self.append_log(f"Loaded {loaded} layer(s) ({skipped} unchecked).", "SUCCESS")
        QMessageBox.information(self, "Layers Loaded",
            f"{loaded} layer(s) added to QGIS map.\n({skipped} were unchecked.)")

    def _log(self, msg, level="INFO"):
        self.append_log(msg, level)

    def append_log(self, message, level="INFO"):
        colors = {"INFO":"#d4d4d4","SUCCESS":"#4ec94e",
                  "WARNING":"#f0c060","ERROR":"#f07070","STEP":"#60b0f0"}
        icons  = {"SUCCESS":"✅","WARNING":"⚠️","ERROR":"❌","STEP":"▶"}
        color  = colors.get(level, "#d4d4d4")
        icon   = icons.get(level, "•")
        self.log_text.append(f'<span style="color:{color};">{icon} {message}</span>')
        cur = self.log_text.textCursor()
        cur.movePosition(QTextCursor.End)
        self.log_text.setTextCursor(cur)
