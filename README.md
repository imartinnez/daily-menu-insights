# Daily Menu Insights

Aplicación Python para extraer menús desde una fuente configurada de forma privada,
guardar un histórico SQLite local, completar valores nutricionales ausentes con un
LLM opcional y enviar un informe HTML diario.

El repositorio es deliberadamente genérico: no contiene el dominio de origen,
identificadores de sedes, nombres de restaurantes, bases de datos, direcciones de
correo ni credenciales.

## Componentes

```text
scraper.py      extracción, normalización y comando diario
database.py     almacenamiento SQLite local
nutrition.py    estimaciones opcionales y recomendaciones deterministas
mailer.py       email compacto e informe HTML interactivo
models.py       contratos de tipos compartidos
app.py          dashboard local de Streamlit
demo.ipynb      recorrido manual del pipeline
```

La explicación de cada módulo se mantiene en
[`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md).

## Instalación local

Requiere Python 3.11 o posterior:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Crea manualmente un archivo `.env`. Está ignorado por Git y no existe una
plantilla pública para evitar publicar datos que identifiquen la fuente.

Variables privadas obligatorias:

```text
MENU_SOURCE_URL       URL HTTPS de la fuente
MENU_TARGETS_JSON     objeto JSON con las sedes que deben procesarse
```

La estructura genérica de `MENU_TARGETS_JSON` es:

```json
{
  "identificador": {
    "short_name": "Nombre privado de la sede",
    "location": "Ubicación privada"
  }
}
```

El identificador debe ser numérico. No copies valores reales a documentación,
issues, logs ni commits.

Variables opcionales:

```text
REPORT_TITLE
MENU_SOURCE_LABEL
LLM_PROVIDER
OLLAMA_BASE_URL
OLLAMA_MODEL
GEMINI_API_KEY
GEMINI_MODEL
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
EMAIL_FROM
EMAIL_FROM_NAME
EMAIL_TO
ALLOW_PARTIAL_EMAIL
SCRAPE_ATTEMPTS
SCRAPE_RETRY_SECONDS
```

## Ejecución y vistas previas

```powershell
python scraper.py --date today --no-llm --preview output/menu_hoy.html
```

Se generan dos archivos ignorados por Git:

- `output/menu_hoy.html`: informe interactivo completo.
- `output/menu_hoy_email.html`: cuerpo exacto del correo.

El informe permite cambiar de restaurante, filtrar categorías, ordenar platos y
desplegar ingredientes, alérgenos y procedencia nutricional. Los datos publicados,
estimados y desconocidos se distinguen mediante `●`, `*` y `—`.

Cuando no se configura un alojamiento HTTPS privado, el informe completo se adjunta
al correo. Este es el comportamiento usado por el workflow público: no despliega
GitHub Pages ni publica el HTML como artifact.

## SQLite

La base se crea por defecto en `data/menus.db`. Tanto esa ruta como los backups se
ignoran expresamente y nunca deben añadirse al repositorio público.

```powershell
python scraper.py --date today --no-llm --backup --preview output/menu_hoy.html
```

En local se conserva el histórico y la caché de estimaciones. El runner hospedado de
GitHub es efímero: el workflow público no vuelve a confirmar SQLite ni backups. Si se
necesita persistencia remota, debe usarse un almacenamiento privado independiente.

## LLM opcional

Los valores oficiales siempre tienen prioridad. El LLM solo intenta completar
campos centrales ausentes y la respuesta se valida antes de guardarse.

- `LLM_PROVIDER=none`: no realiza llamadas externas.
- `LLM_PROVIDER=ollama`: usa un servidor local configurado mediante variables.
- `LLM_PROVIDER=gemini`: requiere `GEMINI_API_KEY` como secreto.

No introduzcas claves directamente en comandos, notebooks, archivos de ejemplo o
capturas. En GitHub deben configurarse en **Settings → Secrets and variables →
Actions → Secrets**.

## Correo

El envío requiere `--send-email` y las variables SMTP privadas. El mensaje contiene
una alternativa `text/plain`, un cuerpo `text/html` y el informe interactivo adjunto.

```powershell
python scraper.py --date today --no-llm --preview output/menu_hoy.html --send-email
```

El estado `sent` evita duplicados para una fecha. `--force-email` solo debe usarse
después de comprobar el buzón.

## GitHub Actions

Configura estos Secrets sin revelar sus valores:

```text
MENU_SOURCE_URL
MENU_TARGETS_JSON
SMTP_HOST
SMTP_PORT
SMTP_USERNAME
SMTP_PASSWORD
EMAIL_FROM
EMAIL_TO
GEMINI_API_KEY        solo si se usa Gemini
```

El workflow:

- Tiene permiso de solo lectura sobre el repositorio.
- No publica Pages.
- No sube HTML, SQLite ni backups como artifacts.
- No escribe datos en Git.
- No imprime el dominio, los nombres de las sedes ni los platos en sus logs.
- Envía el informe programado directamente por SMTP.

Las ejecuciones manuales no envían correo salvo que se marque expresamente
`send_email`.

## Comprobaciones

```powershell
python -m compileall -q app.py database.py mailer.py models.py nutrition.py scraper.py
python -m mypy
python scraper.py --help
```

Antes de publicar un cambio, revisa también los archivos rastreados y el historial:

```powershell
git grep -in "nombre_que_no_debe_publicarse"
git log --all -- .env
git status --short
```

## Seguridad

- `.env`, SQLite, backups, `output/` y `site/` están ignorados.
- El dominio y las sedes se inyectan mediante Secrets.
- Los nombres de variables de entorno no son credenciales; sus valores sí lo son.
- Una clave que haya llegado a Git debe rotarse aunque después se borre.
- Un commit posterior no elimina contenido del historial: para retirar datos ya
  publicados hay que reescribir la rama y revisar copias, forks y caches del proveedor.

Los datos nutricionales son orientativos. Los alérgenos y decisiones médicas deben
confirmarse con el proveedor correspondiente.
