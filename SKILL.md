---
name: tabular
description: >
  Limpieza, normalización y consulta de datos CSV con uv, pandas y SQLite.
  Convierte un CSV desordenado en un proyecto reproducible con base de datos
  normalizada, consultas SQL y funciones persistentes. Úsala siempre que el
  usuario comparta un CSV, pida limpiar o normalizar datos, migrar un reporte
  a una base de datos, responder preguntas como "producto más vendido" o
  "ventas por mes", o crear herramientas de consulta reutilizables, aunque no
  mencione pandas, SQLite ni normalización. Trabaja exclusivamente con CSV:
  si el usuario trae XLSX o XLS, pídele exportarlo a CSV UTF-8; no proceses
  hojas de cálculo. No usar para editar formato de Excel, PDFs, scraping ni
  dashboards.
license: MIT
compatibility: Requiere uv, Python 3.12+ y SQLite. Solo entrada CSV. Funciona en Windows, macOS y Linux.
metadata:
  version: "0.2.0-rc1"
  opencode/slash: "true"
---

# tabular

Convierte un CSV en un proyecto de datos reproducible: CSV inmutable, base
SQLite normalizada, consultas SQL y funciones reutilizables. La IA analiza,
decide y explica; `scripts/tabular.py` ejecuta la transformación sin improvisar.

## Entrada válida: solo CSV

Acepta únicamente archivos `.csv`. Si el usuario adjunta `.xlsx`, `.xls` o
cualquier hoja de cálculo:

1. No lo abras.
2. No instales `openpyxl` ni conviertas con LibreOffice.
3. Responde: “Solo proceso CSV. Guarda la hoja como CSV UTF-8 y vuelve a
   subirla.”
4. Espera el CSV antes de crear el proyecto.

`tabular.py` también rechaza cualquier archivo que no termine en `.csv`.

## Requisitos

- `uv` instalado y disponible en el PATH.
- Python 3.12 gestionado por uv; no hace falta instalarlo a mano.
- `pandas` en el entorno del proyecto (`uv add pandas`).
- SQLite viene incluido en Python; no instales el CLI.
- Windows: instala uv con `winget install --id=astral-sh.uv -e` o con
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`,
  y reinicia la terminal.

## Principios

1. **El CSV original es inmutable.** Se copia a `data/raw/` y se registra su
   SHA-256. Nunca se edita el archivo que subió el usuario.
2. **Primero se perfila, luego se transforma.** No escribas pandas ad hoc para
   “ver qué trae”. Usa `profile`.
3. **Primero el grano, luego el esquema.** Define qué representa una fila antes
   de crear tablas.
4. **SQLite es la fuente de verdad para consultar.** pandas solo se usa para
   leer y transformar; las respuestas salen de SQL.
5. **Nada se descarta en silencio.** Toda fila o valor rechazado queda en
   `reports/rechazos.csv` y se explica en el reporte.
6. **No se responde una cifra sin ejecutarla.** Toda respuesta numérica debe
   venir de `tabular.py query` o de una función persistente.
7. **Si `verify` falla, no se entrega.** No hay consultas ni reporte sobre una
   base inconsistente.

## Flujo obligatorio

### 0. Identificar el CSV y el proyecto

Confirma la ruta del CSV y deriva un nombre de proyecto en snake_case:
`ventas_enero.csv` → `ventas_enero`. Si el nombre no es obvio, pregunta.

Un proyecto corresponde a un CSV. Si el usuario entrega varios, crea un
proyecto por archivo y no los mezcles sin una instrucción explícita.

Si el archivo no es `.csv`, aplica la regla de entrada y detente.

### 1. Crear el proyecto con uv

Elige el bloque según tu shell. Los comandos `uv run python ...` posteriores
son idénticos en todos los sistemas.

#### Linux, macOS o Git Bash

Ejecuta desde el directorio donde quieras crear el proyecto:

```bash
SKILL_DIR="$HOME/.agents/skills/tabular"
PROJECT="ventas_enero"
CSV="/ruta/al/ventas_enero.csv"

uv init --bare --python 3.12 --name "$PROJECT" "$PROJECT"
cd "$PROJECT"
uv python pin 3.12
uv add pandas

mkdir -p data/raw db schema sql/views sql/queries functions reports scripts
cp "$SKILL_DIR/scripts/tabular.py" scripts/tabular.py
cp "$CSV" "data/raw/$(basename "$CSV")"

cat > .gitignore <<'EOF'
.venv/
__pycache__/
*.pyc
db/*.sqlite
db/*.sqlite-*
EOF
```

No sobrescribas un proyecto existente. Si la carpeta ya existe, pregunta si
continuar, usar otro nombre o actualizar el proyecto.

#### Windows (PowerShell)

Si la skill está instalada en `.config\opencode\skills`, ajusta `$SkillDir`.
No uses `mkdir -p`, `cp` ni here-documents de Bash.

```powershell
$SkillDir = Join-Path $env:USERPROFILE ".agents\skills\tabular"
$Project  = "ventas_enero"
$Csv      = "C:\datos\ventas_enero.csv"

uv init --bare --python 3.12 --name $Project $Project
Set-Location $Project
uv python pin 3.12
uv add pandas

$dirs = @("data\raw", "db", "schema", "sql\views", "sql\queries", "functions", "reports", "scripts")
foreach ($dir in $dirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Copy-Item (Join-Path $SkillDir "scripts\tabular.py") "scripts\tabular.py"
Copy-Item $Csv (Join-Path "data\raw" (Split-Path $Csv -Leaf))

@"
.venv/
__pycache__/
*.pyc
db/*.sqlite
db/*.sqlite-*
"@ | Set-Content -Encoding ascii .gitignore
```

Notas de Windows:

- Cierra Excel y DB Browser antes de `load` o `verify`; Windows bloquea los
  archivos abiertos.
- Usa una ruta corta y fuera de OneDrive, por ejemplo `C:\datos\ventas_enero`.
- No necesitas el comando `sqlite3`; usa `tabular.py query` y `tabular.py schema`.
- Para verificar el hash usa `Get-FileHash data\raw\ventas_enero.csv -Algorithm SHA256`.
- Si el proyecto está en WSL, instala la skill en el home de Linux y evita
  `/mnt/c` para `.venv` y la base de datos.

### 2. Perfilar el CSV

```bash
uv run python scripts/tabular.py profile data/raw/ventas_enero.csv
```

Genera `schema/profile.json` y muestra un resumen. Revisa:

- encoding y delimitador detectados;
- fila de encabezado detectada;
- filas duplicadas, vacías y candidatas a totales;
- columnas con ceros iniciales, fechas ambiguas, montos con coma decimal;
- candidatos a clave y a dimensión;
- columnas con nulos disfrazados (`N/A`, `-`, `NULL`, vacío).

Si el perfil se equivocó de encabezado o delimitador, vuelve a ejecutar con
`--header-row`, `--delimiter` o `--encoding`. No edites el CSV.

### 3. Interpretar y preguntar solo lo necesario

Avanza automáticamente y documenta supuestos, salvo que encuentres:

- grano ambiguo (¿una fila es una venta o un resumen?);
- fechas ambiguas (`03/04/2026` con día y mes ≤ 12);
- una clave candidata con duplicados y valores contradictorios;
- filas de totales mezcladas con datos;
- moneda o separador decimal imposible de interpretar;
- encabezados duplicados o vacíos que impidan mapear.

En esos casos, pregunta al humano con datos concretos. En todo lo demás,
decide, aplica y deja el supuesto escrito en `reports/ANALISIS.md`.

### 4. Escribir el mapping

Lee `~/.agents/skills/tabular/references/data-guide.md` antes de escribir el
mapping. En Windows es
`%USERPROFILE%\.agents\skills\tabular\references\data-guide.md`. El archivo
`schema/mapping.json` declara el grano, las tablas, las columnas, los tipos,
las claves y las relaciones. Ejemplo para ventas:

```json
{
  "source": {
    "path": "data/raw/ventas_enero.csv",
    "header_row": 1,
    "footer_rows": 0
  },
  "locale": {
    "decimal": ",",
    "thousands": ".",
    "date": "dd/mm/yyyy"
  },
  "tables": [
    {
      "name": "categorias",
      "kind": "dimension",
      "key": ["nombre"],
      "columns": [
        {"name": "nombre", "source": "Categoría", "type": "TEXT", "required": true}
      ]
    },
    {
      "name": "productos",
      "kind": "dimension",
      "key": ["sku"],
      "columns": [
        {"name": "sku", "source": "SKU", "type": "TEXT", "required": true},
        {"name": "nombre", "source": "Producto", "type": "TEXT", "required": true},
        {"name": "categoria_id", "source": "Categoría", "type": "FK",
         "references": "categorias.nombre"}
      ]
    },
    {
      "name": "ventas",
      "kind": "fact",
      "columns": [
        {"name": "fecha", "source": "Fecha", "type": "DATE", "required": true},
        {"name": "producto_id", "source": "SKU", "type": "FK",
         "references": "productos.sku"},
        {"name": "cantidad", "source": "Cantidad", "type": "INTEGER", "required": true},
        {"name": "total_centavos", "source": "Total", "type": "MONEY", "required": true}
      ]
    }
  ]
}
```

Reglas:

- `id` se agrega automáticamente; no lo declares.
- `kind` es `dimension` o `fact`.
- Toda dimensión necesita `key` (clave natural, una o más columnas).
- Las FKs se declaran como `"FK"` y `references: "tabla.columna_natural"`.
- El dinero se declara `MONEY` y se guarda en centavos (`INTEGER`).
- Las fechas se declaran `DATE` y se guardan como `YYYY-MM-DD`.
- Los IDs y códigos con ceros iniciales se declaran `TEXT`.
- Si el CSV tiene encabezados duplicados, usa `source_index` (1-based) en la
  columna correspondiente.

### 5. Ensayar la carga

```bash
uv run python scripts/tabular.py load schema/mapping.json --dry-run
```

No reemplaza la base; solo escribe `reports/rechazos.csv` si hay rechazos.
Revisa el resumen y ese archivo:

- filas fuente, aceptadas y rechazadas;
- filas por tabla;
- control de totales;
- motivo de cada rechazo.

Si hay rechazos bloqueantes, corrige el mapping o pregunta al humano. Si son
aceptables, documenta la decisión y usa `--allow-rejects` en la carga real del
paso 6.

### 6. Cargar y verificar

Caso normal, sin rechazos:

```bash
uv run python scripts/tabular.py load schema/mapping.json
uv run python scripts/tabular.py verify --mapping schema/mapping.json
```

Si aceptaste rechazos de forma explícita, usa la misma opción en ambos comandos:

```bash
uv run python scripts/tabular.py load schema/mapping.json --allow-rejects
uv run python scripts/tabular.py verify --mapping schema/mapping.json --allow-rejects
```

`load` escribe en una base temporal y solo la reemplaza si la verificación
pasa. `verify` compara la base con el CSV: conteos, totales, FKs, claves,
nulos y hashes de fuente/mapping.

Si `verify` falla:

1. no respondas consultas;
2. no escribas el reporte final;
3. corrige el mapping o los datos de entrada con el humano;
4. repite dry-run, load y verify.

### 7. Reporte final

Escribe `reports/ANALISIS.md` con esta plantilla exacta:

```markdown
# Proyecto <nombre>

## Resumen
Qué contiene el CSV, cuántas filas y qué se construyó.

## Tablas creadas
| Tabla | Tipo | Filas | Propósito |
|---|---|---|---|
| categorias | dimensión | <filas> | Evita repetir el nombre de la categoría |
| productos | dimensión | <filas> | Catálogo único por SKU |
| ventas | hechos | <filas> | Una fila por línea de venta |

## Organización
Rutas de `data/raw`, `db`, `schema`, `sql`, `functions` y `reports`.

## Decisiones y supuestos
Encoding, delimitador, fechas, moneda, filas de totales excluidas,
deduplicación aplicada y cualquier decisión que cambie el resultado.

## Calidad de datos
Duplicados, nulos, filas rechazadas, totales de control y problemas detectados.

## Consultas de ejemplo
Preguntas que el usuario puede hacer y el SQL o comando equivalente.

## Funciones disponibles
Nombre, archivo, propósito y ejemplo de uso.

## Cómo reconstruir
`uv run python scripts/tabular.py load schema/mapping.json`
```

Además, actualiza `README.md` con el propósito del proyecto y los comandos
básicos.

## Consultas

```bash
uv run python scripts/tabular.py query "SELECT ..." --format table
uv run python scripts/tabular.py query --file sql/queries/ventas_mes.sql
uv run python scripts/tabular.py schema
```

Reglas:

- Solo `SELECT`, `WITH` y `EXPLAIN`; el CLI abre la base en modo lectura.
- Límite por defecto: 200 filas. Si truncas, dilo.
- Responde primero el dato, luego el contexto. Ejemplo: “El producto más
  vendido fue Laptop Pro, con 8 unidades.”
- Si la pregunta exige una cifra, ejecútala. No la calcules “de memoria” ni
  leyendo el CSV.
- Para preguntas complejas, crea una vista en `sql/views/*.sql` y vuelve a
  ejecutar `load` para aplicarla. Las vistas quedan versionadas.
- Guarda consultas reutilizables en `sql/queries/*.sql`.

## Funciones persistentes

Cuando el usuario pida una herramienta reutilizable, crea
`functions/func_<nombre>.py` con esta plantilla y regístrala en
`FUNCTIONS.md`:

```python
#!/usr/bin/env python3
"""Producto más vendido por unidades."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "ventas_enero.sqlite"


def producto_mas_vendido(limit: int = 5) -> list[dict]:
    sql = """
    SELECT p.nombre AS producto, SUM(v.cantidad) AS unidades
    FROM ventas AS v
    JOIN productos AS p ON p.id = v.producto_id
    GROUP BY p.id, p.nombre
    ORDER BY unidades DESC, producto ASC
    LIMIT :limit
    """
    uri = f"file:{DB_PATH}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, {"limit": limit})]


if __name__ == "__main__":
    print(json.dumps(producto_mas_vendido(), ensure_ascii=False, indent=2))
```

Ajusta el nombre de la base y la consulta. Pruébala antes de entregar:

```bash
uv run python functions/func_producto_mas_vendido.py
```

En `FUNCTIONS.md`, agrega una fila con nombre, archivo, propósito, parámetros y
ejemplo. No dupliques lógica: si la consulta crece, muévela a una vista SQL.

## Anti-patrones

- Leer `.xlsx` o intentar convertirlo dentro de la skill.
- Leer el CSV completo en el contexto en lugar de usar `profile`.
- Editar `data/raw/` o “limpiar” el archivo original.
- Crear tablas a mano con `sqlite3` fuera del mapping.
- Cargar sin `--dry-run` y sin revisar `reports/rechazos.csv`.
- Borrar duplicados sin declarar la clave de negocio.
- Guardar dinero como `REAL` o porcentajes sin documentar la escala.
- Responder con pandas en memoria en lugar de consultar SQLite.
- Ocultar filas rechazadas o totales que no cuadran.
- Entregar sin `verify` en verde.

## Criterio de terminado

- Existe `uv.lock` y el proyecto corre con `uv run`.
- Existen `schema/profile.json`, `schema/mapping.json` y `schema/schema.sql`.
- La base se reconstruye desde cero con un comando.
- `verify` pasa sin errores.
- `reports/ANALISIS.md` está escrito.
- Las consultas de ejemplo funcionan.
- El SHA-256 de `data/raw` no cambió.
- No hay `openpyxl` ni archivos `.xlsx` en el proyecto.

## Referencia

Lee `~/.agents/skills/tabular/references/data-guide.md` (en Windows,
`%USERPROFILE%\.agents\skills\tabular\references\data-guide.md`) para:

- trampas específicas de CSV;
- especificación completa de `mapping.json`;
- reglas de tipos, fechas, dinero y nulos;
- criterios de normalización;
- recetas SQL y patrones de consulta.
