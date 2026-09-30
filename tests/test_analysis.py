"""El simulador genera un seeing conocido; el análisis debe recuperarlo."""

import numpy as np
import pytest

from dimm_app.analysis import compute_seeing, differential_sigma_px, find_spots, pair_vector
from dimm_app.astro import airmass, parse_sexagesimal
from dimm_app.cameras import SimulatorCamera
from dimm_app.config import Config
from dimm_app.fitsio import write_fits

PIX = 0.305
D, B = 0.06, 0.175


@pytest.mark.parametrize("seeing", [0.8, 1.5, 3.0])
def test_recovers_seeing_from_vectors(seeing):
    rng = np.random.default_rng(1)
    sl, st = differential_sigma_px(seeing, PIX, D, B)
    n = 4000
    sep = np.array([46.0, 9.0])
    u = sep / np.linalg.norm(sep)
    p = np.array([-u[1], u[0]])
    v = sep + np.outer(rng.normal(0, sl, n), u) + np.outer(rng.normal(0, st, n), p)
    r = compute_seeing(v, PIX, D, B)
    assert r.seeing_l == pytest.approx(seeing, rel=0.05)
    assert r.seeing_t == pytest.approx(seeing, rel=0.05)
    assert r.seeing_arcsec == pytest.approx(seeing, rel=0.05)


def test_zenith_correction():
    rng = np.random.default_rng(2)
    v = np.array([40.0, 0]) + rng.normal(0, 2, (500, 2))
    r = compute_seeing(v, PIX, D, B, airmass=2.0)
    assert r.seeing_zenith == pytest.approx(r.seeing_arcsec * 2 ** -0.6)


def test_end_to_end_simulator(monkeypatch):
    """Imágenes sintéticas -> detección -> seeing, dentro de ±15%."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    cfg = Config()
    cfg.sim_seeing_arcsec = 1.5
    cfg.sim_drift = False
    cam = SimulatorCamera(cfg)
    cam.rng = np.random.default_rng(3)
    ref, vecs = None, []
    for _ in range(400):
        img = cam.grab(0.005)
        spots = find_spots(img, 2, cfg.detect_sigma, cfg.centroid_box)
        assert len(spots) == 2
        v = pair_vector(spots, ref)
        ref = v if ref is None else ref
        vecs.append(v)
    r = compute_seeing(np.array(vecs), cfg.pixscale_arcsec, cfg.aperture_d_m, cfg.baseline_b_m)
    assert r.seeing_arcsec == pytest.approx(1.5, rel=0.15)
    assert r.separation_px == pytest.approx(np.hypot(46, 9), abs=1)


def test_pair_vector_orientation_is_stable():
    from dimm_app.analysis import Spot
    a, b = Spot(10, 10, 1, 1), Spot(50, 12, 1, 1)
    v1 = pair_vector([a, b], None)
    v2 = pair_vector([b, a], v1)
    assert np.allclose(v1, v2)


@pytest.mark.parametrize("s,expected", [
    ("12:30:00", 12.5),
    ("12:30.0", 12.5),
    ("+45*30'00", 45.5),
    ("-05\xdf30", -5.5),
    ("-05*30:36#", -5.51),
    ("074*05", 74 + 5 / 60),
])
def test_parse_lx200_formats(s, expected):
    assert parse_sexagesimal(s) == pytest.approx(expected, abs=1e-4)


def test_airmass():
    assert airmass(90) == pytest.approx(1.0, abs=1e-3)
    assert airmass(30) == pytest.approx(1.995, abs=0.01)
    assert airmass(-1) is None


def test_altaz_polaris_matches_latitude():
    from datetime import datetime, timezone
    from dimm_app.astro import altaz
    from dimm_app.stars import STARS
    polaris = next(s for s in STARS if s["name"] == "Polaris")
    alt, _ = altaz(polaris["ra_deg"], polaris["dec_deg"], 40.0, -74.0,
                   datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert alt == pytest.approx(40.0, abs=1.2)   # Polaris está a <1° del polo


def test_visible_stars_sorted_by_altitude():
    from dimm_app.stars import visible_stars
    s = visible_stars(4.6, -74.08)
    alts = [x["alt_deg"] for x in s]
    assert alts == sorted(alts, reverse=True) and all(a >= 0 for a in alts)


def test_fits_writer(tmp_path):
    img = np.arange(12, dtype=np.uint16).reshape(3, 4) * 1000
    p = write_fits(tmp_path / "t.fits", img, {"EXPTIME": 0.005})
    raw = p.read_bytes()
    assert len(raw) % 2880 == 0
    assert raw.startswith(b"SIMPLE  =                    T")
    data = np.frombuffer(raw[2880:2880 + 24], dtype=">i2").astype(np.int32) + 32768
    assert np.array_equal(data.reshape(3, 4), img[::-1])
