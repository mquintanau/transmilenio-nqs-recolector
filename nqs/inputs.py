"""Entradas de validaciones y archivo de lecturas meteorológicas del SAB."""
import csv
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, time as clock, timedelta

from openpyxl import load_workbook

from .catalog import normalize
from .common import BOGOTA, coordinates, csv_writer, file_hash, get_input, now, number, read_csv
from .common import timestamp, write_csv, write_json
from .live import Requests

RAIN_FIELDS = "captured_at measured_at sensor_id station longitude latitude rain_mm sensor_status source"
VALIDATION_FIELDS = "date window_from window_to station_code station_name gtfs_station_id validations accesses source source_sheet source_sha256"


def rain_rows(data, captured, source):
    sensors = json.loads(data)["TipoSensores"]
    if not isinstance(sensors, list):
        raise ValueError("Formato de sensores inválido")
    for sensor in sensors:
        measured = timestamp(sensor.get("FECHALECTURA"), BOGOTA)
        xy = coordinates(sensor.get("LONGITUD"), sensor.get("LATITUD"))
        mm = number(sensor.get("VALORLECTURA"))
        if measured and xy and sensor.get("IDSENSOR") is not None:
            yield dict(captured_at=captured.isoformat(), measured_at=measured.isoformat(),
                       sensor_id=sensor["IDSENSOR"], station=sensor.get("ESTACION"),
                       longitude=xy[0], latitude=xy[1], rain_mm=mm if mm is not None and mm >= 0 else None,
                       sensor_status=sensor.get("ESTADO"), source=source)


def collect_rain(args):
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    polls = out.with_name(out.stem + "_polls.csv")
    start = time.monotonic()
    deadline = start + args.duration if args.duration else None
    http = Requests(args.max_requests, args.timeout, deadline)
    seen = set()
    with (out.open("a+", newline="", encoding="utf-8") as raw,
          polls.open("a+", newline="", encoding="utf-8") as log):
        raw.seek(0)
        for row in csv.DictReader(raw):
            seen.add((row["sensor_id"], row["measured_at"], row["rain_mm"], row["sensor_status"]))
        writer = csv.DictWriter(raw, RAIN_FIELDS.split()) if raw.tell() else csv_writer(raw, RAIN_FIELDS)
        poll_fields = "captured_at status readings new_readings source"
        logger = csv.DictWriter(log, poll_fields.split()) if log.tell() else csv_writer(log, poll_fields)
        slot = start
        try:
            while (http.count == 0 or deadline is not None and slot < deadline) and (
                    args.max_requests is None or http.count < args.max_requests):
                if slot > time.monotonic():
                    time.sleep(max(0, slot - time.monotonic()))
                status, body = http.fetch(args.url, "idiger", "lluvia")
                captured = now()
                rows = []
                if status == "ok":
                    try:
                        rows = list(rain_rows(body, captured, args.url))
                        if not rows:
                            status = "sin_lecturas"
                    except (AttributeError, KeyError, TypeError, ValueError):
                        status = "respuesta_invalida"
                added = 0
                for row in rows:
                    key = tuple(str(row[k]) if row[k] is not None else "" for k in (
                        "sensor_id", "measured_at", "rain_mm", "sensor_status"))
                    if key not in seen:
                        writer.writerow(row)
                        seen.add(key)
                        added += 1
                logger.writerow(dict(captured_at=captured.isoformat(), status=status,
                                     readings=len(rows), new_readings=added, source=args.url))
                raw.flush()
                log.flush()
                if status == "fin_sesion":
                    break
                slot = start + (int((time.monotonic() - start) // args.interval) + 1) * args.interval
        except KeyboardInterrupt:
            pass
    return out


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def as_clock(value):
    if isinstance(value, clock):
        return value
    try:
        return clock.fromisoformat(str(value))
    except ValueError:
        return None


def column(value):
    return normalize(value).strip().replace("_", " ")


def station_label(value):
    match = re.match(r"^\((\d+)\)\s*(.+)$", str(value).strip())
    return (match[1], match[2]) if match else ("", str(value).strip())


def validation_records(rows, since, until, days):
    """Emite filas tanto de la tabla mensual ancha como de un CSV largo."""
    for row in rows:
        names = {column(value): index for index, value in enumerate(row) if value is not None}
        if "estacion" in names and ("intervalo" in names or "hora" in names):
            break
    else:
        raise ValueError("Se requieren las columnas Estación e Intervalo en la tabla de validaciones")
    date_columns = [(i, day) for i, value in enumerate(row) if (day := as_date(value)) and (not days or day in days)]
    wide = any(as_date(value) for value in row)
    if not wide and not {"fecha", "validaciones"}.issubset(names):
        raise ValueError("El archivo requiere columnas por fecha o Fecha y Validaciones")
    for row in rows:
        def field(*keys):
            for key in keys:
                index = names.get(key)
                if index is not None and index < len(row):
                    return row[index]
            return None

        station = field("estacion")
        slot = as_clock(field("intervalo", "hora"))
        if not station or not slot or not since <= slot < until:
            continue
        code, label = station_label(station)
        access = (field("acceso de estacion", "acceso") or "").strip()
        phase, line = field("fase"), field("linea")
        # La planilla también contiene validaciones efectuadas a bordo de rutas
        # duales. Las filas Dual con zona y acceso de estación sí son entradas.
        if normalize(phase) == "dual" and "zona " not in normalize(line):
            continue
        values = ((day, row[i]) for i, day in date_columns if i < len(row)) if wide else (
            (as_date(field("fecha")), field("validaciones")),)
        for day, count in values:
            count = number(count)
            if day and (not days or day in days) and count is not None and count >= 0 and count.is_integer():
                yield day, slot, code, label, access, str(phase or ""), str(line or ""), int(count)


def validations(args):
    cache = getattr(args, "raw_dir", None) or (args.out.parent.parent / "raw" / "insumos")
    input_path = get_input(args.input, cache)
    source = args.source or str(args.input)
    sha = file_hash(input_path)
    days = set(args.date)
    station_mapping = {}
    if args.catalog:
        for row in read_csv(args.catalog / "stations.csv"):
            code = row["station_id"]
            station_mapping[code.lstrip("0") if code.isdigit() else code] = code
    grouped = defaultdict(lambda: [0, set()])
    scope_codes = set(station_mapping) if args.corridor_only else None
    source_sheet = args.sheet or ""
    if str(args.input).lower().split("?")[0].endswith(".xlsx"):
        workbook = load_workbook(input_path, read_only=True, data_only=True)
        try:
            source_sheet = args.sheet or ("Validaciones Tullave" if "Validaciones Tullave" in workbook.sheetnames
                                           else workbook.sheetnames[0])
            for record in validation_records(workbook[source_sheet].values, args.start, args.end, days):
                add_validation(grouped, scope_codes, record)
        finally:
            workbook.close()
    else:
        with input_path.open(encoding=args.encoding, newline="") as stream:
            text = stream.read(8192)
            stream.seek(0)
            try:
                dialect = csv.Sniffer().sniff(text, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            for record in validation_records(csv.reader(stream, dialect), args.start, args.end, days):
                add_validation(grouped, scope_codes, record)
    rows = []
    for (day, slot, code, label), (total, accesses) in sorted(grouped.items()):
        start = datetime.combine(day, slot, BOGOTA)
        rows.append(dict(date=day, window_from=start.isoformat(), window_to=(start + timedelta(minutes=15)).isoformat(),
                         station_code=code, station_name=label, gtfs_station_id=station_mapping.get(code.lstrip("0")),
                         validations=total, accesses=len(accesses), source=source, source_sheet=source_sheet,
                         source_sha256=sha))
    write_csv(args.out, VALIDATION_FIELDS, rows)
    write_json(args.out.with_suffix(".json"),
               dict(source=source, sha256=sha, sheet=source_sheet, rows=len(rows), timezone=BOGOTA.key,
                    dates=sorted(days), start=args.start, end=args.end, corridor_only=args.corridor_only))
    return len(rows)


def add_validation(grouped, scope_codes, record):
    day, slot, code, label, access, phase, line, count = record
    if scope_codes is None or code.lstrip("0") in scope_codes:
        key = day, slot, code, label
        grouped[key][0] += count
        grouped[key][1].add(access)
