"""Captura GTFS Realtime y Croma con presupuesto por sesión."""
import json
import os
import time
import uuid
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as pb

from .common import CORRIDOR, CROMA, GTFS, RAIN, RT, VALIDATIONS
from .common import coordinates, csv_writer, digest, now, timestamp, write_json

GPS = "sample_id source captured_at gps_at feed_at vehicle_id route_id route_code trip_id trip_start_date trip_start_time longitude latitude stop_id stop_status source_live"
POLLS = "sample_id slot_from slot_to captured_at source status requests received fresh scope_route scope_lon scope_lat scope_radius_m"
REQUESTS = "sample_id source endpoint requested_at http_status outcome credits_remaining credit_reset retry_after cache"


class NoRedirect(HTTPRedirectHandler):
    """Evita reenviar la autorización de Croma a un destino redirigido."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_seconds(value):
    try:
        return max(1, float(value))
    except (TypeError, ValueError):
        try:
            return max(1, (parsedate_to_datetime(value) - now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 60


class Requests:
    def __init__(self, limit, timeout=10, deadline=None, log=None):
        self.network = build_opener(NoRedirect())
        self.limit = limit
        self.timeout = timeout
        self.deadline = deadline
        self.log = log or (lambda row: None)
        self.count = 0
        self.croma_unavailable = None
        self.resume = 0

    def fetch(self, url, source, endpoint, sample_id="", payload=None, method=None):
        if self.limit is not None and self.count >= self.limit:
            return "presupuesto", None
        if source == "croma":
            key = os.environ.get("CROMA_API_KEY")
            if not key:
                return "sin_clave", None
            if self.croma_unavailable:
                return self.croma_unavailable, None
            if time.monotonic() < self.resume:
                return "espera_429", None
        timeout = self.timeout
        if self.deadline is not None and self.count > 0:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                return "fin_sesion", None
        headers = {"User-Agent": "nqs-piloto/1", "Accept": "*/*"}
        if source == "croma":
            headers.update({"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        request = Request(url, data=None if payload is None else json.dumps(payload).encode(),
                          headers=headers, method=method)
        requested = now()
        self.count += 1
        status, reply_headers, data = None, {}, None
        try:
            with self.network.open(request, timeout=timeout) as response:
                status, reply_headers = response.status, response.headers
                data = response.read()
            outcome = "ok"
        except HTTPError as exc:
            status, reply_headers = exc.code, exc.headers
            outcome = f"http_{status}"
            exc.close()
        except (URLError, OSError, ValueError):
            outcome = "red_o_timeout"
        credit, reset, retry, cache = (reply_headers.get(h, "") for h in (
            "X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After", "X-Cache"))
        if source == "croma":
            if status in (401, 402, 403):
                self.croma_unavailable = outcome
            elif status == 429:
                delay = retry_seconds(retry)
                if not retry and (reset_at := timestamp(reset)) is not None:
                    delay = max(delay, (reset_at - now()).total_seconds())
                self.resume = time.monotonic() + delay
            elif credit.isdigit() and int(credit) < 10:
                self.croma_unavailable = "creditos_insuficientes"
        self.log(dict(sample_id=sample_id, source=source, endpoint=endpoint, requested_at=requested.isoformat(),
                      http_status=status, outcome=outcome, credits_remaining=credit, credit_reset=reset,
                      retry_after=retry, cache=cache))
        return outcome, data


def vehicle_row(source, sample_id, captured, feed_at, vehicle_id, plate, lon, lat, gps_at, **fields):
    # No se persisten placas, etiquetas ni identificadores crudos de vehículo.
    vehicle_id = str(vehicle_id or "").strip()
    plate = str(plate or "").strip()
    if plate and vehicle_id.casefold() == plate.casefold():
        vehicle_id = ""
    xy = coordinates(lon, lat)
    gps_at = timestamp(gps_at)
    return dict(sample_id=sample_id, source=source, captured_at=captured.isoformat(),
                gps_at=gps_at.isoformat() if gps_at else "", feed_at=feed_at,
                vehicle_id=digest(f"{source}\0{vehicle_id}".encode())[:24] if vehicle_id else "",
                longitude=xy[0] if xy else "", latitude=xy[1] if xy else "", **fields)


def quality(row, freshness_s):
    observed, captured = timestamp(row.get("gps_at")), timestamp(row.get("captured_at"))
    if not observed or not captured:
        return "sin_hora_gps", None
    age = (captured - observed).total_seconds()
    if age < 0:
        return "hora_futura", age
    if age > freshness_s:
        return "atrasado", age
    if coordinates(row.get("longitude"), row.get("latitude")) is None:
        return "sin_coordenadas", age
    if not row.get("vehicle_id"):
        return "sin_vehiculo", age
    return "fresco", age


def parse_feed(data):
    feed = pb.FeedMessage()
    feed.ParseFromString(data)
    if not feed.IsInitialized():
        raise ValueError("Feed GTFS Realtime incompleto")
    return feed


def official_sample(http, sample_id, url):
    status, data = http.fetch(url, "oficial", "vehicle", sample_id)
    if status != "ok":
        return status, []
    try:
        feed = parse_feed(data)
    except (DecodeError, ValueError):
        return "protobuf_invalido", []
    if feed.header.incrementality == pb.FeedHeader.DIFFERENTIAL:
        return "feed_diferencial", []
    captured = now()
    feed_at = timestamp(feed.header.timestamp if feed.header.HasField("timestamp") else None)
    rows = []
    for entity in feed.entity:
        if not entity.HasField("vehicle") or entity.is_deleted:
            continue
        v = entity.vehicle
        has_position = v.HasField("position")
        rows.append(vehicle_row("oficial", sample_id, captured, feed_at.isoformat() if feed_at else "",
                    v.vehicle.id, v.vehicle.license_plate, v.position.longitude if has_position else None,
                    v.position.latitude if has_position else None, v.timestamp if v.HasField("timestamp") else None,
                    route_id=v.trip.route_id, route_code="", trip_id=v.trip.trip_id,
                    trip_start_date=v.trip.start_date, trip_start_time=v.trip.start_time, stop_id=v.stop_id,
                    stop_status=pb.VehiclePosition.VehicleStopStatus.Name(v.current_status).lower()
                    if v.HasField("current_status") else "", source_live=""))
    return "ok", rows


def croma_endpoint(args=None):
    custom = getattr(args, "croma_url", None) if args is not None else None
    if custom:
        return custom
    base = os.environ.get("CROMA_API_URL")
    if base:
        return base.rstrip("/") + "/co/transmilenio/vehicles/v1"
    return CROMA


def croma_sample(http, sample_id, scope, per_page, max_pages, url=None):
    rows = []
    visited = set()
    endpoint_url = url or croma_endpoint()
    for page in range(1, max_pages + 1):
        status, body = http.fetch(endpoint_url, "croma", "vehicles", sample_id, scope | {"page": page, "per_page": per_page})
        if status != "ok":
            return ("parcial_" + status if rows else status), rows
        try:
            document = json.loads(body)
            data = document["data"]
            vehicles = data["vehicles"]
            if not isinstance(vehicles, list) or int(data["page"]) != page:
                raise ValueError()
            total_pages = int(data["total_pages"])
            if total_pages < 0 or len(vehicles) > per_page:
                raise ValueError()
            fingerprint = tuple((v.get("vehicle_id"), v.get("reported_at")) for v in vehicles)
            if fingerprint and fingerprint in visited:
                return "paginacion_repetida", rows
            visited.add(fingerprint)
        except (AttributeError, KeyError, ValueError, TypeError):
            return "respuesta_invalida", rows
        captured = now()
        rows.extend(vehicle_row("croma", sample_id, captured, data.get("as_of"), v.get("vehicle_id"),
                    v.get("license_plate"), v.get("longitude"), v.get("latitude"), v.get("reported_at"),
                    route_id=v.get("route_id"), route_code=v.get("route_code"), trip_id=v.get("trip_id"),
                    trip_start_date="", trip_start_time=v.get("trip_start_time"), stop_id=v.get("stop_id"),
                    stop_status=v.get("stop_status"), source_live=data.get("live")) for v in vehicles)
        if page >= total_pages or not vehicles:
            return "ok", rows
    return "limite_paginas", rows


def requested_routes(route_arg):
    if not route_arg:
        return [None]
    items = route_arg if isinstance(route_arg, (list, tuple)) else [route_arg]
    routes = []
    for item in items:
        for token in str(item).split(","):
            token = token.strip()
            if token and token not in routes:
                routes.append(token)
    return routes or [None]


def scope_for(args, route=None):
    params = {}
    if route is None:
        routes = requested_routes(args.route)
        route = routes[0] if len(routes) == 1 else None
    if route:
        params["route"] = route
    if args.latitude is not None:
        params |= dict(latitude=args.latitude, longitude=args.longitude, radius_m=args.radius_m)
    return params


def catalog_route_lookup(path, requested):
    if not requested or path is None:
        return set()
    from .common import read_csv

    return {row["route_id"] for row in read_csv(path / "patterns.csv")
            if requested in (row["route_id"], row["route_code"])}


def capture(args):
    started = now()
    out = args.out / (started.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True)
    routes = requested_routes(args.route)
    known_by_route = {r: catalog_route_lookup(args.catalog, r) for r in routes if r}
    endpoint_url = croma_endpoint(args)
    catalog_id = None
    if args.catalog is not None:
        catalog_id = json.loads((args.catalog / "catalog.json").read_text(encoding="utf-8"))["catalog_id"]
    manifest = dict(session_id=out.name, source=args.source, started_at=started.isoformat(),
                    routes=[r for r in routes if r],
                    geo_scope=dict(latitude=args.latitude, longitude=args.longitude, radius_m=args.radius_m)
                    if args.latitude is not None else {},
                    interval_s=args.interval, duration_s=args.duration, freshness_s=args.freshness_s,
                    request_limit=args.max_requests, croma_per_page=args.per_page, croma_max_pages=args.max_pages,
                    catalog_id=catalog_id)
    write_json(out / "session.json", manifest)
    start = time.monotonic()
    deadline = start + args.duration if args.duration else None
    with ((out / "gps.csv").open("w", newline="", encoding="utf-8") as gps,
          (out / "polls.csv").open("w", newline="", encoding="utf-8") as polls,
          (out / "requests.csv").open("w", newline="", encoding="utf-8") as requests):
        gps_writer, poll_writer, request_writer = (csv_writer(stream, fields) for stream, fields in
                                                  ((gps, GPS), (polls, POLLS), (requests, REQUESTS)))
        http = Requests(args.max_requests, args.timeout, deadline, request_writer.writerow)
        sample_number = 0
        slot = start
        try:
            while (sample_number == 0 or deadline is not None and slot < deadline) and (
                    args.max_requests is None or http.count < args.max_requests):
                delay = slot - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                if sample_number and deadline is not None and time.monotonic() >= deadline:
                    break
                sample_number += 1
                sample_id = f"{out.name}:{sample_number}"
                slot_from = started + timedelta(seconds=slot - start)
                slot_to = slot_from + timedelta(seconds=min(args.interval, deadline - slot) if deadline else 0)
                for source in ("oficial", "croma") if args.source == "auto" else (args.source,):
                    if source == "oficial":
                        before = http.count
                        status, all_rows = official_sample(http, sample_id, args.vehicle_url)
                        if routes != [None]:
                            allowed_ids = set().union(*known_by_route.values()) | set(routes)
                            allowed_codes = set(routes)
                            rows = [r for r in all_rows if r["route_id"] in allowed_ids
                                    or str(r.get("route_code") or "") in allowed_codes]
                        else:
                            rows = all_rows
                        gps_writer.writerows(rows)
                        fresh = sum(quality(row, args.freshness_s)[0] == "fresco" for row in rows)
                        poll_writer.writerow(dict(sample_id=sample_id, source=source, slot_from=slot_from.isoformat(),
                            slot_to=slot_to.isoformat(), captured_at=now().isoformat(),
                            status=status if status != "ok" or fresh else ("sin_gps_fresco" if rows else "vacio"),
                            requests=http.count - before, received=len(rows), fresh=fresh,
                            scope_route="|".join(r for r in routes if r),
                            scope_lon="", scope_lat="", scope_radius_m=""))
                        if status == "ok" and fresh:
                            break
                    else:
                        any_fresh = False
                        for route in routes:
                            before = http.count
                            scope = scope_for(args, route=route)
                            status, rows = croma_sample(http, sample_id, scope, args.per_page, args.max_pages,
                                                        url=endpoint_url)
                            gps_writer.writerows(rows)
                            fresh = sum(quality(row, args.freshness_s)[0] == "fresco" for row in rows)
                            any_fresh = any_fresh or (status == "ok" and fresh > 0)
                            poll_writer.writerow(dict(sample_id=sample_id, source=source, slot_from=slot_from.isoformat(),
                                slot_to=slot_to.isoformat(), captured_at=now().isoformat(),
                                status=status if status != "ok" or fresh else ("sin_gps_fresco" if rows else "vacio"),
                                requests=http.count - before, received=len(rows), fresh=fresh,
                                scope_route=route or "",
                                scope_lon=args.longitude if args.latitude is not None else "",
                                scope_lat=args.latitude if args.latitude is not None else "",
                                scope_radius_m=args.radius_m if args.latitude is not None else ""))
                            if status in ("sin_clave", "presupuesto", "fin_sesion") or http.croma_unavailable:
                                break
                        if any_fresh:
                            break
                for stream in (gps, polls, requests):
                    stream.flush()
                if args.source == "croma" and (not os.environ.get("CROMA_API_KEY") or http.croma_unavailable):
                    break
                # Si una consulta ocupó varios intervalos, los huecos quedan sin cobertura.
                elapsed = time.monotonic() - start
                slot = start + max(sample_number, int(elapsed // args.interval) + 1) * args.interval
            if deadline is not None and slot >= deadline and (
                    args.max_requests is None or http.count < args.max_requests):
                time.sleep(max(0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            manifest["interrupted"] = True
        finally:
            manifest.update(ended_at=now().isoformat(), requests=http.count, samples=sample_number)
            write_json(out / "session.json", manifest)
    print(out)
    return out


def check(args):
    http = Requests(None, args.timeout)
    results = {}
    for kind, url in RT.items():
        status, body = http.fetch(args.vehicle_url if kind == "vehicle" else url, "oficial", kind)
        if status == "ok":
            try:
                feed = parse_feed(body)
                status = f"ok ({len(feed.entity)} entidades)"
            except (DecodeError, ValueError):
                status = "protobuf_invalido"
        results[f"oficial_{kind}"] = status
        print(f"oficial {kind}: {status}")
    if args.croma:
        route = requested_routes(args.route)[0]
        status, rows = croma_sample(http, "check", scope_for(args, route=route), args.per_page, args.max_pages,
                                    url=croma_endpoint(args))
        fresh = sum(quality(row, args.freshness_s)[0] == "fresco" for row in rows)
        stale = sum(quality(row, args.freshness_s)[0] == "atrasado" for row in rows)
        results["croma_vehicles"] = f"{status} ({len(rows)} posiciones, {fresh} frescas, {stale} atrasadas)"
        print(f"croma vehicles: {results['croma_vehicles']}")
    else:
        results["croma"] = "CROMA_API_KEY disponible" if os.environ.get("CROMA_API_KEY") else "sin_clave"
        print(f"croma: {results['croma']}")
    if args.static:
        for name, url, method in (("gtfs", args.gtfs, "HEAD"), ("trazados", CORRIDOR, "GET"),
                                  ("lluvia", RAIN, "GET"), ("validaciones", VALIDATIONS, "GET")):
            status, _ = http.fetch(url, "publico", name, method=method)
            results[name] = status
            print(f"{name}: {status}")
    return results
