"""La API completa debe responder JSON válido en todos los estados (incluida la calibración)."""

import time

import pytest
from fastapi.testclient import TestClient

from dimm_app import config as config_mod
from dimm_app import engine as engine_mod
from dimm_app import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(engine_mod, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "data_dir", lambda: tmp_path)
    cfg = config_mod.Config()
    cfg.frames_per_measure, cfg.min_valid_frames = 60, 30
    cfg.site_lat_deg, cfg.site_lon_deg = 4.6, -74.08
    monkeypatch.setattr(server, "cfg", cfg)
    with TestClient(server.app) as c:
        yield c


def poll_until(c, cond, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = c.get("/api/state")
        assert r.status_code == 200, r.text
        s = r.json()
        if cond(s):
            return s
        time.sleep(0.2)
    raise AssertionError("timeout")


def test_full_flow(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/info").status_code == 200
    assert client.get("/api/stars").status_code == 200
    r = client.post("/api/camera/connect", json={"backend": "simulator"})
    assert r.status_code == 200, r.text
    assert client.post("/api/coords/star", json={"name": "Altair"}).status_code == 200

    # calibración: el estado debe seguir siendo JSON válido durante y después
    assert client.post("/api/calibrate/start", json={"duration_s": 6}).status_code == 200
    s = poll_until(client, lambda s: s["mode"] == "live", timeout=20)
    assert isinstance(s["pixscale"]["calibrated"], bool)

    assert client.post("/api/measure/start", json={"continuous": False}).status_code == 200
    s = poll_until(client, lambda s: s["n_results"] >= 1)
    assert client.get("/api/preview.jpg").headers["content-type"] == "image/jpeg"
    csv = client.get("/api/results.csv").text
    assert "seeing_arcsec" in csv and "catalog:Altair" in csv

    assert client.post("/api/calibrate/reset", json={}).status_code == 200
    assert client.post("/api/camera/disconnect", json={}).status_code == 200


def test_errors_are_readable(client):
    r = client.post("/api/measure/start", json={})
    assert r.status_code == 400 and "cámara" in r.json()["detail"]
    r = client.post("/api/coords/manual", json={"ra": "xx", "dec": "yy"})
    assert r.status_code == 400
    r = client.post("/api/camera/connect", json={"backend": "webcam", "index": 9})
    assert r.status_code == 400
