"""Estrellas brillantes (mag < 2.7) de todo el cielo, J2000.

Con un campo de pocos minutos de arco el plate solving casi nunca funciona, así que
la forma práctica de tener coordenadas es elegir la estrella apuntada de esta lista.
La precesión desde J2000 (~20') no importa para calcular la masa de aire.
"""

from __future__ import annotations

from .astro import airmass, altaz, parse_sexagesimal

# nombre, RA (h:m:s), Dec (d:m:s), magnitud V
_STARS = """
Sirius|06:45:08.9|-16:42:58|-1.46
Canopus|06:23:57.1|-52:41:44|-0.74
Rigil Kentaurus|14:39:36.5|-60:50:02|-0.27
Arcturus|14:15:39.7|+19:10:57|-0.05
Vega|18:36:56.3|+38:47:01|0.03
Capella|05:16:41.4|+45:59:53|0.08
Rigel|05:14:32.3|-08:12:06|0.13
Procyon|07:39:18.1|+05:13:30|0.34
Achernar|01:37:42.8|-57:14:12|0.46
Betelgeuse|05:55:10.3|+07:24:25|0.50
Hadar|14:03:49.4|-60:22:23|0.61
Altair|19:50:47.0|+08:52:06|0.76
Acrux|12:26:35.9|-63:05:57|0.76
Aldebaran|04:35:55.2|+16:30:33|0.86
Antares|16:29:24.4|-26:25:55|0.96
Spica|13:25:11.6|-11:09:41|0.97
Pollux|07:45:18.9|+28:01:34|1.14
Fomalhaut|22:57:39.0|-29:37:20|1.16
Deneb|20:41:25.9|+45:16:49|1.25
Mimosa|12:47:43.3|-59:41:19|1.25
Regulus|10:08:22.3|+11:58:02|1.35
Adhara|06:58:37.5|-28:58:20|1.50
Castor|07:34:36.0|+31:53:18|1.58
Shaula|17:33:36.5|-37:06:14|1.62
Gacrux|12:31:09.9|-57:06:48|1.63
Bellatrix|05:25:07.9|+06:20:59|1.64
Elnath|05:26:17.5|+28:36:27|1.65
Miaplacidus|09:13:12.0|-69:43:02|1.68
Alnilam|05:36:12.8|-01:12:07|1.69
Alnair|22:08:14.0|-46:57:40|1.74
Alnitak|05:40:45.5|-01:56:34|1.77
Alioth|12:54:01.7|+55:57:35|1.77
Dubhe|11:03:43.7|+61:45:03|1.79
Mirfak|03:24:19.4|+49:51:40|1.79
Wezen|07:08:23.5|-26:23:36|1.84
Kaus Australis|18:24:10.3|-34:23:05|1.85
Avior|08:22:30.8|-59:30:34|1.86
Alkaid|13:47:32.4|+49:18:48|1.86
Sargas|17:37:19.1|-42:59:52|1.87
Menkalinan|05:59:31.7|+44:56:51|1.90
Atria|16:48:39.9|-69:01:40|1.91
Alhena|06:37:42.7|+16:23:57|1.92
Peacock|20:25:38.9|-56:44:06|1.94
Polaris|02:31:49.1|+89:15:51|1.98
Mirzam|06:22:42.0|-17:57:21|1.98
Alphard|09:27:35.2|-08:39:31|1.99
Hamal|02:07:10.4|+23:27:45|2.00
Diphda|00:43:35.4|-17:59:12|2.04
Nunki|18:55:15.9|-26:17:48|2.05
Menkent|14:06:41.0|-36:22:12|2.06
Alpheratz|00:08:23.3|+29:05:26|2.06
Mirach|01:09:43.9|+35:37:14|2.07
Rasalhague|17:34:56.1|+12:33:36|2.08
Kochab|14:50:42.3|+74:09:20|2.08
Saiph|05:47:45.4|-09:40:11|2.09
Algol|03:08:10.1|+40:57:20|2.10
Almach|02:03:54.0|+42:19:47|2.10
Denebola|11:49:03.6|+14:34:19|2.13
Mizar|13:23:55.5|+54:55:31|2.23
Mintaka|05:32:00.4|-00:17:57|2.23
Sadr|20:22:13.7|+40:15:24|2.23
Alphecca|15:34:41.3|+26:42:53|2.23
Schedar|00:40:30.4|+56:32:14|2.24
Eltanin|17:56:36.4|+51:29:20|2.24
Caph|00:09:10.7|+59:08:59|2.28
Merak|11:01:50.5|+56:22:57|2.37
Enif|21:44:11.2|+09:52:30|2.39
Scheat|23:03:46.5|+28:04:58|2.42
Markab|23:04:45.7|+15:12:19|2.49
Zubeneschamali|15:17:00.4|-09:22:59|2.61
Unukalhai|15:44:16.1|+06:25:32|2.63
"""

STARS = []
for line in _STARS.strip().splitlines():
    name, ra, dec, mag = line.split("|")
    STARS.append({"name": name, "ra_deg": parse_sexagesimal(ra) * 15.0,
                  "dec_deg": parse_sexagesimal(dec), "mag": float(mag)})


def visible_stars(lat: float | None, lon: float | None, min_alt: float = 0.0) -> list[dict]:
    """Estrellas con altura y masa de aire actuales, las más altas primero.
    Sin sitio conocido devuelve toda la lista ordenada por brillo."""
    if lat is None or lon is None:
        return [dict(s, alt_deg=None, airmass=None) for s in STARS]
    out = []
    for s in STARS:
        alt, az = altaz(s["ra_deg"], s["dec_deg"], lat, lon)
        if alt >= min_alt:
            am = airmass(alt)
            out.append(dict(s, alt_deg=round(alt, 1), az_deg=round(az, 1),
                            airmass=round(am, 2) if am else None))
    return sorted(out, key=lambda s: -s["alt_deg"])
