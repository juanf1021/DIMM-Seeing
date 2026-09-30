"""Punto de entrada: arranca el servidor local y abre el navegador.

    python run.py            -> http://localhost:8000 (o el siguiente puerto libre)
    python run.py --port 9000 --no-browser
    python run.py --host 0.0.0.0   (para verlo desde el celular en la misma red)
"""

import argparse
import socket
import sys
import threading
import webbrowser

import uvicorn

from dimm_app import __version__
from dimm_app.server import app


def free_port(host: str, start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # igual que uvicorn: en Mac/Linux ignorar conexiones viejas en TIME_WAIT
            # (en Windows SO_REUSEADDR permitiría robar un puerto en uso: no se usa)
            if sys.platform != "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No hay puertos libres entre {start} y {start + tries - 1}")


def main() -> None:
    ap = argparse.ArgumentParser(description="DIMM Seeing Monitor")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    port = free_port(args.host, args.port)
    url = f"http://localhost:{port}"
    print(f"DIMM Seeing Monitor v{__version__}")
    print(f"Interfaz: {url}")
    print("Deje esta ventana abierta mientras usa la app. Ciérrela para salir.")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=args.host, port=port, log_level="warning")
    except Exception as e:  # noqa: BLE001 — en el .exe, que el error quede visible
        print(f"\nError: {e}")
        if getattr(sys, "frozen", False):
            input("Presione Enter para cerrar…")
        raise


if __name__ == "__main__":
    main()
