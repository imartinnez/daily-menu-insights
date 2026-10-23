"""Estimación nutricional opcional y recomendaciones deterministas.

El LLM solo completa métricas ausentes. La selección de platos, las restricciones
de estructura y la puntuación permanecen en código para que una misma base de
datos produzca recomendaciones reproducibles y auditables.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from itertools import product
from typing import Callable, Literal, cast

import requests

from database import MenuDatabase
from models import (
    ConfidenceLevel,
    CourseKind,
    EnrichmentCounts,
    EstimateValues,
    JsonObject,
    MenuCombination,
    MenuRow,
    NutrientField,
    NutrientSource,
    NutritionTotals,
    Recommendation,
    RecommendationProfile,
    Recommendations,
    StoredEstimate,
)

LOGGER = logging.getLogger("smart_menu.nutrition")
CORE_FIELDS: tuple[NutrientField, ...] = ("kcal", "proteinas_g", "carbohidratos_g", "grasas_g", "fibra_g")
NUTRITION_PROMPT_VERSION = 2
ESTIMATE_KEY_BY_FIELD: dict[NutrientField, str] = {
    "kcal": "kcal_estimadas",
    "proteinas_g": "proteinas_g",
    "carbohidratos_g": "carbohidratos_g",
    "grasas_g": "grasas_g",
    "fibra_g": "fibra_g",
}
ESTIMATE_FIELDS: tuple[str, ...] = (
    "proteinas_g",
    "grasas_g",
    "carbohidratos_g",
    "fibra_g",
    "kcal_estimadas",
    "grasas_saturadas_g",
    "azucares_g",
    "sal_g",
    "tamano_racion_asumido_g",
    "nivel_confianza",
    "justificacion_breve",
    "supuestos",
    "campos_indeterminados",
)


class NutritionProviderError(RuntimeError):
    """Indica que un proveedor LLM no está disponible o incumplió el contrato.

    El enriquecimiento captura esta excepción por plato, registra el fallo y
    continúa con datos oficiales. Otros errores se propagan porque podrían
    revelar un defecto de programación o persistencia.
    """


def _as_number(value: object) -> float | None:
    """Convierte un valor LLM a número dentro de un intervalo defensivo.

    Args:
        value: Campo arbitrario de la respuesta JSON.

    Returns:
        ``float`` entre 0 y 10 000 o ``None`` si es ausente/no plausible.

    El límite no afirma validez nutricional; evita persistir negativos, infinitos
    prácticos o texto que contaminaría sumas y rankings posteriores.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        converted = float(cast(str | int | float, value))
    except (TypeError, ValueError):
        return None
    return converted if 0 <= converted <= 10000 else None


def _strip_json_fence(value: str) -> str:
    """Elimina cercas Markdown que algunos modelos añaden pese al modo JSON.

    Args:
        value: Respuesta textual completa del modelo.

    Returns:
        Contenido sin una cerca exterior `````json``/````` ``.
    """
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", value.strip(), flags=re.IGNORECASE)


def validate_estimate(
    payload: object,
    published_kcal: float | None = None,
    *,
    required_fields: tuple[NutrientField, ...] = (),
    allow_indeterminate: bool = False,
) -> EstimateValues:
    """Valida y normaliza una respuesta LLM antes de persistirla.

    Args:
        payload: Objeto JSON decodificado devuelto por el proveedor.
        published_kcal: Energía oficial del plato, si la fuente la publica.
        required_fields: Métricas oficiales ausentes que la respuesta debe resolver.
        allow_indeterminate: Permite justificar ``null`` cuando la composición
            depende de elecciones que la fuente no concreta.

    Returns:
        Contrato completo con números acotados, confianza normalizada y textos
        limitados de longitud.

    Raises:
        NutritionProviderError: Si la raíz no es un objeto o deja una métrica
            exigida sin cifra ni una declaración de indeterminación permitida.

    Una Kcal oficial fuerza ``kcal_estimadas`` a ``None``. Esta regla impide que
    una cifra inferida compita con la fuente aunque el modelo ignore el prompt.
    """
    if not isinstance(payload, dict):
        raise NutritionProviderError("El modelo no devolvió un objeto JSON.")
    result: dict[str, object] = {}
    for key in ESTIMATE_FIELDS:
        result[key] = payload.get(key)
    for key in ("proteinas_g", "grasas_g", "carbohidratos_g", "fibra_g", "kcal_estimadas", "grasas_saturadas_g", "azucares_g", "sal_g", "tamano_racion_asumido_g"):
        result[key] = _as_number(result[key])
    # Nunca sustituye Kcal publicada por una inferida.
    if published_kcal is not None:
        result["kcal_estimadas"] = None
    confidence = str(result.get("nivel_confianza") or "bajo").lower()
    normalized_confidence: ConfidenceLevel = cast(
        ConfidenceLevel, confidence if confidence in {"alto", "medio", "bajo"} else "bajo"
    )
    result["nivel_confianza"] = normalized_confidence
    result["justificacion_breve"] = str(result.get("justificacion_breve") or "Estimación sin justificación detallada.")[:500]
    assumptions = result.get("supuestos")
    if isinstance(assumptions, str):
        assumptions = [assumptions]
    result["supuestos"] = [str(item)[:300] for item in assumptions] if isinstance(assumptions, list) else []
    raw_indeterminate = result.get("campos_indeterminados")
    indeterminate: list[NutrientField] = []
    if allow_indeterminate and isinstance(raw_indeterminate, list):
        for field in raw_indeterminate:
            if not isinstance(field, str):
                continue
            normalized_field = "kcal" if field == "kcal_estimadas" else field
            if normalized_field in CORE_FIELDS:
                indeterminate.append(cast(NutrientField, normalized_field))
    result["campos_indeterminados"] = list(dict.fromkeys(indeterminate))

    unresolved = [
        field
        for field in required_fields
        if result[ESTIMATE_KEY_BY_FIELD[field]] is None and field not in indeterminate
    ]
    if unresolved:
        raise NutritionProviderError(
            "El modelo dejó sin estimar métricas que sí admiten una aproximación: "
            + ", ".join(unresolved)
        )
    has_numeric_estimate = any(
        result[ESTIMATE_KEY_BY_FIELD[field]] is not None for field in required_fields
    )
    if has_numeric_estimate and result["tamano_racion_asumido_g"] is None:
        raise NutritionProviderError("La estimación no declara el tamaño de ración asumido.")
    return cast(EstimateValues, result)


def _is_variable_dish(row: MenuRow) -> bool:
    """Detecta platos cuya composición depende explícitamente del comensal.

    Esta decisión se toma en código, no por el LLM. Así un modelo no puede usar
    ``null`` por mera cautela en una receta convencional; solo se permite para
    descripciones que anuncian buffet, selección o personalización.
    """
    text = " ".join(
        [row["name"], row.get("description") or "", *row.get("ingredients", [])]
    ).casefold()
    markers = (
        "a tu gusto",
        "buffet",
        "elige los ingredientes",
        "selecciona los ingredientes",
        "personalizable",
        "personaliza",
    )
    return any(marker in text for marker in markers)


def _missing_core_fields(row: MenuRow) -> tuple[NutrientField, ...]:
    """Devuelve las métricas centrales que no aporta la fuente oficial."""
    published = row.get("published_nutrition", {})
    return tuple(
        field for field in CORE_FIELDS if (published.get(field) or {}).get("value") is None
    )


def _estimate_is_reusable(row: MenuRow, stored: StoredEstimate) -> bool:
    """Comprueba versión y cobertura antes de aceptar una entrada de caché.

    Una estimación parcial antigua ya no bloquea nuevas llamadas. Los únicos
    huecos reutilizables son los declarados indeterminados para un plato cuya
    composición variable también haya sido verificada localmente.
    """
    if stored["prompt_version"] != NUTRITION_PROMPT_VERSION:
        return False
    values = stored["values"]
    declared = set(values.get("campos_indeterminados", [])) if _is_variable_dish(row) else set()
    return all(
        values.get(ESTIMATE_KEY_BY_FIELD[field]) is not None or field in declared
        for field in _missing_core_fields(row)
    )


def _prompt_for_dish(row: MenuRow) -> str:
    """Construye el contrato de entrada textual para estimar un plato.

    Args:
        row: Plato persistido con datos oficiales y metadatos de clasificación.

    Returns:
        Prompt en español que exige exclusivamente el objeto JSON esperado.

    Solo se envían datos gastronómicos públicos. La instrucción repite las reglas
    críticas —no inventar alérgenos ni reemplazar Kcal— porque la validación local
    puede proteger números, pero no verificar conocimiento culinario externo.
    """
    published = row.get("published_nutrition", {})
    official = {key: value.get("value") for key, value in published.items() if isinstance(value, dict)}
    return (
        f"Contrato de estimación nutricional v{NUTRITION_PROMPT_VERSION}. Estima de forma prudente la "
        "nutrición de una ración de restaurante. No inventes alérgenos ni modifiques los valores oficiales. "
        "Para cada métrica oficial ausente debes producir una aproximación numérica basada en una ración "
        "razonable y explicar el supuesto. Usa 0 cuando sea una estimación válida; no uses null por cautela. "
        "Solo puedes usar null si el campo composicion_variable de los datos es true y la cifra depende "
        "realmente de lo que el comensal elija. En ese caso incluye su nombre en campos_indeterminados. "
        "Devuelve exclusivamente JSON con estas claves: "
        "proteinas_g, grasas_g, carbohidratos_g, fibra_g, kcal_estimadas, grasas_saturadas_g, azucares_g, "
        "sal_g, tamano_racion_asumido_g, nivel_confianza, justificacion_breve, supuestos, "
        "campos_indeterminados. Debes indicar tamano_racion_asumido_g cuando estimes alguna cifra. "
        "En campos_indeterminados usa solo: kcal, proteinas_g, carbohidratos_g, grasas_g o fibra_g. "
        "kcal_estimadas debe ser null si Kcal oficial existe. Datos del plato:\n"
        + json.dumps(
            {
                "version_prompt": NUTRITION_PROMPT_VERSION,
                "nombre": row["name"],
                "descripcion": row.get("description"),
                "ingredientes": row.get("ingredients", []),
                "grupos": row.get("groups", []),
                "categorías": row.get("categories", []),
                "nutrición_oficial": official,
                "base_de_medida": row.get("nutrition_basis"),
                "composicion_variable": _is_variable_dish(row),
            },
            ensure_ascii=False,
        )
    )


def _ollama(prompt: str) -> tuple[JsonObject, str]:
    """Solicita JSON a un servidor Ollama compatible con la API local.

    Args:
        prompt: Instrucción y datos públicos de un único plato.

    Returns:
        Objeto JSON sin validar y nombre del modelo utilizado.

    Raises:
        NutritionProviderError: Ante red, timeout, forma de respuesta o JSON inválido.
    """
    model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        response = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "format": "json", "stream": False, "options": {"temperature": 0.1}},
            timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "90")),
        )
        response.raise_for_status()
        payload = cast(JsonObject, response.json())
        response_text = cast(str, payload["response"])
        return cast(JsonObject, json.loads(_strip_json_fence(response_text))), model
    except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise NutritionProviderError(f"Ollama no respondió con JSON válido: {exc}") from exc


def _gemini(prompt: str) -> tuple[JsonObject, str]:
    """Solicita JSON a Gemini mediante REST sin incorporar un SDK adicional.

    Args:
        prompt: Instrucción y datos públicos de un único plato.

    Returns:
        Objeto JSON sin validar y modelo configurado.

    Raises:
        NutritionProviderError: Si falta la clave o la respuesta HTTP/JSON no
            contiene la ruta esperada de Gemini.

    Modelo y clave proceden exclusivamente del entorno para no fijar credenciales
    en código y para permitir sustituir un modelo retirado sin desplegar cambios.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise NutritionProviderError("Falta GEMINI_API_KEY.")
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    request_body: JsonObject = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    try:
        # La cabecera evita incluir la clave en la URL y, por extensión, en
        # excepciones HTTP, proxys o registros de diagnóstico.
        response = requests.post(
            url,
            headers={"x-goog-api-key": api_key},
            json=request_body,
            timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "90")),
        )
        response.raise_for_status()
        payload = cast(JsonObject, response.json())
        candidates = cast(list[JsonObject], payload["candidates"])
        content = cast(JsonObject, candidates[0]["content"])
        parts = cast(list[JsonObject], content["parts"])
        text = cast(str, parts[0]["text"])
        return cast(JsonObject, json.loads(_strip_json_fence(text))), model
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "desconocido"
        if status == 404:
            raise NutritionProviderError(
                f"El modelo Gemini '{model}' no está disponible para esta clave (HTTP 404). "
                "Revise GEMINI_MODEL en las variables de GitHub."
            ) from exc
        raise NutritionProviderError(f"Gemini rechazó la solicitud (HTTP {status}): {exc}") from exc
    except (requests.RequestException, KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        raise NutritionProviderError(f"Gemini no respondió con JSON válido: {exc}") from exc


def enrich_missing_nutrition(
    database: MenuDatabase, rows: list[MenuRow], provider_name: str = "none"
) -> EnrichmentCounts:
    """Completa campos centrales ausentes reutilizando primero la caché.

    Args:
        database: Almacén donde consultar y guardar estimaciones por receta.
        rows: Platos disponibles, posiblemente repetidos entre restaurantes.
        provider_name: ``none``, ``ollama`` o ``gemini`` (sin distinguir mayúsculas).

    Returns:
        Contadores de recetas cacheadas, estimadas, omitidas y fallidas.

    Una comprensión indexada por ``content_hash`` deduplica recetas compartidas.
    Un fallo del proveedor afecta solo al plato actual y no impide generar el
    informe con los datos oficiales y la caché ya disponible.
    """
    provider_name = provider_name.lower()
    counts: EnrichmentCounts = {"cached": 0, "estimated": 0, "skipped": 0, "failed": 0}
    unique_rows: dict[str, MenuRow] = {row["content_hash"]: row for row in rows}
    for content_hash, row in unique_rows.items():
        published = row.get("published_nutrition", {})
        missing_fields = _missing_core_fields(row)
        if not missing_fields:
            counts["skipped"] += 1
            continue
        cached = database.get_estimate(content_hash)
        if cached is not None and _estimate_is_reusable(row, cached):
            counts["cached"] += 1
            continue
        if provider_name == "none":
            counts["skipped"] += 1
            continue
        official_kcal = (published.get("kcal") or {}).get("value")
        try:
            prompt = _prompt_for_dish(row)
            if provider_name == "ollama":
                raw, model = _ollama(prompt)
            elif provider_name == "gemini":
                raw, model = _gemini(prompt)
            else:
                raise NutritionProviderError(f"Proveedor LLM desconocido: {provider_name}")
            values = validate_estimate(
                raw,
                official_kcal,
                required_fields=missing_fields,
                allow_indeterminate=_is_variable_dish(row),
            )
            database.save_estimate(
                content_hash,
                provider_name,
                model,
                NUTRITION_PROMPT_VERSION,
                values,
            )
            counts["estimated"] += 1
        except NutritionProviderError as exc:
            # Un fallo LLM no invalida la extracción ni impide el informe con datos oficiales.
            # Los logs de Actions pueden ser públicos; no se incluye el nombre
            # del plato ni contenido del prompt en el diagnóstico.
            LOGGER.warning("No se pudo estimar un plato: %s", exc)
            counts["failed"] += 1
    LOGGER.info("LLM: caché=%s estimados=%s omitidos=%s fallidos=%s", *counts.values())
    return counts


def nutrient_value(row: MenuRow, field: NutrientField) -> tuple[float | None, NutrientSource]:
    """Resuelve una métrica dando prioridad absoluta a la publicación oficial.

    Args:
        row: Plato con nutrición oficial y estimación opcional.
        field: Una de las cinco métricas centrales comparables.

    Returns:
        Par ``(valor, procedencia)``. El valor es ``None`` cuando ninguna fuente
        lo aporta.
    """
    published = row.get("published_nutrition", {}).get(field)
    if isinstance(published, dict) and published.get("value") is not None:
        return _as_number(published["value"]), "publicado"
    estimate = row.get("estimate")
    estimated_field = "kcal_estimadas" if field == "kcal" else field
    value = _as_number(estimate.get(estimated_field) if estimate is not None else None)
    return (value, "estimado") if value is not None else (None, "no disponible")


def classify_dish(row: MenuRow) -> CourseKind:
    """Asigna un único hueco de menú; el grupo oficial tiene prioridad.

    Esto evita que una categoría como ``Arroz`` convierta un primer plato en
    guarnición. Cada plato solo puede ocupar uno de los cuatro cursos.

    Args:
        row: Plato con grupos oficiales y categorías descriptivas.

    Returns:
        Curso normalizado o ``other`` cuando no hay evidencia suficiente.
    """
    groups = " ".join(row.get("groups", [])).lower()
    if "postre" in groups:
        return "dessert"
    if any(token in groups for token in ("guarnici", "acompañ")):
        return "side"
    if any(token in groups for token in ("primero", "entrante", "aperitivo")):
        return "first"
    if any(token in groups for token in ("segundo", "plato único", "plato unico")):
        return "main"
    text = " ".join([*row.get("categories", []), row.get("name", "")]).lower()
    if any(token in text for token in ("postre", "helado", "fruta", "yogur")):
        return "dessert"
    if any(token in text for token in ("guarnici", "acompañ")):
        return "side"
    if any(token in text for token in ("primero", "entrante", "aperitivo", "cuchara", "ensalada", "sopa", "crema", "arroz", "pasta")):
        return "first"
    if any(token in text for token in ("principal", "carne", "pescado", "plancha", "wok", "poke")):
        return "main"
    return "other"


COURSE_LABELS: dict[CourseKind, str] = {
    "first": "Primero",
    "main": "Segundo",
    "side": "Guarnición",
    "dessert": "Postre",
    "other": "Plato",
}
MENU_COURSES: frozenset[CourseKind] = frozenset(("first", "main", "side", "dessert"))


def menu_structure_error(items: list[MenuRow]) -> str | None:
    """Devuelve el motivo por el que una selección no es un menú válido.

    La restricción se centraliza aquí para que el selector manual y las
    recomendaciones automáticas obedezcan exactamente la misma regla.

    Args:
        items: Platos de una combinación candidata.

    Returns:
        Motivo legible del rechazo o ``None`` si la estructura es válida.
    """
    kinds = [classify_dish(item) for item in items]
    unknown = [COURSE_LABELS[kind] for kind in kinds if kind not in MENU_COURSES]
    if unknown:
        return "Hay platos sin curso inequívoco: " + ", ".join(sorted(set(unknown))) + "."
    duplicates = [COURSE_LABELS[kind] for kind in MENU_COURSES if kinds.count(kind) > 1]
    if duplicates:
        return "Solo se permite un plato de cada curso: " + ", ".join(duplicates) + "."
    if _repeats_staple(items):
        return "La combinación repite una base (arroz, pasta, patata, cuscús o pan)."
    return None


def _repeats_staple(items: list[MenuRow]) -> bool:
    """Detecta si dos platos contienen la misma base de hidrato principal.

    Args:
        items: Platos ya clasificados de una combinación.

    Returns:
        ``True`` al repetir arroz, pasta, patata, cuscús o pan.

    Es una heurística textual conservadora; no afirma cantidades ni composición
    nutricional y solo evita recomendaciones evidentemente redundantes.
    """
    staples = ("arroz", "pasta", "patata", "cuscús", "cuscus", "pan")
    used: set[str] = set()
    for item in items:
        text = " ".join([item.get("name", ""), *item.get("ingredients", [])]).lower()
        current = {staple for staple in staples if staple in text}
        if used.intersection(current):
            return True
        used.update(current)
    return False


def _is_vegetarian(row: MenuRow) -> bool | None:
    """Infiere una señal vegetariana triestado mediante términos explícitos.

    Args:
        row: Plato con nombre, descripción e ingredientes publicados.

    Returns:
        ``False`` si aparece un término animal, ``True`` si aparece uno vegetal
        sin términos animales y ``None`` cuando el texto no permite decidir.

    Esta señal no sustituye información dietética ni de alérgenos del restaurante.
    """
    text = " ".join([row.get("name", ""), row.get("description") or "", *row.get("ingredients", [])]).lower()
    animal = ("pollo", "ternera", "cerdo", "jamón", "pavo", "atún", "salmón", "bacalao", "pescado", "merluza", "lubina", "carne", "gamba", "langost", "marisco")
    if any(token in text for token in animal):
        return False
    plant = ("veget", "tofu", "legumbre", "lenteja", "garbanzo", "alubia", "verdura", "ensalada", "seta")
    return True if any(token in text for token in plant) else None


def calculate_combination(items: list[MenuRow]) -> MenuCombination:
    """Calcula totales comparables para una combinación estructuralmente válida.

    Args:
        items: Selección no vacía de un mismo restaurante.

    Returns:
        Componentes, sumas, fuentes, confianza y señales vegetarianas.

    Raises:
        ValueError: Si hay cursos ambiguos/duplicados o una base repetida.
        IndexError: Si el llamador incumple la precondición de selección no vacía.

    Una suma queda en ``None`` si falta ese campo en cualquier componente. Sumar
    solo los valores disponibles aparentaría un total completo y sesgaría el ranking.
    """
    error = menu_structure_error(items)
    if error:
        raise ValueError(error)
    totals = cast(NutritionTotals, {})
    sources: set[NutrientSource] = set()
    for field in CORE_FIELDS:
        pairs = [nutrient_value(item, field) for item in items]
        available = [value for value, _ in pairs if value is not None]
        totals[field] = round(sum(available), 1) if len(available) == len(items) else None
        sources.update(source for _, source in pairs)
    estimate_used = "estimado" in sources
    # No hay unidad pública. Se pueden comparar internamente de forma orientativa,
    # nunca como gramos o raciones confirmadas.
    confidence: Literal["bajo", "medio"] = "bajo" if estimate_used else "medio"
    return {
        "restaurant_id": items[0]["restaurant_id"],
        "restaurant_name": items[0]["restaurant_name"],
        "components": [
            {
                "name": item["name"],
                "content_hash": item["content_hash"],
                "kind": classify_dish(item),
                "course": COURSE_LABELS[classify_dish(item)],
            }
            for item in items
        ],
        "totals": totals,
        "sources": sorted(sources),
        "confidence": confidence,
        "basis_note": "La web no publica si los valores son por ración o por 100 g; las sumas son comparativas y orientativas.",
        "vegetarian": all(_is_vegetarian(item) is True for item in items),
        "vegetarian_uncertain": any(_is_vegetarian(item) is None for item in items),
    }


def _enough_information(combo: MenuCombination, profile: RecommendationProfile) -> bool:
    """Comprueba que un candidato tenga las métricas exigidas por un perfil.

    Args:
        combo: Combinación calculada.
        profile: Objetivo de recomendación que se va a puntuar.

    Returns:
        ``True`` si la comparación puede hacerse sin sustituir ausencias por cero.
    """
    totals = combo["totals"]
    known = sum(totals[field] is not None for field in CORE_FIELDS)
    if profile == "high_protein":
        return totals["proteinas_g"] is not None and known >= 2
    if profile == "light":
        return totals["kcal"] is not None and known >= 2
    if profile == "recovery":
        return totals["proteinas_g"] is not None and totals["carbohidratos_g"] is not None
    return totals["kcal"] is not None and totals["proteinas_g"] is not None and known >= 2


def _score(
    combo: MenuCombination,
    profile: RecommendationProfile,
    calorie_target: float | None = None,
    protein_target: float | None = None,
) -> float:
    """Asigna una puntuación determinista según el objetivo nutricional.

    Args:
        combo: Candidato que ya superó ``_enough_information``.
        profile: Regla de ponderación solicitada.
        calorie_target: Objetivo energético usado solo por el perfil personalizado.
        protein_target: Objetivo proteico usado solo por el perfil personalizado.

    Returns:
        Puntuación relativa; solo es significativa frente a otros candidatos del
        mismo perfil.

    Los ceros de la función auxiliar no convierten datos ausentes en válidos: la
    precondición anterior decide primero qué campos mínimos deben existir.
    """
    value: Callable[[NutrientField], float] = lambda key: combo["totals"].get(key) or 0.0
    kcal, protein, carbs, fat, fiber = (value(key) for key in CORE_FIELDS)
    if profile == "high_protein":
        return protein * 12 - kcal * 0.04 + fiber * 0.8
    if profile == "light":
        return protein * 3 + fiber * 2 - kcal * 0.12 - fat * 0.5
    if profile == "recovery":
        return protein * 7 + carbs * 1.8 - fat * 1.1 - abs(kcal - 700) * 0.05
    if profile == "custom":
        return -abs(kcal - (calorie_target or kcal)) - abs(protein - (protein_target or protein)) * 12
    return protein * 4 + fiber * 2 - abs(kcal - 650) * 0.08 - abs(carbs - 70) * 0.06 - fat * 0.25


def _explanation(combo: MenuCombination, profile: RecommendationProfile) -> str:
    """Genera una justificación breve a partir de métricas, sin usar un LLM.

    Args:
        combo: Combinación ganadora.
        profile: Perfil que determinó su puntuación.

    Returns:
        Texto mostrado en email y dashboard.
    """
    totals = combo["totals"]
    protein = totals.get("proteinas_g")
    kcal = totals.get("kcal")
    if profile == "high_protein":
        return f"Prioriza proteína disponible ({protein:g} g) frente a las demás combinaciones comparables." if protein is not None else "Sin proteína comparable."
    if profile == "light":
        return f"Aporta {kcal:g} Kcal publicadas/estimadas comparables con buen equilibrio disponible." if kcal is not None else "Sin energía comparable."
    if profile == "recovery":
        return "Combina proteína e hidratos disponibles para una comida posterior al entrenamiento."
    return "Equilibra energía, proteína, hidratos, grasa y fibra según los datos disponibles."


def _candidate_combinations(rows: list[MenuRow]) -> list[MenuCombination]:
    """Enumera menús válidos sin mezclar sedes ni repetir cursos.

    Args:
        rows: Platos disponibles en la fecha seleccionada.

    Returns:
        Combinaciones calculadas que contienen exactamente un segundo y hasta un
        primero, una guarnición y un postre.

    Los cortes 6/8/4/3 limitan el producto cartesiano en menús anómalamente grandes.
    No alteran los platos almacenados; solo acotan el coste del ranking.
    """
    candidates: list[MenuCombination] = []
    by_restaurant: dict[int, list[MenuRow]] = defaultdict(list)
    for row in rows:
        by_restaurant[row["restaurant_id"]].append(row)
    for restaurant_rows in by_restaurant.values():
        classified: dict[CourseKind, list[MenuRow]] = defaultdict(list)
        for row in restaurant_rows:
            classified[classify_dish(row)].append(row)
        # No se usa una categoría ambigua como segundo: si el portal no permite
        # identificarlo, es más honesto no recomendar esa combinación.
        mains = classified["main"]
        firsts = classified["first"]
        sides = classified["side"]
        desserts = classified["dessert"]
        if not mains:
            continue
        # Límites para evitar una explosión combinatoria con menús muy extensos.
        for first, main, side, dessert in product(
            [None, *firsts[:6]], mains[:8], [None, *sides[:4]], [None, *desserts[:3]]
        ):
            selected = [item for item in (first, main, side, dessert) if item is not None]
            if not selected or len({item["content_hash"] for item in selected}) != len(selected):
                continue
            if menu_structure_error(selected):
                continue
            candidates.append(calculate_combination(selected))
    return candidates


def _best(
    candidates: list[MenuCombination],
    profile: RecommendationProfile,
    **targets: float | None,
) -> Recommendation | None:
    """Selecciona el máximo puntuado entre candidatos suficientemente informados.

    Args:
        candidates: Combinaciones de todos los restaurantes o de una sola sede.
        profile: Objetivo cuya regla de puntuación se aplica.
        **targets: Objetivos opcionales reenviados a ``_score``.

    Returns:
        Copia enriquecida del ganador o ``None`` cuando no se puede comparar.

    Se calcula la puntuación otra vez al construir el resultado para conservar la
    función de orden pura y no mutar los candidatos compartidos por otros perfiles.
    """
    valid = [item for item in candidates if _enough_information(item, profile)]
    if not valid:
        return None
    best = max(valid, key=lambda item: _score(item, profile, **targets))
    return cast(
        Recommendation,
        {
            **best,
            "profile": profile,
            "score": round(_score(best, profile, **targets), 2),
            "reason": _explanation(best, profile),
        },
    )


def recommend_daily(
    rows: list[MenuRow], calorie_target: float | None = None, protein_target: float | None = None
) -> Recommendations:
    """Calcula recomendaciones globales y una opción equilibrada por sede.

    Args:
        rows: Menú persistido de una fecha.
        calorie_target: Objetivo opcional del perfil personalizado.
        protein_target: Objetivo opcional del perfil personalizado.

    Returns:
        Ganadores por perfil, comparativa por restaurante y número de candidatos.

    La selección nunca se delega al modelo de lenguaje. El LLM puede aportar una
    métrica ausente, pero estas reglas siguen siendo inspeccionables y repetibles.
    """
    candidates = _candidate_combinations(rows)
    by_restaurant: dict[str, Recommendation | None] = {}
    for restaurant_id in sorted({item["restaurant_id"] for item in candidates}):
        own = [item for item in candidates if item["restaurant_id"] == restaurant_id]
        by_restaurant[str(restaurant_id)] = _best(own, "balanced")
    return {
        "balanced": _best(candidates, "balanced"),
        "high_protein": _best(candidates, "high_protein"),
        "light": _best(candidates, "light"),
        "recovery": _best(candidates, "recovery"),
        "custom": _best(candidates, "custom", calorie_target=calorie_target, protein_target=protein_target)
        if calorie_target is not None or protein_target is not None
        else None,
        "by_restaurant": by_restaurant,
        "candidate_count": len(candidates),
    }
