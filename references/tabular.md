# tabular: runtime permanente

`tabular.py` trabaja sobre una base creada por `csvsqlite.py`. No lee CSV, no
crea la base y no usa pandas.

## Comandos

```bash
python scripts/tabular.py query "SELECT ..."
python scripts/tabular.py schema
python scripts/tabular.py schema --write
python scripts/tabular.py verify --integrity
python scripts/tabular.py history
python scripts/tabular.py history --show 7
python scripts/tabular.py mutate --sql "..." --confirm
```

`query` es solo lectura. `mutate` es el único punto de escritura.

## `query`

```bash
python scripts/tabular.py query "SELECT * FROM ventas LIMIT 10"
python scripts/tabular.py query --file sql/queries/ventas_mes.sql
python scripts/tabular.py query "SELECT ..." --format json
python scripts/tabular.py query "SELECT ..." --out reports/resultado.csv
```

- Solo `SELECT`, `WITH` y `EXPLAIN`.
- Límite por defecto: 200 filas.
- Timeout: 15 segundos.
- La conexión es de solo lectura.

## `mutate`

Por defecto hace rollback. Solo aplica con `--confirm`.

```bash
# simulación
python scripts/tabular.py mutate --sql "UPDATE ventas SET total=30000 WHERE id=1"

# aplicar
python scripts/tabular.py mutate --sql "UPDATE ventas SET total=30000 WHERE id=1" --confirm
```

Flags:

| Flag | Uso |
|---|---|
| `--confirm` | Aplica el cambio |
| `--confirm-delete` | Obligatorio para `DELETE` |
| `--allow-irreversible` | Sin undo ni backup |
| `--allow-destructive` | `DROP`/`RENAME`/`DROP COLUMN` |
| `--allow-full-table` | `UPDATE`/`DELETE` sin `WHERE` |
| `--backup` | Fuerza backup completo |
| `--capture-sql` | SELECT previo para undo de un `UPDATE` |
| `--keep-history N` | Undo files a conservar (100 por defecto) |
| `--keep-backups N` | Backups a conservar (10 por defecto) |

### Confirmación

1. Ejecuta `mutate` sin `--confirm`.
2. Muestra tipo, filas afectadas, undo/backup y advertencias.
3. Pregunta explícitamente.
4. Para `DELETE`, di cuántas filas y si hay undo.
5. Solo tras un “sí”, aplica con `--confirm` y el flag extra.

En un subagente sin humano, detente en el dry-run; solo aplica si el prompt del
usuario autorizó esa mutación.

### Undo

- `INSERT`: se ejecuta con `RETURNING *`; el undo borra por `id`.
- `DELETE`: se ejecuta con `RETURNING *`; el undo reinserta.
- `UPDATE`: se captura el antes con una consulta; el undo restaura por `id`.

```bash
python scripts/tabular.py mutate --sql "UPDATE ventas SET total=30000 WHERE id=1" \
  --capture-sql "SELECT * FROM ventas WHERE id=1" --confirm
```

El undo queda en `reports/changes/<timestamp>_<tipo>_undo.sql`. Para revertir:

```bash
python scripts/tabular.py mutate --file reports/changes/<timestamp>_<tipo>_undo.sql --confirm
```

Un `DELETE` múltiple genera un `INSERT` multi-fila. El undo de un `UPDATE` de
varias filas puede ser varias sentencias; al aplicarlo puede requerir
`--allow-irreversible` o `--backup`.

### Límites del undo

- SQL complejo (`WITH`, múltiples sentencias, `REPLACE`, `ON CONFLICT`) puede
  no tener undo automático.
- Triggers y cascadas pueden hacer que el undo sea parcial.
- Un `DELETE`/`UPDATE` masivo puede generar undo grande; usa `--backup`.
- Cambios hechos fuera de `mutate` no quedan en el historial.
- `mutate` no conoce reglas de negocio; si cambias cantidad o precio, actualiza
  también el total.

## `history`

```bash
python scripts/tabular.py history
python scripts/tabular.py history --limit 100
python scripts/tabular.py history --show 3
```

`_changes` guarda:

- fecha;
- tipo (`init`, `data`, `data_delete`, `schema`, `schema_destructive`);
- SQL;
- filas afectadas;
- reversible;
- undo/backup;
- snapshots de esquema.

`reports/changes/` guarda el SQL, el undo y el reporte legible.

## `schema`

```bash
python scripts/tabular.py schema
python scripts/tabular.py schema --write
python scripts/tabular.py schema --format sql
```

`schema` introspecciona la base real. `schema --write` genera
`schema/schema.sql` desde SQLite.

`plantilla.yaml` es documentación; `tabular` no la parsea. Si se cambia la
estructura con `mutate`, actualiza la plantilla y ejecuta `schema --write`.

## `verify`

```bash
python scripts/tabular.py verify --integrity
```

Comprueba:

- `PRAGMA integrity_check`;
- FKs huérfanas;
- existencia y filas de `_imports` y `_changes`;
- conteo de tablas.

No reconstruye nada desde CSV. La base es la fuente de verdad.

## Backups

- DDL/destructivo: backup automático.
- `--backup`: backup de cualquier DML.
- `DELETE`/`UPDATE` normal: undo, no backup completo.
- Backups en `db/backups/`; últimos 10 por defecto.
- Undo en `reports/changes/`; últimos 100 por defecto.

## Funciones persistentes

Las funciones de `functions/` son de solo lectura y usan `META`. No se ejecutan
desde `tabular.py`; se llaman con `python` o `uv run`.
