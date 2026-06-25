# -*- coding: utf-8 -*-
"""
WatershedProcessor  —  v7.0.0
Author : Dat Vu <datmast@gmail.com>

Thread-safety contract
----------------------
  run_phase1()       → runs in QThread  (WBT subprocess only, NO QGIS API)
  run_phase2_wbt()   → runs in QThread  (WBT subprocess only, NO QGIS API)
  run_phase2_qgis()  → runs on MAIN THREAD (QGIS/GDAL geometry ops)

All QGIS geometry operations (intersection, area calc, raster identify) are
confined to run_phase2_qgis() and _build_subbasins_info() which are called
from the main thread only.  This avoids the Windows GEOS / Qt thread-safety
crashes seen in earlier versions.

_run_wbt() uses Popen + communicate() with CREATE_NO_WINDOW on Windows to
avoid the subprocess reader-thread access violation.
"""

import os
import subprocess
import shutil
import platform
import math


class WatershedProcessor:

    PHASE1_STEPS   = 5
    PHASE2_WBT_STEPS  = 6   # steps 6-11
    PHASE2_QGIS_STEPS = 2   # steps 12-13
    TOTAL_STEPS    = 13

    def __init__(self):
        self.log_callback      = None
        self.progress_callback = None
        self.cancel_requested  = False

    # ═══════════════════════════════════════════════════════════════
    #  PHASE 1  —  QThread-safe  (WBT subprocess only)
    # ═══════════════════════════════════════════════════════════════

    def run_phase1(self, params: dict):
        """Steps 1-5. Returns (ok, msg, streams_shp_path)."""
        self.cancel_requested = False
        try:
            wbt = self._resolve_wbt(params.get("wbt_path"))
            if wbt is None:
                return False, "WhiteboxTools executable not found.", None

            out = params["output_dir"]
            dem = params["dem_path"]
            os.makedirs(out, exist_ok=True)
            self._log(f"WBT: {wbt}", "INFO")
            step = 0

            # Step 1 — Fill Depressions
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 1/13 — Fill Depressions (FillDepressionsWangAndLiu)", "STEP")
            filled_dem = os.path.join(out, "WBT_Filled_DEM.tif")
            args = ["--run=FillDepressionsWangAndLiu",
                    f"--dem={dem}", f"--output={filled_dem}"]
            if params.get("fix_flat", True):
                args.append("--fix_flats")
            fi = params.get("flat_increment", 0.001)
            if fi > 0:
                args.append(f"--flat_increment={fi}")
            ok, msg = self._run_wbt(wbt, args)
            if not ok: return False, f"Step 1 failed: {msg}", None
            self._progress(step)

            # Step 2 — D8 Pointer
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 2/13 — D8 Pointer", "STEP")
            d8_pointer = os.path.join(out, "WBT_D8_Pointer.tif")
            args = ["--run=D8Pointer",
                    f"--dem={filled_dem}", f"--output={d8_pointer}"]
            if params.get("esri_pointer"):
                args.append("--esri_pntr")
            ok, msg = self._run_wbt(wbt, args)
            if not ok: return False, f"Step 2 failed: {msg}", None
            self._progress(step)

            # Step 3 — D8 Flow Accumulation
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 3/13 — D8 Flow Accumulation", "STEP")
            d8_accumu = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            args = ["--run=D8FlowAccumulation",
                    f"--dem={filled_dem}", f"--output={d8_accumu}",
                    "--out_type=cells"]
            if params.get("log_transform"):
                args.append("--log")
            ok, msg = self._run_wbt(wbt, args)
            if not ok: return False, f"Step 3 failed: {msg}", None
            self._progress(step)

            # Step 4 — Extract Streams
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 4/13 — Extract Streams", "STEP")
            streams_raster = os.path.join(out, "WBT_ExtractStreams.tif")
            threshold = self._resolve_threshold(params, filled_dem)
            self._log(f"  Threshold: {threshold:.0f} cells", "INFO")
            args = ["--run=ExtractStreams",
                    f"--flow_accum={d8_accumu}",
                    f"--output={streams_raster}",
                    f"--threshold={threshold}"]
            ok, msg = self._run_wbt(wbt, args)
            if not ok: return False, f"Step 4 failed: {msg}", None
            self._progress(step)

            # Step 5 — Vectorize Streams
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 5/13 — Vectorize Stream Network", "STEP")
            streams_shp = os.path.join(out, "WBT_ExtractStreams_vector.shp")
            args = ["--run=RasterStreamsToVector",
                    f"--streams={streams_raster}",
                    f"--d8_pntr={d8_pointer}",
                    f"--output={streams_shp}"]
            if params.get("esri_pointer"):
                args.append("--esri_pntr")
            ok, msg = self._run_wbt(wbt, args)
            if not ok:
                self._log(f"Vectorize streams warning: {msg}", "WARNING")
                streams_shp = None
            else:
                self._log("Stream network ready — place outlet then run Phase 2.", "SUCCESS")
            self._progress(step)

            return True, "Phase 1 complete.", streams_shp

        except Exception as e:
            import traceback
            self._log(traceback.format_exc(), "ERROR")
            return False, str(e), None

    # ═══════════════════════════════════════════════════════════════
    #  PHASE 2 WBT  —  QThread-safe  (WBT subprocess only)
    # ═══════════════════════════════════════════════════════════════

    def run_phase2_wbt(self, params: dict):
        """
        Steps 6-11: WBT calls only.
        If params['no_outlet'] is True, steps 6-10 are skipped and the entire
        DEM is used to generate subbasins directly (no pour-point required).
        Returns (ok, msg, ctx) where ctx is passed to run_phase2_qgis().
        """
        self.cancel_requested = False
        try:
            wbt = self._resolve_wbt(params.get("wbt_path"))
            if wbt is None:
                return False, "WhiteboxTools executable not found.", None

            out            = params["output_dir"]
            no_outlet      = params.get("no_outlet", False)
            filled_dem     = os.path.join(out, "WBT_Filled_DEM.tif")
            d8_pointer     = os.path.join(out, "WBT_D8_Pointer.tif")
            streams_raster = os.path.join(out, "WBT_ExtractStreams.tif")

            for f, n in [(filled_dem, "WBT_Filled_DEM.tif"),
                         (d8_pointer, "WBT_D8_Pointer.tif"),
                         (streams_raster, "WBT_ExtractStreams.tif")]:
                if not os.path.exists(f):
                    return False, (
                        f"Phase 1 output '{n}' not found. Run Phase 1 first."), None

            step = self.PHASE1_STEPS

            if no_outlet:
                # ── NO OUTLET MODE ─────────────────────────────────────────
                # Skip steps 6-10 (snap, watershed, unnest, LFP).
                # Jump straight to Step 11 — full DEM subbasins.
                self._log("Phase 2 running in NO-OUTLET mode — all subbasins across full DEM.", "INFO")
                self._log("Steps 6-10 (snap/watershed/unnest/LFP) are skipped.", "INFO")
                step += 5   # advance counter past skipped steps

                # Step 11 — Subbasins full DEM
                if self._cancelled(): return False, "Cancelled.", None
                step += 1
                self._log("STEP 11/13 — Subbasins (full DEM)", "STEP")
                subbasins_full = os.path.join(out, "_tmp_Subbasins_full.tif")
                args = ["--run=Subbasins",
                        f"--d8_pntr={d8_pointer}",
                        f"--streams={streams_raster}",
                        f"--output={subbasins_full}"]
                if params.get("esri_pointer"):
                    args.append("--esri_pntr")
                ok, msg = self._run_wbt(wbt, args)
                sub_ok = ok
                if not ok:
                    self._log(f"Subbasins warning: {msg}", "WARNING")
                    subbasins_full = None

                lfp_all_shp = None
                if sub_ok and subbasins_full:
                    self._log("  Computing LFP per subbasin (full DEM)…", "INFO")
                    lfp_all_shp = os.path.join(out, "_tmp_lfp_alldem.shp")
                    ok_la, msg_la = self._run_wbt(wbt, [
                        "--run=LongestFlowpath",
                        f"--dem={filled_dem}",
                        f"--basins={subbasins_full}",
                        f"--output={lfp_all_shp}"])
                    if not ok_la:
                        self._log("  LFP per subbasin warning.", "WARNING")
                        lfp_all_shp = None
                self._progress(step)

                ctx = {
                    "out":              out,
                    "filled_dem":       filled_dem,
                    "watershed_raster": None,       # no watershed in no-outlet mode
                    "subbasins_full":   subbasins_full,
                    "lfp_ws_shp":       None,
                    "lfp_all_shp":      lfp_all_shp,
                    "sub_ok":           sub_ok,
                    "wbt":              wbt,
                    "no_outlet":        True,
                }
                return True, "Phase 2 WBT complete (no-outlet mode).", ctx

            # ── NORMAL (with outlet) MODE ──────────────────────────────────
            outlet = params["outlet_path"]

            # Step 6 — Snap Pour Points
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 6/13 — Snap Pour Points (JensonSnapPourPoints)", "STEP")
            outlet_snapped = os.path.join(out, "outlet_snapped.shp")
            ok, msg = self._run_wbt(wbt, [
                "--run=JensonSnapPourPoints",
                f"--pour_pts={outlet}",
                f"--streams={streams_raster}",
                f"--output={outlet_snapped}",
                f"--snap_dist={params.get('snap_distance', 50)}"])
            if not ok: return False, f"Step 6 failed: {msg}", None
            self._progress(step)

            # Step 7 — Watershed
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 7/13 — Delineate Watershed", "STEP")
            watershed_raster = os.path.join(out, "WBT_Watershed.tif")
            args = ["--run=Watershed",
                    f"--d8_pntr={d8_pointer}",
                    f"--pour_pts={outlet_snapped}",
                    f"--output={watershed_raster}"]
            if params.get("esri_pointer"):
                args.append("--esri_pntr")
            ok, msg = self._run_wbt(wbt, args)
            if not ok: return False, f"Step 7 failed: {msg}", None
            self._progress(step)

            # Step 8 — Vectorize Watershed
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 8/13 — Vectorize Watershed", "STEP")
            watershed_shp = os.path.join(out, "WBT_Watershed_Boundary.shp")
            ok, msg = self._run_wbt(wbt, [
                "--run=RasterToVectorPolygons",
                f"--input={watershed_raster}",
                f"--output={watershed_shp}"])
            if not ok: return False, f"Step 8 failed: {msg}", None
            self._progress(step)

            # Step 9 — UnnestBasins (optional)
            step += 1
            if params.get("run_unnest", True):
                if self._cancelled(): return False, "Cancelled.", None
                self._log("STEP 9/13 — UnnestBasins", "STEP")
                args = ["--run=UnnestBasins",
                        f"--d8_pntr={d8_pointer}",
                        f"--pour_pts={outlet_snapped}",
                        f"--output={os.path.join(out, 'WBT_UnnestBasins.tif')}"]
                if params.get("esri_pointer"):
                    args.append("--esri_pntr")
                ok, msg = self._run_wbt(wbt, args)
                if not ok:
                    self._log(f"UnnestBasins warning: {msg}", "WARNING")
            else:
                self._log("STEP 9/13 — UnnestBasins skipped.", "INFO")
            self._progress(step)

            # Step 10 — LongestFlowPath whole watershed (optional)
            step += 1
            if params.get("run_longest_flow", True):
                if self._cancelled(): return False, "Cancelled.", None
                self._log("STEP 10/13 — LongestFlowPath (whole watershed)", "STEP")
                ok, msg = self._run_wbt(wbt, [
                    "--run=LongestFlowpath",
                    f"--dem={filled_dem}",
                    f"--basins={watershed_raster}",
                    f"--output={os.path.join(out, 'WBT_LongestFlowPath.shp')}"])
                if not ok:
                    self._log(f"LongestFlowPath warning: {msg}", "WARNING")
            else:
                self._log("STEP 10/13 — LongestFlowPath skipped.", "INFO")
            self._progress(step)

            # Step 11 — Subbasins (full DEM) + per-subbasin LFP
            if self._cancelled(): return False, "Cancelled.", None
            step += 1
            self._log("STEP 11/13 — Subbasins (full DEM) + per-subbasin LFP", "STEP")
            subbasins_full = os.path.join(out, "_tmp_Subbasins_full.tif")
            sub_ok = False

            args = ["--run=Subbasins",
                    f"--d8_pntr={d8_pointer}",
                    f"--streams={streams_raster}",
                    f"--output={subbasins_full}"]
            if params.get("esri_pointer"):
                args.append("--esri_pntr")
            ok, msg = self._run_wbt(wbt, args)
            sub_ok = ok
            if not ok:
                self._log(f"Subbasins warning: {msg}", "WARNING")
                subbasins_full = None

            lfp_ws_shp  = None
            lfp_all_shp = None
            if sub_ok and subbasins_full:
                self._log("  Computing LFP per subbasin (full DEM)…", "INFO")
                lfp_all_shp = os.path.join(out, "_tmp_lfp_alldem.shp")
                ok_la, msg_la = self._run_wbt(wbt, [
                    "--run=LongestFlowpath",
                    f"--dem={filled_dem}",
                    f"--basins={subbasins_full}",
                    f"--output={lfp_all_shp}"])
                if not ok_la:
                    self._log("  LFP per subbasin warning.", "WARNING")
                    lfp_all_shp = None
                else:
                    lfp_ws_shp = lfp_all_shp

            self._progress(step)

            ctx = {
                "out":              out,
                "filled_dem":       filled_dem,
                "watershed_raster": watershed_raster,
                "subbasins_full":   subbasins_full,
                "lfp_ws_shp":       lfp_ws_shp,
                "lfp_all_shp":      lfp_all_shp,
                "sub_ok":           sub_ok,
                "wbt":              wbt,
                "no_outlet":        False,
            }
            return True, "Phase 2 WBT complete.", ctx

        except Exception as e:
            import traceback
            self._log(traceback.format_exc(), "ERROR")
            return False, str(e), None

    # ═══════════════════════════════════════════════════════════════
    #  PHASE 2 QGIS  —  MAIN THREAD ONLY  (geometry / raster ops)
    # ═══════════════════════════════════════════════════════════════

    def run_phase2_qgis(self, ctx: dict):
        """
        Steps 12-13: mask subbasin raster + build info shapefiles.
        Called on MAIN THREAD via QTimer.singleShot deferral in dialog.
        In no_outlet mode, Step 12 (watershed masking) is skipped.
        """
        if ctx is None:
            return False, "No WBT context."
        try:
            out              = ctx["out"]
            filled_dem       = ctx["filled_dem"]
            watershed_raster = ctx.get("watershed_raster")
            subbasins_full   = ctx.get("subbasins_full")
            lfp_ws_shp       = ctx.get("lfp_ws_shp")
            lfp_all_shp      = ctx.get("lfp_all_shp")
            sub_ok           = ctx.get("sub_ok", False)
            wbt              = ctx["wbt"]
            no_outlet        = ctx.get("no_outlet", False)

            subbasins_raster = os.path.join(out, "WBT_Subbasins.tif")
            subbasins_shp    = os.path.join(out, "WBT_Subbasins_Info.shp")
            alldem_shp       = os.path.join(out, "WBT_AllDEM_Subbasins.shp")

            # Step 12 — Mask + WBT_Subbasins_Info.shp
            if no_outlet:
                # No watershed boundary → skip masking, skip WBT_Subbasins_Info.shp
                self._log("STEP 12/13 — Skipped (no-outlet mode, no watershed boundary).", "INFO")
            else:
                self._log("STEP 12/13 — Mask subbasins + WBT_Subbasins_Info.shp", "STEP")
                if sub_ok and subbasins_full and os.path.exists(subbasins_full):
                    mask_ok, mask_msg = self._mask_raster_to_watershed(
                        subbasins_full, watershed_raster, subbasins_raster)
                    if not mask_ok:
                        self._log(f"  Mask warning: {mask_msg} — using full raster.", "WARNING")
                        shutil.copy2(subbasins_full, subbasins_raster)

                    ok2, msg2 = self._build_subbasins_info(
                        wbt=wbt,
                        filled_dem=filled_dem,
                        subbasins_raster=subbasins_raster,
                        lfp_shp_path=lfp_ws_shp,
                        output_shp=subbasins_shp,
                        output_dir=out,
                        label="Watershed")
                    if ok2:
                        self._log("WBT_Subbasins_Info.shp done.", "SUCCESS")
                    else:
                        self._log(f"WBT_Subbasins_Info.shp warning: {msg2}", "WARNING")
                else:
                    self._log("STEP 12/13 — Skipped (no subbasin raster).", "WARNING")
            self._progress(12)

            # Enrich WBT_Watershed_Boundary.shp (with-outlet mode only)
            if not no_outlet:
                ws_shp = os.path.join(out, "WBT_Watershed_Boundary.shp")
                lfp_ws_whole = os.path.join(out, "WBT_LongestFlowPath.shp")
                if os.path.exists(ws_shp):
                    self._log("Enriching WBT_Watershed_Boundary.shp with area + LFP attributes…", "INFO")
                    ok_ws, msg_ws = self._enrich_watershed_boundary(
                        ws_shp, lfp_ws_whole, filled_dem, out)
                    if ok_ws:
                        self._log("WBT_Watershed_Boundary.shp enriched.", "SUCCESS")
                    else:
                        self._log(f"Watershed boundary enrichment warning: {msg_ws}", "WARNING")

            # Step 13 — WBT_AllDEM_Subbasins.shp
            self._log("STEP 13/13 — All-DEM Subbasins shapefile", "STEP")
            if sub_ok and subbasins_full and os.path.exists(subbasins_full):
                ok3, msg3 = self._build_subbasins_info(
                    wbt=wbt,
                    filled_dem=filled_dem,
                    subbasins_raster=subbasins_full,
                    lfp_shp_path=lfp_all_shp,
                    output_shp=alldem_shp,
                    output_dir=out,
                    label="AllDEM")
                if ok3:
                    self._log("WBT_AllDEM_Subbasins.shp done.", "SUCCESS")
                else:
                    self._log(f"WBT_AllDEM_Subbasins.shp warning: {msg3}", "WARNING")
            else:
                self._log("STEP 13/13 — Skipped.", "WARNING")
            self._progress(13)

            # Cleanup temp rasters
            for tmp in [subbasins_full,
                        os.path.join(out, "_tmp_lfp_alldem.shp")]:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

            self._log("=" * 55, "INFO")
            self._log("All steps complete!", "SUCCESS")
            self._list_outputs(out)
            return True, f"Complete! Outputs: {out}"

        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self._log(tb, "ERROR")
            return False, f"{e}\n\nFull traceback in Log tab."

    # ═══════════════════════════════════════════════════════════════
    #  Watershed boundary enrichment  —  area + whole-WS LFP attrs
    # ═══════════════════════════════════════════════════════════════

    def _enrich_watershed_boundary(self, ws_shp, lfp_shp, filled_dem, output_dir):
        """
        Rewrite WBT_Watershed_Boundary.shp adding:
          WS_AREA_M2, WS_AREA_HA  — watershed area
          LFP_LEN     — whole-watershed longest flow path length (m)
          LFP_UP      — LFP upstream elevation
          LFP_DN      — LFP downstream elevation
          LFP_SLP     — simple slope (H_up-H_dn)/L × 100%
          SLP_EA      — equal-area slope (H85-H10)/L × 100%
        Uses positional field indices — no name lookup.
        """
        try:
            from osgeo import gdal, ogr, osr
            gdal.UseExceptions()
        except ImportError:
            return False, "GDAL not available"

        import uuid

        # ── Read original watershed polygons ──────────────────────────────────
        ws_ds  = ogr.Open(ws_shp)
        if ws_ds is None:
            return False, f"Cannot open {ws_shp}"
        ws_lyr  = ws_ds.GetLayer(0)
        ws_defn = ws_lyr.GetLayerDefn()
        srs     = ws_lyr.GetSpatialRef()

        # ── Read LFP attributes (whole watershed LFP) ─────────────────────────
        lfp_data = {}   # {basin_id: dict} — basin_id from BASIN field
        if lfp_shp and os.path.exists(lfp_shp):
            lfp_ds  = ogr.Open(lfp_shp)
            if lfp_ds:
                lfp_lyr  = lfp_ds.GetLayer(0)
                lfp_defn = lfp_lyr.GetLayerDefn()
                lfp_fnu  = [lfp_defn.GetFieldDefn(i).GetNameRef().upper()
                             for i in range(lfp_defn.GetFieldCount())]

                def _fv(feat, cands):
                    for c in cands:
                        if c.upper() in lfp_fnu:
                            fn = lfp_defn.GetFieldDefn(
                                lfp_fnu.index(c.upper())).GetNameRef()
                            try: return float(feat.GetField(fn))
                            except: pass
                    return -9999.0

                lfp_lyr.ResetReading()
                for lf in lfp_lyr:
                    bid    = -1
                    for c in ["BASIN","basin","VALUE","value"]:
                        if c.upper() in lfp_fnu:
                            fn = lfp_defn.GetFieldDefn(
                                lfp_fnu.index(c.upper())).GetNameRef()
                            try: bid = int(lf.GetField(fn)); break
                            except: pass
                    geom    = lf.GetGeometryRef()
                    raw_len = _fv(lf, ["LENGTH",   "length"])
                    raw_up  = _fv(lf, ["UP_ELEV",  "up_elev"])
                    raw_dn  = _fv(lf, ["DN_ELEV",  "dn_elev"])
                    raw_slp = _fv(lf, ["AVG_SLOPE","avg_slope"])
                    elevs   = self._sample_dem_gdal(filled_dem, geom, 200)
                    if elevs and len(elevs) >= 5:
                        up_elv  = max(elevs)
                        dn_elv  = min(elevs)
                        lfp_len = raw_len if raw_len > 0 else (geom.Length() if geom else -9999.0)
                        avg_slp = ((up_elv - dn_elv) / lfp_len * 100.0) if lfp_len > 0 else -9999.0
                        ea_slp  = self._equal_area_slope(elevs, lfp_len)
                    else:
                        up_elv = raw_up; dn_elv = raw_dn
                        lfp_len = raw_len; avg_slp = raw_slp; ea_slp = raw_slp
                    # Use bid=-1 as "whole watershed" if single outlet
                    lfp_data[bid] = dict(
                        LEN=lfp_len, UP=up_elv, DN=dn_elv,
                        SLP=avg_slp, EA=ea_slp)
                lfp_ds = None

        # ── Write enriched shapefile to temp, then replace original ───────────
        tmp_out = os.path.join(output_dir, f"_tmp_ws_enrich_{uuid.uuid4().hex[:6]}.shp")
        drv     = ogr.GetDriverByName("ESRI Shapefile")
        if os.path.exists(tmp_out):
            drv.DeleteDataSource(tmp_out)
        out_ds  = drv.CreateDataSource(os.path.dirname(tmp_out))
        out_lyr = out_ds.CreateLayer(
            os.path.splitext(os.path.basename(tmp_out))[0],
            srs=srs, geom_type=ogr.wkbPolygon)

        # Copy existing fields from original ws shapefile
        for i in range(ws_defn.GetFieldCount()):
            out_lyr.CreateField(ws_defn.GetFieldDefn(i))
        n_orig = ws_defn.GetFieldCount()

        # New fields — max 8 chars, positional
        new_fields = [
            ("WS_AR_M2", ogr.OFTReal,    20, 2),
            ("WS_AR_HA", ogr.OFTReal,    20, 4),
            ("LFP_LEN",  ogr.OFTReal,    20, 2),
            ("LFP_UP",   ogr.OFTReal,    20, 3),
            ("LFP_DN",   ogr.OFTReal,    20, 3),
            ("LFP_SLP",  ogr.OFTReal,    20, 4),
            ("SLP_EA",   ogr.OFTReal,    20, 4),
        ]
        for fname, ftype, width, prec in new_fields:
            fd = ogr.FieldDefn(fname, ftype)
            fd.SetWidth(width); fd.SetPrecision(prec)
            out_lyr.CreateField(fd)

        # Positional indices for new fields
        I_AR_M2 = n_orig + 0
        I_AR_HA = n_orig + 1
        I_LEN   = n_orig + 2
        I_UP    = n_orig + 3
        I_DN    = n_orig + 4
        I_SLP   = n_orig + 5
        I_EA    = n_orig + 6

        out_defn = out_lyr.GetLayerDefn()
        ws_lyr.ResetReading()
        for ws_feat in ws_lyr:
            geom    = ws_feat.GetGeometryRef()
            area_m2 = geom.GetArea() if geom else 0.0
            area_ha = area_m2 / 10000.0

            # Find matching LFP entry
            # Try by VALUE field first, then fall back to first entry
            bid = -1
            val_fn = None
            for c in ["VALUE","value","BASIN","basin"]:
                idx = ws_defn.GetFieldIndex(c)
                if idx >= 0:
                    try: bid = int(ws_feat.GetField(idx)); val_fn = c; break
                    except: pass

            lfp = lfp_data.get(bid) or (next(iter(lfp_data.values())) if lfp_data else {})

            nf = ogr.Feature(out_defn)
            if geom:
                nf.SetGeometry(geom.Clone())
            # Copy original fields
            for i in range(n_orig):
                nf.SetField(i, ws_feat.GetField(i))
            # Set new fields by position
            nf.SetField(I_AR_M2, round(area_m2, 2))
            nf.SetField(I_AR_HA, round(area_ha, 4))
            nf.SetField(I_LEN,   round(lfp.get("LEN", -9999.0), 2))
            nf.SetField(I_UP,    round(lfp.get("UP",  -9999.0), 3))
            nf.SetField(I_DN,    round(lfp.get("DN",  -9999.0), 3))
            nf.SetField(I_SLP,   round(lfp.get("SLP", -9999.0), 4))
            nf.SetField(I_EA,    round(lfp.get("EA",  -9999.0), 4))
            out_lyr.CreateFeature(nf)

        out_ds.FlushCache()
        out_ds = None
        ws_ds  = None

        # Replace original with enriched version
        drv.DeleteDataSource(ws_shp)
        # Move all component files
        tmp_base = os.path.splitext(tmp_out)[0]
        ws_base  = os.path.splitext(ws_shp)[0]
        for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj"]:
            src = tmp_base + ext
            dst = ws_base  + ext
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except Exception:
                    pass

        self._log("  WBT_Watershed_Boundary.shp: WS_AR_M2, WS_AR_HA, "
                  "LFP_LEN, LFP_UP, LFP_DN, LFP_SLP, SLP_EA added.", "SUCCESS")
        return True, "OK"

    # ═══════════════════════════════════════════════════════════════
    #  Raster masking  —  pure GDAL, no QgsRasterCalculator
    # ═══════════════════════════════════════════════════════════════

    def _mask_raster_to_watershed(self, subbasins_raster, watershed_raster,
                                   output_raster):
        """
        Mask subbasins_raster to watershed_raster using pure GDAL numpy math.
        Pixels outside the watershed (ws == nodata or ws == 0) → nodata in output.
        Does NOT use QgsRasterCalculator — avoids 'Invalid index: -1' in QGIS 3.40.
        """
        try:
            from osgeo import gdal
            import numpy as np
            gdal.UseExceptions()
        except ImportError:
            return False, "GDAL/numpy not available"

        try:
            # Open subbasin raster
            sub_ds   = gdal.Open(subbasins_raster, gdal.GA_ReadOnly)
            if sub_ds is None:
                return False, f"Cannot open {subbasins_raster}"
            sub_band = sub_ds.GetRasterBand(1)
            sub_nd   = sub_band.GetNoDataValue()
            sub_arr  = sub_band.ReadAsArray().astype(np.float64)
            gt       = sub_ds.GetGeoTransform()
            proj     = sub_ds.GetProjection()
            cols     = sub_ds.RasterXSize
            rows     = sub_ds.RasterYSize
            sub_ds   = None

            # Open watershed raster — may have different resolution/extent
            ws_ds    = gdal.Open(watershed_raster, gdal.GA_ReadOnly)
            if ws_ds is None:
                return False, f"Cannot open {watershed_raster}"
            ws_band  = ws_ds.GetRasterBand(1)
            ws_nd    = ws_band.GetNoDataValue()

            # Resample watershed to match subbasin grid if needed
            ws_cols = ws_ds.RasterXSize
            ws_rows = ws_ds.RasterYSize
            ws_gt   = ws_ds.GetGeoTransform()

            if ws_cols != cols or ws_rows != rows or ws_gt != gt:
                # Warp watershed to subbasin grid using gdal.Warp
                mem_drv = gdal.GetDriverByName("MEM")
                ws_mem  = mem_drv.Create("", cols, rows, 1, gdal.GDT_Float32)
                ws_mem.SetGeoTransform(gt)
                ws_mem.SetProjection(proj)
                gdal.ReprojectImage(ws_ds, ws_mem)
                ws_arr = ws_mem.GetRasterBand(1).ReadAsArray().astype(np.float64)
                ws_mem = None
            else:
                ws_arr = ws_band.ReadAsArray().astype(np.float64)
            ws_ds = None

            # Build mask: True where watershed is valid (not nodata, not 0)
            if ws_nd is not None:
                ws_mask = (ws_arr != ws_nd) & (ws_arr > 0)
            else:
                ws_mask = (ws_arr > 0)

            # Apply mask: outside watershed → -9999 (will become nodata)
            OUT_ND = -9999.0
            result = np.where(ws_mask, sub_arr, OUT_ND)

            # Write output GeoTIFF
            drv     = gdal.GetDriverByName("GTiff")
            out_ds  = drv.Create(output_raster, cols, rows, 1, gdal.GDT_Float32,
                                  options=["COMPRESS=LZW", "TILED=YES"])
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            out_band = out_ds.GetRasterBand(1)
            out_band.SetNoDataValue(OUT_ND)
            out_band.WriteArray(result.astype(np.float32))
            out_band.FlushCache()
            out_ds = None

            self._log(
                f"  Masked {int(ws_mask.sum())} cells inside watershed "
                f"({int((~ws_mask).sum())} outside → nodata).", "INFO")
            return True, "OK"

        except Exception as e:
            import traceback
            self._log(traceback.format_exc(), "WARNING")
            return False, str(e)

    # ═══════════════════════════════════════════════════════════════
    #  Subbasin info builder  —  pure GDAL/OGR, no WBT, no QGIS
    # ═══════════════════════════════════════════════════════════════

    def _build_subbasins_info(self, wbt, filled_dem, subbasins_raster,
                               lfp_shp_path, output_shp, output_dir, label="Sub"):
        """
        Build subbasin info shapefile using only GDAL/OGR.
        Polygonization uses gdal.Polygonize — no WBT subprocess, no QGIS API.
        LFP join is pure OGR attribute lookup + GDAL raster pixel sampling.
        """
        try:
            from osgeo import gdal, ogr, osr
            gdal.UseExceptions()
        except ImportError:
            return False, "GDAL not available"

        import uuid, struct

        self._log(f"  [{label}] Polygonizing subbasin raster (GDAL)…", "INFO")

        # ── (a) Polygonize with gdal.Polygonize into OGR Memory layer ───────
        src_ds   = gdal.Open(subbasins_raster, gdal.GA_ReadOnly)
        if src_ds is None:
            return False, f"Cannot open {subbasins_raster}"
        src_band = src_ds.GetRasterBand(1)
        proj_wkt = src_ds.GetProjection()
        src_ds_gt = src_ds.GetGeoTransform()

        srs = osr.SpatialReference()
        srs.ImportFromWkt(proj_wkt)

        # Use a temp file-based shapefile (Memory driver can cause issues on Windows)
        tmp_poly = os.path.join(output_dir, f"_tmp_{label}_{uuid.uuid4().hex[:6]}.shp")
        shp_drv  = ogr.GetDriverByName("ESRI Shapefile")
        if os.path.exists(tmp_poly):
            shp_drv.DeleteDataSource(tmp_poly)
        tmp_ds  = shp_drv.CreateDataSource(os.path.dirname(tmp_poly))
        tmp_lyr = tmp_ds.CreateLayer(
            os.path.splitext(os.path.basename(tmp_poly))[0],
            srs=srs, geom_type=ogr.wkbPolygon)
        fd = ogr.FieldDefn("VALUE", ogr.OFTInteger)
        tmp_lyr.CreateField(fd)

        # Use src_band as mask — only polygonize non-nodata cells
        gdal.Polygonize(src_band, src_band, tmp_lyr, 0, [], callback=None)
        tmp_ds.FlushCache()
        tmp_ds  = None
        src_ds  = None

        # Re-open for reading
        poly_ds  = ogr.Open(tmp_poly)
        if poly_ds is None:
            return False, f"Cannot open polygonized result: {tmp_poly}"
        poly_lyr  = poly_ds.GetLayer(0)
        poly_defn = poly_lyr.GetLayerDefn()

        poly_field_names = [poly_defn.GetFieldDefn(i).GetNameRef()
                            for i in range(poly_defn.GetFieldCount())]
        val_field = None
        for c in ["VALUE", "value", "FID", "fid"]:
            if c in poly_field_names:
                val_field = c
                break

        # ── (b) Build LFP attribute lookup {bid -> dict} ─────────────────────
        lfp_lookup = {}
        if lfp_shp_path and os.path.exists(lfp_shp_path):
            self._log(f"  [{label}] Building LFP attribute lookup…", "INFO")
            lfp_ds = ogr.Open(lfp_shp_path)
            if lfp_ds:
                lfp_lyr  = lfp_ds.GetLayer(0)
                lfp_defn = lfp_lyr.GetLayerDefn()
                lfp_fnu  = [lfp_defn.GetFieldDefn(i).GetNameRef().upper()
                             for i in range(lfp_defn.GetFieldCount())]

                def _fval(feat, candidates):
                    for c in candidates:
                        if c.upper() in lfp_fnu:
                            fn = lfp_defn.GetFieldDefn(
                                lfp_fnu.index(c.upper())).GetNameRef()
                            try:
                                return float(feat.GetField(fn))
                            except (TypeError, ValueError):
                                pass
                    return -9999.0

                def _bid(feat):
                    for c in ["BASIN","basin","VALUE","value"]:
                        if c.upper() in lfp_fnu:
                            fn = lfp_defn.GetFieldDefn(
                                lfp_fnu.index(c.upper())).GetNameRef()
                            try:
                                return int(feat.GetField(fn))
                            except (TypeError, ValueError):
                                pass
                    return feat.GetFID()

                lfp_lyr.ResetReading()
                for lf in lfp_lyr:
                    bid     = _bid(lf)
                    geom    = lf.GetGeometryRef()
                    raw_len = _fval(lf, ["LENGTH",    "length"])
                    raw_up  = _fval(lf, ["UP_ELEV",   "up_elev"])
                    raw_dn  = _fval(lf, ["DN_ELEV",   "dn_elev"])
                    raw_slp = _fval(lf, ["AVG_SLOPE", "avg_slope"])

                    elevs = self._sample_dem_gdal(filled_dem, geom, 150)
                    if elevs and len(elevs) >= 5:
                        up_elv  = max(elevs)
                        dn_elv  = min(elevs)
                        lfp_len = raw_len if raw_len > 0 else (geom.Length() if geom else -9999.0)
                        avg_slp = ((up_elv - dn_elv) / lfp_len * 100.0) if lfp_len > 0 else -9999.0
                        ea_slp  = self._equal_area_slope(elevs, lfp_len)
                    else:
                        up_elv  = raw_up;  dn_elv  = raw_dn
                        lfp_len = raw_len; avg_slp = raw_slp; ea_slp = raw_slp

                    lfp_lookup[bid] = dict(
                        LFP_LEN=lfp_len, LFP_UP=up_elv,
                        LFP_DN=dn_elv,   LFP_SLOPE=avg_slp, CH_SLP_EA=ea_slp)
                lfp_ds = None

        # ── (c) Write output shapefile ────────────────────────────────────────
        # GDAL ESRI Shapefile driver: CreateDataSource() needs the DIRECTORY,
        # not the full .shp path — passing a .shp path raises
        # "is not a directory" on Windows GDAL builds.
        self._log(f"  [{label}] Writing {os.path.basename(output_shp)}…", "INFO")
        out_drv      = ogr.GetDriverByName("ESRI Shapefile")
        shp_dir      = os.path.dirname(output_shp)
        shp_basename = os.path.splitext(os.path.basename(output_shp))[0]
        if os.path.exists(output_shp):
            out_drv.DeleteDataSource(output_shp)
        out_ds  = out_drv.CreateDataSource(shp_dir)
        out_lyr = out_ds.CreateLayer(shp_basename, srs=srs, geom_type=ogr.wkbPolygon)

        # Field names MUST be ≤8 chars — DBF spec says 10 but many GDAL/Windows
        # builds silently truncate to 8, causing the KeyError on lookup.
        # Using positional indices (0,1,2…) to set values — immune to name issues.
        field_defs = [
            # (name_max8, OGR_type,      width, precision)
            ("SB_ID",    ogr.OFTInteger, 10,    0),
            ("AREA_M2",  ogr.OFTReal,    20,    2),
            ("AREA_HA",  ogr.OFTReal,    20,    4),
            ("LFP_LEN",  ogr.OFTReal,    20,    2),   # LFP length (m)
            ("LFP_UP",   ogr.OFTReal,    20,    3),   # upstream elev
            ("LFP_DN",   ogr.OFTReal,    20,    3),   # downstream elev
            ("LFP_SLP",  ogr.OFTReal,    20,    4),   # simple slope %
            ("SLP_EA",   ogr.OFTReal,    20,    4),   # equal-area slope %
        ]
        for fname, ftype, width, prec in field_defs:
            fd = ogr.FieldDefn(fname, ftype)
            fd.SetWidth(width)
            if prec:
                fd.SetPrecision(prec)
            out_lyr.CreateField(fd)

        # Use positional indices 0..N — never look up by name
        F_SB_ID   = 0
        F_AREA_M2 = 1
        F_AREA_HA = 2
        F_LFP_LEN = 3
        F_LFP_UP  = 4
        F_LFP_DN  = 5
        F_LFP_SLP = 6
        F_SLP_EA  = 7

        lyr_defn = out_lyr.GetLayerDefn()
        written  = 0
        poly_lyr.ResetReading()
        for poly_feat in poly_lyr:
            if val_field:
                try:
                    bid = int(poly_feat.GetField(val_field))
                except (TypeError, ValueError):
                    bid = poly_feat.GetFID()
            else:
                bid = poly_feat.GetFID()

            geom = poly_feat.GetGeometryRef()
            if geom is None:
                continue

            area_m2 = geom.GetArea()
            area_ha  = area_m2 / 10000.0
            lfp      = lfp_lookup.get(bid, {})

            out_feat = ogr.Feature(lyr_defn)
            out_feat.SetGeometry(geom.Clone())
            out_feat.SetField(F_SB_ID,   int(bid))
            out_feat.SetField(F_AREA_M2, round(area_m2, 2))
            out_feat.SetField(F_AREA_HA, round(area_ha, 4))
            out_feat.SetField(F_LFP_LEN, round(lfp.get("LFP_LEN",   -9999.0), 2))
            out_feat.SetField(F_LFP_UP,  round(lfp.get("LFP_UP",    -9999.0), 3))
            out_feat.SetField(F_LFP_DN,  round(lfp.get("LFP_DN",    -9999.0), 3))
            out_feat.SetField(F_LFP_SLP, round(lfp.get("LFP_SLOPE", -9999.0), 4))
            out_feat.SetField(F_SLP_EA,  round(lfp.get("CH_SLP_EA", -9999.0), 4))
            out_lyr.CreateFeature(out_feat)
            written += 1

        out_ds.FlushCache()
        out_ds  = None
        poly_ds = None

        # Cleanup temp shapefile
        if os.path.exists(tmp_poly):
            try:
                shp_drv.DeleteDataSource(tmp_poly)
            except Exception:
                pass

        self._log(f"  [{label}] Written {written} subbasins → {output_shp}", "SUCCESS")
        return True, "OK"

    # ═══════════════════════════════════════════════════════════════
    #  DEM sampling  —  GDAL only, thread-safe
    # ═══════════════════════════════════════════════════════════════

    def _sample_dem_gdal(self, dem_path, ogr_geom, n_samples=150):
        """
        Sample DEM elevations at equally-spaced points along an OGR geometry
        using GDAL ReadRaster — no QGIS API, thread-safe.
        """
        try:
            from osgeo import gdal
            gdal.UseExceptions()
        except ImportError:
            return []

        ds = gdal.Open(dem_path)
        if ds is None:
            return []
        gt   = ds.GetGeoTransform()
        band = ds.GetRasterBand(1)
        nd   = band.GetNoDataValue()
        cols = ds.RasterXSize
        rows = ds.RasterYSize

        # Extract all vertices from the geometry
        pts = []
        if ogr_geom is not None:
            geom_type = ogr_geom.GetGeometryType()
            # Flatten to 2D line/multiline
            if ogr_geom.GetGeometryCount() > 0:
                for i in range(ogr_geom.GetGeometryCount()):
                    sub = ogr_geom.GetGeometryRef(i)
                    for j in range(sub.GetPointCount()):
                        pts.append((sub.GetX(j), sub.GetY(j)))
            else:
                for j in range(ogr_geom.GetPointCount()):
                    pts.append((ogr_geom.GetX(j), ogr_geom.GetY(j)))

        if not pts:
            return []

        # Sub-sample to n_samples evenly from vertex list
        step = max(1, len(pts) // n_samples)
        sample_pts = pts[::step]

        elevs = []
        for x, y in sample_pts:
            # Convert map coords → pixel coords
            px = int((x - gt[0]) / gt[1])
            py = int((y - gt[3]) / gt[5])
            if 0 <= px < cols and 0 <= py < rows:
                val = band.ReadRaster(px, py, 1, 1, buf_type=gdal.GDT_Float32)
                if val:
                    import struct
                    v = struct.unpack("f", val)[0]
                    if nd is None or abs(v - nd) > 1e-6:
                        elevs.append(float(v))

        ds = None
        return elevs

    # ═══════════════════════════════════════════════════════════════
    #  WBT subprocess  —  QThread-safe
    # ═══════════════════════════════════════════════════════════════

    def _run_wbt(self, wbt_exe, args):
        cmd = [wbt_exe] + args
        self._log(f"CMD: {' '.join(cmd)}", "INFO")
        try:
            kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(cmd, **kwargs)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.communicate()
                return False, "WBT timed out (>10 min)."

            stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
            stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""

            for line in stdout.strip().split("\n"):
                if line.strip():
                    self._log(f"  WBT: {line}", "INFO")
            if stderr:
                for line in stderr.strip().split("\n"):
                    if line.strip():
                        self._log(f"  WBT stderr: {line}", "WARNING")

            if proc.returncode != 0:
                return False, f"Exit {proc.returncode}: {stderr}"
            self._log("  → Done.", "SUCCESS")
            return True, "OK"

        except FileNotFoundError:
            return False, f"WBT not found: {wbt_exe}"
        except Exception as e:
            return False, str(e)

    def _resolve_wbt(self, wbt_path=None):
        if wbt_path and os.path.isfile(wbt_path):
            return wbt_path
        exe = "whitebox_tools.exe" if platform.system() == "Windows" else "whitebox_tools"
        found = shutil.which(exe)
        if found:
            return found
        candidates = []
        if platform.system() == "Windows":
            candidates = [
                r"C:\WBT\whitebox_tools.exe",
                r"C:\whitebox_tools\whitebox_tools.exe",
                r"C:\WhiteboxTools_win_amd64\WBT\whitebox_tools.exe",
                os.path.expanduser(r"~\WBT\whitebox_tools.exe"),
                os.path.expanduser(r"~\WhiteboxTools_win_amd64\WBT\whitebox_tools.exe"),
            ]
        else:
            candidates = [
                "/usr/local/bin/whitebox_tools",
                os.path.expanduser("~/WBT/whitebox_tools"),
                "/opt/whitebox_tools/whitebox_tools",
            ]
        try:
            from qgis.core import QgsApplication
            plug = os.path.join(
                QgsApplication.qgisSettingsDirPath(),
                "python", "plugins", "whitebox_for_processing", "WBT", exe)
            candidates.append(plug)
        except Exception:
            pass
        for c in candidates:
            if os.path.isfile(c):
                return c
        self._log("WBT not found — specify path manually.", "WARNING")
        return None

    def _resolve_threshold(self, params, dem_path):
        min_ha = params.get("min_catchment_area_ha", 0)
        if min_ha and min_ha > 0:
            cs = self._get_cell_size_m(dem_path)
            if cs and cs > 0:
                t = max(1, int(min_ha * 10000.0 / (cs * cs)))
                self._log(
                    f"  Min area {min_ha} ha → cell {cs:.2f} m → {t} cells", "INFO")
                return t
        return params.get("channel_threshold", 10000)

    def _get_cell_size_m(self, raster_path):
        try:
            from osgeo import gdal
            ds = gdal.Open(raster_path)
            if ds is None:
                return None
            gt = ds.GetGeoTransform()
            cs = abs(gt[1])
            # Check if degrees (geographic CRS)
            srs_wkt = ds.GetProjection()
            from osgeo import osr
            srs = osr.SpatialReference()
            srs.ImportFromWkt(srs_wkt)
            if srs.IsGeographic():
                cs = cs * 111320.0
            ds = None
            return cs
        except Exception:
            return None

    def _equal_area_slope(self, elevations, length_m):
        if len(elevations) < 5 or length_m <= 0:
            return -9999.0
        s   = sorted(elevations)
        n   = len(s)
        h10 = s[max(0, int(math.floor(0.10 * n)))]
        h85 = s[min(n - 1, int(math.floor(0.85 * n)))]
        return round((h85 - h10) / length_m * 100.0, 4)

    def _find_field(self, field_names, candidates):
        fnu = [n.upper() for n in field_names]
        for c in candidates:
            if c.upper() in fnu:
                return field_names[fnu.index(c.upper())]
        return None

    def _log(self, message, level="INFO"):
        if self.log_callback:
            self.log_callback(message, level)

    def _progress(self, step):
        pct = int(step / self.TOTAL_STEPS * 100)
        if self.progress_callback:
            self.progress_callback(min(pct, 99))

    def _cancelled(self):
        return getattr(self, "cancel_requested", False)

    def _list_outputs(self, output_dir):
        self._log("Output files:", "INFO")
        for f in sorted(os.listdir(output_dir)):
            if f.startswith("_tmp"):
                continue
            fp = os.path.join(output_dir, f)
            self._log(f"  {f:<48} {os.path.getsize(fp)/1024:>8.1f} KB", "INFO")
