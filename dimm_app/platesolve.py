"""Plate solving con ASTAP (gratis, offline): https://www.hnsky.org/astap.htm

Instalar ASTAP + una base de estrellas (p.ej. D50). La app llama al ejecutable
de línea de comandos y lee el archivo .ini que genera.

Importante: quitar la máscara DIMM (o desenfocar menos) al resolver; las
estrellas dobladas confunden al solver.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class SolveError(RuntimeError):
    pass


def find_astap(configured: str) -> str | None:
    candidates = [
        configured,
        r"C:\Program Files\astap\astap_cli.exe",
        r"C:\Program Files\astap\astap.exe",
        "/Applications/ASTAP.app/Contents/MacOS/astap",
        "/usr/local/bin/astap_cli",
        "/opt/astap/astap_cli",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return shutil.which("astap_cli") or shutil.which("astap")


def solve(fits_path: Path, astap: str, fov_height_deg: float, radius_deg: float = 30.0,
          ra_hint_deg: float | None = None, dec_hint_deg: float | None = None,
          timeout: float = 120.0) -> dict:
    """Devuelve {'ra_deg', 'dec_deg', 'rotation_deg', 'scale_arcsec'}."""
    exe = find_astap(astap)
    if not exe:
        raise SolveError("No se encontró ASTAP. Instálalo y configura la ruta en Ajustes.")
    cmd = [exe, "-f", str(fits_path), "-fov", f"{fov_height_deg:.4f}", "-r", f"{radius_deg:.1f}", "-z", "0"]
    if ra_hint_deg is not None and dec_hint_deg is not None:
        cmd += ["-ra", f"{ra_hint_deg / 15:.5f}", "-spd", f"{dec_hint_deg + 90:.5f}"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise SolveError("ASTAP tardó demasiado") from e

    ini = fits_path.with_suffix(".ini")
    if not ini.exists():
        raise SolveError("ASTAP no generó resultado")
    vals = {}
    for line in ini.read_text(errors="ignore").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    if vals.get("PLTSOLVD", "F").upper() != "T":
        raise SolveError(vals.get("ERROR") or "No se pudo resolver la imagen (pocas estrellas o campo equivocado)")
    out = {"ra_deg": float(vals["CRVAL1"]), "dec_deg": float(vals["CRVAL2"])}
    if "CROTA2" in vals:
        out["rotation_deg"] = float(vals["CROTA2"])
    if "CDELT2" in vals:
        out["scale_arcsec"] = abs(float(vals["CDELT2"])) * 3600
    return out
