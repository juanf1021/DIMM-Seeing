"""Pruebas del flujo completo con el simulador: medir y calibrar la escala por deriva."""

import time

import numpy as np
import pytest

from dimm_app import config as config_mod
from dimm_app import engine as engine_mod
from dimm_app.analysis import calibrate_from_drift, find_spots
from dimm_app.config import Config


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(engine_mod, "data_dir", lambda: tmp_path)
    cfg = Config()
    cfg.frames_per_measure = 60
    cfg.min_valid_frames = 30
    cfg.sim_seeing_arcsec = 1.5
    eng = engine_mod.Engine(cfg)
    yield eng
    eng.shutdown()


def wait_for(cond, timeout=30.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_measure_with_simulator(engine, tmp_path):
    engine.cfg.sim_drift = False
    engine.cfg.site_lat_deg, engine.cfg.site_lon_deg = 4.6, -74.08
    engine.connect_camera("simulator")
    engine.set_manual_coords(0.0, 4.6, "Cenital")   # dec = latitud -> pasa cerca del cenit
    engine.start_measure(continuous=False)
    assert wait_for(lambda: engine.results), "no terminó la medición"
    r = engine.results[0]
    assert r["seeing_arcsec"] == pytest.approx(1.5, rel=0.3)
    assert r["coord_source"] == "catalog:Cenital"
    assert engine.mode == "live"
    csvs = list(tmp_path.glob("seeing_*.csv"))
    assert csvs and "seeing_arcsec" in csvs[0].read_text(encoding="utf-8-sig")


def test_csv_locked_does_not_stop_measuring(engine, monkeypatch):
    """En Windows, si Excel tiene abierto el CSV, la medición debe seguir."""
    engine.cfg.sim_drift = False
    real_open = engine_mod.Path.open

    def locked(self, *a, **k):
        if self.suffix == ".csv":
            raise PermissionError("archivo en uso")
        return real_open(self, *a, **k)

    monkeypatch.setattr(engine_mod.Path, "open", locked)
    engine.connect_camera("simulator")
    engine.start_measure(continuous=True)
    assert wait_for(lambda: len(engine.results) >= 2), "la medición se detuvo"
    assert engine.mode == "measure"
    assert any("CSV" in lg["msg"] for lg in engine.logs)


def test_drift_calibration_with_simulator(engine):
    engine.cfg.sim_drift = True
    engine.cfg.pixscale_override_arcsec = 0.0
    engine.connect_camera("simulator")
    engine.set_manual_coords(0.0, 0.0, "Ecuatorial")
    cam = engine.camera
    cam._x0, cam._t0 = 60.0, time.monotonic()   # entra por el borde: no alcanza a dar la vuelta
    engine.start_calibration(duration_s=6)
    assert wait_for(lambda: engine.mode == "live", timeout=20)
    assert engine.last_calibration, [lg["msg"] for lg in engine.logs]
    assert engine.cfg.pixscale_override_arcsec == pytest.approx(engine.cfg.physical_pixscale, rel=0.03)


def test_calibration_requires_star(engine):
    engine.connect_camera("simulator")
    with pytest.raises(RuntimeError, match="estrella"):
        engine.start_calibration()


def test_calibrate_from_drift_math():
    t = np.linspace(0, 10, 200)
    speed = 15.041 * np.cos(np.radians(20)) / 0.5          # escala real 0.5"/px
    ang = np.radians(30)
    rng = np.random.default_rng(0)
    x = 100 + speed * np.cos(ang) * t + rng.normal(0, 0.8, t.size)
    y = 200 + speed * np.sin(ang) * t + rng.normal(0, 0.8, t.size)
    cal = calibrate_from_drift(t, x, y, 20.0)
    assert cal.pixscale_arcsec == pytest.approx(0.5, rel=0.01)
    assert cal.angle_deg == pytest.approx(30, abs=1)
    assert cal.r2 > 0.99


def test_calibrate_rejects_tracking_mount():
    t = np.linspace(0, 10, 100)
    with pytest.raises(ValueError, match="siguiendo"):
        calibrate_from_drift(t, np.full(100, 50.0), np.full(100, 50.0), 10.0)


def test_hot_pixels_are_ignored():
    rng = np.random.default_rng(1)
    img = rng.normal(100, 5, (200, 300)).astype(np.float32)
    img[20, 30] = img[150, 250] = 4000                      # pixeles calientes
    yy, xx = np.mgrid[0:200, 0:300]
    for cx in (120, 170):
        img += 800 * np.exp(-((xx - cx) ** 2 + (yy - 100) ** 2) / (2 * 2.0**2))
    spots = find_spots(img, 2)
    assert sorted(round(s.x) for s in spots) == [120, 170]
