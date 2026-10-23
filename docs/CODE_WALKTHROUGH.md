# Guía técnica de Daily Menu Insights

Esta guía describe el comportamiento real del código en la revisión de octubre de
2026. Los docstrings son la referencia más próxima a cada símbolo; este documento
explica cómo encajan las piezas y por qué existen algunas decisiones que no son
evidentes al leer un único módulo.

## Flujo y límites de responsabilidad

```text
HTML público
  └─ scraper.py: descarga, interpreta y deduplica
       └─ database.py: transacción e histórico SQLite
            ├─ nutrition.py: completa huecos y puntúa combinaciones
            ├─ mailer.py: genera HTML/texto y, opcionalmente, usa SMTP
            └─ app.py: consulta interactiva del histórico
```

`models.py` no introduce objetos de dominio en ejecución. Sus `TypedDict`,
`Literal`, alias y `Protocol` documentan los diccionarios que cruzan los límites
anteriores. Esto mantiene compatibles SQLite, JSON, el notebook y las interfaces
existentes, pero permite detectar nombres o tipos incoherentes con análisis estático.

No hay código asíncrono. Las llamadas HTTP y SMTP son secuenciales y tienen timeout.
El volumen esperado —una página y decenas de platos al día— no justifica añadir un
event loop ni complejidad de concurrencia dentro del proceso. El workflow sí declara
un grupo `concurrency` para no solapar dos ejecuciones que escriban el mismo SQLite.

## `models.py`: contratos compartidos

- `JsonScalar`, `JsonValue` y `JsonObject` expresan recursivamente los valores que
  pueden proceder del portal o de un proveedor LLM. Se evita `Any`: un consumidor
  debe comprobar o declarar la forma que espera.
- Los `Literal` (`NutrientField`, `CourseKind`, `NutrientSource`, etc.) limitan
  cadenas que participan en decisiones. Un error ortográfico deja de parecer un
  estado válido para un comprobador de tipos.
- `NutritionDatum` conserva el valor original (`raw`), el número parseado y si la
  fuente lo publicó. `PublishedNutrition` fija las siete métricas leídas del portal.
- `EstimateValues` representa únicamente una respuesta que ya atravesó
  `validate_estimate`; la respuesta cruda nunca llega a SQLite.
- `DishRecord` es la receta final del scraper. `MutableDishRecord` es su estado
  transitorio con conjuntos, usados para unir grupos sin duplicarlos.
- `RestaurantExtraction`, `MenuRow` y `ExtractionSummary` son, respectivamente,
  los límites scraper→persistencia, persistencia→aplicación y persistencia→informe.
- `MenuCombination`, `Recommendation` y `Recommendations` hacen explícita la
  diferencia entre un candidato calculado y uno que ya fue elegido y explicado.
- `PipelineArgs` es un `Protocol`: `run_pipeline` no depende de la clase dinámica
  `argparse.Namespace`, solo de los atributos enumerados. No crea ni valida objetos
  en ejecución; el tipado es estructural.

## `scraper.py`: origen y orquestación

### Tipos y errores

`ScraperError` separa los fallos esperables de red/contrato de otros errores. `main`
lo registra sin traceback; un defecto no previsto conserva el traceback. La
`dataclass ExtractionResult` agrupa fecha, instante y sedes. Su propiedad
`total_dishes` suma las listas ya deduplicadas y no vuelve a interpretar el HTML.

### Funciones

- `load_env_file(path) -> None`: lee un `.env` deliberadamente sencillo. Ignora
  comentarios y líneas sin `=`, elimina comillas exteriores y usa
  `os.environ.setdefault`; por ello una variable inyectada por Actions siempre gana.
  No implementa expansión, escapes ni comandos, lo que reduce comportamiento oculto.
- `resolve_date(value) -> date`: acepta fecha ISO o los alias `today`/`hoy`. Los
  alias usan `Europe/Madrid`, evitando que una ejecución nocturna en un runner UTC
  consulte el día anterior.
- `normalize_text(value) -> str`: elimina diacríticos, normaliza espacios y pasa a
  minúsculas para identidad/comparación. El texto presentado al usuario se conserva.
- `parse_number(value) -> float | None`: admite números y textos con coma decimal,
  elimina unidades y devuelve ausencia si no puede interpretar. Rechaza booleanos
  porque Python los considera enteros.
- `parse_json_value(value, fallback) -> JsonValue`: absorbe la inconsistencia del
  portal entre campos ya expandidos y campos JSON serializados. Un JSON inválido no
  aborta el plato; devuelve el fallback indicado por el llamador.
- `extract_js_json(html, name) -> JsonValue`: localiza una asignación JavaScript y
  recorre carácter a carácter profundidad, comillas y escapes. Una regex que buscara
  el primer `]`/`}` fallaría con esos caracteres dentro de descripciones. Lanza
  `ScraperError` cuando la variable falta o el literal no se cierra/decodifica.
- `download_page(url, timeout) -> str`: realiza una petición identificable con
  revalidación de caché, exige estado HTTP correcto y comprueba dos marcadores del
  contrato antes de devolver el HTML.
- `_source_nutrition(dish) -> PublishedNutrition`: busca alias españoles de cada
  métrica dentro de `properties[0]`, conserva el texto original y produce siempre
  el mismo conjunto de campos.
- `_hash_dish(...) -> str`: serializa una forma canónica y calcula SHA-256. Excluye
  restaurante/fecha para reutilizar una estimación si dos sedes comparten receta;
  incluye nutrición original para invalidar la caché cuando la fuente cambia datos.
- `_day_groups(menu, requested) -> list[JsonObject]`: reproduce el ciclo rotatorio
  publicado: `(fecha - inicio) % rotatorio`. Fechas anteriores, inicios inválidos o
  contenidos vacíos producen una lista vacía.
- `parse_menu_html(html, requested) -> ExtractionResult`: carga cuatro colecciones,
  indexa platos/alérgenos/grupos, recorre los menús aplicables y agrega por hash. Los
  conjuntos eliminan repeticiones entre grupos; al finalizar se convierten en listas
  ordenadas. Una sede inexistente queda `error`; una sede sin platos aplicables queda
  `no_menu_published`; solo una lista no vacía queda `success`.
- `scrape_date(requested, url, attempts, retry_seconds) -> ExtractionResult`: reintenta
  red y menús incompletos, con espera limitada a 60 segundos. Devuelve inmediatamente
  la primera fotografía completa. Si agotó intentos pero obtuvo datos parciales,
  devuelve exclusivamente el último intento; jamás combina hoy con datos anteriores.
- `persist_extraction(database, result) -> None`: hace upsert de sedes/recetas y
  registra cada intento. Solo un estado `success` reemplaza disponibilidades: un fallo
  temporal no borra un menú válido guardado previamente.
- `configure_logging(verbose) -> None`: instala formato y nivel global mediante
  `basicConfig`, que respeta handlers ya configurados por un anfitrión.
- `run_pipeline(args) -> int`: ordena descarga → SQLite → LLM opcional → reglas →
  informe → exportación/backup → SMTP. Devuelve `2` si se solicitó correo parcial sin
  permiso. Los imports tardíos reducen dependencias al reutilizar solo el parser.
- `build_parser() -> ArgumentParser`: concentra CLI y defaults procedentes del
  entorno; los argumentos explícitos siempre prevalecen.
- `main() -> int`: carga `.env`, configura logs y traduce errores a códigos de proceso.
  `SystemExit(main())` permite que GitHub Actions detecte el fallo.

### Supuesto que conviene vigilar

Tras decodificar las cuatro variables principales, el código declara su estructura
mediante `cast`, no mediante validación exhaustiva de cada nodo. Si el portal conserva
JSON válido pero cambia listas por objetos, puede aparecer `TypeError`/`AttributeError`
en vez de `ScraperError`. No se ha cambiado porque añadir una capa de esquema sería
una modificación funcional; es una mejora futura razonable.

## `database.py`: transacciones e histórico

`MenuDatabase` crea el directorio y aplica el esquema en el constructor. Cada método
abre una conexión corta mediante `connection()`. El decorador `@contextmanager`
convierte su generador en el objeto usado por `with`: confirma al salir normalmente,
revierte ante cualquier excepción y cierra siempre. `row_factory = sqlite3.Row`
permite nombres de columna; `PRAGMA foreign_keys = ON` activa integridad que SQLite
mantiene desactivada por defecto en cada conexión.

### Funciones y métodos

- `utc_now() -> str`: produce ISO UTC con microsegundos para ordenar intentos muy
  próximos y evitar colisiones del índice de extracciones.
- `__init__(path) -> None`: normaliza la ruta, crea el directorio e invoca el esquema.
- `connection() -> Iterator[Connection]`: frontera transaccional descrita arriba.
- `initialize() -> None`: crea tablas/índices de forma idempotente. Consulta
  `PRAGMA table_info` y añade `dish_type` o `prompt_version` a bases antiguas; las
  estimaciones previas quedan marcadas como versión 1. Es una migración mínima
  compatible hacia delante, no un framework general de migraciones.
- `upsert_restaurant(restaurant) -> None`: conserva el ID público estable y actualiza
  nombre, ubicación y timestamp.
- `upsert_dish(dish) -> None`: serializa listas/diccionarios como JSON. En conflicto
  mantiene `first_seen_at`, actualiza contenido visible y avanza `last_seen_at`.
- `_menu_item_payload(menu_date, restaurant_id, dish) -> MenuItemPayload`: adapta un
  `DishRecord` a parámetros SQL, ordenando/serializando grupos y categorías.
- `upsert_menu_item(...) -> None`: operación idempotente para una disponibilidad
  individual. Se conserva como API útil aunque el flujo normal use sustitución total.
- `replace_menu_items(menu_date, restaurant_id, dishes) -> None`: borra e inserta la
  fotografía completa dentro de una sola transacción. Resuelve correcciones del portal
  que retiran platos sin dejar residuos de una extracción anterior.
- `record_extraction(...) -> None`: añade, no sobrescribe, un intento por sede.
- `get_estimate(content_hash) -> StoredEstimate | None`: busca por versión de receta y
  deserializa `values_json`; es la caché previa a cualquier llamada LLM.
- `save_estimate(..., prompt_version, values) -> None`: persiste solo
  `EstimateValues` validados y conserva proveedor, modelo y versión del contrato
  para decidir cuándo debe renovarse la caché.
- `menu_for_date(menu_date) -> list[MenuRow]`: une disponibilidad, restaurante y receta;
  el `LEFT JOIN` mantiene platos sin IA. Deserializa todos los JSON antes de devolver.
- `extraction_summary(menu_date) -> list[ExtractionSummary]`: selecciona `MAX(id)` por
  sede para mostrar el último intento real, no una mezcla histórica.
- `report_status(report_key) -> str | None`: consulta la máquina de estado SMTP.
- `record_report(...) -> None`: hace upsert de `sending`, `sent` o
  `failed_or_uncertain` bajo una clave diaria estable.
- `export_csv(menu_date, path) -> Path`: exporta una vista reducida con BOM UTF-8 para
  Excel; una fecha sin filas lanza `ValueError`.
- `backup(destination) -> Path`: usa `sqlite3.Connection.backup`, que genera una imagen
  consistente incluso si existe otra conexión, en lugar de copiar bytes a medias.

SQLite serializa escritores y el workflow impide solapamientos. El código no añade
un coordinador multiproceso propio; varios procesos locales concurrentes podrían
encontrar bloqueos conforme a los timeouts predeterminados de SQLite.

## `nutrition.py`: validación, enriquecimiento y ranking

### Proveedor LLM

- `NutritionProviderError`: fallo recuperable por plato. El pipeline continúa con
  datos oficiales/caché.
- `_as_number(value) -> float | None`: acepta solo cifras entre 0 y 10 000. Es un
  guardarraíl técnico, no una validación clínica.
- `_strip_json_fence(value) -> str`: tolera modelos que envuelven JSON en Markdown.
- `validate_estimate(payload, published_kcal, required_fields, allow_indeterminate)
  -> EstimateValues`: exige raíz objeto, conserva solo claves conocidas, normaliza
  cifras/confianza/textos y anula Kcal estimadas cuando existe Kcal oficial. Rechaza
  huecos injustificados y exige la ración asumida cuando hay valores de IA.
- `_is_variable_dish(row) -> bool`: reconoce en código buffets y platos configurables;
  solo esos casos pueden declarar campos indeterminados.
- `_estimate_is_reusable(row, stored) -> bool`: exige la versión vigente del prompt
  y cobertura completa antes de evitar una nueva llamada al proveedor.
- `_prompt_for_dish(row) -> str`: envía nombre, descripción, ingredientes, grupos,
  categorías, nutrición oficial y base de medida. Exige JSON y prohíbe inventar
  alérgenos. No incluye credenciales ni datos personales.
- `_ollama(prompt) -> tuple[JsonObject, str]`: llama a `/api/generate` con formato JSON,
  sin streaming y temperatura 0.1. URL, modelo y timeout salen del entorno.
- `_gemini(prompt) -> tuple[JsonObject, str]`: llama a `generateContent` por REST con
  MIME JSON. No requiere SDK y obtiene clave/modelo exclusivamente del entorno.
- `enrich_missing_nutrition(database, rows, provider_name) -> EnrichmentCounts`:
  deduplica por hash, omite recetas completas, usa solo caché vigente y completa, y
  procesa en secuencia. La secuencia reduce picos de cuota; cambiar la versión del
  prompt renueva las estimaciones existentes sin borrar menús ni valores oficiales.
- `nutrient_value(row, field) -> tuple[float | None, NutrientSource]`: fuente oficial
  primero, estimación después, ausencia al final. Toda presentación usa este único
  criterio para no etiquetar orígenes de manera distinta.

### Reglas de menú

- `classify_dish(row) -> CourseKind`: prioriza grupos oficiales y usa categorías/nombre
  como fallback. Devuelve un único curso; `other` significa ambigüedad, no segundo.
- `menu_structure_error(items) -> str | None`: centraliza cursos desconocidos,
  duplicados y bases repetidas para dashboard y recomendaciones.
- `_repeats_staple(items) -> bool`: heurística textual para arroz, pasta, patata,
  cuscús o pan. Puede tener falsos positivos/negativos; no interpreta cantidades.
- `_is_vegetarian(row) -> bool | None`: señal triestado basada en términos explícitos.
  No certifica un plato ni se usa para ocultar alérgenos.
- `calculate_combination(items) -> MenuCombination`: exige selección no vacía y válida.
  Una métrica total queda `None` si falta en cualquier plato; sumar solo disponibles
  daría una falsa sensación de total completo.
- `_enough_information(combo, profile) -> bool`: define mínimos distintos antes de
  puntuar. Evita que ausencias convertidas en cero ganen un ranking.
- `_score(...) -> float`: reglas lineales relativas para equilibrado, proteína, ligero,
  recuperación y objetivo personalizado. No deben interpretarse como recomendación
  médica ni compararse entre perfiles.
- `_explanation(combo, profile) -> str`: explicación determinista del ganador.
- `_candidate_combinations(rows) -> list[MenuCombination]`: agrupa por sede y calcula
  producto de `[primero opcional] × segundo × [guarnición opcional] × [postre opcional]`.
  Requiere segundo y limita cada lista a 6/8/4/3 para contener memoria y CPU.
- `_best(candidates, profile, **targets) -> Recommendation | None`: filtra, elige por
  `max` y devuelve una copia con perfil, puntuación y razón sin mutar candidatos.
- `recommend_daily(...) -> Recommendations`: calcula cuatro perfiles globales, uno
  personalizado opcional y una opción equilibrada por restaurante.

## `mailer.py`: presentación y SMTP

- `DailyReport`: `dataclass(frozen=True)` que mantiene juntos el email HTML, su
  alternativa de texto, el informe interactivo, la URL opcional y la clave idempotente.
- `human_date(value) -> str`: fecha española; propaga `ValueError` si la entrada no es ISO.
- `number(value) -> str`: un decimal máximo o `—`.
- `_report_url(base_url, menu_date) -> str | None`: solo acepta bases HTTPS sin
  credenciales, query ni fragmento; así nunca construye un botón sobre una URL dudosa.
- `_grouped_recommendations(...)`: agrupa objetivos que eligieron los mismos hashes y
  evita duplicar el menú sin alterar qué opción ganó cada perfil.
- `_build_email_html(...)`: genera únicamente cabecera, recomendaciones y cuatro
  tarjetas de sede. No contiene JavaScript ni tablas nutricionales.
- `_dish_card(row)`: usa la clasificación compartida de `nutrition.py`, presenta las
  cinco métricas primero y relega ingredientes, alérgenos y procedencia a `details`.
- `_build_interactive_html(...)`: incorpora los cuatro paneles en un documento estático;
  JavaScript solo cambia visibilidad, filtro, orden y hash, sin red ni backend.
- `build_report(...) -> DailyReport`: función pura; mantiene el argumento histórico
  `extracted_at`, pero ya no expone ese dato técnico al lector.
- `save_preview` guarda el informe interactivo; `save_email_preview` guarda el cuerpo
  exacto del correo; `save_site_report` y `save_site_index` construyen el artefacto de Pages.
- `_build_email_message(...)`: garantiza `text/plain` + `text/html`; cuando no existe
  URL pública añade el informe autónomo como adjunto HTML.
- `send_report(database, report, force, preview_path) -> bool`: omite claves ya `sent`
  salvo `force`, valida entorno, crea mensaje multiparte y elige SSL directo en 465 o
  STARTTLS en otros puertos. Registra `sending` antes de red y `sent` después de
  `send_message`. Cualquier excepción queda `failed_or_uncertain` y se propaga.

SMTP no permite garantizar exactamente una entrega: una desconexión puede ocurrir
después de que el servidor acepte el mensaje pero antes de que el cliente reciba la
respuesta. Por eso el estado no se marca simplemente como `failed`.

## `app.py`: ejecución reactiva de Streamlit

El archivo es un script declarativo; no expone una clase de aplicación. En cada rerun:

1. Inicializa/verifica SQLite y consulta fechas.
2. Carga menú, estado y recomendaciones de la fecha elegida.
3. Renderiza métricas, tarjetas y tablas.
4. Valida la calculadora con las mismas reglas de `nutrition.py`.
5. Solo al pulsar el botón construye y guarda una vista previa.

`formatted_metric(field, unit)` es un cierre local que captura `item`. Se invoca de
inmediato dentro de la misma iteración; no se guarda para ejecución posterior, evitando
el problema habitual de cierres que terminan observando el último valor del bucle.
El `format_func` del multiselect es un callback de presentación: el valor estable del
widget sigue siendo el hash y la etiqueta puede cambiar sin perder la selección.

## `demo.ipynb`: recorrido operativo

El notebook llama a las mismas funciones públicas en siete fases. `provider = "none"`
y `SEND_TEST_EMAIL = False` son barreras deliberadas: abrir o ejecutar todas las celdas
no consume cuota ni envía correo. `_prompt_for_dish` es una función privada importada
solo para enseñanza; otro código de producción no debería depender de ella.

## Configuración y automatización

- `.env.example` enumera opciones sin valores privados. `.env` tiene prioridad menor
  que variables ya inyectadas y está ignorado por Git.
- `requirements.txt` solo contiene dependencias de ejecución/interfaz; SQLite, SMTP,
  email y tipos pertenecen a la librería estándar.
- `requirements-dev.txt` añade `mypy` de forma opcional y `mypy.ini` fija Python 3.11,
  módulos revisados y prohibición de funciones sin anotaciones completas.
- `.github/workflows/daily_menu.yml` instala Python 3.11, ejecuta el pipeline, conserva
  el HTML siete días y versiona SQLite/backups. Una ejecución programada envía una vez;
  una manual marcada añade `--force-email`. `contents: write` es necesario para el
  commit, y `concurrency` evita escrituras simultáneas.
- El workflow hospedado no puede acceder al `localhost:11434` de un ordenador personal.
  Ollama solo funciona allí con ejecución local o un runner autohospedado. Para runners
  hospedados, el backend remoto implementado es Gemini.

## Comportamientos dudosos que no se han cambiado

- Las unidades nutricionales oficiales no declaran base por ración o 100 g. Las sumas
  se muestran como comparativas, no como mediciones clínicas.
- La clasificación por texto y las señales vegetarianas son heurísticas.
- El parser confía en la forma de colecciones JSON una vez decodificadas.
- El HTML se construye como una cadena larga para compatibilidad de correo; separarlo
  en plantillas sería un refactor funcional fuera de esta revisión.
- El histórico SQLite binario crece en Git como copias completas.
- Al retirar la suite de pruebas por decisión del repositorio, queda menos protección
  frente a regresiones. La validación mínima restante es compilación, importación y una
  ejecución real sin correo.
