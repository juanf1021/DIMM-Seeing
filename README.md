# DIMM Seeing Monitor

Mide el **seeing** atmosférico con el Meade LX200 GPS 10" + máscara de 2 agujeros + una cámara USB
en el ocular, usando la técnica DIMM (la misma del C-DIMM de Carnegie en Las Campanas).

- Funciona con el telescopio **apuntado a mano** y **sin seguimiento**: no hace falta cable al LX200.
- Corre en Windows como `DIMM-Seeing.exe`. La interfaz se abre sola en el navegador.
- Guarda un CSV por noche con seeing, r₀, masa de aire y la estrella medida.

---

## 1. Cómo funciona

1. La **máscara de 2 agujeros** hace que la estrella forme **2 imágenes** en la cámara
   (separadas por un prisma en un agujero, o desenfocando un poco).
2. La turbulencia hace que esas 2 imágenes **tiemblen una respecto a la otra**. La app toma
   cientos de fotos muy cortas y mide la varianza de esa separación.
3. Con la fórmula de Tokovinin (2002) esa varianza da **r₀** y el **seeing** (FWHM en arcsec a 500 nm).
   Con la altura de la estrella se corrige al **cenit**.

Como se mide la *diferencia* entre las 2 imágenes, que el telescopio no siga la estrella, las
vibraciones y el viento sobre el tubo casi no afectan.

## 2. Qué se necesita

| Qué | Detalle |
|---|---|
| Máscara | Ya la tienen. **Medir** diámetro de cada agujero (D) y distancia entre centros (B) y ponerlos en Ajustes. |
| Cámara | La del ocular, por USB. Tipo webcam (UVC): no necesita drivers. Si es de marca astronómica (ZWO, QHY, SVBONY con driver…), también sirve por ASCOM. |
| PC | Windows 10/11. |
| Opcional | Cable serial al LX200 (solo lee hora y sitio del GPS). ASTAP para plate solving. |

## 3. Uso en la noche (paso a paso)

**La primera vez:**
1. Abrir `DIMM-Seeing.exe`. Si Windows muestra "Windows protegió su PC": *Más información → Ejecutar de todas formas*.
   Deje la ventana negra abierta: es la app. El navegador se abre solo (si no, entre a `http://localhost:8000`).
2. **Ajustes → Máscara**: D y B reales en metros (ej. 60 mm → `0.06`).
3. **Ajustes → Sitio**: latitud y longitud del lugar (Norte y Este positivos; ej. Bogotá `4.60`, `-74.08`).
4. Guardar ajustes.

**Cada noche:**
1. **① Cámara** → *Cámara USB* → elegir la del telescopio en la lista (no la webcam del portátil) → *Conectar*.
2. Apuntar el telescopio a una estrella brillante (mag < 2) y alta (> 45°).
3. **② Estrella apuntada** → elegir esa estrella en la lista (está ordenada por altura) → *Estoy apuntando a esta estrella*.
4. Ajustar **Exposición** y **Ganancia** hasta que el visor diga "✓ 2 imágenes detectadas" sin saturar.
   Use la exposición **más corta posible** (ideal ≤ 10 ms).
5. **③ Escala** (la primera vez, o si cambia cámara/ocular/resolución): poner la estrella en el
   borde por donde **entra** al campo → *Calibrar con la deriva* → no tocar el telescopio 15 s.
   La app mide la escala real "/px con el movimiento de la estrella.
6. *Medición continua*. Cuando la estrella salga del campo, la app termina la medición con los
   frames que tenga (o la descarta si son muy pocos) y **espera** a que la vuelva a apuntar.

## 4. Mensajes frecuentes

| Mensaje | Qué hacer |
|---|---|
| "no se detecta estrella" | Apuntar mejor, subir exposición/ganancia. |
| "solo se ve 1 imagen" | Las 2 imágenes están juntas: desenfocar un poco o revisar la máscara. |
| "saturada" | Bajar exposición o ganancia. |
| "brillos muy distintos" | Hay otra estrella en el campo, o un agujero tapado. |
| "estrella en el borde" | Re-centrar. |
| "Seeing longitudinal y transversal muy distintos" | Vibración, viento, o el valor de B está mal. |
| "No se pudo escribir el CSV" | El archivo está abierto en Excel: la app sigue y guarda en otro archivo. |

## 5. Resultados

- Gráfica de la noche en pantalla + botón **Descargar CSV**.
- Archivo por noche en `%APPDATA%\DIMM-Seeing\seeing_AAAAMMDD.csv` (la ruta exacta se muestra bajo la gráfica).
- Columnas: hora UTC, seeing (línea de vista y cenit), r₀, σ longitudinal/transversal, frames,
  masa de aire, altura, RA/Dec, estrella, exposición, escala "/px, cámara.

## 6. Límites conocidos

- **Exposición:** con más de ~10 ms el seeing sale subestimado (la turbulencia se promedia).
  Muchas webcams no bajan de 30 ms: compare noches con la misma exposición.
- **Plate solving:** con 2540 mm de focal el campo es de pocos minutos de arco y casi nunca
  resuelve. Por eso las coordenadas salen del catálogo de estrellas.
- No se corrige aún el sesgo por ruido de centroide ni se extrapola a exposición cero.

## 7. Desarrollo

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx   # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q                           # tests: el cálculo se valida con el simulador
.venv/bin/python run.py                                 # abre http://localhost:8000
python run.py --host 0.0.0.0                            # verlo desde el celular en la misma red
```

### Generar el `.exe`

- **En Windows:** doble clic en `build_windows.bat` → `dist\DIMM-Seeing.exe`.
- **Desde GitHub:** el workflow *Build Windows exe* corre los tests en Windows y compila el `.exe`
  (Actions → Build Windows exe → Run workflow → descargar el artefacto).

### Estructura

| Archivo | Qué hace |
|---|---|
| `dimm_app/analysis.py` | Detección de las 2 imágenes, fórmula DIMM, calibración por deriva |
| `dimm_app/cameras.py` | Cámara USB (OpenCV), ASCOM, ZWO, simulador |
| `dimm_app/engine.py` | Bucle de captura, mediciones, calibración, CSV |
| `dimm_app/stars.py` | Catálogo de estrellas brillantes |
| `dimm_app/telescope.py` | Protocolo LX200 por serial (opcional) |
| `dimm_app/platesolve.py` | Llamada a ASTAP (opcional) |
| `dimm_app/server.py` | API web |
| `dimm_app/static/` | Interfaz |

## Referencias

- Tokovinin, A. (2002). *From Differential Image Motion to Seeing*. PASP 114, 1156.
- Sarazin, M. & Roddier, F. (1990). *The ESO differential image motion monitor*. A&A 227, 294.
- Carnegie C-DIMM: https://instrumentation.obs.carnegiescience.edu/Software/CDIMM/
