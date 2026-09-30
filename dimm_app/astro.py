"""Utilidades astronómicas mínimas (sin astropy): tiempo sidéreo, alt/az, masa de aire."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone


def julian_date(t: datetime) -> float:
    t = t.astimezone(timezone.utc)
    return t.timestamp() / 86400.0 + 2440587.5


def lst_deg(t: datetime, lon_east_deg: float) -> float:
    """Tiempo sidéreo local en grados."""
    d = julian_date(t) - 2451545.0
    gmst = 280.46061837 + 360.98564736629 * d
    return (gmst + lon_east_deg) % 360.0


def altaz(ra_deg: float, dec_deg: float, lat_deg: float, lon_east_deg: float,
          t: datetime | None = None) -> tuple[float, float]:
    """Devuelve (altitud, azimut) en grados. Azimut desde el norte hacia el este."""
    t = t or datetime.now(timezone.utc)
    ha = math.radians((lst_deg(t, lon_east_deg) - ra_deg) % 360.0)
    dec, lat = math.radians(dec_deg), math.radians(lat_deg)
    sin_alt = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    cos_az = (math.sin(dec) - math.sin(alt) * math.sin(lat)) / max(math.cos(alt) * math.cos(lat), 1e-12)
    az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
    if math.sin(ha) > 0:
        az = 360.0 - az
    return math.degrees(alt), az


def airmass(alt_deg: float) -> float | None:
    """Masa de aire (Kasten & Young 1989). None si el objeto está bajo el horizonte."""
    if alt_deg <= 0:
        return None
    z = 90.0 - alt_deg
    return 1.0 / (math.cos(math.radians(z)) + 0.50572 * (96.07995 - z) ** -1.6364)


_SEXA = re.compile(r"([+-]?)\s*(\d+)[^\d.]+(\d+)(?:[^\d.]+(\d+(?:\.\d+)?))?")


def parse_sexagesimal(s: str) -> float:
    """'12:34:56', '+45*12'33', '-05ß30', '12:34.5' -> valor decimal."""
    s = s.strip().rstrip("#")
    m = _SEXA.match(s)
    if not m:
        return float(s)
    sign = -1.0 if m.group(1) == "-" else 1.0
    a, b = float(m.group(2)), m.group(3)
    rest = s[m.end(3):]
    # formato "MM.T" (minutos con décimas) en baja precisión del LX200
    if m.group(4) is None and rest.startswith("."):
        mm = float(b + rest.split("#")[0][:3])
        return sign * (a + mm / 60.0)
    c = float(m.group(4)) if m.group(4) else 0.0
    return sign * (a + float(b) / 60.0 + c / 3600.0)


def fmt_hms(hours: float) -> str:
    hours %= 24.0
    h = int(hours)
    m = int((hours - h) * 60)
    s = (hours - h - m / 60) * 3600
    return f"{h:02d}h{m:02d}m{s:04.1f}s"


def fmt_dms(deg: float) -> str:
    sign = "-" if deg < 0 else "+"
    deg = abs(deg)
    d = int(deg)
    m = int((deg - d) * 60)
    s = (deg - d - m / 60) * 3600
    return f"{sign}{d:02d}°{m:02d}'{s:04.1f}\""
