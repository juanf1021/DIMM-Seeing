"""Backends de cámara.

Todas las cámaras exponen la misma interfaz mínima para el motor:
    connect() / disconnect()
    grab(exposure_s) -> np.ndarray 2D
    set_gain(gain), set_binning(b), set_roi(roi | None)

Backends:
    simulator  – estrellas dobles sintéticas con seeing conocido (siempre disponible)
    ascom      – cualquier cámara con driver ASCOM (Windows, recomendado)
    zwo        – ZWO ASI directo por SDK (más rápido que ASCOM)
    webcam     – cámaras UVC / DirectShow vía OpenCV (control de exposición limitado)
"""

from __future__ import annotations

import math
import sys
import time

import numpy as np

from .analysis import differential_sigma_px


class CameraError(RuntimeError):
    pass


class TransientFrameError(CameraError):
    """Un frame se perdió pero la cámara sigue funcionando: se ignora."""


class Camera:
    backend = "base"
    width = 0
    height = 0
    max_adu = 65535

    def __init__(self) -> None:
        self.name = self.backend
        self.roi: tuple[int, int, int, int] | None = None
        self.binning = 1

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def set_gain(self, gain: int) -> None: ...

    def set_binning(self, b: int) -> None:
        self.binning = max(1, int(b))

    def set_roi(self, roi: tuple[int, int, int, int] | None) -> None:
        self.roi = roi

    def grab(self, exposure_s: float) -> np.ndarray:
        raise NotImplementedError

    def info(self) -> dict:
        return {"backend": self.backend, "name": self.name, "width": self.width,
                "height": self.height, "max_adu": self.max_adu, "roi": self.roi,
                "note": getattr(self, "exposure_note", "")}


# ---------------------------------------------------------------------------
# Simulador
# ---------------------------------------------------------------------------
class SimulatorCamera(Camera):
    """Genera el patrón DIMM (2 imágenes de una estrella) con un seeing conocido.

    El movimiento diferencial entre las 2 imágenes sigue exactamente la teoría
    DIMM, así que el valor medido debería coincidir con `seeing` (±ruido).
    Con `drift=True` la estrella cruza el campo como en un telescopio sin
    seguimiento, y reaparece al otro lado (como si alguien la re-apuntara).
    """

    backend = "simulator"
    width, height = 640, 480
    max_adu = 65535

    def __init__(self, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.name = "Simulador DIMM"
        self.rng = np.random.default_rng()
        self._t0 = time.monotonic()
        self._x0 = self.width * 0.3
        self._y0 = self.height * 0.5
        self.separation = np.array([46.0, 9.0])   # px, por el desenfoque/prisma
        self.dec_deg = 30.0                        # el motor la ajusta a la estrella elegida

    def _star_pos(self) -> tuple[float, float]:
        cfg = self.cfg
        x, y = self._x0, self._y0
        if cfg.sim_drift:
            # 15.041"/s * cos(dec) -> px/s con la escala física (la "verdadera")
            speed = 15.041 * math.cos(math.radians(self.dec_deg)) / cfg.physical_pixscale
            span = self.width - 120
            x = 60 + ((self._x0 - 60 + speed * (time.monotonic() - self._t0)) % span)
        return x, y

    def grab(self, exposure_s: float) -> np.ndarray:
        cfg = self.cfg
        # Simula la cadencia de una cámara real (~60 fps máx.)
        time.sleep(max(exposure_s, 1 / 60))
        sl, st = differential_sigma_px(cfg.sim_seeing_arcsec, cfg.physical_pixscale,
                                       cfg.aperture_d_m, cfg.baseline_b_m)
        x, y = self._star_pos()
        common = self.rng.normal(0, 1.2, 2)                 # vibración / viento
        u = self.separation / np.linalg.norm(self.separation)
        p = np.array([-u[1], u[0]])
        diff = u * self.rng.normal(0, sl) + p * self.rng.normal(0, st)
        c1 = np.array([x, y]) + common - self.separation / 2 - diff / 2
        c2 = np.array([x, y]) + common + self.separation / 2 + diff / 2

        w, h = self.width, self.height
        # tamaño de la mancha ~ difracción de un agujero de D (lambda/D)
        fwhm_arc = max(0.5e-6 / cfg.aperture_d_m * 206265, 1.0)
        sig = fwhm_arc / cfg.physical_pixscale / 2.355
        amp = 30000.0 * min(1.0, exposure_s / 0.005) * self.rng.uniform(0.8, 1.2)
        # fondo: ruido gaussiano (aprox. de Poisson + lectura) en todo el frame
        bg = 800.0
        img = (bg + self.rng.standard_normal((h, w), dtype=np.float32) * np.sqrt(bg + 12**2)).astype(np.float32)
        # estrellas: solo se calculan en una ventana pequeña (mucho más rápido)
        r = int(6 * sig) + 2
        for c in (c1, c2):
            cx, cy = int(round(c[0])), int(round(c[1]))
            x0, x1 = max(0, cx - r), min(w, cx + r + 1)
            y0, y1 = max(0, cy - r), min(h, cy + r + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            yy, xx = np.mgrid[y0:y1, x0:x1]
            star = amp * np.exp(-((xx - c[0]) ** 2 + (yy - c[1]) ** 2) / (2 * sig**2))
            img[y0:y1, x0:x1] += self.rng.poisson(star).astype(np.float32)
        img = np.clip(img, 0, self.max_adu)
        if self.roi:
            x0, y0, rw, rh = self.roi
            img = img[y0:y0 + rh, x0:x0 + rw]
        return img


# ---------------------------------------------------------------------------
# ASCOM (Windows)
# ---------------------------------------------------------------------------
class AscomCamera(Camera):
    """Cualquier cámara con driver ASCOM. Requiere ASCOM Platform + pywin32.

    Todas las llamadas deben hacerse desde el mismo hilo (el motor lo garantiza).
    """

    backend = "ascom"

    def __init__(self, prog_id: str | None = None) -> None:
        super().__init__()
        self.prog_id = prog_id
        self.cam = None

    @staticmethod
    def choose() -> str:
        """Abre el selector de ASCOM y devuelve el ProgID elegido."""
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        chooser = win32com.client.Dispatch("ASCOM.Utilities.Chooser")
        chooser.DeviceType = "Camera"
        prog = chooser.Choose(None)
        if not prog:
            raise CameraError("No se seleccionó ninguna cámara ASCOM")
        return str(prog)

    def connect(self) -> None:
        if sys.platform != "win32":
            raise CameraError("ASCOM solo está disponible en Windows")
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        if not self.prog_id:
            self.prog_id = self.choose()
        self.cam = win32com.client.Dispatch(self.prog_id)
        self.cam.Connected = True
        self.name = str(self.cam.Name)
        self.width = int(self.cam.CameraXSize)
        self.height = int(self.cam.CameraYSize)
        try:
            self.max_adu = int(self.cam.MaxADU)
        except Exception:
            pass

    def disconnect(self) -> None:
        if self.cam is not None:
            try:
                self.cam.Connected = False
            finally:
                self.cam = None

    def set_gain(self, gain: int) -> None:
        try:
            self.cam.Gain = int(gain)
        except Exception:
            pass  # no todas las cámaras soportan Gain

    def _apply_frame(self) -> None:
        cam, b = self.cam, self.binning
        cam.BinX = b
        cam.BinY = b
        if self.roi:
            x0, y0, w, h = self.roi
            cam.StartX, cam.StartY, cam.NumX, cam.NumY = x0 // b, y0 // b, max(1, w // b), max(1, h // b)
        else:
            cam.StartX, cam.StartY = 0, 0
            cam.NumX, cam.NumY = self.width // b, self.height // b

    def grab(self, exposure_s: float) -> np.ndarray:
        if self.cam is None:
            raise CameraError("Cámara ASCOM no conectada")
        self._apply_frame()
        self.cam.StartExposure(float(exposure_s), True)
        deadline = time.monotonic() + exposure_s + 30
        while not self.cam.ImageReady:
            if time.monotonic() > deadline:
                raise CameraError("Timeout esperando la imagen ASCOM")
            time.sleep(0.001)
        # ASCOM entrega ImageArray como [x][y] -> transponer a [y][x]
        return np.asarray(self.cam.ImageArray, dtype=np.float32).T


# ---------------------------------------------------------------------------
# ZWO ASI (SDK nativo, modo video = alta cadencia)
# ---------------------------------------------------------------------------
class ZwoCamera(Camera):
    backend = "zwo"

    def __init__(self, sdk_path: str = "", index: int = 0) -> None:
        super().__init__()
        self.sdk_path = sdk_path
        self.index = index
        self.cam = None
        self._exp_us = None
        self._video = False

    def connect(self) -> None:
        try:
            import zwoasi as asi
        except ImportError as e:
            raise CameraError("Falta el paquete 'zwoasi' (pip install zwoasi)") from e
        self.asi = asi
        path = self.sdk_path
        if not path:
            from pathlib import Path
            lib = "ASICamera2.dll" if sys.platform == "win32" else "libASICamera2.dylib"
            # buscar junto al .exe y en la carpeta actual
            for d in (Path(sys.executable).parent, Path.cwd()):
                if (d / lib).exists():
                    path = str(d / lib)
                    break
            else:
                path = lib
        try:
            asi.init(path)
        except asi.ZWO_Error as e:
            if "already" not in str(e).lower():
                raise CameraError(f"No se pudo cargar el SDK de ZWO ({path}): {e}") from e
        if asi.get_num_cameras() == 0:
            raise CameraError("No se encontró ninguna cámara ZWO")
        self.cam = asi.Camera(self.index)
        props = self.cam.get_camera_property()
        self.name = props["Name"]
        self.width, self.height = props["MaxWidth"], props["MaxHeight"]
        self.cam.set_control_value(asi.ASI_BANDWIDTHOVERLOAD, 80)
        self.cam.set_image_type(asi.ASI_IMG_RAW16)
        self._reset_roi()

    def _reset_roi(self) -> None:
        if self._video:
            self.cam.stop_video_capture()
            self._video = False
        b = self.binning
        if self.roi:
            x0, y0, w, h = self.roi
            w, h = (w // b) // 8 * 8, (h // b) // 2 * 2
            self.cam.set_roi(start_x=x0 // b, start_y=y0 // b, width=max(w, 8), height=max(h, 2), bins=b)
        else:
            self.cam.set_roi(bins=b)

    def set_binning(self, b: int) -> None:
        super().set_binning(b)
        if self.cam:
            self._reset_roi()

    def set_roi(self, roi) -> None:
        super().set_roi(roi)
        if self.cam:
            self._reset_roi()

    def set_gain(self, gain: int) -> None:
        if self.cam:
            self.cam.set_control_value(self.asi.ASI_GAIN, int(gain))

    def disconnect(self) -> None:
        if self.cam:
            try:
                if self._video:
                    self.cam.stop_video_capture()
                self.cam.close()
            finally:
                self.cam = None
                self._video = False

    def grab(self, exposure_s: float) -> np.ndarray:
        if self.cam is None:
            raise CameraError("Cámara ZWO no conectada")
        us = int(exposure_s * 1e6)
        if us != self._exp_us:
            self.cam.set_control_value(self.asi.ASI_EXPOSURE, us)
            self._exp_us = us
        if not self._video:
            self.cam.start_video_capture()
            self._video = True
        timeout_ms = int(exposure_s * 2000) + 500
        return self.cam.capture_video_frame(timeout=timeout_ms).astype(np.float32)


# ---------------------------------------------------------------------------
# Webcam / UVC (OpenCV)
# ---------------------------------------------------------------------------
def webcam_names() -> list[str]:
    """Nombres de las cámaras DirectShow en el orden de sus índices (solo Windows)."""
    if sys.platform != "win32":
        return []
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(FilterGraph().get_input_devices())
    except Exception:  # noqa: BLE001 — pygrabber es opcional
        return []


def list_webcams(max_index: int = 6, in_use: dict | None = None) -> list[dict]:
    """Prueba los índices 0..max_index y devuelve las cámaras que abren.
    `in_use`: cámara que esta app ya tiene abierta; no se vuelve a abrir (en
    Windows abrirla dos veces puede cortar el video)."""
    try:
        import cv2
    except ImportError:
        return []
    names = webcam_names()
    found = []
    for i in range(max_index):
        if names and i >= len(names):
            break
        if in_use and in_use["index"] == i:
            found.append({**in_use, "in_use": True})
            continue
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if sys.platform == "win32" else cv2.VideoCapture(i)
        try:
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                label = names[i] if i < len(names) else f"Cámara #{i}"
                found.append({"index": i, "name": label, "width": w, "height": h})
        finally:
            cap.release()
    return found


class WebcamCamera(Camera):
    """Cámaras USB tipo webcam (UVC): cámaras de ocular, planetarias baratas, etc.

    En Windows se intenta DirectShow y, si falla, Media Foundation.
    """

    backend = "webcam"
    max_adu = 255

    def __init__(self, index: int = 0, width: int = 0, height: int = 0) -> None:
        super().__init__()
        self.index = index
        self.req_size = (width, height)
        self.cap = None
        self._exp = None
        self._fails = 0
        self.exposure_note = ""

    def _open(self, api) -> bool:
        self.cap = self.cv2.VideoCapture(self.index, api)
        if self.cap.isOpened():
            ok, _ = self.cap.read()
            if ok:
                return True
        self.cap.release()
        self.cap = None
        return False

    def connect(self) -> None:
        try:
            import cv2
        except ImportError as e:
            raise CameraError("Falta OpenCV (pip install opencv-python)") from e
        self.cv2 = cv2
        apis = [cv2.CAP_DSHOW, cv2.CAP_MSMF] if sys.platform == "win32" else [cv2.CAP_ANY]
        if not any(self._open(api) for api in apis):
            raise CameraError(f"No se pudo abrir la cámara #{self.index}. "
                              "¿Está conectada? ¿La está usando otro programa (Zoom, SharpCap…)?")
        w, h = self.req_size
        if w and h:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # exposición manual (DirectShow: 0.25 = manual, 0.75 = auto)
        self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25 if sys.platform == "win32" else 1)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        names = webcam_names()
        self.name = names[self.index] if self.index < len(names) else f"Cámara USB #{self.index}"

    def disconnect(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def set_gain(self, gain: int) -> None:
        if self.cap is not None:
            self.cap.set(self.cv2.CAP_PROP_GAIN, float(gain))

    def grab(self, exposure_s: float) -> np.ndarray:
        if self.cap is None:
            raise CameraError("Cámara USB no conectada")
        if exposure_s != self._exp:
            # DirectShow usa log2(segundos): -8 ≈ 3.9 ms, -7 ≈ 7.8 ms, -5 ≈ 31 ms
            val = round(math.log2(max(exposure_s, 1e-4))) if sys.platform == "win32" else exposure_s * 10000
            self.cap.set(self.cv2.CAP_PROP_EXPOSURE, val)
            self._exp = exposure_s
            got = self.cap.get(self.cv2.CAP_PROP_EXPOSURE)
            if sys.platform == "win32" and got < 0:
                self.exposure_note = f"exposición real ≈ {2 ** got * 1000:.1f} ms (valor {got:g})"
            else:
                self.exposure_note = f"exposición reportada por el driver: {got:g}"
        ok, frame = self.cap.read()
        if not ok or frame is None:
            # fallos sueltos de USB son normales; solo error si se repiten
            self._fails += 1
            if self._fails >= 10:
                raise CameraError("La cámara dejó de entregar imágenes (¿cable USB?)")
            raise TransientFrameError("frame perdido")
        self._fails = 0
        if frame.ndim == 3:
            frame = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
        img = frame.astype(np.float32)
        if self.roi:
            x0, y0, w, h = self.roi
            img = img[y0:y0 + h, x0:x0 + w]
        return img


BACKENDS = {
    "webcam": "Cámara USB (webcam / UVC)",
    "ascom": "ASCOM (driver de la marca)",
    "zwo": "ZWO ASI (SDK directo)",
    "simulator": "Simulador (sin hardware)",
}


def make_camera(backend: str, cfg, **opts) -> Camera:
    if backend == "simulator":
        return SimulatorCamera(cfg)
    if backend == "ascom":
        return AscomCamera(opts.get("prog_id") or None)
    if backend == "zwo":
        return ZwoCamera(cfg.zwo_sdk_path, int(opts.get("index") or 0))
    if backend == "webcam":
        return WebcamCamera(int(opts.get("index") or 0), cfg.webcam_width, cfg.webcam_height)
    raise CameraError(f"Backend desconocido: {backend}")
