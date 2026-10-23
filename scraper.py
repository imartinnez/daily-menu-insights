"""Extracción y comando principal de Daily Menu Insights.

La fuente configurada publica sus datos en variables JSON incluidas en el HTML.
El módulo descarga una sola página por intento y analiza ese contrato, evitando
automatización de navegador y peticiones individuales por restaurante.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import requests

from database import MenuDatabase
from models import (
    DishRecord,
    ExtractionStatus,
    JsonObject,
    JsonValue,
    MutableDishRecord,
    NutritionDatum,
    PipelineArgs,
    PublishedNutrition,
    RestaurantExtraction,
    RestaurantTarget,
)

MADRID = ZoneInfo("Europe/Madrid")
LOGGER = logging.getLogger("smart_menu")


class ScraperError(RuntimeError):
    """Indica un fallo recuperable de red o del contrato público del portal.

    ``main`` captura esta excepción para mostrar un mensaje breve y devolver un
    código de salida distinto de cero sin ocultar errores de programación ajenos
    al scraping.
    """


def configured_targets() -> dict[int, RestaurantTarget]:
    """Carga las sedes desde ``MENU_TARGETS_JSON`` sin publicarlas en el código.

    El secreto debe ser un objeto JSON cuyas claves sean identificadores y cuyos
    valores contengan ``short_name`` y ``location``. La validación temprana evita
    ejecutar una extracción contra sedes distintas por una configuración parcial.
    """
    raw = os.getenv("MENU_TARGETS_JSON", "").strip()
    if not raw:
        raise ScraperError("Falta MENU_TARGETS_JSON con la configuración privada de sedes.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScraperError("MENU_TARGETS_JSON no contiene JSON válido.") from exc
    if not isinstance(payload, dict) or not payload:
        raise ScraperError("MENU_TARGETS_JSON debe ser un objeto JSON no vacío.")
    targets: dict[int, RestaurantTarget] = {}
    for raw_identifier, raw_target in payload.items():
        if not str(raw_identifier).isdigit() or not isinstance(raw_target, dict):
            raise ScraperError("Cada sede configurada necesita un identificador numérico y un objeto.")
        short_name = str(raw_target.get("short_name") or "").strip()
        location = str(raw_target.get("location") or "").strip()
        if not short_name:
            raise ScraperError("Cada sede configurada necesita short_name.")
        targets[int(str(raw_identifier))] = {
            "short_name": short_name,
            "location": location,
        }
    return targets


def configured_source_url(explicit_url: str | None = None) -> str:
    """Resuelve el origen privado desde argumento o variable de entorno."""
    value = (explicit_url or os.getenv("MENU_SOURCE_URL") or "").strip()
    if not value:
        raise ScraperError("Falta MENU_SOURCE_URL o el argumento --url.")
    if not value.startswith("https://"):
        raise ScraperError("La fuente del menú debe usar HTTPS.")
    return value


@dataclass
class ExtractionResult:
    """Fotografía inmutable por convención de una extracción diaria.

    Attributes:
        menu_date: Fecha ISO solicitada, no la fecha del reloj al persistir.
        extracted_at: Instante de descarga en la zona ``Europe/Madrid``.
        restaurants: Resultado independiente de cada identificador objetivo.

    ``dataclass`` genera el constructor y una representación útil para depurar;
    se mantiene mutable porque el proyecto no promete inmutabilidad pública.
    """

    menu_date: str
    extracted_at: str
    restaurants: dict[int, RestaurantExtraction]

    @property
    def total_dishes(self) -> int:
        """Suma los platos únicos de todas las sedes de la extracción."""
        return sum(len(item.get("dishes", [])) for item in self.restaurants.values())


def load_env_file(path: str | Path = ".env") -> None:
    """Carga pares ``CLAVE=VALOR`` sin reemplazar el entorno del proceso.

    Args:
        path: Archivo local opcional. Si no existe, la función no hace nada.

    Solo se admiten líneas simples; no se expanden variables ni se interpretan
    comandos. ``setdefault`` garantiza que GitHub Actions, el shell o un gestor
    de secretos tengan prioridad sobre el archivo local.
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resolve_date(value: str) -> date:
    """Resuelve un alias diario o valida una fecha ISO explícita.

    Args:
        value: ``today``, ``hoy`` o una fecha ``YYYY-MM-DD``.

    Returns:
        Fecha solicitada; los alias usan la zona de Madrid, no UTC.

    Raises:
        argparse.ArgumentTypeError: Si el texto no es un valor admitido.
    """
    if value.lower() in {"today", "hoy"}:
        return datetime.now(MADRID).date()

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("La fecha debe ser YYYY-MM-DD, today o hoy.") from exc


def normalize_text(value: object) -> str:
    """Crea una versión comparable sin modificar el texto presentado al usuario.

    Args:
        value: Valor convertible a texto; ``None`` equivale a cadena vacía.

    Returns:
        Texto en minúsculas, sin diacríticos y con espacios colapsados.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_number(value: object) -> float | None:
    """Interpreta números publicados con coma decimal y unidades incrustadas.

    Args:
        value: Número, texto o valor ausente procedente del JSON público.

    Returns:
        Número de coma flotante o ``None`` cuando no hay una cifra válida.

    Los booleanos se rechazan expresamente porque en Python son subclases de
    ``int`` y, sin esta comprobación, ``True`` se convertiría en ``1.0``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^0-9,.-]", "", text).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_json_value(value: object, fallback: JsonValue) -> JsonValue:
    """Deserializa campos que el portal publica unas veces como JSON y otras ya expandidos.

    Args:
        value: Cadena JSON, lista, diccionario o valor ausente.
        fallback: Valor que se devuelve ante ausencia o JSON inválido.

    Returns:
        Lista o diccionario existente, resultado de ``json.loads`` o ``fallback``.
    """
    if value is None or value == "":
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(cast(str | bytes | bytearray, value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def extract_js_json(html: str, name: str) -> JsonValue:
    """Extrae un literal JSON asignado a una variable JavaScript.

    Args:
        html: Documento completo descargado del portal.
        name: Nombre exacto de la variable global buscada.

    Returns:
        Valor JSON decodificado.

    Raises:
        ScraperError: Si falta la variable, no empieza por ``[``/``{``, el
            literal está incompleto o su contenido no es JSON válido.

    El recorrido entiende comillas y escapes, por lo que no se rompe al
    encontrar corchetes dentro de textos. Una expresión regular que buscara el
    primer cierre produciría cortes falsos en esos casos.
    """
    expression = re.compile(rf"(?:\b(?:const|let|var)\s+)?\b{re.escape(name)}\s*=\s*")
    match = expression.search(html)
    if not match:
        raise ScraperError(f"No se encontró la variable pública '{name}' en el HTML.")
    start = match.end()
    while start < len(html) and html[start].isspace():
        start += 1
    if start >= len(html) or html[start] not in "[{":
        raise ScraperError(f"La variable '{name}' no contiene un literal JSON.")
    opening = html[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise ScraperError(f"El JSON publicado en '{name}' no es válido: {exc}") from exc
    raise ScraperError(f"La asignación '{name}' quedó sin cerrar.")


def download_page(url: str | None = None, timeout: int = 30) -> str:
    """Descarga el portal y verifica que conserve las variables esperadas.

    Args:
        url: Página pública que contiene los menús hidratados.
        timeout: Límite total de espera de ``requests`` en segundos.

    Returns:
        HTML completo de la respuesta.

    Raises:
        ScraperError: Ante errores HTTP/red o si faltan marcadores del contrato.

    Las cabeceras de no caché obligan a revalidar una respuesta potencialmente
    antigua, pero no incrementan el número de solicitudes de cada intento.
    """
    resolved_url = configured_source_url(url)
    headers: dict[str, str] = {
        "User-Agent": "DailyMenuInsights/1.0 (+personal-menu-analysis; respectful single daily request)",
        "Accept-Language": "es-ES,es;q=0.9",
        # Pide revalidación, sin aumentar el número de solicitudes ni aceptar
        # ciegamente una respuesta intermedia almacenada en caché.
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    try:
        response = requests.get(resolved_url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        # No se incorpora la URL al log: en repositorios públicos identifica la
        # organización aunque el valor llegue desde un Secret de Actions.
        raise ScraperError(
            f"No se pudo descargar la fuente configurada ({type(exc).__name__})."
        ) from exc
    if "ubicacionesAll" not in response.text or "const platos" not in response.text:
        raise ScraperError("La página descargada no contiene el contrato de menú esperado.")
    return response.text


def _source_nutrition(dish: JsonObject) -> PublishedNutrition:
    """Normaliza los nombres nutricionales heterogéneos usados por la fuente.

    Args:
        dish: Objeto de plato tal como aparece en la colección global del portal.

    Returns:
        Los siete campos conocidos con texto original, valor numérico y origen.

    Conservar ``raw`` junto a ``value`` permite auditar el parseo y regenerar el
    hash si el proveedor cambia una unidad o representación textual.
    """
    properties = parse_json_value(dish.get("properties"), [])
    props = cast(JsonObject, properties[0]) if isinstance(properties, list) and properties else {}
    aliases: dict[str, tuple[str, ...]] = {
        "kcal": ("calorias", "kcal"),
        "carbohidratos_g": ("carbohidratos", "hidratos", "hc"),
        "proteinas_g": ("proteina", "proteinas", "proteínas"),
        "grasas_g": ("grasas",),
        "fibra_g": ("fibra",),
        "azucares_g": ("azucares", "azúcares"),
        "sal_g": ("sal",),
    }
    result: dict[str, NutritionDatum] = {}
    for target, candidates in aliases.items():
        raw = next((props.get(key) for key in candidates if key in props), None)
        result[target] = {"raw": raw, "value": parse_number(raw), "source": "publicado" if raw not in (None, "") else "ausente"}
    return cast(PublishedNutrition, result)


def _hash_dish(
    name: str,
    description: str,
    ingredients: list[str],
    allergens: list[str],
    nutrition: PublishedNutrition,
) -> str:
    """Calcula la identidad de una versión semántica de receta.

    Args:
        name: Nombre visible del plato.
        description: Descripción publicada, posiblemente vacía.
        ingredients: Ingredientes normalizados como lista.
        allergens: Nombres o identificadores de alérgenos.
        nutrition: Valores oficiales conservando su texto original.

    Returns:
        SHA-256 hexadecimal estable para el mismo contenido.

    No se incluye restaurante ni fecha: una receta idéntica compartida por dos
    sedes reutiliza estimación. Las listas se ordenan para que el orden incidental
    de la fuente no invalide la caché.
    """
    canonical: JsonObject = {
        "name": normalize_text(name),
        "description": normalize_text(description),
        "ingredients": cast(list[JsonValue], sorted(normalize_text(item) for item in ingredients)),
        "allergens": cast(list[JsonValue], sorted(normalize_text(item) for item in allergens)),
        "nutrition": {
            "kcal": nutrition["kcal"]["raw"],
            "carbohidratos_g": nutrition["carbohidratos_g"]["raw"],
            "proteinas_g": nutrition["proteinas_g"]["raw"],
            "grasas_g": nutrition["grasas_g"]["raw"],
            "fibra_g": nutrition["fibra_g"]["raw"],
            "azucares_g": nutrition["azucares_g"]["raw"],
            "sal_g": nutrition["sal_g"]["raw"],
        },
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _day_groups(menu: JsonObject, requested: date) -> list[JsonObject]:
    """Selecciona los grupos de una fecha dentro de un menú rotatorio.

    Args:
        menu: Definición de menú publicada para una sede.
        requested: Fecha que se desea resolver.

    Returns:
        Grupos del día o una lista vacía si el menú no aplica/no es válido.

    La posición es la distancia desde ``start`` módulo ``rotatorio``. El valor
    rotatorio se acota al contenido disponible para no indexar ciclos que la
    publicación declara de forma inconsistente.
    """
    content = parse_json_value(menu.get("content"), [])
    if not isinstance(content, list) or not content:
        return []
    raw_start = str(menu.get("start") or "")[:10]
    try:
        start_date = date.fromisoformat(raw_start)
    except ValueError:
        return []
    if requested < start_date:
        return []
    try:
        rotation = int(cast(str | int | float, menu.get("rotatorio") or len(content)))
    except (TypeError, ValueError):
        rotation = len(content)
    rotation = max(1, min(rotation, len(content)))
    index = (requested - start_date).days % rotation
    day_data = content[index] if index < len(content) else {}
    if not isinstance(day_data, dict):
        return []
    groups = day_data.get("grupos", [])
    return cast(list[JsonObject], groups) if isinstance(groups, list) else []


def parse_menu_html(html: str, requested: date) -> ExtractionResult:
    """Convierte el contrato HTML en una fotografía de los cuatro restaurantes.

    Args:
        html: Documento que contiene las colecciones JSON globales.
        requested: Fecha del menú que se resolverá en cada calendario rotatorio.

    Returns:
        Resultado por sede, incluidos estados parciales y platos deduplicados.

    Raises:
        ScraperError: Si falta o no se puede decodificar una colección obligatoria.

    Los ``cast`` expresan el contrato observado sin añadir coerciones silenciosas:
    un cambio estructural relevante debe fallar y hacerse visible, no producir un
    menú aparentemente válido con datos reinterpretados.
    """
    locations = cast(list[JsonObject], extract_js_json(html, "ubicacionesAll"))
    all_dishes = cast(list[JsonObject], extract_js_json(html, "platos"))
    all_allergens = cast(list[JsonObject], extract_js_json(html, "alergenosAll"))
    meal_groups = cast(list[JsonObject], extract_js_json(html, "gruposComidas"))
    dishes_by_id: dict[str, JsonObject] = {str(item.get("id")): item for item in all_dishes}
    allergen_by_id: dict[str, str] = {
        str(item.get("id")): str(item.get("name") or "Alérgeno sin nombre") for item in all_allergens
    }
    group_by_id: dict[str, str] = {
        str(item.get("id")): str(item.get("name_es") or item.get("name") or "") for item in meal_groups
    }
    locations_by_id: dict[int, JsonObject] = {
        int(str(item.get("id"))): item for item in locations if str(item.get("id", "")).isdigit()
    }
    extracted_at = datetime.now(MADRID).isoformat(timespec="seconds")
    extracted: dict[int, RestaurantExtraction] = {}

    for restaurant_id, target in configured_targets().items():
        location = locations_by_id.get(restaurant_id)
        if not location:
            extracted[restaurant_id] = {"status": "error", "detail": "El identificador no figura en el HTML publicado.", "dishes": []}
            continue
        aggregation: dict[str, MutableDishRecord] = {}
        applicable_menus = 0
        menus = cast(list[JsonObject], location.get("menus") or [])
        for menu in menus:
            groups = _day_groups(menu, requested)
            if groups:
                applicable_menus += 1
            for group in groups:
                group_name = str(group.get("name") or "Sin grupo")
                dish_ids = cast(list[JsonValue], group.get("platos") or [])
                for dish_id in dish_ids:
                    raw = dishes_by_id.get(str(dish_id))
                    if not raw:
                        LOGGER.warning("Un plato referenciado no figura en la colección global.")
                        continue
                    name = str(raw.get("name") or "Plato sin nombre").strip()
                    raw_ingredients = cast(list[JsonValue], parse_json_value(raw.get("ingredientes"), []))
                    ingredients: list[str] = [
                        str(value).strip() for value in raw_ingredients if str(value).strip()
                    ]
                    allergen_ids = cast(list[JsonValue], parse_json_value(raw.get("alergenos"), []))
                    allergens = [allergen_by_id.get(str(value), f"ID {value}") for value in allergen_ids]
                    nutrition = _source_nutrition(raw)
                    category_data = cast(JsonObject, raw.get("categoria_plato") or {})
                    category = str(category_data.get("name") or "Sin categoría")
                    content_hash = _hash_dish(name, str(raw.get("descripcion") or ""), ingredients, allergens, nutrition)
                    if content_hash not in aggregation:
                        source_dish_id = cast(str | int | None, raw.get("id"))
                        aggregation[content_hash] = {
                            "content_hash": content_hash,
                            "source_dish_id": source_dish_id,
                            "normalized_name": normalize_text(name),
                            "name": name,
                            "description": str(raw.get("descripcion") or "").strip(),
                            "dish_type": raw.get("tipo"),
                            "ingredients": ingredients,
                            "allergens": allergens,
                            "published_nutrition": nutrition,
                            # La web etiqueta Kcal y HC, pero no declara base/ración: no se inventa.
                            "nutrition_basis": "no declarada por el proveedor (no consta por ración ni por 100 g)",
                            "source_payload": {
                                "source_dish_id": source_dish_id,
                                "grupo_id": raw.get("grupo"),
                                "tipo": raw.get("tipo"),
                                "grupo_publicado": group_by_id.get(str(raw.get("grupo"))),
                                "categoria_id": raw.get("categoria_id"),
                                "properties": raw.get("properties"),
                            },
                            "groups": set(),
                            "categories": set(),
                            "menu_names": set(),
                        }
                    item = aggregation[content_hash]
                    item["groups"].add(str(group_name))
                    item["categories"].add(str(category))
                    item["menu_names"].add(str(menu.get("name") or "Menú"))

        dishes: list[DishRecord] = []
        for item in aggregation.values():
            dishes.append(
                {
                    "content_hash": item["content_hash"],
                    "source_dish_id": item["source_dish_id"],
                    "normalized_name": item["normalized_name"],
                    "name": item["name"],
                    "description": item["description"],
                    "dish_type": item["dish_type"],
                    "ingredients": item["ingredients"],
                    "allergens": item["allergens"],
                    "published_nutrition": item["published_nutrition"],
                    "nutrition_basis": item["nutrition_basis"],
                    "source_payload": item["source_payload"],
                    "groups": sorted(item["groups"]),
                    "categories": sorted(item["categories"]),
                    "menu_names": sorted(item["menu_names"]),
                }
            )
        status: ExtractionStatus = "success" if dishes else "no_menu_published"
        detail = "" if dishes else (
            "No hay grupos aplicables a la fecha solicitada." if not applicable_menus else "El menú no contiene platos publicados."
        )
        extracted[restaurant_id] = {
            "status": status,
            "detail": detail,
            "name": target["short_name"],
            "location": target["location"],
            "dishes": sorted(dishes, key=lambda item: item["name"].casefold()),
        }
    return ExtractionResult(requested.isoformat(), extracted_at, extracted)


def scrape_date(
    requested: date, url: str | None = None, attempts: int = 2, retry_seconds: int = 20
) -> ExtractionResult:
    """Descarga y extrae los cuatro restaurantes con reintentos acotados.

    Args:
        requested: Fecha que debe resolver el contrato rotatorio.
        url: Portal de origen.
        attempts: Número de intentos; los valores menores que uno se convierten en uno.
        retry_seconds: Espera entre intentos, acotada internamente entre 0 y 60.

    Returns:
        Primera extracción completa o, agotados los intentos, el último resultado
        parcial obtenido.

    Raises:
        ScraperError: Si todos los intentos fallan antes de producir un resultado.

    Solo devuelve la extracción del último intento, nunca mezcla ni recicla datos
    de un día anterior cuando una sede aún no ha publicado su menú.
    """
    max_attempts = max(1, attempts)
    last_error: ScraperError | None = None
    last_result: ExtractionResult | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = parse_menu_html(download_page(url), requested)
            if all(item["status"] == "success" for item in result.restaurants.values()):
                return result
            LOGGER.warning("Intento %s/%s incompleto para %s.", attempt, max_attempts, requested)
            last_result = result
        except ScraperError as exc:
            last_error = exc
            LOGGER.warning("Intento %s/%s falló: %s", attempt, max_attempts, exc)
            last_result = None
        if attempt < max_attempts:
            time.sleep(max(0, min(retry_seconds, 60)))
    if last_result is not None:
        return last_result
    raise last_error or ScraperError("No se pudo obtener ningún resultado.")


def persist_extraction(database: MenuDatabase, result: ExtractionResult) -> None:
    """Persiste recetas, disponibilidades y el resultado de cada sede.

    Args:
        database: Almacén SQLite ya inicializado.
        result: Fotografía producida por ``scrape_date``.

    Una sede con estado ``success`` reemplaza por completo su menú de esa fecha;
    un estado parcial solo se registra y no borra una publicación válida anterior.
    Esto evita convertir un fallo temporal del portal en pérdida de histórico.
    """
    for restaurant_id, restaurant in result.restaurants.items():
        database.upsert_restaurant(
            {
                "id": restaurant_id,
                "name": restaurant.get("name") or f"Restaurante {restaurant_id}",
                "location": restaurant.get("location") or "",
            }
        )
        dishes = restaurant.get("dishes", [])
        for dish in dishes:
            database.upsert_dish(dish)
        if restaurant["status"] == "success":
            # Sustituir, y no acumular, evita que una corrección del portal
            # mezcle platos de una publicación anterior bajo la misma fecha.
            database.replace_menu_items(result.menu_date, restaurant_id, dishes)
        database.record_extraction(
            result.menu_date,
            restaurant_id,
            restaurant["status"],
            len(dishes),
            restaurant.get("detail", ""),
        )


def configure_logging(verbose: bool = False) -> None:
    """Configura el nivel y formato común de los módulos de la aplicación.

    Args:
        verbose: Activa mensajes ``DEBUG`` cuando es verdadero.

    ``basicConfig`` no reemplaza handlers instalados por un proceso anfitrión,
    comportamiento útil al importar el módulo desde Jupyter o Streamlit.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_pipeline(args: PipelineArgs) -> int:
    """Orquesta extracción, persistencia, análisis, salida y envío opcional.

    Args:
        args: Configuración estructuralmente compatible con ``PipelineArgs``.

    Returns:
        ``0`` al completar el flujo y ``2`` cuando se solicita correo pero la
        extracción es parcial y la política prohíbe enviarlo.

    Raises:
        ScraperError: Propagado si no se obtiene ninguna extracción.
        Exception: Los fallos de persistencia, renderizado o SMTP se propagan para
            que ``main`` termine con error y GitHub Actions no muestre un falso éxito.

    Los imports tardíos evitan cargar presentación y nutrición cuando otro módulo
    reutiliza exclusivamente las funciones de parsing.
    """
    from mailer import (
        build_report,
        save_email_preview,
        save_preview,
        save_site_index,
        save_site_report,
        send_report,
    )
    from nutrition import enrich_missing_nutrition, recommend_daily

    requested = resolve_date(args.date)
    database = MenuDatabase(args.database)
    result = scrape_date(requested, args.url, args.attempts, args.retry_seconds)
    persist_extraction(database, result)
    LOGGER.info("Extracción %s: %s platos en %s restaurantes.", result.menu_date, result.total_dishes, len(result.restaurants))
    for position, value in enumerate(result.restaurants.values(), start=1):
        LOGGER.info(
            "Sede %-6s %-18s %s platos",
            position,
            value["status"],
            len(value["dishes"]),
        )

    rows = database.menu_for_date(result.menu_date)
    if not args.no_llm:
        enrich_missing_nutrition(database, rows, provider_name=args.provider)
        rows = database.menu_for_date(result.menu_date)
    recommendations = recommend_daily(rows)
    extraction = database.extraction_summary(result.menu_date)
    report = build_report(
        result.menu_date,
        result.extracted_at,
        rows,
        extraction,
        recommendations,
        os.getenv("REPORT_BASE_URL"),
    )

    preview_path: Path | None = None
    if args.preview or args.send_email:
        preview_path = Path(args.preview or f"output/menu_{result.menu_date}.html")
        save_preview(report, preview_path)
        suffix = preview_path.suffix or ".html"
        email_preview_path = preview_path.with_name(f"{preview_path.stem}_email{suffix}")
        save_email_preview(report, email_preview_path)
        LOGGER.info("Informe interactivo: %s", preview_path.resolve())
        LOGGER.info("Vista previa del correo: %s", email_preview_path.resolve())
    if args.site_dir:
        # Cada despliegue vuelve a generar el histórico desde SQLite. GitHub Pages
        # sustituye el artefacto completo, por lo que esta reconstrucción mantiene
        # válidos los enlaces fechados enviados en días anteriores.
        with database.connection() as connection:
            historical_dates = [
                str(item[0])
                for item in connection.execute(
                    "SELECT DISTINCT menu_date FROM menu_items ORDER BY menu_date"
                ).fetchall()
            ]
        for historical_date in historical_dates:
            if historical_date == result.menu_date:
                historical_report = report
            else:
                historical_rows = database.menu_for_date(historical_date)
                historical_extraction = database.extraction_summary(historical_date)
                historical_report = build_report(
                    historical_date,
                    "",
                    historical_rows,
                    historical_extraction,
                    recommend_daily(historical_rows),
                    os.getenv("REPORT_BASE_URL"),
                )
            save_site_report(historical_report, args.site_dir)
        save_site_index(args.site_dir, result.menu_date)
        LOGGER.info("Sitio estático generado: %s", Path(args.site_dir).resolve())
    if args.export_csv:
        exported = database.export_csv(result.menu_date, args.export_csv)
        LOGGER.info("CSV exportado: %s", exported.resolve())
    if args.backup:
        backup_path = Path(args.backup) if args.backup != "auto" else Path("data/backups") / f"menus-{result.menu_date}.db"
        database.backup(backup_path)
        LOGGER.info("Copia SQLite: %s", backup_path.resolve())

    if args.send_email:
        complete = all(item["status"] == "success" for item in extraction)
        allow_partial = args.allow_partial or os.getenv("ALLOW_PARTIAL_EMAIL", "false").lower() == "true"
        if not complete and not allow_partial:
            LOGGER.error("No se envía el informe: la extracción está incompleta. Use --allow-partial para cambiar esta política.")
            return 2
        sent = send_report(database, report, force=args.force_email, preview_path=preview_path)
        if sent:
            LOGGER.info("Correo aceptado por el servidor SMTP para %s.", result.menu_date)
        else:
            LOGGER.warning(
                "Correo omitido: el informe de %s ya estaba marcado como enviado. Use --force-email para reenviarlo.",
                result.menu_date,
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Define la interfaz de línea de comandos y sus valores por entorno.

    Returns:
        Parser configurado, todavía sin consumir ``sys.argv``.

    Los argumentos explícitos prevalecen sobre las variables de entorno porque
    ``argparse`` solo usa estas últimas para construir valores predeterminados.
    """
    parser = argparse.ArgumentParser(description="Daily Menu Insights: extracción y reporte diario")
    parser.add_argument("--date", default="today", help="today/hoy o YYYY-MM-DD")
    parser.add_argument("--database", default="data/menus.db", help="Ruta al SQLite")
    parser.add_argument("--url", default=os.getenv("MENU_SOURCE_URL", ""), help="URL privada del portal")
    parser.add_argument("--attempts", type=int, default=int(os.getenv("SCRAPE_ATTEMPTS", "2")), help="Intentos acotados cuando falta un menú")
    parser.add_argument("--retry-seconds", type=int, default=int(os.getenv("SCRAPE_RETRY_SECONDS", "20")), help="Espera máxima entre reintentos (0-60)")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "none"), choices=["none", "ollama", "gemini"], help="Proveedor de estimaciones")
    parser.add_argument("--no-llm", action="store_true", help="No llama al LLM; reutiliza la caché existente")
    parser.add_argument("--preview", help="Guarda el informe HTML en esta ruta")
    parser.add_argument("--site-dir", help="Genera el histórico HTML apto para publicación estática")
    parser.add_argument("--send-email", action="store_true", help="Envía por SMTP solo después de generar el informe")
    parser.add_argument("--force-email", action="store_true", help="Reintenta un informe ya marcado como enviado")
    parser.add_argument("--allow-partial", action="store_true", help="Permite envío si falta algún restaurante")
    parser.add_argument("--export-csv", help="Exporta el menú del día a CSV")
    parser.add_argument("--backup", nargs="?", const="auto", help="Crea una copia SQLite; opcionalmente indica ruta")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    """Punto de entrada de consola con códigos de salida aptos para automatización.

    Returns:
        Código de proceso: ``0`` éxito, ``1`` error y ``2`` informe parcial no enviado.

    ``ScraperError`` recibe un mensaje conciso; los errores inesperados conservan
    el traceback en el log para poder diagnosticarlos en GitHub Actions.
    """
    load_env_file()
    args = cast(PipelineArgs, build_parser().parse_args())
    configure_logging(args.verbose)
    try:
        return run_pipeline(args)
    except ScraperError as exc:
        LOGGER.error("Extracción abortada: %s", exc)
        return 1
    except Exception:
        LOGGER.exception("Ejecución interrumpida")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
