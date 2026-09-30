"""Motor de adquisición y medición.

Toda la interacción con la cámara ocurre en UN solo hilo (requisito de ASCOM/COM).
El servidor web le envía comandos a ese hilo con `Engine.call(fn)`.

Modos:
    idle       – cámara conectada sin capturar
    live       – vista en vivo
    measure    – acumulando frames para una medición de seeing
    calibrate  – siguiendo la deriva de la estrella para calibrar la escala "/px
"""

from __future__ import annotations

import csv
import io
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .analysis import calibrate_from_drift, compute_seeing, find_spots, pair_vector
from .astro import airmass, altaz
from .cameras import Camera, SimulatorCamera, TransientFrameError, make_camera
from .config import Config, data_dir
from .fitsio import write_fits
from .platesolve import SolveError, solve
from .telescope import TelescopeMonitor

CSV_FIELDS = [
    "time_utc", "seeing_arcsec", "seeing_zenith", "seeing_l", "seeing_t", "r0_cm",
    "sigma_l_arcsec", "sigma_t_arcsec", "separation_px", "n_frames", "attempts",
    "airmass", "alt_deg", "ra_deg", "dec_deg", "coord_source", "exposure_ms",
    "pixscale_arcsec", "camera",
]

MISS_LIMIT = 15          # frames seguidos sin estrella = "salió del campo"
CALIB_MIN_S = 4.0        # duración mínima útil de una calibración por deriva
CALIB_MIN_R2 = 0.98      # calidad mínima del ajuste lineal para aplicarla


def _span(track: list) -> float:
    return track[-1][0] - track[0][0] if len(track) > 1 else 0.0


class Engine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.camera: Camera | None = None
        self.mode = "idle"
        self.continuous = False
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()

        self.preview_jpeg: bytes | None = None
        self.preview_meta: dict = {}
        self._last_preview = 0.0
        self.fps = 0.0
        self._fps_t = time.monotonic()
        self._fps_n = 0
        self._err_streak = 0
        self._last_err = ""

        self.results: list[dict] = []
        self.logs: deque = deque(maxlen=300)
        self._log_seq = 0
        self._log_lock = threading.Lock()
        self.progress = {"valid": 0, "attempts": 0, "target": 0, "waiting": False}
        self._vectors: list[np.ndarray] = []
        self._ref: np.ndarray | None = None
        self._miss = 0
        self._warned_airmass = False

        self._track: list[tuple[float, float, float]] = []
        self._best_track: list[tuple[float, float, float]] = []
        self._calib_t0 = 0.0
        self._calib_duration = 15.0
        self.last_calibration: dict | None = None

        self._csv_path: Path | None = None

        # coordenadas del objeto apuntado: catálogo / manual / platesolve (si no, LX200)
        self.coords: dict = {"source": None, "name": None, "ra_deg": None, "dec_deg": None, "time": None}
        self.solving = False

        self.telescope = TelescopeMonitor(on_site=self._on_site)
        self._thread = threading.Thread(target=self._run, name="camera-worker", daemon=True)
        self._thread.start()
        self.log("info", "Listo. Conecte la cámara y elija la estrella que va a apuntar.")

    # ------------------------------------------------------------------ util
    def log(self, level: str, msg: str) -> None:
        with self._log_lock:
            self._log_seq += 1
            self.logs.append({"seq": self._log_seq, "t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})

    def call(self, fn, timeout: float = 60.0):
        """Ejecuta `fn()` en el hilo de la cámara y devuelve su resultado."""
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout=timeout)

    def shutdown(self) -> None:
        try:
            self.call(self._disconnect_camera, timeout=10)
        except Exception:  # noqa: BLE001 — cerrar como se pueda
            pass
        self._stop.set()
        self.telescope.disconnect()

    def _on_site(self, lat: float, lon: float) -> None:
        self.cfg.site_lat_deg = round(lat, 4)
        self.cfg.site_lon_deg = round(lon, 4)
        self.cfg.save()
        self.log("info", f"Sitio desde GPS del LX200: lat {lat:.3f}°, lon {lon:.3f}°")

    # --------------------------------------------------------- comandos cámara
    def connect_camera(self, backend: str, **opts) -> dict:
        def _do():
            self._disconnect_camera()
            cam = make_camera(backend, self.cfg, **opts)
            cam.connect()
            cam.set_binning(self.cfg.binning)
            cam.set_gain(self.cfg.gain)
            if isinstance(cam, SimulatorCamera) and self.coords["dec_deg"] is not None:
                cam.dec_deg = self.coords["dec_deg"]
            self.camera = cam
            self.mode = "live"
            self._err_streak = 0
            # recordar para la próxima vez
            self.cfg.camera_backend = backend
            self.cfg.camera_index = int(opts.get("index") or 0)
            if backend == "ascom" and getattr(cam, "prog_id", None):
                self.cfg.ascom_prog_id = cam.prog_id
            self.cfg.save()
            self.log("ok", f"Cámara conectada: {cam.name} ({cam.width}x{cam.height})")
            return cam.info()
        return self.call(_do)

    def _disconnect_camera(self) -> None:
        self.mode = "idle"
        if self.camera:
            name = self.camera.name
            try:
                self.camera.disconnect()
            finally:
                self.camera = None
            self.log("info", f"Cámara desconectada: {name}")

    def disconnect_camera(self) -> None:
        self.call(self._disconnect_camera)

    def apply_config(self) -> None:
        def _do():
            if self.camera:
                self.camera.set_binning(self.cfg.binning)
                self.camera.set_gain(self.cfg.gain)
        self.call(_do)

    def set_live(self, on: bool) -> None:
        def _do():
            if not self.camera:
                raise RuntimeError("No hay cámara conectada")
            self.mode = "live" if on else "idle"
        self.call(_do)

    def start_measure(self, continuous: bool) -> None:
        def _do():
            if not self.camera:
                raise RuntimeError("No hay cámara conectada")
            self.continuous = continuous
            self._begin_measure()
            self.mode = "measure"
            self.log("info", "Medición continua iniciada" if continuous else "Medición única iniciada")
            if self.cfg.exposure_ms > 10:
                self.log("warn", f"Exposición de {self.cfg.exposure_ms:g} ms: con más de ~10 ms la turbulencia "
                                 "se promedia y el seeing sale MENOR al real. Use la más corta que deje ver la estrella.")
        self.call(_do)

    def stop_measure(self) -> None:
        def _do():
            if self.mode in ("measure", "calibrate"):
                self.log("info", "Medición detenida" if self.mode == "measure" else "Calibración cancelada")
                self.mode = "live"
                self.continuous = False
        self.call(_do)

    # ----------------------------------------------------------- coordenadas
    def set_manual_coords(self, ra_deg: float, dec_deg: float, name: str | None = None) -> None:
        self.coords = {"source": "catalog" if name else "manual", "name": name,
                       "ra_deg": ra_deg, "dec_deg": dec_deg, "time": time.time()}
        if isinstance(self.camera, SimulatorCamera):
            self.camera.dec_deg = dec_deg
        self.log("info", f"Estrella apuntada: {name}" if name else "Coordenadas manuales guardadas")

    def plate_solve(self) -> None:
        if self.solving:
            raise RuntimeError("Ya hay un plate solve en curso")
        cfg = self.cfg

        def _grab():
            if not self.camera:
                raise RuntimeError("No hay cámara conectada")
            prev_roi = self.camera.roi
            self.camera.set_roi(None)
            try:
                img = self.camera.grab(cfg.solve_exposure_s)
            finally:
                self.camera.set_roi(prev_roi)
            return img

        img = self.call(_grab, timeout=cfg.solve_exposure_s + 60)
        path = data_dir() / "platesolve.fits"
        write_fits(path, img, {"EXPTIME": cfg.solve_exposure_s, "FOCALLEN": cfg.focal_length_mm,
                               "XPIXSZ": cfg.pixel_size_um * cfg.binning,
                               "YPIXSZ": cfg.pixel_size_um * cfg.binning})
        fov_h = img.shape[0] * cfg.pixscale_arcsec / 3600.0
        hint = self.telescope.state
        self.solving = True
        self.log("info", f"Resolviendo placa (campo {fov_h * 60:.1f}')…")

        def _solve():
            try:
                res = solve(path, cfg.astap_path, fov_h, cfg.solve_radius_deg, hint.ra_deg, hint.dec_deg)
                self.coords = {"source": "platesolve", "name": None, "time": time.time(), **res}
                self.log("ok", f"Plate solve OK: RA {res['ra_deg']:.4f}°, Dec {res['dec_deg']:.4f}°")
            except SolveError as e:
                self.log("error", f"Plate solve falló: {e}")
            finally:
                self.solving = False

        threading.Thread(target=_solve, daemon=True).start()

    # ----------------------------------------------------------- calibración
    def start_calibration(self, duration_s: float = 15.0) -> None:
        def _do():
            if not self.camera:
                raise RuntimeError("No hay cámara conectada")
            if self.coords["dec_deg"] is None:
                raise RuntimeError("Primero elija la estrella apuntada (se necesita su declinación)")
            self._track = []
            self._best_track = []
            self._miss = 0
            self._calib_duration = float(duration_s)
            self._calib_t0 = time.monotonic()
            self.mode = "calibrate"
            self.log("info", "Calibrando escala: deje que la estrella cruce el campo sin tocar el telescopio…")
        self.call(_do)

    def _close_segment(self) -> None:
        """Guarda el tramo continuo más largo visto y empieza uno nuevo."""
        if _span(self._track) > _span(self._best_track):
            self._best_track = self._track
        self._track = []

    def reset_calibration(self) -> None:
        self.cfg.pixscale_override_arcsec = 0.0
        self.cfg.save()
        self.last_calibration = None
        self.log("info", f"Calibración borrada: se usa la escala teórica {self.cfg.physical_pixscale:.3f}\"/px")

    def _calib_step(self, spots, valid: bool) -> None:
        now = time.monotonic()
        if valid:
            cx = (spots[0].x + spots[1].x) / 2
            cy = (spots[0].y + spots[1].y) / 2
            if len(self._track) >= 3:
                # salto brusco = alguien movió el telescopio: empezar de nuevo
                steps = np.diff(np.array(self._track)[-3:, 1:], axis=0)
                typical = max(float(np.median(np.hypot(steps[:, 0], steps[:, 1]))), 2.0)
                last = self._track[-1]
                if np.hypot(cx - last[1], cy - last[2]) > 10 * typical + 10:
                    self._close_segment()
            self._track.append((now, cx, cy))
            self._miss = 0
        else:
            self._miss += 1
            if self._miss == MISS_LIMIT:
                self._close_segment()   # la estrella salió: el siguiente tramo empieza de cero

        timed_out = now - self._calib_t0 >= self._calib_duration
        lost = self._miss >= MISS_LIMIT and _span(self._best_track) >= CALIB_MIN_S
        if not (timed_out or lost):
            return
        self._close_segment()
        self.mode = "live"
        track = self._best_track
        if _span(track) < CALIB_MIN_S:
            self.log("warn", "Calibración fallida: la estrella no se vio el tiempo suficiente. "
                             "Apunte la estrella al borde de entrada del campo y repita.")
            return
        t, x, y = np.array(track).T
        try:
            cal = calibrate_from_drift(t, x, y, self.coords["dec_deg"])
        except ValueError as e:
            self.log("warn", f"Calibración fallida: {e}")
            return
        self.last_calibration = cal.to_dict()
        ratio = cal.pixscale_arcsec / self.cfg.physical_pixscale
        msg = (f"Escala medida {cal.pixscale_arcsec:.3f}\"/px (teórica {self.cfg.physical_pixscale:.3f}, "
               f"x{ratio:.2f}), deriva {cal.speed_px_s:.1f} px/s a {cal.angle_deg:.0f}°, R²={cal.r2:.4f}")
        if cal.r2 >= CALIB_MIN_R2:
            self.cfg.pixscale_override_arcsec = round(float(cal.pixscale_arcsec), 4)
            self.cfg.save()
            self.log("ok", "Calibración aplicada. " + msg)
        else:
            self.log("warn", "Calibración NO aplicada (movimiento irregular: ¿viento o se tocó el tubo?). " + msg)

    # ----------------------------------------------------------- bucle worker
    def _run(self) -> None:
        while not self._stop.is_set():
            busy = self.camera is not None and self.mode in ("live", "measure", "calibrate")
            try:
                fn, fut = self._q.get(timeout=0 if busy else 0.05)
                try:
                    fut.set_result(fn())
                except Exception as e:  # noqa: BLE001 — se devuelve al servidor
                    fut.set_exception(e)
                continue
            except queue.Empty:
                pass
            if not busy:
                continue
            try:
                self._step()
                self._err_streak = 0
            except TransientFrameError:
                continue
            except Exception as e:  # noqa: BLE001 — no matar el hilo por un frame malo
                self._err_streak += 1
                if str(e) != self._last_err:
                    self.log("error", f"Error de cámara: {e}")
                    self._last_err = str(e)
                if self._err_streak >= 5:
                    self.log("error", "Demasiados errores seguidos: captura detenida. Revise la cámara y reconecte.")
                    self.mode = "idle"
                    self._err_streak = 0
                else:
                    time.sleep(0.2)

    def _step(self) -> None:
        cfg = self.cfg
        img = self.camera.grab(cfg.exposure_ms / 1000.0)
        if img.ndim != 2 or img.size == 0:
            raise RuntimeError(f"imagen con forma inesperada {img.shape}")
        spots = find_spots(img, 2, cfg.detect_sigma, cfg.centroid_box)
        valid, reason = self._validate(img, spots)

        self._fps_n += 1
        now = time.monotonic()
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_n, self._fps_t = 0, now

        if self.mode == "measure":
            self._measure_step(spots, valid)
        elif self.mode == "calibrate":
            self._calib_step(spots, valid)

        if now - self._last_preview > 0.2:
            self._last_preview = now
            self._make_preview(img, spots, valid, reason)

    def _validate(self, img: np.ndarray, spots) -> tuple[bool, str]:
        cfg = self.cfg
        if len(spots) < 2:
            return False, ("solo se ve 1 imagen: desenfoque un poco o revise la máscara" if spots
                           else "no se detecta estrella")
        h, w = img.shape
        m = cfg.centroid_box
        for s in spots:
            if s.x < m or s.y < m or s.x > w - m or s.y > h - m:
                return False, "estrella en el borde del campo"
            if s.peak >= min(cfg.saturation_adu, self.camera.max_adu) * 0.98:
                return False, "saturada: baje exposición o ganancia"
        f1, f2 = spots[0].flux, spots[1].flux
        if max(f1, f2) / max(min(f1, f2), 1e-6) > 3:
            return False, "brillos muy distintos (¿otra estrella en el campo?)"
        return True, "ok"

    # ------------------------------------------------------------- medición
    def _begin_measure(self) -> None:
        self._vectors = []
        self._ref = None
        self._miss = 0
        self.progress = {"valid": 0, "attempts": 0, "target": self.cfg.frames_per_measure, "waiting": True}

    def _measure_step(self, spots, valid: bool) -> None:
        cfg = self.cfg
        p = self.progress
        if valid:
            v = pair_vector(spots, self._ref)
            if self._ref is None:
                self._ref = v
            self._vectors.append(v)
            p["valid"] += 1
            p["attempts"] += 1
            p["waiting"] = False
            self._miss = 0
            if p["valid"] >= cfg.frames_per_measure:
                self._finish_measure()
            return

        if p["valid"] == 0:
            p["waiting"] = True     # aún no llega la estrella: esperar sin contar
            return
        p["attempts"] += 1
        self._miss += 1
        if self._miss < MISS_LIMIT:
            return
        # la estrella salió del campo (sin seguimiento es normal)
        if p["valid"] >= cfg.min_valid_frames:
            self.log("info", f"La estrella salió del campo: se usan {p['valid']} frames.")
            self._finish_measure()
        else:
            self.log("warn", f"La estrella salió del campo con solo {p['valid']} frames "
                             f"(mínimo {cfg.min_valid_frames}). Re-apunte; la medición sigue esperando.")
            self._begin_measure()

    def _finish_measure(self) -> None:
        cfg = self.cfg
        am, alt = self._airmass()
        try:
            r = compute_seeing(np.array(self._vectors), cfg.pixscale_arcsec, cfg.aperture_d_m,
                               cfg.baseline_b_m, cfg.wavelength_nm * 1e-9, am)
        except ValueError as e:
            self.log("error", f"No se pudo calcular: {e}")
            self._after_measure()
            return
        c = self.coords if self.coords["source"] else {
            "source": "lx200" if self.telescope.state.connected else None,
            "ra_deg": self.telescope.state.ra_deg, "dec_deg": self.telescope.state.dec_deg}
        row = {
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **r.to_dict(),
            "attempts": self.progress["attempts"],
            "airmass": round(am, 3) if am else None,
            "alt_deg": round(alt, 2) if alt is not None else None,
            "ra_deg": round(c["ra_deg"], 5) if c.get("ra_deg") is not None else None,
            "dec_deg": round(c["dec_deg"], 5) if c.get("dec_deg") is not None else None,
            "coord_source": f"catalog:{c['name']}" if c.get("source") == "catalog" else c.get("source"),
            "exposure_ms": cfg.exposure_ms,
            "pixscale_arcsec": round(cfg.pixscale_arcsec, 4),
            "camera": self.camera.name if self.camera else None,
        }
        self.results.append(row)
        self._append_csv(row)
        z = f" (cenit {r.seeing_zenith:.2f}\")" if r.seeing_zenith else ""
        self.log("ok", f"Seeing {r.seeing_arcsec:.2f}\"{z}  r0={r.r0_cm:.1f} cm  n={r.n_frames}")
        if am is None and not self._warned_airmass:
            self._warned_airmass = True
            self.log("warn", "Sin corrección al cenit: elija la estrella apuntada y ponga latitud/longitud en Ajustes.")
        if abs(r.seeing_l - r.seeing_t) / max(r.seeing_arcsec, 1e-6) > 0.5:
            self.log("warn", "Seeing longitudinal y transversal muy distintos: revise vibraciones, "
                             "que la separación B sea la real, o viento sobre el tubo.")
        self._after_measure()

    def _after_measure(self) -> None:
        if self.continuous:
            self._begin_measure()
        else:
            self.mode = "live"

    def _airmass(self) -> tuple[float | None, float | None]:
        cfg = self.cfg
        c = self.coords
        if c["source"] and cfg.site_lat_deg is not None and cfg.site_lon_deg is not None:
            alt, _ = altaz(c["ra_deg"], c["dec_deg"], cfg.site_lat_deg, cfg.site_lon_deg)
            return airmass(alt), alt
        alt = self.telescope.state.alt_deg
        if alt is not None:
            return airmass(alt), alt
        return None, None

    def _append_csv(self, row: dict) -> None:
        # una noche = un archivo (fecha local de la tarde en que empezó)
        night = (datetime.now() - timedelta(hours=12)).strftime("%Y%m%d")
        path = self._csv_path or data_dir() / f"seeing_{night}.csv"
        for attempt in range(2):
            try:
                new = not path.exists()
                # utf-8-sig: Excel en Windows abre bien los acentos
                with path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    if new:
                        w.writeheader()
                    w.writerow(row)
                self._csv_path = path
                return
            except OSError:
                # típico en Windows: el archivo está abierto en Excel
                if attempt == 0:
                    path = data_dir() / f"seeing_{night}_{datetime.now():%H%M%S}.csv"
                    self.log("warn", f"No se pudo escribir el CSV (¿abierto en Excel?). Se usa {path.name}")
        self.log("error", "No se pudo guardar el CSV; los datos siguen en 'Descargar CSV'.")

    def results_csv(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(self.results)
        return buf.getvalue()

    # --------------------------------------------------------------- preview
    def _make_preview(self, img: np.ndarray, spots, valid: bool, reason: str) -> None:
        lo = float(np.percentile(img[::2, ::2], 5))
        hi = float(max(img.max(), lo + 1))
        # estiramiento raíz cuadrada: se ven fondo y estrella a la vez
        norm = np.sqrt(np.clip((img - lo) / (hi - lo), 0, 1))
        im = Image.fromarray((norm * 255).astype(np.uint8))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        self.preview_jpeg = buf.getvalue()
        self.preview_meta = {
            "w": int(img.shape[1]), "h": int(img.shape[0]),
            "spots": [{"x": round(s.x, 2), "y": round(s.y, 2), "peak": round(s.peak)} for s in spots],
            "valid": valid, "reason": reason, "max": round(float(img.max())),
            "bg": round(float(np.median(img[::4, ::4])), 1), "t": time.time(),
        }
        if self.cfg.save_fits and self.mode == "measure":
            try:
                write_fits(data_dir() / "ultimo_frame.fits", img, {"EXPTIME": self.cfg.exposure_ms / 1000})
            except OSError:
                pass

    # ----------------------------------------------------------------- estado
    def state(self, since_log: int = 0, since_result: int = 0) -> dict:
        tel = self.telescope.state
        with self._log_lock:
            logs = [lg for lg in self.logs if lg["seq"] > since_log]
        calib = None
        if self.mode == "calibrate":
            calib = {"elapsed": round(time.monotonic() - self._calib_t0, 1),
                     "duration": self._calib_duration, "points": len(self._track)}
        return {
            "mode": self.mode,
            "continuous": self.continuous,
            "camera": self.camera.info() if self.camera else None,
            "fps": round(self.fps, 1),
            "progress": self.progress,
            "calibrating": calib,
            "last_calibration": self.last_calibration,
            "preview": self.preview_meta,
            "telescope": dict(tel.__dict__),
            "coords": self.coords,
            "solving": self.solving,
            "results": self.results[since_result:],
            "n_results": len(self.results),
            "logs": logs,
            "site": {"lat": self.cfg.site_lat_deg, "lon": self.cfg.site_lon_deg},
            "pixscale": {"used": round(float(self.cfg.pixscale_arcsec), 4),
                         "physical": round(float(self.cfg.physical_pixscale), 4),
                         "calibrated": bool(self.cfg.pixscale_override_arcsec > 0)},
            "csv_path": str(self._csv_path) if self._csv_path else None,
        }
