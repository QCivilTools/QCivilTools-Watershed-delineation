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

    PHASE1_STEPS = 5
    PHASE2_WBT_STEPS = 6   # steps 6-11
    PHASE2_QGIS_STEPS = 2   # steps 12-13
    TOTAL_STEPS = 13

    def __init__(self):
        self.log_callback = None
        self.progress_callback = None
        self.cancel_requested = False

    # ═══════════════════════════════════════════════════════════════
    #  PHASE 1  —  QThread-safe  (WBT subprocess only)
    # ═══════════════════════════════════════════════════════════════

    def run_phase1(self, params: dict):
        """Steps 1-5. Returns (ok, msg, streams_shp_path)."""
        engine = params.get("engine", "wbt")
        if engine == "grass":
            return self.run_phase1_grass(params)
        if engine == "taudem":
            return self.run_phase1_taudem(params)
        self.cancel_requested = False
        try:
            wbt = self._resolve_wbt(params.get("wbt_path"))
            if wbt is None:
                return False, "WhiteboxTools executable not found.", None

            out = params["output_dir"]
            dem = params["dem_path"]
            os.makedirs(out, exist_ok=True)
            self._delete_phase1_outputs(out)
            self._log(f"WBT: {wbt}", "INFO")
            step = 0

            # Step 1 — Fill Depressions
            if self._cancelled():
                return False, "Cancelled.", None
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
            if not ok:
                return False, f"Step 1 failed: {msg}", None
            self._progress(step)

            # Step 2 — D8 Pointer
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 2/13 — D8 Pointer", "STEP")
            d8_pointer = os.path.join(out, "WBT_D8_Pointer.tif")
            args = ["--run=D8Pointer",
                    f"--dem={filled_dem}", f"--output={d8_pointer}"]
            if params.get("esri_pointer"):
                args.append("--esri_pntr")
            ok, msg = self._run_wbt(wbt, args)
            if not ok:
                return False, f"Step 2 failed: {msg}", None
            self._progress(step)

            # Step 3 — D8 Flow Accumulation
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 3/13 — D8 Flow Accumulation", "STEP")
            d8_accumu = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            args = ["--run=D8FlowAccumulation",
                    f"--dem={filled_dem}", f"--output={d8_accumu}",
                    "--out_type=cells"]
            if params.get("log_transform"):
                args.append("--log")
            ok, msg = self._run_wbt(wbt, args)
            if not ok:
                return False, f"Step 3 failed: {msg}", None
            self._progress(step)

            # Step 4 — Extract Streams
            if self._cancelled():
                return False, "Cancelled.", None
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
            if not ok:
                return False, f"Step 4 failed: {msg}", None
            # Check stream raster actually has data before vectorizing
            if not self._check_raster_has_data(streams_raster):
                max_acc = 0
                try:
                    from osgeo import gdal as _gd
                    import numpy as _np
                    _ds = _gd.Open(os.path.normpath(d8_accumu))
                    if _ds:
                        _arr = _ds.GetRasterBand(1).ReadAsArray()
                        _nd = _ds.GetRasterBand(1).GetNoDataValue()
                        if _nd is not None:
                            _arr = _np.where(_np.abs(_arr.astype(_np.float64) - _nd) < 1, 0, _arr)
                        max_acc = int(_arr.max())
                        _ds = None
                except (RuntimeError, AttributeError):
                    pass  # raster read failed
                return False, (
                    f"Stream extraction produced no stream cells.\n"
                    f"Threshold ({threshold:.0f} cells) may be too high "
                    f"for this DEM.\n"
                    f"Max flow accumulation in DEM: {max_acc} cells.\n"
                    f"Try a lower threshold (e.g. {max(1, max_acc // 100)} cells)."), None
            self._progress(step)

            # Step 5 — Vectorize Streams
            if self._cancelled():
                return False, "Cancelled.", None
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
                self._log(f"  WBT vectorize failed: {msg}", "WARNING")
                self._log("  Falling back to GDAL vectorization…", "INFO")
                streams_shp = self._vectorize_streams_with_d8(
                    streams_raster, d8_pointer, streams_shp)
                if not streams_shp:
                    streams_shp = self._vectorize_streams_gdal(
                        streams_raster, streams_shp)
            else:
                self._log("Stream network ready — place outlet then run Phase 2.",
                          "SUCCESS")
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
        engine = params.get("engine", "wbt")
        if engine == "grass":
            return self.run_phase2_grass(params)
        if engine == "taudem":
            return self.run_phase2_taudem(params)
        self.cancel_requested = False
        try:
            wbt = self._resolve_wbt(params.get("wbt_path"))
            if wbt is None:
                return False, "WhiteboxTools executable not found.", None

            out = params["output_dir"]
            no_outlet = params.get("no_outlet", False)
            self._delete_phase2_outputs(out)
            filled_dem = os.path.join(out, "WBT_Filled_DEM.tif")
            d8_pointer = os.path.join(out, "WBT_D8_Pointer.tif")
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
                if self._cancelled():
                    return False, "Cancelled.", None
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
                    "out": out,
                    "filled_dem": filled_dem,
                    "watershed_raster": None,       # no watershed in no-outlet mode
                    "subbasins_full": subbasins_full,
                    "lfp_ws_shp": None,
                    "lfp_all_shp": lfp_all_shp,
                    "sub_ok": sub_ok,
                    "wbt": wbt,
                    "no_outlet": True,
                }
                return True, "Phase 2 WBT complete (no-outlet mode).", ctx

            # ── NORMAL (with outlet) MODE ──────────────────────────────────
            outlet = params["outlet_path"]

            # Step 6 — Snap Pour Points
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 6/13 — Snap Pour Points (JensonSnapPourPoints)", "STEP")
            outlet_snapped = os.path.join(out, "outlet_snapped.shp")
            ok, msg = self._run_wbt(wbt, [
                "--run=JensonSnapPourPoints",
                f"--pour_pts={outlet}",
                f"--streams={streams_raster}",
                f"--output={outlet_snapped}",
                f"--snap_dist={params.get('snap_distance', 50)}"])
            if not ok:
                return False, f"Step 6 failed: {msg}", None
            self._progress(step)

            # Step 7 — Watershed
            if self._cancelled():
                return False, "Cancelled.", None
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
            if not ok:
                return False, f"Step 7 failed: {msg}", None
            self._progress(step)

            # Step 8 — Vectorize Watershed
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 8/13 — Vectorize Watershed", "STEP")
            watershed_shp = os.path.join(out, "WBT_Watershed_Boundary.shp")
            ok, msg = self._run_wbt(wbt, [
                "--run=RasterToVectorPolygons",
                f"--input={watershed_raster}",
                f"--output={watershed_shp}"])
            if not ok:
                return False, f"Step 8 failed: {msg}", None
            self._progress(step)

            # Step 9 — UnnestBasins (optional)
            step += 1
            if params.get("run_unnest", True):
                if self._cancelled():
                    return False, "Cancelled.", None
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
                if self._cancelled():
                    return False, "Cancelled.", None
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
            if self._cancelled():
                return False, "Cancelled.", None
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

            lfp_ws_shp = None
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
                "out": out,
                "filled_dem": filled_dem,
                "watershed_raster": watershed_raster,
                "subbasins_full": subbasins_full,
                "lfp_ws_shp": lfp_ws_shp,
                "lfp_all_shp": lfp_all_shp,
                "sub_ok": sub_ok,
                "wbt": wbt,
                "no_outlet": False,
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
            out = ctx["out"]
            filled_dem = ctx["filled_dem"]
            watershed_raster = ctx.get("watershed_raster")
            subbasins_full = ctx.get("subbasins_full")
            lfp_ws_shp = ctx.get("lfp_ws_shp")
            lfp_all_shp = ctx.get("lfp_all_shp")
            sub_ok = ctx.get("sub_ok", False)
            wbt = ctx["wbt"]
            no_outlet = ctx.get("no_outlet", False)

            subbasins_raster = os.path.join(out, "WBT_Subbasins.tif")
            subbasins_shp = os.path.join(out, "WBT_Subbasins_Info.shp")
            alldem_shp = os.path.join(out, "WBT_AllDEM_Subbasins.shp")

            # Step 12 — Mask + WBT_Subbasins_Info.shp
            if no_outlet:
                self._log("STEP 12/13 — Skipped (no-outlet mode, no watershed boundary).",
                          "INFO")
            else:
                self._log("STEP 12/13 — Mask subbasins + WBT_Subbasins_Info.shp", "STEP")
                # Build subbasins_raster from best available source
                if os.path.exists(subbasins_raster) and \
                        os.path.getsize(subbasins_raster) > 1000:
                    pass  # already written by _mask_and_label in Phase 2
                elif sub_ok and subbasins_full and os.path.exists(subbasins_full):
                    mask_ok, mask_msg = self._mask_raster_to_watershed(
                        subbasins_full, watershed_raster, subbasins_raster)
                    if not mask_ok:
                        self._log(f"  Mask warning: {mask_msg}", "WARNING")
                        shutil.copy2(subbasins_full, subbasins_raster)
                else:
                    # No GRASS basin raster — use watershed mask as single basin
                    self._log("  No basin raster — using watershed as basin 1.", "INFO")
                    try:
                        from osgeo import gdal as _gd12
                        import numpy as _np12
                        _ws_ds = _gd12.Open(os.path.normpath(watershed_raster))
                        if _ws_ds:
                            _ws_arr = _ws_ds.GetRasterBand(1).ReadAsArray()
                            _gt12, _proj12 = _ws_ds.GetGeoTransform(), _ws_ds.GetProjection()
                            _ws_ds = None
                            _sub = _np12.where(_ws_arr > 0, 1, 0).astype(_np12.int32)
                            _d = _gd12.GetDriverByName("GTiff")
                            _o = _d.Create(os.path.normpath(subbasins_raster),
                                           _sub.shape[1], _sub.shape[0], 1, _gd12.GDT_Int32)
                            _o.SetGeoTransform(_gt12); _o.SetProjection(_proj12)
                            _o.GetRasterBand(1).SetNoDataValue(0)
                            _o.GetRasterBand(1).WriteArray(_sub)
                            _o.FlushCache(); _o = None
                    except Exception as _e12:
                        self._log(f"  Single-basin error: {_e12}", "WARNING")

                if os.path.exists(subbasins_raster) and \
                        os.path.getsize(subbasins_raster) > 1000:
                    # Compute per-subbasin LFP (same as WBT LongestFlowpath --basins)
                    d8_accumu_path = os.path.join(out, "WBT_D8_FlowAccumu.tif")
                    _lfp_ws_per_sb = os.path.join(out, "_tmp_lfp_ws_persub.shp")
                    _lfp_ws_for_info = lfp_ws_shp  # default: whole-watershed LFP
                    if os.path.exists(d8_accumu_path):
                        self._log("  Computing per-subbasin LFP for Subbasins Info…",
                                  "INFO")
                        if self._compute_lfp_per_subbasin(
                                d8_accumu_path, subbasins_raster, _lfp_ws_per_sb):
                            _lfp_ws_for_info = _lfp_ws_per_sb
                    ok2, msg2 = self._build_subbasins_info(
                        wbt=wbt, filled_dem=filled_dem,
                        subbasins_raster=subbasins_raster,
                        lfp_shp_path=_lfp_ws_for_info,
                        output_shp=subbasins_shp,
                        output_dir=out, label="Watershed")
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
            alldem_src = None
            if subbasins_full and os.path.exists(subbasins_full)                     and os.path.getsize(subbasins_full) > 1000:
                alldem_src = subbasins_full
            elif os.path.exists(subbasins_raster)                     and os.path.getsize(subbasins_raster) > 1000:
                alldem_src = subbasins_raster
            if alldem_src:
                # Use best available LFP — lfp_all_shp preferred, else lfp_ws_shp
                _lfp_for_alldem = lfp_all_shp or lfp_ws_shp
                ok3, msg3 = self._build_subbasins_info(
                    wbt=wbt,
                    filled_dem=filled_dem,
                    subbasins_raster=alldem_src,
                    lfp_shp_path=_lfp_for_alldem,
                    output_shp=alldem_shp,
                    output_dir=out,
                    label="AllDEM")
                if ok3:
                    self._log("WBT_AllDEM_Subbasins.shp done.", "SUCCESS")
                else:
                    self._log(f"WBT_AllDEM_Subbasins.shp warning: {msg3}", "WARNING")
            else:
                self._log("STEP 13/13 — Skipped (no subbasin raster).", "WARNING")
            self._progress(13)

            # Cleanup temp rasters
            for tmp in [subbasins_full,
                        os.path.join(out, "_tmp_lfp_alldem.shp")]:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
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
            from osgeo import gdal, ogr
            gdal.UseExceptions()
        except ImportError:
            return False, "GDAL not available"

        import uuid

        # ── Read original watershed polygons ──────────────────────────────────
        ws_ds = ogr.Open(ws_shp)
        if ws_ds is None:
            return False, f"Cannot open {ws_shp}"
        ws_lyr = ws_ds.GetLayer(0)
        ws_defn = ws_lyr.GetLayerDefn()
        srs = ws_lyr.GetSpatialRef()

        # ── Read LFP attributes (whole watershed LFP) ─────────────────────────
        lfp_data = {}   # {basin_id: dict} — basin_id from BASIN field
        if lfp_shp and os.path.exists(lfp_shp):
            lfp_ds = ogr.Open(lfp_shp)
            if lfp_ds:
                lfp_lyr = lfp_ds.GetLayer(0)
                lfp_defn = lfp_lyr.GetLayerDefn()
                lfp_fnu = [lfp_defn.GetFieldDefn(i).GetNameRef().upper()
                           for i in range(lfp_defn.GetFieldCount())]

                def _fv(feat, cands):
                    for c in cands:
                        if c.upper() in lfp_fnu:
                            fn = lfp_defn.GetFieldDefn(
                                lfp_fnu.index(c.upper())).GetNameRef()
                            try:
                                return float(feat.GetField(fn))
                            except (TypeError, ValueError, KeyError):
                                pass  # field value not numeric
                    return -9999.0

                lfp_lyr.ResetReading()
                for lf in lfp_lyr:
                    bid = -1
                    for c in ["BASIN", "basin", "VALUE", "value"]:
                        if c.upper() in lfp_fnu:
                            fn = lfp_defn.GetFieldDefn(
                                lfp_fnu.index(c.upper())).GetNameRef()
                            try:
                                bid = int(lf.GetField(fn))
                                break
                            except (TypeError, ValueError, KeyError):
                                pass  # field access failed
                    geom = lf.GetGeometryRef()
                    raw_len = _fv(lf, ["LENGTH", "length"])
                    raw_up = _fv(lf, ["UP_ELEV", "up_elev"])
                    raw_dn = _fv(lf, ["DN_ELEV", "dn_elev"])
                    raw_slp = _fv(lf, ["AVG_SLOPE", "avg_slope"])
                    elevs = self._sample_dem_gdal(filled_dem, geom, 200)
                    if elevs and len(elevs) >= 5:
                        up_elv = max(elevs)
                        dn_elv = min(elevs)
                        lfp_len = raw_len if raw_len > 0 else (geom.Length() if geom else -9999.0)
                        avg_slp = ((up_elv - dn_elv) / lfp_len * 100.0) if lfp_len > 0 else -9999.0
                        ea_slp = self._equal_area_slope(elevs, lfp_len)
                    else:
                        up_elv = raw_up
                        dn_elv = raw_dn
                        lfp_len = raw_len
                        avg_slp = raw_slp
                        ea_slp = raw_slp
                    # Use bid=-1 as "whole watershed" if single outlet
                    lfp_data[bid] = dict(
                        LEN=lfp_len, UP=up_elv, DN=dn_elv,
                        SLP=avg_slp, EA=ea_slp)
                lfp_ds = None

        # ── Write enriched shapefile to temp, then replace original ───────────
        gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")
        tmp_out = os.path.join(output_dir, f"_tmp_ws_enrich_{uuid.uuid4().hex[:6]}.shp")
        drv = ogr.GetDriverByName("ESRI Shapefile")
        if os.path.exists(tmp_out):
            drv.DeleteDataSource(tmp_out)
        out_ds = drv.CreateDataSource(tmp_out)
        out_lyr = out_ds.CreateLayer(
            "ws_enrich",
            srs=srs, geom_type=ogr.wkbPolygon)

        # Copy existing fields from original ws shapefile
        for i in range(ws_defn.GetFieldCount()):
            out_lyr.CreateField(ws_defn.GetFieldDefn(i))
        n_orig = ws_defn.GetFieldCount()

        # New fields — max 8 chars, positional
        new_fields = [
            ("WS_AR_M2", ogr.OFTReal, 20, 2),
            ("WS_AR_HA", ogr.OFTReal, 20, 4),
            ("LFP_LEN", ogr.OFTReal, 20, 2),
            ("LFP_UP", ogr.OFTReal, 20, 3),
            ("LFP_DN", ogr.OFTReal, 20, 3),
            ("LFP_SLP", ogr.OFTReal, 20, 4),
            ("SLP_EA", ogr.OFTReal, 20, 4),
        ]
        for fname, ftype, width, prec in new_fields:
            fd = ogr.FieldDefn(fname, ftype)
            fd.SetWidth(width)
            fd.SetPrecision(prec)
            out_lyr.CreateField(fd)

        # Positional indices for new fields
        I_AR_M2 = n_orig + 0
        I_AR_HA = n_orig + 1
        I_LEN = n_orig + 2
        I_UP = n_orig + 3
        I_DN = n_orig + 4
        I_SLP = n_orig + 5
        I_EA = n_orig + 6

        out_defn = out_lyr.GetLayerDefn()
        ws_lyr.ResetReading()
        for ws_feat in ws_lyr:
            geom = ws_feat.GetGeometryRef()
            area_m2 = geom.GetArea() if geom else 0.0
            area_ha = area_m2 / 10000.0

            # Find matching LFP entry
            # Try by VALUE field first, then fall back to first entry
            bid = -1
            for c in ["VALUE", "value", "BASIN", "basin"]:
                idx = ws_defn.GetFieldIndex(c)
                if idx >= 0:
                    try:
                        bid = int(ws_feat.GetField(idx))
                        break
                    except (RuntimeError, AttributeError):
                        pass  # snap attempt failed

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
            nf.SetField(I_LEN, round(lfp.get("LEN", -9999.0), 2))
            nf.SetField(I_UP, round(lfp.get("UP", -9999.0), 3))
            nf.SetField(I_DN, round(lfp.get("DN", -9999.0), 3))
            nf.SetField(I_SLP, round(lfp.get("SLP", -9999.0), 4))
            nf.SetField(I_EA, round(lfp.get("EA", -9999.0), 4))
            out_lyr.CreateFeature(nf)

        out_ds.FlushCache()
        out_ds = None
        ws_ds = None

        # Replace original with enriched version
        drv.DeleteDataSource(ws_shp)
        # Move all component files
        tmp_base = os.path.splitext(tmp_out)[0]
        ws_base = os.path.splitext(ws_shp)[0]
        for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj"]:
            src = tmp_base + ext
            dst = ws_base + ext
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
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
            sub_ds = gdal.Open(subbasins_raster, gdal.GA_ReadOnly)
            if sub_ds is None:
                return False, f"Cannot open {subbasins_raster}"
            sub_band = sub_ds.GetRasterBand(1)
            sub_arr = sub_band.ReadAsArray().astype(np.float64)
            gt = sub_ds.GetGeoTransform()
            proj = sub_ds.GetProjection()
            cols = sub_ds.RasterXSize
            rows = sub_ds.RasterYSize
            sub_ds = None

            # Open watershed raster — may have different resolution/extent
            ws_ds = gdal.Open(watershed_raster, gdal.GA_ReadOnly)
            if ws_ds is None:
                return False, f"Cannot open {watershed_raster}"
            ws_band = ws_ds.GetRasterBand(1)
            ws_nd = ws_band.GetNoDataValue()

            # Resample watershed to match subbasin grid if needed
            ws_cols = ws_ds.RasterXSize
            ws_rows = ws_ds.RasterYSize
            ws_gt = ws_ds.GetGeoTransform()

            if ws_cols != cols or ws_rows != rows or ws_gt != gt:
                # Warp watershed to subbasin grid using gdal.Warp
                mem_drv = gdal.GetDriverByName("MEM")
                ws_mem = mem_drv.Create("", cols, rows, 1, gdal.GDT_Float32)
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

            # Apply mask: outside watershed → 0 (nodata for Int32)
            OUT_ND = 0
            result = np.where(ws_mask, sub_arr, OUT_ND).astype(np.int32)

            # Write output GeoTIFF as Int32 so GDAL Polygonize works correctly
            drv = gdal.GetDriverByName("GTiff")
            out_ds = drv.Create(output_raster, cols, rows, 1, gdal.GDT_Int32,
                                options=["COMPRESS=LZW", "TILED=YES"])
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            out_band = out_ds.GetRasterBand(1)
            out_band.SetNoDataValue(OUT_ND)
            out_band.WriteArray(result)
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

        import uuid

        self._log(f"  [{label}] Polygonizing subbasin raster (GDAL)…", "INFO")

        # ── (a) Polygonize with gdal.Polygonize into OGR Memory layer ───────
        src_ds = gdal.Open(subbasins_raster, gdal.GA_ReadOnly)
        if src_ds is None:
            return False, f"Cannot open {subbasins_raster}"
        src_band = src_ds.GetRasterBand(1)
        proj_wkt = src_ds.GetProjection()

        srs = osr.SpatialReference()
        srs.ImportFromWkt(proj_wkt)

        # Set SHAPE_RESTORE_SHX so GDAL recreates missing .shx if needed
        gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

        tmp_poly = os.path.join(output_dir, f"_tmp_{label}_{uuid.uuid4().hex[:6]}.shp")
        shp_drv = ogr.GetDriverByName("ESRI Shapefile")
        if os.path.exists(tmp_poly):
            shp_drv.DeleteDataSource(tmp_poly)
        tmp_ds = shp_drv.CreateDataSource(tmp_poly)
        tmp_lyr = tmp_ds.CreateLayer(
            "polys",
            srs=srs, geom_type=ogr.wkbPolygon)
        fd = ogr.FieldDefn("VALUE", ogr.OFTInteger)
        fd.SetWidth(10)
        tmp_lyr.CreateField(fd)

        # Use src_band as mask — only polygonize non-nodata cells
        gdal.Polygonize(src_band, src_band, tmp_lyr, 0, [], callback=None)
        tmp_ds.FlushCache()
        tmp_ds = None
        src_ds = None

        # Re-open for reading
        poly_ds = ogr.Open(tmp_poly)
        if poly_ds is None:
            return False, f"Cannot open polygonized result: {tmp_poly}"
        poly_lyr = poly_ds.GetLayer(0)
        poly_defn = poly_lyr.GetLayerDefn()

        poly_field_names = [poly_defn.GetFieldDefn(i).GetNameRef()
                            for i in range(poly_defn.GetFieldCount())]
        val_field = None
        for c in ["VALUE", "value", "FID", "fid"]:
            if c in poly_field_names:
                val_field = c
                break

        # ── (b) Build LFP feature list for spatial matching ───────────────────
        # LFP IDs don't match subbasin IDs (TauDEM/GRASS use different numbering)
        # so we use spatial intersection: each subbasin gets the LFP that passes
        # through it (or the closest one).
        lfp_features = []   # list of (geom, attr_dict)
        lfp_lookup = {}     # bid -> attr_dict (for WBT ID-match fallback)
        if lfp_shp_path and os.path.exists(lfp_shp_path):
            self._log(f"  [{label}] Loading LFP for spatial matching…", "INFO")
            lfp_ds = ogr.Open(lfp_shp_path)
            if lfp_ds:
                lfp_lyr = lfp_ds.GetLayer(0)
                lfp_defn = lfp_lyr.GetLayerDefn()
                lfp_fnu = [lfp_defn.GetFieldDefn(i).GetNameRef().upper()
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
                    for c in ["BASIN", "OUTLET_ID", "basin", "VALUE", "value"]:
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
                    geom = lf.GetGeometryRef()
                    if geom is None:
                        continue
                    geom = geom.Clone()
                    raw_len = _fval(lf, ["LENGTH", "length"])
                    raw_up  = _fval(lf, ["UP_ELEV", "up_elev"])
                    raw_dn  = _fval(lf, ["DN_ELEV", "dn_elev"])
                    raw_slp = _fval(lf, ["AVG_SLOPE", "avg_slope"])
                    elevs = self._sample_dem_gdal(filled_dem, geom, 150)
                    if elevs and len(elevs) >= 5:
                        up_elv  = max(elevs)
                        dn_elv  = min(elevs)
                        lfp_len = raw_len if raw_len > 0 else geom.Length()
                        avg_slp = ((up_elv - dn_elv) / lfp_len * 100.0
                                   if lfp_len > 0 else -9999.0)
                        ea_slp  = self._equal_area_slope(elevs, lfp_len)
                    else:
                        up_elv  = raw_up
                        dn_elv  = raw_dn
                        lfp_len = raw_len
                        avg_slp = raw_slp
                        ea_slp  = raw_slp
                    attrs = dict(LFP_LEN=lfp_len, LFP_UP=up_elv,
                                 LFP_DN=dn_elv, LFP_SLOPE=avg_slp,
                                 CH_SLP_EA=ea_slp)
                    lfp_features.append((geom, attrs))
                    bid = _bid(lf)
                    lfp_lookup[bid] = attrs
                lfp_ds = None
            self._log(f"  [{label}] {len(lfp_features)} LFP feature(s) loaded.",
                      "INFO")

        def _get_lfp_for_geom(poly_geom, bid):
            """
            Match subbasin to its LFP strictly by BASIN ID.
            Per-subbasin LFP (from _compute_lfp_per_subbasin) has BASIN = basin
            raster value, so ID match is exact and correct.
            Spatial fallback is intentionally removed — it causes neighboring
            basins to inherit each other's LFP values incorrectly.
            Basins too small to trace LFP correctly show -9999.
            """
            if not lfp_features:
                return {}
            # Strict ID match only
            return lfp_lookup.get(bid, {})

        # ── (c) Write output shapefile ────────────────────────────────────────
        # GDAL ESRI Shapefile driver: CreateDataSource() needs the DIRECTORY,
        # not the full .shp path — passing a .shp path raises
        # "is not a directory" on Windows GDAL builds.
        self._log(f"  [{label}] Writing {os.path.basename(output_shp)}…", "INFO")
        out_drv = ogr.GetDriverByName("ESRI Shapefile")
        shp_dir = os.path.dirname(output_shp)
        shp_basename = os.path.splitext(os.path.basename(output_shp))[0]
        if os.path.exists(output_shp):
            out_drv.DeleteDataSource(output_shp)
        out_ds = out_drv.CreateDataSource(shp_dir)
        out_lyr = out_ds.CreateLayer(shp_basename, srs=srs, geom_type=ogr.wkbPolygon)

        # Field names MUST be ≤8 chars — DBF spec says 10 but many GDAL/Windows
        # builds silently truncate to 8, causing the KeyError on lookup.
        # Using positional indices (0,1,2…) to set values — immune to name issues.
        field_defs = [
            # (name_max8, OGR_type,      width, precision)
            ("SB_ID", ogr.OFTInteger, 10, 0),
            ("AREA_M2", ogr.OFTReal, 20, 2),
            ("AREA_HA", ogr.OFTReal, 20, 4),
            ("LFP_LEN", ogr.OFTReal, 20, 2),   # LFP length (m)
            ("LFP_UP", ogr.OFTReal, 20, 3),   # upstream elev
            ("LFP_DN", ogr.OFTReal, 20, 3),   # downstream elev
            ("LFP_SLP", ogr.OFTReal, 20, 4),   # simple slope %
            ("SLP_EA", ogr.OFTReal, 20, 4),   # equal-area slope %
        ]
        for fname, ftype, width, prec in field_defs:
            fd = ogr.FieldDefn(fname, ftype)
            fd.SetWidth(width)
            if prec:
                fd.SetPrecision(prec)
            out_lyr.CreateField(fd)

        # Use positional indices 0..N — never look up by name
        F_SB_ID = 0
        F_AREA_M2 = 1
        F_AREA_HA = 2
        F_LFP_LEN = 3
        F_LFP_UP = 4
        F_LFP_DN = 5
        F_LFP_SLP = 6
        F_SLP_EA = 7

        lyr_defn = out_lyr.GetLayerDefn()
        written = 0

        # Group polygons by basin ID — merge disconnected slivers of same basin
        from collections import defaultdict as _dd
        basin_geoms  = _dd(list)   # bid -> [geom, ...]
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
            if geom is not None:
                basin_geoms[bid].append(geom.Clone())

        for bid, geom_list in basin_geoms.items():
            # Union all parts of this basin into one geometry
            if len(geom_list) == 1:
                merged_geom = geom_list[0]
            else:
                merged_geom = geom_list[0]
                for g in geom_list[1:]:
                    try:
                        merged_geom = merged_geom.Union(g)
                    except (RuntimeError, AttributeError, TypeError):
                        pass  # OGR geometry union may fail on invalid geometries

            if merged_geom is None:
                continue

            area_m2 = merged_geom.GetArea()
            area_ha = area_m2 / 10000.0
            lfp = _get_lfp_for_geom(merged_geom, bid)

            out_feat = ogr.Feature(lyr_defn)
            out_feat.SetGeometry(merged_geom)
            out_feat.SetField(F_SB_ID, int(bid))
            out_feat.SetField(F_AREA_M2, round(area_m2, 2))
            out_feat.SetField(F_AREA_HA, round(area_ha, 4))
            out_feat.SetField(F_LFP_LEN, round(lfp.get("LFP_LEN", -9999.0), 2))
            out_feat.SetField(F_LFP_UP, round(lfp.get("LFP_UP", -9999.0), 3))
            out_feat.SetField(F_LFP_DN, round(lfp.get("LFP_DN", -9999.0), 3))
            out_feat.SetField(F_LFP_SLP, round(lfp.get("LFP_SLOPE", -9999.0), 4))
            out_feat.SetField(F_SLP_EA, round(lfp.get("CH_SLP_EA", -9999.0), 4))
            out_lyr.CreateFeature(out_feat)
            written += 1

        out_ds.FlushCache()
        out_ds = None
        poly_ds = None

        # Cleanup temp shapefile
        if os.path.exists(tmp_poly):
            try:
                shp_drv.DeleteDataSource(tmp_poly)
            except (RuntimeError, Exception):  # noqa: BLE001
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
        gt = ds.GetGeoTransform()
        band = ds.GetRasterBand(1)
        nd = band.GetNoDataValue()
        cols = ds.RasterXSize
        rows = ds.RasterYSize

        # Extract all vertices from the geometry
        pts = []
        if ogr_geom is not None:
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

    # ═══════════════════════════════════════════════════════════════
    #  SAGA ENGINE  — Phase 1 and Phase 2
    #  Uses QGIS Processing SAGA provider (saga:* algorithms)
    #  SAGA is bundled with QGIS and is far more reliable than GRASS
    #  for watershed delineation on Windows.
    # ═══════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════
    #  TauDEM engine  — subprocess via mpiexec (no QGIS provider needed)
    # ═══════════════════════════════════════════════════════════════

    def _resolve_taudem(self, taudem_path=None):
        """
        Locate TauDEM executables folder.
        Priority: UI field → QGIS settings → common paths → PATH.
        """
        import shutil

        def _has_exe(folder):
            if not folder:
                return False
            folder = os.path.normpath(str(folder).strip())
            found = (os.path.exists(os.path.join(folder, "pitremove.exe")) or
                     os.path.exists(os.path.join(folder, "pitremove")))
            self._log(
                f"  TauDEM check: {folder} → {'✔ FOUND' if found else '✘ not found'}",
                "INFO")
            return found

        # 1. Explicit UI path
        if taudem_path and taudem_path.strip():
            folder = os.path.normpath(taudem_path.strip())
            if _has_exe(folder):
                self._log(f"  TauDEM: using UI path: {folder}", "INFO")
                return folder
            self._log(
                f"  TauDEM: UI path '{folder}' set but pitremove.exe not found there.",
                "WARNING")

        # 2. QGIS Processing settings
        try:
            from qgis.core import QgsSettings
            for key in ["Processing/TauDEM/folder",
                        "Processing/TauDEM/taudemfolder",
                        "Processing/TauDEM/TAUDEM_FOLDER"]:
                val = QgsSettings().value(key, "")
                if val and _has_exe(str(val)):
                    return os.path.normpath(str(val))
        except Exception as _e:
            self._log(f"  QGIS settings check: {_e}", "INFO")

        # 3. Common install locations
        for folder in [
            r"C:\Program Files\TauDEM\TauDEM5Exe",
            r"C:\Program Files (x86)\TauDEM\TauDEM5Exe",
            r"D:\Program Files\TauDEM\TauDEM5Exe",
            r"C:\TauDEM\TauDEM5Exe",
            r"D:\TauDEM\TauDEM5Exe",
            r"C:\TauDEM",
        ]:
            if _has_exe(folder):
                return folder

        # 4. PATH
        hit = shutil.which("pitremove") or shutil.which("pitremove.exe")
        if hit:
            return os.path.dirname(hit)

        return None

    def _run_taudem(self, taudem_folder, tool, args, mpiexec_path=None):
        """Run a TauDEM tool via mpiexec subprocess."""
        import subprocess, shutil
        ncores = max(1, (os.cpu_count() or 4) // 2)

        # Resolve TauDEM exe
        if taudem_folder:
            exe = os.path.join(taudem_folder, tool + ".exe")
            if not os.path.exists(exe):
                exe = os.path.join(taudem_folder, tool)
        else:
            exe = tool

        # Resolve mpiexec — priority: user field → PATH → common locations
        mpiexec = None
        if mpiexec_path and mpiexec_path.strip():
            p = os.path.normpath(mpiexec_path.strip())
            if os.path.exists(p):
                mpiexec = p
                self._log(f"  mpiexec: {mpiexec}", "INFO")
            else:
                self._log(f"  mpiexec path set but not found: {p}", "WARNING")
        if not mpiexec:
            mpiexec = shutil.which("mpiexec") or shutil.which("mpiexec.exe")
        if not mpiexec:
            for c in [
                r"C:\Program Files\Microsoft MPI\Bin\mpiexec.exe",
                r"C:\Program Files (x86)\Microsoft MPI\Bin\mpiexec.exe",
                r"C:\Program Files\MPICH2\bin\mpiexec.exe",
            ]:
                if os.path.exists(c):
                    mpiexec = c
                    self._log(f"  mpiexec: {c}", "INFO")
                    break
        if not mpiexec:
            return False, (
                "mpiexec not found. Set the mpiexec path in the TauDEM "
                "engine settings, or install MS-MPI.")

        cmd = [mpiexec, "-n", str(ncores), exe] + args
        # Validate all cmd elements are strings (no shell injection via list)
        cmd = [str(c) for c in cmd]
        self._log(f"  TauDEM: {tool} (cores={ncores})", "INFO")
        try:
            # CREATE_NO_WINDOW suppresses cmd popup on Windows
            _flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                _flags = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(  # nosec B603 - cmd is a validated list, shell=False
                cmd, capture_output=True, text=True, timeout=3600,
                creationflags=_flags)
            for line in ((result.stdout or "") + (result.stderr or "")).splitlines():
                line = line.strip()
                if not line:
                    continue
                # Filter harmless GDAL plugin load errors (DLL not found)
                if "Can't load requested DLL" in line or \
                   "The specified procedure could not be found" in line or \
                   "127: The specified" in line:
                    continue
                self._log(f"  {line}", "INFO")
            if result.returncode != 0:
                return False, f"{tool} exit={result.returncode}"
            return True, "OK"
        except FileNotFoundError as exc:
            return False, f"Executable not found: {exc}"
        except subprocess.TimeoutExpired:
            return False, f"{tool} timed out"
        except Exception as exc:
            return False, str(exc)

    def run_phase1_taudem(self, params):
        """
        Phase 1 using TauDEM executables via mpiexec.
        Matches WBT steps 1-5. D8 encoding = WBT powers-of-2.
        Tool names match the actual .exe files in TauDEM5Exe folder.
        """
        self.cancel_requested = False
        try:
            taudem = self._resolve_taudem(params.get("taudem_path"))
            mpiexec_path = params.get("mpiexec_path", "")
            if taudem is None:
                return False, (
                    "TauDEM executables not found.\n"
                    "Set the TauDEM folder in the plugin (engine settings) "
                    "or install TauDEM to C:\\Program Files\\TauDEM\\TauDEM5Exe."), None

            out = params["output_dir"]
            dem = params["dem_path"]
            os.makedirs(out, exist_ok=True)
            self._delete_phase1_outputs(out)
            self._log(f"TauDEM: {taudem or '(PATH)'}", "INFO")

            filled_dem     = os.path.join(out, "WBT_Filled_DEM.tif")
            d8_pointer     = os.path.join(out, "WBT_D8_Pointer.tif")
            d8_slopes      = os.path.join(out, "_tmp_d8_slopes.tif")
            d8_accumu      = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            streams_raster = os.path.join(out, "WBT_ExtractStreams.tif")
            streams_shp    = os.path.join(out, "WBT_ExtractStreams_vector.shp")

            threshold     = self._resolve_threshold(params, dem)
            threshold_int = max(1, int(threshold))
            self._log(f"  Threshold: {threshold_int} cells", "INFO")

            # Step 1 — pitremove
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 1/5 (TauDEM) — pitremove", "STEP")
            ok, msg = self._run_taudem(taudem, "pitremove",
                ["-z", dem, "-fel", filled_dem], mpiexec_path)
            if not ok or not os.path.exists(filled_dem):
                return False, f"pitremove failed: {msg}", None
            self._progress(1)

            # Step 2 — d8flowdir
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 2/5 (TauDEM) — d8flowdir", "STEP")
            ok, msg = self._run_taudem(taudem, "d8flowdir",
                ["-fel", filled_dem, "-p", d8_pointer, "-sd8", d8_slopes], mpiexec_path)
            if not ok or not os.path.exists(d8_pointer):
                return False, f"d8flowdir failed: {msg}", None
            self._progress(2)

            # Step 3 — aread8
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 3/5 (TauDEM) — aread8", "STEP")
            ok, msg = self._run_taudem(taudem, "aread8",
                ["-p", d8_pointer, "-ad8", d8_accumu, "-nc"], mpiexec_path)
            if not ok or not os.path.exists(d8_accumu):
                return False, f"aread8 failed: {msg}", None
            self._progress(3)

            # Step 4 — threshold
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 4/5 (TauDEM) — threshold", "STEP")
            ok, msg = self._run_taudem(taudem, "threshold",
                ["-ssa", d8_accumu, "-src", streams_raster,
                 "-thresh", str(float(threshold_int))], mpiexec_path)
            if not ok or not os.path.exists(streams_raster):
                return False, f"threshold failed: {msg}", None
            if not self._check_raster_has_data(streams_raster):
                return False, (
                    f"Stream extraction produced no cells. "
                    f"Threshold ({threshold_int} cells) is too high. "
                    "Try a lower threshold."), None
            self._progress(4)

            # Step 5 — vectorize streams using D8 pointer for proper lines
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 5/5 (TauDEM) — vectorize streams", "STEP")
            streams_shp = self._vectorize_streams_with_d8(
                streams_raster, d8_pointer, streams_shp)
            if not streams_shp:
                # Fallback
                streams_shp = self._vectorize_streams_gdal(streams_raster, streams_shp)
            if streams_shp:
                self._log(
                    "Stream network ready — place outlet then run Phase 2.", "SUCCESS")
            else:
                self._log(
                    "Stream raster ready. Load WBT_ExtractStreams.tif instead.",
                    "WARNING")
            self._progress(5)

            for f in [d8_slopes]:
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except OSError:
                    pass

            return True, "Phase 1 complete (TauDEM).", streams_shp

        except Exception:
            import traceback
            self._log(traceback.format_exc(), "ERROR")
            return False, "Phase 1 TauDEM error — see Log tab.", None

    # ═══════════════════════════════════════════════════════════════
    #  PHASE 2 TauDEM
    # ═══════════════════════════════════════════════════════════════

    def run_phase2_taudem(self, params):
        """
        Phase 2 using TauDEM executables via mpiexec.
        Matches WBT steps 6-11. D8 powers-of-2 encoding = same as WBT,
        so run_phase2_qgis() reuses unchanged.
        """
        self.cancel_requested = False
        try:
            taudem = self._resolve_taudem(params.get("taudem_path"))
            mpiexec_path = params.get("mpiexec_path", "")
            if taudem is None:
                return False, "TauDEM executables not found.", None

            out = params["output_dir"]
            no_outlet = params.get("no_outlet", False)
            self._delete_phase2_outputs(out)
            self._log(f"TauDEM: {taudem or '(PATH)'}", "INFO")

            filled_dem     = os.path.join(out, "WBT_Filled_DEM.tif")
            d8_pointer     = os.path.join(out, "WBT_D8_Pointer.tif")
            d8_accumu      = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            streams_raster = os.path.join(out, "WBT_ExtractStreams.tif")

            for f, n in [(filled_dem, "WBT_Filled_DEM.tif"),
                         (d8_pointer, "WBT_D8_Pointer.tif"),
                         (streams_raster, "WBT_ExtractStreams.tif")]:
                if not os.path.exists(f):
                    return False, (
                        f"Phase 1 output '{n}' not found. Run Phase 1 first."), None

            step = self.PHASE1_STEPS

            # ── NO OUTLET MODE ─────────────────────────────────────────────
            if no_outlet:
                self._log("TauDEM Phase 2: NO-OUTLET mode.", "INFO")
                step += 6
                self._log("STEP 11/13 (TauDEM) — streamnet (full DEM subbasins)", "STEP")
                subbasins_full = os.path.join(out, "_tmp_Subbasins_full.tif")
                net_shp   = os.path.join(out, "_tmp_net.shp")
                tree_f    = os.path.join(out, "_tmp_tree.dat")
                coord_f   = os.path.join(out, "_tmp_coord.dat")
                ord_f     = os.path.join(out, "_tmp_ord.tif")
                ok, msg = self._run_taudem(taudem, "streamnet", [
                    "-fel", filled_dem, "-p", d8_pointer,
                    "-ad8", d8_accumu, "-src", streams_raster,
                    "-ord", ord_f, "-tree", tree_f,
                    "-coord", coord_f, "-net", net_shp,
                    "-w", subbasins_full], mpiexec_path)
                sub_ok = ok and self._check_raster_has_data(subbasins_full)
                if not sub_ok:
                    self._log(f"  streamnet warning: {msg}", "WARNING")
                    subbasins_full = None
                self._progress(step)
                _lfp_no_path = os.path.join(out, "_tmp_lfp_alldem.shp")
                lfp_no_shp = None
                if sub_ok and subbasins_full and os.path.exists(subbasins_full):
                    if self._compute_lfp_per_subbasin(
                            d8_accumu, subbasins_full, _lfp_no_path):
                        lfp_no_shp = _lfp_no_path
                ctx = {
                    "out": out, "filled_dem": filled_dem,
                    "watershed_raster": None,
                    "subbasins_full": subbasins_full,
                    "lfp_ws_shp": None, "lfp_all_shp": lfp_no_shp,
                    "sub_ok": sub_ok, "wbt": None,
                    "no_outlet": True, "engine": "taudem",
                }
                return True, "Phase 2 TauDEM complete (no-outlet).", ctx

            # ── WITH OUTLET ────────────────────────────────────────────────
            outlet = params["outlet_path"]
            snap_dist = params.get("snap_distance", 50)

            # Step 6 — moveoutletstostreams
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 6/13 (TauDEM) — moveoutletstostreams", "STEP")
            outlet_snapped = os.path.join(out, "outlet_snapped.shp")
            ok, msg = self._run_taudem(taudem, "moveoutletstostreams", [
                "-p", d8_pointer, "-src", streams_raster,
                "-o", outlet, "-om", outlet_snapped], mpiexec_path)
            if not ok or not os.path.exists(outlet_snapped):
                self._log(f"  moveoutletstostreams failed: {msg} — GDAL snap fallback",
                          "WARNING")
                ok_s = self._snap_outlet_to_stream_gdal(
                    outlet, streams_raster, outlet_snapped, snap_dist)
                if not ok_s:
                    import shutil as _sh
                    _sh.copy2(outlet, outlet_snapped)
            self._progress(step)

            # Step 7 — gagewatershed
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 7/13 (TauDEM) — gagewatershed", "STEP")
            watershed_raster = os.path.join(out, "WBT_Watershed.tif")
            ok, msg = self._run_taudem(taudem, "gagewatershed", [
                "-p", d8_pointer, "-o", outlet_snapped,
                "-gw", watershed_raster], mpiexec_path)
            if not ok or not os.path.exists(watershed_raster):
                return False, f"gagewatershed failed: {msg}", None
            self._progress(step)

            # Step 8 — vectorize watershed (GDAL)
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 8/13 (TauDEM) — vectorize watershed (GDAL)", "STEP")
            watershed_shp = os.path.join(out, "WBT_Watershed_Boundary.shp")
            # gagewatershed labels each outlet's cells with outlet FID
            # polygonize each unique value as a separate polygon
            try:
                import numpy as _np8
                from osgeo import gdal as _gd8, ogr as _og8, osr as _os8
                _ds8 = _gd8.Open(os.path.normpath(watershed_raster))
                _arr8 = _ds8.GetRasterBand(1).ReadAsArray()
                _nd8  = _ds8.GetRasterBand(1).GetNoDataValue()
                _gt8  = _ds8.GetGeoTransform()
                _pr8  = _ds8.GetProjection()
                _ds8  = None
                _ids  = _np8.unique(_arr8)
                if _nd8 is not None:
                    _ids = _ids[(_ids != int(_nd8)) & (_ids > 0)]
                else:
                    _ids = _ids[_ids > 0]
                if len(_ids) == 0:
                    self._log("  Watershed raster empty — gagewatershed may have failed.",
                              "WARNING")
                    # Try polygonize as binary (value > 0)
                    ws_ok = self._polygonize_binary_raster(
                        watershed_raster, watershed_shp)
                else:
                    _srs8 = _os8.SpatialReference()
                    _srs8.ImportFromWkt(_pr8)
                    _drv8 = _og8.GetDriverByName("ESRI Shapefile")
                    self._delete_shapefile(watershed_shp)
                    _wds8 = _drv8.CreateDataSource(os.path.normpath(watershed_shp))
                    _wl8  = _wds8.CreateLayer("ws", srs=_srs8,
                                              geom_type=_og8.wkbPolygon)
                    _wl8.CreateField(_og8.FieldDefn("OUTLET_ID", _og8.OFTInteger))
                    _wl8.CreateField(_og8.FieldDefn("WS_CELLS",  _og8.OFTInteger))

                    # Build independent mask per outlet ID
                    _masks = {int(_id): (_arr8 == _id) for _id in _ids}

                    # Expand nested: if outlet coord of ID_b falls inside mask of ID_a,
                    # then ID_a should contain all of ID_b's cells too
                    if len(_ids) > 1:
                        _all_coords = self._get_all_point_coords(outlet_snapped)
                        _expanded = {int(_id): _m.copy()
                                     for _id, _m in _masks.items()}
                        _rs, _cs = _arr8.shape
                        for _i, _id_a in enumerate(_ids):
                            _id_a = int(_id_a)
                            for _j, _id_b in enumerate(_ids):
                                _id_b = int(_id_b)
                                if _id_a == _id_b:
                                    continue
                                if _j >= len(_all_coords):
                                    continue
                                _bx, _by = _all_coords[_j]
                                _bc = int((_bx - _gt8[0]) / _gt8[1])
                                _br = int((_by - _gt8[3]) / _gt8[5])
                                _br = max(0, min(_rs-1, _br))
                                _bc = max(0, min(_cs-1, _bc))
                                if _masks[_id_a][_br, _bc]:
                                    _expanded[_id_a] |= _masks[_id_b]
                                    self._log(
                                        f"  Outlet {_id_a} contains "
                                        f"outlet {_id_b} — expanding.",
                                        "INFO")
                        _masks = _expanded

                    for _id in _ids:
                        _id = int(_id)
                        _bin = _np8.where(_masks[_id], 255, 0).astype(_np8.uint8)
                        _mem = _gd8.GetDriverByName("MEM").Create(
                            "", _arr8.shape[1], _arr8.shape[0], 1, _gd8.GDT_Byte)
                        _mem.SetGeoTransform(_gt8); _mem.SetProjection(_pr8)
                        _mem.GetRasterBand(1).WriteArray(_bin)
                        _mb = _mem.GetRasterBand(1)
                        _tl = _wds8.CreateLayer(f"t{_id}", srs=_srs8,
                                                geom_type=_og8.wkbPolygon)
                        _tl.CreateField(_og8.FieldDefn("V", _og8.OFTInteger))
                        _gd8.Polygonize(_mb, _mb, _tl, 0)
                        _mem = None
                        _ug = None
                        _tl.ResetReading()
                        for _f8 in _tl:
                            _g = _f8.GetGeometryRef()
                            if _g:
                                _ug = _g.Clone() if _ug is None else _ug.Union(_g)
                        if _ug:
                            _of = _og8.Feature(_wl8.GetLayerDefn())
                            _of.SetGeometry(_ug)
                            _of.SetField("OUTLET_ID", _id)
                            _of.SetField("WS_CELLS",  int(_masks[_id].sum()))
                            _wl8.CreateFeature(_of)
                        for _li in range(_wds8.GetLayerCount()):
                            if _wds8.GetLayerByIndex(_li).GetName() == f"t{_id}":
                                _wds8.DeleteLayer(_li); break
                    _wds8.FlushCache(); _wds8 = None
                    ws_ok = True
                if ws_ok:
                    self._log(f"  Watershed: {len(_ids)} polygon(s).", "SUCCESS")
            except Exception as _e8:
                self._log(f"  Watershed polygonize error: {_e8}", "WARNING")
                ws_ok = self._polygonize_binary_raster(
                    watershed_raster, watershed_shp)
            self._progress(step)

            # Step 9 — UnnestBasins skipped
            step += 1
            self._log("STEP 9/13 — UnnestBasins skipped (TauDEM engine).", "INFO")
            self._progress(step)

            # Step 10 — LFP via accumulation trace
            step += 1
            if params.get("run_longest_flow", True):
                if self._cancelled():
                    return False, "Cancelled.", None
                self._log("STEP 10/13 (TauDEM) — Longest Flow Path", "STEP")
                lfp_ws_path = os.path.join(out, "WBT_LongestFlowPath.shp")
                all_coords = self._get_all_point_coords(outlet_snapped)
                lfp_ok = self._compute_lfp_upstream_from_outlet(
                    d8_accumu, streams_raster, all_coords, lfp_ws_path,
                    watershed_raster=watershed_raster)
                lfp_ws_shp = lfp_ws_path if lfp_ok else None
            else:
                self._log("STEP 10/13 — LFP skipped.", "INFO")
                lfp_ws_shp = None
            self._progress(step)

            # Step 11 — streamnet (subbasins)
            if self._cancelled():
                return False, "Cancelled.", None
            step += 1
            self._log("STEP 11/13 (TauDEM) — streamnet (subbasins)", "STEP")
            # Per-watershed subbasins (with outlet)
            subbasins_ws = os.path.join(out, "_tmp_Subbasins_ws.tif")
            net_shp2  = os.path.join(out, "_tmp_net2.shp")
            tree_f2   = os.path.join(out, "_tmp_tree2.dat")
            coord_f2  = os.path.join(out, "_tmp_coord2.dat")
            ord_f2    = os.path.join(out, "_tmp_ord2.tif")
            ok_sub, msg_sub = self._run_taudem(taudem, "streamnet", [
                "-fel", filled_dem, "-p", d8_pointer,
                "-ad8", d8_accumu, "-src", streams_raster,
                "-o", outlet_snapped,
                "-ord", ord_f2, "-tree", tree_f2,
                "-coord", coord_f2, "-net", net_shp2,
                "-w", subbasins_ws], mpiexec_path)
            sub_ok = ok_sub and self._check_raster_has_data(subbasins_ws)
            if not sub_ok:
                self._log(f"  streamnet (ws) warning: {msg_sub}", "WARNING")
                subbasins_ws = None

            # Full-DEM subbasins (no outlet — for AllDEM_Subbasins output)
            subbasins_full = os.path.join(out, "_tmp_Subbasins_full.tif")
            net_shp3  = os.path.join(out, "_tmp_net3.shp")
            tree_f3   = os.path.join(out, "_tmp_tree3.dat")
            coord_f3  = os.path.join(out, "_tmp_coord3.dat")
            ord_f3    = os.path.join(out, "_tmp_ord3.tif")
            ok_full, msg_full = self._run_taudem(taudem, "streamnet", [
                "-fel", filled_dem, "-p", d8_pointer,
                "-ad8", d8_accumu, "-src", streams_raster,
                "-ord", ord_f3, "-tree", tree_f3,
                "-coord", coord_f3, "-net", net_shp3,
                "-w", subbasins_full], mpiexec_path)
            if not (ok_full and self._check_raster_has_data(subbasins_full)):
                self._log(f"  streamnet (full DEM) warning: {msg_full}", "WARNING")
                subbasins_full = subbasins_ws  # fallback to watershed subbasins
            self._progress(step)

            # Compute per-subbasin LFP (replicates WBT LongestFlowpath)
            lfp_all_shp = None
            _lfp_all_path = os.path.join(out, "_tmp_lfp_alldem.shp")
            if subbasins_full and os.path.exists(subbasins_full):
                self._log("  Computing LFP per subbasin…", "INFO")
                ok_lfp_all = self._compute_lfp_per_subbasin(
                    d8_accumu, subbasins_full, _lfp_all_path)
                if ok_lfp_all:
                    lfp_all_shp = _lfp_all_path
            if not lfp_all_shp and subbasins_ws and os.path.exists(subbasins_ws):
                ok_lfp_ws = self._compute_lfp_per_subbasin(
                    d8_accumu, subbasins_ws, _lfp_all_path)
                if ok_lfp_ws:
                    lfp_all_shp = _lfp_all_path

            ctx = {
                "out": out, "filled_dem": filled_dem,
                "watershed_raster": watershed_raster,
                "subbasins_full": subbasins_full,
                "lfp_ws_shp": lfp_ws_shp,
                "lfp_all_shp": lfp_all_shp,
                "sub_ok": sub_ok or ok_full, "wbt": None,
                "no_outlet": False, "engine": "taudem",
            }
            return True, "Phase 2 TauDEM complete.", ctx

        except Exception:
            import traceback
            self._log(traceback.format_exc(), "ERROR")
            return False, "Phase 2 TauDEM error — see Log tab.", None


    def run_phase1_grass(self, params):
        """Phase 1 using SAGA (called 'grass' for backward compat with dialog)."""
        return self.run_phase1_saga(params)

    def run_phase2_grass(self, params):
        """Phase 2 using SAGA (called 'grass' for backward compat with dialog)."""
        return self.run_phase2_saga(params)

    def run_phase1_saga(self, params):
        """
        Phase 1 using GRASS tools (per OCWGIS tutorial):
        1. r.watershed → accumulation + drainage (raw, with negatives)
        2. r.stream.extract → proper stream vector (not r.to.vect)
        The drainage raster is saved RAW (with negatives) for r.water.outlet,
        and ABS for LFP computation.
        """
        self.cancel_requested = False
        try:
            out = params["output_dir"]
            dem = params["dem_path"]
            os.makedirs(out, exist_ok=True)
            self._delete_phase1_outputs(out)

            filled_dem     = os.path.join(out, "WBT_Filled_DEM.tif")
            d8_pointer     = os.path.join(out, "WBT_D8_Pointer.tif")
            d8_pointer_raw = os.path.join(out, "_tmp_D8_Pointer_raw.tif")
            d8_accumu      = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            streams_raster = os.path.join(out, "WBT_ExtractStreams.tif")
            streams_shp    = os.path.join(out, "WBT_ExtractStreams_vector.shp")

            threshold     = self._resolve_threshold(params, dem)
            threshold_int = max(1, int(threshold))
            self._log(f"  Threshold: {threshold_int} cells", "INFO")

            # Step 1: r.watershed (handles sink filling internally with -s flag)
            # Save drainage RAW (with negatives) — needed by r.water.outlet
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 1/3 (GRASS) — r.watershed (fill+D8+accum+streams)", "STEP")
            ok, msg = self._run_grass_tool(
                "grass7:r.watershed",
                {"elevation": dem,
                 "threshold": threshold_int,
                 "accumulation": d8_accumu,
                 "drainage": d8_pointer_raw,
                 "stream": streams_raster,
                 "-s": True,
                 "GRASS_OUTPUT_TYPE_PARAMETER": 5})
            if not ok:
                return False, f"GRASS r.watershed failed: {msg}", None

            # Convert drainage to absolute values for LFP/snap usage
            # (GRASS drainage has negatives at boundaries — abs needed for BFS)
            import numpy as _np_p1
            from osgeo import gdal as _gdal_p1
            _ds = _gdal_p1.Open(os.path.normpath(d8_pointer_raw))
            if _ds:
                _gt = _ds.GetGeoTransform()
                _proj = _ds.GetProjection()
                _arr = _ds.GetRasterBand(1).ReadAsArray()
                _nd = _ds.GetRasterBand(1).GetNoDataValue()
                _ds = None
                _abs = _np_p1.abs(_arr.astype(_np_p1.int32))
                if _nd is not None:
                    _abs[_np_p1.abs(_arr.astype(_np_p1.float64) - _nd) < 0.5] = 0
                _drv = _gdal_p1.GetDriverByName("GTiff")
                _out = _drv.Create(os.path.normpath(d8_pointer),
                                   _abs.shape[1], _abs.shape[0],
                                   1, _gdal_p1.GDT_Int32)
                _out.SetGeoTransform(_gt)
                _out.SetProjection(_proj)
                _out.GetRasterBand(1).SetNoDataValue(0)
                _out.GetRasterBand(1).WriteArray(_abs)
                _out.FlushCache()
                _out = None
                self._log("  D8 abs raster written.", "INFO")

            # Copy DEM as filled (r.watershed fills internally)
            import shutil as _sh
            _sh.copy2(dem, filled_dem)
            self._progress(2)

            # Step 2: Vectorize streams — proper centerlines, not polygon outlines
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 2/3 (GRASS) — Vectorize streams", "STEP")

            if not os.path.exists(streams_raster) or \
                    os.path.getsize(streams_raster) < 100:
                return False, (
                    "Stream raster not produced. "
                    "Threshold may be too high for this DEM extent."), None

            # Try r.stream.extract (best — topological centerlines)
            streams_gpkg = os.path.join(out, "_tmp_streams.gpkg")
            streams_v_shp = streams_shp  # target shp

            ok2 = False
            # Try several GRASS_OUTPUT_TYPE_PARAMETER values (varies by QGIS version)
            for _otype in [0, 1, 2, 3, 4, 5]:
                _ok, _msg = self._run_grass_tool(
                    "grass7:r.stream.extract",
                    {"elevation": filled_dem,
                     "accumulation": d8_accumu,
                     "threshold": threshold_int,
                     "stream_vector": streams_gpkg,
                     "GRASS_OUTPUT_TYPE_PARAMETER": _otype})
                if _ok and os.path.exists(streams_gpkg) \
                        and os.path.getsize(streams_gpkg) > 1000:
                    ok2 = True
                    self._log(f"  r.stream.extract OK (output_type={_otype})", "INFO")
                    break

            if ok2:
                try:
                    from osgeo import ogr as _ogr_v
                    self._delete_shapefile(streams_v_shp)
                    _src = _ogr_v.Open(streams_gpkg)
                    if _src and _src.GetLayerCount() > 0:
                        _drv = _ogr_v.GetDriverByName("ESRI Shapefile")
                        _drv.CopyDataSource(_src, os.path.normpath(streams_v_shp))
                        _src = None
                        self._log("  Streams from r.stream.extract.", "SUCCESS")
                        streams_shp = streams_v_shp
                    else:
                        ok2 = False
                except Exception as _e:
                    self._log(f"  gpkg→shp: {_e}", "WARNING")
                    ok2 = False

            if not ok2:
                # Fallback 1: r.to.vect type=line
                self._log("  r.stream.extract failed — trying r.to.vect", "INFO")
                _ok3, _msg3 = self._run_grass_tool(
                    "grass7:r.to.vect",
                    {"input": streams_raster,
                     "type": 0,
                     "output": streams_v_shp,
                     "GRASS_OUTPUT_TYPE_PARAMETER": 2})
                if _ok3 and os.path.exists(streams_v_shp) \
                        and os.path.getsize(streams_v_shp) > 100:
                    streams_shp = streams_v_shp
                    self._log("  Streams from r.to.vect.", "INFO")
                else:
                    # Fallback 2: GDAL D8-based vectorization (always works)
                    self._log("  r.to.vect failed — GDAL vectorization", "INFO")
                    d8_abs = os.path.join(out, "WBT_D8_Pointer.tif")
                    streams_shp = self._vectorize_streams_with_d8(
                        streams_raster, d8_abs, streams_v_shp)
                    if not streams_shp:
                        streams_shp = self._vectorize_streams_gdal(
                            streams_raster, streams_v_shp)
            self._progress(5)

            if streams_shp and os.path.exists(streams_shp):
                self._log(
                    "Stream network ready — place outlet then run Phase 2.", "SUCCESS")
            else:
                self._log(
                    "Stream raster ready. Load WBT_ExtractStreams.tif instead.",
                    "WARNING")
                streams_shp = None

            return True, "Phase 1 complete (GRASS+GDAL).", streams_shp

        except Exception:
            import traceback
            self._log(traceback.format_exc(), "ERROR")
            return False, "Phase 1 error — see Log tab.", None

    def run_phase2_saga(self, params):
        """
        Phase 2: Hybrid GRASS+GDAL approach.
        - GRASS r.water.outlet: watershed delineation per outlet
        - GDAL: polygonize watershed, snap outlet
        - GRASS r.watershed: subbasins
        - GDAL/numpy BFS: LFP along stream network
        All tools already proven to work in earlier sessions.
        """
        self.cancel_requested = False
        try:
            out = params["output_dir"]
            no_outlet = params.get("no_outlet", False)
            filled_dem     = os.path.join(out, "WBT_Filled_DEM.tif")
            d8_pointer     = os.path.join(out, "WBT_D8_Pointer.tif")
            streams_raster = os.path.join(out, "WBT_ExtractStreams.tif")

            for f, n in [(filled_dem,     "WBT_Filled_DEM.tif"),
                         (d8_pointer,     "WBT_D8_Pointer.tif"),
                         (streams_raster, "WBT_ExtractStreams.tif")]:
                if not os.path.exists(f):
                    return False, (
                        f"Phase 1 output '{n}' not found. Run Phase 1 first."), None

            self._delete_phase2_outputs(out)

            watershed_raster = os.path.join(out, "WBT_Watershed.tif")
            ws_boundary_shp  = os.path.join(out, "WBT_Watershed_Boundary.shp")
            subbasins_full   = os.path.join(out, "_tmp_Subbasins_full.tif")

            threshold     = self._resolve_threshold(params, filled_dem)
            threshold_int = max(1, int(threshold))

            # ── No-outlet mode ───────────────────────────────────────────────
            if no_outlet:
                self._log("STEP 11 (GRASS) — Full-DEM subbasins (accumulation-based)",
                          "STEP")
                # Use existing Phase 1 outputs: stream raster + D8 accumulation
                # Delineate subbasins by watershedding each stream cell headward
                _streams_rst = os.path.join(out, "WBT_ExtractStreams.tif")
                _accumu_rst  = os.path.join(out, "WBT_D8_FlowAccumu.tif")
                _d8_ptr      = os.path.join(out, "WBT_D8_Pointer.tif")
                sub_ok = False
                if all(os.path.exists(f) for f in
                       [_streams_rst, _accumu_rst, _d8_ptr]):
                    sub_ok = self._delineate_subbasins_from_streams(
                        _streams_rst, _accumu_rst, _d8_ptr, subbasins_full)
                if not sub_ok:
                    # Fallback: r.watershed basin (may produce few basins)
                    self._log("  Fallback: r.watershed basin output", "INFO")
                    ok, msg = self._run_grass_tool(
                        "grass7:r.watershed",
                        {"elevation": filled_dem,
                         "threshold": threshold_int,
                         "basin": subbasins_full,
                         "accumulation": os.path.join(out, "_tmp_no_accum.tif"),
                         "-s": True,
                         "GRASS_OUTPUT_TYPE_PARAMETER": 5})
                    sub_ok = self._check_raster_has_data(subbasins_full)
                if not sub_ok:
                    self._log("  Subbasins failed.", "WARNING")
                    subbasins_full = None
                # Per-subbasin LFP using accumulation raster
                _lfp_no_path = os.path.join(out, "_tmp_lfp_alldem.shp")
                _accumu_no   = os.path.join(out, "WBT_D8_FlowAccumu.tif")
                lfp_no_shp   = None
                if sub_ok and subbasins_full and os.path.exists(subbasins_full)                         and os.path.exists(_accumu_no):
                    if self._compute_lfp_per_subbasin(
                            _accumu_no, subbasins_full, _lfp_no_path):
                        lfp_no_shp = _lfp_no_path
                ctx = {
                    "out": out, "filled_dem": filled_dem,
                    "watershed_raster": None,
                    "subbasins_full": subbasins_full,
                    "lfp_ws_shp": None, "lfp_all_shp": lfp_no_shp,
                    "sub_ok": sub_ok, "wbt": None,
                    "no_outlet": True, "engine": "grass",
                }
                return True, "Phase 2 complete (no-outlet).", ctx

            # ── WITH OUTLET ──────────────────────────────────────────────────
            outlet = params["outlet_path"]
            outlet_snapped = os.path.join(out, "outlet_snapped.shp")
            snap_dist = params.get("snap_distance", 50)

            # Step 6 — Snap outlet to stream
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 6/13 — Snap outlet to stream (GDAL)", "STEP")
            ok_snap = self._snap_outlet_to_stream_gdal(
                outlet, streams_raster, outlet_snapped, snap_dist)
            if not ok_snap:
                self._log("  Snap warning — using original outlet.", "WARNING")
                import shutil as _sh
                _sh.copy2(outlet, outlet_snapped)
            all_coords = self._get_all_point_coords(outlet_snapped)
            if not all_coords:
                all_coords = self._get_all_point_coords(outlet)
            self._log(f"  Outlet count: {len(all_coords)}", "INFO")
            self._progress(self.PHASE1_STEPS + 1)

            # r.water.outlet needs the RAW drainage (with negatives)
            # Per OCWGIS tutorial: use flowdirabs (abs values) — but GRASS
            # r.water.outlet actually accepts both; use raw for correctness
            d8_pointer_raw = os.path.join(out, "_tmp_D8_Pointer_raw.tif")
            d8_for_outlet = d8_pointer_raw if os.path.exists(d8_pointer_raw) \
                else d8_pointer

            # Step 7 — Watershed delineation: GRASS r.water.outlet per outlet
            if self._cancelled():
                return False, "Cancelled.", None
            self._log(
                f"STEP 7/13 (GRASS) — r.water.outlet × {len(all_coords)}", "STEP")

            import numpy as _np
            from osgeo import gdal as _gdal_ws, ogr as _ogr_ws, osr as _osr_ws
            merged_arr = None
            merged_gt = None
            merged_proj = None
            per_outlet_valid = []  # (outlet_id, valid_bool_mask)

            for idx_pt, (px, py) in enumerate(all_coords):
                ws_tmp = watershed_raster.replace(".tif", f"_tmp_{idx_pt}.tif")
                ok_ws, msg_ws = self._run_grass_tool(
                    "grass7:r.water.outlet",
                    {"input": d8_for_outlet,
                     "output": ws_tmp,
                     "coordinates": f"{px},{py}",
                     "GRASS_OUTPUT_TYPE_PARAMETER": 5})
                if not ok_ws or not os.path.exists(ws_tmp) \
                        or os.path.getsize(ws_tmp) < 100:
                    self._log(
                        f"  r.water.outlet outlet {idx_pt+1} failed: {msg_ws}",
                        "WARNING")
                    continue
                ds_tmp = _gdal_ws.Open(os.path.normpath(ws_tmp))
                if ds_tmp is None:
                    continue
                arr = ds_tmp.GetRasterBand(1).ReadAsArray().astype(_np.float64)
                nd  = ds_tmp.GetRasterBand(1).GetNoDataValue()
                if merged_arr is None:
                    merged_arr  = _np.zeros(arr.shape, dtype=_np.int32)
                    merged_gt   = ds_tmp.GetGeoTransform()
                    merged_proj = ds_tmp.GetProjection()
                if nd is not None:
                    valid = ((_np.abs(arr - nd) > 0.5) & (arr > 0) & (arr < 1e30))
                else:
                    valid = (arr > 0) & (arr < 1e30) & _np.isfinite(arr)
                n_valid = int(valid.sum())
                self._log(f"  Outlet {idx_pt+1}: {n_valid} cells", "INFO")
                if n_valid > 0:
                    per_outlet_valid.append((idx_pt + 1, valid.copy()))
                    # Merged raster: first outlet to claim a cell wins
                    # (will be corrected after all outlets processed)
                    merged_arr[valid] = idx_pt + 1
                ds_tmp = None
                for ext in [".tif", ".tfw", ".tif.aux.xml"]:
                    p = os.path.normpath(ws_tmp.replace(".tif", "") + ext)
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass

            if merged_arr is None:
                return False, (
                    "r.water.outlet failed for all outlets. "
                    "Check that outlet is on a valid stream cell."), None

            # Fix nested watersheds: a downstream outlet should contain ALL cells
            # of any upstream outlet that falls within its watershed area.
            # If outlet A's mask contains the outlet POINT of outlet B,
            # then outlet B is nested inside A → expand A's mask to include B's cells.
            if len(per_outlet_valid) > 1:
                def xy_to_rc_ws(x, y, gt_ws, rows_ws, cols_ws):
                    c = int((x - gt_ws[0]) / gt_ws[1])
                    r = int((y - gt_ws[3]) / gt_ws[5])
                    return (max(0, min(rows_ws-1, r)),
                            max(0, min(cols_ws-1, c)))

                ws_rows, ws_cols = merged_arr.shape
                # Build expanded masks: for each outlet, union its mask with all
                # nested outlets' masks
                expanded = {out_id: mask.copy()
                            for out_id, mask in per_outlet_valid}
                for i, (id_a, mask_a) in enumerate(per_outlet_valid):
                    for j, (id_b, mask_b) in enumerate(per_outlet_valid):
                        if i == j:
                            continue
                        # Check if outlet B's coordinate falls inside mask_a
                        bx, by = all_coords[j]
                        br, bc = xy_to_rc_ws(bx, by, merged_gt,
                                              ws_rows, ws_cols)
                        if mask_a[br, bc]:
                            # B is nested inside A → A should contain B's cells
                            expanded[id_a] = expanded[id_a] | mask_b
                            self._log(
                                f"  Outlet {id_a} contains outlet {id_b} "
                                f"— expanding watershed.", "INFO")
                # Replace per_outlet_valid with expanded masks
                per_outlet_valid = [(oid, expanded[oid])
                                    for oid, _ in per_outlet_valid]
                # Rebuild merged_arr: largest watershed (most cells) claims first
                per_outlet_valid.sort(key=lambda x: -int(x[1].sum()))
                merged_arr = _np.zeros_like(merged_arr)
                for out_id, mask in per_outlet_valid:
                    unclaimed = mask & (merged_arr == 0)
                    merged_arr[unclaimed] = out_id
                # Restore original order
                per_outlet_valid.sort(key=lambda x: x[0])

            self._log(
                f"  Watershed: {len(per_outlet_valid)} polygon(s).", "SUCCESS")

            # Write merged watershed raster (each outlet = unique ID)
            drv_tif = _gdal_ws.GetDriverByName("GTiff")
            ws_out = drv_tif.Create(
                os.path.normpath(watershed_raster),
                merged_arr.shape[1], merged_arr.shape[0],
                1, _gdal_ws.GDT_Int32)
            ws_out.SetGeoTransform(merged_gt)
            ws_out.SetProjection(merged_proj)
            ws_out.GetRasterBand(1).SetNoDataValue(0)
            ws_out.GetRasterBand(1).WriteArray(merged_arr)
            ws_out.FlushCache()
            ws_out = None
            self._progress(self.PHASE1_STEPS + 2)

            # Step 8 — Polygonize each outlet as a SEPARATE polygon
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 8/13 (GDAL) — Polygonize per-outlet watershed", "STEP")
            self._delete_shapefile(ws_boundary_shp)

            srs_ws = _osr_ws.SpatialReference()
            srs_ws.ImportFromWkt(merged_proj)
            drv_shp = _ogr_ws.GetDriverByName("ESRI Shapefile")
            ws_ds = drv_shp.CreateDataSource(os.path.normpath(ws_boundary_shp))
            ws_lyr = ws_ds.CreateLayer("boundary", srs=srs_ws,
                                       geom_type=_ogr_ws.wkbMultiPolygon)
            ws_lyr.CreateField(_ogr_ws.FieldDefn("OUTLET_ID", _ogr_ws.OFTInteger))
            ws_lyr.CreateField(_ogr_ws.FieldDefn("WS_CELLS",  _ogr_ws.OFTInteger))

            for out_id, valid_mask in per_outlet_valid:
                bin_arr = _np.where(valid_mask, 255, 0).astype(_np.uint8)
                mem_drv = _gdal_ws.GetDriverByName("MEM")
                mem_ds = mem_drv.Create(
                    "", merged_arr.shape[1], merged_arr.shape[0],
                    1, _gdal_ws.GDT_Byte)
                mem_ds.SetGeoTransform(merged_gt)
                mem_ds.SetProjection(merged_proj)
                mem_ds.GetRasterBand(1).WriteArray(bin_arr)
                mask_band = mem_ds.GetRasterBand(1)
                # Polygonize into a temp layer
                tmp_name = f"tmp_{out_id}"
                tmp_lyr = ws_ds.CreateLayer(
                    tmp_name, srs=srs_ws, geom_type=_ogr_ws.wkbPolygon)
                tmp_lyr.CreateField(_ogr_ws.FieldDefn("V", _ogr_ws.OFTInteger))
                _gdal_ws.Polygonize(mask_band, mask_band, tmp_lyr, 0)
                mem_ds = None
                # Union all polygons → one multipolygon per outlet
                union_geom = None
                tmp_lyr.ResetReading()
                for f2 in tmp_lyr:
                    g = f2.GetGeometryRef()
                    if g:
                        union_geom = g.Clone() if union_geom is None                             else union_geom.Union(g)
                if union_geom:
                    out_feat = _ogr_ws.Feature(ws_lyr.GetLayerDefn())
                    out_feat.SetGeometry(union_geom)
                    out_feat.SetField("OUTLET_ID", out_id)
                    out_feat.SetField("WS_CELLS",  int(valid_mask.sum()))
                    ws_lyr.CreateFeature(out_feat)
                # Delete temp layer
                for li in range(ws_ds.GetLayerCount()):
                    if ws_ds.GetLayerByIndex(li).GetName() == tmp_name:
                        ws_ds.DeleteLayer(li)
                        break

            ws_ds.FlushCache()
            ws_ds = None
            self._log(
                f"  {len(per_outlet_valid)} watershed polygon(s) written.",
                "SUCCESS")
            self._progress(self.PHASE1_STEPS + 3)

            # Step 9: UnnestBasins — skipped for GRASS
            self._log("STEP 9 (GRASS) — UnnestBasins skipped.", "INFO")

            # Step 10: Watershed LFP via accumulation trace
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 10 (GRASS) — Longest Flow Path (accumulation trace)",
                      "STEP")
            d8_accumu_g = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            lfp_ws_shp_g = None
            if os.path.exists(d8_accumu_g):
                _lfp_ws_path_g = os.path.join(out, "WBT_LongestFlowPath.shp")
                all_coords_g   = self._get_all_point_coords(outlet_snapped)
                if all_coords_g:
                    lfp_ok_g = self._compute_lfp_upstream_from_outlet(
                        d8_accumu_g, None, all_coords_g, _lfp_ws_path_g,
                        watershed_raster=watershed_raster)
                    if lfp_ok_g:
                        lfp_ws_shp_g = _lfp_ws_path_g
                        self._log("  Watershed LFP written.", "SUCCESS")
            self._progress(self.PHASE1_STEPS + 4)

            # Step 11: Full-DEM subbasins using stream-based delineation
            if self._cancelled():
                return False, "Cancelled.", None
            self._log("STEP 11/13 (GRASS) — subbasins (stream-based)", "STEP")
            subbasins_full = os.path.join(out, "_tmp_Subbasins_full.tif")
            _streams_g = os.path.join(out, "WBT_ExtractStreams.tif")
            _accumu_g  = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            _d8ptr_g   = os.path.join(out, "WBT_D8_Pointer.tif")
            sub_ok = False
            if all(os.path.exists(f) for f in [_streams_g, _accumu_g, _d8ptr_g]):
                sub_ok = self._delineate_subbasins_from_streams(
                    _streams_g, _accumu_g, _d8ptr_g, subbasins_full)
            if not sub_ok:
                self._log("  Stream subbasins failed — r.watershed fallback", "WARNING")
                _tmp_accum_sub = os.path.join(out, "_tmp_sub_accum2.tif")
                ok_fb, _ = self._run_grass_tool(
                    "grass7:r.watershed",
                    {"elevation": filled_dem, "threshold": threshold_int,
                     "basin": subbasins_full, "accumulation": _tmp_accum_sub,
                     "-s": True, "GRASS_OUTPUT_TYPE_PARAMETER": 5})
                sub_ok = ok_fb and self._check_raster_has_data(subbasins_full)
            if not sub_ok:
                self._log("  All subbasin methods failed.", "WARNING")
                subbasins_full = None
            self._progress(self.PHASE1_STEPS + 5)

            # Per-subbasin LFP from accumulation raster
            lfp_grass_shp = None
            d8_accumu_grass = os.path.join(out, "WBT_D8_FlowAccumu.tif")
            _lfp_grass_path = os.path.join(out, "_tmp_lfp_alldem.shp")
            if sub_ok and subbasins_full and os.path.exists(subbasins_full)                     and os.path.exists(d8_accumu_grass):
                self._log("  Computing LFP per subbasin (GRASS)…", "INFO")
                if self._compute_lfp_per_subbasin(
                        d8_accumu_grass, subbasins_full, _lfp_grass_path):
                    lfp_grass_shp = _lfp_grass_path

            ctx = {
                "out": out, "filled_dem": filled_dem,
                "watershed_raster": watershed_raster,
                "subbasins_full": subbasins_full,
                "lfp_ws_shp": lfp_ws_shp_g,   # watershed LFP
                "lfp_all_shp": lfp_grass_shp,  # per-subbasin LFP
                "sub_ok": sub_ok, "wbt": None,
                "no_outlet": False, "engine": "grass",
            }
            return True, "Phase 2 complete (GRASS — watershed + LFP + subbasins).", ctx

        except Exception:
            import traceback
            self._log(traceback.format_exc(), "ERROR")
            return False, "Phase 2 error — see Log tab.", None


    def _check_raster_has_data(self, path):
        """Return True if raster exists and has at least one non-nodata cell."""
        if not path or not os.path.exists(os.path.normpath(path)):
            return False
        try:
            from osgeo import gdal as _gdal
            ds = _gdal.Open(os.path.normpath(path))
            if ds is None:
                return False
            nd = ds.GetRasterBand(1).GetNoDataValue()
            arr = ds.GetRasterBand(1).ReadAsArray()
            ds = None
            if nd is not None:
                import numpy as np
                return bool((np.abs(arr.astype(np.float64) - nd) > 1.0).any())
            import numpy as np
            return bool((arr != 0).any())
        except Exception:
            return False

    def _run_saga_tool(self, algorithm, parameters):
        """Run a SAGA algorithm via QGIS Processing. Returns (ok, msg)."""
        try:
            import processing
            from qgis.core import QgsProcessingFeedback

            # Detect SAGA provider prefix: 'saga' (QGIS 3.x) or 'sagang' (newer)
            if not hasattr(WatershedProcessor, '_saga_prefix'):
                try:
                    from qgis.core import QgsApplication
                    reg = QgsApplication.processingRegistry()
                    pids = [p.id() for p in reg.providers()]
                    if "sagang" in pids:
                        WatershedProcessor._saga_prefix = "sagang:"
                    elif "saga" in pids:
                        WatershedProcessor._saga_prefix = "saga:"
                    else:
                        WatershedProcessor._saga_prefix = "saga:"
                except Exception:
                    WatershedProcessor._saga_prefix = "saga:"
            prefix = WatershedProcessor._saga_prefix

            # Normalise algorithm name
            alg = algorithm
            if ":" in alg:
                alg = prefix + alg.split(":", 1)[1]
            else:
                alg = prefix + alg

            class _FB(QgsProcessingFeedback):
                def __init__(self, lf):
                    super().__init__()
                    self._lf = lf

                def pushInfo(self, info):
                    if info.strip():
                        self._lf(f"  SAGA: {info}", "INFO")

                def reportError(self, error, fatal=False):
                    # Suppress SAGA version check warnings — try anyway
                    if "unsupported SAGA version" in error.lower():
                        return
                    if error.strip():
                        self._lf(f"  SAGA ERR: {error}", "WARNING")

                def setProgressText(self, text):
                    pass

            self._log(f"  alg: {alg}", "INFO")
            processing.run(alg, parameters, feedback=_FB(self._log))
            self._log("  → Done.", "SUCCESS")
            return True, "OK"
        except Exception as exc:
            return False, str(exc)


    def _fill_sinks_gdal(self, dem_path, out_path):
        """
        Fill DEM sinks using GDAL FillNodata + simple surface interpolation.
        For flat/simple DEMs this is sufficient for watershed analysis.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal
            ds = _gdal.Open(os.path.normpath(dem_path))
            if ds is None:
                return False
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
            arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
            nd = ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = arr.shape
            ds = None

            # Simple sink fill: raise each cell to min of its 4 neighbours
            # if it is lower than all of them (a true sink).
            # One pass is enough for most practical DEMs.
            filled = arr.copy()
            if nd is not None:
                valid = np.abs(filled - nd) > 0.001
            else:
                valid = np.isfinite(filled)

            # Vectorized single-pass fill
            interior = (slice(1, rows-1), slice(1, cols-1))
            v = filled[interior]
            n = filled[:-2, 1:-1]   # north
            s = filled[2:,  1:-1]   # south
            w = filled[1:-1, :-2]   # west
            e = filled[1:-1, 2:]    # east
            min_n = np.minimum(np.minimum(n, s), np.minimum(w, e))
            sink = (v < min_n) & valid[interior]
            filled[interior][sink] = min_n[sink] + 0.001

            drv = _gdal.GetDriverByName("GTiff")
            out = drv.Create(os.path.normpath(out_path),
                             cols, rows, 1, _gdal.GDT_Float32,
                             options=["COMPRESS=LZW"])
            out.SetGeoTransform(gt)
            out.SetProjection(proj)
            b = out.GetRasterBand(1)
            if nd is not None:
                b.SetNoDataValue(float(nd))
            b.WriteArray(filled)
            out.FlushCache()
            out = None
            return True
        except Exception as exc:
            self._log(f"  _fill_sinks_gdal error: {exc}", "WARNING")
            return False

    def _compute_d8_and_accumulation(self, filled_dem, d8_out, accum_out):
        """
        Compute D8 flow direction and accumulation using pure numpy.
        D8 encoding: WBT style (powers of 2, outflow direction).
        1=E 2=NE 4=N 8=NW 16=W 32=SW 64=S 128=SE
        """
        try:
            import numpy as np
            from collections import deque
            from osgeo import gdal as _gdal
            ds = _gdal.Open(os.path.normpath(filled_dem))
            if ds is None:
                return False
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
            dem = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
            nd = ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = dem.shape
            ds = None

            if nd is not None:
                valid = np.abs(dem - nd) > 0.001
            else:
                valid = np.isfinite(dem)

            # 8 directions: (dr, dc, code, diag_weight)
            directions = [
                (0,  1,   1, False),   # E
                (-1, 1,   2, True),    # NE
                (-1, 0,   4, False),   # N
                (-1, -1,  8, True),    # NW
                (0,  -1, 16, False),   # W
                (1,  -1, 32, True),    # SW
                (1,   0, 64, False),   # S
                (1,   1, 128, True),   # SE
            ]

            cell_x = abs(gt[1])
            cell_y = abs(gt[5])
            diag_dist = (cell_x**2 + cell_y**2) ** 0.5

            d8 = np.zeros((rows, cols), dtype=np.int32)
            # Vectorized steepest-descent D8
            # For direction (dr,dc): current cell at (r,c), neighbour at (r+dr,c+dc)
            # curr_r = rows where r+dr is in [0,rows): slice(max(0,-dr), rows-max(0,dr))
            # neigh  = curr shifted by (dr,dc)
            max_slope = np.full((rows, cols), -1e30)
            for dr, dc, code, diag in directions:
                dist = diag_dist if diag else (cell_x if dc != 0 else cell_y)
                cr = slice(max(0, -dr), rows - max(0, dr))
                cc = slice(max(0, -dc), cols - max(0, dc))
                nr_s = slice(cr.start + dr, cr.stop + dr)
                nc_s = slice(cc.start + dc, cc.stop + dc)
                slope = (dem[cr, cc] - dem[nr_s, nc_s]) / dist
                v_curr  = valid[cr, cc]
                v_neigh = valid[nr_s, nc_s]
                update  = v_curr & v_neigh & (slope > max_slope[cr, cc])
                max_slope[cr, cc][update] = slope[update]
                d8[cr, cc][update] = code

            # Write D8
            drv = _gdal.GetDriverByName("GTiff")
            d8_ds = drv.Create(os.path.normpath(d8_out),
                               cols, rows, 1, _gdal.GDT_Int32)
            d8_ds.SetGeoTransform(gt)
            d8_ds.SetProjection(proj)
            b = d8_ds.GetRasterBand(1)
            b.SetNoDataValue(0)
            b.WriteArray(d8)
            d8_ds.FlushCache()
            d8_ds = None

            # Accumulation: count inflows per cell (vectorized)
            d8_deltas = {
                1: (0, 1), 2: (-1, 1), 4: (-1, 0), 8: (-1, -1),
                16: (0, -1), 32: (1, -1), 64: (1, 0), 128: (1, 1),
            }
            inflow = np.zeros((rows, cols), dtype=np.int32)
            for code, (dr, dc) in d8_deltas.items():
                src_mask = (d8 == code) & valid
                sr2, sc2 = np.where(src_mask)
                nr2 = sr2 + dr
                nc2 = sc2 + dc
                in_bounds = ((nr2 >= 0) & (nr2 < rows) &
                             (nc2 >= 0) & (nc2 < cols))
                np.add.at(inflow, (nr2[in_bounds], nc2[in_bounds]), 1)

            accum = np.where(valid, 1, 0).astype(np.int32)
            q = deque()
            hw_r, hw_c = np.where(valid & (inflow == 0))
            for r, c in zip(hw_r, hw_c):
                q.append((int(r), int(c)))
            remaining = inflow.copy()
            while q:
                r, c = q.popleft()
                if not valid[r, c] or d8[r, c] == 0:
                    continue
                dr, dc = d8_deltas[d8[r, c]]
                nr, nc = r + dr, c + dc
                if not (0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]):
                    continue
                accum[nr, nc] += accum[r, c]
                remaining[nr, nc] -= 1
                if remaining[nr, nc] <= 0:
                    q.append((nr, nc))

            ac_ds = drv.Create(os.path.normpath(accum_out),
                               cols, rows, 1, _gdal.GDT_Int32)
            ac_ds.SetGeoTransform(gt)
            ac_ds.SetProjection(proj)
            b = ac_ds.GetRasterBand(1)
            b.SetNoDataValue(0)
            b.WriteArray(accum)
            ac_ds.FlushCache()
            ac_ds = None
            self._log(
                f"  D8 + accum computed: max_accum={int(accum.max())}", "INFO")
            return True
        except Exception as exc:
            self._log(f"  _compute_d8_and_accumulation error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    def _accumulation_from_d8_pointer(self, d8_raster, accum_out):
        """
        Compute flow accumulation from an existing D8 flow direction raster.
        Handles both SAGA D8 encoding (0-7 or 1-8 clockwise from E)
        and WBT encoding (powers of 2).
        """
        try:
            import numpy as np
            from collections import deque
            from osgeo import gdal as _gdal
            ds = _gdal.Open(os.path.normpath(d8_raster))
            if ds is None:
                return False
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
            d8_raw = ds.GetRasterBand(1).ReadAsArray()
            d8_nd = ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = d8_raw.shape
            ds = None

            d8 = d8_raw.astype(np.int32)
            if d8_nd is not None:
                d8[np.abs(d8_raw.astype(np.float64) - d8_nd) < 0.5] = -1

            # Detect encoding from unique values
            vals = np.unique(d8[d8 >= 0])
            self._log(f"  D8 pointer unique values: {vals[:10].tolist()}", "INFO")

            wbt_set = {1, 2, 4, 8, 16, 32, 64, 128}
            saga_01_set = {0, 1, 2, 3, 4, 5, 6, 7}  # SAGA 0-based clockwise from E

            val_set = set(vals.tolist())
            if val_set.issubset(wbt_set):
                # WBT: outflow direction (powers of 2)
                d8_deltas = {
                    1: (0, 1), 2: (-1, 1), 4: (-1, 0), 8: (-1, -1),
                    16: (0, -1), 32: (1, -1), 64: (1, 0), 128: (1, 1),
                }
                enc = "WBT"
            elif val_set.issubset(saga_01_set | {-1}):
                # SAGA 0-based: 0=E,1=SE,2=S,3=SW,4=W,5=NW,6=N,7=NE (clockwise from E)
                d8_deltas = {
                    0: (0, 1), 1: (1, 1), 2: (1, 0), 3: (1, -1),
                    4: (0, -1), 5: (-1, -1), 6: (-1, 0), 7: (-1, 1),
                }
                enc = "SAGA-0"
            else:
                # SAGA/GRASS 1-based or other — try 1=E clockwise
                d8_deltas = {
                    1: (0, 1), 2: (1, 1), 3: (1, 0), 4: (1, -1),
                    5: (0, -1), 6: (-1, -1), 7: (-1, 0), 8: (-1, 1),
                }
                enc = "SAGA-1"
            self._log(f"  D8 encoding: {enc}", "INFO")

            # Count inflow to each cell
            valid = (d8 >= 0)
            inflow = np.zeros((rows, cols), dtype=np.int32)
            for code, (dr, dc) in d8_deltas.items():
                src_mask = (d8 == code) & valid
                sr, sc = np.where(src_mask)
                nr = sr + dr
                nc = sc + dc
                ok_mask = (nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
                np.add.at(inflow, (nr[ok_mask], nc[ok_mask]), 1)

            # BFS accumulation from headwaters
            accum = np.where(valid, 1, 0).astype(np.int32)
            q = deque()
            hw_r, hw_c = np.where(valid & (inflow == 0))
            for r, c in zip(hw_r.tolist(), hw_c.tolist()):
                q.append((r, c))
            remaining = inflow.copy()
            while q:
                r, c = q.popleft()
                code = int(d8[r, c])
                if code < 0 or code not in d8_deltas:
                    continue
                dr, dc = d8_deltas[code]
                nr, nc = r + dr, c + dc
                if not (0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]):
                    continue
                accum[nr, nc] += accum[r, c]
                remaining[nr, nc] -= 1
                if remaining[nr, nc] <= 0:
                    q.append((int(nr), int(nc)))

            self._log(f"  Accumulation: max={int(accum.max())} cells", "INFO")

            from osgeo import gdal as _gdal2
            drv = _gdal2.GetDriverByName("GTiff")
            out_ds = drv.Create(os.path.normpath(accum_out),
                                cols, rows, 1, _gdal2.GDT_Int32,
                                options=["COMPRESS=LZW"])
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            b = out_ds.GetRasterBand(1)
            b.SetNoDataValue(0)
            b.WriteArray(accum)
            out_ds.FlushCache()
            out_ds = None
            return True
        except Exception as exc:
            self._log(f"  _accumulation_from_d8_pointer error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    def _threshold_raster_to_binary(self, accum_raster, threshold, out_raster):
        """Create a binary stream raster: 1 where accum >= threshold, 0 elsewhere."""
        try:
            import numpy as np
            from osgeo import gdal as _gdal
            ds = _gdal.Open(os.path.normpath(accum_raster))
            if ds is None:
                return False
            arr = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
            nd = ds.GetRasterBand(1).GetNoDataValue()
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
            ds = None
            if nd is not None:
                stream = np.where((arr >= threshold) & (arr != nd), 1, 0).astype(np.int32)
            else:
                stream = np.where(arr >= threshold, 1, 0).astype(np.int32)
            drv = _gdal.GetDriverByName("GTiff")
            out = drv.Create(os.path.normpath(out_raster),
                             stream.shape[1], stream.shape[0], 1, _gdal.GDT_Int32)
            out.SetGeoTransform(gt)
            out.SetProjection(proj)
            b = out.GetRasterBand(1)
            b.SetNoDataValue(0)
            b.WriteArray(stream)
            out.FlushCache()
            out = None
            return True
        except Exception as exc:
            self._log(f"  threshold_raster error: {exc}", "WARNING")
            return False

    def _vectorize_streams_with_d8(self, streams_raster, d8_raster, out_shp):
        """
        Vectorize stream raster to connected centerline shapefile using D8.
        Auto-detects D8 encoding (WBT powers-of-2 or TauDEM/GRASS 1-8).
        Traces each stream from headwater to mouth, one polyline per branch.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr

            st_ds = _gdal.Open(os.path.normpath(streams_raster))
            if st_ds is None:
                return None
            gt   = st_ds.GetGeoTransform()
            proj = st_ds.GetProjection()
            st_arr = st_ds.GetRasterBand(1).ReadAsArray()
            st_nd  = st_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = st_arr.shape
            st_ds = None

            stream = (st_arr != st_nd) & (st_arr > 0) if st_nd is not None                 else st_arr > 0
            n_cells = int(stream.sum())
            self._log(f"  Stream cells: {n_cells}", "INFO")
            if n_cells == 0:
                return None

            d8_ds = _gdal.Open(os.path.normpath(d8_raster))
            if d8_ds is None:
                return None
            d8_arr = d8_ds.GetRasterBand(1).ReadAsArray().astype(np.int32)
            d8_nd  = d8_ds.GetRasterBand(1).GetNoDataValue()
            d8_ds  = None
            if d8_nd is not None:
                d8_arr[np.abs(d8_arr.astype(np.float64) - d8_nd) < 0.5] = 0

            # Auto-detect D8 encoding from unique values
            vals = set(np.unique(d8_arr[stream & (d8_arr > 0)]).tolist())
            wbt_set = {1, 2, 4, 8, 16, 32, 64, 128}
            if vals.issubset(wbt_set):
                # WBT: outflow, powers of 2
                d8_deltas = {
                    1: (0,1), 2:(-1,1), 4:(-1,0), 8:(-1,-1),
                    16:(0,-1), 32:(1,-1), 64:(1,0), 128:(1,1)}
                enc = "WBT"
            else:
                # TauDEM/GRASS: 1-8 sequential clockwise from E
                d8_deltas = {
                    1:(0,1), 2:(-1,1), 3:(-1,0), 4:(-1,-1),
                    5:(0,-1), 6:(1,-1), 7:(1,0), 8:(1,1)}
                enc = "TauDEM/1-8"
            self._log(f"  D8 encoding: {enc} (vals sample: {sorted(vals)[:8]})",
                      "INFO")

            def rc_to_xy(r, c):
                return (gt[0]+(c+0.5)*gt[1], gt[3]+(r+0.5)*gt[5])

            # Count inflows per stream cell to find headwaters (inflow==0)
            inflow = np.zeros((rows, cols), dtype=np.int8)
            for code, (dr, dc) in d8_deltas.items():
                src_mask = (d8_arr == code) & stream
                sr, sc = np.where(src_mask)
                nr, nc = sr + dr, sc + dc
                ok = ((nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
                      & stream[nr, nc])
                np.add.at(inflow, (nr[ok], nc[ok]), 1)

            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)
            self._delete_shapefile(out_shp)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            vec_ds = drv.CreateDataSource(os.path.normpath(out_shp))
            lyr = vec_ds.CreateLayer("streams", srs=srs,
                                     geom_type=_ogr.wkbLineString)
            lyr.CreateField(_ogr.FieldDefn("ID", _ogr.OFTInteger))

            visited = np.zeros((rows, cols), dtype=bool)
            fid = 1

            def trace_from(start_r, start_c):
                nonlocal fid
                if visited[start_r, start_c]:
                    return
                path = [(start_r, start_c)]
                visited[start_r, start_c] = True
                r, c = start_r, start_c
                while True:
                    code = int(d8_arr[r, c])
                    if code not in d8_deltas:
                        break
                    dr, dc = d8_deltas[code]
                    nr, nc = r+dr, c+dc
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        break
                    if not stream[nr, nc]:
                        break
                    path.append((nr, nc))
                    if visited[nr, nc]:
                        break  # hit already-traced cell — stop here
                    visited[nr, nc] = True
                    r, c = nr, nc
                if len(path) >= 2:
                    line = _ogr.Geometry(_ogr.wkbLineString)
                    for rr, cc in path:
                        line.AddPoint(*rc_to_xy(rr, cc))
                    feat = _ogr.Feature(lyr.GetLayerDefn())
                    feat.SetGeometry(line)
                    feat.SetField("ID", fid)
                    lyr.CreateFeature(feat)
                    fid += 1

            # Trace from headwaters first (inflow == 0 = no upstream stream cell)
            hw_r, hw_c = np.where(stream & (inflow == 0))
            for r, c in zip(hw_r.tolist(), hw_c.tolist()):
                trace_from(r, c)
            # Then any remaining unvisited (loops, disconnected)
            all_r, all_c = np.where(stream & ~visited)
            for r, c in zip(all_r.tolist(), all_c.tolist()):
                trace_from(r, c)

            vec_ds.FlushCache()
            vec_ds = None
            self._log(f"  Stream lines: {fid-1} segments", "INFO")
            return out_shp if fid > 1 and os.path.exists(out_shp) else None
        except Exception as exc:
            self._log(f"  _vectorize_streams_with_d8: {exc}", "WARNING")
            return None

    def _vectorize_streams_gdal(self, streams_raster, streams_shp):
        """
        Convert stream raster to line shapefile using GDAL Polygonize (C-level, fast).
        Polygonizes stream cells, then extracts polygon boundaries as lines.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

            ds = _gdal.Open(os.path.normpath(streams_raster))
            if ds is None:
                return None
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
            arr = ds.GetRasterBand(1).ReadAsArray()
            nd = ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = arr.shape
            ds = None

            if nd is not None:
                stream = ((arr != nd) & (arr > 0)).astype(np.uint8)
            else:
                stream = (arr > 0).astype(np.uint8)

            n_cells = int(stream.sum())
            self._log(f"  Stream raster: {n_cells} stream cells", "INFO")
            if n_cells == 0:
                return None

            # Write stream mask to MEM raster for Polygonize
            mem_drv = _gdal.GetDriverByName("MEM")
            mask_ds = mem_drv.Create("", cols, rows, 1, _gdal.GDT_Byte)
            mask_ds.SetGeoTransform(gt)
            mask_ds.SetProjection(proj)
            mask_ds.GetRasterBand(1).WriteArray(stream)
            mask_band = mask_ds.GetRasterBand(1)

            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)

            # Polygonize (C-level, fast)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            tmp_shp = os.path.normpath(streams_shp.replace(".shp", "_tmp.shp"))
            self._delete_shapefile(tmp_shp)
            poly_ds = drv.CreateDataSource(tmp_shp)
            poly_lyr = poly_ds.CreateLayer("s", srs=srs,
                                           geom_type=_ogr.wkbPolygon)
            poly_lyr.CreateField(_ogr.FieldDefn("V", _ogr.OFTInteger))
            _gdal.Polygonize(mask_band, mask_band, poly_lyr, 0)
            poly_ds.FlushCache()
            n_polys = poly_lyr.GetFeatureCount()
            poly_ds = None
            mask_ds = None
            self._log(f"  Stream polygons: {n_polys}", "INFO")

            if n_polys == 0:
                self._delete_shapefile(tmp_shp)
                return None

            # Convert polygon boundaries to line strings
            self._delete_shapefile(streams_shp)
            line_ds = drv.CreateDataSource(os.path.normpath(streams_shp))
            line_lyr = line_ds.CreateLayer("streams", srs=srs,
                                           geom_type=_ogr.wkbLineString)
            line_lyr.CreateField(_ogr.FieldDefn("ID", _ogr.OFTInteger))

            poly_ds2 = _ogr.Open(tmp_shp)
            fid = 1
            if poly_ds2:
                p_lyr = poly_ds2.GetLayer(0)
                for feat in p_lyr:
                    geom = feat.GetGeometryRef()
                    if geom is None:
                        continue
                    boundary = geom.Boundary()
                    if boundary is None:
                        continue
                    lf = _ogr.Feature(line_lyr.GetLayerDefn())
                    lf.SetGeometry(boundary)
                    lf.SetField("ID", fid)
                    line_lyr.CreateFeature(lf)
                    fid += 1
                poly_ds2 = None

            line_ds.FlushCache()
            line_ds = None
            self._delete_shapefile(tmp_shp)
            self._log(f"  Stream lines: {fid-1} features", "INFO")

            if os.path.exists(streams_shp) and os.path.getsize(streams_shp) > 100:
                return streams_shp
            return None
        except Exception as exc:
            self._log(f"  vectorize_streams error: {exc}", "WARNING")
            return None

    def _write_single_point_shp(self, x, y, template_shp, out_shp):
        """Write a single point to a shapefile, using the CRS from template_shp."""
        try:
            from osgeo import ogr as _ogr, osr as _osr
            # Get CRS from template
            srs = _osr.SpatialReference()
            t_ds = _ogr.Open(os.path.normpath(template_shp))
            if t_ds:
                srs = t_ds.GetLayer(0).GetSpatialRef()
                t_ds = None
            self._delete_shapefile(out_shp)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            ds = drv.CreateDataSource(os.path.normpath(out_shp))
            lyr = ds.CreateLayer("outlet", srs=srs, geom_type=_ogr.wkbPoint)
            lyr.CreateField(_ogr.FieldDefn("id", _ogr.OFTInteger))
            feat = _ogr.Feature(lyr.GetLayerDefn())
            pt = _ogr.Geometry(_ogr.wkbPoint)
            pt.AddPoint(x, y)
            feat.SetGeometry(pt)
            feat.SetField("id", 1)
            lyr.CreateFeature(feat)
            ds.FlushCache()
            ds = None
            return True
        except Exception as exc:
            self._log(f"  write_single_point warning: {exc}", "WARNING")
            return False

    def _delineate_watershed_from_outlet(self, accum_raster, d8_raster,
                                         ox, oy, out_tif):
        """
        Fallback watershed delineation using upstream D8 BFS from outlet.
        Writes a binary 1/0 raster to out_tif.
        """
        try:
            import numpy as np
            from collections import deque
            from osgeo import gdal as _gdal
            ds = _gdal.Open(os.path.normpath(d8_raster))
            if ds is None:
                return False
            gt = ds.GetGeoTransform()
            proj = ds.GetProjection()
            d8_raw = ds.GetRasterBand(1).ReadAsArray()
            d8_nd = ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = d8_raw.shape
            ds = None
            d8_arr = np.abs(d8_raw.astype(np.int32))
            if d8_nd is not None:
                d8_arr[np.abs(d8_raw.astype(np.float64) - d8_nd) < 0.5] = 0
            # WBT D8 outflow encoding
            d8_deltas = {
                1: (0, 1), 2: (-1, 1), 4: (-1, 0), 8: (-1, -1),
                16: (0, -1), 32: (1, -1), 64: (1, 0), 128: (1, 1),
            }
            c0 = int((ox - gt[0]) / gt[1])
            r0 = int((oy - gt[3]) / gt[5])
            if not (0 <= r0 < rows and 0 <= c0 < cols):
                return False
            result = np.zeros((rows, cols), dtype=np.int32)
            q = deque()
            q.append((r0, c0))
            result[r0, c0] = 1
            while q:
                r, c = q.popleft()
                for code, (dr, dc) in d8_deltas.items():
                    ur, uc = r - dr, c - dc
                    if not (0 <= ur < rows and 0 <= uc < cols):
                        continue
                    if result[ur, uc]:
                        continue
                    if int(d8_arr[ur, uc]) == code:
                        result[ur, uc] = 1
                        q.append((ur, uc))
            drv = _gdal.GetDriverByName("GTiff")
            out_ds = drv.Create(os.path.normpath(out_tif),
                                cols, rows, 1, _gdal.GDT_Int32)
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            b = out_ds.GetRasterBand(1)
            b.SetNoDataValue(0)
            b.WriteArray(result)
            out_ds.FlushCache()
            out_ds = None
            return True
        except Exception as exc:
            self._log(f"  delineate_watershed_from_outlet error: {exc}", "WARNING")
            return False


    def _snap_outlet_to_stream_gdal(self, outlet_shp, streams_raster,
                                     output_shp, max_dist_m):
        """
        Snap outlet point(s) to nearest stream cell within max_dist_m.
        Uses GDAL/numpy — no GRASS required.
        Returns True on success.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

            # Read stream raster
            stream_ds = _gdal.Open(streams_raster)
            if stream_ds is None:
                return False
            gt = stream_ds.GetGeoTransform()
            stream_arr = stream_ds.GetRasterBand(1).ReadAsArray()
            nd = stream_ds.GetRasterBand(1).GetNoDataValue()
            srs_wkt = stream_ds.GetProjection()
            stream_ds = None

            cell_x = abs(gt[1])
            cell_y = abs(gt[5])
            radius_cells_x = int(max_dist_m / cell_x) + 2
            radius_cells_y = int(max_dist_m / cell_y) + 2

            # Get stream cell indices
            if nd is not None:
                stream_mask = (stream_arr != nd) & (stream_arr > 0)
            else:
                stream_mask = stream_arr > 0
            stream_rows, stream_cols = np.where(stream_mask)

            if stream_rows.size == 0:
                self._log("  No stream cells found for snapping.", "WARNING")
                return False

            # Build stream cell coordinates
            stream_xs = gt[0] + (stream_cols + 0.5) * gt[1]
            stream_ys = gt[3] + (stream_rows + 0.5) * gt[5]

            # Read outlet points
            out_ds = _ogr.Open(outlet_shp)
            if out_ds is None:
                return False
            out_lyr = out_ds.GetLayer(0)

            srs = _osr.SpatialReference()
            srs.ImportFromWkt(srs_wkt)

            drv = _ogr.GetDriverByName("ESRI Shapefile")
            self._delete_shapefile(output_shp)
            snap_ds = drv.CreateDataSource(output_shp)
            snap_lyr = snap_ds.CreateLayer("snapped", srs=srs,
                                           geom_type=_ogr.wkbPoint)
            # Copy fields
            defn = out_lyr.GetLayerDefn()
            for j in range(defn.GetFieldCount()):
                snap_lyr.CreateField(defn.GetFieldDefn(j))

            for feat in out_lyr:
                geom = feat.GetGeometryRef()
                if geom is None:
                    continue
                px, py = geom.GetX(), geom.GetY()

                # Filter to candidate cells within bounding box
                col0 = int((px - gt[0]) / gt[1])
                row0 = int((py - gt[3]) / gt[5])
                r_lo = max(0, row0 - radius_cells_y)
                r_hi = min(stream_arr.shape[0] - 1, row0 + radius_cells_y)
                c_lo = max(0, col0 - radius_cells_x)
                c_hi = min(stream_arr.shape[1] - 1, col0 + radius_cells_x)

                mask = (stream_rows >= r_lo) & (stream_rows <= r_hi) & \
                       (stream_cols >= c_lo) & (stream_cols <= c_hi)
                cand_x = stream_xs[mask]
                cand_y = stream_ys[mask]

                if cand_x.size == 0:
                    snap_x, snap_y = px, py
                    self._log("  No nearby stream cell — using original point.", "INFO")
                else:
                    dists = np.hypot(cand_x - px, cand_y - py)
                    nearest = np.argmin(dists)
                    snap_x = float(cand_x[nearest])
                    snap_y = float(cand_y[nearest])
                    self._log(
                        f"  Snapped {px:.1f},{py:.1f} → {snap_x:.1f},{snap_y:.1f} "
                        f"(d={dists[nearest]:.1f} m)", "INFO")

                new_feat = _ogr.Feature(snap_lyr.GetLayerDefn())
                pt = _ogr.Geometry(_ogr.wkbPoint)
                pt.AddPoint(snap_x, snap_y)
                new_feat.SetGeometry(pt)
                for j in range(defn.GetFieldCount()):
                    new_feat.SetField(j, feat.GetField(j))
                snap_lyr.CreateFeature(new_feat)

            snap_ds.FlushCache()
            snap_ds = None
            out_ds = None
            return True

        except Exception as exc:
            self._log(f"  Snap GDAL error: {exc}", "WARNING")
            return False

    def _polygonize_binary_raster(self, raster_path, out_shp, value=1):
        """
        Polygonize cells == value in a raster into a shapefile.
        Uses GDAL Polygonize. Returns True on success.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")
            raster_path = os.path.normpath(raster_path)
            out_shp = os.path.normpath(out_shp)
            ds = _gdal.Open(raster_path)
            if ds is None:
                return False
            band = ds.GetRasterBand(1)
            proj = ds.GetProjection()
            nd_val = band.GetNoDataValue()
            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)

            # Create a mask band: 255 where value==target, 0 elsewhere
            arr = band.ReadAsArray()
            if nd_val is not None:
                valid = (arr == value) & (arr != int(nd_val))
            else:
                valid = arr == value
            mask_arr = np.where(valid, 255, 0).astype(np.uint8)
            mem_drv = _gdal.GetDriverByName("MEM")
            mask_ds = mem_drv.Create("", ds.RasterXSize, ds.RasterYSize, 1,
                                     _gdal.GDT_Byte)
            mask_ds.SetGeoTransform(ds.GetGeoTransform())
            mask_ds.SetProjection(proj)
            mask_ds.GetRasterBand(1).WriteArray(mask_arr)
            mask_band = mask_ds.GetRasterBand(1)
            ds = None

            drv = _ogr.GetDriverByName("ESRI Shapefile")
            if os.path.exists(out_shp):
                drv.DeleteDataSource(out_shp)
            vec_ds = drv.CreateDataSource(out_shp)
            lyr = vec_ds.CreateLayer("watershed", srs=srs,
                                     geom_type=_ogr.wkbPolygon)
            fd = _ogr.FieldDefn("VALUE", _ogr.OFTInteger)
            lyr.CreateField(fd)

            # Use mask_band so only target-value cells produce polygons
            _gdal.Polygonize(mask_band, mask_band, lyr, 0)
            mask_ds = None
            vec_ds.FlushCache()
            count = lyr.GetFeatureCount()
            vec_ds = None
            ds = None
            return count > 0
        except Exception as exc:
            self._log(f"  _polygonize_binary_raster error: {exc}", "WARNING")
            return False

    def _delineate_subbasins_from_streams(self, streams_raster, accum_raster,
                                          d8_raster, out_raster):
        """
        Delineate subbasins by assigning each non-stream cell to its nearest
        downstream stream cell. Each unique stream cell order segment = one basin.
        Uses D8 flow direction to trace each cell downstream until it hits a
        stream cell, then labels the cell with that stream cell's basin ID.
        Produces one subbasin per stream segment — matching WBT Subbasins output.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal

            # Load D8 pointer
            d8_ds  = _gdal.Open(os.path.normpath(d8_raster))
            acc_ds = _gdal.Open(os.path.normpath(accum_raster))
            st_ds  = _gdal.Open(os.path.normpath(streams_raster))
            if d8_ds is None or acc_ds is None or st_ds is None:
                return False
            gt   = acc_ds.GetGeoTransform()
            proj = acc_ds.GetProjection()
            d8   = d8_ds.GetRasterBand(1).ReadAsArray().astype(np.int32)
            d8nd = d8_ds.GetRasterBand(1).GetNoDataValue()
            acc  = acc_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
            st   = st_ds.GetRasterBand(1).ReadAsArray()
            stnd = st_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = d8.shape
            d8_ds = acc_ds = st_ds = None

            if d8nd is not None:
                d8[np.abs(d8.astype(np.float64) - d8nd) < 0.5] = 0
            acc[acc < 0] = 0

            # Stream mask
            if stnd is not None:
                stream = (st != stnd) & (st > 0)
            else:
                stream = st > 0

            # Auto-detect D8 encoding
            vals = set(np.unique(d8[d8 > 0]).tolist())
            wbt_set = {1,2,4,8,16,32,64,128}
            if vals.issubset(wbt_set):
                d8_deltas = {1:(0,1),2:(-1,1),4:(-1,0),8:(-1,-1),
                             16:(0,-1),32:(1,-1),64:(1,0),128:(1,1)}
            else:
                d8_deltas = {1:(0,1),2:(-1,1),3:(-1,0),4:(-1,-1),
                             5:(0,-1),6:(1,-1),7:(1,0),8:(1,1)}

            # Label stream cells: each gets a unique ID based on accumulation rank
            # Stream cells with inflow from other stream cells are junctions
            # Label by sorting stream cells by accumulation (headwaters first)
            stream_ids = np.zeros((rows, cols), dtype=np.int32)
            str_r, str_c = np.where(stream)
            str_acc = acc[str_r, str_c]
            order = np.argsort(str_acc)  # headwaters first

            # Check inflow from stream
            stream_inflow = np.zeros((rows, cols), dtype=np.int8)
            for code, (dr, dc) in d8_deltas.items():
                src_mask = (d8 == code) & stream
                sr, sc = np.where(src_mask)
                nr, nc = sr + dr, sc + dc
                ok = ((nr >= 0) & (nr < rows) & (nc >= 0) & (nc < cols)
                      & stream[nr, nc])
                np.add.at(stream_inflow, (nr[ok], nc[ok]), 1)

            # Each headwater or junction starts a new subbasin
            bid = 1
            for idx in order:
                r, c = int(str_r[idx]), int(str_c[idx])
                if stream_ids[r, c] == 0:
                    stream_ids[r, c] = bid
                    bid += 1

            # For junction cells (inflow > 1): keep existing ID, downstream
            # segment gets a new ID
            # Propagate IDs downstream along stream
            for idx in order:
                r, c = int(str_r[idx]), int(str_c[idx])
                code = int(d8[r, c])
                if code not in d8_deltas:
                    continue
                dr, dc = d8_deltas[code]
                nr, nc = r + dr, c + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    continue
                if stream[nr, nc] and stream_ids[nr, nc] == 0:
                    stream_ids[nr, nc] = stream_ids[r, c]

            # Now assign each non-stream cell to the stream cell it drains to
            basins = stream_ids.copy()
            # Process in order of decreasing accumulation (downstream first)
            all_r, all_c = np.where(~stream & (acc > 0))
            all_acc = acc[all_r, all_c]
            all_order = np.argsort(-all_acc)  # downstream first

            for idx in all_order:
                r, c = int(all_r[idx]), int(all_c[idx])
                # Trace downstream until we hit a stream or assigned cell
                curr_r, curr_c = r, c
                path = [(r, c)]
                for _ in range(rows + cols):
                    code = int(d8[curr_r, curr_c])
                    if code not in d8_deltas:
                        break
                    dr, dc = d8_deltas[code]
                    nr, nc = curr_r + dr, curr_c + dc
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        break
                    if stream_ids[nr, nc] > 0 or basins[nr, nc] > 0:
                        target_id = stream_ids[nr, nc] or basins[nr, nc]
                        for pr, pc in path:
                            basins[pr, pc] = target_id
                        break
                    path.append((nr, nc))
                    curr_r, curr_c = nr, nc

            n_basins = len(np.unique(basins[basins > 0]))
            self._log(f"  Subbasins: {n_basins} unique basins created", "INFO")
            if n_basins == 0:
                return False

            # Write output
            drv = _gdal.GetDriverByName("GTiff")
            out_ds = drv.Create(os.path.normpath(out_raster),
                                cols, rows, 1, _gdal.GDT_Int32,
                                options=["COMPRESS=LZW"])
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            out_ds.GetRasterBand(1).SetNoDataValue(0)
            out_ds.GetRasterBand(1).WriteArray(basins)
            out_ds.FlushCache()
            out_ds = None
            return True

        except Exception as exc:
            self._log(f"  _delineate_subbasins_from_streams error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    def _compute_lfp_per_subbasin(self, accum_raster, subbasins_raster,
                                   out_shp):
        """
        Compute Longest Flow Path for every subbasin independently.
        Replicates WBT LongestFlowpath --dem --basins behaviour.
        For each unique basin ID in subbasins_raster:
          1. Restrict accumulation to that basin's cells only
          2. Find the outlet cell = cell with MAX accumulation touching basin edge
          3. Trace upstream from outlet following MAX acc neighbours within basin
          4. Write as one LineString feature with BASIN field = basin ID
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

            # Load accumulation
            acc_ds = _gdal.Open(os.path.normpath(accum_raster))
            if acc_ds is None:
                self._log("  LFP/basin: cannot open accumulation raster", "WARNING")
                return False
            gt   = acc_ds.GetGeoTransform()
            proj = acc_ds.GetProjection()
            cell_x = abs(gt[1])
            cell_y = abs(gt[5])
            acc = acc_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
            acc_nd = acc_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = acc.shape
            acc_ds = None
            if acc_nd is not None:
                acc[np.abs(acc - acc_nd) < 1.0] = 0
            acc[acc < 0] = 0
            acc[~np.isfinite(acc)] = 0

            # Load subbasins
            sb_ds = _gdal.Open(os.path.normpath(subbasins_raster))
            if sb_ds is None:
                self._log("  LFP/basin: cannot open subbasin raster", "WARNING")
                return False
            sb_arr = sb_ds.GetRasterBand(1).ReadAsArray().astype(np.int32)
            sb_nd  = sb_ds.GetRasterBand(1).GetNoDataValue()
            sb_ds  = None
            if sb_nd is not None:
                sb_arr[np.abs(sb_arr.astype(np.float64) - sb_nd) < 0.5] = 0

            basin_ids = [int(v) for v in np.unique(sb_arr) if v > 0]
            self._log(f"  LFP/basin: {len(basin_ids)} basins", "INFO")
            if not basin_ids:
                return False

            def rc_to_xy(r, c):
                return (gt[0]+(c+0.5)*gt[1], gt[3]+(r+0.5)*gt[5])

            adj8 = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
            diag_set = {(-1,-1),(1,-1),(-1,1),(1,1)}

            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)
            self._delete_shapefile(out_shp)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            vec_ds = drv.CreateDataSource(os.path.normpath(out_shp))
            lyr = vec_ds.CreateLayer("lfp", srs=srs,
                                     geom_type=_ogr.wkbLineString)
            lyr.CreateField(_ogr.FieldDefn("BASIN",     _ogr.OFTInteger))
            lyr.CreateField(_ogr.FieldDefn("LENGTH",    _ogr.OFTReal))
            lyr.CreateField(_ogr.FieldDefn("LFP_CELLS", _ogr.OFTInteger))

            n_written = 0
            for bid in basin_ids:
                basin_mask = sb_arr == bid
                basin_acc  = np.where(basin_mask, acc, 0.0)

                if basin_acc.max() == 0:
                    continue

                # Outlet = cell with MAX accumulation in this basin
                flat_idx = np.argmax(basin_acc)
                outlet_r, outlet_c = np.unravel_index(flat_idx, acc.shape)

                # Trace upstream within this basin
                trace = [(int(outlet_r), int(outlet_c))]
                r, c = int(outlet_r), int(outlet_c)
                visited = {(r, c)}
                total_len = 0.0

                for _ in range(int(basin_mask.sum()) + 10):
                    cur_acc = float(acc[r, c])
                    candidates = []
                    for dr, dc in adj8:
                        nr, nc = r+dr, c+dc
                        if not (0 <= nr < rows and 0 <= nc < cols):
                            continue
                        if (nr, nc) in visited:
                            continue
                        if not basin_mask[nr, nc]:
                            continue
                        nb_acc = float(acc[nr, nc])
                        if 0 < nb_acc < cur_acc:
                            candidates.append((nb_acc, nr, nc))
                    if not candidates:
                        break
                    candidates.sort(key=lambda x: -x[0])
                    _, nr, nc = candidates[0]
                    step = ((cell_x**2+cell_y**2)**0.5
                            if (nr-r, nc-c) in diag_set
                            else (cell_x if nc != c else cell_y))
                    total_len += step
                    r, c = nr, nc
                    visited.add((r, c))
                    trace.append((r, c))

                if len(trace) < 2:
                    continue

                trace.reverse()  # headwater → outlet
                line = _ogr.Geometry(_ogr.wkbLineString)
                for rr, cc in trace:
                    line.AddPoint(*rc_to_xy(rr, cc))
                feat = _ogr.Feature(lyr.GetLayerDefn())
                feat.SetGeometry(line)
                feat.SetField("BASIN",     bid)
                feat.SetField("LENGTH",    total_len)
                feat.SetField("LFP_CELLS", len(trace))
                lyr.CreateFeature(feat)
                n_written += 1

            vec_ds.FlushCache()
            vec_ds = None
            self._log(f"  LFP/basin: {n_written} paths written", "INFO")
            return n_written > 0

        except Exception as exc:
            self._log(f"  _compute_lfp_per_subbasin error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    def _compute_lfp_upstream_from_outlet(self, accum_raster, streams_raster,
                                          outlet_coords, out_shp,
                                          watershed_raster=None):
        """
        Compute Longest Flow Path for each outlet independently.
        Each outlet gets its own LFP traced upstream, written as a separate
        feature in the output shapefile.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

            acc_ds = _gdal.Open(os.path.normpath(accum_raster))
            if acc_ds is None:
                self._log("  LFP: cannot open accumulation raster", "WARNING")
                return False
            gt     = acc_ds.GetGeoTransform()
            proj   = acc_ds.GetProjection()
            cell_x = abs(gt[1])
            cell_y = abs(gt[5])
            acc_raw = acc_ds.GetRasterBand(1).ReadAsArray()
            acc_nd  = acc_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = acc_raw.shape
            acc_ds = None

            acc = acc_raw.astype(np.float64)
            if acc_nd is not None:
                acc[np.abs(acc - acc_nd) < 1.0] = 0
            acc[acc < 0] = 0
            acc[~np.isfinite(acc)] = 0

            self._log(
                f"  LFP: acc grid {rows}x{cols}, max={float(acc.max()):.0f}",
                "INFO")

            # Build watershed mask (per-outlet if multiple polygons)
            ws_arr = None
            if watershed_raster and os.path.exists(os.path.normpath(watershed_raster)):
                try:
                    ws_ds = _gdal.Open(os.path.normpath(watershed_raster))
                    if ws_ds:
                        _arr = ws_ds.GetRasterBand(1).ReadAsArray()
                        _nd  = ws_ds.GetRasterBand(1).GetNoDataValue()
                        ws_ds = None
                        if _arr.shape == acc.shape:
                            ws_arr = _arr
                except Exception as _we:
                    self._log(f"  LFP ws_arr: {_we}", "INFO")

            def xy_to_rc(x, y):
                c = int((x - gt[0]) / gt[1])
                r = int((y - gt[3]) / gt[5])
                return (max(0, min(rows-1, r)), max(0, min(cols-1, c)))

            def rc_to_xy(r, c):
                return (gt[0]+(c+0.5)*gt[1], gt[3]+(r+0.5)*gt[5])

            adj8 = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
            diag_set = {(-1,-1),(1,-1),(-1,1),(1,1)}
            search_r = max(10, int(200 / cell_x))

            def find_outlet_cell(ox, oy, ws_mask):
                """Find best outlet cell near (ox,oy) with upstream neighbours."""
                r0, c0 = xy_to_rc(ox, oy)
                r_lo = max(0, r0 - search_r)
                r_hi = min(rows-1, r0 + search_r)
                c_lo = max(0, c0 - search_r)
                c_hi = min(cols-1, c0 + search_r)
                sub_acc = acc[r_lo:r_hi+1, c_lo:c_hi+1]
                if ws_mask is not None:
                    valid = ws_mask[r_lo:r_hi+1, c_lo:c_hi+1] & (sub_acc > 0)
                else:
                    valid = sub_acc > 0
                if not valid.any():
                    # widen search
                    valid = sub_acc > 0
                if not valid.any():
                    return None
                masked = np.where(valid, sub_acc, 0.0)
                cand_r, cand_c = np.where(masked > 0)
                cand_acc = masked[cand_r, cand_c]
                order = np.argsort(-cand_acc)
                best_r, best_c, best_score = r0, c0, -1
                for idx in order[:50]:
                    gr = r_lo + int(cand_r[idx])
                    gc = c_lo + int(cand_c[idx])
                    cur = float(acc[gr, gc])
                    n_up = sum(1 for dr, dc in adj8
                               if 0 <= gr+dr < rows and 0 <= gc+dc < cols
                               and 0 < float(acc[gr+dr, gc+dc]) < cur)
                    score = cur * (1 + n_up)
                    if n_up > 0 and score > best_score:
                        best_score = score
                        best_r, best_c = gr, gc
                if best_score < 0:
                    # fallback: max acc cell
                    fl = np.argmax(masked)
                    lr, lc = np.unravel_index(fl, masked.shape)
                    best_r, best_c = r_lo+int(lr), c_lo+int(lc)
                return (best_r, best_c)

            def trace_upstream(outlet_r, outlet_c, ws_mask):
                """Trace upstream from outlet, return (trace, length_m)."""
                trace = [(outlet_r, outlet_c)]
                r, c = outlet_r, outlet_c
                visited = {(r, c)}
                total_len = 0.0
                for _ in range(rows * cols):
                    cur_acc = float(acc[r, c])
                    candidates = []
                    for dr, dc in adj8:
                        nr, nc = r+dr, c+dc
                        if not (0 <= nr < rows and 0 <= nc < cols):
                            continue
                        if (nr, nc) in visited:
                            continue
                        if ws_mask is not None and not ws_mask[nr, nc]:
                            continue
                        nb_acc = float(acc[nr, nc])
                        if 0 < nb_acc < cur_acc:
                            candidates.append((nb_acc, nr, nc))
                    if not candidates:
                        break
                    candidates.sort(key=lambda x: -x[0])
                    _, nr, nc = candidates[0]
                    step = ((cell_x**2+cell_y**2)**0.5
                            if (nr-r, nc-c) in diag_set
                            else (cell_x if nc != c else cell_y))
                    total_len += step
                    r, c = nr, nc
                    visited.add((r, c))
                    trace.append((r, c))
                trace.reverse()
                return trace, total_len

            # Build output shapefile
            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            self._delete_shapefile(out_shp)
            vec_ds = drv.CreateDataSource(out_shp)
            lyr = vec_ds.CreateLayer("lfp", srs=srs,
                                     geom_type=_ogr.wkbLineString)
            lyr.CreateField(_ogr.FieldDefn("OUTLET_ID", _ogr.OFTInteger))
            lyr.CreateField(_ogr.FieldDefn("LENGTH",    _ogr.OFTReal))
            lyr.CreateField(_ogr.FieldDefn("LFP_CELLS", _ogr.OFTInteger))

            n_written = 0
            for idx, (ox, oy) in enumerate(outlet_coords):
                outlet_id = idx + 1

                # Build per-outlet watershed mask if ws_arr has unique IDs
                ws_mask = None
                if ws_arr is not None:
                    unique_ids = np.unique(ws_arr[ws_arr > 0])
                    if len(unique_ids) > 1:
                        # Multi-outlet: each outlet has its own ID
                        ws_mask = ws_arr == outlet_id
                    else:
                        # Single merged watershed
                        ws_mask = ws_arr > 0
                    if ws_mask is not None and int(ws_mask.sum()) < 10:
                        ws_mask = (ws_arr > 0)  # fallback to full mask

                # Find outlet cell
                cell = find_outlet_cell(ox, oy, ws_mask)
                if cell is None:
                    self._log(f"  LFP outlet {outlet_id}: no valid cell found",
                              "WARNING")
                    continue
                outlet_r, outlet_c = cell
                self._log(
                    f"  LFP outlet {outlet_id}: cell ({outlet_r},{outlet_c}), "
                    f"acc={float(acc[outlet_r,outlet_c]):.0f}", "INFO")

                trace, total_len = trace_upstream(outlet_r, outlet_c, ws_mask)
                self._log(
                    f"  LFP outlet {outlet_id}: {len(trace)} cells, "
                    f"{total_len:.1f}m", "INFO")

                if len(trace) < 2:
                    self._log(f"  LFP outlet {outlet_id}: too short, skipped.",
                              "WARNING")
                    continue

                line = _ogr.Geometry(_ogr.wkbLineString)
                for rr, cc in trace:
                    line.AddPoint(*rc_to_xy(rr, cc))
                feat = _ogr.Feature(lyr.GetLayerDefn())
                feat.SetGeometry(line)
                feat.SetField("OUTLET_ID", outlet_id)
                feat.SetField("LENGTH",    total_len)
                feat.SetField("LFP_CELLS", len(trace))
                lyr.CreateFeature(feat)
                n_written += 1

            vec_ds.FlushCache()
            vec_ds = None

            if n_written == 0:
                self._log("  LFP: no paths written.", "WARNING")
                return False
            self._log(f"  LFP: {n_written} path(s) written.", "SUCCESS")
            return True

        except Exception as exc:
            self._log(f"  LFP error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False


        """
        Compute Longest Flow Path by tracing UPSTREAM from the outlet.

        Finds the outlet cell (highest accumulation near clicked point inside
        watershed), then walks upstream always choosing the neighbour with the
        highest accumulation that is less than the current cell.
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

            acc_ds = _gdal.Open(os.path.normpath(accum_raster))
            if acc_ds is None:
                self._log("  LFP: cannot open accumulation raster", "WARNING")
                return False
            gt     = acc_ds.GetGeoTransform()
            proj   = acc_ds.GetProjection()
            cell_x = abs(gt[1])
            cell_y = abs(gt[5])
            acc_raw = acc_ds.GetRasterBand(1).ReadAsArray()
            acc_nd  = acc_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = acc_raw.shape
            acc_ds = None

            acc = acc_raw.astype(np.float64)
            if acc_nd is not None:
                acc[np.abs(acc - acc_nd) < 1.0] = 0
            acc[acc < 0] = 0
            acc[~np.isfinite(acc)] = 0

            self._log(
                f"  LFP: acc grid {rows}x{cols}, max={float(acc.max()):.0f}",
                "INFO")

            # Build watershed mask
            ws_mask = None
            if watershed_raster and os.path.exists(os.path.normpath(watershed_raster)):
                try:
                    ws_ds = _gdal.Open(os.path.normpath(watershed_raster))
                    if ws_ds:
                        ws_arr = ws_ds.GetRasterBand(1).ReadAsArray()
                        ws_nd  = ws_ds.GetRasterBand(1).GetNoDataValue()
                        ws_ds  = None
                        if ws_arr.shape == acc.shape:
                            ws_mask = (ws_arr != ws_nd) & (ws_arr > 0)                                 if ws_nd is not None else ws_arr > 0
                            n_ws = int(ws_mask.sum())
                            self._log(f"  LFP: watershed mask {n_ws} cells", "INFO")
                            if n_ws < 10:
                                ws_mask = None
                except Exception as _we:
                    self._log(f"  LFP watershed mask: {_we}", "INFO")
                    ws_mask = None

            diag_set = {(-1,-1),(1,-1),(-1,1),(1,1)}

            # ── Find best outlet cell ──────────────────────────────────────
            # Strategy: in the search radius around each outlet coordinate,
            # find the stream cell that has the MOST upstream neighbours
            # with lower accumulation — this is the true mouth of the stream.
            search_r = max(10, int(200 / cell_x))

            def count_upstream_nbs(r, c):
                """Count 8-neighbours with acc < acc[r,c] and acc > 0."""
                cur = float(acc[r, c])
                if cur <= 0:
                    return 0
                count = 0
                for dr, dc in adj8:
                    nr, nc = r+dr, c+dc
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        continue
                    nb = float(acc[nr, nc])
                    if 0 < nb < cur:
                        count += 1
                return count

            outlet_r, outlet_c = 0, 0
            best_score = -1

            for ox, oy in outlet_coords:
                r0, c0 = xy_to_rc(ox, oy)
                r_lo = max(0, r0 - search_r)
                r_hi = min(rows-1, r0 + search_r)
                c_lo = max(0, c0 - search_r)
                c_hi = min(cols-1, c0 + search_r)
                sub_acc = acc[r_lo:r_hi+1, c_lo:c_hi+1]

                # Only consider cells inside watershed (if mask available)
                if ws_mask is not None:
                    sub_ws = ws_mask[r_lo:r_hi+1, c_lo:c_hi+1]
                    valid = sub_ws & (sub_acc > 0)
                else:
                    valid = sub_acc > 0

                if not valid.any():
                    continue

                # Among valid cells, find the one with highest accumulation
                # that also has at least one upstream neighbour
                masked = np.where(valid, sub_acc, 0.0)
                # Sort candidate positions by accumulation descending
                cand_r, cand_c = np.where(masked > 0)
                cand_acc = masked[cand_r, cand_c]
                order = np.argsort(-cand_acc)

                for idx in order[:50]:  # check top 50 candidates
                    gr = r_lo + int(cand_r[idx])
                    gc = c_lo + int(cand_c[idx])
                    n_up = count_upstream_nbs(gr, gc)
                    # Score = accumulation value weighted by having upstreams
                    score = float(acc[gr, gc]) * (1 + n_up)
                    if n_up > 0 and score > best_score:
                        best_score = score
                        outlet_r, outlet_c = gr, gc

            # Fallback: just use maximum accumulation near outlet
            if best_score < 0:
                for ox, oy in outlet_coords:
                    r0, c0 = xy_to_rc(ox, oy)
                    r_lo = max(0, r0 - search_r)
                    r_hi = min(rows-1, r0 + search_r)
                    c_lo = max(0, c0 - search_r)
                    c_hi = min(cols-1, c0 + search_r)
                    sub = acc[r_lo:r_hi+1, c_lo:c_hi+1]
                    if sub.max() > 0:
                        fl = np.argmax(sub)
                        lr, lc = np.unravel_index(fl, sub.shape)
                        outlet_r, outlet_c = r_lo+int(lr), c_lo+int(lc)

            self._log(
                f"  LFP: outlet cell ({outlet_r},{outlet_c}), "
                f"acc={float(acc[outlet_r,outlet_c]):.0f}", "INFO")

            # ── Trace upstream from outlet ─────────────────────────────────
            trace = [(outlet_r, outlet_c)]
            r, c = outlet_r, outlet_c
            visited = set()
            visited.add((r, c))
            total_len = 0.0

            for _ in range(rows * cols):
                cur_acc = float(acc[r, c])
                candidates = []
                for dr, dc in adj8:
                    nr, nc = r+dr, c+dc
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        continue
                    if (nr, nc) in visited:
                        continue
                    if ws_mask is not None and not ws_mask[nr, nc]:
                        continue
                    nb_acc = float(acc[nr, nc])
                    if 0 < nb_acc < cur_acc:
                        candidates.append((nb_acc, nr, nc))
                if not candidates:
                    break
                candidates.sort(key=lambda x: -x[0])
                nb_acc, nr, nc = candidates[0]
                step = ((cell_x**2+cell_y**2)**0.5
                        if (nr-r, nc-c) in diag_set
                        else (cell_x if nc != c else cell_y))
                total_len += step
                r, c = nr, nc
                visited.add((r, c))
                trace.append((r, c))

            self._log(
                f"  LFP: {len(trace)} cells, {total_len:.1f}m", "INFO")
            if len(trace) < 2:
                self._log("  LFP: trace too short.", "WARNING")
                return False

            trace.reverse()  # headwater → outlet

            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            self._delete_shapefile(out_shp)
            vec_ds = drv.CreateDataSource(out_shp)
            lyr = vec_ds.CreateLayer("lfp", srs=srs,
                                     geom_type=_ogr.wkbLineString)
            lyr.CreateField(_ogr.FieldDefn("BASIN",     _ogr.OFTInteger))
            lyr.CreateField(_ogr.FieldDefn("LENGTH",    _ogr.OFTReal))
            lyr.CreateField(_ogr.FieldDefn("LFP_CELLS", _ogr.OFTInteger))
            line = _ogr.Geometry(_ogr.wkbLineString)
            for rr, cc in trace:
                line.AddPoint(*rc_to_xy(rr, cc))
            feat = _ogr.Feature(lyr.GetLayerDefn())
            feat.SetGeometry(line)
            feat.SetField("BASIN",     1)
            feat.SetField("LENGTH",    total_len)
            feat.SetField("LFP_CELLS", len(trace))
            lyr.CreateFeature(feat)
            vec_ds.FlushCache()
            vec_ds = None
            self._log(f"  LFP: {len(trace)} cells, {total_len:.1f}m", "SUCCESS")
            return True

        except Exception as exc:
            self._log(f"  LFP error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    def _compute_lfp_from_accumulation(self, accum_raster, streams_raster,
                                        watershed_raster, out_shp):
        """
        Compute Longest Flow Path using flow accumulation raster.

        Correct algorithm (per RASHMS/OCWGIS tutorials):
        1. Load accumulation raster — use the watershed_raster to build ws_mask
        2. Find all STREAM cells inside watershed (accum >= threshold is stream,
           but we use the stream raster directly)
        3. Head = stream cell with MINIMUM accumulation inside watershed
           (= furthest upstream headwater of the longest flow path)
        4. Trace DOWNSTREAM: at each step move to the neighbour with the
           HIGHEST accumulation among all 8 neighbours still inside watershed.
           This always moves toward the outlet.

        Key fix: both the accum and watershed rasters are on the FULL DEM grid.
        The watershed raster uses nodata=0 and value=1.
        We build ws_mask as arr==1, not arr>0 (avoids nodata issues with 0).
        """
        try:
            import numpy as np
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

            # Open accumulation raster
            acc_ds = _gdal.Open(os.path.normpath(accum_raster))
            if acc_ds is None:
                self._log("  LFP: cannot open accumulation raster", "WARNING")
                return False
            gt   = acc_ds.GetGeoTransform()
            proj = acc_ds.GetProjection()
            cell_x = abs(gt[1])
            cell_y = abs(gt[5])
            acc_arr = acc_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
            acc_nd  = acc_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = acc_arr.shape
            acc_ds = None

            # Zero out nodata and negatives in accumulation
            if acc_nd is not None:
                acc_arr[np.abs(acc_arr - acc_nd) < 1.0] = 0
            acc_arr[acc_arr < 0] = 0
            acc_arr[~np.isfinite(acc_arr)] = 0

            self._log(
                f"  LFP: accum grid {rows}x{cols}, "
                f"max={float(acc_arr.max()):.0f}", "INFO")

            # Build watershed mask from watershed raster
            # watershed raster: nodata=0, value=1 (written by our merge code)
            ws_mask = None
            if watershed_raster and os.path.exists(os.path.normpath(watershed_raster)):
                ws_ds = _gdal.Open(os.path.normpath(watershed_raster))
                if ws_ds is not None:
                    ws_arr_raw = ws_ds.GetRasterBand(1).ReadAsArray()
                    ws_nd_raw  = ws_ds.GetRasterBand(1).GetNoDataValue()
                    ws_rows = ws_ds.RasterYSize
                    ws_cols = ws_ds.RasterXSize
                    ws_gt   = ws_ds.GetGeoTransform()
                    ws_ds   = None

                    if ws_rows == rows and ws_cols == cols and ws_gt == gt:
                        # Same grid — direct use
                        if ws_nd_raw is not None:
                            ws_mask = (ws_arr_raw != ws_nd_raw) & (ws_arr_raw > 0)
                        else:
                            ws_mask = ws_arr_raw > 0
                    else:
                        # Different grid — resample watershed to accum grid
                        self._log(
                            f"  LFP: resampling ws {ws_rows}x{ws_cols} "
                            f"-> {rows}x{cols}", "INFO")
                        mem = _gdal.GetDriverByName("MEM").Create(
                            "", cols, rows, 1, _gdal.GDT_Float32)
                        mem.SetGeoTransform(gt)
                        mem.SetProjection(proj)
                        ws_src = _gdal.Open(os.path.normpath(watershed_raster))
                        _gdal.ReprojectImage(ws_src, mem)
                        ws_src = None
                        ws_re = mem.GetRasterBand(1).ReadAsArray()
                        mem = None
                        ws_mask = ws_re > 0.5

            n_ws = int(ws_mask.sum()) if ws_mask is not None else rows * cols
            self._log(f"  LFP: {n_ws} watershed cells", "INFO")

            # If watershed mask too small (< 10 cells) or None, use full accum grid
            if ws_mask is None or n_ws < 10:
                self._log(
                    "  LFP: watershed mask too small — using full accumulation grid",
                    "WARNING")
                ws_mask = acc_arr > 0

            # Build stream mask from stream raster
            stream_mask = ws_mask.copy()
            if streams_raster and os.path.exists(os.path.normpath(streams_raster)):
                st_ds = _gdal.Open(os.path.normpath(streams_raster))
                if st_ds is not None:
                    st_arr = st_ds.GetRasterBand(1).ReadAsArray()
                    st_nd  = st_ds.GetRasterBand(1).GetNoDataValue()
                    st_rows = st_ds.RasterYSize
                    st_cols = st_ds.RasterXSize
                    st_ds  = None
                    if st_rows == rows and st_cols == cols:
                        if st_nd is not None:
                            st_mask = (st_arr != st_nd) & (st_arr > 0)
                        else:
                            st_mask = st_arr > 0
                        candidate = st_mask & ws_mask
                        n_st = int(candidate.sum())
                        self._log(
                            f"  LFP: {n_st} stream cells in watershed", "INFO")
                        if n_st >= 2:
                            stream_mask = candidate

            # Find head = stream cell with MINIMUM accumulation
            # (= fewest upstream cells = furthest headwater)
            masked_acc = np.where(stream_mask & (acc_arr > 0), acc_arr, np.inf)
            if np.all(np.isinf(masked_acc)):
                self._log("  LFP: no valid stream cells found.", "WARNING")
                return False
            head_r, head_c = np.unravel_index(np.argmin(masked_acc), acc_arr.shape)
            head_acc = float(acc_arr[head_r, head_c])
            self._log(
                f"  LFP: head=({head_r},{head_c}), acc={head_acc:.0f}", "INFO")

            # Trace DOWNSTREAM from head following maximum accumulation
            adj8 = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
            diag_set = {(-1,-1),(1,-1),(-1,1),(1,1)}

            def rc_to_xy(r, c):
                return (gt[0] + (c + 0.5) * gt[1],
                        gt[3] + (r + 0.5) * gt[5])

            trace = [(head_r, head_c)]
            r, c = int(head_r), int(head_c)
            visited = set()
            visited.add((r, c))
            total_len = 0.0
            max_steps = int(ws_mask.sum()) + 10

            for _ in range(max_steps):
                cur_acc = float(acc_arr[r, c])
                best_acc = cur_acc  # must increase to move downstream
                best_nr, best_nc = -1, -1
                for dr, dc in adj8:
                    nr, nc = r + dr, c + dc
                    if not (0 <= nr < rows and 0 <= nc < cols):
                        continue
                    if (nr, nc) in visited:
                        continue
                    if not ws_mask[nr, nc]:
                        continue
                    nb_acc = float(acc_arr[nr, nc])
                    if nb_acc > best_acc:
                        best_acc = nb_acc
                        best_nr, best_nc = nr, nc
                if best_nr < 0:
                    break
                step = ((cell_x**2 + cell_y**2)**0.5
                        if (best_nr - r, best_nc - c) in diag_set
                        else (cell_x if best_nc != c else cell_y))
                total_len += step
                r, c = best_nr, best_nc
                visited.add((r, c))
                trace.append((r, c))

            self._log(
                f"  LFP: trace={len(trace)} cells, {total_len:.1f}m", "INFO")
            if len(trace) < 2:
                self._log("  LFP: trace too short.", "WARNING")
                return False

            # Write shapefile
            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            self._delete_shapefile(out_shp)
            vec_ds = drv.CreateDataSource(out_shp)
            lyr = vec_ds.CreateLayer("lfp", srs=srs,
                                     geom_type=_ogr.wkbLineString)
            lyr.CreateField(_ogr.FieldDefn("BASIN",     _ogr.OFTInteger))
            lyr.CreateField(_ogr.FieldDefn("LENGTH",    _ogr.OFTReal))
            lyr.CreateField(_ogr.FieldDefn("LFP_CELLS", _ogr.OFTInteger))
            line = _ogr.Geometry(_ogr.wkbLineString)
            for rr, cc in trace:
                x, y = rc_to_xy(rr, cc)
                line.AddPoint(x, y)
            feat = _ogr.Feature(lyr.GetLayerDefn())
            feat.SetGeometry(line)
            feat.SetField("BASIN",     1)
            feat.SetField("LENGTH",    total_len)
            feat.SetField("LFP_CELLS", len(trace))
            lyr.CreateFeature(feat)
            vec_ds.FlushCache()
            vec_ds = None
            self._log(
                f"  LFP: written {len(trace)} cells, {total_len:.1f}m", "SUCCESS")
            return True

        except Exception as exc:
            self._log(f"  _compute_lfp_from_accumulation error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    def _compute_lfp_from_streams(self, streams_raster, d8_raster,
                                   watershed_raster, out_shp):
        """
        Compute Longest Flow Path by tracing along the stream network raster.
        1. Find all stream cells inside the watershed.
        2. BFS upstream from the outlet along stream cells only.
        3. The stream cell furthest upstream = LFP head.
        4. Trace downstream from head along stream cells to outlet.
        Falls back to D8-only trace if stream raster is unavailable.
        """
        try:
            import numpy as np
            from collections import deque
            from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")
            _gdal.UseExceptions()

            # Open D8 raster (primary grid reference)
            d8_path = os.path.normpath(d8_raster)
            d8_ds = _gdal.Open(d8_path)
            if d8_ds is None:
                self._log("  LFP: cannot open D8 raster.", "WARNING")
                return False

            gt = d8_ds.GetGeoTransform()
            proj = d8_ds.GetProjection()
            cell_x = abs(gt[1])
            cell_y = abs(gt[5])
            d8_raw = d8_ds.GetRasterBand(1).ReadAsArray()
            d8_nd = d8_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = d8_raw.shape
            d8_ds = None

            d8_arr = np.abs(d8_raw.astype(np.int32))
            if d8_nd is not None:
                d8_arr[np.abs(d8_raw.astype(np.float64) - d8_nd) < 0.5] = 0

            # GRASS r.watershed drainage = INFLOW direction
            # Outflow = opposite direction
            sample = np.unique(d8_arr[d8_arr > 0])
            wbt_set = {1, 2, 4, 8, 16, 32, 64, 128}
            if set(sample[:8].tolist()).issubset(wbt_set):
                d8_deltas = {
                    1: (0, 1), 2: (-1, 1), 4: (-1, 0), 8: (-1, -1),
                    16: (0, -1), 32: (1, -1), 64: (1, 0), 128: (1, 1),
                }
                enc = "WBT"
            else:
                # GRASS inflow -> outflow = opposite
                d8_deltas = {
                    1: (0, -1), 2: (1, -1), 3: (1, 0), 4: (1, 1),
                    5: (0, 1), 6: (-1, 1), 7: (-1, 0), 8: (-1, -1),
                }
                enc = "GRASS"
            self._log(f"  LFP: D8 {rows}x{cols}, enc={enc}", "INFO")

            # Build watershed mask
            ws_path = os.path.normpath(watershed_raster) if watershed_raster else None
            if ws_path and os.path.exists(ws_path):
                ws_ds = _gdal.Open(ws_path)
                if ws_ds:
                    ws_rows = ws_ds.RasterYSize
                    ws_cols = ws_ds.RasterXSize
                    if ws_rows == rows and ws_cols == cols:
                        ws_arr = ws_ds.GetRasterBand(1).ReadAsArray()
                        ws_nd = ws_ds.GetRasterBand(1).GetNoDataValue()
                    else:
                        mem = _gdal.GetDriverByName("MEM").Create(
                            "", cols, rows, 1, _gdal.GDT_Float32)
                        mem.SetGeoTransform(gt)
                        mem.SetProjection(proj)
                        _gdal.ReprojectImage(ws_ds, mem)
                        ws_arr = mem.GetRasterBand(1).ReadAsArray()
                        ws_nd = mem.GetRasterBand(1).GetNoDataValue()
                        mem = None
                    ws_ds = None
                    ws_mask = ((ws_arr != ws_nd) & (ws_arr > 0)
                               if ws_nd is not None else ws_arr > 0)
                else:
                    ws_mask = np.ones((rows, cols), dtype=bool)
            else:
                ws_mask = np.ones((rows, cols), dtype=bool)

            # Build stream mask from stream raster
            stream_mask = None
            if streams_raster and os.path.exists(os.path.normpath(streams_raster)):
                st_ds = _gdal.Open(os.path.normpath(streams_raster))
                if st_ds:
                    st_rows = st_ds.RasterYSize
                    st_cols = st_ds.RasterXSize
                    if st_rows == rows and st_cols == cols:
                        st_arr = st_ds.GetRasterBand(1).ReadAsArray()
                        st_nd = st_ds.GetRasterBand(1).GetNoDataValue()
                    else:
                        mem2 = _gdal.GetDriverByName("MEM").Create(
                            "", cols, rows, 1, _gdal.GDT_Float32)
                        mem2.SetGeoTransform(gt)
                        mem2.SetProjection(proj)
                        _gdal.ReprojectImage(st_ds, mem2)
                        st_arr = mem2.GetRasterBand(1).ReadAsArray()
                        st_nd = mem2.GetRasterBand(1).GetNoDataValue()
                        mem2 = None
                    st_ds = None
                    stream_mask = ((st_arr != st_nd) & (st_arr > 0)
                                   if st_nd is not None else st_arr > 0)
                    stream_mask &= ws_mask
                    n_stream = int(stream_mask.sum())
                    self._log(f"  LFP: {n_stream} stream cells in watershed", "INFO")

            # If no stream raster, use all watershed cells
            if stream_mask is None or stream_mask.sum() == 0:
                self._log("  LFP: no stream raster — using all watershed cells", "INFO")
                stream_mask = ws_mask.copy()

            n_ws = int(ws_mask.sum())
            self._log(f"  LFP: {n_ws} watershed cells", "INFO")

            # Find outlet cells: stream cells whose D8 neighbour exits the watershed
            outlet_cells = []
            sr, sc = np.where(stream_mask)
            for r, c in zip(sr, sc):
                code = int(d8_arr[r, c])
                delta = d8_deltas.get(code)
                if delta is None or code == 0:
                    outlet_cells.append((r, c))
                    continue
                nr, nc = r + delta[0], c + delta[1]
                if (nr < 0 or nr >= rows or nc < 0 or nc >= cols
                        or not ws_mask[nr, nc]):
                    outlet_cells.append((r, c))

            self._log(f"  LFP: {len(outlet_cells)} outlet cells", "INFO")
            if not outlet_cells:
                # Fallback: boundary stream cells
                outlet_cells = [(r, c) for r, c in zip(sr, sc)
                                if r == 0 or r == rows-1 or c == 0 or c == cols-1]
                if not outlet_cells:
                    outlet_cells = list(zip(sr[:5], sc[:5]))

            # Upstream BFS along stream cells from outlet
            dist = np.full((rows, cols), -1.0)
            q = deque()
            for r, c in outlet_cells:
                dist[r, c] = 0.0
                q.append((r, c))

            def step_len(dr, dc):
                return (cell_x**2 + cell_y**2)**0.5                     if dr != 0 and dc != 0 else (cell_x if dc != 0 else cell_y)

            # For upstream BFS: find cells that drain INTO (r,c)
            # = cells (r-dr, c-dc) with d8_code matching (dr,dc)
            while q:
                r, c = q.popleft()
                for code, (dr, dc) in d8_deltas.items():
                    ur, uc = r - dr, c - dc
                    if not (0 <= ur < rows and 0 <= uc < cols):
                        continue
                    if not stream_mask[ur, uc]:
                        continue
                    if int(d8_arr[ur, uc]) != code:
                        continue
                    sl = step_len(dr, dc)
                    new_d = dist[r, c] + sl
                    if new_d > dist[ur, uc]:
                        dist[ur, uc] = new_d
                        q.append((ur, uc))

            valid = dist[stream_mask]
            if valid.size == 0 or valid.max() < 0:
                self._log("  LFP: BFS produced no distances.", "WARNING")
                return False

            head_r, head_c = np.unravel_index(
                np.where(stream_mask, dist, -1.0).argmax(), dist.shape)
            total_len = float(dist[head_r, head_c])
            self._log(
                f"  LFP: head=({head_r},{head_c}), len={total_len:.1f}m", "INFO")

            # Trace DOWNSTREAM along stream cells from head to outlet
            def rc_to_xy(r, c):
                return (gt[0] + (c + 0.5) * gt[1],
                        gt[3] + (r + 0.5) * gt[5])

            trace = [rc_to_xy(head_r, head_c)]
            r, c = int(head_r), int(head_c)
            visited = set()
            for _ in range(rows * cols):
                if (r, c) in visited:
                    break
                visited.add((r, c))
                code = int(d8_arr[r, c])
                delta = d8_deltas.get(code)
                if delta is None or code == 0:
                    break
                nr, nc = r + delta[0], c + delta[1]
                if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                    break
                if not ws_mask[nr, nc]:
                    break
                if dist[nr, nc] <= 0 and dist[nr, nc] >= 0:
                    trace.append(rc_to_xy(nr, nc))
                    break
                r, c = nr, nc
                trace.append(rc_to_xy(r, c))

            self._log(f"  LFP: trace={len(trace)} cells", "INFO")
            if len(trace) < 2:
                self._log("  LFP: trace too short.", "WARNING")
                return False

            srs = _osr.SpatialReference()
            srs.ImportFromWkt(proj)
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            self._delete_shapefile(out_shp)
            vec_ds = drv.CreateDataSource(out_shp)
            lyr = vec_ds.CreateLayer("lfp", srs=srs,
                                     geom_type=_ogr.wkbLineString)
            lyr.CreateField(_ogr.FieldDefn("BASIN",     _ogr.OFTInteger))
            lyr.CreateField(_ogr.FieldDefn("LENGTH",    _ogr.OFTReal))
            lyr.CreateField(_ogr.FieldDefn("LFP_CELLS", _ogr.OFTInteger))
            line = _ogr.Geometry(_ogr.wkbLineString)
            for x, y in trace:
                line.AddPoint(x, y)
            feat = _ogr.Feature(lyr.GetLayerDefn())
            feat.SetGeometry(line)
            feat.SetField("BASIN",     1)
            feat.SetField("LENGTH",    total_len)
            feat.SetField("LFP_CELLS", len(trace))
            lyr.CreateFeature(feat)
            vec_ds.FlushCache()
            vec_ds = None
            self._log(f"  LFP: written {len(trace)} cells, {total_len:.1f}m", "SUCCESS")
            return True

        except Exception as exc:
            self._log(f"  _compute_lfp_from_streams error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    # Keep old name as alias
    def _compute_lfp_from_d8(self, d8_raster, watershed_raster, out_shp):
        return self._compute_lfp_from_streams(
            None, d8_raster, watershed_raster, out_shp)

    def _get_first_point_coords(self, shp_path):
        """Return (x, y) of the first point feature in a layer."""
        coords = self._get_all_point_coords(shp_path)
        return (coords[0][0], coords[0][1]) if coords else (None, None)

    def _get_all_point_coords(self, shp_path):
        """Return list of (x, y) for all point features in a layer."""
        try:
            from osgeo import ogr as _ogr
            ds = _ogr.Open(os.path.normpath(shp_path))
            if ds is None:
                return []
            lyr = ds.GetLayer(0)
            coords = []
            for feat in lyr:
                geom = feat.GetGeometryRef()
                if geom is None:
                    continue
                # Handle both Point and MultiPoint
                if geom.GetGeometryCount() > 0:
                    for i in range(geom.GetGeometryCount()):
                        g = geom.GetGeometryRef(i)
                        coords.append((g.GetX(), g.GetY()))
                else:
                    coords.append((geom.GetX(), geom.GetY()))
            ds = None
            return coords
        except Exception:
            return []


    # ── Output file housekeeping ─────────────────────────────────────────────

    _PHASE1_FILES = [
        "WBT_Filled_DEM.tif", "WBT_D8_Pointer.tif", "WBT_D8_FlowAccumu.tif",
        "WBT_ExtractStreams.tif", "WBT_ExtractStreams_vector.shp",
        "_tmp_D8_Pointer_raw.tif",
    ]
    _PHASE2_FILES = [
        "outlet_snapped.shp", "WBT_Watershed.tif", "WBT_Watershed_Boundary.shp",
        "WBT_UnnestBasins.tif", "WBT_LongestFlowPath.shp",
        "WBT_Subbasins.tif", "_tmp_Subbasins_full.tif",
        "WBT_Subbasins_Info.shp", "WBT_AllDEM_Subbasins.shp",
        "_tmp_lfp_alldem.shp", "_tmp_lfp_len.tif",
    ]

    def _delete_phase1_outputs(self, out_dir):
        """Delete Phase 1 output files before a rerun."""
        for fname in self._PHASE1_FILES:
            fp = os.path.join(out_dir, fname)
            if fname.endswith(".shp"):
                self._delete_shapefile(fp)
            elif os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass

    def _delete_phase2_outputs(self, out_dir):
        """Delete Phase 2 output files before a rerun."""
        for fname in self._PHASE2_FILES:
            fp = os.path.join(out_dir, fname)
            if fname.endswith(".shp"):
                self._delete_shapefile(fp)
            elif os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        # Delete numbered UnnestBasins and tmp watershed outputs
        for i in range(0, 50):
            for pattern in [f"WBT_UnnestBasins_{i}.tif",
                            f"WBT_Watershed_tmp_{i}.tif",
                            f"WBT_Watershed_tmp_{i}.tfw",
                            f"WBT_Watershed_tmp_{i}.tif.aux.xml"]:
                fp = os.path.join(out_dir, pattern)
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
                else:
                    if pattern.startswith("WBT_UnnestBasins"):
                        break

    def _run_grass_tool(self, algorithm, parameters):
        """Run a GRASS algorithm via QGIS Processing. Returns (ok, msg)."""
        try:
            import processing
            from qgis.core import QgsProcessingFeedback, QgsApplication

            # Auto-detect the correct GRASS prefix once per session
            if not hasattr(WatershedProcessor, '_grass_prefix'):
                reg = QgsApplication.processingRegistry()
                providers = [p.id() for p in reg.providers()]
                if "grass" in providers:
                    WatershedProcessor._grass_prefix = "grass:"
                elif "grass7" in providers:
                    WatershedProcessor._grass_prefix = "grass7:"
                else:
                    WatershedProcessor._grass_prefix = "grass7:"

            prefix = WatershedProcessor._grass_prefix
            if not algorithm.startswith(prefix):
                if algorithm.startswith("grass7:"):
                    algorithm = prefix + algorithm[len("grass7:"):]
                elif algorithm.startswith("grass:"):
                    algorithm = prefix + algorithm[len("grass:"):]

            class _Feedback(QgsProcessingFeedback):
                def __init__(self, log_fn):
                    super().__init__()
                    self._lf = log_fn

                def pushInfo(self, info):
                    if info.strip():
                        self._lf(f"  GRASS: {info}", "INFO")

                def reportError(self, error, fatal=False):
                    if "SetColorTable" in error or "SetRasterColorTable" in error:
                        return
                    if error.strip():
                        self._lf(f"  GRASS ERR: {error}", "WARNING")

                def setProgressText(self, text):
                    pass

            feedback = _Feedback(self._log)
            self._log(f"  alg: {algorithm}", "INFO")
            processing.run(algorithm, parameters, feedback=feedback)
            self._log("  \u2192 Done.", "SUCCESS")
            return True, "OK"
        except Exception as exc:
            return False, str(exc)

    def _resolve_grass(self, grass_path=None):
        """Find the grass executable (informational only)."""
        exe = "grass.bat" if platform.system() == "Windows" else "grass"
        if grass_path:
            for name in ["grass.bat", "grass84.bat", "grass82.bat",
                         "grass", "grass84", "grass82"]:
                c = os.path.join(grass_path, name)
                if os.path.isfile(c):
                    return c
        found = shutil.which(exe)
        if found:
            return found
        for alt in ["grass84", "grass82", "grass84.bat", "grass82.bat"]:
            found = shutil.which(alt)
            if found:
                return found
        if platform.system() == "Windows":
            for osgeo in [r"C:\OSGeo4W", r"C:\OSGeo4W64"]:
                for gv in ["grass84", "grass82"]:
                    c = os.path.join(osgeo, "apps", "grass", gv, "grass.bat")
                    if os.path.isfile(c):
                        return c
        return None

    def _mask_and_label_subbasins_multi(self, subbasins_full, outlet_coords,
                                        d8_raster, merged_ws_raster, output_tif):
        """
        For multi-outlet runs: upstream BFS from each outlet to delineate
        per-outlet watershed cells, then label subbasin IDs uniquely.
        If subbasins_full is None/invalid, create basin IDs from flow accumulation zones.
        """
        try:
            import numpy as np
            from collections import deque as _dq
            from osgeo import gdal as _gdal
            _gdal.SetConfigOption("SHAPE_RESTORE_SHX", "YES")

            # Load D8
            d8_ds = _gdal.Open(os.path.normpath(d8_raster))
            if d8_ds is None:
                return False
            gt = d8_ds.GetGeoTransform()
            proj = d8_ds.GetProjection()
            d8_raw = d8_ds.GetRasterBand(1).ReadAsArray()
            d8_nd = d8_ds.GetRasterBand(1).GetNoDataValue()
            rows, cols = d8_raw.shape
            d8_ds = None

            d8_arr = np.abs(d8_raw.astype(np.int32))
            if d8_nd is not None:
                d8_arr[np.abs(d8_raw.astype(np.float64) - d8_nd) < 0.5] = 0

            sample = np.unique(d8_arr[d8_arr > 0])
            wbt_set = {1, 2, 4, 8, 16, 32, 64, 128}
            if set(sample[:8].tolist()).issubset(wbt_set):
                d8_deltas = {
                    1: (0, 1), 2: (-1, 1), 4: (-1, 0), 8: (-1, -1),
                    16: (0, -1), 32: (1, -1), 64: (1, 0), 128: (1, 1),
                }
            else:
                d8_deltas = {
                    1: (0, -1), 2: (1, -1), 3: (1, 0), 4: (1, 1),
                    5: (0, 1), 6: (-1, 1), 7: (-1, 0), 8: (-1, -1),
                }

            # Load subbasin raster if available
            sub_arr = None
            if subbasins_full and os.path.exists(os.path.normpath(subbasins_full)):
                sub_ds = _gdal.Open(os.path.normpath(subbasins_full))
                if sub_ds:
                    sub_raw = sub_ds.GetRasterBand(1).ReadAsArray()
                    sub_nd = sub_ds.GetRasterBand(1).GetNoDataValue()
                    sub_ds = None
                    sub_f = sub_raw.astype(np.float64)
                    if sub_nd is not None:
                        nd_mask = np.abs(sub_f - sub_nd) < 1.0
                    else:
                        nd_mask = ~np.isfinite(sub_f)
                    nd_mask |= sub_f < 0.5
                    sub_arr_candidate = sub_f.astype(np.int32)
                    sub_arr_candidate[nd_mask] = 0
                    unique_ids = np.unique(sub_arr_candidate[sub_arr_candidate > 0])
                    if len(unique_ids) > 0:
                        sub_arr = sub_arr_candidate
                        self._log(f"  Sub: using GRASS basin raster, "
                                  f"{len(unique_ids)} IDs", "INFO")

            def upstream_mask(ox, oy):
                c0 = int((ox - gt[0]) / gt[1])
                r0 = int((oy - gt[3]) / gt[5])
                if not (0 <= r0 < rows and 0 <= c0 < cols):
                    return None
                mask = np.zeros((rows, cols), dtype=bool)
                q = _dq()
                q.append((r0, c0))
                mask[r0, c0] = True
                while q:
                    r, c = q.popleft()
                    for code, (dr, dc) in d8_deltas.items():
                        ur, uc = r - dr, c - dc
                        if not (0 <= ur < rows and 0 <= uc < cols):
                            continue
                        if mask[ur, uc]:
                            continue
                        if int(d8_arr[ur, uc]) == code:
                            mask[ur, uc] = True
                            q.append((ur, uc))
                return mask

            result = np.zeros((rows, cols), dtype=np.int32)
            id_offset = 0
            for idx, (ox, oy) in enumerate(outlet_coords):
                ws_mask_i = upstream_mask(ox, oy)
                if ws_mask_i is None or not ws_mask_i.any():
                    self._log(f"  Outlet {idx+1}: no upstream cells.", "WARNING")
                    continue

                if sub_arr is not None:
                    # Use GRASS basin IDs within this outlet watershed
                    basin_ids = np.unique(sub_arr[ws_mask_i & (sub_arr > 0)])
                    for bid in basin_ids:
                        result[(sub_arr == bid) & ws_mask_i] = id_offset + int(bid)
                    max_id = int(basin_ids.max()) if basin_ids.size > 0 else 0
                    id_offset += max_id + 1
                    self._log(f"  Outlet {idx+1}: {len(basin_ids)} subbasins", "INFO")
                else:
                    # No GRASS basin raster — label each cell with outlet index
                    result[ws_mask_i] = idx + 1
                    self._log(
                        f"  Outlet {idx+1}: {int(ws_mask_i.sum())} cells "
                        f"(no subbasin raster, using outline only)", "INFO")

            if not result.any():
                return False

            drv = _gdal.GetDriverByName("GTiff")
            out_ds = drv.Create(os.path.normpath(output_tif),
                                cols, rows, 1, _gdal.GDT_Int32,
                                options=["COMPRESS=LZW", "TILED=YES"])
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            b = out_ds.GetRasterBand(1)
            b.SetNoDataValue(0)
            b.WriteArray(result)
            out_ds.FlushCache()
            out_ds = None
            self._log(
                f"  Subbasin raster written: {int((result > 0).sum())} cells", "INFO")
            return True
        except Exception as exc:
            self._log(f"  _mask_and_label_subbasins_multi error: {exc}", "WARNING")
            import traceback
            self._log(traceback.format_exc(), "INFO")
            return False

    def _delete_shapefile(self, shp_path):
        """Remove a shapefile and its sidecar files if they exist."""
        shp_path = os.path.normpath(shp_path)
        if not os.path.exists(shp_path):
            return
        try:
            from osgeo import ogr as _ogr
            from osgeo import gdal as _gdal_del
            _gdal_del.SetConfigOption("SHAPE_RESTORE_SHX", "YES")
            drv = _ogr.GetDriverByName("ESRI Shapefile")
            drv.DeleteDataSource(shp_path)
            return
        except (RuntimeError, AttributeError):
            pass  # driver delete unavailable
        base = os.path.splitext(shp_path)[0]
        for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj",
                    ".atx", ".sbn", ".sbx", ".fbn", ".fbx"]:
            p = base + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _run_wbt(self, wbt_exe, args):
        # cmd is a list (shell=False); no shell injection possible
        cmd = [str(wbt_exe)] + [str(a) for a in args]
        self._log(f"CMD: {' '.join(cmd)}", "INFO")
        try:
            kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(cmd, **kwargs)  # nosec B603 - cmd is a validated list, shell=False
            try:
                stdout_b, stderr_b = proc.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
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
        except (AttributeError, ImportError, KeyError):
            pass  # optional WBT plugin detection
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
        s = sorted(elevations)
        n = len(s)
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
            self._log(f"  {f:<48} {os.path.getsize(fp) / 1024:>8.1f} KB", "INFO")    # ═══════════════════════════════════════════════════════════════