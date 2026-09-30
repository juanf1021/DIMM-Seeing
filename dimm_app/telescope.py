"""Meade LX200 GPS por puerto serial (protocolo LX200 clásico).

Conexión: cable RS-232 del puerto "RS-232" del telescopio + adaptador USB-serial.
9600 baudios, 8N1. Solo se LEEN datos (posición, sitio, hora); la app nunca
mueve el telescopio.

Ojo: si el tubo se mueve a mano con los frenos sueltos, el telescopio pierde la
referencia y RA/Dec del LX200 quedan mal. Para coordenadas confiables use
plate solving.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .astro import parse_sexagesimal


def list_serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [{"device": p.device, "description": p.description} for p in list_ports.comports()]


@dataclass
class TelescopeState:
    connected: bool = False
    port: str = ""
    ra_deg: float | None = None
    dec_deg: float | None = None
    alt_deg: float | None = None
    az_deg: float | None = None
    lat_deg: float | None = None
    lon_east_deg: float | None = None
    local_time: str | None = None
    date: str | None = None
    error: str | None = None
    updated: float = field(default=0.0)


class LX200:
    def __init__(self, port: str, baud: int = 9600, timeout: float = 1.0) -> None:
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None
        self.lock = threading.Lock()

    def connect(self) -> None:
        import serial

        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(0.3)
        self.ser.reset_input_buffer()
        # Asegura alta precisión (HH:MM:SS). :U# alterna; revisar formato y corregir.
        ra = self.query(":GR#")
        if ra and "." in ra:
            self.command(":U#")

    def close(self) -> None:
        if self.ser:
            self.ser.close()
            self.ser = None

    def command(self, cmd: str) -> None:
        with self.lock:
            self.ser.write(cmd.encode("latin-1"))

    def query(self, cmd: str) -> str:
        """Envía un comando y lee la respuesta hasta '#'."""
        with self.lock:
            self.ser.reset_input_buffer()
            self.ser.write(cmd.encode("latin-1"))
            raw = self.ser.read_until(b"#")
        return raw.decode("latin-1").strip().rstrip("#")

    def read_state(self) -> TelescopeState:
        st = TelescopeState(connected=True, port=self.port)
        ra = self.query(":GR#")
        dec = self.query(":GD#")
        if ra:
            st.ra_deg = parse_sexagesimal(ra) * 15.0
        if dec:
            st.dec_deg = parse_sexagesimal(dec)
        alt = self.query(":GA#")
        az = self.query(":GZ#")
        if alt:
            st.alt_deg = parse_sexagesimal(alt)
        if az:
            st.az_deg = parse_sexagesimal(az)
        st.updated = time.time()
        return st

    def read_site(self, st: TelescopeState) -> None:
        lat = self.query(":Gt#")
        lon = self.query(":Gg#")
        if lat:
            st.lat_deg = parse_sexagesimal(lat)
        if lon:
            # LX200: longitud positiva al OESTE -> convertir a Este positivo
            lw = parse_sexagesimal(lon)
            lon_e = -lw
            if lon_e < -180:
                lon_e += 360
            st.lon_east_deg = lon_e
        st.local_time = self.query(":GL#") or None
        st.date = self.query(":GC#") or None


class TelescopeMonitor:
    """Hilo que consulta el LX200 cada pocos segundos."""

    def __init__(self, on_site=None) -> None:
        self.scope: LX200 | None = None
        self.state = TelescopeState()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_site = on_site

    def connect(self, port: str) -> None:
        self.disconnect()
        scope = LX200(port)
        scope.connect()
        self.scope = scope
        st = scope.read_state()
        scope.read_site(st)
        self.state = st
        if self.on_site and st.lat_deg is not None and st.lon_east_deg is not None:
            self.on_site(st.lat_deg, st.lon_east_deg)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self.scope:
            self.scope.close()
        self.scope = None
        self.state = TelescopeState()

    def _loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                st = self.scope.read_state()
                st.lat_deg, st.lon_east_deg = self.state.lat_deg, self.state.lon_east_deg
                st.local_time, st.date = self.state.local_time, self.state.date
                self.state = st
            except Exception as e:  # noqa: BLE001 — mostrar cualquier error de serial
                self.state.error = str(e)
