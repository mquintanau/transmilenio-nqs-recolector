"""Formatos y fuentes compartidos por el recolector."""
import csv
import hashlib
import json
import math
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BOGOTA = ZoneInfo("America/Bogota")
GTFS = "https://storage.googleapis.com/gtfs-estaticos/GTFS_20260929.zip"
CORRIDOR = (
    "https://gis.transmilenio.gov.co/arcgis/rest/services/ConsultaSubgerenciaPlanificacionSITP/"
    "Consulta_Planificacion_SITP/FeatureServer/5/query?"
    + urlencode({"where": "le_troncal IN ('E','G')", "outFields": "*", "outSR": 4326, "f": "geojson"})
)
RT = {kind: f"https://gis.transmilenio.gov.co/gtfs/{filename}" for kind, filename in (
    ("vehicle", "vehiclepos.pb"), ("trip", "tripupdate.pb"), ("alerts", "serviceAlerts.pb")
)}
CROMA = "https://api.croma.run/co/transmilenio/vehicles/v1"
RAIN = "https://app.sab.gov.co/sab/ServletTipoSensores?idtiposensor=5"
VALIDATIONS = "https://validaciones.datosabiertos.transmilenio.gov.co/validaciones_mensuales.html"


def now():
    return datetime.now(UTC)


def local_iso(value):
    return value.astimezone(BOGOTA).isoformat() if value is not None else ""


def parse_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in ("true", "1", "yes", "si", "sí"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def timestamp(value, local=UTC):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(float(value), UTC)
        text = str(value).strip()
        if text.isdigit():
            return datetime.fromtimestamp(float(text), UTC)
        try:
            numeric = float(text)
            if math.isfinite(numeric) and abs(numeric) >= 1e8:
                return datetime.fromtimestamp(numeric, UTC)
        except ValueError:
            pass
        date = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=local)
        return date.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def coordinates(lon, lat):
    lon, lat = number(lon), number(lat)
    if lon is not None and lat is not None and abs(lon) <= 180 and abs(lat) <= 90:
        return lon, lat
    return None


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_url(value):
    return urlsplit(str(value)).scheme in ("https", "http")


def get_input(value, cache):
    """Una URL se descarga como artefacto inmutable por ejecución."""
    if not is_url(value):
        return Path(value)
    cache.mkdir(parents=True, exist_ok=True)
    filename = Path(urlsplit(value).path).name or "input"
    target = cache / (digest(value.encode())[:12] + "_" + filename)
    partial = target.with_suffix(target.suffix + ".part")
    try:
        with urlopen(Request(value, headers={"User-Agent": "nqs-piloto/1"}), timeout=60) as response:
            with partial.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def csv_writer(stream, fields):
    writer = csv.DictWriter(stream, fields.split())
    writer.writeheader()
    return writer


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv_writer(stream, fields)
        writer.writerows(rows)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
