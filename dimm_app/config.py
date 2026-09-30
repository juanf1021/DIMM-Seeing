"""Configuración persistente (JSON en la carpeta del usuario)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


def data_dir() -> Path:
    """Carpeta donde se guardan config, logs y FITS."""
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "DIMM-Seeing"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    # --- Óptica ---
    focal_length_mm: float = 2540.0      # LX200 10" f/10
    pixel_size_um: float = 3.75          # tamaño de pixel de la cámara
    binning: int = 1
    aperture_d_m: float = 0.06           # diámetro de cada agujero de la máscara
    baseline_b_m: float = 0.175          # distancia entre centros de los agujeros
    wavelength_nm: float = 500.0         # seeing reportado a esta longitud de onda

    # --- Adquisición ---
    exposure_ms: float = 5.0
    gain: int = 100
    frames_per_measure: int = 200
    min_valid_frames: int = 50
    detect_sigma: float = 6.0            # umbral de detección (sigmas sobre el fondo)
    centroid_box: int = 15               # ventana de centroide (px)
    saturation_adu: int = 60000

    # --- Sitio (se autocompleta desde el GPS del LX200) ---
    site_lat_deg: float | None = None
    site_lon_deg: float | None = None    # Este positivo

    # --- Plate solving (ASTAP) ---
    astap_path: str = r"C:\Program Files\astap\astap_cli.exe"
    solve_exposure_s: float = 2.0
    solve_radius_deg: float = 30.0

    # --- Guardado ---
    save_fits: bool = False

    # --- Simulador (para probar sin cielo) ---
    sim_seeing_arcsec: float = 1.5
    sim_drift: bool = True               # simula telescopio sin seguimiento

    # --- Escala calibrada (deriva de la estrella); 0 = usar focal y pixel ---
    pixscale_override_arcsec: float = 0.0

    # --- Cámaras (se recuerda la última usada) ---
    camera_backend: str = ""
    camera_index: int = 0
    ascom_prog_id: str = ""
    zwo_sdk_path: str = ""               # ruta a ASICamera2.dll (vacío = buscar)
    webcam_width: int = 0                # 0 = resolución por defecto del driver
    webcam_height: int = 0

    @property
    def physical_pixscale(self) -> float:
        """Escala teórica en arcsec/pixel según focal, pixel y binning."""
        return 206.265 * self.pixel_size_um * self.binning / self.focal_length_mm

    @property
    def pixscale_arcsec(self) -> float:
        """Escala usada en los cálculos: la calibrada si existe, si no la teórica."""
        if self.pixscale_override_arcsec > 0:
            return self.pixscale_override_arcsec
        return self.physical_pixscale

    def update(self, data: dict) -> None:
        names = {f.name: f for f in fields(self)}
        for k, v in data.items():
            if k not in names:
                continue
            cur = getattr(self, k)
            if v is None or v == "":
                if k in ("site_lat_deg", "site_lon_deg"):
                    setattr(self, k, None)
                continue
            if isinstance(cur, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "si", "sí")
            elif isinstance(cur, int) and not isinstance(cur, bool):
                v = int(float(v))
            elif isinstance(cur, float) or k in ("site_lat_deg", "site_lon_deg"):
                v = float(v)
            setattr(self, k, v)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pixscale_arcsec"] = round(self.pixscale_arcsec, 4)
        d["physical_pixscale"] = round(self.physical_pixscale, 4)
        return d

    # --- persistencia ---
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        p = data_dir() / "config.json"
        if p.exists():
            try:
                cfg.update(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        return cfg

    def save(self) -> None:
        d = asdict(self)
        (data_dir() / "config.json").write_text(json.dumps(d, indent=2), encoding="utf-8")
