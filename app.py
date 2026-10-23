"""Dashboard local Streamlit para consultar histórico y recomendaciones.

Streamlit vuelve a ejecutar el módulo completo después de cada interacción. Por
eso no se conserva una conexión SQLite global: ``MenuDatabase`` abre conexiones
cortas y los cálculos se reconstruyen desde el histórico en cada ciclo. El módulo
solo escribe cuando el usuario pulsa el botón de vista previa; nunca envía correo.
"""

from __future__ import annotations

import streamlit as st

from database import MenuDatabase
from mailer import build_report, save_preview
from models import ExtractionSummary, GlobalRecommendationKey, MenuRow, NutrientField, Recommendations
from nutrition import calculate_combination, menu_structure_error, nutrient_value, recommend_daily

st.set_page_config(page_title="Daily Menu Insights", page_icon="🍽️", layout="wide")
st.title("🍽️ Daily Menu Insights")
st.caption("🟢 Oficial: dato publicado por el restaurante · 🟠 IA: estimación orientativa para un dato ausente · —: no disponible")

database: MenuDatabase = MenuDatabase()
with database.connection() as conn:
    dates: list[str] = [
        row["menu_date"]
        for row in conn.execute("SELECT DISTINCT menu_date FROM menu_items ORDER BY menu_date DESC").fetchall()
    ]
if not dates:
    st.info("Aún no hay menús. Ejecuta `python scraper.py --date today --preview output/menu.html`.")
    st.stop()

selected_date: str = st.sidebar.selectbox("Fecha del menú", dates, index=0)
rows: list[MenuRow] = database.menu_for_date(selected_date)
summary: list[ExtractionSummary] = database.extraction_summary(selected_date)
recommendations: Recommendations = recommend_daily(rows)

columns = st.columns(4)
columns[0].metric("Restaurantes con platos", len({row["restaurant_id"] for row in rows}))
columns[1].metric("Platos disponibles", len(rows))
columns[2].metric("Combinaciones evaluadas", recommendations["candidate_count"])
columns[3].metric("Extracciones correctas", sum(item["status"] == "success" for item in summary))

st.subheader("Recomendaciones")
recommendation_columns = st.columns(4)
recommendation_keys: tuple[GlobalRecommendationKey, ...] = (
    "balanced",
    "high_protein",
    "light",
    "recovery",
)
for column, label, key in zip(
    recommendation_columns,
    ("Equilibrado", "Alto en proteínas", "Ligero", "Post-entrenamiento"),
    recommendation_keys,
):
    combo = recommendations.get(key)
    with column:
        st.markdown(f"**{label}**")
        if combo:
            st.write(combo["restaurant_name"])
            st.caption(" · ".join(f"{item['course']}: {item['name']}" for item in combo["components"]))
            totals = combo["totals"]
            st.write(
                f"Calorías: {totals['kcal'] if totals['kcal'] is not None else '—'} Kcal · "
                f"Proteínas: {totals['proteinas_g'] if totals['proteinas_g'] is not None else '—'} g"
            )
            st.caption("🟠 Incluye estimación de IA" if "estimado" in combo["sources"] else "🟢 Datos oficiales del restaurante")
            st.caption(combo["reason"])
        else:
            st.caption("Datos insuficientes para una comparación fiable.")

st.subheader("Menús completos")
for restaurant_id in sorted({row["restaurant_id"] for row in rows}):
    subset = [row for row in rows if row["restaurant_id"] == restaurant_id]
    with st.expander(f"{subset[0]['restaurant_name']} ({len(subset)} platos)", expanded=True):
        display_rows: list[dict[str, str]] = []
        for item in subset:
            def formatted_metric(field: NutrientField, unit: str) -> str:
                """Formatea una métrica del ``item`` capturado por esta iteración.

                La función se invoca inmediatamente al construir la fila; no se
                almacena como callback. Por ello el cierre siempre observa el plato
                actual y no el último valor del bucle.
                """
                value, source = nutrient_value(item, field)
                if value is None:
                    return "No disponible"
                origin = "Oficial" if source == "publicado" else "IA"
                return f"{value:g} {unit} · {origin}"

            estimate = item.get("estimate")
            core_fields: tuple[NutrientField, ...] = (
                "kcal",
                "proteinas_g",
                "carbohidratos_g",
                "grasas_g",
                "fibra_g",
            )
            uses_ai = any(
                nutrient_value(item, field)[1] == "estimado"
                for field in core_fields
            )
            ai_detail = "—"
            if estimate is not None and (uses_ai or estimate.get("campos_indeterminados")):
                serving = estimate.get("tamano_racion_asumido_g")
                serving_text = f"{serving:g} g" if serving is not None else "no aplicable"
                assumptions = "; ".join(estimate.get("supuestos", [])) or "sin supuestos adicionales"
                ai_detail = (
                    f"{item.get('estimate_model') or 'Modelo no registrado'} · "
                    f"prompt v{item.get('estimate_prompt_version') or 'anterior'} · "
                    f"confianza {estimate.get('nivel_confianza', 'baja')} · "
                    f"ración {serving_text} · {assumptions}"
                )

            display_rows.append(
                {
                    "Plato": item["name"],
                    "Grupo": " / ".join(item["groups"]),
                    "Categoría": " / ".join(item["categories"]),
                    "Calorías (Kcal)": formatted_metric("kcal", "Kcal"),
                    "Proteínas (g)": formatted_metric("proteinas_g", "g"),
                    "Hidratos (g)": formatted_metric("carbohidratos_g", "g"),
                    "Grasas (g)": formatted_metric("grasas_g", "g"),
                    "Fibra (g)": formatted_metric("fibra_g", "g"),
                    "Detalle IA": ai_detail,
                    "Alérgenos": ", ".join(item["allergens"]) or "No publicados",
                }
            )
        st.dataframe(display_rows, use_container_width=True, hide_index=True)

st.subheader("Calculadora manual")
options: dict[str, MenuRow] = {row["content_hash"]: row for row in rows}
selected_hashes = st.multiselect(
    "Selecciona como máximo un primero, un segundo, una guarnición y un postre del mismo restaurante",
    options=list(options),
    # El widget conserva hashes estables y este callback solo calcula su etiqueta visible.
    format_func=lambda key: f"{options[key]['restaurant_name']} — {options[key]['name']}",
)
if selected_hashes:
    selection: list[MenuRow] = [options[key] for key in selected_hashes]
    restaurant_ids: set[int] = {row["restaurant_id"] for row in selection}
    if len(restaurant_ids) > 1:
        st.warning("Selecciona platos de un único restaurante para crear una combinación válida.")
    else:
        structure_error = menu_structure_error(selection)
        if structure_error:
            st.warning(structure_error)
        else:
            total = calculate_combination(selection)
            st.json(total["totals"])
            st.caption(total["basis_note"])

if st.button("Guardar vista previa del email"):
    report = build_report(selected_date, "Generado desde dashboard", rows, summary, recommendations)
    path = save_preview(report, f"output/menu_{selected_date}.html")
    st.success(f"Vista previa guardada en {path.resolve()}")

with st.expander("Estado de extracción"):
    st.json(summary)
