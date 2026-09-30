"""Servidor web local: API + interfaz (http://localhost:8000)."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .astro import parse_sexagesimal
from .cameras import BACKENDS, AscomCamera, list_webcams
from .config import Config, data_dir
from .engine import Engine
from .stars import STARS, visible_stars
from .telescope import list_serial_ports


def static_dir() -> Path:
    # PyInstaller descomprime los datos en sys._MEIPASS
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
    return base / "dimm_app" / "static"


cfg = Config.load()
engine: Engine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = Engine(cfg)
    yield
    engine.shutdown()


app = FastAPI(title="DIMM Seeing Monitor", version=__version__, lifespan=lifespan)


def _run(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — mensaje claro para la interfaz
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/")
def index():
    return FileResponse(static_dir() / "index.html")


@app.get("/api/info")
def info():
    return {"version": __version__, "backends": BACKENDS, "platform": sys.platform,
            "data_dir": str(data_dir())}


@app.get("/api/state")
def state(since_log: int = 0, since_result: int = 0):
    return engine.state(since_log, since_result)


@app.get("/api/preview.jpg")
def preview():
    if not engine.preview_jpeg:
        return Response(status_code=204)
    return Response(engine.preview_jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- config
@app.get("/api/config")
def get_config():
    return cfg.to_dict()


@app.post("/api/config")
def set_config(data: dict = Body(...)):
    _run(cfg.update, data)
    cfg.save()
    if engine.camera:
        _run(engine.apply_config)
    return cfg.to_dict()


# ---------------------------------------------------------------- cámara
@app.post("/api/camera/connect")
def camera_connect(data: dict = Body(...)):
    backend = data.get("backend", "simulator")
    return _run(engine.connect_camera, backend, prog_id=data.get("prog_id"), index=data.get("index"))


@app.get("/api/webcams")
def webcams():
    """Cámaras USB disponibles (índice + nombre en Windows)."""
    cam = engine.camera
    in_use = None
    if cam is not None and cam.backend == "webcam":
        in_use = {"index": cam.index, "name": cam.name, "width": cam.width, "height": cam.height}
    return list_webcams(in_use=in_use)


@app.post("/api/camera/ascom-choose")
def ascom_choose():
    return {"prog_id": _run(engine.call, AscomCamera.choose, 300)}


@app.post("/api/camera/disconnect")
def camera_disconnect():
    _run(engine.disconnect_camera)
    return {"ok": True}


@app.post("/api/live")
def live(data: dict = Body(...)):
    _run(engine.set_live, bool(data.get("on", True)))
    return {"ok": True}


# ---------------------------------------------------------------- medición
@app.post("/api/measure/start")
def measure_start(data: dict = Body(default={})):
    _run(engine.start_measure, bool(data.get("continuous", False)))
    return {"ok": True}


@app.post("/api/measure/stop")
def measure_stop():
    _run(engine.stop_measure)
    return {"ok": True}


@app.post("/api/calibrate/start")
def calibrate_start(data: dict = Body(default={})):
    _run(engine.start_calibration, float(data.get("duration_s", 15)))
    return {"ok": True}


@app.post("/api/calibrate/reset")
def calibrate_reset():
    engine.reset_calibration()
    return cfg.to_dict()


@app.get("/api/results.csv")
def results_csv():
    return PlainTextResponse(engine.results_csv(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=seeing.csv"})


# ---------------------------------------------------------------- telescopio
@app.get("/api/serial-ports")
def serial_ports():
    return list_serial_ports()


@app.post("/api/telescope/connect")
def telescope_connect(data: dict = Body(...)):
    port = data.get("port")
    if not port:
        raise HTTPException(400, "Seleccione un puerto")
    _run(engine.telescope.connect, port)
    engine.log("ok", f"LX200 conectado en {port}")
    return engine.telescope.state.__dict__


@app.post("/api/telescope/disconnect")
def telescope_disconnect():
    engine.telescope.disconnect()
    engine.log("info", "LX200 desconectado")
    return {"ok": True}


# ---------------------------------------------------------------- coordenadas
@app.post("/api/coords/manual")
def coords_manual(data: dict = Body(...)):
    try:
        ra = parse_sexagesimal(str(data["ra"])) * 15.0     # horas -> grados
        dec = parse_sexagesimal(str(data["dec"]))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, "Formato: RA 05:14:32  Dec -08:12:05") from e
    engine.set_manual_coords(ra, dec)
    return engine.coords


@app.get("/api/stars")
def stars():
    return visible_stars(cfg.site_lat_deg, cfg.site_lon_deg)


@app.post("/api/coords/star")
def coords_star(data: dict = Body(...)):
    star = next((s for s in STARS if s["name"] == data.get("name")), None)
    if not star:
        raise HTTPException(400, "Estrella desconocida")
    engine.set_manual_coords(star["ra_deg"], star["dec_deg"], star["name"])
    return engine.coords


@app.post("/api/platesolve")
def platesolve():
    _run(engine.plate_solve)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=static_dir()), name="static")
