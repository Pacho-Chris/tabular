---
name: tabular
description: >
  Limpieza, normalización y gestión de datos CSV con uv, pandas y SQLite.
  Importa un CSV una sola vez, archiva la fuente y convierte la base SQLite en
  la fuente de verdad para consultar y mutar con CRUD y DDL controlado. Úsala
  siempre que el usuario comparta un CSV, pida limpiar o normalizar datos,
  crear una base, consultar, actualizar o eliminar registros, agregar columnas,
  o crear funciones de consulta reutilizables, aunque no mencione pandas,
  SQLite ni normalización. Trabaja exclusivamente con CSV: si el usuario trae
  XLSX o XLS, pídele exportarlo a CSV UTF-8; no proceses hojas de cálculo. No
  usar para editar formato de Excel, PDFs, scraping ni dashboards.
license: MIT
compatibility: Requiere uv, Python 3.12+ y SQLite. Solo entrada CSV. Funciona en Windows, macOS y Linux.
metadata:
  version: "0.3.0-rc1"
  opencode/slash: "true"
---

# tabular

El CSV es la semilla. Después de `init`, **la base SQLite es la fuente de
verdad** y el proyecto se trabaja con consultas, CRUD y cambios de estructura
controlados.

La IA decide y explica; `scripts/tabular.py` ejecuta, valida y deja historial.

## Entrada válida: solo CSV

Acepta únicamente archivos `.csv`. Si el usuario adjunta `.xlsx` o `.xls`:

1. No lo abras.
2. No instales `openpyxl` ni conviertas con LibreOffice.
3. Responde: “Solo proceso CSV. Guarda la hoja como CSV UTF-8 y vuelve a
   subirla.”
4. Espera el CSV antes de crear el proyecto.

`tabular.py` también rechaza cualquier archivo que no termine en `.csv`.

## Principios

1. **El CSV se importa una sola vez.** `init` crea la base, verifica, archiva
   el CSV y registra su SHA-256 en `_imports`.
2. **Después, la base manda.** No se reconstruye desde el CSV en cada consulta.
3. **Todo cambio de datos pasa por `mutate`.** `query` es solo lectura.
4. **Nada se aplica sin confirmación.** `mutate` hace rollback por defecto;
   solo con `--confirm` aplica.
5. **`DELETE` pide confirmación extra.** Usa `--confirm-delete`; si no hay
   undo, exige `--allow-irreversible` o `--backup`.
6. **DDL destructivo pide confirmación extra.** `DROP`/`RENAME` requiere
   `--allow-destructive` además de `--confirm`.
7. **Todo cambio deja historial.** `_changes`, `reports/changes/`, backups y
   snapshots de esquema.
8. **Si `verify --integrity` falla, no se entrega.**
9. **El mapping ayuda al agente, pero después de un DDL puede quedar viejo.**
   Usa `schema --check` y `schema --write`.

## Flujo

### 0. Identificar CSV y proyecto

Deriva un nombre snake_case: `ventas_enero.csv` → `ventas_enero`. Un proyecto
corresponde a un CSV inicial. Si hay varios, crea un proyecto por archivo.

### 1. Preparar el proyecto

Sigue `references/setup.md` para instalar uv, crear el proyecto, copiar
`tabular.py`, crear carpetas y configurar `.gitignore`. En Windows usa el
bloque PowerShell de ese archivo.

Los comandos posteriores son iguales en todos los sistemas:

```bash
uv run python scripts/tabular.py ...
```

### 2. Perfilar el CSV

```bash
uv run python scripts/tabular.py profile data/raw/ventas_enero.csv
```

Genera `schema/profile.json`. Revisa encoding, delimitador, encabezado,
duplicados, nulos, tipos, footer y candidatos a clave/dimensión.

Si el perfil se equivocó de encabezado o delimitador, repite con
`--header-row`, `--delimiter` o `--encoding`. No edites el CSV.

### 3. Interpretar y escribir el mapping

Lee `references/data-guide.md` para la especificación completa. El mapping
declara grano, tablas, dimensiones, hechos, claves, tipos y FKs.

Reglas rápidas:

- `id` se agrega automáticamente.
- `kind` es `dimension` o `fact`.
- Toda dimensión necesita `key`.
- `MONEY` se guarda en centavos.
- `DATE` se guarda como `YYYY-MM-DD`.
- IDs con ceros iniciales se declaran `TEXT`.
- Encabezados duplicados o vacíos usan `source_index` (1-based).

Pregunta al humano solo si hay ambigüedad real: grano, fechas
`dd/mm` vs `mm/dd`, clave con conflictos, filas de totales o moneda.

### 4. Importar una sola vez

```bash
uv run python scripts/tabular.py init schema/mapping.json
```

`init`:

1. construye una base temporal;
2. valida el mapping y convierte los datos;
3. verifica conteos, totales, FKs e integridad;
4. archiva el CSV en `data/imports/AAAA-MM-DD_nombre.csv` como solo lectura;
5. registra `_imports` y el primer `_changes`;
6. reemplaza la base de forma atómica;
7. escribe `schema/schema.sql`.

Si hay filas rechazadas, revisa `reports/rechazos.csv` (solo se crea si hay
rechazos). Solo usa `--allow-rejects` tras decidir que esos rechazos son
aceptables. `init` ya escribe `schema/schema.sql`; `schema --write` solo es
necesario después de un DDL o de cambiar una vista.

Si ya existe una base, `init` se niega. `init --force` hace backup y recrea;
pierde los cambios e historial de la base. `--delete-source` borra el CSV
original solo después de archivar y verificar.

### 5. Reporte inicial

Escribe `reports/ANALISIS.md` con la plantilla de `references/templates.md`.
Incluye tablas, filas, decisiones, calidad, consultas de ejemplo y cómo
reconstruir.

### 6. Trabajo diario: consultas

```bash
uv run python scripts/tabular.py query "SELECT ..."
uv run python scripts/tabular.py query --file sql/queries/ventas_mes.sql
uv run python scripts/tabular.py schema
uv run python scripts/tabular.py verify --integrity
```

- `query` solo permite `SELECT`, `WITH` y `EXPLAIN`.
- Responde primero el dato, después el contexto.
- Si la pregunta pide una cifra, ejecútala; no la calcules de memoria.
- Para consultas frecuentes, guarda SQL en `sql/queries/`.
- `init` aplica las vistas de `sql/views/`. Después de `init`, para crear o
  cambiar una vista usa `mutate` y luego `schema --write`.

### 7. CRUD y mutaciones

`mutate` es el único punto de escritura. Por defecto hace rollback; con
`--confirm` aplica.

```bash
# simulación
uv run python scripts/tabular.py mutate --sql "UPDATE ventas SET cantidad=3 WHERE id=42"

# aplicar
uv run python scripts/tabular.py mutate --sql "UPDATE ventas SET cantidad=3 WHERE id=42" --confirm
```

Antes de aplicar, muestra al usuario:

- qué cambia;
- cuántas filas;
- si hay undo;
- si hay backup;
- cualquier advertencia.

Nunca uses `--confirm` sin un “sí” explícito del usuario. Si trabajas en un
subagente sin humano, detente en el dry-run y no apliques; solo aplica si el
prompt del usuario autorizó explícitamente esa mutación. El protocolo detallado
y los flags están en `references/mutate.md`.

### 8. Cambios de estructura

`mutate` también acepta DDL:

```bash
uv run python scripts/tabular.py mutate --sql "ALTER TABLE ventas ADD COLUMN descuento_centavos INTEGER" --confirm
```

- Los DDL hacen backup y snapshot del esquema.
- `DROP`/`RENAME` requieren `--allow-destructive`.
- Después de un DDL, ejecuta `schema --check`; si hay diferencias, actualiza
  `mapping.json` o `schema.sql`.

### 9. Historial y undo

```bash
uv run python scripts/tabular.py history
uv run python scripts/tabular.py history --show 7
```

Para revertir una operación con undo:

```bash
uv run python scripts/tabular.py mutate --file reports/changes/<timestamp>_<tipo>_undo.sql --confirm
```

`DELETE`/`INSERT` generan undo automático con `RETURNING`. `UPDATE` captura el
antes con una consulta. Si no se puede generar undo, el script lo advierte y
exige backup o `--allow-irreversible`.

### 10. Funciones persistentes

Crea `functions/func_<nombre>.py` con conexión de solo lectura y un diccionario
`META` (nombre, descripción, parámetros con tipo/requerido). Las fechas de
corte se reciben como argumentos; nunca uses `date('now')`.

Registra la función en `FUNCTIONS.md` y pruébala antes de entregar. La
plantilla está en `references/templates.md`.

## Confirmación antes de escribir

1. Ejecuta `mutate` sin `--confirm`.
2. Muestra el resumen y las filas afectadas.
3. Pregunta: “¿Confirmas?”. Para `DELETE`, di cuántas filas se borran y si hay
   undo. Si no hay undo, dilo y ofrece backup o cancelar.
4. Solo tras un “sí”, ejecuta con `--confirm` y el flag extra que corresponda.

Flags:

- `--confirm`: aplicar.
- `--confirm-delete`: obligatorio para `DELETE`.
- `--allow-irreversible`: sin undo ni backup.
- `--allow-destructive`: `DROP`/`RENAME`.
- `--allow-full-table`: `UPDATE`/`DELETE` sin `WHERE`.
- `--backup`: forzar backup completo.
- `--capture-sql`: SELECT previo para undo de `UPDATE`.

## Anti-patrones

- Reconstruir la base desde el CSV después de `init`.
- Editar el `.sqlite` con DB Browser en lugar de `mutate`.
- Ejecutar `mutate --confirm` sin mostrar el resumen al usuario.
- Borrar sin `--confirm-delete`.
- Ocultar que una operación no tiene undo.
- Asumir que `mapping.json` sigue siendo la verdad después de un DDL.
- Usar `date('now')` en una función persistente.
- Exportar o procesar XLSX.

## Criterio de terminado

- Existe `uv.lock` y el proyecto corre con `uv run`.
- Existen `schema/profile.json`, `schema/mapping.json` y `schema/schema.sql`.
- `init` verificó y archivó el CSV; `_imports` tiene una fila.
- `verify --integrity` pasa.
- `reports/ANALISIS.md` está escrito.
- `schema --check` pasa o las diferencias están explicadas.
- Las consultas de ejemplo funcionan.
- No hay `openpyxl` ni archivos `.xlsx`.

## Referencias

- `references/setup.md`: instalación, Windows, scaffolding, backups.
- `references/data-guide.md`: CSV, tipos, mapping, normalización, SQL.
- `references/mutate.md`: CRUD, DDL, undo, confirmación e historial.
- `references/templates.md`: `ANALISIS.md` y función con `META`.
