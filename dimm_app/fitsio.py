"""Escritor FITS mínimo (16 bits sin signo), sin depender de astropy."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np


def _card(key: str, value, comment: str = "") -> bytes:
    if isinstance(value, bool):
        v = "T" if value else "F"
        s = f"{key:<8}= {v:>20}"
    elif isinstance(value, (int, np.integer)):
        s = f"{key:<8}= {int(value):>20}"
    elif isinstance(value, (float, np.floating)):
        s = f"{key:<8}= {float(value):>20.10G}"
    else:
        s = f"{key:<8}= '{str(value):<8}'"
    if comment:
        s += f" / {comment}"
    return s[:80].ljust(80).encode("ascii")


def write_fits(path: str | Path, img: np.ndarray, header: dict | None = None) -> Path:
    path = Path(path)
    data = np.clip(np.asarray(img), 0, 65535).astype(np.uint16)
    h, w = data.shape
    cards = [
        _card("SIMPLE", True),
        _card("BITPIX", 16),
        _card("NAXIS", 2),
        _card("NAXIS1", w),
        _card("NAXIS2", h),
        _card("BZERO", 32768),
        _card("BSCALE", 1),
        _card("DATE-OBS", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]),
    ]
    for k, v in (header or {}).items():
        if v is not None:
            cards.append(_card(k.upper()[:8], v))
    cards.append(b"END".ljust(80))
    hdr = b"".join(cards)
    hdr += b" " * (-len(hdr) % 2880)
    # FITS: big-endian int16 con BZERO=32768; fila 0 = abajo (convención FITS)
    raw = (data.astype(np.int32) - 32768).astype(">i2")[::-1].tobytes()
    raw += b"\0" * (-len(raw) % 2880)
    path.write_bytes(hdr + raw)
    return path
