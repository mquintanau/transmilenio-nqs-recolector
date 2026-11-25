"""Interfaz de línea de comandos para el recolector reproducible de la NQS."""
import argparse
from datetime import date, time as clock
from pathlib import Path

from .catalog import build_catalog
from .common import CORRIDOR, GTFS, RAIN, RT
from .inputs import collect_rain, validations
from .live import capture, check
from .metrics import export_metrics


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Fecha inválida (use YYYY-MM-DD): {value}") from exc


def parse_clock(value):
    try:
        return clock.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Hora inválida (use HH:MM): {value}") from exc


def parse_positive_int(min_value, max_value=None):
    def _parse(value):
        try:
            num = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"Entero inválido: {value}") from exc
        if num < min_value or (max_value is not None and num > max_value):
            bound = f"[{min_value}, {max_value}]" if max_value is not None else f">= {min_value}"
            raise argparse.ArgumentTypeError(f"Valor fuera de rango {bound}: {num}")
        return num
    return _parse


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m nqs",
        description="Recolector reproducible de operación TransMilenio en la NQS (Bloques B y C).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. Catálogo
    p_cat = sub.add_parser("catalog", help="Construye el catálogo de patrones, estaciones y controles NQS.")
    p_cat.add_argument("--gtfs", default=GTFS, help="URL o ruta local del ZIP GTFS estático.")
    p_cat.add_argument("--corridor", default=CORRIDOR, help="URL o ruta local del GeoJSON de trazados E y G.")
    p_cat.add_argument("--out", type=Path, default=Path("datos_nqs/derived/catalog"), help="Carpeta de salida derivada.")
    p_cat.add_argument("--raw-dir", type=Path, default=None, help="Carpeta para insumos crudos descargados.")
    p_cat.add_argument("--version", default=None, help="Etiqueta explícita de versión del GTFS.")
    p_cat.add_argument("--tolerance-m", type=float, default=30.0, help="Tolerancia geométrica al trazado en metros.")
    p_cat.add_argument("--min-overlap-m", type=float, default=300.0, help="Coincidencia continua mínima en metros.")
    p_cat.add_argument("--station-m", type=float, default=65.0, help="Radio de asociación estación/plataforma en metros.")
    p_cat.add_argument("--link-gap-m", type=float, default=100.0, help="Brecha máxima para enlazar tramos contiguos.")
    p_cat.add_argument("--trunk-agency", action="append", default=None, help="agency_id troncal (repetible).")
    p_cat.set_defaults(func=build_catalog)

    # 2. Verificación de fuentes
    p_chk = sub.add_parser("check", help="Comprueba el estado actual de los feeds oficiales, Croma e insumos.")
    p_chk.add_argument("--vehicle-url", default=RT["vehicle"], help="URL GTFS-RT de posiciones de vehículos.")
    p_chk.add_argument("--gtfs", default=GTFS, help="URL GTFS estático para verificación.")
    p_chk.add_argument("--croma", action="store_true", help="Ejecuta una consulta de prueba a Croma (usa CROMA_API_KEY).")
    p_chk.add_argument("--croma-url", default=None, help="Endpoint alternativo de Croma vehicles.")
    p_chk.add_argument("--static", action="store_true", help="Verifica también GTFS, trazados, lluvia y validaciones.")
    p_chk.add_argument("--route", action="append", default=None, help="Filtro opcional de ruta para Croma.")
    p_chk.add_argument("--latitude", type=float, default=None, help="Latitud opcional para filtro radial Croma.")
    p_chk.add_argument("--longitude", type=float, default=None, help="Longitud opcional para filtro radial Croma.")
    p_chk.add_argument("--radius-m", type=parse_positive_int(50, 5000), default=500, help="Radio Croma (50-5000 m).")
    p_chk.add_argument("--per-page", type=parse_positive_int(1, 500), default=100, help="Vehículos por página Croma.")
    p_chk.add_argument("--max-pages", type=parse_positive_int(1), default=1, help="Máximo de páginas por consulta.")
    p_chk.add_argument("--freshness-s", type=float, default=90.0, help="Umbral de frescura GPS en segundos.")
    p_chk.add_argument("--timeout", type=float, default=10.0, help="Timeout HTTP en segundos.")
    p_chk.set_defaults(func=check)

    # 3. Captura en vivo (muestra única o sesión)
    p_cap = sub.add_parser("capture", help="Captura una muestra puntual (--duration 0) o una sesión en vivo.")
    p_cap.add_argument("--source", choices=("auto", "oficial", "croma"), default="auto",
                       help="Fuente en vivo: oficial, croma o auto (oficial con respaldo Croma).")
    p_cap.add_argument("--out", type=Path, default=Path("datos_nqs/raw/live"), help="Carpeta raíz para sesiones crudas.")
    p_cap.add_argument("--catalog", type=Path, default=None, help="Catálogo NQS para enlazar route_id y registrar catalog_id.")
    p_cap.add_argument("--duration", type=float, default=0.0, help="Duración en segundos (0 = una sola muestra).")
    p_cap.add_argument("--interval", type=float, default=30.0, help="Intervalo entre encuestas en segundos.")
    p_cap.add_argument("--max-requests", type=parse_positive_int(1), default=None, help="Límite máximo de solicitudes HTTP.")
    p_cap.add_argument("--freshness-s", type=float, default=90.0, help="Umbral de frescura GPS en segundos.")
    p_cap.add_argument("--route", action="append", default=None,
                       help="Ruta(s) a consultar (repetible o separada por comas, ej. 4,B12,E42,M47).")
    p_cap.add_argument("--latitude", type=float, default=None, help="Latitud para filtro geográfico Croma.")
    p_cap.add_argument("--longitude", type=float, default=None, help="Longitud para filtro geográfico Croma.")
    p_cap.add_argument("--radius-m", type=parse_positive_int(50, 5000), default=500, help="Radio Croma (50-5000 m).")
    p_cap.add_argument("--per-page", type=parse_positive_int(1, 500), default=500, help="Vehículos por página Croma (1-500).")
    p_cap.add_argument("--max-pages", type=parse_positive_int(1), default=20, help="Máximo de páginas Croma por muestra.")
    p_cap.add_argument("--vehicle-url", default=RT["vehicle"], help="URL GTFS-RT de posiciones.")
    p_cap.add_argument("--croma-url", default=None, help="Endpoint alternativo de Croma vehicles.")
    p_cap.add_argument("--timeout", type=float, default=10.0, help="Timeout HTTP en segundos.")
    p_cap.set_defaults(func=capture)

    # 4. Captura independiente de lluvia SAB/IDIGER
    p_rain = sub.add_parser("rain", help="Archiva lecturas minutales de lluvia del SAB/IDIGER antes de sobrescritura.")
    p_rain.add_argument("--url", default=RAIN, help="URL del endpoint JSON de pluviómetros IDIGER.")
    p_rain.add_argument("--out", type=Path, default=Path("datos_nqs/raw/lluvia/sab_lluvia.csv"),
                        help="CSV acumulativo de lecturas crudas de lluvia.")
    p_rain.add_argument("--duration", type=float, default=0.0, help="Duración en segundos (0 = una captura).")
    p_rain.add_argument("--interval", type=float, default=60.0, help="Intervalo entre consultas en segundos.")
    p_rain.add_argument("--max-requests", type=parse_positive_int(1), default=None, help="Máximo de solicitudes HTTP.")
    p_rain.add_argument("--timeout", type=float, default=15.0, help="Timeout HTTP en segundos.")
    p_rain.set_defaults(func=collect_rain)

    # 5. Importación de validaciones oficiales
    p_val = sub.add_parser("validations", help="Importa validaciones oficiales por estación y franja (XLSX/CSV).")
    p_val.add_argument("--input", required=True, help="Ruta local o URL del archivo oficial de validaciones.")
    p_val.add_argument("--catalog", type=Path, default=Path("datos_nqs/derived/catalog"),
                       help="Catálogo NQS para cruzar códigos de estación con GTFS.")
    p_val.add_argument("--out", type=Path, default=Path("datos_nqs/derived/validaciones/validaciones_nqs.csv"),
                       help="CSV derivado de validaciones por estación y franja.")
    p_val.add_argument("--raw-dir", type=Path, default=None, help="Carpeta para insumos crudos descargados.")
    p_val.add_argument("--source", default=None, help="Etiqueta o URL oficial de procedencia.")
    p_val.add_argument("--sheet", default=None, help="Nombre de hoja XLSX (por defecto 'Validaciones Tullave').")
    p_val.add_argument("--encoding", default="utf-8-sig", help="Codificación para archivos CSV.")
    p_val.add_argument("--date", action="append", type=parse_date, default=[], help="Fecha(s) YYYY-MM-DD a incluir.")
    p_val.add_argument("--start", type=parse_clock, default=clock(7, 0), help="Inicio de franja HH:MM (America/Bogota).")
    p_val.add_argument("--end", type=parse_clock, default=clock(8, 0), help="Fin de franja HH:MM (America/Bogota).")
    p_val.add_argument("--corridor-only", action="store_true", help="Conserva solo estaciones del catálogo NQS.")
    p_val.set_defaults(func=validations)

    # 6. Procesamiento y exportación de métricas para Bloques B y C
    p_met = sub.add_parser("metrics", help="Procesa calidad, pasos por control y exporta métricas para Bloques B y C.")
    p_met.add_argument("--catalog", type=Path, default=Path("datos_nqs/derived/catalog"), help="Carpeta del catálogo NQS.")
    p_met.add_argument("--sessions", type=Path, nargs="+", required=True,
                       help="Una o más carpetas de sesión cruda (o directorio contenedor).")
    p_met.add_argument("--out", type=Path, default=Path("datos_nqs/derived/metrics"), help="Carpeta de salida derivada.")
    p_met.add_argument("--rain", type=Path, default=None, help="CSV de lluvia IDIGER para comparar seco vs lluvia.")
    p_met.add_argument("--validations", type=Path, default=None, help="CSV de validaciones importadas (para procedencia).")
    p_met.add_argument("--date", action="append", type=parse_date, default=[], help="Filtrar fecha(s) YYYY-MM-DD (Bogotá).")
    p_met.add_argument("--start", type=parse_clock, default=None, help="Inicio de franja HH:MM (America/Bogota).")
    p_met.add_argument("--end", type=parse_clock, default=None, help="Fin de franja HH:MM (America/Bogota).")
    p_met.add_argument("--window-minutes", type=parse_positive_int(5, 1440), default=60,
                       help="Tamaño de franja en minutos si no se fija --start/--end.")
    p_met.add_argument("--freshness-s", type=float, default=90.0, help="Umbral máximo de atraso GPS en segundos.")
    p_met.add_argument("--min-coverage", type=float, default=0.7, help="Cobertura mínima de ventana [0-1].")
    p_met.add_argument("--max-gap-s", type=float, default=180.0, help="Brecha máxima de trayectoria para interpolar/estimar.")
    p_met.add_argument("--checkpoint-radius-m", type=float, default=45.0, help="Radio de proximidad al control en metros.")
    p_met.add_argument("--max-off-route-m", type=float, default=65.0, help="Distancia máxima del GPS al eje del patrón.")
    p_met.add_argument("--run-split-gap-s", type=float, default=900.0, help="Pausa para separar viajes distintos del mismo bus.")
    p_met.add_argument("--min-samples", type=parse_positive_int(1), default=5, help="Muestra mínima para percentiles.")
    p_met.add_argument("--rain-threshold-mm", type=float, default=0.1, help="Umbral de lluvia en mm.")
    p_met.add_argument("--rain-radius-m", type=float, default=5000.0, help="Radio de pluviómetros cercanos en metros.")
    p_met.add_argument("--rain-window-min", type=float, default=15.0, help="Ventana temporal de lectura pluviométrica (min).")
    p_met.add_argument("--include-non-operational", action="store_true", help="Incluye patrones zonales/alimentadores.")
    p_met.set_defaults(func=export_metrics)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in ("check", "capture") and (args.latitude is None) != (args.longitude is None):
        parser.error("Debe indicar --latitude y --longitude juntos.")
    if args.command == "metrics" and (args.start is None) != (args.end is None):
        parser.error("Debe indicar --start y --end juntos.")
    result = args.func(args)
    if args.command == "catalog":
        print(f"Catálogo generado en {args.out} ({result} patrones)")
    elif args.command == "validations":
        print(f"Validaciones exportadas en {args.out} ({result} filas)")
    elif args.command == "rain":
        print(f"Serie de lluvia actualizada en {result}")
    elif args.command == "metrics":
        print(f"Métricas exportadas en {args.out}: {result}")
    return result


if __name__ == "__main__":
    main()
