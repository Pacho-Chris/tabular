# CRUD, DDL, undo e historial

`mutate` es el único punto de escritura de la base. `query` es solo lectura.

## Flujo obligatorio

1. `mutate` sin `--confirm`: rollback, no cambia nada.
2. El agente muestra tipo, filas afectadas, si hay undo/backup y advertencias.
3. El agente pregunta explícitamente.
4. Solo tras un “sí”, aplica con `--confirm` y los flags necesarios.

Nunca ejecutes `--confirm` sin confirmación del usuario. En un subagente sin
humano, detente después del dry-run; solo aplica si el prompt del usuario
autorizó explícitamente esa mutación.

## Operaciones normales

```bash
# simulación
uv run python scripts/tabular.py mutate --sql "UPDATE ventas SET cantidad=3 WHERE id=42"

# aplicar
uv run python scripts/tabular.py mutate --sql "UPDATE ventas SET cantidad=3 WHERE id=42" --confirm
```

```bash
# insertar
uv run python scripts/tabular.py mutate \
  --sql "INSERT INTO ventas (fecha, producto_id, cantidad, precio_centavos, total_centavos) VALUES ('2026-02-01', 1, 2, 150050, 300100)" \
  --confirm
```

```bash
# borrar
uv run python scripts/tabular.py mutate \
  --sql "DELETE FROM ventas WHERE id=42" \
  --confirm --confirm-delete
```

## Flags

| Flag | Uso |
|---|---|
| `--confirm` | Aplica la mutación |
| `--confirm-delete` | Obligatorio para `DELETE` |
| `--allow-irreversible` | Permite aplicar sin undo ni backup |
| `--allow-destructive` | Permite `DROP`/`RENAME`/`DROP COLUMN` |
| `--allow-full-table` | Permite `UPDATE`/`DELETE` sin `WHERE` |
| `--backup` | Fuerza backup completo antes de aplicar |
| `--capture-sql` | SELECT previo para capturar el antes de un `UPDATE` |
| `--keep-history N` | Undo files a conservar (100 por defecto) |
| `--keep-backups N` | Backups completos a conservar (10 por defecto) |

## Undo automático

- `INSERT`: se ejecuta con `RETURNING *`; el undo borra por `id`.
- `DELETE`: se ejecuta con `RETURNING *`; el undo reinserta las filas.
- `UPDATE`: se captura el antes con una consulta; el undo restaura los valores
  anteriores por `id`.

Ejemplo de `UPDATE` con captura explícita:

```bash
uv run python scripts/tabular.py mutate \
  --sql "UPDATE ventas SET cantidad=3 WHERE id=42" \
  --capture-sql "SELECT * FROM ventas WHERE id=42" \
  --confirm
```

El undo queda en `reports/changes/<timestamp>_<tipo>_undo.sql`.

Para revertir:

```bash
uv run python scripts/tabular.py mutate --file reports/changes/<timestamp>_<tipo>_undo.sql --confirm
```

La reversión se registra como una nueva operación. Esa nueva operación también
genera su propio undo, así que puedes crear una cadena de reversiones.

Un `DELETE` de varias filas genera un único `INSERT` multi-fila. El undo de un
`UPDATE` que afectó muchas filas puede ser varias sentencias; al aplicarlo,
`mutate` puede pedir `--allow-irreversible` o `--backup`.

`mutate` no conoce reglas de negocio. Si cambias `cantidad` o `precio`, actualiza
también `total_centavos` en la misma operación o mediante una vista/trigger.

Si no se puede generar undo:

- `mutate` lo marca como **no reversible**;
- para aplicar exige `--backup` o `--allow-irreversible`;
- el agente debe decirlo claramente antes de pedir confirmación.

## DDL

```bash
# agregar columna
uv run python scripts/tabular.py mutate \
  --sql "ALTER TABLE ventas ADD COLUMN descuento_centavos INTEGER" \
  --confirm
```

```bash
# cambio destructivo
uv run python scripts/tabular.py mutate \
  --sql "ALTER TABLE ventas DROP COLUMN descuento_centavos" \
  --confirm --allow-destructive
```

Los DDL:

- hacen backup completo;
- guardan `schema/history/schema_<timestamp>_before.sql` y `_after.sql`;
- regeneran `schema/schema.sql`;
- avisan que `mapping.json` puede estar desactualizado.

Después de un DDL:

```bash
uv run python scripts/tabular.py schema --check
uv run python scripts/tabular.py schema --write
```

`mapping.json` es el modelo semántico; `schema.sql` es la estructura real. Si
cambias la estructura, actualiza el mapping para que el próximo agente entienda
el proyecto.

## Historial

```bash
uv run python scripts/tabular.py history
uv run python scripts/tabular.py history --limit 100
uv run python scripts/tabular.py history --show 7
```

`_changes` guarda:

- fecha;
- tipo (`init`, `data`, `data_delete`, `schema`, `schema_destructive`);
- SQL;
- filas afectadas;
- reversible;
- undo/backup;
- snapshots de esquema.

`reports/changes/` guarda el SQL original, el undo y el reporte legible.

## Límites

- SQL complejo (`WITH`, múltiples sentencias, `REPLACE`, `ON CONFLICT`) puede
  no tener undo automático. Usa `--backup`.
- Un `DELETE`/`UPDATE` masivo puede generar un undo grande; considera
  `--backup`.
- Cambios fuera de `mutate` (DB Browser, `sqlite3`) no quedan en el historial.
- El undo es best effort: triggers, cascadas o cambios posteriores pueden
  impedir una restauración exacta. El backup completo es la garantía.
