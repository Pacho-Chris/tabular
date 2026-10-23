# Plantillas

## `reports/ANALISIS.md`

```markdown
# Proyecto <nombre>

## Resumen
Qué contiene el CSV inicial, cuántas filas se importaron y qué se construyó.

## Tablas creadas
| Tabla | Tipo | Filas | Propósito |
|---|---|---|---|
| categorias | dimensión | <filas> | Evita repetir el nombre de la categoría |
| productos | dimensión | <filas> | Catálogo único por SKU |
| ventas | hechos | <filas> | Una fila por línea de venta |

## Organización
Rutas de `data/imports`, `db`, `schema`, `sql`, `functions`, `reports` y
`db/backups`.

## Decisiones y supuestos
Encoding, delimitador, fechas, moneda, filas de totales excluidas,
deduplicación aplicada y cualquier decisión que cambie el resultado.

## Calidad de datos
Duplicados, nulos, filas rechazadas, totales de control y problemas detectados.

## Consultas de ejemplo
Preguntas que el usuario puede hacer y el SQL o comando equivalente.

## Funciones disponibles
Nombre, archivo, propósito, parámetros y ejemplo de uso.

## Historial y reconstrucción
- Base actual: `db/<nombre>.sqlite`
- Esquema: `schema/schema.sql`
- Historial: `_changes` y `reports/changes/`
- Backup: `db/backups/`
```

## Función persistente con `META`

Archivo `functions/func_producto_mas_vendido.py`:

```python
#!/usr/bin/env python3
"""Producto más vendido por unidades."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "ventas_enero.sqlite"

META = {
    "name": "producto_mas_vendido",
    "description": "Devuelve los productos con más unidades vendidas.",
    "params": [
        {
            "name": "limit",
            "type": "integer",
            "required": False,
            "default": 5,
        }
    ],
}


def producto_mas_vendido(limit: int = 5) -> list[dict]:
    if limit < 1:
        raise ValueError("limit debe ser mayor que cero")
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

Reglas:

- Conexión **solo lectura** (`mode=ro`).
- `META` describe nombre, propósito y parámetros para una interfaz futura.
- Los tipos de `params` son `text`, `integer`, `real`, `date` o `boolean`.
- Toda fecha de corte se recibe como argumento; nunca `date('now')`.
- Valida los parámetros antes de consultar.
- Registra la función en `FUNCTIONS.md`.
- Si la consulta crece, muévela a `sql/views/` y deja la función como wrapper.

Fila de `FUNCTIONS.md`:

```markdown
| Función | Archivo | Propósito | Parámetros | Ejemplo |
|---|---|---|---|---|
| producto_mas_vendido | functions/func_producto_mas_vendido.py | Ranking por unidades | limit (integer, opcional) | `uv run python functions/func_producto_mas_vendido.py` |
```

## Vista SQL

Archivo `sql/views/vw_ventas_detalle.sql`:

```sql
CREATE VIEW IF NOT EXISTS vw_ventas_detalle AS
SELECT v.id,
       v.fecha,
       p.sku,
       p.nombre AS producto,
       c.nombre AS categoria,
       v.cantidad,
       v.total_centavos
FROM ventas AS v
JOIN productos AS p ON p.id = v.producto_id
JOIN categorias AS c ON c.id = p.categoria_id;
```

Después de crear o cambiar una vista:

```bash
uv run python scripts/tabular.py schema --write
```

La vista es solo lectura; no cambia datos.
