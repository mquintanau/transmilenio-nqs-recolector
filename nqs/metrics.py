"""Procesamiento de calidad, pasos por control y métricas para los Bloques B y C."""
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time as clock, timedelta
from pathlib import Path

import numpy as np
import shapely as geo
from shapely.geometry import Point, shape

from .catalog import TO_METRES
from .common import BOGOTA, coordinates, local_iso, number, parse_bool, read_csv, timestamp, write_csv, write_json
from .live import quality

OBSERVATION_FIELDS = (
    "session_id sample_id source captured_at_utc captured_at_bogota gps_at_utc gps_at_bogota "
    "date_bogota time_window gps_age_s source_live source_live_stale_mismatch quality_status "
    "is_duplicate vehicle_id route_id route_code trip_id pattern_match_status pattern_id "
    "candidate_pattern_ids direction system operational longitude latitude dist_to_pattern_m "
    "position_m stop_id stop_status usable_for_metrics"
)
QUALITY_LOG_FIELDS = "event_type severity session_id sample_id source date_bogota time_window detail count"
COVERAGE_FIELDS = (
    "date_bogota time_window window_from_bogota window_to_bogota source sessions expected_polls "
    "attempted_polls ok_polls fresh_polls error_polls stale_only_polls empty_polls received_rows "
    "fresh_rows covered_seconds session_overlap_seconds window_seconds poll_coverage_ratio "
    "window_coverage_ratio telemetry_available coverage_status"
)
CROSSING_FIELDS = (
    "crossing_id session_id source date_bogota time_window route_id route_code direction pattern_id "
    "span control_order station_id station_name stop_status corridor_section vehicle_id trip_id "
    "run_id crossed_at_utc crossed_at_bogota detection_method bracketing_gap_s rain_regime "
    "rain_mm_nearest nearest_rain_sensor_m"
)
SEGMENT_FIELDS = (
    "session_id source date_bogota time_window route_id route_code direction pattern_id span "
    "from_station_id from_station_name to_station_id to_station_name from_stop_status "
    "to_stop_status corridor_section vehicle_id trip_id run_id entered_at_utc exited_at_utc "
    "entered_at_bogota distance_m travel_time_s speed_kmh gps_points_in_segment "
    "max_internal_gap_s coverage_ok exclusion_reason rain_regime rain_mm_nearest "
    "nearest_rain_sensor_m"
)
BLOCK_B_FIELDS = (
    "aggregation_level date_bogota time_window route_code route_id direction pattern_id "
    "station_id station_name stop_status corridor_section telemetry_available coverage_status "
    "poll_coverage_ratio observed_hours valid_trips crossing_count observed_departures_per_hour "
    "headway_n headway_mean_s headway_median_s headway_p10_s headway_p90_s headway_cv reliable"
)
BLOCK_C_FIELDS = (
    "aggregation_level date_bogota time_window route_code route_id direction pattern_id "
    "from_station_id from_station_name to_station_id to_station_name corridor_section rain_regime "
    "telemetry_available coverage_status distance_m sample_size excluded_low_coverage_n "
    "sufficient_samples mean_s median_s p10_s p25_s p75_s p90_s iqr_s speed_median_kmh"
)
RAIN_COMPARISON_FIELDS = (
    "route_code direction from_station_id from_station_name to_station_id to_station_name "
    "corridor_section time_window n_dry median_dry_s p90_dry_s n_rain median_rain_s p90_rain_s "
    "delta_median_s pct_increase_median comparable_sufficient"
)

EXPLICIT_ASSUMPTIONS = {
    "observabilidad_fuentes": {
        "validaciones_troncales": (
            "Las validaciones oficiales miden entradas totales por torniquete en estación y franja; "
            "no registran estación de salida, trasbordos internos ni el servicio abordado."
        ),
        "telemetria_gps": (
            "Los feeds GTFS Realtime y Croma miden oferta observada (posición de buses, pasos por "
            "puntos de control, intervalos y tiempos de recorrido por tramo); no miden ocupación "
            "ni demanda rechazada o sin cupo."
        ),
        "ausencia_telemetria": (
            "Las ventanas sin muestras frescas o con fallos del feed se reportan como dato faltante "
            "(celda vacía y telemetry_available=0), nunca como cero buses."
        ),
    },
    "supuestos_requeridos_para_bloques_a_b_c": {
        "matriz_origen_destino": (
            "Para inferir destino por viaje a partir de validaciones se requiere una matriz OD externa, "
            "encuesta de movilidad, encadenamiento de viajes de tarjeta inteligente o participaciones "
            "de planeación como las del Bloque A."
        ),
        "asignacion_a_servicios": (
            "Asignar demanda OD entre servicios compatibles S_k (que paran en origen y destino) exige "
            "una regla explícita de elección de ruta (p. ej. reparto proporcional a frecuencias observadas "
            "o estrategias óptimas tipo Spiess-Florian)."
        ),
        "demanda_residual": (
            "La demanda que requiere buses de refuerzo se obtiene restando a la demanda total la "
            "capacidad efectiva de la oferta regular observada (frecuencia regular * cupo operativo "
            "supuesto * factor de ocupación disponible)."
        ),
        "tiempos_completos_de_ciclo": (
            "El recolector mide tiempos de circulación dentro de los tramos oficiales E/G (incluida "
            "la conexión Bosa-Soacha hasta San Mateo y los ramales La Castellana y Calle 6). Los tiempos "
            "exteriores fuera de E/G y los tiempos de regulación en cabecera requieren seguimiento GPS "
            "fuera del corredor o supuestos de programación como en el Bloque A."
        ),
        "capacidad_vehicular": (
            "El cupo por vehículo (p. ej. 60 pax por salida adicional en el Bloque A o capacidad nominal "
            "de articulado/biarticulado) es un parámetro exógeno no medido por GPS."
        ),
        "estacion_sena": (
            "La estación SENA (7009) se conserva en el inventario y puntos de control como punto de paso "
            "(stop_status=paso) mientras permanezca fuera de servicio por obras."
        ),
    },
}


def fmt(value, digits=3):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    if isinstance(value, float):
        return round(value, digits)
    return value


def window_bounds(dt_utc, window_minutes, window_start=None, window_end=None):
    """Devuelve (date_bogota_str, label, start_bogota, end_bogota) para un instante UTC."""
    local_dt = dt_utc.astimezone(BOGOTA)
    day = local_dt.date()
    if window_start is not None and window_end is not None:
        ws = datetime.combine(day, window_start, BOGOTA)
        we = datetime.combine(day, window_end, BOGOTA)
        if ws <= local_dt < we:
            label = f"{window_start.strftime('%H:%M')}-{window_end.strftime('%H:%M')}"
            return day.isoformat(), label, ws, we
    minutes = local_dt.hour * 60 + local_dt.minute
    bucket_start = (minutes // window_minutes) * window_minutes
    start_dt = datetime.combine(day, clock(0, 0), BOGOTA) + timedelta(minutes=bucket_start)
    end_dt = start_dt + timedelta(minutes=window_minutes)
    label = f"{start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"
    return day.isoformat(), label, start_dt, end_dt


def matches_filter(dt_utc, filter_dates, window_start, window_end):
    local_dt = dt_utc.astimezone(BOGOTA)
    if filter_dates and local_dt.date() not in filter_dates:
        return False
    if window_start is not None and window_end is not None:
        t = local_dt.time()
        if not (window_start <= t < window_end):
            return False
    return True


def load_catalog(catalog_dir):
    catalog_dir = Path(catalog_dir)
    meta = json.loads((catalog_dir / "catalog.json").read_text(encoding="utf-8"))
    patterns = {}
    by_route_id = defaultdict(list)
    by_route_code = defaultdict(list)
    for row in read_csv(catalog_dir / "patterns.csv"):
        pid = row["pattern_id"]
        row["operational_bool"] = row.get("operational") in ("1", 1, True, "True", "true")
        patterns[pid] = row
        by_route_id[row["route_id"]].append(pid)
        if row.get("route_code"):
            by_route_code[row["route_code"]].append(pid)
    trips = {}
    for row in read_csv(catalog_dir / "trips.csv"):
        trips[row["trip_id"]] = (row["pattern_id"], row["route_id"])
    controls = defaultdict(list)
    for row in read_csv(catalog_dir / "pattern_controls.csv"):
        controls[row["pattern_id"]].append(
            dict(
                span=int(row["span"]),
                control_order=int(row["control_order"]),
                station_id=row["station_id"],
                station_name=row["station_name"],
                position_m=float(row["position_m"]),
                x=float(row["x"]),
                y=float(row["y"]),
                stop_status=row["stop_status"],
                corridor_section=row["corridor_section"],
            )
        )
    for pid in controls:
        controls[pid].sort(key=lambda c: (c["span"], c["control_order"]))
    geometries = {}
    geojson = json.loads((catalog_dir / "patterns.geojson").read_text(encoding="utf-8"))
    for feature in geojson["features"]:
        pid = feature["properties"]["pattern_id"]
        line_ll = shape(feature["geometry"])
        geometries[pid] = geo.transform(line_ll, TO_METRES.transform, interleaved=False)
    return dict(
        meta=meta,
        patterns=patterns,
        by_route_id=by_route_id,
        by_route_code=by_route_code,
        trips=trips,
        controls=controls,
        geometries=geometries,
    )


def discover_sessions(session_paths):
    dirs = []
    for raw_path in session_paths:
        path = Path(raw_path)
        if (path / "gps.csv").exists() or (path / "polls.csv").exists() or (path / "session.json").exists():
            dirs.append(path)
        elif path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_dir() and ((child / "gps.csv").exists() or (child / "polls.csv").exists()):
                    dirs.append(child)
    return dirs


class RainIndex:
    """Índice espacio-temporal simple de lecturas pluviométricas SAB/IDIGER."""

    def __init__(self, rain_path, radius_m=5000.0, fallback_radius_m=10000.0, window_min=15.0, threshold_mm=0.1):
        self.radius_m = radius_m
        self.fallback_radius_m = fallback_radius_m
        self.window_s = window_min * 60.0
        self.threshold_mm = threshold_mm
        self.sensors = {}
        self.readings = defaultdict(list)
        self.loaded = False
        if rain_path and Path(rain_path).exists():
            for row in read_csv(Path(rain_path)):
                measured = timestamp(row.get("measured_at"))
                xy = coordinates(row.get("longitude"), row.get("latitude"))
                mm = number(row.get("rain_mm"))
                sid = str(row.get("sensor_id") or "").strip()
                if not measured or not xy or mm is None or not sid:
                    continue
                if sid not in self.sensors:
                    x, y = TO_METRES.transform(xy[0], xy[1])
                    self.sensors[sid] = (x, y)
                self.readings[sid].append((measured.timestamp(), mm))
            for sid in self.readings:
                self.readings[sid].sort()
            self.loaded = bool(self.sensors)

    def classify(self, x, y, dt_utc):
        if not self.loaded or dt_utc is None:
            return "sin_datos_lluvia", None, None
        target_ts = dt_utc.timestamp()
        nearby = []
        nearest_dist = None
        for sid, (sx, sy) in self.sensors.items():
            dist = math.hypot(x - sx, y - sy)
            if dist > self.fallback_radius_m:
                continue
            # Busca la lectura más reciente dentro de [target - window_s, target + 300s].
            best_mm = None
            best_dt = None
            for ts, mm in self.readings[sid]:
                diff = target_ts - ts
                if -300.0 <= diff <= self.window_s:
                    if best_dt is None or abs(diff) < best_dt:
                        best_dt = abs(diff)
                        best_mm = mm
            if best_mm is not None:
                nearby.append((dist, best_mm))
        if not nearby:
            return "sin_datos_lluvia", None, None
        nearby.sort()
        in_radius = [item for item in nearby if item[0] <= self.radius_m]
        chosen = in_radius if in_radius else [nearby[0]]
        nearest_dist = chosen[0][0]
        max_mm = max(mm for _, mm in chosen)
        regime = "lluvia" if max_mm >= self.threshold_mm else "seco"
        return regime, round(max_mm, 3), round(nearest_dist, 1)


def match_pattern(row, catalog, include_non_operational=False):
    trip_id = (row.get("trip_id") or "").strip()
    route_id = (row.get("route_id") or "").strip()
    route_code = (row.get("route_code") or "").strip()
    if trip_id and trip_id in catalog["trips"]:
        pid, cat_route_id = catalog["trips"][trip_id]
        if route_id and route_id != cat_route_id:
            return "ambiguo", "", f"{pid}"
        return "exact_trip_id", pid, ""
    candidates = []
    if route_id and route_id in catalog["by_route_id"]:
        candidates = list(catalog["by_route_id"][route_id])
    elif route_code and route_code in catalog["by_route_code"]:
        candidates = list(catalog["by_route_code"][route_code])
    if not include_non_operational:
        op_candidates = [pid for pid in candidates if catalog["patterns"][pid]["operational_bool"]]
        if op_candidates:
            candidates = op_candidates
    if len(candidates) == 1:
        return ("sin_trip_id_unico" if not trip_id else "trip_no_en_catalogo_unico"), candidates[0], ""
    if len(candidates) > 1:
        return "ambiguo", "", "|".join(sorted(candidates))
    return "fuera_de_catalogo", "", ""


def merge_intervals(intervals):
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def interval_overlap_seconds(a_start, a_end, b_start, b_end):
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0.0, (end - start).total_seconds())


def compute_coverage(session_dirs, window_minutes, filter_dates, window_start, window_end, min_coverage):
    """Calcula cobertura por ventana temporal y fuente sin confundir fallos con cero buses."""
    buckets = defaultdict(lambda: dict(
        sessions=set(),
        expected_polls=0.0,
        attempted_polls=0,
        ok_polls=0,
        fresh_polls=0,
        error_polls=0,
        stale_only_polls=0,
        empty_polls=0,
        received_rows=0,
        fresh_rows=0,
        covered_intervals=[],
        overlap_intervals=[],
        window_from=None,
        window_to=None,
    ))
    quality_events = []

    for sdir in session_dirs:
        session_id = sdir.name
        manifest = {}
        if (sdir / "session.json").exists():
            manifest = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
        interval_s = float(manifest.get("interval_s") or 30.0)
        s_start = timestamp(manifest.get("started_at"))
        s_end = timestamp(manifest.get("ended_at"))
        if s_start and (not s_end or s_end < s_start):
            s_end = s_start + timedelta(seconds=float(manifest.get("duration_s") or 0.0))

        # Registra errores de solicitudes HTTP / red / presupuesto en quality_log.
        req_path = sdir / "requests.csv"
        if req_path.exists():
            req_err_counts = Counter()
            for r in read_csv(req_path):
                outcome = (r.get("outcome") or "").strip()
                if outcome and outcome != "ok":
                    req_dt = timestamp(r.get("requested_at"))
                    if req_dt and not matches_filter(req_dt, filter_dates, window_start, window_end):
                        continue
                    d_str, w_lbl = (window_bounds(req_dt, window_minutes, window_start, window_end)[:2]
                                    if req_dt else ("", ""))
                    req_err_counts[(r.get("source", ""), d_str, w_lbl, f"{r.get('endpoint', '')}:{outcome}")] += 1
            for (src, d_str, w_lbl, detail), cnt in sorted(req_err_counts.items()):
                quality_events.append(dict(
                    event_type="error_solicitud", severity="error", session_id=session_id, sample_id="",
                    source=src, date_bogota=d_str, time_window=w_lbl, detail=detail, count=cnt,
                ))

        polls_path = sdir / "polls.csv"
        if not polls_path.exists():
            continue
        session_windows = set()
        for poll in read_csv(polls_path):
            slot_from = timestamp(poll.get("slot_from")) or timestamp(poll.get("captured_at"))
            if not slot_from:
                continue
            if not matches_filter(slot_from, filter_dates, window_start, window_end):
                continue
            slot_to = timestamp(poll.get("slot_to"))
            if not slot_to or slot_to <= slot_from:
                slot_to = slot_from + timedelta(seconds=interval_s)
            d_str, w_lbl, w_from, w_to = window_bounds(slot_from, window_minutes, window_start, window_end)
            src = (poll.get("source") or "desconocido").strip()
            status = (poll.get("status") or "").strip()
            received = int(number(poll.get("received")) or 0)
            fresh = int(number(poll.get("fresh")) or 0)
            slot_clip_a = max(slot_from, w_from.astimezone(slot_from.tzinfo))
            slot_clip_b = min(slot_to, w_to.astimezone(slot_from.tzinfo))
            session_windows.add((d_str, w_lbl, w_from, w_to, src))
            session_windows.add((d_str, w_lbl, w_from, w_to, "combinado"))

            for bucket_src in (src, "combinado"):
                b = buckets[(d_str, w_lbl, bucket_src)]
                b["sessions"].add(session_id)
                b["window_from"] = w_from
                b["window_to"] = w_to
                b["attempted_polls"] += 1
                b["received_rows"] += received
                b["fresh_rows"] += fresh
                if status == "ok":
                    b["ok_polls"] += 1
                    if fresh > 0:
                        b["fresh_polls"] += 1
                        if slot_clip_b > slot_clip_a:
                            b["covered_intervals"].append((slot_clip_a.timestamp(), slot_clip_b.timestamp()))
                elif status == "sin_gps_fresco":
                    b["stale_only_polls"] += 1
                elif status == "vacio":
                    b["empty_polls"] += 1
                else:
                    b["error_polls"] += 1

        # Calcula polls esperados y solapamiento de sesión por ventana tocada.
        for d_str, w_lbl, w_from, w_to, bucket_src in session_windows:
            b = buckets[(d_str, w_lbl, bucket_src)]
            if s_start and s_end:
                w_from_utc = w_from.astimezone(s_start.tzinfo)
                w_to_utc = w_to.astimezone(s_start.tzinfo)
                if s_end == s_start:
                    # Captura puntual de una sola muestra.
                    overlap_s = min(interval_s, (w_to_utc - w_from_utc).total_seconds())
                    b["expected_polls"] += 1.0
                    b["overlap_intervals"].append((s_start.timestamp(), s_start.timestamp() + overlap_s))
                else:
                    ov = interval_overlap_seconds(s_start, s_end, w_from_utc, w_to_utc)
                    if ov > 0:
                        b["expected_polls"] += max(1.0, math.ceil(ov / max(interval_s, 1.0)))
                        b["overlap_intervals"].append(
                            (max(s_start, w_from_utc).timestamp(), min(s_end, w_to_utc).timestamp())
                        )
            else:
                b["expected_polls"] += max(1.0, float(b["attempted_polls"]))

    # Si el usuario pidió explícitamente fecha + franja y no hubo ningún poll, crea fila sin_telemetria.
    if filter_dates and window_start is not None and window_end is not None:
        w_lbl = f"{window_start.strftime('%H:%M')}-{window_end.strftime('%H:%M')}"
        for day in sorted(filter_dates):
            d_str = day.isoformat()
            if (d_str, w_lbl, "combinado") not in buckets:
                w_from = datetime.combine(day, window_start, BOGOTA)
                w_to = datetime.combine(day, window_end, BOGOTA)
                b = buckets[(d_str, w_lbl, "combinado")]
                b["window_from"] = w_from
                b["window_to"] = w_to

    coverage_rows = []
    combined_lookup = {}
    for (d_str, w_lbl, src), b in sorted(buckets.items()):
        w_from, w_to = b["window_from"], b["window_to"]
        window_s = (w_to - w_from).total_seconds() if (w_from and w_to) else window_minutes * 60.0
        merged_cov = merge_intervals(b["covered_intervals"])
        covered_s = sum(end - start for start, end in merged_cov)
        merged_ov = merge_intervals(b["overlap_intervals"])
        overlap_s = sum(end - start for start, end in merged_ov)
        expected = max(1, int(round(b["expected_polls"]))) if b["expected_polls"] > 0 else 0
        # Para combinado, las ranuras válidas se aproximan por tiempo cubierto / intervalo o fresh_polls acotado.
        if overlap_s > 0:
            poll_ratio = min(1.0, covered_s / overlap_s)
        elif expected > 0:
            poll_ratio = min(1.0, b["fresh_polls"] / expected)
        else:
            poll_ratio = 0.0
        win_ratio = min(1.0, covered_s / window_s) if window_s > 0 else 0.0
        telemetry_ok = int(b["fresh_polls"] > 0 and covered_s > 0)
        if not telemetry_ok:
            status = "sin_telemetria"
        elif poll_ratio < min_coverage or win_ratio < min_coverage:
            status = "cobertura_insuficiente"
        else:
            status = "suficiente"

        row = dict(
            date_bogota=d_str,
            time_window=w_lbl,
            window_from_bogota=w_from.isoformat() if w_from else "",
            window_to_bogota=w_to.isoformat() if w_to else "",
            source=src,
            sessions=len(b["sessions"]),
            expected_polls=expected,
            attempted_polls=b["attempted_polls"],
            ok_polls=b["ok_polls"],
            fresh_polls=b["fresh_polls"],
            error_polls=b["error_polls"],
            stale_only_polls=b["stale_only_polls"],
            empty_polls=b["empty_polls"],
            received_rows=b["received_rows"],
            fresh_rows=b["fresh_rows"],
            covered_seconds=round(covered_s, 1),
            session_overlap_seconds=round(overlap_s, 1),
            window_seconds=round(window_s, 1),
            poll_coverage_ratio=round(poll_ratio, 4),
            window_coverage_ratio=round(win_ratio, 4),
            telemetry_available=telemetry_ok,
            coverage_status=status,
        )
        coverage_rows.append(row)
        if src == "combinado":
            combined_lookup[(d_str, w_lbl)] = (row, merged_cov)
            if status != "suficiente":
                quality_events.append(dict(
                    event_type="ventana_" + status,
                    severity="warning" if status == "cobertura_insuficiente" else "error",
                    session_id="|".join(sorted(b["sessions"])),
                    sample_id="",
                    source="combinado",
                    date_bogota=d_str,
                    time_window=w_lbl,
                    detail=f"poll_coverage={poll_ratio:.2f},window_coverage={win_ratio:.2f},errors={b['error_polls']}",
                    count=1,
                ))
    return coverage_rows, combined_lookup, quality_events


def detect_crossings_and_segments(
    usable_obs,
    catalog,
    rain_index,
    max_gap_s=180.0,
    checkpoint_radius_m=45.0,
    run_split_gap_s=900.0,
    min_speed_kmh=1.0,
    max_speed_kmh=90.0,
    window_minutes=60,
    window_start=None,
    window_end=None,
):
    """Detecta pasos ordenados (una sola vez por evento) y estima tiempos entre controles."""
    grouped = defaultdict(list)
    for obs in usable_obs:
        trip_key = (obs["trip_id"], obs.get("trip_start_date", ""), obs.get("trip_start_time", ""))
        key = (obs["source"], obs["vehicle_id"], obs["pattern_id"], trip_key)
        grouped[key].append(obs)

    crossings = []
    segments = []
    crossing_seq = 0

    for (source, vehicle_id, pattern_id, _), pts in sorted(grouped.items()):
        pts.sort(key=lambda o: (o["gps_dt"], o["captured_dt"]))
        pat = catalog["patterns"][pattern_id]
        pat_controls = catalog["controls"].get(pattern_id, [])
        if not pat_controls:
            continue

        # Divide en viajes/corridas si hay pausa larga o retroceso fuerte de ciclo nuevo.
        runs = []
        current = [pts[0]]
        for p in pts[1:]:
            dt_s = (p["gps_dt"] - current[-1]["gps_dt"]).total_seconds()
            dp_m = p["position_m"] - current[-1]["position_m"]
            if dt_s > run_split_gap_s or dp_m < -500.0:
                runs.append(current)
                current = [p]
            else:
                current.append(p)
        runs.append(current)

        by_span = defaultdict(list)
        for ctrl in pat_controls:
            by_span[ctrl["span"]].append(ctrl)

        for run_idx, run_pts in enumerate(runs, 1):
            run_id = f"{vehicle_id[:10]}:{pattern_id[:8]}:{run_idx}"
            for span, span_controls in sorted(by_span.items()):
                span_crossings = []
                last_time = None
                for ctrl in span_controls:
                    target_pos = ctrl["position_m"]
                    detected = None

                    # 1) Intenta interpolación lineal entre dos observaciones consecutivas que cruzan el control.
                    for p1, p2 in zip(run_pts, run_pts[1:]):
                        dt_s = (p2["gps_dt"] - p1["gps_dt"]).total_seconds()
                        if dt_s <= 0 or dt_s > max_gap_s:
                            continue
                        s1, s2 = p1["position_m"], p2["position_m"]
                        if s1 <= target_pos <= s2 and s2 > s1:
                            frac = (target_pos - s1) / (s2 - s1)
                            cross_dt = p1["gps_dt"] + timedelta(seconds=frac * dt_s)
                            if last_time is not None and cross_dt < last_time:
                                continue
                            detected = (cross_dt, "interpolado", dt_s, p2["session_id"], p2["trip_id"])
                            break

                    # 2) Si no hubo cruce interpolado, usa proximidad directa (p. ej. bus detenido o inicio/fin).
                    if detected is None:
                        near = []
                        for idx, p in enumerate(run_pts):
                            if last_time is not None and p["gps_dt"] < last_time:
                                continue
                            dist_along = abs(p["position_m"] - target_pos)
                            if dist_along <= checkpoint_radius_m:
                                # Estima brecha local con observación vecina más cercana.
                                neighbor_gaps = []
                                if idx > 0:
                                    neighbor_gaps.append((p["gps_dt"] - run_pts[idx - 1]["gps_dt"]).total_seconds())
                                if idx + 1 < len(run_pts):
                                    neighbor_gaps.append((run_pts[idx + 1]["gps_dt"] - p["gps_dt"]).total_seconds())
                                local_gap = min(neighbor_gaps) if neighbor_gaps else None
                                near.append((dist_along, p["gps_dt"], local_gap, p["session_id"], p["trip_id"]))
                        if near:
                            # Toma el punto más cercano al control (en empate, el primero): un único evento.
                            _, cross_dt, local_gap, sess_id, trip_id = min(near, key=lambda x: (x[0], x[1]))
                            detected = (cross_dt, "proximidad", local_gap, sess_id, trip_id)

                    if detected is None:
                        continue
                    cross_dt, method, gap_s, sess_id, trip_id = detected
                    last_time = cross_dt
                    crossing_seq += 1
                    d_str, w_lbl, _, _ = window_bounds(cross_dt, window_minutes, window_start, window_end)
                    rain_regime, rain_mm, rain_dist = rain_index.classify(ctrl["x"], ctrl["y"], cross_dt)
                    crossing_row = dict(
                        crossing_id=f"X{crossing_seq:07d}",
                        session_id=sess_id,
                        source=source,
                        date_bogota=d_str,
                        time_window=w_lbl,
                        route_id=pat["route_id"],
                        route_code=pat["route_code"],
                        direction=pat["direction"],
                        pattern_id=pattern_id,
                        span=span,
                        control_order=ctrl["control_order"],
                        station_id=ctrl["station_id"],
                        station_name=ctrl["station_name"],
                        stop_status=ctrl["stop_status"],
                        corridor_section=ctrl["corridor_section"],
                        vehicle_id=vehicle_id,
                        trip_id=trip_id,
                        run_id=run_id,
                        crossed_at_utc=cross_dt.isoformat(),
                        crossed_at_bogota=local_iso(cross_dt),
                        detection_method=method,
                        bracketing_gap_s=fmt(gap_s, 1),
                        rain_regime=rain_regime,
                        rain_mm_nearest=fmt(rain_mm, 3),
                        nearest_rain_sensor_m=fmt(rain_dist, 1),
                        _dt=cross_dt,
                        _ctrl=ctrl,
                        _gap_s=gap_s,
                    )
                    crossings.append(crossing_row)
                    span_crossings.append(crossing_row)

                # Tiempos entre puntos de control consecutivos del mismo span.
                for c1, c2 in zip(span_crossings, span_crossings[1:]):
                    # Solo tramos consecutivos en orden de control (evita mezclar saltos sin intermedios).
                    if c2["control_order"] != c1["control_order"] + 1:
                        continue
                    t1, t2 = c1["_dt"], c2["_dt"]
                    travel_s = (t2 - t1).total_seconds()
                    dist_m = c2["_ctrl"]["position_m"] - c1["_ctrl"]["position_m"]
                    if dist_m <= 0:
                        continue
                    # Evalúa cobertura de la trayectoria dentro del tramo [t1, t2].
                    seg_pts = [p for p in run_pts if t1 <= p["gps_dt"] <= t2]
                    # Incluye el punto inmediatamente anterior a t1 y posterior a t2 para medir la brecha real.
                    before = [p for p in run_pts if p["gps_dt"] < t1]
                    after = [p for p in run_pts if p["gps_dt"] > t2]
                    chain = ([before[-1]] if before else []) + seg_pts + ([after[0]] if after else [])
                    internal_gaps = [
                        (b["gps_dt"] - a["gps_dt"]).total_seconds()
                        for a, b in zip(chain, chain[1:])
                    ]
                    if c1["_gap_s"] is not None:
                        internal_gaps.append(c1["_gap_s"])
                    if c2["_gap_s"] is not None:
                        internal_gaps.append(c2["_gap_s"])
                    max_internal_gap = max(internal_gaps) if internal_gaps else travel_s

                    speed_kmh = (dist_m / travel_s) * 3.6 if travel_s > 0 else None
                    reason = ""
                    if travel_s <= 0:
                        reason = "tiempo_no_positivo"
                    elif max_internal_gap > max_gap_s:
                        reason = "brecha_trayectoria"
                    elif len(chain) < 2:
                        reason = "pocos_puntos_trayectoria"
                    elif speed_kmh is None or speed_kmh < min_speed_kmh or speed_kmh > max_speed_kmh:
                        reason = "velocidad_fuera_rango"
                    coverage_ok = int(reason == "")

                    mid_dt = t1 + timedelta(seconds=travel_s / 2.0)
                    mid_x = 0.5 * (c1["_ctrl"]["x"] + c2["_ctrl"]["x"])
                    mid_y = 0.5 * (c1["_ctrl"]["y"] + c2["_ctrl"]["y"])
                    rain_regime, rain_mm, rain_dist = rain_index.classify(mid_x, mid_y, mid_dt)
                    d_str, w_lbl, _, _ = window_bounds(t1, window_minutes, window_start, window_end)
                    segments.append(dict(
                        session_id=c1["session_id"],
                        source=source,
                        date_bogota=d_str,
                        time_window=w_lbl,
                        route_id=pat["route_id"],
                        route_code=pat["route_code"],
                        direction=pat["direction"],
                        pattern_id=pattern_id,
                        span=span,
                        from_station_id=c1["station_id"],
                        from_station_name=c1["station_name"],
                        to_station_id=c2["station_id"],
                        to_station_name=c2["station_name"],
                        from_stop_status=c1["stop_status"],
                        to_stop_status=c2["stop_status"],
                        corridor_section=c1["corridor_section"],
                        vehicle_id=vehicle_id,
                        trip_id=c1["trip_id"],
                        run_id=run_id,
                        entered_at_utc=t1.isoformat(),
                        exited_at_utc=t2.isoformat(),
                        entered_at_bogota=local_iso(t1),
                        distance_m=round(dist_m, 2),
                        travel_time_s=round(travel_s, 2),
                        speed_kmh=fmt(speed_kmh, 2),
                        gps_points_in_segment=len(seg_pts),
                        max_internal_gap_s=round(max_internal_gap, 1),
                        coverage_ok=coverage_ok,
                        exclusion_reason=reason,
                        rain_regime=rain_regime,
                        rain_mm_nearest=fmt(rain_mm, 3),
                        nearest_rain_sensor_m=fmt(rain_dist, 1),
                    ))
    return crossings, segments


def interval_has_gap(t1_ts, t2_ts, covered_intervals, max_gap_s):
    """True si entre t1 y t2 hay una caída de telemetría superior a max_gap_s."""
    if not covered_intervals:
        return False
    # Calcula cuánto tiempo dentro de [t1, t2] NO estuvo cubierto por ninguna encuesta fresca.
    covered = 0.0
    for a, b in covered_intervals:
        if b <= t1_ts or a >= t2_ts:
            continue
        covered += min(b, t2_ts) - max(a, t1_ts)
    uncovered = max(0.0, (t2_ts - t1_ts) - covered)
    return uncovered > max_gap_s


def build_block_b(crossings, combined_coverage, catalog, max_gap_s=180.0):
    rows = []
    # 1) Agregación por patrón y por (ruta, sentido).
    for level in ("pattern", "route_direction"):
        grouped = defaultdict(list)
        for c in crossings:
            if level == "pattern":
                key = (
                    c["date_bogota"], c["time_window"], c["route_code"], c["route_id"], c["direction"],
                    c["pattern_id"], c["station_id"], c["station_name"], c["stop_status"], c["corridor_section"],
                )
            else:
                key = (
                    c["date_bogota"], c["time_window"], c["route_code"], "", c["direction"],
                    "", c["station_id"], c["station_name"], c["stop_status"], c["corridor_section"],
                )
            grouped[key].append(c)

        for key, items in sorted(grouped.items()):
            d_str, w_lbl, rcode, rid, direction, pid, sid, sname, stop_status, section = key
            cov_info = combined_coverage.get((d_str, w_lbl))
            cov_row = cov_info[0] if cov_info else None
            cov_intervals = cov_info[1] if cov_info else []
            telemetry_ok = cov_row["telemetry_available"] if cov_row else 1
            cov_status = cov_row["coverage_status"] if cov_row else "cobertura_insuficiente"
            poll_ratio = cov_row["poll_coverage_ratio"] if cov_row else ""
            covered_s = float(cov_row["covered_seconds"]) if cov_row else 0.0
            observed_h = covered_s / 3600.0 if covered_s > 0 else 0.0

            if not telemetry_ok:
                rows.append(dict(
                    aggregation_level=level, date_bogota=d_str, time_window=w_lbl, route_code=rcode,
                    route_id=rid, direction=direction, pattern_id=pid, station_id=sid, station_name=sname,
                    stop_status=stop_status, corridor_section=section, telemetry_available=0,
                    coverage_status=cov_status, poll_coverage_ratio=poll_ratio, observed_hours="",
                    valid_trips="", crossing_count="", observed_departures_per_hour="",
                    headway_n="", headway_mean_s="", headway_median_s="", headway_p10_s="",
                    headway_p90_s="", headway_cv="", reliable=0,
                ))
                continue

            items.sort(key=lambda x: x["_dt"])
            valid_trips = len({(x["vehicle_id"], x["run_id"]) for x in items})
            crossing_count = len(items)
            dep_per_h = (crossing_count / observed_h) if covered_s >= 60.0 else None
            headways = []
            for a, b in zip(items, items[1:]):
                if a["vehicle_id"] == b["vehicle_id"] and a["run_id"] == b["run_id"]:
                    continue
                hw = (b["_dt"] - a["_dt"]).total_seconds()
                if hw > 0 and not interval_has_gap(a["_dt"].timestamp(), b["_dt"].timestamp(), cov_intervals, max_gap_s):
                    headways.append(hw)
            hw_arr = np.asarray(headways, dtype=float)
            hw_mean = float(hw_arr.mean()) if len(hw_arr) else None
            hw_std = float(hw_arr.std(ddof=1)) if len(hw_arr) >= 2 else None
            hw_cv = (hw_std / hw_mean) if (hw_std is not None and hw_mean and hw_mean > 0) else None

            rows.append(dict(
                aggregation_level=level,
                date_bogota=d_str,
                time_window=w_lbl,
                route_code=rcode,
                route_id=rid,
                direction=direction,
                pattern_id=pid,
                station_id=sid,
                station_name=sname,
                stop_status=stop_status,
                corridor_section=section,
                telemetry_available=1,
                coverage_status=cov_status,
                poll_coverage_ratio=poll_ratio,
                observed_hours=round(observed_h, 4) if observed_h > 0 else "",
                valid_trips=valid_trips,
                crossing_count=crossing_count,
                observed_departures_per_hour=fmt(dep_per_h, 2),
                headway_n=len(hw_arr),
                headway_mean_s=fmt(hw_mean, 2),
                headway_median_s=fmt(float(np.median(hw_arr)), 2) if len(hw_arr) else "",
                headway_p10_s=fmt(float(np.percentile(hw_arr, 10)), 2) if len(hw_arr) >= 2 else "",
                headway_p90_s=fmt(float(np.percentile(hw_arr, 90)), 2) if len(hw_arr) >= 2 else "",
                headway_cv=fmt(hw_cv, 4),
                reliable=int(cov_status == "suficiente"),
            ))

    # 2) Ventanas sin telemetría: emite explícitamente dato faltante (vacío, nunca cero).
    for (d_str, w_lbl), (cov_row, _) in sorted(combined_coverage.items()):
        if cov_row["telemetry_available"] == 0:
            seen_rd = set()
            for pat in catalog["patterns"].values():
                if not pat["operational_bool"]:
                    continue
                rd = (pat["route_code"], pat["direction"])
                if rd in seen_rd:
                    continue
                seen_rd.add(rd)
                rows.append(dict(
                    aggregation_level="route_direction",
                    date_bogota=d_str,
                    time_window=w_lbl,
                    route_code=pat["route_code"],
                    route_id="",
                    direction=pat["direction"],
                    pattern_id="",
                    station_id="",
                    station_name="",
                    stop_status="",
                    corridor_section=pat["corridor_sections"],
                    telemetry_available=0,
                    coverage_status=cov_row["coverage_status"],
                    poll_coverage_ratio=cov_row["poll_coverage_ratio"],
                    observed_hours="",
                    valid_trips="",
                    crossing_count="",
                    observed_departures_per_hour="",
                    headway_n="",
                    headway_mean_s="",
                    headway_median_s="",
                    headway_p10_s="",
                    headway_p90_s="",
                    headway_cv="",
                    reliable=0,
                ))
    return rows


def build_block_c(segments, combined_coverage, min_samples=5):
    rows = []
    for level in ("pattern", "route_direction"):
        grouped = defaultdict(list)
        for seg in segments:
            for regime in (seg["rain_regime"], "todos"):
                if level == "pattern":
                    key = (
                        seg["date_bogota"], seg["time_window"], seg["route_code"], seg["route_id"],
                        seg["direction"], seg["pattern_id"], seg["from_station_id"], seg["from_station_name"],
                        seg["to_station_id"], seg["to_station_name"], seg["corridor_section"], regime,
                    )
                else:
                    key = (
                        seg["date_bogota"], seg["time_window"], seg["route_code"], "",
                        seg["direction"], "", seg["from_station_id"], seg["from_station_name"],
                        seg["to_station_id"], seg["to_station_name"], seg["corridor_section"], regime,
                    )
                grouped[key].append(seg)

        for key, items in sorted(grouped.items()):
            d_str, w_lbl, rcode, rid, direction, pid, f_id, f_name, t_id, t_name, section, regime = key
            cov_info = combined_coverage.get((d_str, w_lbl))
            cov_row = cov_info[0] if cov_info else None
            telemetry_ok = cov_row["telemetry_available"] if cov_row else 1
            cov_status = cov_row["coverage_status"] if cov_row else "cobertura_insuficiente"

            valid = [s for s in items if s["coverage_ok"] == 1]
            excluded = len(items) - len(valid)
            dist_m = float(np.median([s["distance_m"] for s in items])) if items else None
            if not telemetry_ok:
                rows.append(dict(
                    aggregation_level=level, date_bogota=d_str, time_window=w_lbl, route_code=rcode,
                    route_id=rid, direction=direction, pattern_id=pid, from_station_id=f_id,
                    from_station_name=f_name, to_station_id=t_id, to_station_name=t_name,
                    corridor_section=section, rain_regime=regime, telemetry_available=0,
                    coverage_status=cov_status, distance_m=fmt(dist_m, 2), sample_size="",
                    excluded_low_coverage_n="", sufficient_samples=0, mean_s="", median_s="",
                    p10_s="", p25_s="", p75_s="", p90_s="", iqr_s="", speed_median_kmh="",
                ))
                continue

            arr = np.asarray([s["travel_time_s"] for s in valid], dtype=float)
            n = len(arr)
            sufficient = int(n >= min_samples)
            med = float(np.median(arr)) if n >= 1 else None
            speed_med = ((dist_m / med) * 3.6) if (med and med > 0 and dist_m) else None
            p10 = float(np.percentile(arr, 10)) if sufficient else None
            p25 = float(np.percentile(arr, 25)) if sufficient else None
            p75 = float(np.percentile(arr, 75)) if sufficient else None
            p90 = float(np.percentile(arr, 90)) if sufficient else None
            iqr = (p75 - p25) if sufficient else None

            rows.append(dict(
                aggregation_level=level,
                date_bogota=d_str,
                time_window=w_lbl,
                route_code=rcode,
                route_id=rid,
                direction=direction,
                pattern_id=pid,
                from_station_id=f_id,
                from_station_name=f_name,
                to_station_id=t_id,
                to_station_name=t_name,
                corridor_section=section,
                rain_regime=regime,
                telemetry_available=1,
                coverage_status=cov_status,
                distance_m=fmt(dist_m, 2),
                sample_size=n,
                excluded_low_coverage_n=excluded,
                sufficient_samples=sufficient,
                mean_s=fmt(float(arr.mean()), 2) if n >= 1 else "",
                median_s=fmt(med, 2),
                p10_s=fmt(p10, 2),
                p25_s=fmt(p25, 2),
                p75_s=fmt(p75, 2),
                p90_s=fmt(p90, 2),
                iqr_s=fmt(iqr, 2),
                speed_median_kmh=fmt(speed_med, 2),
            ))
    return rows


def build_rain_comparison(segments, min_samples=5):
    grouped = defaultdict(lambda: {"seco": [], "lluvia": []})
    for seg in segments:
        if seg["coverage_ok"] != 1:
            continue
        if seg["rain_regime"] not in ("seco", "lluvia"):
            continue
        key = (
            seg["route_code"], seg["direction"], seg["from_station_id"], seg["from_station_name"],
            seg["to_station_id"], seg["to_station_name"], seg["corridor_section"], seg["time_window"],
        )
        grouped[key][seg["rain_regime"]].append(seg["travel_time_s"])

    rows = []
    for key, buckets in sorted(grouped.items()):
        rcode, direction, f_id, f_name, t_id, t_name, section, w_lbl = key
        dry = np.asarray(buckets["seco"], dtype=float)
        rain = np.asarray(buckets["lluvia"], dtype=float)
        n_dry, n_rain = len(dry), len(rain)
        med_dry = float(np.median(dry)) if n_dry >= 1 else None
        med_rain = float(np.median(rain)) if n_rain >= 1 else None
        p90_dry = float(np.percentile(dry, 90)) if n_dry >= min_samples else None
        p90_rain = float(np.percentile(rain, 90)) if n_rain >= min_samples else None
        delta = (med_rain - med_dry) if (med_dry is not None and med_rain is not None) else None
        pct = ((delta / med_dry) * 100.0) if (delta is not None and med_dry > 0) else None
        rows.append(dict(
            route_code=rcode,
            direction=direction,
            from_station_id=f_id,
            from_station_name=f_name,
            to_station_id=t_id,
            to_station_name=t_name,
            corridor_section=section,
            time_window=w_lbl,
            n_dry=n_dry,
            median_dry_s=fmt(med_dry, 2),
            p90_dry_s=fmt(p90_dry, 2),
            n_rain=n_rain,
            median_rain_s=fmt(med_rain, 2),
            p90_rain_s=fmt(p90_rain, 2),
            delta_median_s=fmt(delta, 2),
            pct_increase_median=fmt(pct, 2),
            comparable_sufficient=int(n_dry >= min_samples and n_rain >= min_samples),
        ))
    return rows


def export_metrics(args):
    catalog = load_catalog(args.catalog)
    session_dirs = discover_sessions(args.sessions)
    filter_dates = set(args.date or [])
    window_start = args.start
    window_end = args.end
    window_minutes = args.window_minutes
    freshness_s = args.freshness_s
    rain_index = RainIndex(
        args.rain,
        radius_m=args.rain_radius_m,
        window_min=args.rain_window_min,
        threshold_mm=args.rain_threshold_mm,
    )

    coverage_rows, combined_coverage, quality_events = compute_coverage(
        session_dirs, window_minutes, filter_dates, window_start, window_end, args.min_coverage
    )

    # 1) Lee todas las observaciones GPS y evalúa calidad + asociación de patrón.
    raw_records = []
    for sdir in session_dirs:
        gps_path = sdir / "gps.csv"
        if not gps_path.exists():
            continue
        for row in read_csv(gps_path):
            captured_dt = timestamp(row.get("captured_at"))
            gps_dt = timestamp(row.get("gps_at"))
            ref_dt = gps_dt or captured_dt
            if ref_dt and not matches_filter(ref_dt, filter_dates, window_start, window_end):
                continue
            raw_records.append((sdir.name, row, captured_dt, gps_dt))

    # Ordena por hora GPS y hora de captura para deduplicar conservando la primera captura (menor atraso).
    raw_records.sort(key=lambda item: (
        item[3].timestamp() if item[3] else float("inf"),
        item[2].timestamp() if item[2] else float("inf"),
        item[1].get("sample_id", ""),
    ))

    seen_reports = set()
    observations_out = []
    usable_obs = []
    quality_counts = Counter()

    for session_id, row, captured_dt, gps_dt in raw_records:
        q_status, age_s = quality(row, freshness_s)
        src = (row.get("source") or "").strip()
        src_live = parse_bool(row.get("source_live"))
        live_mismatch = int(src_live is True and q_status == "atrasado")

        ref_dt = gps_dt or captured_dt
        if ref_dt:
            d_str, w_lbl, _, _ = window_bounds(ref_dt, window_minutes, window_start, window_end)
        else:
            d_str, w_lbl = "", ""

        vid = (row.get("vehicle_id") or "").strip()
        dedup_key = (src, vid, gps_dt.isoformat()) if (vid and gps_dt) else None
        is_dup = 0
        if dedup_key is not None:
            if dedup_key in seen_reports:
                is_dup = 1
            else:
                seen_reports.add(dedup_key)

        match_status, pid, candidates = match_pattern(row, catalog, include_non_operational=args.include_non_operational)
        pat = catalog["patterns"].get(pid) if pid else None
        xy = coordinates(row.get("longitude"), row.get("latitude"))
        dist_m = None
        pos_m = None
        if xy and pid and pid in catalog["geometries"]:
            pt = Point(TO_METRES.transform(xy[0], xy[1]))
            line = catalog["geometries"][pid]
            dist_m = float(line.distance(pt))
            pos_m = float(line.project(pt))

        usable = int(
            q_status == "fresco"
            and is_dup == 0
            and pid != ""
            and match_status != "ambiguo"
            and (args.include_non_operational or (pat is not None and pat["operational_bool"]))
            and dist_m is not None
            and dist_m <= args.max_off_route_m
        )

        # Registro agregado de anomalías en quality_log.
        if q_status != "fresco":
            quality_counts[(session_id, src, d_str, w_lbl, f"calidad:{q_status}")] += 1
        if live_mismatch:
            quality_counts[(session_id, src, d_str, w_lbl, "croma_live_true_pero_atrasado")] += 1
        if is_dup:
            quality_counts[(session_id, src, d_str, w_lbl, "duplicado")] += 1
        if match_status == "ambiguo":
            quality_counts[(session_id, src, d_str, w_lbl, "patron_ambiguo")] += 1
        elif match_status == "fuera_de_catalogo":
            quality_counts[(session_id, src, d_str, w_lbl, "fuera_de_catalogo")] += 1
        elif dist_m is not None and dist_m > args.max_off_route_m and q_status == "fresco" and not is_dup:
            quality_counts[(session_id, src, d_str, w_lbl, "fuera_de_trazado")] += 1

        obs_row = dict(
            session_id=session_id,
            sample_id=row.get("sample_id", ""),
            source=src,
            captured_at_utc=captured_dt.isoformat() if captured_dt else "",
            captured_at_bogota=local_iso(captured_dt),
            gps_at_utc=gps_dt.isoformat() if gps_dt else "",
            gps_at_bogota=local_iso(gps_dt),
            date_bogota=d_str,
            time_window=w_lbl,
            gps_age_s=fmt(age_s, 2),
            source_live="" if src_live is None else int(src_live),
            source_live_stale_mismatch=live_mismatch,
            quality_status=q_status,
            is_duplicate=is_dup,
            vehicle_id=vid,
            route_id=row.get("route_id", ""),
            route_code=pat["route_code"] if pat else row.get("route_code", ""),
            trip_id=row.get("trip_id", ""),
            pattern_match_status=match_status,
            pattern_id=pid,
            candidate_pattern_ids=candidates,
            direction=pat["direction"] if pat else "",
            system=pat["system"] if pat else "",
            operational=int(pat["operational_bool"]) if pat else "",
            longitude=xy[0] if xy else "",
            latitude=xy[1] if xy else "",
            dist_to_pattern_m=fmt(dist_m, 2),
            position_m=fmt(pos_m, 2),
            stop_id=row.get("stop_id", ""),
            stop_status=row.get("stop_status", ""),
            usable_for_metrics=usable,
        )
        observations_out.append(obs_row)
        if usable:
            usable_obs.append(dict(
                obs_row,
                gps_dt=gps_dt,
                captured_dt=captured_dt,
                position_m=pos_m,
                trip_start_date=row.get("trip_start_date", ""),
                trip_start_time=row.get("trip_start_time", ""),
            ))

    for (sess, src, d_str, w_lbl, detail), cnt in sorted(quality_counts.items()):
        quality_events.append(dict(
            event_type="observaciones",
            severity="warning",
            session_id=sess,
            sample_id="",
            source=src,
            date_bogota=d_str,
            time_window=w_lbl,
            detail=detail,
            count=cnt,
        ))

    crossings, segments = detect_crossings_and_segments(
        usable_obs,
        catalog,
        rain_index,
        max_gap_s=args.max_gap_s,
        checkpoint_radius_m=args.checkpoint_radius_m,
        run_split_gap_s=args.run_split_gap_s,
        window_minutes=window_minutes,
        window_start=window_start,
        window_end=window_end,
    )
    block_b_rows = build_block_b(crossings, combined_coverage, catalog, max_gap_s=args.max_gap_s)
    block_c_rows = build_block_c(segments, combined_coverage, min_samples=args.min_samples)
    rain_comp_rows = build_rain_comparison(segments, min_samples=args.min_samples)

    out = Path(args.out)
    write_csv(out / "gps_observations.csv", OBSERVATION_FIELDS, observations_out)
    write_csv(out / "quality_log.csv", QUALITY_LOG_FIELDS, quality_events)
    write_csv(out / "capture_coverage.csv", COVERAGE_FIELDS, coverage_rows)
    write_csv(
        out / "checkpoint_crossings.csv",
        CROSSING_FIELDS,
        ({k: row[k] for k in CROSSING_FIELDS.split()} for row in crossings),
    )
    write_csv(out / "segment_travel_times.csv", SEGMENT_FIELDS, segments)
    write_csv(out / "metrics_block_b_headways.csv", BLOCK_B_FIELDS, block_b_rows)
    write_csv(out / "metrics_block_c_segments.csv", BLOCK_C_FIELDS, block_c_rows)
    write_csv(out / "rain_comparison.csv", RAIN_COMPARISON_FIELDS, rain_comp_rows)

    validations_meta = None
    if args.validations and Path(args.validations).exists():
        v_meta_path = Path(args.validations).with_suffix(".json")
        validations_meta = (
            json.loads(v_meta_path.read_text(encoding="utf-8"))
            if v_meta_path.exists()
            else {"path": str(args.validations)}
        )

    assumptions_doc = dict(
        EXPLICIT_ASSUMPTIONS,
        provenance=dict(
            catalog_id=catalog["meta"].get("catalog_id"),
            gtfs_version=catalog["meta"].get("gtfs_version"),
            gtfs_sha256=catalog["meta"].get("gtfs_sha256"),
            corridor_sha256=catalog["meta"].get("corridor_sha256"),
            sessions=[str(p) for p in session_dirs],
            rain_source=str(args.rain) if args.rain else None,
            validations_source=validations_meta,
            timezone=BOGOTA.key,
            parameters=dict(
                freshness_s=freshness_s,
                min_coverage=args.min_coverage,
                max_gap_s=args.max_gap_s,
                checkpoint_radius_m=args.checkpoint_radius_m,
                max_off_route_m=args.max_off_route_m,
                min_samples=args.min_samples,
                window_minutes=window_minutes,
                window_start=window_start.isoformat() if window_start else None,
                window_end=window_end.isoformat() if window_end else None,
                rain_threshold_mm=args.rain_threshold_mm,
                rain_radius_m=args.rain_radius_m,
                rain_window_min=args.rain_window_min,
            ),
        ),
        summary=dict(
            observations_total=len(observations_out),
            observations_usable=len(usable_obs),
            crossings_total=len(crossings),
            segments_total=len(segments),
            segments_valid=sum(s["coverage_ok"] for s in segments),
            block_b_rows=len(block_b_rows),
            block_c_rows=len(block_c_rows),
            rain_comparison_rows=len(rain_comp_rows),
        ),
    )
    write_json(out / "assumptions.json", assumptions_doc)
    return assumptions_doc["summary"]
