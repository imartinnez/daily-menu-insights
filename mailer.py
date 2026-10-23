"""Presentación del informe diario y entrega SMTP idempotente.

El módulo produce dos superficies a partir de las mismas filas: un correo breve,
compatible con clientes conservadores, y un informe HTML autónomo para navegador.
No calcula recomendaciones ni modifica datos nutricionales; únicamente conserva y
expone la procedencia que ya llega desde SQLite.
"""

from __future__ import annotations

import html
import os
import re
import smtplib
import ssl
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from database import MenuDatabase
from models import (
    CourseKind,
    EstimateNumericField,
    ExtractionSummary,
    GlobalRecommendationKey,
    MenuRow,
    NutrientField,
    NutrientSource,
    Recommendation,
    Recommendations,
)
from nutrition import COURSE_LABELS, classify_dish, nutrient_value


SPANISH_DAYS: tuple[str, ...] = (
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"
)
SPANISH_MONTHS: tuple[str, ...] = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
PROFILE_META: tuple[tuple[GlobalRecommendationKey, str, str], ...] = (
    ("balanced", "Mejor menú equilibrado", "Equilibrado"),
    ("high_protein", "Mejor opción alta en proteínas", "Alto en proteínas"),
    ("light", "Mejor opción ligera", "Ligero"),
    ("recovery", "Mejor opción post-entrenamiento", "Post-entrenamiento"),
)
CORE_METRICS: tuple[tuple[NutrientField, str, str], ...] = (
    ("kcal", "Calorías", "kcal"),
    ("proteinas_g", "Proteína", "g"),
    ("carbohidratos_g", "Hidratos", "g"),
    ("grasas_g", "Grasas", "g"),
    ("fibra_g", "Fibra", "g"),
)
CategoryFilter = Literal["all", "first", "main", "side", "dessert", "other"]


@dataclass(frozen=True)
class RestaurantView:
    """Identidad de presentación derivada de los datos privados de ejecución."""

    identifier: int
    name: str
    slug: str


@dataclass(frozen=True)
class DailyReport:
    """Artefactos inmutables de una ejecución diaria.

    ``html`` es el cuerpo compacto del correo. ``interactive_html`` contiene todos
    los platos y la interacción para navegador. ``report_url`` solo existe cuando
    se configuró una base HTTPS válida; su ausencia hace que SMTP adjunte el informe.
    """

    menu_date: str
    subject: str
    html: str
    text: str
    report_key: str
    interactive_html: str
    report_url: str | None


def human_date(value: str) -> str:
    """Convierte una fecha ISO en una fecha larga española."""
    parsed = date.fromisoformat(value)
    return (
        f"{SPANISH_DAYS[parsed.weekday()].capitalize()}, {parsed.day} de "
        f"{SPANISH_MONTHS[parsed.month - 1]} de {parsed.year}"
    )


def number(value: float | None) -> str:
    """Muestra como máximo un decimal y usa coma decimal en la interfaz."""
    if value is None:
        return "—"
    rendered = f"{value:.1f}".rstrip("0").rstrip(".")
    return rendered.replace(".", ",")


def _clean(value: object) -> str:
    """Normaliza texto de la fuente sin interpretar su contenido como HTML."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.removeprefix(". ")


def _escape(value: object) -> str:
    """Limpia y escapa una cadena externa para un contexto de texto HTML."""
    return html.escape(_clean(value), quote=True)


def _report_title() -> str:
    """Obtiene una marca genérica configurable sin acoplarla a la fuente."""
    return _clean(os.getenv("REPORT_TITLE", "Daily Menu Insights"))


def _source_label() -> str:
    """Devuelve una atribución neutra; la URL privada nunca se renderiza."""
    return _clean(os.getenv("MENU_SOURCE_LABEL", "Proveedor del menú"))


def _slug(value: str, identifier: int) -> str:
    """Crea anclas estables sin revelar identificadores fuera del HTML generado."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    return slug or f"restaurant-{identifier}"


def _restaurant_views(
    rows: list[MenuRow], extraction: list[ExtractionSummary]
) -> list[RestaurantView]:
    """Deriva sedes de SQLite en vez de publicar nombres e IDs en el código."""
    names: dict[int, str] = {
        item["restaurant_id"]: _clean(item["name"]) for item in extraction
    }
    for row in rows:
        names.setdefault(row["restaurant_id"], _clean(row["restaurant_name"]))
    return [
        RestaurantView(identifier, name, _slug(name, identifier))
        for identifier, name in sorted(names.items(), key=lambda item: item[0])
    ]


def _report_url(base_url: str | None, menu_date: str) -> str | None:
    """Construye la URL diaria y rechaza configuraciones inseguras o ambiguas.

    Se exige HTTPS y una URL sin credenciales, query ni fragmento para que los
    enlaces del correo nunca parezcan funcionales cuando no lo son.
    """
    if not base_url or not base_url.strip():
        return None
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("REPORT_BASE_URL debe ser una URL HTTPS sin credenciales, query ni fragmento.")
    path = f"{parsed.path.rstrip('/')}/{menu_date}/"
    return urlunsplit(("https", parsed.netloc, path, "", ""))


def _metric(row: MenuRow, field: NutrientField) -> tuple[str, NutrientSource]:
    """Devuelve el texto ya formateado y su procedencia sin alterar la cifra."""
    value, source = nutrient_value(row, field)
    return number(value), source


def _source_mark(source: NutrientSource) -> str:
    """Crea un indicador textual accesible; el significado no depende del color."""
    if source == "publicado":
        return '<span class="origin official" title="Valor publicado" aria-label="Valor publicado">●</span>'
    if source == "estimado":
        return '<span class="origin estimated" title="Valor estimado con IA" aria-label="Valor estimado con IA">*</span>'
    return ""


def _combo_signature(combo: Recommendation) -> tuple[int, tuple[str, ...]]:
    """Identifica menús idénticos para no repetirlos por cada objetivo ganador."""
    hashes = tuple(sorted(component["content_hash"] for component in combo["components"]))
    return combo["restaurant_id"], hashes


def _grouped_recommendations(
    recommendations: Recommendations,
) -> list[tuple[Recommendation, list[str]]]:
    """Agrupa perfiles que eligieron exactamente la misma combinación."""
    grouped: dict[tuple[int, tuple[str, ...]], tuple[Recommendation, list[str]]] = {}
    for key, _title, badge in PROFILE_META:
        combo = recommendations.get(key)
        if combo is None:
            continue
        signature = _combo_signature(combo)
        if signature not in grouped:
            grouped[signature] = (combo, [])
        grouped[signature][1].append(badge)
    return list(grouped.values())


def _combo_dishes(combo: Recommendation) -> str:
    """Compone una línea compacta respetando el orden natural de los cursos."""
    order: dict[CourseKind, int] = {"first": 0, "main": 1, "side": 2, "dessert": 3, "other": 4}
    items = sorted(combo["components"], key=lambda item: order[item["kind"]])
    return " · ".join(_clean(item["name"]) for item in items)


def _combo_metrics(combo: Recommendation) -> str:
    """Resume las tres métricas decisivas sin abreviaturas crípticas."""
    totals = combo["totals"]
    return (
        f"{number(totals.get('kcal'))} kcal · "
        f"{number(totals.get('proteinas_g'))} g proteína · "
        f"{number(totals.get('carbohidratos_g'))} g hidratos"
    )


def _recommendation_card(combo: Recommendation, badges: list[str]) -> str:
    """Renderiza una recomendación editorial; varios objetivos comparten tarjeta."""
    badge_html = "".join(f'<span class="badge">{_escape(label)}</span>' for label in badges)
    return f"""
    <div class="recommendation-card">
      <div class="badges">{badge_html}</div>
      <h3>{_escape(combo['restaurant_name'])}</h3>
      <p class="dish-line">{_escape(_combo_dishes(combo))}</p>
      <p class="hero-metrics">{_escape(_combo_metrics(combo))}</p>
      <p class="reason">{_escape(combo['reason'])}</p>
    </div>"""


def _status_map(extraction: list[ExtractionSummary]) -> dict[int, ExtractionSummary]:
    """Indexa el estado más reciente para representar también sedes sin platos."""
    return {item["restaurant_id"]: item for item in extraction}


def _restaurant_rows(rows: list[MenuRow]) -> dict[int, list[MenuRow]]:
    """Agrupa platos por sede conservando el orden recuperado de SQLite."""
    result: dict[int, list[MenuRow]] = defaultdict(list)
    seen: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        restaurant_id = row["restaurant_id"]
        if row["content_hash"] in seen[restaurant_id]:
            continue
        seen[restaurant_id].add(row["content_hash"])
        result[restaurant_id].append(row)
    return result


def _email_restaurant_card(
    restaurant_id: int,
    name: str,
    slug: str,
    dishes: list[MenuRow],
    recommendation: Recommendation | None,
    status: ExtractionSummary | None,
    report_url: str | None,
) -> str:
    """Crea una tarjeta de sede con enlace real o alternativa explícita."""
    available = bool(dishes)
    if available:
        count_text = f"{len(dishes)} platos disponibles"
        choice = _combo_dishes(recommendation) if recommendation else "Consulta el menú completo"
        summary = _combo_metrics(recommendation) if recommendation else "Sin combinación comparable"
        lead_label = "Mejor opción:"
    else:
        count_text = "Menú pendiente de publicación"
        detail = _clean(status.get("detail")) if status else ""
        choice = detail or "No se han publicado platos para esta fecha."
        summary = "Sin inventar opciones ni métricas"
        lead_label = "Estado:"
    if report_url:
        href = f"{report_url}#{slug}"
        action = (
            f'<a class="button" href="{html.escape(href, quote=True)}">'
            f'Ver menú de {_escape(name)} →</a>'
        )
    else:
        action = '<p class="attachment-note">Disponible en el informe HTML adjunto</p>'
    state_class = "restaurant-card" if available else "restaurant-card pending"
    return f"""
    <div class="{state_class}">
      <p class="restaurant-kicker">{_escape(count_text)}</p>
      <h3>{_escape(name)}</h3>
      <p class="best"><b>{lead_label}</b> {_escape(choice)}</p>
      <p class="restaurant-metrics">{_escape(summary)}</p>
      {action}
    </div>"""


EMAIL_CSS = """
body{margin:0;padding:0;background:#f3f4f5;color:#24272b;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}
.wrapper{width:100%;background:#f3f4f5;padding:22px 0}.shell{width:100%;max-width:680px;margin:0 auto;background:#fff}
.header{padding:30px 34px 24px;border-top:5px solid #c8102e}.brand{margin:0;color:#c8102e;font-size:11px;line-height:1.4;font-weight:800;letter-spacing:1.7px}
h1{margin:8px 0 8px;color:#202327;font-size:30px;line-height:1.12}.date{margin:0;color:#5f666d;font-size:15px}.availability{margin:14px 0 0;color:#70777e;font-size:12px}
.content{padding:0 34px 32px}.section{padding-top:26px}.section-label{margin:0 0 5px;color:#c8102e;font-size:11px;font-weight:800;letter-spacing:1.35px;text-transform:uppercase}
h2{margin:0 0 16px;font-size:21px;line-height:1.3}.recommendation-card{margin:0 0 12px;padding:20px;border:1px solid #e4e6e8;border-left:4px solid #c8102e;border-radius:9px;background:#fff}
.badges{margin-bottom:8px}.badge{display:inline-block;margin:0 5px 5px 0;padding:5px 8px;border-radius:999px;background:#fcebed;color:#a80d27;font-size:10px;font-weight:800;text-transform:uppercase}
.recommendation-card h3,.restaurant-card h3{margin:0;color:#202327;font-size:18px}.dish-line{margin:8px 0;color:#4d5359;font-size:13px;line-height:1.55}.hero-metrics{margin:13px 0 7px;color:#202327;font-size:16px;font-weight:800;line-height:1.45}.reason{margin:0;color:#727980;font-size:12px;line-height:1.5}
.restaurant-card{margin:0 0 10px;padding:18px;border-radius:9px;background:#f7f8f8}.restaurant-card.pending{background:#fafafa;color:#747a80}.restaurant-kicker{margin:0 0 5px;color:#66706d;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.45px}
.best{margin:9px 0 5px;font-size:13px;line-height:1.5}.restaurant-metrics{margin:0 0 13px;color:#596169;font-size:12px}.button{display:inline-block;padding:10px 13px;border-radius:5px;background:#c8102e;color:#fff!important;text-decoration:none;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.35px}
.attachment-note{margin:10px 0 0;color:#8b434e;font-size:11px;font-weight:700}.footer{padding:22px 34px 28px;border-top:1px solid #e6e7e9;color:#6c7379;font-size:11px;line-height:1.65}.footer a{color:#9f0c24}
@media only screen and (max-width:620px){.wrapper{padding:0}.header{padding:25px 21px 20px}.content{padding:0 21px 25px}.footer{padding:20px 21px 24px}h1{font-size:27px}.hero-metrics{font-size:15px}.recommendation-card{padding:17px}.restaurant-card{padding:16px}}
"""


def _build_email_html(
    menu_date: str,
    rows: list[MenuRow],
    extraction: list[ExtractionSummary],
    recommendations: Recommendations,
    report_url: str | None,
) -> str:
    """Construye el resumen ejecutivo sin incluir listados completos de platos."""
    grouped_rows = _restaurant_rows(rows)
    statuses = _status_map(extraction)
    restaurants = _restaurant_views(rows, extraction)
    available_count = sum(bool(grouped_rows.get(item.identifier)) for item in restaurants)
    recommendation_cards = "".join(
        _recommendation_card(combo, badges)
        for combo, badges in _grouped_recommendations(recommendations)
    ) or '<p class="reason">No hay datos suficientes para recomendaciones comparables.</p>'
    restaurant_cards = "".join(
        _email_restaurant_card(
            item.identifier,
            item.name,
            item.slug,
            grouped_rows.get(item.identifier, []),
            recommendations.get("by_restaurant", {}).get(str(item.identifier)),
            statuses.get(item.identifier),
            report_url,
        )
        for item in restaurants
    )
    full_report_link = (
        f'<br><a href="{html.escape(report_url, quote=True)}">Informe nutricional completo</a>'
        if report_url else "<br>El informe nutricional completo se adjunta como archivo HTML."
    )
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_escape(_report_title())}</title><style>{EMAIL_CSS}</style></head>
<body><div class="wrapper"><div class="shell">
  <div class="header"><p class="brand">{_escape(_report_title().upper())}</p><h1>¿Qué comemos hoy?</h1><p class="date">{_escape(human_date(menu_date))}</p><p class="availability">{available_count} restaurantes disponibles</p></div>
  <div class="content">
    <div class="section"><p class="section-label">Lo mejor de hoy</p><h2>Recomendaciones de un vistazo</h2>{recommendation_cards}</div>
    <div class="section"><p class="section-label">Dónde comer hoy</p><h2>Compara y abre cada menú</h2>{restaurant_cards}</div>
  </div>
  <div class="footer">Fuente: {_escape(_source_label())} · Actualización: {_escape(human_date(menu_date))}.<br>Los valores marcados como estimados proceden del modelo nutricional y no sustituyen datos oficiales.{full_report_link}</div>
</div></div></body></html>"""


def _secondary_value(row: MenuRow, field: EstimateNumericField) -> str:
    """Lee una métrica secundaria estimada; nunca la presenta como oficial."""
    estimate = row.get("estimate")
    return number(estimate.get(field) if estimate is not None else None)


def _dish_card(row: MenuRow) -> str:
    """Renderiza una ficha escaneable y desplaza el contexto a ``details``."""
    course = classify_dish(row)
    groups = row.get("groups") or row.get("categories") or [COURSE_LABELS[course]]
    metric_html = "".join(
        f'<div class="metric"><span>{_escape(label)}</span><strong>{value}</strong><small>{unit if value != "—" else ""}{_source_mark(source)}</small></div>'
        for field, label, unit in CORE_METRICS
        for value, source in [_metric(row, field)]
    )
    ingredients = row.get("ingredients", [])
    allergens = row.get("allergens", [])
    allergens_html = (
        "".join(f'<span class="allergen">{_escape(item)}</span>' for item in allergens)
        if allergens else '<span class="muted">No publicados</span>'
    )
    estimate = row.get("estimate")
    provenance = "Sin estimaciones de IA"
    if estimate is not None and any(nutrient_value(row, field)[1] == "estimado" for field, _label, _unit in CORE_METRICS):
        assumptions = "; ".join(_clean(item) for item in estimate.get("supuestos", []))
        provenance = (
            f"Estimación {row.get('estimate_provider') or 'IA'} · modelo {row.get('estimate_model') or 'no registrado'} · "
            f"confianza {estimate.get('nivel_confianza', 'baja')}"
            + (f" · {assumptions}" if assumptions else "")
        )
    density_value, _density_source = nutrient_value(row, "proteinas_g")
    kcal_value, _kcal_source = nutrient_value(row, "kcal")
    density = (density_value / kcal_value * 100) if density_value is not None and kcal_value and kcal_value > 0 else -1
    protein_value, _protein_source = nutrient_value(row, "proteinas_g")
    description = _clean(row.get("description"))
    return f"""
    <article class="dish-card" data-course="{course}" data-name="{_escape(row['name'].casefold())}" data-protein="{protein_value if protein_value is not None else -1}" data-kcal="{kcal_value if kcal_value is not None else 999999}" data-density="{density:.6f}">
      <div class="dish-heading"><div><p class="dish-type">{_escape(' · '.join(groups))}</p><h3>{_escape(row['name'])}</h3></div></div>
      <div class="metrics">{metric_html}</div>
      <details><summary>Ver ingredientes, alérgenos y procedencia</summary><div class="detail-grid">
        <div><h4>Ingredientes</h4><p>{_escape(', '.join(ingredients)) if ingredients else '<span class="muted">No publicados</span>'}</p></div>
        <div><h4>Alérgenos</h4><div class="allergens">{allergens_html}</div></div>
        {f'<div class="wide"><h4>Descripción</h4><p>{_escape(description)}</p></div>' if description else ''}
        <div><h4>Valores secundarios estimados</h4><p>Saturadas: {_secondary_value(row, 'grasas_saturadas_g')} g · Azúcares: {_secondary_value(row, 'azucares_g')} g · Sal: {_secondary_value(row, 'sal_g')} g</p></div>
        <div><h4>Procedencia</h4><p>{_escape(provenance)}</p></div>
      </div></details>
    </article>"""


INTERACTIVE_CSS = """
:root{--red:#c8102e;--ink:#202327;--muted:#687078;--line:#e2e4e6;--paper:#fff;--canvas:#f4f5f5;--green:#2f6d58}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--canvas);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;line-height:1.45}
button,select,summary{font:inherit}.page{max-width:1120px;margin:0 auto;padding:30px 24px 60px}.masthead{padding:31px 34px;border-top:5px solid var(--red);border-radius:12px 12px 0 0;background:var(--paper)}
.brand{margin:0;color:var(--red);font-size:11px;font-weight:850;letter-spacing:1.7px}.masthead h1{margin:8px 0 7px;font-size:34px;line-height:1.12}.masthead .date{margin:0;color:var(--muted)}
.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0 22px}.summary-card{padding:17px;border-radius:9px;background:#fff}.summary-card small{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.55px}.summary-card strong{display:block;margin-top:4px;font-size:16px}
.restaurant-tabs,.category-tabs{display:flex;gap:8px;overflow-x:auto;scrollbar-width:thin}.restaurant-tabs{position:sticky;top:0;z-index:5;margin:0;padding:13px;background:#fff;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.tab{flex:0 0 auto;min-height:42px;padding:9px 15px;border:1px solid var(--line);border-radius:999px;background:#fff;color:#4b5157;cursor:pointer;font-weight:750}.tab:hover,.tab:focus-visible{border-color:#aeb3b7}.tab.active{border-color:var(--red);background:var(--red);color:#fff}
.panel{padding:27px 31px 35px;background:#fff}.panel[hidden]{display:none}.restaurant-intro{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:19px}.restaurant-intro h2{margin:0;font-size:27px}.restaurant-intro p{margin:4px 0 0;color:var(--muted)}
.controls{display:flex;justify-content:space-between;gap:15px;align-items:center;margin:0 0 18px}.category-tab{flex:0 0 auto;min-height:38px;padding:7px 11px;border:0;border-bottom:2px solid transparent;background:transparent;color:#626970;cursor:pointer;font-weight:700}.category-tab.active{border-color:var(--red);color:var(--red)}.category-tab span{margin-left:4px;color:#969ca1;font-size:11px}
.sort{min-height:40px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:#444a50}.dish-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.dish-card{min-width:0;padding:19px;border:1px solid var(--line);border-radius:10px;background:#fff}.dish-card.is-hidden{display:none}.dish-type{margin:0 0 4px;color:var(--green);font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.65px}.dish-card h3{overflow-wrap:anywhere;margin:0;font-size:18px;line-height:1.27}
.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px;margin:16px 0 12px}.metric{min-width:0;padding:9px 5px;border-radius:6px;background:#f5f6f6;text-align:center}.metric span{display:block;overflow:hidden;color:var(--muted);font-size:9px;text-overflow:ellipsis;text-transform:uppercase}.metric strong{display:inline-block;margin-top:3px;font-variant-numeric:tabular-nums;font-size:17px}.metric small{margin-left:2px;color:#727980;font-size:10px}.origin{margin-left:3px;font-weight:900}.official{color:var(--green)}.estimated{color:#9d5b10}
details{margin-top:8px;border-top:1px solid #eceeef;padding-top:10px}summary{cursor:pointer;color:#565d63;font-size:12px;font-weight:700}summary:hover{color:var(--red)}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:13px;padding:13px;border-radius:7px;background:#fafafa}.detail-grid h4{margin:0 0 4px;font-size:11px;text-transform:uppercase;color:#646b71}.detail-grid p{margin:0;color:#50565c;font-size:12px;line-height:1.55}.detail-grid .wide{grid-column:1/-1}.allergen{display:inline-block;margin:0 4px 4px 0;padding:4px 7px;border-radius:4px;background:#f4e8ea;color:#781325;font-size:10px}.muted{color:#8b9196}
.empty-state{padding:48px 20px;border:1px dashed #d6d9db;border-radius:9px;background:#fafafa;text-align:center}.empty-state h3{margin:0 0 7px}.empty-state p{margin:0;color:var(--muted)}.no-results{display:none;margin:25px 0;color:var(--muted);text-align:center}.legend{margin-top:18px;padding:15px;border-radius:8px;background:#f7f8f8;color:#666d73;font-size:12px}.legend .origin{margin-left:8px}.footer{padding:24px 31px;border-top:1px solid var(--line);background:#fff;color:#747b81;font-size:11px}.footer a{color:#9f0c24}
@media(max-width:760px){.page{padding:0 0 35px}.masthead{border-radius:0;padding:25px 20px 21px}.masthead h1{font-size:29px}.summary{grid-template-columns:1fr;margin:10px 12px 16px}.restaurant-tabs{padding:10px 12px}.panel{padding:23px 16px 30px}.restaurant-intro{display:block}.restaurant-intro h2{font-size:24px}.controls{display:block}.category-tabs{margin:0 -3px 12px}.sort{width:100%}.dish-list{grid-template-columns:1fr}.dish-card{padding:16px}.metrics{grid-template-columns:repeat(3,1fr)}.metric:nth-child(n+4){grid-column:span 1}.detail-grid{grid-template-columns:1fr}.detail-grid .wide{grid-column:auto}.footer{padding:21px 18px}.tab{min-height:44px}}
"""


INTERACTIVE_JS = """
(() => {
  const tabs = [...document.querySelectorAll('[data-restaurant-tab]')];
  const panels = [...document.querySelectorAll('[data-restaurant-panel]')];
  const valid = new Set(tabs.map(tab => tab.dataset.restaurantTab));
  function activateRestaurant(slug, updateHash = true) {
    if (!valid.has(slug)) slug = tabs[0]?.dataset.restaurantTab;
    tabs.forEach(tab => { const active = tab.dataset.restaurantTab === slug; tab.classList.toggle('active', active); tab.setAttribute('aria-selected', String(active)); });
    panels.forEach(panel => { panel.hidden = panel.dataset.restaurantPanel !== slug; });
    if (updateHash && slug) history.replaceState(null, '', `#${slug}`);
  }
  tabs.forEach(tab => tab.addEventListener('click', () => activateRestaurant(tab.dataset.restaurantTab)));
  addEventListener('hashchange', () => activateRestaurant(location.hash.slice(1), false));
  document.querySelectorAll('[data-restaurant-panel]').forEach(panel => {
    const categoryButtons = [...panel.querySelectorAll('[data-category]')];
    const list = panel.querySelector('.dish-list');
    const cards = list ? [...list.querySelectorAll('.dish-card')] : [];
    const empty = panel.querySelector('.no-results');
    let category = 'all';
    function filter() {
      let visible = 0;
      cards.forEach(card => { const show = category === 'all' || card.dataset.course === category; card.classList.toggle('is-hidden', !show); if (show) visible += 1; });
      if (empty) empty.style.display = visible ? 'none' : 'block';
    }
    categoryButtons.forEach(button => button.addEventListener('click', () => { category = button.dataset.category; categoryButtons.forEach(item => { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); }); filter(); }));
    panel.querySelector('[data-sort]')?.addEventListener('change', event => {
      const mode = event.target.value;
      cards.sort((a, b) => mode === 'protein' ? Number(b.dataset.protein) - Number(a.dataset.protein) : mode === 'kcal' ? Number(a.dataset.kcal) - Number(b.dataset.kcal) : mode === 'density' ? Number(b.dataset.density) - Number(a.dataset.density) : Number(a.dataset.original) - Number(b.dataset.original));
      cards.forEach(card => list.appendChild(card));
    });
    cards.forEach((card, index) => card.dataset.original = String(index));
  });
  activateRestaurant(location.hash.slice(1), false);
})();
"""


def _category_buttons(dishes: list[MenuRow]) -> str:
    """Crea filtros solo para categorías presentes, además de ``Todos``."""
    definitions: tuple[tuple[CategoryFilter, str], ...] = (
        ("all", "Todos"), ("first", "Primeros"), ("main", "Segundos"),
        ("side", "Guarniciones"), ("dessert", "Postres"), ("other", "Otros"),
    )
    counts: dict[CourseKind, int] = defaultdict(int)
    for dish in dishes:
        counts[classify_dish(dish)] += 1
    buttons: list[str] = []
    for key, label in definitions:
        count = len(dishes) if key == "all" else counts[key]
        if key != "all" and count == 0:
            continue
        active = " active" if key == "all" else ""
        buttons.append(
            f'<button class="category-tab{active}" type="button" aria-pressed="{"true" if key == "all" else "false"}" data-category="{key}">{label}<span>{count}</span></button>'
        )
    return "".join(buttons)


def _restaurant_panel(
    identifier: int,
    name: str,
    slug: str,
    dishes: list[MenuRow],
    status: ExtractionSummary | None,
    active: bool,
) -> str:
    """Construye un panel aislado; JS nunca necesita descargar datos adicionales."""
    hidden = "" if active else " hidden"
    if not dishes:
        detail = _clean(status.get("detail")) if status else "La fuente no devolvió un menú para esta fecha."
        return f"""<section class="panel" id="panel-{slug}" role="tabpanel" data-restaurant-panel="{slug}" aria-labelledby="title-{slug}"{hidden}><div class="restaurant-intro"><div><h2 id="title-{slug}">{_escape(name)}</h2><p>Menú no disponible</p></div></div><div class="empty-state"><h3>Menú pendiente de publicación</h3><p>{_escape(detail)}</p></div></section>"""
    cards = "".join(_dish_card(dish) for dish in dishes)
    return f"""<section class="panel" id="panel-{slug}" role="tabpanel" data-restaurant-panel="{slug}" aria-labelledby="title-{slug}"{hidden}>
      <div class="restaurant-intro"><div><h2 id="title-{slug}">{_escape(name)}</h2><p>{len(dishes)} platos únicos disponibles</p></div></div>
      <div class="controls"><div class="category-tabs" aria-label="Filtrar categorías">{_category_buttons(dishes)}</div>
        <label><span class="muted">Ordenar: </span><select class="sort" data-sort><option value="original">Orden original</option><option value="protein">Mayor proteína</option><option value="kcal">Menor energía</option><option value="density">Mayor densidad proteica</option></select></label></div>
      <div class="dish-list">{cards}</div><p class="no-results">No hay platos en esta categoría.</p>
      <div class="legend"><b>Procedencia:</b><span class="origin official">●</span> valor publicado <span class="origin estimated">*</span> estimación con IA · <b>—</b> dato desconocido. La fuente no aclara si todas las cifras son por ración o por 100 g.</div>
    </section>"""


def _build_interactive_html(
    menu_date: str,
    rows: list[MenuRow],
    extraction: list[ExtractionSummary],
    recommendations: Recommendations,
) -> str:
    """Genera la aplicación estática autocontenida con todos los platos."""
    grouped_rows = _restaurant_rows(rows)
    statuses = _status_map(extraction)
    restaurants = _restaurant_views(rows, extraction)
    available_count = sum(bool(grouped_rows.get(item.identifier)) for item in restaurants)
    best = recommendations.get("balanced")
    tab_html = "".join(
        f'<button class="tab{" active" if index == 0 else ""}" type="button" role="tab" aria-controls="panel-{item.slug}" aria-selected="{"true" if index == 0 else "false"}" data-restaurant-tab="{item.slug}">{_escape(item.name)}</button>'
        for index, item in enumerate(restaurants)
    )
    panels = "".join(
        _restaurant_panel(item.identifier, item.name, item.slug, grouped_rows.get(item.identifier, []), statuses.get(item.identifier), index == 0)
        for index, item in enumerate(restaurants)
    )
    best_text = _combo_dishes(best) if best is not None else "Sin recomendación comparable"
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>Menú nutricional · {menu_date}</title><style>{INTERACTIVE_CSS}</style></head>
<body><main class="page">
  <header class="masthead"><p class="brand">{_escape(_report_title().upper())}</p><h1>Menú nutricional del día</h1><p class="date">{_escape(human_date(menu_date))}</p></header>
  <section class="summary" aria-label="Resumen"><div class="summary-card"><small>Restaurantes disponibles</small><strong>{available_count} de {len(restaurants)}</strong></div><div class="summary-card"><small>Platos únicos</small><strong>{len(rows)}</strong></div><div class="summary-card"><small>Menú equilibrado</small><strong>{_escape(best['restaurant_name'] if best else 'No disponible')}</strong></div></section>
  <nav class="restaurant-tabs" role="tablist" aria-label="Restaurantes">{tab_html}</nav>{panels}
  <footer class="footer">Fuente: {_escape(_source_label())}. Los datos estimados son orientativos. Mejor combinación del día: {_escape(best_text)}.</footer>
</main><script>{INTERACTIVE_JS}</script></body></html>"""


def _text_report(
    menu_date: str,
    rows: list[MenuRow],
    extraction: list[ExtractionSummary],
    recommendations: Recommendations,
    report_url: str | None,
) -> str:
    """Produce la alternativa accesible ``text/plain`` del resumen ejecutivo."""
    grouped_rows = _restaurant_rows(rows)
    lines = [_report_title().upper(), "¿Qué comemos hoy?", human_date(menu_date), "", "LO MEJOR DE HOY"]
    for combo, badges in _grouped_recommendations(recommendations):
        lines.extend([
            f"{', '.join(badges)} — {combo['restaurant_name']}",
            _combo_dishes(combo),
            _combo_metrics(combo),
            _clean(combo["reason"]),
            "",
        ])
    lines.append("DÓNDE COMER HOY")
    status_by_id = _status_map(extraction)
    for restaurant in _restaurant_views(rows, extraction):
        dishes = grouped_rows.get(restaurant.identifier, [])
        if dishes:
            lines.append(f"- {restaurant.name}: {len(dishes)} platos" + (f" — {report_url}#{restaurant.slug}" if report_url else ""))
        else:
            status = status_by_id.get(restaurant.identifier)
            detail = _clean(status.get("detail")) if status else "menú pendiente"
            lines.append(f"- {restaurant.name}: {detail}")
    if report_url:
        lines.extend(["", f"Informe completo: {report_url}"])
    else:
        lines.extend(["", "El informe completo se incluye como archivo HTML adjunto."])
    lines.extend(["", f"Fuente: {_source_label()}", "Algunos valores nutricionales son estimaciones orientativas."])
    return "\n".join(lines)


def build_report(
    menu_date: str,
    extracted_at: str,
    rows: list[MenuRow],
    extraction: list[ExtractionSummary],
    recommendations: Recommendations,
    report_base_url: str | None = None,
) -> DailyReport:
    """Construye ambos informes sin I/O y preserva la interfaz histórica.

    ``extracted_at`` se acepta para no romper consumidores existentes, pero no se
    muestra: es diagnóstico técnico y no aporta valor al lector del informe.
    """
    del extracted_at
    report_url = _report_url(report_base_url, menu_date)
    return DailyReport(
        menu_date=menu_date,
        subject=f"{_report_title()} · ¿Qué comemos hoy? · {menu_date}",
        html=_build_email_html(menu_date, rows, extraction, recommendations, report_url),
        text=_text_report(menu_date, rows, extraction, recommendations, report_url),
        report_key=f"daily:{menu_date}",
        interactive_html=_build_interactive_html(menu_date, rows, extraction, recommendations),
        report_url=report_url,
    )


def _write_html(content: str, path: str | Path) -> Path:
    """Escribe UTF-8 creando únicamente los directorios padre necesarios."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def save_preview(report: DailyReport, path: str | Path) -> Path:
    """Guarda el informe interactivo completo; mantiene el nombre público previo."""
    return _write_html(report.interactive_html, path)


def save_email_preview(report: DailyReport, path: str | Path) -> Path:
    """Guarda el cuerpo exacto ``text/html`` que verá el cliente de correo."""
    return _write_html(report.html, path)


def save_site_report(report: DailyReport, site_dir: str | Path) -> Path:
    """Guarda una fecha en ``site/fecha/index.html`` sin copiar SQLite."""
    return _write_html(report.interactive_html, Path(site_dir) / report.menu_date / "index.html")


def save_site_index(site_dir: str | Path, menu_date: str) -> Path:
    """Crea una portada mínima que dirige a la fecha publicada más reciente."""
    target = html.escape(menu_date, quote=True)
    content = f'<!doctype html><html lang="es"><head><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={target}/"><link rel="canonical" href="{target}/"><title>{_escape(_report_title())}</title></head><body><p><a href="{target}/">Abrir el informe de {target}</a></p></body></html>'
    return _write_html(content, Path(site_dir) / "index.html")


def _build_email_message(report: DailyReport, sender: str, recipients: list[str]) -> EmailMessage:
    """Ensambla MIME con texto, HTML y adjunto solo cuando no existe URL pública."""
    message = EmailMessage()
    message["Subject"] = report.subject
    message["From"] = formataddr((os.getenv("EMAIL_FROM_NAME", _report_title()), sender))
    message["To"] = ", ".join(recipients)
    message["Message-ID"] = make_msgid(idstring=report.report_key.replace(":", "-"))
    message.set_content(report.text)
    message.add_alternative(report.html, subtype="html")
    if report.report_url is None:
        message.add_attachment(
            report.interactive_html,
            subtype="html",
            filename=f"daily-menu-{report.menu_date}.html",
        )
    return message


def send_report(
    database: MenuDatabase,
    report: DailyReport,
    force: bool = False,
    preview_path: Path | None = None,
) -> bool:
    """Envía el multipart por SMTP y registra un estado durable e idempotente."""
    if database.report_status(report.report_key) == "sent" and not force:
        return False
    host, username, password = os.getenv("SMTP_HOST"), os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD")
    sender, recipients = os.getenv("EMAIL_FROM"), os.getenv("EMAIL_TO")
    if not host or not username or not password or not sender or not recipients:
        raise RuntimeError("Faltan SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM o EMAIL_TO.")
    recipient_list = [value.strip() for value in recipients.split(",") if value.strip()]
    port = int(os.getenv("SMTP_PORT", "465"))
    message = _build_email_message(report, sender, recipient_list)
    database.record_report(report.report_key, report.menu_date, "sending", html_path=str(preview_path or ""))
    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as client:
                client.login(username, password)
                client.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=30) as client:
                client.starttls(context=context)
                client.login(username, password)
                client.send_message(message)
    except Exception as exc:
        database.record_report(report.report_key, report.menu_date, "failed_or_uncertain", str(exc), str(preview_path or ""))
        raise
    database.record_report(report.report_key, report.menu_date, "sent", "SMTP accepted message", str(preview_path or ""))
    return True
