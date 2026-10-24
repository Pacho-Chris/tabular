---
name: tabular
description: >
  Análisis y limpieza de cualquier CSV, creación de una base SQLite y runtime
  CRUD para trabajar esa base. Usa csvsqlite como herramienta temporal de
  validación, plantilla y construcción; después conserva solo la plantilla, el
  CSV archivado, la base SQLite y tabular.py. Úsala cuando el usuario comparta
  un CSV, pida validar o limpiar datos, crear una base, consultar, actualizar o
  eliminar registros, agregar columnas o crear funciones reutilizables, aunque
  no mencione pandas, SQLite ni normalización. Solo CSV; si trae XLSX, pide
  exportarlo a CSV UTF-8. No usar para editar formato de Excel, PDFs, scraping
  ni dashboards.
license: MIT
compatibility: Requiere uv, Python 3.12+ y SQLite. Solo entrada CSV. Windows, macOS y Linux.
metadata:
  version: "0.4.0-rc1"
  opencode/slash: "true"
---

# tabular

Dos herramientas con dos ciclos de vida:

```text
csvsqlite.py   → andamio temporal: check → init → build
plantilla.yaml → documentación del esquema (se conserva)
proyecto.sqlite → base definitiva (se conserva)
tabular.py     → runtime permanente: query / mutate / history / schema / verify
CSV            → se conserva archivado
```

`csvsqlite.py` se usa una vez y se elimina del proyecto. `tabular.py` se queda.

## Entrada válida: solo CSV

Acepta únicamente `.csv`. Si el usuario adjunta `.xlsx` o `.xls`:

1. No lo abras.
2. No instales `openpyxl` ni conviertas con LibreOffice.
3. Pide: “Guarda la hoja como CSV UTF-8 y vuelve a subirla.”
4. Espera el CSV.

## Principios

1. **Cualquier CSV es válido como punto de partida.** No asumas clientes,
   productos ni tablas concretas.
2. **Por defecto, una sola tabla plana.** Separar columnas repetidas en tablas
   relacionadas es opcional y se pregunta al usuario.
3. **La IA lee el reporte de `csvsqlite check`.** No interpreta el CSV a ciegas.
4. **El dinero se confirma con el humano.** Si una columna es dinero, se avisa
   que se guardará en centavos: `286.96` → `28696`.
5. **`csvsqlite build` deja la base lista:** `id`, centavos, `_imports`,
   `_changes`, `schema.sql`, CSV archivado.
6. **Después, la base SQLite es la fuente de verdad.** El CSV no se reimporta.
7. **Todo cambio pasa por `tabular mutate`.** `query` es solo lectura.
8. **Nada se aplica sin confirmación.** `mutate` hace rollback por defecto.
9. **Todo cambio deja historial.** `_changes`, undo y backups.
10. **La plantilla es documentación.** `tabular` no la parsea; la estructura
    real está en SQLite y en `schema.sql`.

## Flujo

### 1. Validar el CSV (temporal)

Copia `scripts/csvsqlite.py` al proyecto de forma temporal, o ejecútalo desde
la skill. Después se elimina del proyecto.

```bash
python scripts/csvsqlite.py check "datos.csv" -o reports/check_datos.md
```

`check` detecta encoding, delimitador, tipos, fechas ambiguas, notación
científica, columnas mixtas, nulos, cabeceras y filas irregulares. No modifica
el CSV.

Si hay falsos bloqueantes en columnas de texto que contienen números:

```bash
python scripts/csvsqlite.py check "datos.csv" \
  --forzar-texto "Nro Factura" --forzar-texto "COMENTARIOS FC" \
  -o reports/check_datos.md
```

Para CSV con títulos o totales:

```bash
--header-row 3 --footer-rows 1
```

### 2. Preguntar al humano

La IA lee el reporte y pregunta solo lo necesario:

- ¿Cómo se llama la tabla principal? Propón el nombre del CSV.
- ¿Qué columnas son dinero? ¿En qué moneda? ¿Confirmas centavos?
- ¿Hay columnas repetitivas que quieras separar en una tabla relacionada?
- ¿Las fechas son `dd/mm` o `mm/dd`?
- ¿Hay filas de totales o encabezados desplazados?
- ¿Los bloqueantes son datos reales o columnas de texto con números?

No conviertas dinero ni separes tablas sin confirmación.

### 3. Generar la plantilla

```bash
python scripts/csvsqlite.py init "datos.csv" --tabla datos -o plantilla.yaml
```

Revisa `plantilla.yaml`:

- `tabla`: nombre de la tabla principal.
- `nombre_db`: nombre de cada columna en SQLite.
- `tipo`: `TEXT`, `INTEGER`, `REAL`, `DECIMAL`, `MONEY`, `BOOLEAN`, `DATE`,
  `DATETIME`, `TIME`.
- `MONEY` se guarda en centavos enteros.
- `separar_en` + `columna_tabla_separada`: extrae una columna repetitiva a una
  tabla relacionada. Es opcional.
- `clave_primaria`: clave natural única (se implementa como `UNIQUE`; el `id`
  interno sigue siendo la PK).
- `unico`, `indice`, `enum`, `formato_fuente`, `mapeo`.

### 4. Construir la base

```bash
python scripts/csvsqlite.py build "datos.csv" \
  -s plantilla.yaml \
  -o db/datos.sqlite \
  --raiz .
```

`build`:

1. valida todos los valores contra la plantilla;
2. crea la tabla principal con `id INTEGER PRIMARY KEY` y `_source_row`;
3. crea tablas relacionadas si hay `separar_en`;
4. convierte dinero a centavos;
5. activa `PRAGMA foreign_keys=ON`;
6. archiva el CSV en `data/imports/AAAA-MM-DD_nombre.csv`;
7. crea `_imports` y `_changes`;
8. escribe `schema/schema.sql`;
9. escribe `db/datos.sqlite`.

Si hay errores, no escribe la base y genera un reporte de errores.

Después de construir:

```bash
rm scripts/csvsqlite.py
```

El CSV queda archivado; no se borra.

### 5. Trabajo diario con `tabular.py`

```bash
python scripts/tabular.py query "SELECT ..."
python scripts/tabular.py schema
python scripts/tabular.py schema --write
python scripts/tabular.py verify --integrity
python scripts/tabular.py history
```

`tabular.py` no lee CSV, no crea la base y no usa pandas.

### 6. CRUD y DDL

```bash
# simulación: rollback
python scripts/tabular.py mutate --sql "UPDATE datos SET ... WHERE id=1"

# aplicar
python scripts/tabular.py mutate --sql "UPDATE datos SET ... WHERE id=1" --confirm
```

Antes de aplicar, muestra:

- qué cambia;
- cuántas filas;
- si hay undo;
- si hay backup;
- advertencias.

Nunca uses `--confirm` sin un “sí” explícito. En un subagente sin humano,
detente en el dry-run; solo aplica si el prompt autorizó esa mutación.

Flags:

- `--confirm`: aplicar.
- `--confirm-delete`: obligatorio para `DELETE`.
- `--allow-irreversible`: sin undo ni backup.
- `--allow-destructive`: `DROP`/`RENAME`.
- `--allow-full-table`: `UPDATE`/`DELETE` sin `WHERE`.
- `--backup`: forzar backup completo.
- `--capture-sql`: SELECT previo para undo de un `UPDATE`.

### 7. Historial y undo

```bash
python scripts/tabular.py history
python scripts/tabular.py history --show 7
python scripts/tabular.py mutate --file reports/changes/<timestamp>_<tipo>_undo.sql --confirm
```

`_changes` registra cada operación. `reports/changes/` guarda SQL, undo y
reporte. `db/backups/` guarda backups de DDL/destructivos o `--backup`.
Retención por defecto: 100 undo y 10 backups.

### 8. Funciones persistentes

Crea `functions/func_<nombre>.py` con conexión de solo lectura y `META`
(nombre, descripción, parámetros con tipo/requerido). Las fechas de corte se
reciben como argumentos; nunca `date('now')`.

Registra la función en `FUNCTIONS.md` y pruébala. Plantilla en
`references/templates.md`.

## Confirmación antes de escribir

1. Ejecuta `mutate` sin `--confirm`.
2. Muestra resumen y filas afectadas.
3. Pregunta: “¿Confirmas?”.
4. Para `DELETE`, di cuántas filas y si hay undo.
5. Solo tras un “sí”, aplica con `--confirm` y el flag extra.

## Qué queda en el proyecto

```text
proyecto/
├── db/datos.sqlite
├── data/imports/AAAA-MM-DD_datos.csv
├── plantilla.yaml
├── schema/schema.sql
├── scripts/tabular.py
├── sql/queries/
├── sql/views/
├── functions/
├── FUNCTIONS.md
├── reports/ANALISIS.md
├── reports/check_datos.md
└── README.md
```

Sin `csvsqlite.py`, sin `mapping.json`, sin `profile.json`, sin pandas.

## Anti-patrones

- Reimportar el CSV después de crear la base.
- Leer el CSV completo en el contexto en vez de usar `check`.
- Separar tablas sin preguntar.
- Convertir dinero a centavos sin avisar.
- Editar SQLite con DB Browser en vez de `mutate`.
- Ejecutar `mutate --confirm` sin confirmación.
- Ocultar que una operación no tiene undo.
- Usar `date('now')` en funciones.
- Procesar XLSX.

## Criterio de terminado

- `csvsqlite check` fue revisado y los bloqueantes explicados.
- `plantilla.yaml` está ajustada.
- `csvsqlite build` creó DB, `_imports`, `_changes` y `schema.sql`.
- El CSV está archivado.
- `verify --integrity` pasa.
- `reports/ANALISIS.md` está escrito.
- `csvsqlite.py` fue eliminado del proyecto.
- `tabular.py` funciona sin pandas.
- No hay `.xlsx` ni `openpyxl`.

## Referencias

- `references/setup.md`: uv, proyecto, Windows, estructura.
- `references/csvsqlite.md`: check/init/build, plantilla y separación.
- `references/tabular.md`: query/mutate/history/schema/verify.
- `references/data-guide.md`: trampas CSV, tipos, dinero, errores.
- `references/templates.md`: `ANALISIS.md` y funciones con `META`.
