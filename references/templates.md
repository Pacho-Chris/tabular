# Plantillas

## `reports/ANALISIS.md`

```markdown
# Proyecto <nombre>

## Resumen
Qué contiene el CSV inicial, cuántas filas se importaron, qué tabla principal
se creó y qué columnas se separaron.

## Tablas creadas
| Tabla | Tipo | Filas | Propósito |
|---|---|---|---|
| <tabla_principal> | principal | <filas> | Una fila por registro |
| <tabla_relacionada> | relacionada | <filas> | Evita repetir valores de `<columna>` |

## Origen y organización
- CSV original: `data/raw/<archivo>.csv`
- CSV archivado: `data/imports/<fecha>_<archivo>.csv`
- Base: `db/<nombre>.sqlite`
- Plantilla: `plantilla.yaml`
- Esquema real: `schema/schema.sql`
- Reporte de validación: `reports/check_<archivo>.md`

## Decisiones y supuestos
Encoding, delimitador, header/footer, fechas, dinero y escala de centavos,
separación de tablas, nulos y cualquier decisión que cambie el resultado.

## Calidad de datos
Filas leídas, descartadas, nulos, duplicados, fechas ambiguas, notación
científica y advertencias del `check`.

## Dinero
Columnas convertidas a centavos, moneda, ejemplo (`286.96` → `28696`) y
confirmación del humano.

## Consultas de ejemplo
Preguntas que el usuario puede hacer y el SQL o comando equivalente.

## Funciones disponibles
Nombre, archivo, propósito, parámetros y ejemplo de uso.

## Historial y reconstrucción
- Historial: `_changes` y `reports/changes/`
- Backups: `db/backups/`
- La base es la fuente de verdad; el CSV no se reimporta.
```

## Función persistente con `META`

Archivo `functions/func_ejemplo.py`:

```python
#!/usr/bin/env python3
"""Ejemplo de función de consulta de solo lectura."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "datos.sqlite"

META = {
    "name": "ejemplo",
    "description": "Devuelve una muestra de la tabla principal.",
    "params": [
        {
            "name": "limit",
            "type": "integer",
            "required": False,
            "default": 10,
        }
    ],
}


def ejemplo(limit: int = 10) -> list[dict]:
    if limit < 1:
        raise ValueError("limit debe ser mayor que cero")
    sql = 'SELECT * FROM "<tabla_principal>" LIMIT :limit'
    uri = DB_PATH.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, {"limit": limit})]


if __name__ == "__main__":
    print(json.dumps(ejemplo(), ensure_ascii=False, indent=2))
```

Reglas:

- Conexión **solo lectura** (`mode=ro`).
- `META` describe nombre, propósito y parámetros.
- Tipos de `params`: `text`, `integer`, `real`, `date`, `boolean`.
- Toda fecha de corte se recibe como argumento; nunca `date('now')`.
- Valida los parámetros.
- Registra la función en `FUNCTIONS.md`.
- Si la consulta crece, muévela a una vista SQL y deja la función como wrapper.

Fila de `FUNCTIONS.md`:

```markdown
| Función | Archivo | Propósito | Parámetros | Ejemplo |
|---|---|---|---|---|
| ejemplo | functions/func_ejemplo.py | Muestra de la tabla principal | limit (integer, opcional) | `python functions/func_ejemplo.py` |
```

## Vista SQL

Archivo `sql/views/vw_ejemplo.sql`:

```sql
CREATE VIEW IF NOT EXISTS vw_ejemplo AS
SELECT *
FROM "<tabla_principal>";
```

La vista debe crearse o cambiarse con `tabular mutate` (DDL); después:

```bash
python scripts/tabular.py schema --write
```

Una vista es solo lectura; no cambia datos.
