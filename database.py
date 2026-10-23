"""Persistencia SQLite para Daily Menu Insights.

El módulo usa solo la librería estándar para que el histórico sea fácil de
mover, inspeccionar y conservar también desde GitHub Actions.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, TypedDict, cast

from models import (
    DishRecord,
    EstimateValues,
    ExtractionSummary,
    MenuRow,
    RestaurantRecord,
    StoredEstimate,
)


class MenuItemPayload(TypedDict):
    """Parámetros nombrados usados al escribir una disponibilidad de menú."""

    menu_date: str
    restaurant_id: int
    content_hash: str
    source_dish_id: str
    menu_name: str
    groups_json: str
    categories_json: str
    observed_at: str
    updated_at: str


def utc_now() -> str:
    """Crea una marca temporal UTC comparable y serializable en SQLite.

    Returns:
        Fecha y hora ISO 8601 con zona horaria y precisión de microsegundos.
    """
    # Microsegundos evitan colisionar registros de dos extracciones consecutivas
    # del mismo día dentro de una misma ejecución de pruebas o reintento.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class MenuDatabase:
    """Capa transaccional e idempotente sobre el histórico SQLite.

    Attributes:
        path: Ruta del archivo SQLite. El constructor crea su directorio padre
            y aplica el esquema antes de aceptar operaciones.

    La clase abre conexiones cortas por operación. Este diseño evita mantener
    descriptores abiertos durante las llamadas HTTP o el renderizado y encaja
    con procesos efímeros como GitHub Actions.
    """

    def __init__(self, path: str | Path = "data/menus.db") -> None:
        """Prepara el archivo de base de datos y garantiza que tenga el esquema.

        Args:
            path: Ruta existente o futura del archivo SQLite.

        Raises:
            sqlite3.Error: Si el archivo no puede abrirse o el esquema falla.
        """
        self.path: Path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Abre una transacción que confirma al salir o revierte ante un error.

        Yields:
            Conexión con filas accesibles por nombre y claves foráneas activas.

        Raises:
            Exception: Propaga el error original después de ejecutar rollback.

        ``contextmanager`` convierte este generador en un contexto válido para
        ``with``. El cierre en ``finally`` se ejecuta incluso si falla el commit.
        """
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Crea tablas e índices y aplica migraciones compatibles hacia delante.

        ``CREATE ... IF NOT EXISTS`` hace segura su ejecución en cada arranque.
        Las comprobaciones de ``dish_type`` y ``prompt_version`` permiten abrir
        bases anteriores sin exigir un sistema de migraciones independiente. Las
        estimaciones preexistentes reciben la versión 1 para que nutrición pueda
        decidir si debe renovarlas.

        Raises:
            sqlite3.Error: Si SQLite no puede crear o migrar el esquema.
        """
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS restaurants (
                    source_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    location TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dishes (
                    content_hash TEXT PRIMARY KEY,
                    source_dish_id TEXT,
                    normalized_name TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT,
                    dish_type TEXT,
                    ingredients_json TEXT NOT NULL,
                    allergens_json TEXT NOT NULL,
                    published_nutrition_json TEXT NOT NULL,
                    nutrition_basis TEXT NOT NULL,
                    source_payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dishes_source_id ON dishes(source_dish_id);
                CREATE INDEX IF NOT EXISTS idx_dishes_normalized_name ON dishes(normalized_name);

                CREATE TABLE IF NOT EXISTS menu_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    menu_date TEXT NOT NULL,
                    restaurant_id INTEGER NOT NULL REFERENCES restaurants(source_id),
                    content_hash TEXT NOT NULL REFERENCES dishes(content_hash),
                    source_dish_id TEXT,
                    menu_name TEXT,
                    groups_json TEXT NOT NULL,
                    categories_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(menu_date, restaurant_id, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_menu_items_date_restaurant
                    ON menu_items(menu_date, restaurant_id);
                CREATE INDEX IF NOT EXISTS idx_menu_items_dish ON menu_items(content_hash);

                CREATE TABLE IF NOT EXISTS nutrition_estimates (
                    content_hash TEXT PRIMARY KEY REFERENCES dishes(content_hash),
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version INTEGER NOT NULL DEFAULT 1,
                    values_json TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    assumptions TEXT,
                    estimated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS extraction_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    menu_date TEXT NOT NULL,
                    restaurant_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    dish_count INTEGER NOT NULL DEFAULT 0,
                    detail TEXT,
                    extracted_at TEXT NOT NULL,
                    UNIQUE(menu_date, restaurant_id, extracted_at)
                );
                CREATE INDEX IF NOT EXISTS idx_extraction_date ON extraction_runs(menu_date, restaurant_id);

                CREATE TABLE IF NOT EXISTS report_runs (
                    report_key TEXT PRIMARY KEY,
                    menu_date TEXT NOT NULL,
                    report_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    html_path TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Migración pequeña para bases creadas con una versión anterior.
            dish_columns = {row["name"] for row in conn.execute("PRAGMA table_info(dishes)").fetchall()}
            if "dish_type" not in dish_columns:
                conn.execute("ALTER TABLE dishes ADD COLUMN dish_type TEXT")
            estimate_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(nutrition_estimates)").fetchall()
            }
            if "prompt_version" not in estimate_columns:
                # Las filas anteriores a esta migración usaban el contrato original.
                # Marcarlas como v1 permite renovarlas sin borrar su trazabilidad.
                conn.execute(
                    "ALTER TABLE nutrition_estimates "
                    "ADD COLUMN prompt_version INTEGER NOT NULL DEFAULT 1"
                )

    def upsert_restaurant(self, restaurant: RestaurantRecord) -> None:
        """Inserta una sede o actualiza su nombre y ubicación.

        Args:
            restaurant: Identificador estable de la fuente y metadatos visibles.
        """
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO restaurants(source_id, name, location, updated_at)
                VALUES (:id, :name, :location, :updated_at)
                ON CONFLICT(source_id) DO UPDATE SET
                    name=excluded.name, location=excluded.location, updated_at=excluded.updated_at
                """,
                {**restaurant, "updated_at": utc_now()},
            )

    def upsert_dish(self, dish: DishRecord) -> None:
        """Conserva la última representación conocida de una receta concreta.

        Args:
            dish: Receta normalizada identificada por su hash de contenido.

        El ``content_hash`` separa una modificación real de una simple nueva
        aparición. ``first_seen_at`` permanece estable en un conflicto, mientras
        que ``last_seen_at`` y el contenido visible se actualizan.
        """
        payload: dict[str, str] = {
            "content_hash": dish["content_hash"],
            "source_dish_id": str(dish.get("source_dish_id") or ""),
            "normalized_name": dish["normalized_name"],
            "name": dish["name"],
            "description": dish.get("description") or "",
            "dish_type": str(dish.get("dish_type") or ""),
            "ingredients_json": json.dumps(dish.get("ingredients", []), ensure_ascii=False),
            "allergens_json": json.dumps(dish.get("allergens", []), ensure_ascii=False),
            "published_nutrition_json": json.dumps(dish.get("published_nutrition", {}), ensure_ascii=False),
            "nutrition_basis": dish.get("nutrition_basis", "no declarada por la fuente"),
            "source_payload_json": json.dumps(dish.get("source_payload", {}), ensure_ascii=False),
            "seen_at": utc_now(),
        }
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO dishes(
                    content_hash, source_dish_id, normalized_name, name, description, dish_type,
                    ingredients_json, allergens_json, published_nutrition_json,
                    nutrition_basis, source_payload_json, first_seen_at, last_seen_at
                ) VALUES (
                    :content_hash, :source_dish_id, :normalized_name, :name, :description, :dish_type,
                    :ingredients_json, :allergens_json, :published_nutrition_json,
                    :nutrition_basis, :source_payload_json, :seen_at, :seen_at
                )
                ON CONFLICT(content_hash) DO UPDATE SET
                    source_dish_id=excluded.source_dish_id, name=excluded.name,
                    description=excluded.description, dish_type=excluded.dish_type,
                    ingredients_json=excluded.ingredients_json,
                    allergens_json=excluded.allergens_json,
                    published_nutrition_json=excluded.published_nutrition_json,
                    nutrition_basis=excluded.nutrition_basis,
                    source_payload_json=excluded.source_payload_json,
                    last_seen_at=excluded.last_seen_at
                """,
                payload,
            )

    @staticmethod
    def _menu_item_payload(menu_date: str, restaurant_id: int, dish: DishRecord) -> MenuItemPayload:
        """Serializa la relación fecha-restaurante-receta para SQLite.

        Args:
            menu_date: Fecha ISO del menú.
            restaurant_id: Identificador de la sede en la fuente.
            dish: Receta ya persistida en ``dishes``.

        Returns:
            Mapeo compatible con los parámetros nombrados de la consulta SQL.
        """
        return {
            "menu_date": menu_date,
            "restaurant_id": restaurant_id,
            "content_hash": dish["content_hash"],
            "source_dish_id": str(dish.get("source_dish_id") or ""),
            "menu_name": "; ".join(dish.get("menu_names", [])),
            "groups_json": json.dumps(sorted(dish.get("groups", [])), ensure_ascii=False),
            "categories_json": json.dumps(sorted(dish.get("categories", [])), ensure_ascii=False),
            "observed_at": utc_now(),
            "updated_at": utc_now(),
        }

    def upsert_menu_item(self, menu_date: str, restaurant_id: int, dish: DishRecord) -> None:
        """Inserta o actualiza una disponibilidad sin crear duplicados.

        Args:
            menu_date: Fecha ISO del menú.
            restaurant_id: Sede que publica el plato.
            dish: Receta normalizada.
        """
        payload = self._menu_item_payload(menu_date, restaurant_id, dish)
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO menu_items(
                    menu_date, restaurant_id, content_hash, source_dish_id, menu_name,
                    groups_json, categories_json, observed_at, updated_at
                ) VALUES (
                    :menu_date, :restaurant_id, :content_hash, :source_dish_id, :menu_name,
                    :groups_json, :categories_json, :observed_at, :updated_at
                )
                ON CONFLICT(menu_date, restaurant_id, content_hash) DO UPDATE SET
                    source_dish_id=excluded.source_dish_id, menu_name=excluded.menu_name,
                    groups_json=excluded.groups_json, categories_json=excluded.categories_json,
                    observed_at=excluded.observed_at, updated_at=excluded.updated_at
                """,
                payload,
            )

    def replace_menu_items(self, menu_date: str, restaurant_id: int, dishes: list[DishRecord]) -> None:
        """Sincroniza por completo un restaurante y fecha tras una extracción válida.

        Una web puede corregir el menú durante el día. En ese caso, conservar
        filas que ya no figuran en la fuente mezclaría platos antiguos con los
        actuales. La sustitución se hace en una transacción y solo se invoca
        cuando el scraper ha devuelto un menú con estado ``success``.

        Args:
            menu_date: Fecha ISO que se sincroniza.
            restaurant_id: Sede cuyo menú se reemplaza.
            dishes: Fotografía completa y validada de la publicación actual.
        """
        payloads = [self._menu_item_payload(menu_date, restaurant_id, dish) for dish in dishes]
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM menu_items WHERE menu_date = ? AND restaurant_id = ?",
                (menu_date, restaurant_id),
            )
            conn.executemany(
                """
                INSERT INTO menu_items(
                    menu_date, restaurant_id, content_hash, source_dish_id, menu_name,
                    groups_json, categories_json, observed_at, updated_at
                ) VALUES (
                    :menu_date, :restaurant_id, :content_hash, :source_dish_id, :menu_name,
                    :groups_json, :categories_json, :observed_at, :updated_at
                )
                """,
                payloads,
            )

    def record_extraction(
        self, menu_date: str, restaurant_id: int, status: str, dish_count: int, detail: str = ""
    ) -> None:
        """Añade una observación del scraper sin sustituir intentos anteriores.

        Args:
            menu_date: Fecha ISO solicitada.
            restaurant_id: Sede consultada.
            status: Resultado textual de la extracción.
            dish_count: Número de recetas únicas observadas.
            detail: Diagnóstico legible cuando la extracción no fue completa.

        El histórico de intentos permite distinguir un menú ausente de una
        corrección posterior publicada el mismo día.
        """
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO extraction_runs(menu_date, restaurant_id, status, dish_count, detail, extracted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (menu_date, restaurant_id, status, dish_count, detail, utc_now()),
            )

    def get_estimate(self, content_hash: str) -> StoredEstimate | None:
        """Recupera la estimación cacheada de una versión exacta de receta.

        Args:
            content_hash: SHA-256 calculado a partir del contenido del plato.

        Returns:
            Estimación deserializada con sus metadatos, o ``None`` si no existe.
        """
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM nutrition_estimates WHERE content_hash = ?", (content_hash,)
            ).fetchone()
        if not row:
            return None
        result: dict[str, object] = dict(row)
        serialized_values = cast(str, result.pop("values_json"))
        result["values"] = json.loads(serialized_values)
        return cast(StoredEstimate, result)

    def save_estimate(
        self,
        content_hash: str,
        provider: str,
        model: str,
        prompt_version: int,
        values: EstimateValues,
    ) -> None:
        """Guarda una salida LLM validada y reemplaza una estimación anterior.

        Args:
            content_hash: Versión de receta a la que pertenece el cálculo.
            provider: Backend que respondió, por ejemplo ``gemini`` u ``ollama``.
            model: Identificador concreto del modelo para trazabilidad.
            prompt_version: Revisión del contrato que produjo la estimación.
            values: Valores normalizados; nunca se persiste la respuesta cruda.
        """
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO nutrition_estimates(
                    content_hash, provider, model, prompt_version, values_json,
                    confidence, assumptions, estimated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(content_hash) DO UPDATE SET
                    provider=excluded.provider, model=excluded.model,
                    prompt_version=excluded.prompt_version, values_json=excluded.values_json,
                    confidence=excluded.confidence, assumptions=excluded.assumptions,
                    estimated_at=excluded.estimated_at
                """,
                (
                    content_hash,
                    provider,
                    model,
                    prompt_version,
                    json.dumps(values, ensure_ascii=False),
                    values.get("nivel_confianza", "bajo"),
                    json.dumps(values.get("supuestos", []), ensure_ascii=False),
                    utc_now(),
                ),
            )

    def menu_for_date(self, menu_date: str) -> list[MenuRow]:
        """Combina disponibilidad, receta, sede y estimación para una fecha.

        Args:
            menu_date: Fecha ISO que se quiere consultar.

        Returns:
            Filas listas para análisis y presentación. Los campos JSON se
            devuelven como listas o diccionarios, no como texto serializado.

        El ``LEFT JOIN`` conserva platos que no tienen estimación: la IA es una
        mejora opcional y nunca una condición para mostrar datos oficiales.
        """
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT mi.menu_date, mi.restaurant_id, r.name AS restaurant_name,
                       mi.menu_name, mi.groups_json, mi.categories_json,
                       d.content_hash, d.source_dish_id, d.name, d.description,
                       d.dish_type,
                       d.ingredients_json, d.allergens_json, d.published_nutrition_json,
                       d.nutrition_basis, ne.provider AS estimate_provider,
                       ne.model AS estimate_model,
                       ne.prompt_version AS estimate_prompt_version,
                       ne.values_json AS estimate_values_json,
                       ne.confidence AS estimate_confidence
                FROM menu_items mi
                JOIN restaurants r ON r.source_id = mi.restaurant_id
                JOIN dishes d ON d.content_hash = mi.content_hash
                LEFT JOIN nutrition_estimates ne ON ne.content_hash = d.content_hash
                WHERE mi.menu_date = ?
                ORDER BY r.name, d.name
                """,
                (menu_date,),
            ).fetchall()
        result: list[MenuRow] = []
        for row in rows:
            item: dict[str, object] = dict(row)
            for field in ("groups_json", "categories_json", "ingredients_json", "allergens_json", "published_nutrition_json"):
                serialized = cast(str | None, item.pop(field))
                item[field.removesuffix("_json")] = json.loads(serialized or "[]")
            estimate_json = cast(str | None, item.pop("estimate_values_json"))
            item["estimate"] = json.loads(estimate_json or "null")
            result.append(cast(MenuRow, item))
        return result

    def extraction_summary(self, menu_date: str) -> list[ExtractionSummary]:
        """Devuelve el intento más reciente de cada sede para una fecha.

        Args:
            menu_date: Fecha ISO consultada.

        Returns:
            Hasta una fila por restaurante, ordenadas por nombre.

        La subconsulta usa ``MAX(id)`` por sede. No se usa la hora porque dos
        reintentos muy próximos pueden compartir orden lexicográfico ambiguo en
        bases antiguas; el identificador autoincremental refleja el orden real.
        """
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT er.restaurant_id, r.name, er.status, er.dish_count, er.detail, er.extracted_at
                FROM extraction_runs er
                JOIN restaurants r ON r.source_id = er.restaurant_id
                WHERE er.id IN (
                    SELECT MAX(id) FROM extraction_runs WHERE menu_date = ? GROUP BY restaurant_id
                )
                ORDER BY r.name
                """,
                (menu_date,),
            ).fetchall()
        return [cast(ExtractionSummary, dict(row)) for row in rows]

    def report_status(self, report_key: str) -> str | None:
        """Consulta el último estado de un informe idempotente.

        Args:
            report_key: Clave estable, actualmente ``daily:YYYY-MM-DD``.

        Returns:
            Estado persistido o ``None`` si nunca se intentó generar el envío.
        """
        with self.connection() as conn:
            row = conn.execute("SELECT status FROM report_runs WHERE report_key = ?", (report_key,)).fetchone()
        return row["status"] if row else None

    def record_report(
        self, report_key: str, menu_date: str, status: str, detail: str = "", html_path: str = ""
    ) -> None:
        """Inserta o actualiza el estado durable de un informe.

        Args:
            report_key: Identificador usado para evitar correos duplicados.
            menu_date: Fecha ISO representada por el informe.
            status: Estado de la máquina de envío (por ejemplo ``sending`` o
                ``sent``).
            detail: Resultado SMTP o diagnóstico del fallo.
            html_path: Ruta de la vista previa asociada, si existe.
        """
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO report_runs(report_key, menu_date, report_type, status, detail, html_path, updated_at)
                VALUES (?, ?, 'daily', ?, ?, ?, ?)
                ON CONFLICT(report_key) DO UPDATE SET
                    status=excluded.status, detail=excluded.detail,
                    html_path=excluded.html_path, updated_at=excluded.updated_at
                """,
                (report_key, menu_date, status, detail, html_path, utc_now()),
            )

    def export_csv(self, menu_date: str, path: str | Path) -> Path:
        """Exporta una vista reducida del menú usando UTF-8 con BOM.

        Args:
            menu_date: Fecha ISO que se exportará.
            path: Archivo CSV de destino.

        Returns:
            Ruta normalizada del archivo escrito.

        Raises:
            ValueError: Si la fecha todavía no tiene platos persistidos.

        El BOM facilita que Excel detecte UTF-8 al abrir nombres españoles.
        """
        import csv

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self.menu_for_date(menu_date)
        if not rows:
            raise ValueError(f"No hay platos guardados para {menu_date}.")
        columns = ["restaurant_name", "name", "description", "groups", "categories", "published_nutrition", "allergens"]
        with target.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in columns})
        return target

    def backup(self, destination: str | Path) -> Path:
        """Genera una copia transaccional mediante la API nativa de SQLite.

        Args:
            destination: Archivo de destino; se crean sus directorios padre.

        Returns:
            Ruta de la copia creada.

        ``Connection.backup`` mantiene una imagen coherente aunque otra conexión
        esté escribiendo, a diferencia de copiar directamente el archivo binario.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as source, sqlite3.connect(target) as copy:
            source.backup(copy)
        return target
