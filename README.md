# Recolector reproducible de operación TransMilenio — NQS (Bloques B y C)

Herramienta reproducible en Python (`python -m nqs`) para construir el catálogo geométrico del corredor NQS (**E: NQS Central**, **G: NQS Sur**, ramales **La Castellana** y **Calle 6**, y conexión **Bosa–Soacha hasta San Mateo**), capturar telemetría en vivo, archivar lluvia minutal del IDIGER, importar validaciones oficiales y exportar insumos tabulares para:

- **Bloque B**: intervalos observados (*headways*), salidas observadas por hora y tiempos de espera.
- **Bloque C**: tiempos de recorrido por tramo (tamaño de muestra, mediana y percentiles) y comparación seco vs. lluvia para asignación de flota y frontera de Pareto.

## Instalación y dependencias (Unix y Windows)

Requiere **Python 3.12+** (verificado en Python 3.14). El archivo `requirements-nqs.txt` incluye `tzdata` para que la zona horaria `America/Bogota` funcione también en Windows.

### macOS / Linux (bash / zsh)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-nqs.txt
```

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-nqs.txt
```

> Si PowerShell bloquea la activación del script, ejecuta una sola vez: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, o usa directamente `.venv\Scripts\python.exe -m nqs ...`.

### Windows (Símbolo del sistema / CMD)

```bat
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-nqs.txt
```

Con el entorno virtual activado, los comandos `python -m nqs ...` funcionan igual en macOS, Linux y Windows. Si prefieres no activar el entorno, sustituye `python` por `.venv/bin/python` (Unix) o `.venv\Scripts\python.exe` (Windows).

## Separación de datos crudos y derivados

- `datos_nqs/raw/insumos/`: descargas inmutables de GTFS, GeoJSON de trazados y planillas oficiales.
- `datos_nqs/raw/live/<sesion>/`: sesiones crudas de telemetría (`gps.csv`, `polls.csv`, `requests.csv`, `session.json`). Nunca almacena placas ni credenciales (`vehicle_id` se seudonimiza con SHA-256).
- `datos_nqs/raw/lluvia/`: serie acumulativa de lecturas minutales SAB/IDIGER (`sab_lluvia.csv`, `sab_lluvia_polls.csv`).
- `datos_nqs/derived/catalog/`: catálogo de patrones, estaciones, plataformas, controles y trazabilidad del Bloque A.
- `datos_nqs/derived/validaciones/`: validaciones oficiales agregadas por estación y franja de 15 min con procedencia y SHA-256.
- `datos_nqs/derived/metrics*/`: observaciones auditadas, cobertura, pasos por control, tiempos por tramo, métricas B/C, comparación por lluvia y `assumptions.json`.

## Comandos principales

> Los ejemplos se muestran en una sola línea para que puedan copiarse directamente en macOS, Linux, PowerShell y CMD con el entorno `.venv` activado.

### 1. Generar el catálogo de rutas NQS

Con las fuentes oficiales en línea (GTFS de referencia `GTFS_20260929.zip` y trazados oficiales E/G):

```bash
python -m nqs catalog --out datos_nqs/derived/catalog --tolerance-m 30 --min-overlap-m 300
```

Para usar un ZIP GTFS posterior o archivos locales (registrando la versión):

```bash
python -m nqs catalog --gtfs ruta/al/GTFS_YYYYMMDD.zip --corridor ruta/al/corredor_eg.geojson --version YYYYMMDD --out datos_nqs/derived/catalog
```

### 2. Comprobar fuentes disponibles

```bash
python -m nqs check --static
```

### 3. Captura en vivo: muestra puntual y prueba de 15 minutos

Muestra puntual (feed oficial con respaldo Croma si `CROMA_API_KEY` está configurada en el entorno):

```bash
python -m nqs capture --source auto --catalog datos_nqs/derived/catalog --duration 0 --max-requests 10
```

Para usar **Croma**, define `CROMA_API_KEY` en tu terminal antes de ejecutar la captura (nunca pegues la clave en archivos del repositorio ni en chats):

- **macOS / Linux (bash / zsh)**:
  ```bash
  export CROMA_API_KEY="tu_clave_local"
  ```
- **Windows (PowerShell)**:
  ```powershell
  $env:CROMA_API_KEY="tu_clave_local"
  ```
- **Windows (CMD)**:
  ```bat
  set CROMA_API_KEY=tu_clave_local
  ```

Luego ejecuta la prueba de **15 minutos** (`--duration 900`, intervalo `30 s`):

```bash
python -m nqs capture --source croma --catalog datos_nqs/derived/catalog --out datos_nqs/raw/live --route 4,B12,G12,E42,S42,M47,G47 --duration 900 --interval 30 --per-page 500 --max-pages 1 --max-requests 210
```

> **Presupuesto de Croma**: `--max-requests` acota todas las solicitudes HTTP de la sesión (incluidas páginas y fallos). Filtrar con `--route` o filtro geográfico (`--latitude`, `--longitude`, `--radius-m` entre 50 y 5000 m) evita paginar toda la ciudad en cada sondeo.

### 4. Captura independiente de lluvia minutal (SAB / IDIGER)

Permite archivar la serie antes de que el endpoint se sobrescriba:

```bash
python -m nqs rain --out datos_nqs/raw/lluvia/sab_lluvia.csv --duration 3600 --interval 60 --max-requests 60
```

### 5. Importar validaciones oficiales por estación y franja (07:00–08:00)

```bash
python -m nqs validations --input ruta/a/validaciones_mensuales.xlsx --catalog datos_nqs/derived/catalog --out datos_nqs/derived/validaciones/validaciones_nqs_0700_0800.csv --start 07:00 --end 08:00 --corridor-only --source "https://validaciones.datosabiertos.transmilenio.gov.co/validaciones_mensuales.html"
```

### 6. Exportar métricas para la franja 07:00–08:00 (`America/Bogota`)

Reemplaza `<ID_SESION>` por la carpeta impresa al finalizar `capture`:

```bash
python -m nqs metrics --catalog datos_nqs/derived/catalog --sessions datos_nqs/raw/live/<ID_SESION> --date 2026-11-24 --start 07:00 --end 08:00 --freshness-s 90 --rain datos_nqs/raw/lluvia/sab_lluvia.csv --validations datos_nqs/derived/validaciones/validaciones_nqs_0700_0800.csv --out datos_nqs/derived/metrics_0700_0800
```

### 7. Ejecutar pruebas automatizadas

```bash
python -m unittest discover -s tests -v
```

## Esquemas CSV documentados

| Archivo | Contenido principal |
| --- | --- |
| `derived/catalog/patterns.csv` | `pattern_id`, `route_id`, `route_code`, `destination`, `direction`, `system` (`troncal`/`zonal`/`alimentador`/`dual`), `operational`, `overlap_m`, `max_continuous_m`, `corridor_sections`. |
| `derived/catalog/pattern_controls.csv` | Puntos de control ordenados por `span` y `control_order`, `station_id`, `position_m`, `stop_status` (`parada`/`paso`), `platforms`, `pickup`, `dropoff`, `corridor_section`. |
| `derived/catalog/pattern_stations.csv` | Estado (`parada`, `paso`, `fuera`) por patrón y estación del corredor. |
| `derived/catalog/pattern_stops.csv` y `trips.csv` | Secuencia de paradas con resolución `platform_id -> station_id` y enlace exacto `trip_id -> pattern_id`. |
| `derived/catalog/block_a.csv` y `shape_audit.csv` | Trazabilidad de los cuatro servicios del Bloque A (`4`, `B12`/`G12`, `E42`/`S42`, `M47`/`G47`) y auditoría de formas excluidas (p. ej. rutas que solo cruzan la NQS). |
| `derived/metrics*/gps_observations.csv` | Observaciones con `captured_at_*` vs `gps_at_*`, `gps_age_s`, `source_live`, `source_live_stale_mismatch`, `quality_status`, `is_duplicate`, `pattern_match_status` (`exact_trip_id`, `ambiguo`, etc.) y `usable_for_metrics`. |
| `derived/metrics*/capture_coverage.csv` y `quality_log.csv` | Cobertura por ventana (`suficiente`, `cobertura_insuficiente`, `sin_telemetria`, `telemetry_available`) y registro consolidado de errores HTTP, atrasos, duplicados y ambigüedades. |
| `derived/metrics*/checkpoint_crossings.csv` | Pasos únicos por punto de control ordenados por sentido, método (`interpolado`/`proximidad`), brecha y régimen de lluvia. |
| `derived/metrics*/segment_travel_times.csv` | Tiempos de recorrido entre controles consecutivos con filtro de cobertura (`coverage_ok`, `max_internal_gap_s`, `exclusion_reason`). |
| `derived/metrics*/metrics_block_b_headways.csv` | **Insumo Bloque B**: `valid_trips`, `crossing_count`, `observed_departures_per_hour`, `headway_n`, `headway_mean_s`, `headway_median_s`, `headway_p10_s`, `headway_p90_s`, `headway_cv`. Celdas vacías cuando `telemetry_available=0` (nunca cero buses). |
| `derived/metrics*/metrics_block_c_segments.csv` | **Insumo Bloque C**: `distance_m`, `sample_size`, `sufficient_samples`, `mean_s`, `median_s`, `p10_s`, `p25_s`, `p75_s`, `p90_s`, `iqr_s`, `speed_median_kmh` por ruta, sentido, tramo, franja y régimen de lluvia. |
| `derived/metrics*/rain_comparison.csv` | Comparación `seco` vs `lluvia` en el mismo tramo, sentido y franja con `n_dry`, `n_rain`, medianas, p90 y variación porcentual. |
| `derived/metrics*/assumptions.json` | Procedencia completa y supuestos explícitos que separan oferta observada (GPS), entradas totales (validaciones) y supuestos externos para OD, asignación a servicios, demanda residual y tiempos completos de ciclo. |

## Supuestos explícitos

- **Validaciones vs. GPS**: las validaciones oficiales miden entradas totales por torniquete (sin destino ni ruta abordada); el GPS mide oferta vehicular observada (sin ocupación ni demanda rechazada).
- **Ausencia de telemetría**: cualquier ventana sin muestras frescas se reporta como dato faltante (`""`, `telemetry_available=0`), nunca como cero buses.
- **Puente hacia los modelos**: estimar matriz OD, asignación entre servicios $S_k$, demanda residual y tiempos exteriores/regulación del ciclo completo requiere supuestos o insumos complementarios documentados en `assumptions.json`.
- **Estación SENA (`7009`)**: conservada en inventario y controles como `paso` mientras permanezca cerrada por obras.
