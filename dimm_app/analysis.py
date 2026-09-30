"""Detección de las 2 imágenes de la estrella y cálculo DIMM del seeing.

Referencia: Tokovinin (2002), PASP 114, 1156 — "From Differential Image Motion
to Seeing". Varianza del movimiento diferencial (tilt G, en rad^2):

    sigma_l^2 = K_l * lambda^2 * r0^(-5/3) * D^(-1/3)   (longitudinal)
    sigma_t^2 = K_t * lambda^2 * r0^(-5/3) * D^(-1/3)   (transversal)

    K_l = 0.364 * (1 - 0.532 b^(-1/3) - 0.024 b^(-7/3))
    K_t = 0.364 * (1 - 0.798 b^(-1/3) + 0.018 b^(-7/3)),   b = B / D

Seeing (FWHM) = 0.98 * lambda / r0. La varianza del tilt es acromática, así
que usar lambda = 500 nm da r0 y seeing referidos a 500 nm.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

RAD2ARCSEC = 206264.806


@dataclass
class Spot:
    x: float
    y: float
    flux: float
    peak: float


def k_coefficients(d_m: float, b_m: float) -> tuple[float, float]:
    b = b_m / d_m
    kl = 0.364 * (1 - 0.532 * b ** (-1 / 3) - 0.024 * b ** (-7 / 3))
    kt = 0.364 * (1 - 0.798 * b ** (-1 / 3) + 0.018 * b ** (-7 / 3))
    return kl, kt


def find_spots(img: np.ndarray, n: int = 2, k_sigma: float = 6.0, box: int = 15,
               min_area: int = 3) -> list[Spot]:
    """Encuentra las `n` fuentes más brillantes y devuelve su centroide.
    Descarta manchas de menos de `min_area` pixeles (pixeles calientes, rayos cósmicos)."""
    img = np.asarray(img, dtype=np.float32)
    # fondo y ruido sobre una submuestra: 16x más rápido, igual de robusto
    step = 4 if img.size > 200_000 else 2 if img.size > 20_000 else 1
    sample = img[::step, ::step]
    bg = float(np.median(sample))
    mad = float(np.median(np.abs(sample - bg)))
    noise = max(1.4826 * mad, 1e-3)
    mask = img > bg + k_sigma * noise
    if not mask.any():
        return []
    labels, nlab = ndimage.label(mask)
    if nlab == 0:
        return []
    idx = np.arange(1, nlab + 1)
    area = ndimage.sum(mask, labels, idx)
    idx = idx[area >= min_area]
    if idx.size == 0:
        return []
    flux = ndimage.sum(img - bg, labels, idx)
    order = np.argsort(flux)[::-1][:n]
    peaks = ndimage.maximum_position(img, labels, idx[order])

    h, w = img.shape
    half = box // 2
    spots: list[Spot] = []
    for (py, px) in peaks:
        y0, y1 = max(0, py - half), min(h, py + half + 1)
        x0, x1 = max(0, px - half), min(w, px + half + 1)
        sub = img[y0:y1, x0:x1] - bg
        sub = np.clip(sub - k_sigma * noise * 0.5, 0, None)
        tot = float(sub.sum())
        if tot <= 0:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        spots.append(Spot(
            x=float((xx * sub).sum() / tot),
            y=float((yy * sub).sum() / tot),
            flux=tot,
            peak=float(img[py, px]),
        ))
    return spots


def pair_vector(spots: list[Spot], ref: np.ndarray | None) -> np.ndarray | None:
    """Vector (dx, dy) entre las 2 imágenes, orientado igual que `ref`."""
    if len(spots) < 2:
        return None
    a, b = spots[0], spots[1]
    v = np.array([b.x - a.x, b.y - a.y])
    if ref is None:
        # orden estable: el vector apunta a +x (o +y si es casi vertical)
        if abs(v[0]) >= abs(v[1]):
            return v if v[0] >= 0 else -v
        return v if v[1] >= 0 else -v
    return v if float(np.dot(v, ref)) >= 0 else -v


@dataclass
class SeeingResult:
    seeing_arcsec: float        # promedio l/t, en la línea de vista
    seeing_zenith: float | None  # corregido por masa de aire
    seeing_l: float
    seeing_t: float
    r0_cm: float
    sigma_l_arcsec: float
    sigma_t_arcsec: float
    separation_px: float
    n_frames: int

    def to_dict(self) -> dict:
        return {k: (round(float(v), 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def compute_seeing(
    vectors: np.ndarray,
    pixscale_arcsec: float,
    d_m: float,
    b_m: float,
    wavelength_m: float = 500e-9,
    airmass: float | None = None,
) -> SeeingResult:
    """vectors: array (N, 2) de separaciones (dx, dy) en pixeles, una por frame."""
    v = np.asarray(vectors, dtype=float)
    if v.ndim != 2 or v.shape[0] < 3:
        raise ValueError("se necesitan al menos 3 frames válidos")
    mean = v.mean(axis=0)
    sep = float(np.hypot(*mean))
    u = mean / sep
    perp = np.array([-u[1], u[0]])
    lon = v @ u
    tra = v @ perp

    scale_rad = pixscale_arcsec / RAD2ARCSEC
    var_l = float(np.var(lon, ddof=1)) * scale_rad**2
    var_t = float(np.var(tra, ddof=1)) * scale_rad**2
    kl, kt = k_coefficients(d_m, b_m)
    lam = wavelength_m

    def r0_from(var: float, k: float) -> float:
        var = max(var, 1e-30)
        return (k * lam**2 * d_m ** (-1 / 3) / var) ** (3 / 5)

    r0_l = r0_from(var_l, kl)
    r0_t = r0_from(var_t, kt)
    eps_l = 0.98 * lam / r0_l * RAD2ARCSEC
    eps_t = 0.98 * lam / r0_t * RAD2ARCSEC
    eps = 0.5 * (eps_l + eps_t)
    r0 = 0.98 * lam / (eps / RAD2ARCSEC)
    eps_z = eps * airmass ** (-3 / 5) if airmass and airmass >= 1 else None

    return SeeingResult(
        seeing_arcsec=eps,
        seeing_zenith=eps_z,
        seeing_l=eps_l,
        seeing_t=eps_t,
        r0_cm=r0 * 100,
        sigma_l_arcsec=np.sqrt(var_l) * RAD2ARCSEC,
        sigma_t_arcsec=np.sqrt(var_t) * RAD2ARCSEC,
        separation_px=sep,
        n_frames=int(v.shape[0]),
    )


SIDEREAL_ARCSEC_PER_S = 15.041


@dataclass
class DriftCalibration:
    pixscale_arcsec: float     # escala medida
    speed_px_s: float          # velocidad de la deriva en el sensor
    angle_deg: float           # dirección de la deriva (0 = +x, 90 = +y): es el eje Este-Oeste
    r2: float                  # calidad del ajuste lineal (1 = perfecto)
    n_points: int
    duration_s: float

    def to_dict(self) -> dict:
        return {k: (round(float(v), 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def calibrate_from_drift(t: np.ndarray, x: np.ndarray, y: np.ndarray, dec_deg: float) -> DriftCalibration:
    """Calibra la escala de placa con la deriva de una estrella (telescopio SIN seguimiento).

    La estrella se mueve a 15.041"/s * cos(Dec). Midiendo cuántos pixeles por
    segundo se mueve en la imagen se obtiene la escala en "/px, sin necesidad de
    saber el tamaño de pixel ni si la cámara escala la imagen.
    """
    t, x, y = (np.asarray(a, dtype=float) for a in (t, x, y))
    if t.size < 10:
        raise ValueError("muy pocos puntos (la estrella no se vio el tiempo suficiente)")
    if abs(dec_deg) > 75:
        raise ValueError("use una estrella con |Dec| < 75° (cerca del polo casi no se mueve)")
    t = t - t[0]
    A = np.vstack([t, np.ones_like(t)]).T
    (vx, x0), *_ = np.linalg.lstsq(A, x, rcond=None)
    (vy, y0), *_ = np.linalg.lstsq(A, y, rcond=None)
    speed = float(np.hypot(vx, vy))
    resid = np.concatenate([x - (vx * t + x0), y - (vy * t + y0)])
    total = np.concatenate([x - x.mean(), y - y.mean()])
    r2 = 1.0 - float(np.sum(resid**2)) / max(float(np.sum(total**2)), 1e-12)
    if speed * t[-1] < 20:
        raise ValueError("la estrella casi no se movió: ¿el telescopio está siguiendo? "
                         "Apague el seguimiento o use la escala teórica")
    rate = SIDEREAL_ARCSEC_PER_S * np.cos(np.radians(dec_deg))
    return DriftCalibration(
        pixscale_arcsec=rate / speed,
        speed_px_s=speed,
        angle_deg=float(np.degrees(np.arctan2(vy, vx))),
        r2=r2,
        n_points=int(t.size),
        duration_s=float(t[-1]),
    )


def differential_sigma_px(seeing_arcsec: float, pixscale_arcsec: float, d_m: float, b_m: float,
                          wavelength_m: float = 500e-9) -> tuple[float, float]:
    """Inverso: sigma esperada (px) del movimiento diferencial para un seeing dado.
    Lo usa el simulador y los tests."""
    r0 = 0.98 * wavelength_m / (seeing_arcsec / RAD2ARCSEC)
    kl, kt = k_coefficients(d_m, b_m)
    base = wavelength_m**2 * r0 ** (-5 / 3) * d_m ** (-1 / 3)
    to_px = RAD2ARCSEC / pixscale_arcsec
    return float(np.sqrt(kl * base) * to_px), float(np.sqrt(kt * base) * to_px)
