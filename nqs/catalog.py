"""Catálogo geométrico de recorridos, estaciones y viajes GTFS."""
import csv
import io
import json
import re
import unicodedata
from collections import defaultdict
from zipfile import ZipFile

import numpy as np
import shapely as geo
from pyproj import Transformer
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import substring

from .common import digest, file_hash, get_input, write_csv, write_json

TO_METRES = Transformer.from_crs(4326, 32618, always_xy=True)
TO_LONLAT = Transformer.from_crs(32618, 4326, always_xy=True)
BLOCK_A = {"4": "4", "B12": "G12", "E42": "S42", "M47": "G47"}
PATTERNS = "pattern_id route_id route_code destination direction_id direction system operational shape_ids trip_count overlap_m max_continuous_m corridor_sections first_stop last_stop"
CONTROLS = "pattern_id span control_order station_id station_name position_m x y stop_status platforms pickup dropoff corridor_section control_source"
TRIPS = "trip_id pattern_id route_id service_id shape_id"
STATIONS = "station_id station_name longitude latitude corridor_section distance_m"
PATTERN_STOPS = "pattern_id stop_order platform_id station_id station_name pickup_type drop_off_type"


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFD", str(value).casefold())
                   if not unicodedata.combining(c))


def route_system(route, agencies, trunk_ids):
    agency = route.get("agency_id", "")
    if agency in trunk_ids:
        return "troncal"
    name = normalize(agencies.get(agency, ""))
    if "alimentador" in name:
        return "alimentador"
    if "dual" in name:
        return "dual"
    if "zonal" in name:
        return "zonal"
    if "cable" in name:
        return "cable"
    return "desconocido"


def gtfs_rows(archive, name, required=True):
    found = [member for member in archive.namelist() if member.split("/")[-1] == name + ".txt"]
    if not found and not required:
        return
    if len(found) != 1:
        raise ValueError(f"Se esperaba un archivo {name}.txt dentro del GTFS")
    with archive.open(found[0]) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def load_corridor(path):
    features = json.loads(path.read_bytes())["features"]
    parts = {}
    records = []
    for feature in features:
        props = {k.lower(): v for k, v in feature["properties"].items()}
        zone = str(props.get("le_troncal", "")).strip().upper()
        if zone not in ("E", "G"):
            continue
        section = props["id_trazado"]
        line = geo.transform(shape(feature["geometry"]), TO_METRES.transform, interleaved=False)
        if line.geom_type not in ("LineString", "MultiLineString") or line.is_empty:
            raise ValueError(f"Geometría inválida en el trazado {section}")
        parts[section] = geo.union_all([parts[section], line]) if section in parts else line
        records.append({"type": "Feature", "properties": feature["properties"] | {"length_utm_m": round(line.length, 2)},
                        "geometry": feature["geometry"]})
    if not parts:
        raise ValueError("El GeoJSON no contiene tramos oficiales E o G")
    return parts, records


def intervals(line, envelope):
    """Devuelve intervalos en el eje de la ruta, incluso cuando la ruta se cruza a sí misma."""
    if not line.intersects(envelope):
        return []
    points = np.asarray(line.coords)
    segments = geo.linestrings(np.stack((points[:-1], points[1:]), axis=1))
    lengths = geo.length(segments)
    offsets = np.r_[0, lengths.cumsum()[:-1]]
    hits = np.flatnonzero((lengths > 0) & geo.intersects(segments, envelope))
    clipped = geo.intersection(segments[hits], envelope)
    result = []
    for index, intersection in zip(hits, clipped, strict=True):
        clipped_intervals = []
        for part in geo.get_parts(intersection):
            if part.geom_type != "LineString" or part.length == 0:
                continue
            ends = sorted(segments[index].project(Point(xy)) + offsets[index]
                          for xy in (part.coords[0], part.coords[-1]))
            clipped_intervals.append(ends)
        for ends in sorted(clipped_intervals):
            if result and ends[0] <= result[-1][1] + 1e-5:
                result[-1][1] = max(result[-1][1], ends[1])
            else:
                result.append(ends)
    return result


def station_for(stop_id, stops):
    visited = {stop_id}
    while (parent := stops.get(stop_id, {}).get("parent_station")) in stops and parent not in visited:
        stop_id = parent
        visited.add(stop_id)
    return stop_id


def station_inventory(stops, station_ids, sections, station_m):
    inventory = {}
    for station_id in sorted(station_ids):
        stop = stops[station_id]
        if not stop.get("stop_lon") or not stop.get("stop_lat"):
            continue
        lon, lat = float(stop["stop_lon"]), float(stop["stop_lat"])
        point = Point(TO_METRES.transform(lon, lat))
        section = min(sections, key=lambda key: sections[key].distance(point))
        distance = sections[section].distance(point)
        portal = station_id == "7000" and "portal sur" in normalize(stop["stop_name"]) and "TZ010" in sections
        if distance <= station_m or portal:
            inventory[station_id] = dict(station_id=station_id, station_name=stop["stop_name"],
                                         longitude=lon, latitude=lat, corridor_section=section,
                                         distance_m=round(distance, 2))
    return inventory


def measurement_spans(spans, min_overlap, link_gap):
    linked = []
    for start, end in spans:
        if linked and start - linked[-1][1] <= link_gap:
            linked[-1][1] = end
        else:
            linked.append([start, end])
    return [(start, end) for start, end in linked if end - start >= min_overlap]


def trip_extent(line, sequence, stops, station_m):
    positions = []
    for index in (0, -1):
        stop = stops.get(sequence[index][0], {})
        if stop.get("stop_lon") and stop.get("stop_lat"):
            point = Point(TO_METRES.transform(float(stop["stop_lon"]), float(stop["stop_lat"])))
            if line.distance(point) <= station_m:
                if index == -1 and not line.is_simple:
                    positions.append(line.length - geo.reverse(line).project(point))
                else:
                    positions.append(line.project(point))
                continue
        positions.append(0.0 if index == 0 else line.length)
    if positions[1] <= positions[0]:
        return 0.0, line.length
    return tuple(positions)


def clip_spans(spans, start, end):
    return [(max(a, start), min(b, end)) for a, b in spans if a < end and start < b]


def pattern_controls(pattern_id, line, spans, sequence, stations, stops, sections, station_m):
    platforms_by_station = defaultdict(list)
    for i, (stop_id, pickup, dropoff) in enumerate(sequence):
        platforms_by_station[station_for(stop_id, stops)].append((stop_id, pickup, dropoff, i))
    points = {}
    for station_id, station in stations.items():
        point = Point(TO_METRES.transform(station["longitude"], station["latitude"]))
        platforms = platforms_by_station[station_id]
        sources = [(point, "estacion")]
        for stop_id, _, _, _ in platforms:
            stop = stops[stop_id]
            if stop.get("stop_lon") and stop.get("stop_lat"):
                platform = Point(TO_METRES.transform(float(stop["stop_lon"]), float(stop["stop_lat"])))
                sources.append((platform, "plataforma"))
        points[station_id] = sources, platforms
    portal_links = set()
    # El acceso al Portal Sur se aparta del eje oficial. Solo se amplía el tramo
    # de una variante que atiende el Portal, por su propio shape GTFS.
    if "7000" in points and points["7000"][1] and spans:
        sources, _ = points["7000"]
        point, _ = min(sources, key=lambda item: (item[1] != "plataforma", line.distance(item[0])))
        position = line.project(point)
        index = min(range(len(spans)), key=lambda i: min(abs(position - end) for end in spans[i]))
        start, end = spans[index]
        if min(abs(position - start), abs(position - end)) <= 500 and line.distance(point) <= station_m:
            spans[index] = [min(start, position), max(end, position)]
            portal_links.add(index)
    for index, (start, end) in enumerate(spans):
        segment = substring(line, start, end)
        controls = []
        for station_id, (sources, platforms) in points.items():
            station = stations[station_id]
            point, control_source = min(sources, key=lambda item: (item[1] != "plataforma"
                                        or segment.distance(item[0]) > station_m, segment.distance(item[0])))
            if segment.distance(point) > station_m:
                continue
            pos = start + segment.project(point)
            projected = line.interpolate(pos)
            pickup = any(p == "0" and i < len(sequence) - 1 for _, p, _, i in platforms)
            dropoff = any(d == "0" and i > 0 for _, _, d, i in platforms)
            scheduled = any(p != "1" or d != "1" for _, p, d, _ in platforms)
            if station_id == "7000" and index in portal_links:
                control_source = "acceso_portal_gtfs"
            section = min(sections, key=lambda key: sections[key].distance(projected))
            controls.append(dict(pattern_id=pattern_id, span=index + 1, station_id=station_id,
                                 station_name=station["station_name"], position_m=round(pos, 3),
                                 x=round(projected.x, 3), y=round(projected.y, 3),
                                 stop_status="parada" if scheduled else "paso",
                                 platforms="|".join(dict.fromkeys(stop for stop, _, _, _ in platforms)),
                                 pickup=int(pickup), dropoff=int(dropoff), corridor_section=section,
                                 control_source=control_source))
        for order, control in enumerate(sorted(controls, key=lambda row: (row["position_m"], row["station_id"])), 1):
            yield dict(control, control_order=order)


def get_direction(line, spans):
    start, end = max(spans, key=lambda span: span[1] - span[0])
    a, b = line.interpolate(start), line.interpolate(end)
    # El signo se obtiene del desplazamiento dominante sobre el corredor.
    if abs(b.y - a.y) >= abs(b.x - a.x) * 0.33:
        return "norte" if b.y > a.y else "sur"
    return "oriente" if b.x > a.x else "occidente"


def build_catalog(args):
    out = args.out
    cache = getattr(args, "raw_dir", None) or (out.parent.parent / "raw" / "insumos")
    gtfs = get_input(args.gtfs, cache)
    corridor = get_input(args.corridor, cache)
    sections, records = load_corridor(corridor)
    buffers = {key: line.buffer(args.tolerance_m) for key, line in sections.items()}
    envelope = geo.union_all(list(buffers.values()))
    with ZipFile(gtfs) as archive:
        feed = next(gtfs_rows(archive, "feed_info", False), {})
        routes = {row["route_id"]: row for row in gtfs_rows(archive, "routes")}
        agencies = {row.get("agency_id", ""): row["agency_name"] for row in gtfs_rows(archive, "agency", False)}
        trunk_ids = set(args.trunk_agency or (key for key, name in agencies.items()
                        if "troncal" in normalize(name) and "dual" not in normalize(name)))
        if not trunk_ids:
            raise ValueError("El GTFS no declara una agencia troncal reconocible. Indíquela con --trunk-agency.")
        trips = {row["trip_id"]: row for row in gtfs_rows(archive, "trips")}
        stops = {row["stop_id"]: row for row in gtfs_rows(archive, "stops")}
        points = defaultdict(list)
        for row in gtfs_rows(archive, "shapes"):
            points[row["shape_id"]].append((int(row["shape_pt_sequence"]), float(row["shape_pt_lon"]),
                                            float(row["shape_pt_lat"])))
        shapes = {}
        shape_audit = {}
        for shape_id, vertices in points.items():
            if len(vertices) < 2:
                continue
            _, lon, lat = np.asarray(sorted(vertices)).T
            x, y = TO_METRES.transform(lon, lat)
            line = LineString(zip(x, y, strict=True))
            spans = intervals(line, envelope)
            max_m = max((b - a for a, b in spans), default=0.0)
            shape_audit[shape_id] = max_m
            if max_m >= args.min_overlap_m:
                detail = {section: intervals(line, buffer) for section, buffer in buffers.items()}
                shapes[shape_id] = line, spans, detail
        points.clear()
        candidate = {key: row for key, row in trips.items() if row.get("shape_id") in shapes}
        stop_times = defaultdict(list)
        trunk_stations = {station_for(stop_id, stops) for stop_id, row in stops.items()
                          if row.get("location_type") == "1"}
        # Lectura secuencial del archivo más grande. Conserva solo los viajes candidatos.
        for row in gtfs_rows(archive, "stop_times"):
            trip_id = row["trip_id"]
            trip = candidate.get(trip_id)
            if trip is None:
                continue
            stop_id = row["stop_id"]
            pickup, dropoff = row.get("pickup_type") or "0", row.get("drop_off_type") or "0"
            stop_times[trip_id].append((int(row["stop_sequence"]), stop_id, pickup, dropoff))
            if stop_id in stops and routes[trip["route_id"]].get("agency_id") in trunk_ids:
                trunk_stations.add(station_for(stop_id, stops))
    stations = station_inventory(stops, trunk_stations, sections, args.station_m)
    groups = {}
    audit = {}
    geometry_ids = {shape_id: digest(data[0].wkb) for shape_id, data in shapes.items()}
    for trip_id, trip in trips.items():
        route_id, shape_id = trip["route_id"], trip.get("shape_id", "")
        audit.setdefault((route_id, shape_id), [0, 0])[0] += 1
        if trip_id not in candidate or trip_id not in stop_times:
            continue
        seq = tuple(tuple(row[1:]) for row in sorted(stop_times[trip_id]))
        direction = trip.get("direction_id", "")
        geometry_id = geometry_ids[shape_id]
        key = route_id, direction, geometry_id, seq
        if key not in groups:
            pattern_id = digest(json.dumps(key, ensure_ascii=False).encode())[:20]
            groups[key] = (pattern_id, [])
        groups[key][1].append(trip)
    patterns, controls, pattern_stops, trip_links, geometries = [], [], [], [], []
    station_status = []
    for (route_id, direction, _, seq), (pattern_id, members) in sorted(groups.items()):
        route = routes[route_id]
        system = route_system(route, agencies, trunk_ids)
        shape_ids = sorted({t["shape_id"] for t in members})
        line, full_spans, detail = shapes[shape_ids[0]]
        start, end = trip_extent(line, seq, stops, args.station_m)
        spans = clip_spans(full_spans, start, end)
        max_m = max((b - a for a, b in spans), default=0)
        if max_m < args.min_overlap_m:
            continue
        for trip in members:
            audit[route_id, trip["shape_id"]][1] += 1
        section_m = {key: sum(b-a for a,b in clip_spans(values, start, end)) for key, values in detail.items()}
        section_m = {key: round(value, 2) for key, value in section_m.items() if value > 0}
        control_spans = measurement_spans(spans, args.min_overlap_m, args.link_gap_m)
        rows = list(pattern_controls(pattern_id, line, control_spans, seq, stations, stops, sections, args.station_m))
        station_controls = defaultdict(list)
        for row in rows:
            station_controls[row["station_id"]].append(row)
        scheduled = {station_for(stop_id, stops) for stop_id, pick, drop in seq if pick != "1" or drop != "1"}
        for station_id in stations:
            hits = station_controls[station_id]
            status = "parada" if station_id in scheduled else "paso" if hits else "fuera"
            station_status.append(dict(pattern_id=pattern_id, station_id=station_id,
                                       status=status, controls=len(hits)))
        facility_access = any(row["stop_status"] == "parada" and
                              stops[row["station_id"]].get("location_type") == "1" for row in rows)
        operational = system == "troncal" or (system == "dual" and facility_access)
        first, last = (stops.get(station_for(s[0], stops), {}).get("stop_name", s[0])
                       for s in (seq[0], seq[-1]))
        patterns.append(dict(pattern_id=pattern_id, route_id=route_id, route_code=route["route_short_name"],
                             destination=route["route_long_name"], direction_id=direction,
                             direction=get_direction(line, control_spans), system=system,
                             operational=int(operational), shape_ids="|".join(shape_ids),
                             trip_count=len(members), overlap_m=round(sum(b-a for a,b in spans), 2),
                             max_continuous_m=round(max_m, 2),
                             corridor_sections="|".join(sorted(section_m)), first_stop=first, last_stop=last))
        geometries.append(dict(type="Feature", properties=dict(pattern_id=pattern_id, overlap_by_section_m=section_m,
                                                                 trip_extent_m=[start, end], spans_m=spans,
                                                                 control_spans_m=control_spans),
                               geometry=mapping(geo.transform(line, TO_LONLAT.transform, interleaved=False))))
        controls.extend(rows)
        trip_links.extend({key: t.get(key, "") for key in TRIPS.split() if key != "pattern_id"}
                          | {"pattern_id": pattern_id} for t in members)
        for order, (platform, pickup, dropoff) in enumerate(seq, 1):
            station_id = station_for(platform, stops)
            pattern_stops.append(dict(pattern_id=pattern_id, stop_order=order, platform_id=platform,
                                      station_id=station_id, station_name=stops.get(station_id, {}).get("stop_name", ""),
                                      pickup_type=pickup, drop_off_type=dropoff))
    gtfs_hash, corridor_hash = file_hash(gtfs), file_hash(corridor)
    settings = dict(schema=1, crs="EPSG:32618", tolerance_m=args.tolerance_m, min_overlap_m=args.min_overlap_m,
                    station_m=args.station_m, link_gap_m=args.link_gap_m, trunk_agencies=sorted(trunk_ids))
    catalog_id = digest(json.dumps([gtfs_hash, corridor_hash, settings], sort_keys=True).encode())
    write_csv(out / "patterns.csv", PATTERNS, patterns)
    write_csv(out / "pattern_stops.csv", PATTERN_STOPS, pattern_stops)
    write_csv(out / "pattern_stations.csv", "pattern_id station_id status controls", station_status)
    write_csv(out / "pattern_controls.csv", CONTROLS, controls)
    write_csv(out / "trips.csv", TRIPS, trip_links)
    write_csv(out / "stations.csv", STATIONS, stations.values())
    write_csv(out / "shape_audit.csv", "route_id route_code system shape_id trips catalogued_trips max_continuous_m",
              (dict(route_id=r, route_code=routes[r]["route_short_name"], system=route_system(routes[r], agencies, trunk_ids),
                    shape_id=s, trips=counts[0], catalogued_trips=counts[1], max_continuous_m=round(shape_audit[s], 2)
                    if s in shape_audit else "") for (r, s), counts in sorted(audit.items())))
    write_json(out / "corridor.geojson", dict(type="FeatureCollection", features=records))
    (out / "patterns.geojson").write_text(json.dumps(
        dict(type="FeatureCollection", features=geometries), separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_comparison(out, routes, patterns)
    date_version = re.search(r"GTFS_(\d{8})", str(args.gtfs))
    version = args.version or (date_version.group(1) if date_version else feed.get("feed_version") or gtfs.name)
    write_json(out / "catalog.json", dict(catalog_id=catalog_id, gtfs_source=args.gtfs, gtfs_sha256=gtfs_hash,
               gtfs_version=version,
               feed_info={k: feed.get(k) for k in ("feed_version", "feed_start_date", "feed_end_date")},
               corridor_source=args.corridor, corridor_sha256=corridor_hash, settings=settings,
               pattern_count=len(patterns), operational_count=sum(p["operational"] for p in patterns)))
    return len(patterns)


def write_comparison(out, routes, patterns):
    fields = "block_a leg expected_code route_id destination pattern_id trip_count result"
    rows = []
    for service, return_code in BLOCK_A.items():
        for leg, code in (("ida", service), ("regreso", return_code)):
            catalogued = False
            for route_id, route in routes.items():
                if route["route_short_name"] != code:
                    continue
                north_leg = "heroe" in normalize(route["route_long_name"])
                if code == "4" and north_leg != (leg == "ida"):
                    continue
                catalogued = True
                matches = [p for p in patterns if p["route_id"] == route_id and p["operational"]]
                common = dict(block_a=service, leg=leg, expected_code=code, route_id=route_id,
                              destination=route["route_long_name"])
                rows.extend(dict(common, pattern_id=p["pattern_id"], trip_count=p["trip_count"], result="catalogado")
                            for p in matches)
                if not matches:
                    rows.append(dict(common, result="sin_patron_nqs"))
            if not catalogued:
                rows.append(dict(block_a=service, leg=leg, expected_code=code, result="codigo_ausente"))
    write_csv(out / "block_a.csv", fields, rows)
