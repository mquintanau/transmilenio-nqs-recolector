"""Pruebas unitarias e integración con datos sintéticos y verificación del catálogo real."""
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime, time as clock, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipFile

from nqs.catalog import TO_LONLAT, build_catalog
from nqs.cli import main as cli_main
from nqs.common import BOGOTA, read_csv, write_csv, write_json
from nqs.inputs import validations
from nqs.live import GPS, POLLS, REQUESTS, capture
from nqs.metrics import export_metrics


def utm_to_ll(x, y):
    lon, lat = TO_LONLAT.transform(x, y)
    return float(lon), float(lat)


def write_mini_corridor(path):
    """Corredor sintético NQS Norte-Sur de 3000 m en EPSG:32618."""
    coords = [list(utm_to_ll(600000.0, 510000.0 + dy)) for dy in (0.0, 1000.0, 2000.0, 3000.0)]
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id_trazado": "TZ008", "le_troncal": "E", "nom_traz": "NQS Central Sintética"},
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        ],
    }
    path.write_text(json.dumps(geojson), encoding="utf-8")


def write_mini_gtfs(zip_path):
    """GTFS mínimo con:
    - Ruta troncal `E42` con parada en S1, S3 y paso sin parar en S2 (dos variantes para probar ambigüedad)
    - Ruta troncal `P99` que recorre toda la NQS pasando SIN parar por ninguna estación NQS
    - Ruta troncal `X01` que SOLO CRUZA perpendicularmente la NQS (debe quedar excluida)
    - Ruta zonal `Z10` que corre paralela a 15 m por 2000 m (zonal -> operational=0)
    """
    def csv_bytes(headers, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    # Estaciones (location_type=1) y plataformas (parent_station).
    s1 = utm_to_ll(600000.0, 510500.0)
    s2 = utm_to_ll(600000.0, 511500.0)
    s3 = utm_to_ll(600000.0, 512500.0)
    s_ext_a = utm_to_ll(600000.0, 509000.0)
    s_ext_b = utm_to_ll(600000.0, 514000.0)
    s_west = utm_to_ll(598500.0, 511500.0)
    s_east = utm_to_ll(601500.0, 511500.0)
    z_a = utm_to_ll(600015.0, 510400.0)
    z_b = utm_to_ll(600015.0, 512600.0)

    stops = [
        ["7110", "Paloquemao", s1[1], s1[0], "1", ""],
        ["71101", "Paloquemao V1", s1[1], s1[0], "0", "7110"],
        ["7109", "CAD", s2[1], s2[0], "1", ""],
        ["71091", "CAD V1", s2[1], s2[0], "0", "7109"],
        ["7107", "Universidad Nacional", s3[1], s3[0], "1", ""],
        ["71071", "Universidad Nacional V1", s3[1], s3[0], "0", "7107"],
        ["9001", "Externa Sur", s_ext_a[1], s_ext_a[0], "1", ""],
        ["9002", "Externa Norte", s_ext_b[1], s_ext_b[0], "1", ""],
        ["9010", "Cruce Oeste", s_west[1], s_west[0], "1", ""],
        ["9011", "Cruce Este", s_east[1], s_east[0], "1", ""],
        ["8001", "Paradero Zonal Sur", z_a[1], z_a[0], "0", ""],
        ["8002", "Paradero Zonal Norte", z_b[1], z_b[0], "0", ""],
    ]

    shapes = []
    # SH_CORR: eje completo de 509000 a 514000 (3000 m sobre NQS).
    for seq, dy in enumerate(range(-1000, 4001, 500), 1):
        lon, lat = utm_to_ll(600000.0, 510000.0 + dy)
        shapes.append(["SH_CORR", lat, lon, seq])
    # SH_CROSS: perpendicular de x=598500 a x=601500 a y=511500 (~60 m dentro del buffer de 30 m).
    for seq, dx in enumerate(range(-1500, 1501, 250), 1):
        lon, lat = utm_to_ll(600000.0 + dx, 511500.0)
        shapes.append(["SH_CROSS", lat, lon, seq])
    # SH_ZONAL: paralelo a x=600015 (15 m del eje, dentro de 30 m) por 2200 m.
    for seq, dy in enumerate(range(400, 2601, 200), 1):
        lon, lat = utm_to_ll(600015.0, 510000.0 + dy)
        shapes.append(["SH_ZONAL", lat, lon, seq])

    agencies = [
        ["1", "TransMilenio Troncal", "https://transmilenio.gov.co", "America/Bogota"],
        ["2", "SITP Zonal", "https://transmilenio.gov.co", "America/Bogota"],
    ]
    routes = [
        ["R_E42", "1", "E42", "7 de Agosto", "3"],
        ["R_P99", "1", "P99", "Expreso Sin Parada NQS", "3"],
        ["R_X01", "1", "X01", "Solo Cruce Transversal", "3"],
        ["R_Z10", "2", "Z10", "Zonal Paralelo NQS", "3"],
    ]
    trips = [
        # Variante 1 de E42: para en S1 y S3, pasa de largo por S2.
        ["R_E42", "WKD", "T_E42_A", "0", "SH_CORR"],
        # Variante 2 de E42 (corta): para en S1, S2 y S3 (crea ambigüedad si falta trip_id).
        ["R_E42", "WKD", "T_E42_B", "0", "SH_CORR"],
        # Ruta P99: solo para fuera de la NQS (9001 -> 9002), pasa de largo por S1, S2 y S3.
        ["R_P99", "WKD", "T_P99_1", "0", "SH_CORR"],
        # Ruta X01: solo cruza la NQS.
        ["R_X01", "WKD", "T_X01_1", "0", "SH_CROSS"],
        # Ruta Z10: zonal paralela.
        ["R_Z10", "WKD", "T_Z10_1", "0", "SH_ZONAL"],
    ]
    stop_times = [
        ["T_E42_A", "07:00:00", "07:00:00", "71101", "1", "0", "0"],
        ["T_E42_A", "07:10:00", "07:10:00", "71071", "2", "0", "0"],
        ["T_E42_B", "07:05:00", "07:05:00", "71101", "1", "0", "0"],
        ["T_E42_B", "07:08:00", "07:08:00", "71091", "2", "0", "0"],
        ["T_E42_B", "07:12:00", "07:12:00", "71071", "3", "0", "0"],
        ["T_P99_1", "07:00:00", "07:00:00", "9001", "1", "0", "0"],
        ["T_P99_1", "07:15:00", "07:15:00", "9002", "2", "0", "0"],
        ["T_X01_1", "07:00:00", "07:00:00", "9010", "1", "0", "0"],
        ["T_X01_1", "07:10:00", "07:10:00", "9011", "2", "0", "0"],
        ["T_Z10_1", "07:00:00", "07:00:00", "8001", "1", "0", "0"],
        ["T_Z10_1", "07:10:00", "07:10:00", "8002", "2", "0", "0"],
    ]
    feed_info = [["TransMilenio", "https://transmilenio.gov.co", "es", "20260101", "20261231", "TEST_20261124"]]

    with ZipFile(zip_path, "w") as zf:
        zf.writestr("agency.txt", csv_bytes(["agency_id", "agency_name", "agency_url", "agency_timezone"], agencies))
        zf.writestr("routes.txt", csv_bytes(["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"], routes))
        zf.writestr("trips.txt", csv_bytes(["route_id", "service_id", "trip_id", "direction_id", "shape_id"], trips))
        zf.writestr("stops.txt", csv_bytes(["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"], stops))
        zf.writestr("shapes.txt", csv_bytes(["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"], shapes))
        zf.writestr("stop_times.txt", csv_bytes(["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence", "pickup_type", "drop_off_type"], stop_times))
        zf.writestr("feed_info.txt", csv_bytes(["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date", "feed_end_date", "feed_version"], feed_info))


class NqsCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corridor_path = self.root / "corridor.geojson"
        self.gtfs_path = self.root / "GTFS_20261124.zip"
        self.catalog_dir = self.root / "derived" / "catalog"
        write_mini_corridor(self.corridor_path)
        write_mini_gtfs(self.gtfs_path)
        build_catalog(SimpleNamespace(
            gtfs=str(self.gtfs_path),
            corridor=str(self.corridor_path),
            out=self.catalog_dir,
            raw_dir=self.root / "raw" / "insumos",
            version=None,
            tolerance_m=30.0,
            min_overlap_m=300.0,
            station_m=65.0,
            link_gap_m=100.0,
            trunk_agency=None,
        ))

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_passthrough_crossing_zonal_and_platforms(self):
        patterns = list(read_csv(self.catalog_dir / "patterns.csv"))
        by_code = {}
        for p in patterns:
            by_code.setdefault(p["route_code"], []).append(p)

        # 1) La ruta que solo cruza la NQS (X01) queda excluida del catálogo y auditada en shape_audit.
        self.assertNotIn("X01", by_code)
        audit = {r["route_code"]: r for r in read_csv(self.catalog_dir / "shape_audit.csv")}
        self.assertEqual(audit["X01"]["catalogued_trips"], "0")
        self.assertLess(float(audit["X01"]["max_continuous_m"]), 300.0)

        # 2) La ruta que recorre la NQS pasando sin parar (P99) sí queda catalogada como troncal operativa,
        #    y todas sus estaciones NQS figuran con stop_status == "paso".
        self.assertIn("P99", by_code)
        p99 = by_code["P99"][0]
        self.assertEqual(p99["system"], "troncal")
        self.assertEqual(p99["operational"], "1")
        controls = [c for c in read_csv(self.catalog_dir / "pattern_controls.csv") if c["pattern_id"] == p99["pattern_id"]]
        self.assertEqual(len(controls), 3)
        self.assertTrue(all(c["stop_status"] == "paso" and c["pickup"] == "0" and c["dropoff"] == "0" for c in controls))

        # 3) Distinción troncal vs zonal paralela.
        self.assertIn("Z10", by_code)
        self.assertEqual(by_code["Z10"][0]["system"], "zonal")
        self.assertEqual(by_code["Z10"][0]["operational"], "0")

        # 4) Relación plataforma -> estación padre en E42 variante A (para en 7110 y 7107, pasa en 7109).
        trips = {r["trip_id"]: r["pattern_id"] for r in read_csv(self.catalog_dir / "trips.csv")}
        e42_a_pid = trips["T_E42_A"]
        e42_ctrls = {c["station_id"]: c for c in read_csv(self.catalog_dir / "pattern_controls.csv") if c["pattern_id"] == e42_a_pid}
        self.assertEqual(e42_ctrls["7110"]["stop_status"], "parada")
        self.assertEqual(e42_ctrls["7110"]["platforms"], "71101")
        self.assertEqual(e42_ctrls["7109"]["stop_status"], "paso")
        self.assertEqual(e42_ctrls["7107"]["stop_status"], "parada")
        self.assertEqual(e42_ctrls["7107"]["platforms"], "71071")

        # 5) Versión GTFS registrada en catalog.json.
        meta = json.loads((self.catalog_dir / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["gtfs_version"], "20261124")

    def test_quality_stale_duplicate_ambiguous_crossings_and_rain(self):
        # Construye dos sesiones de 07:00-08:00 (un día seco y un día lluvioso) + casos de borde.
        day_dry = datetime(2026, 11, 24, 7, 0, 0, tzinfo=BOGOTA).astimezone(UTC)
        day_rain = datetime(2026, 11, 25, 7, 0, 0, tzinfo=BOGOTA).astimezone(UTC)

        def make_session(name, base_dt, bus_speed_scale=1.0, add_edge_cases=False):
            sdir = self.root / "raw" / "live" / name
            sdir.mkdir(parents=True)
            write_json(sdir / "session.json", dict(
                session_id=name, source="croma", started_at=base_dt.isoformat(),
                ended_at=(base_dt + timedelta(hours=1)).isoformat(), interval_s=30.0, duration_s=3600.0,
            ))
            # 120 polls frescos cubriendo toda la hora 07:00-08:00.
            polls = []
            for i in range(120):
                sf = base_dt + timedelta(seconds=30 * i)
                st = sf + timedelta(seconds=30)
                polls.append(dict(
                    sample_id=f"{name}:{i+1}", slot_from=sf.isoformat(), slot_to=st.isoformat(),
                    captured_at=st.isoformat(), source="croma", status="ok", requests=1, received=2, fresh=2,
                    scope_route="", scope_lon="", scope_lat="", scope_radius_m="",
                ))
            write_csv(sdir / "polls.csv", POLLS, polls)
            write_csv(sdir / "requests.csv", REQUESTS, [])

            gps_rows = []
            # 3 buses frescos con intervalo de 10 minutos (600 s) en E42 variante A (trip_id=T_E42_A).
            for bus_idx in range(3):
                t0 = base_dt + timedelta(minutes=5 + 10 * bus_idx)
                vid = f"veh_{bus_idx}"
                # Puntos cada 100 m desde y=510200 hasta y=512800.
                # Con bus_speed_scale=1.0 -> 100 m cada 12 s (~30 km/h -> 120 s por tramo de 1000 m).
                # Con bus_speed_scale=1.5 -> 100 m cada 18 s (~20 km/h -> 180 s por tramo de 1000 m, +50% lluvia).
                step_s = 12.0 * bus_speed_scale
                for k in range(27):
                    y = 510200.0 + 100.0 * k
                    lon, lat = utm_to_ll(600000.0, y)
                    gps_t = t0 + timedelta(seconds=k * step_s)
                    cap_t = gps_t + timedelta(seconds=10)
                    gps_rows.append(dict(
                        sample_id=f"{name}:1", source="croma", captured_at=cap_t.isoformat(),
                        gps_at=gps_t.isoformat(), feed_at=gps_t.isoformat(), vehicle_id=vid,
                        route_id="R_E42", route_code="E42", trip_id="T_E42_A",
                        trip_start_date="20261124", trip_start_time="07:00:00",
                        longitude=lon, latitude=lat, stop_id="", stop_status="in_transit_to", source_live="True",
                    ))
                    # En el primer bus, añade permanencia (dwell) extra cerca de CAD (y=511500) para verificar conteo único.
                    if add_edge_cases and bus_idx == 0 and y == 511500.0:
                        for extra in (4, 8):
                            dt_d = gps_t + timedelta(seconds=extra)
                            gps_rows.append(dict(
                                sample_id=f"{name}:2", source="croma", captured_at=(dt_d + timedelta(seconds=5)).isoformat(),
                                gps_at=dt_d.isoformat(), feed_at=dt_d.isoformat(), vehicle_id=vid,
                                route_id="R_E42", route_code="E42", trip_id="T_E42_A",
                                trip_start_date="20261124", trip_start_time="07:00:00",
                                longitude=lon, latitude=lat, stop_id="", stop_status="in_transit_to", source_live="True",
                            ))

            if add_edge_cases:
                lon_mid, lat_mid = utm_to_ll(600000.0, 511000.0)
                # Caso 1: Posición atrasada 125 s con Croma source_live=True (debe marcarse atrasado y mismatch).
                gps_stale = base_dt + timedelta(minutes=2)
                gps_rows.append(dict(
                    sample_id=f"{name}:5", source="croma", captured_at=(gps_stale + timedelta(seconds=125)).isoformat(),
                    gps_at=gps_stale.isoformat(), feed_at=gps_stale.isoformat(), vehicle_id="veh_stale",
                    route_id="R_E42", route_code="E42", trip_id="T_E42_A", trip_start_date="", trip_start_time="",
                    longitude=lon_mid, latitude=lat_mid, stop_id="", stop_status="", source_live="True",
                ))
                # Caso 2: Observación duplicada (mismo vehicle_id y gps_at capturado 20 s después).
                dup = dict(gps_rows[0])
                dup["sample_id"] = f"{name}:99"
                dup["captured_at"] = (datetime.fromisoformat(dup["captured_at"]) + timedelta(seconds=20)).isoformat()
                gps_rows.append(dup)
                # Caso 3: Observación ambigua (sin trip_id en R_E42, que tiene 2 variantes operativas).
                amb_t = base_dt + timedelta(minutes=3)
                gps_rows.append(dict(
                    sample_id=f"{name}:6", source="croma", captured_at=(amb_t + timedelta(seconds=5)).isoformat(),
                    gps_at=amb_t.isoformat(), feed_at=amb_t.isoformat(), vehicle_id="veh_amb",
                    route_id="R_E42", route_code="E42", trip_id="", trip_start_date="", trip_start_time="",
                    longitude=lon_mid, latitude=lat_mid, stop_id="", stop_status="", source_live="True",
                ))
                # Caso 4: Bus con brecha de trayectoria de 260 s (> max_gap_s=180 s) entre S1 y S2 -> excluido en Bloque C.
                t_gap = base_dt + timedelta(minutes=40)
                for dy, secs in ((400.0, 0), (500.0, 10), (600.0, 20), (1400.0, 280), (1500.0, 290), (1600.0, 300)):
                    lo, la = utm_to_ll(600000.0, 510000.0 + dy)
                    gt = t_gap + timedelta(seconds=secs)
                    gps_rows.append(dict(
                        sample_id=f"{name}:80", source="croma", captured_at=(gt + timedelta(seconds=5)).isoformat(),
                        gps_at=gt.isoformat(), feed_at=gt.isoformat(), vehicle_id="veh_gap",
                        route_id="R_E42", route_code="E42", trip_id="T_E42_A", trip_start_date="", trip_start_time="",
                        longitude=lo, latitude=la, stop_id="", stop_status="", source_live="True",
                    ))
            write_csv(sdir / "gps.csv", GPS, gps_rows)
            return sdir

        s_dry = make_session("sess_dry", day_dry, bus_speed_scale=1.0, add_edge_cases=True)
        s_rain = make_session("sess_rain", day_rain, bus_speed_scale=1.5, add_edge_cases=False)

        # Serie de lluvia: 0.0 mm el 24/11 y 2.4 mm el 25/11 junto al corredor.
        rain_csv = self.root / "raw" / "lluvia" / "sab_lluvia.csv"
        rlon, rlat = utm_to_ll(600200.0, 511500.0)
        write_csv(
            rain_csv,
            "captured_at measured_at sensor_id station longitude latitude rain_mm sensor_status source",
            [
                dict(captured_at=day_dry.isoformat(), measured_at=(day_dry + timedelta(minutes=10)).isoformat(),
                     sensor_id="100", station="NQS Test", longitude=rlon, latitude=rlat, rain_mm=0.0, sensor_status=1, source="test"),
                dict(captured_at=day_rain.isoformat(), measured_at=(day_rain + timedelta(minutes=10)).isoformat(),
                     sensor_id="100", station="NQS Test", longitude=rlon, latitude=rlat, rain_mm=2.4, sensor_status=1, source="test"),
            ],
        )

        out_metrics = self.root / "derived" / "metrics"
        summary = export_metrics(SimpleNamespace(
            catalog=self.catalog_dir,
            sessions=[s_dry, s_rain],
            out=out_metrics,
            rain=rain_csv,
            validations=None,
            date=[],
            start=clock(7, 0),
            end=clock(8, 0),
            window_minutes=60,
            freshness_s=90.0,
            min_coverage=0.7,
            max_gap_s=180.0,
            checkpoint_radius_m=45.0,
            max_off_route_m=65.0,
            run_split_gap_s=900.0,
            min_samples=3,
            rain_threshold_mm=0.1,
            rain_radius_m=5000.0,
            rain_window_min=30.0,
            include_non_operational=False,
        ))
        self.assertGreater(summary["crossings_total"], 0)

        obs = list(read_csv(out_metrics / "gps_observations.csv"))
        # Verifica atraso 125s con source_live=True.
        stale = [o for o in obs if o["vehicle_id"] == "veh_stale"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["quality_status"], "atrasado")
        self.assertEqual(stale[0]["source_live_stale_mismatch"], "1")
        self.assertEqual(stale[0]["usable_for_metrics"], "0")

        # Verifica deduplicación.
        dups = [o for o in obs if o["is_duplicate"] == "1"]
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["usable_for_metrics"], "0")

        # Verifica ambigüedad preservada.
        amb = [o for o in obs if o["vehicle_id"] == "veh_amb"]
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0]["pattern_match_status"], "ambiguo")
        self.assertEqual(len(amb[0]["candidate_pattern_ids"].split("|")), 2)
        self.assertEqual(amb[0]["usable_for_metrics"], "0")

        # Verifica conteo único por control (veh_0 con dwell en CAD debe cruzar CAD exactamente 1 vez).
        crossings = list(read_csv(out_metrics / "checkpoint_crossings.csv"))
        veh0_cad = [c for c in crossings if c["session_id"] == "sess_dry" and c["vehicle_id"] == "veh_0" and c["station_id"] == "7109"]
        self.assertEqual(len(veh0_cad), 1)

        # Verifica exclusión del tramo con brecha de 260 s en veh_gap.
        segs = list(read_csv(out_metrics / "segment_travel_times.csv"))
        gap_segs = [s for s in segs if s["vehicle_id"] == "veh_gap"]
        self.assertEqual(len(gap_segs), 1)
        self.assertEqual(gap_segs[0]["coverage_ok"], "0")
        self.assertEqual(gap_segs[0]["exclusion_reason"], "brecha_trayectoria")

        # Verifica Bloque B (intervalos de 600 s entre 3 buses válidos en día seco).
        block_b = list(read_csv(out_metrics / "metrics_block_b_headways.csv"))
        b_cad = [r for r in block_b if r["aggregation_level"] == "pattern" and r["date_bogota"] == "2026-11-24" and r["station_id"] == "7109"][0]
        self.assertEqual(b_cad["telemetry_available"], "1")
        self.assertEqual(b_cad["valid_trips"], "4")  # 3 buses completos + veh_gap que cruzó CAD
        self.assertEqual(b_cad["reliable"], "1")

        # Verifica comparación seco vs lluvia en mismo tramo (7110 -> 7109), sentido norte y franja 07:00-08:00.
        comp = list(read_csv(out_metrics / "rain_comparison.csv"))
        row_seg1 = [r for r in comp if r["from_station_id"] == "7110" and r["to_station_id"] == "7109"][0]
        self.assertEqual(row_seg1["n_dry"], "3")
        self.assertEqual(row_seg1["n_rain"], "3")
        self.assertAlmostEqual(float(row_seg1["median_dry_s"]), 120.0, delta=2.0)
        self.assertAlmostEqual(float(row_seg1["median_rain_s"]), 180.0, delta=2.0)
        self.assertAlmostEqual(float(row_seg1["pct_increase_median"]), 50.0, delta=2.0)
        self.assertEqual(row_seg1["comparable_sufficient"], "1")

    def test_missing_telemetry_is_missing_not_zero(self):
        base_dt = datetime(2026, 11, 26, 7, 0, 0, tzinfo=BOGOTA).astimezone(UTC)
        sdir = self.root / "raw" / "live" / "sess_failed"
        sdir.mkdir(parents=True)
        write_json(sdir / "session.json", dict(
            session_id="sess_failed", source="oficial", started_at=base_dt.isoformat(),
            ended_at=(base_dt + timedelta(minutes=15)).isoformat(), interval_s=30.0, duration_s=900.0,
        ))
        write_csv(sdir / "gps.csv", GPS, [])
        write_csv(sdir / "polls.csv", POLLS, [
            dict(sample_id="sess_failed:1", slot_from=base_dt.isoformat(),
                 slot_to=(base_dt + timedelta(seconds=30)).isoformat(), captured_at=base_dt.isoformat(),
                 source="oficial", status="http_500", requests=1, received=0, fresh=0,
                 scope_route="", scope_lon="", scope_lat="", scope_radius_m=""),
        ])
        write_csv(sdir / "requests.csv", REQUESTS, [
            dict(sample_id="sess_failed:1", source="oficial", endpoint="vehicle",
                 requested_at=base_dt.isoformat(), http_status=500, outcome="http_500",
                 credits_remaining="", credit_reset="", retry_after="", cache=""),
        ])
        out_metrics = self.root / "derived" / "metrics_failed"
        export_metrics(SimpleNamespace(
            catalog=self.catalog_dir, sessions=[sdir], out=out_metrics, rain=None, validations=None,
            date=[date(2026, 11, 26)], start=clock(7, 0), end=clock(8, 0), window_minutes=60,
            freshness_s=90.0, min_coverage=0.7, max_gap_s=180.0, checkpoint_radius_m=45.0,
            max_off_route_m=65.0, run_split_gap_s=900.0, min_samples=5,
            rain_threshold_mm=0.1, rain_radius_m=5000.0, rain_window_min=15.0, include_non_operational=False,
        ))
        cov = [r for r in read_csv(out_metrics / "capture_coverage.csv") if r["source"] == "combinado"][0]
        self.assertEqual(cov["telemetry_available"], "0")
        self.assertEqual(cov["coverage_status"], "sin_telemetria")

        block_b = list(read_csv(out_metrics / "metrics_block_b_headways.csv"))
        self.assertGreater(len(block_b), 0)
        for row in block_b:
            self.assertEqual(row["telemetry_available"], "0")
            self.assertEqual(row["valid_trips"], "")
            self.assertEqual(row["crossing_count"], "")
            self.assertEqual(row["observed_departures_per_hour"], "")

    def test_privacy_excludes_plates_and_credentials_with_mock_croma(self):
        secret_key = "croma_test_secret_key_never_persist"
        plate_1 = "WNY123"
        plate_2 = "XYZ789"
        now_iso = datetime.now(UTC).isoformat()
        seen_auth = []

        class FakeResponse:
            def __init__(self, body_bytes):
                self.status = 200
                self.headers = {"X-RateLimit-Remaining": "500"}
                self._body = body_bytes

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, req, timeout=10):
                seen_auth.append(req.get_header("Authorization"))
                body = json.loads(req.data.decode("utf-8"))
                page = body.get("page", 1)
                vehicles = [
                    dict(vehicle_id="BUS-001", label=plate_1, license_plate=plate_1,
                         latitude=4.62, longitude=-74.08, route_id="R_E42", route_code="E42",
                         trip_id="T_E42_A", trip_start_time="07:00:00", stop_id="71101",
                         stop_status="stopped_at", reported_at=now_iso),
                    dict(vehicle_id=plate_2, label=plate_2, license_plate=plate_2,
                         latitude=4.63, longitude=-74.08, route_id="R_E42", route_code="E42",
                         trip_id="T_E42_A", trip_start_time="07:05:00", stop_id="71071",
                         stop_status="in_transit_to", reported_at=now_iso),
                ] if page == 1 else []
                payload = json.dumps({"data": {
                    "as_of": now_iso, "live": True, "page": page, "per_page": 100,
                    "total_pages": 1, "count": len(vehicles), "vehicles": vehicles,
                }}).encode("utf-8")
                return FakeResponse(payload)

        old_key = os.environ.get("CROMA_API_KEY")
        os.environ["CROMA_API_KEY"] = secret_key
        try:
            with patch("nqs.live.build_opener", return_value=FakeOpener()):
                session_dir = capture(SimpleNamespace(
                    source="croma",
                    out=self.root / "raw" / "live_mock",
                    catalog=self.catalog_dir,
                    duration=0.0,
                    interval=15.0,
                    max_requests=5,
                    freshness_s=90.0,
                    route=["E42"],
                    latitude=None,
                    longitude=None,
                    radius_m=500,
                    per_page=100,
                    max_pages=2,
                    vehicle_url="https://example.invalid/unused",
                    croma_url="https://api.croma.run/co/transmilenio/vehicles/v1",
                    timeout=5.0,
                ))
        finally:
            if old_key is None:
                os.environ.pop("CROMA_API_KEY", None)
            else:
                os.environ["CROMA_API_KEY"] = old_key

        self.assertEqual(seen_auth, [f"Bearer {secret_key}"])
        for path in session_dir.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(secret_key, text, f"Credencial filtrada en {path.name}")
            self.assertNotIn(plate_1, text, f"Placa filtrada en {path.name}")
            self.assertNotIn(plate_2, text, f"Placa filtrada en {path.name}")
            self.assertNotIn("BUS-001", text, f"vehicle_id sin seudonimizar en {path.name}")

        rows = list(read_csv(session_dir / "gps.csv"))
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["vehicle_id"], "")
        # Cuando vehicle_id coincide con license_plate, se vacía para no persistir la placa.
        self.assertEqual(rows[1]["vehicle_id"], "")

    def test_validations_import_and_provenance(self):
        val_in = self.root / "raw" / "validaciones_muestra.csv"
        val_in.parent.mkdir(parents=True, exist_ok=True)
        write_csv(
            val_in,
            "Fase Linea Estacion Acceso Intervalo Fecha Validaciones",
            [
                {"Fase": "Fase II", "Linea": "Zona E NQS Central", "Estacion": "(07110) Paloquemao",
                 "Acceso": "Acceso Norte", "Intervalo": "07:00:00", "Fecha": "2026-11-24", "Validaciones": "320"},
                {"Fase": "Fase II", "Linea": "Zona E NQS Central", "Estacion": "(07110) Paloquemao",
                 "Acceso": "Acceso Sur", "Intervalo": "07:00:00", "Fecha": "2026-11-24", "Validaciones": "180"},
                # Validación a bordo de ruta dual (sin "Zona "): debe excluirse.
                {"Fase": "Dual", "Linea": "M84", "Estacion": "(07110) Paloquemao",
                 "Acceso": "Bus", "Intervalo": "07:00:00", "Fecha": "2026-11-24", "Validaciones": "45"},
                # Fuera de franja 07:00-08:00: debe excluirse.
                {"Fase": "Fase II", "Linea": "Zona E NQS Central", "Estacion": "(07110) Paloquemao",
                 "Acceso": "Acceso Norte", "Intervalo": "08:15:00", "Fecha": "2026-11-24", "Validaciones": "200"},
            ],
        )
        out_val = self.root / "derived" / "validaciones" / "val.csv"
        count = validations(SimpleNamespace(
            input=str(val_in),
            catalog=self.catalog_dir,
            out=out_val,
            raw_dir=self.root / "raw" / "insumos",
            source="https://validaciones.datosabiertos.transmilenio.gov.co/validaciones_mensuales.html",
            sheet=None,
            encoding="utf-8-sig",
            date=[date(2026, 11, 24)],
            start=clock(7, 0),
            end=clock(8, 0),
            corridor_only=True,
        ))
        self.assertEqual(count, 1)
        rows = list(read_csv(out_val))
        self.assertEqual(rows[0]["gtfs_station_id"], "7110")
        self.assertEqual(rows[0]["validations"], "500")
        self.assertEqual(rows[0]["accesses"], "2")
        self.assertTrue(len(rows[0]["source_sha256"]) == 64)

    def test_real_catalog_block_a_traceability(self):
        real_block_a = Path("datos_nqs/derived/catalog/block_a.csv")
        if not real_block_a.exists():
            self.skipTest("Catálogo real aún no generado en datos_nqs/derived/catalog")
        rows = list(read_csv(real_block_a))
        found = {(r["block_a"], r["leg"], r["expected_code"]) for r in rows if r["result"] == "catalogado"}
        expected = {
            ("4", "ida", "4"), ("4", "regreso", "4"),
            ("B12", "ida", "B12"), ("B12", "regreso", "G12"),
            ("E42", "ida", "E42"), ("E42", "regreso", "S42"),
            ("M47", "ida", "M47"), ("M47", "regreso", "G47"),
        }
        self.assertTrue(expected.issubset(found), f"Faltan servicios Bloque A: {expected - found}")


if __name__ == "__main__":
    unittest.main()
