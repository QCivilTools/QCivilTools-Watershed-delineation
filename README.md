# QCT Watershed Delineation

<p align="center">
  <img src="icons/watershed.png" width="96" alt="QCT Watershed Delineation icon"/>
</p>

<p align="center">
  <b>Part of the <a href="https://github.com/QCivilTools">QCivilTools</a> QGIS plugin suite</b><br/>
  Automated two-phase watershed delineation powered by WhiteboxTools — no command line required.
</p>

---

## Overview

**QCT Watershed Delineation** is a QGIS plugin that automates the full watershed delineation workflow from a raw DEM through to subbasin polygons and flow path analysis. It runs WhiteboxTools (WBT) behind the scenes so you work entirely within the QGIS interface.

The workflow is split into two phases so you can inspect intermediate results before committing to a full delineation.

---

## Features

### Phase 1 — DEM Preprocessing & Stream Network
| Step | Operation |
|------|-----------|
| 1 | Fill depressions (breach/fill DEM) |
| 2 | D8 flow direction |
| 3 | Flow accumulation |
| 4 | Extract stream network (threshold-based) |
| 5 | Vectorise stream network to polylines |

### Phase 2 — Watershed & Subbasin Analysis
| Step | Operation |
|------|-----------|
| 6 | Snap pour points to nearest stream |
| 7 | Delineate watershed polygon |
| 8 | Vectorise watershed boundary |
| 9 | Unnest basins (multi-outlet support) |
| 10 | Longest flow path per subbasin |
| 11 | Subbasins (masked to watershed) |
| 12 | Subbasin info shapefile (area, perimeter, ID) |
| 13 | All-DEM subbasins shapefile |

---

## Requirements

| Dependency | Version |
|------------|---------|
| QGIS | ≥ 3.16 |
| Python | ≥ 3.9 |
| WhiteboxTools | ≥ 2.3 (binary on PATH or configured in plugin settings) |

WhiteboxTools can be downloaded free from [whiteboxgeo.com](https://www.whiteboxgeo.com/geospatial-software/).

---

## Installation

### From ZIP (manual)
1. Download the latest `qct_watershed.zip` from [Releases](../../releases).
2. In QGIS: **Plugins → Manage and Install Plugins → Install from ZIP**.
3. Browse to the downloaded ZIP and click **Install Plugin**.
4. The plugin appears under **QCivilTools → Watershed Delineation** in the menu bar.

### From QGIS Plugin Repository
> Coming soon — submission in progress.

---

## Quick Start

1. Load a DEM raster into your QGIS project.
2. Open **QCivilTools → Watershed Delineation**.
3. **Phase 1**: Select your DEM, set a flow accumulation threshold, and click **Run Phase 1**. Review the extracted stream network.
4. **Phase 2**: Place an outlet point layer (or click to add pour points), then click **Run Phase 2**. Subbasin polygons and flow paths are added to your project automatically.

---

## Output Layers

| Layer | Format | Description |
|-------|--------|-------------|
| Filled DEM | Raster | Depression-filled DEM |
| Flow Direction | Raster | D8 direction grid |
| Flow Accumulation | Raster | Upstream cell count |
| Streams (raster) | Raster | Binary stream mask |
| Streams (vector) | Polyline | Stream network |
| Watershed | Polygon | Delineated watershed boundary |
| Subbasins | Polygon | Individual subbasins masked to watershed |
| Subbasin Info | Polygon | Subbasins with area/perimeter attributes |
| Longest Flow Path | Polyline | Longest hydraulic flow path per subbasin |
| All-DEM Subbasins | Polygon | Full DEM subbasin coverage |

---

## Part of QCivilTools

| Plugin | Description |
|--------|-------------|
| [QCT HEC-RAS Manager](https://github.com/QCivilTools/QCivilTools-HEC-RAS-manager) | HEC-RAS 2D workflow manager — project browser, plan editor, batch runner, result viewer |
| **QCT Watershed Delineation** | Two-phase watershed delineation using WhiteboxTools |

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Author

**Dat Vu** 
[datmast@gmail.com](mailto:datmast@gmail.com)

Issues and feature requests: [GitHub Issues](../../issues)
