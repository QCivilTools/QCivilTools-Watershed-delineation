# QCT Watershed Delineation

<p align="center">
  <img src="https://raw.githubusercontent.com/QCivilTools/QCivilTools-Watershed-delineation/main/icons/watershed.png" width="96" alt="QCT Watershed Delineation icon"/>
</p>

<p align="center">
  <b>Part of the <a href="https://github.com/QCivilTools">QCivilTools</a> QGIS plugin suite</b><br/>
  Automated two-phase watershed delineation powered by WhiteboxTools — no command line required.
</p>

---

## Overview

**QCT Watershed Delineation** is a QGIS plugin that automates the full watershed delineation workflow from a raw DEM through to subbasin polygons and flow path analysis. It runs WhiteboxTools (WBT) behind the scenes so you work entirely within the QGIS interface.

The workflow is split into two phases so you can inspect intermediate results before committing to a full delineation.

> ✅ All output files share the **same CRS as the input DEM** — no manual reprojection needed.

---

## QGIS Version Compatibility

| QGIS Version | Status |
|---|---|
| 3.16 LTR | ✅ Supported (minimum) |
| 3.22, 3.28, 3.34 LTR | ✅ Supported |
| 3.36, 3.40, 3.44 | ✅ Supported |
| 3.x on Windows / Linux / macOS | ✅ Supported |
| QGIS 2.x | ❌ Not supported |

---

## Installing WhiteboxTools

WhiteboxTools (WBT) is a separate free binary that must be installed before running the plugin.

### Option A — Bundled ZIP (recommended)

Download WBT pre-built from this repository — no extra setup required:

📦 **[Download WhiteboxTools.zip](https://github.com/QCivilTools/QCivilTools-Watershed-delineation/blob/main/WhiteboxTools.zip)**

1. Download and extract `WhiteboxTools.zip` to a folder (e.g. `C:\WBT\` on Windows or `~/WBT/` on Linux/macOS).
2. In the plugin, click **Browse Folder…** and select that folder. The plugin will find the executable automatically.

### Option B — Official release

Download from the official repository: [github.com/jblindsay/whitebox-tools/releases](https://github.com/jblindsay/whitebox-tools/releases)

1. Download the archive for your OS and extract it to a folder.
2. In the plugin, click **Browse Folder…** and select the extracted folder.

### Option C — QGIS Processing provider

1. In QGIS: **Plugins → Manage and Install Plugins** → search for **"WhiteboxTools for Processing"**.
2. Install and configure the provider — the plugin will detect the WBT path automatically.

### Setting the WBT path

Click **Browse Folder…** and select the folder containing the WhiteboxTools binary. The plugin will automatically find `whitebox_tools.exe` (Windows) or `whitebox_tools` (Linux/macOS) inside.

Leave the field blank to auto-detect from:
- System `PATH`
- `C:\WBT\`
- `C:\whitebox_tools\`
- `C:\WhiteboxTools_win_amd64\WBT\`
- `~/WBT/` (Linux/macOS)
- QGIS Processing provider setting

---

## Plugin Installation

### From ZIP (manual)
1. Download the latest `qct_watershed.zip` from [Releases](../../releases).
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Browse to the downloaded ZIP and click **Install Plugin**.
4. The plugin appears under **QCivilTools → Watershed Delineation** in the menu bar.

### From QGIS Plugin Repository
> Coming soon — submission in progress.

---

## Quick Start

1. Load a DEM raster into your QGIS project (must be in a **projected CRS**, e.g. NZTM2000 EPSG:2193).
2. Open **QCivilTools → Watershed Delineation**.
3. **Phase 1 tab**: Select your DEM, set the output directory, configure the stream threshold, and click **▶ Run Phase 1**.
4. Tick the outputs you want, then click **🗺 Load Selected Outputs** — only Phase 1 layers are loaded. Inspect the stream network before continuing.
5. **Phase 2 tab**: Click **📍 Pick Outlets on Map** and click directly on the stream network in the QGIS canvas.
   - **Yes — Save & Finish**: saves the point and stops picking.
   - **Add — Save & Continue**: saves the point and lets you pick another.
   - **No — Discard & Continue**: discards the click and lets you try again.
6. Click **▶ Run Phase 2**. Subbasin polygons and flow paths are written to disk.
7. Tick the outputs you want, then click **🗺 Load Selected Outputs** — only Phase 2 layers are loaded.

> **Note:** Closing the plugin will prompt you to unload all loaded layers from the QGIS project. Re-running either phase automatically unloads layers from that phase before the new run starts.

---

## Features

### Phase 1 — DEM Preprocessing & Stream Network

| Step | Operation |
|------|-----------|
| 1 | Fill depressions |
| 2 | D8 flow direction |
| 3 | Flow accumulation |
| 4 | Extract stream network (threshold-based) |
| 5 | Vectorise stream network to polylines |

No layers are loaded automatically after Phase 1. Tick the outputs you want and click **🗺 Load Selected Outputs**.

### Phase 2 — Watershed & Subbasin Analysis

| Step | Operation |
|------|-----------|
| 6 | Snap pour points to nearest stream |
| 7 | Delineate watershed polygon |
| 8 | Vectorise watershed boundary |
| 9 | Unnest basins (multi-outlet support) |
| 10 | Longest flow path per subbasin |
| 11 | Subbasins masked to watershed |
| 12 | Subbasin info shapefile (area, perimeter, slope) |
| 13 | All-DEM subbasins shapefile |

### Outlet Point Picking

Outlet points can be added in two ways:

- **Click on Map** — click **📍 Pick Outlets on Map** to enter pick mode. Each click on the canvas opens a confirmation dialog with three options: save and finish, save and pick another, or discard and try again. Click **✅ Done Picking** at any time to finish.
- **Existing layer** — select any point layer from the map or browse to a `.shp` file.

### Layer Management

- **Load button is tab-aware**: clicking **🗺 Load Selected Outputs** on the Phase 1 tab loads only Phase 1 outputs; on the Phase 2 tab it loads only Phase 2 outputs.
- **Re-running a phase** automatically unloads layers from the previous run of that phase before starting.
- **Closing the plugin** prompts whether to remove all loaded layers from the project.
- All output defaults are unchecked — you choose exactly what gets loaded.

---

## Output Layers

All outputs share the CRS of the input DEM.

| Layer | Format | Description |
|-------|--------|-------------|
| Filled DEM | Raster | Depression-filled DEM |
| Flow Direction | Raster | D8 direction grid |
| Flow Accumulation | Raster | Upstream cell count |
| Streams (raster) | Raster | Binary stream mask |
| Streams (vector) | Polyline | Stream network |
| Watershed | Polygon | Delineated watershed boundary |
| Subbasins | Polygon | Individual subbasins masked to watershed |
| Subbasin Info ★ | Polygon | Subbasins with area, perimeter, and slope attributes |
| Longest Flow Path | Polyline | Longest hydraulic flow path per subbasin |
| All-DEM Subbasins ★ | Polygon | Full DEM subbasin coverage |

### Subbasin attributes

| Field | Description |
|-------|-------------|
| `SB_ID` | Subbasin ID |
| `AREA_M2` / `AREA_HA` | Area in m² and hectares |
| `LFP_LEN` | Longest flow path length (m) |
| `LFP_UP` / `LFP_DN` | Upstream / downstream elevation |
| `LFP_SLP` | Average slope (%) |
| `SLP_EA` | Equal-area slope % (Taylor-Schwartz) |

---

## Requirements

| Dependency | Version |
|------------|---------|
| QGIS | ≥ 3.16 |
| Python | ≥ 3.9 |
| WhiteboxTools | ≥ 2.3 |
| GDAL/OGR | Bundled with QGIS |

---

## Related Plugins

📦 **[Plugin repository & addons](https://github.com/QCivilTools/QCivilTools-Watershed-delineation)**

| Plugin | Description |
|--------|-------------|
| [QCT HEC-RAS Manager](https://github.com/QCivilTools/QCivilTools-HEC-RAS-manager) | HEC-RAS 2D workflow — project browser, plan editor, batch runner, result viewer |
| **QCT Watershed Delineation** | Two-phase watershed delineation using WhiteboxTools |
| QCT 3D Civil Tool | Surfaces, alignments, and earthworks |
| QCT Coordinate Converter | NZGD49 → NZTM2000/NZVD2016 using NZGeoid2016 |
| QCT PENZD Exporter | Point layer → PENZD CSV for total stations and CAD |

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Author

**Dat Vu**  
[datmast@gmail.com](mailto:datmast@gmail.com)  
[github.com/QCivilTools](https://github.com/QCivilTools)  
Issues and feature requests: [GitHub Issues](../../issues)
