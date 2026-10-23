"""Contratos de datos compartidos por los módulos de Daily Menu Insights.

El proyecto intercambia diccionarios porque sus datos nacen como JSON, pasan por
SQLite y terminan serializados como HTML o CSV. Los ``TypedDict`` de este módulo
describen esa estructura sin sustituir los diccionarios por clases en tiempo de
ejecución; por tanto, no cambian el formato persistido ni las interfaces públicas.

Los tipos se concentran aquí para que scraper, persistencia, análisis y renderizado
usen el mismo vocabulario. Si cambia el contrato del portal, este archivo sirve
como inventario de los consumidores que deben revisarse.
"""

from __future__ import annotations

from typing import Literal, NotRequired, Protocol, TypeAlias, TypedDict


JsonScalar: TypeAlias = str | int | float | bool | None
"""Valor JSON que no contiene otros valores."""

JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
"""Valor aceptado por JSON, incluida la anidación recursiva."""

JsonObject: TypeAlias = dict[str, JsonValue]
"""Objeto JSON con claves de texto, tal como lo devuelve el portal."""

NutrientField: TypeAlias = Literal[
    "kcal", "proteinas_g", "carbohidratos_g", "grasas_g", "fibra_g"
]
EstimateNumericField: TypeAlias = Literal[
    "proteinas_g",
    "grasas_g",
    "carbohidratos_g",
    "fibra_g",
    "kcal_estimadas",
    "grasas_saturadas_g",
    "azucares_g",
    "sal_g",
    "tamano_racion_asumido_g",
]
ConfidenceLevel: TypeAlias = Literal["alto", "medio", "bajo"]
NutrientSource: TypeAlias = Literal["publicado", "estimado", "no disponible"]
ExtractionStatus: TypeAlias = Literal["success", "no_menu_published", "error"]
CourseKind: TypeAlias = Literal["first", "main", "side", "dessert", "other"]
RecommendationProfile: TypeAlias = Literal[
    "balanced", "high_protein", "light", "recovery", "custom"
]
GlobalRecommendationKey: TypeAlias = Literal[
    "balanced", "high_protein", "light", "recovery"
]


class RestaurantTarget(TypedDict):
    """Nombre corto y ubicación fija de uno de los restaurantes objetivo."""

    short_name: str
    location: str


class NutritionDatum(TypedDict):
    """Valor nutricional individual conservando el texto original y su origen."""

    raw: JsonValue
    value: float | None
    source: Literal["publicado", "ausente"]


class PublishedNutrition(TypedDict):
    """Campos nutricionales que se intentan leer de la fuente oficial."""

    kcal: NutritionDatum
    carbohidratos_g: NutritionDatum
    proteinas_g: NutritionDatum
    grasas_g: NutritionDatum
    fibra_g: NutritionDatum
    azucares_g: NutritionDatum
    sal_g: NutritionDatum


class EstimateValues(TypedDict):
    """Respuesta de un LLM después de validarla y normalizarla."""

    proteinas_g: float | None
    grasas_g: float | None
    carbohidratos_g: float | None
    fibra_g: float | None
    kcal_estimadas: float | None
    grasas_saturadas_g: float | None
    azucares_g: float | None
    sal_g: float | None
    tamano_racion_asumido_g: float | None
    nivel_confianza: ConfidenceLevel
    justificacion_breve: str
    supuestos: list[str]
    campos_indeterminados: list[NutrientField]


class DishRecord(TypedDict):
    """Receta normalizada producida por el scraper antes de persistirla."""

    content_hash: str
    source_dish_id: str | int | None
    normalized_name: str
    name: str
    description: str
    dish_type: JsonValue
    ingredients: list[str]
    allergens: list[str]
    published_nutrition: PublishedNutrition
    nutrition_basis: str
    source_payload: JsonObject
    groups: list[str]
    categories: list[str]
    menu_names: list[str]


class MutableDishRecord(TypedDict):
    """Variante usada mientras el scraper agrega apariciones de una receta."""

    content_hash: str
    source_dish_id: str | int | None
    normalized_name: str
    name: str
    description: str
    dish_type: JsonValue
    ingredients: list[str]
    allergens: list[str]
    published_nutrition: PublishedNutrition
    nutrition_basis: str
    source_payload: JsonObject
    groups: set[str]
    categories: set[str]
    menu_names: set[str]


class RestaurantExtraction(TypedDict):
    """Resultado de una sede para una fecha concreta."""

    status: ExtractionStatus
    detail: str
    dishes: list[DishRecord]
    name: NotRequired[str]
    location: NotRequired[str]


class RestaurantRecord(TypedDict):
    """Datos mínimos aceptados por la tabla ``restaurants``."""

    id: int
    name: str
    location: str


class StoredEstimate(TypedDict):
    """Estimación recuperada de SQLite junto con sus metadatos de proveedor."""

    content_hash: str
    provider: str
    model: str
    prompt_version: int
    confidence: str
    assumptions: str | None
    estimated_at: str
    values: EstimateValues


class MenuRow(TypedDict):
    """Fila de menú enriquecida que consumen nutrición, email y dashboard."""

    menu_date: str
    restaurant_id: int
    restaurant_name: str
    menu_name: str | None
    groups: list[str]
    categories: list[str]
    content_hash: str
    source_dish_id: str | None
    name: str
    description: str | None
    dish_type: str | None
    ingredients: list[str]
    allergens: list[str]
    published_nutrition: PublishedNutrition
    nutrition_basis: str
    estimate_provider: str | None
    estimate_model: str | None
    estimate_prompt_version: int | None
    estimate_confidence: str | None
    estimate: EstimateValues | None


class ExtractionSummary(TypedDict):
    """Último estado registrado para un restaurante y una fecha."""

    restaurant_id: int
    name: str
    status: ExtractionStatus
    dish_count: int
    detail: str | None
    extracted_at: str


class CombinationComponent(TypedDict):
    """Plato y curso que forman parte de una combinación candidata."""

    name: str
    content_hash: str
    kind: CourseKind
    course: str


class NutritionTotals(TypedDict):
    """Suma nutricional; ``None`` indica que falta al menos un componente."""

    kcal: float | None
    proteinas_g: float | None
    carbohidratos_g: float | None
    grasas_g: float | None
    fibra_g: float | None


class MenuCombination(TypedDict):
    """Combinación válida antes de puntuarla para un perfil."""

    restaurant_id: int
    restaurant_name: str
    components: list[CombinationComponent]
    totals: NutritionTotals
    sources: list[NutrientSource]
    confidence: Literal["bajo", "medio"]
    basis_note: str
    vegetarian: bool
    vegetarian_uncertain: bool


class Recommendation(MenuCombination):
    """Combinación elegida, con puntuación y explicación reproducibles."""

    profile: RecommendationProfile
    score: float
    reason: str


class Recommendations(TypedDict):
    """Conjunto de perfiles calculados para un menú diario."""

    balanced: Recommendation | None
    high_protein: Recommendation | None
    light: Recommendation | None
    recovery: Recommendation | None
    custom: Recommendation | None
    by_restaurant: dict[str, Recommendation | None]
    candidate_count: int


class EnrichmentCounts(TypedDict):
    """Contadores del proceso opcional de enriquecimiento por LLM."""

    cached: int
    estimated: int
    skipped: int
    failed: int


class PipelineArgs(Protocol):
    """Atributos que ``run_pipeline`` necesita del ``Namespace`` de argparse.

    El protocolo evita tipar el argumento como un contenedor dinámico genérico.
    ``argparse.Namespace`` lo satisface estructuralmente tras ``parse_args``.
    """

    date: str
    database: str
    url: str
    attempts: int
    retry_seconds: int
    provider: str
    no_llm: bool
    preview: str | None
    site_dir: str | None
    send_email: bool
    force_email: bool
    allow_partial: bool
    export_csv: str | None
    backup: str | None
    verbose: bool
