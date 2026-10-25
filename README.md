# Daily Menu Insights

Daily Menu Insights es una aplicación Python que automatiza la consulta diaria de
varios menús, normaliza los platos, conserva un histórico local en SQLite, completa
con Gemini determinados valores nutricionales ausentes y genera recomendaciones e
informes HTML enviados por correo.

El proyecto funciona como un pipeline completo: descarga la información, la
transforma a una estructura común, la persiste, calcula combinaciones de menú y
presenta el resultado en un email breve y en un informe interactivo más detallado.

## Flujo del proceso

1. `scraper.py` resuelve la fecha en la zona horaria de Madrid y descarga el HTML
   del origen configurado.
2. El parser localiza las colecciones JSON incrustadas en la página y relaciona
   menús, grupos, platos, ingredientes, alérgenos y valores nutricionales.
3. Los datos se normalizan para que todos los platos tengan el mismo formato. Cada
   receta recibe un hash que permite deduplicarla y reconocerla en otras fechas o
   sedes.
4. `database.py` guarda la extracción en SQLite y registra el estado de cada sede.
   Una extracción correcta sustituye la fotografía completa de esa fecha para no
   conservar platos retirados o pertenecientes a un día anterior.
5. `nutrition.py` consulta la caché y, cuando faltan métricas centrales, puede pedir
   una estimación a Gemini. Los datos publicados conservan siempre la prioridad.
6. El mismo módulo construye combinaciones válidas y elige las mejores opciones
   mediante reglas deterministas.
7. `mailer.py` genera el email, la alternativa de texto y el informe HTML
   interactivo. Si se solicita el envío, el mensaje se entrega mediante SMTP.

## Estructura del proyecto

```text
scraper.py      extracción, normalización, CLI y orquestación diaria
database.py     esquema, consultas, transacciones y copias de SQLite
nutrition.py    Gemini, validación nutricional y recomendaciones
mailer.py       email HTML, informe interactivo y envío SMTP
models.py       contratos de tipos compartidos entre módulos
app.py          dashboard local construido con Streamlit
demo.ipynb      recorrido manual de las fases del pipeline
```

La separación por módulos evita mezclar el contrato de la página de origen con la
base de datos, las reglas nutricionales o la presentación. Los objetos que circulan
entre módulos están descritos mediante `TypedDict`, `Literal`, alias de tipos y un
`Protocol` para los argumentos del pipeline.

## Extracción y normalización

La página se descarga una vez por intento mediante `requests`, con timeout y
comprobaciones mínimas del contenido esperado. El JSON no se obtiene buscando el
primer corchete de cierre: el parser recorre el texto respetando llaves, corchetes,
cadenas y caracteres escapados. Esto permite leer descripciones complejas sin
cortar prematuramente los datos.

El sistema interpreta el ciclo de menús aplicable a la fecha solicitada y crea un
resultado independiente para cada sede. Los estados posibles son menú correcto,
menú no publicado o error. Si la extracción queda incompleta, se realizan reintentos
y se conserva únicamente la última fotografía obtenida; nunca se combinan platos
de distintos intentos o fechas.

Durante la normalización se limpian espacios, se interpretan números con coma
decimal y se convierten ingredientes, alérgenos, grupos y categorías en estructuras
consistentes. El texto visible no se modifica innecesariamente. Un hash SHA-256
identifica el contenido normalizado de cada receta y evita duplicados dentro de una
misma sede.

## Base de datos SQLite

La base se crea automáticamente en `data/menus.db`. Esta carpeta no forma parte del
repositorio y aparece cuando se ejecuta el proyecto por primera vez.

El esquema contiene:

- `restaurants`: sedes configuradas.
- `dishes`: recetas normalizadas y su contenido original.
- `menu_items`: platos disponibles por fecha y sede.
- `nutrition_estimates`: estimaciones validadas y sus metadatos.
- `extraction_runs`: resultado de cada intento de extracción.
- `report_runs`: estado de generación y envío de cada informe.

`MenuDatabase` abre conexiones cortas y utiliza transacciones. Una operación correcta
se confirma y una excepción provoca `rollback`. Los `upsert` hacen repetible la
ejecución sin duplicar recetas, mientras que la sustitución de `menu_items` mantiene
una fotografía exacta del menú publicado.

## Gemini y valores nutricionales

Gemini se utiliza únicamente para completar métricas ausentes. El prompt incluye el
nombre del plato, la descripción, los ingredientes, su clasificación y los valores
publicados que ya existen. La respuesta debe ser un objeto JSON con una estructura
conocida.

Antes de guardar una estimación, el código valida los números, la ración asumida, la
confianza, los supuestos y los campos que el modelo no ha podido determinar. Una
cifra oficial nunca se reemplaza por una estimación. Las respuestas aceptadas se
guardan por hash y versión del prompt, evitando repetir llamadas para una misma
receta cuando la estimación sigue siendo compatible.

El modelo de lenguaje no decide las recomendaciones. Su resultado es solo una fuente
secundaria de datos para las reglas implementadas en Python.

## Recomendaciones

Cada plato se clasifica como primero, segundo, guarnición, postre u otro. Una
combinación puede contener como máximo un plato de cada curso y debe pertenecer a
una única sede. El segundo es obligatorio y se descartan selecciones duplicadas o
con bases evidentes repetidas.

El sistema compara combinaciones para obtener:

- Menú equilibrado.
- Opción alta en proteínas.
- Opción ligera.
- Opción post-entrenamiento.
- Mejor combinación equilibrada de cada sede.

Los totales nutricionales solo se calculan cuando todos los platos de la combinación
tienen la métrica correspondiente. Un dato ausente se mantiene como desconocido y
no se convierte en cero. Las puntuaciones son relativas al perfil evaluado y no se
presentan como recomendaciones médicas.

## Email e informe interactivo

El informe tiene dos niveles. El email contiene la fecha, las mejores opciones y un
resumen de cada sede. Cuando una misma combinación gana en varios perfiles, se
presenta una sola vez con varios distintivos.

El informe HTML completo permite cambiar de sede, filtrar por categoría, ordenar los
platos y desplegar ingredientes, alérgenos y procedencia nutricional. Los valores
publicados, estimados y desconocidos se distinguen sin mezclar las métricas con el
texto secundario. El diseño del HTML se desarrolló con apoyo de Codex y utiliza HTML,
CSS y JavaScript nativo, sin frameworks de frontend.

El correo se construye como un mensaje MIME con una alternativa `text/plain` y otra
`text/html`. Cuando no existe una URL configurada para el informe, el HTML completo
se adjunta al mensaje. El estado almacenado en `report_runs` evita reenviar
automáticamente el mismo informe diario.

## Automatización con GitHub Actions

El workflow `.github/workflows/daily_menu.yml` se ejecuta de lunes a viernes a las
10:30 en la zona horaria `Europe/Madrid`. También permite una ejecución manual.

El job realiza checkout, configura Python 3.11, instala `requirements.txt` y ejecuta
el pipeline. Las ejecuciones programadas envían el correo. En una ejecución manual,
el envío solo se activa al marcar la opción `send_email`.

El runner de GitHub es temporal, por lo que la base SQLite creada durante una
ejecución no se conserva al terminar. El histórico completo se mantiene cuando el
programa se ejecuta en local.

## Instalación local

Se necesita Python 3.11 o posterior:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Crea un archivo local `.env` con la configuración necesaria. Las variables
principales son:

```text
MENU_SOURCE_URL
MENU_TARGETS_JSON
GEMINI_API_KEY
GEMINI_MODEL
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
EMAIL_FROM
EMAIL_FROM_NAME
EMAIL_TO
```

También pueden configurarse `REPORT_TITLE`, `MENU_SOURCE_LABEL`,
`ALLOW_PARTIAL_EMAIL`, `SCRAPE_ATTEMPTS` y `SCRAPE_RETRY_SECONDS`.

## Ejecución

Vista previa sin llamar a Gemini:

```powershell
python scraper.py --date today --no-llm --preview output/menu_hoy.html
```

Vista previa utilizando Gemini para completar campos ausentes:

```powershell
python scraper.py --date today --provider gemini --preview output/menu_hoy.html
```

Envío del correo después de generar las vistas previas:

```powershell
python scraper.py --date today --provider gemini --preview output/menu_hoy.html --send-email
```

Dashboard local:

```powershell
streamlit run app.py
```

Comprobaciones del código:

```powershell
python -m compileall -q app.py database.py mailer.py models.py nutrition.py scraper.py
python -m mypy
python scraper.py --help
```

Los datos nutricionales y las estimaciones son orientativos. Los alérgenos deben
confirmarse siempre con la fuente correspondiente.
