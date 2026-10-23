# Guía de datos CSV

Contenido:

1. [Ciclo de vida](#ciclo-de-vida)
2. [Trampas de CSV](#trampas-de-csv)
3. [Reglas de tipos](#reglas-de-tipos)
4. [Especificación de mapping.json](#especificación-de-mappingjson)
5. [Mapping y esquema real](#mapping-y-esquema-real)
6. [Criterios de normalización](#criterios-de-normalización)
7. [Consultas y vistas](#consultas-y-vistas)
8. [Errores frecuentes](#errores-frecuentes)

---

## Ciclo de vida

```text
CSV → profile → mapping → init → base SQLite
                                  ├── query
                                  ├── mutate
                                  ├── schema
                                  ├── verify --integrity
                                  └── history
```

- `init` se ejecuta una sola vez. Verifica el CSV, lo archiva y crea la base.
- Después de `init`, **la base es la fuente de verdad**.
- `mutate` es el único punto de escritura. Por defecto hace rollback; con
  `--confirm` aplica.
- `query` es solo lectura.
- `schema` muestra la base real; `schema --write` regenera `schema.sql`.
- `verify --integrity` revisa FKs, integridad y metadatos.
- `history` muestra `_changes`, undo y backups.

El CSV no se vuelve a importar para actualizar el proyecto. Los cambios se
hacen con `mutate`. Si en el futuro se necesita una actualización masiva desde
otro CSV, es un flujo nuevo, no parte de esta versión.

---

## Trampas de CSV

### Encoding

El CSV puede venir en:

- `utf-8-sig` (UTF-8 con BOM): el BOM aparece como `\ufeff` en el primer
  encabezado si se lee mal.
- `utf-8`.
- `cp1252` / Windows-1252: común en archivos exportados desde Excel en español.
- `latin-1`.

`profile` detecta el encoding y lo registra. Si el usuario puede elegir,
recomienda **CSV UTF-8**. Si el archivo tiene caracteres raros, no lo edites:
vuelve a perfilar con `--encoding` explícito.

### Delimitador

No siempre es coma:

- `,` en CSV estándar.
- `;` en exportaciones de Excel en español.
- tabulación en algunos sistemas.
- `|` en exportes de bases de datos.

`profile` detecta el delimitador. Si hay comillas con comas dentro, el
detector debe respetarlas; no cuentes comas a mano.

### Campos entre comillas

Un campo puede contener el delimitador o saltos de línea:

```csv
id,descripcion
1,"Laptop, 15 pulgadas"
2,"Texto con
salto de línea"
```

No uses `split(",")` ni `line.split(";")`. `tabular.py` usa el lector CSV de
Python, que respeta las comillas.

### Filas con distinto número de columnas

Puede haber:

- filas con columnas de más al final (a veces solo separadores);
- filas con columnas de menos;
- filas de totales con otro formato.

`profile` cuenta las filas irregulares. Si una fila tiene datos de más, se
rechaza con el número de fila. Si solo tiene separadores vacíos al final, se
recortan.

### Encabezados

- Pueden tener espacios: `" SKU "` → se recorta a `SKU`.
- Pueden estar vacíos.
- Pueden repetirse: dos columnas llamadas `Total`.
- Pueden estar precedidos por filas de título.

Si hay encabezados duplicados o vacíos, usa `source_index` (1-based) en el
mapping. No renombres el CSV original.

### Filas de totales

Es común que el CSV termine con:

```csv
TOTAL,,,,,12345.67
```

No son datos. `profile` las detecta como candidatas y sugiere
`footer_rows`. El mapping debe declarar `footer_rows` para excluirlas. Si una
fila de totales se cuela como dato, los totales de control no cuadrarán.

### Números

Formatos posibles:

- `1234.56`
- `1,234.56`
- `1.234,56`
- `$1,234.56`
- `1.234,56 €`
- `(1,234.56)` para negativo
- `12,5%`

Declara en `locale`:

```json
{"decimal": ",", "thousands": "."}
```

`MONEY` se guarda en centavos. `INTEGER` no admite decimales. `REAL` se usa
solo cuando la precisión decimal no es crítica.

### Fechas

Formatos posibles:

- `2026-01-31`
- `31/01/2026`
- `01/31/2026`
- `31-01-2026`
- `31.01.2026`
- `2026-01-31 14:30:00`

Declara el formato en `locale.date`:

```json
{"date": "dd/mm/yyyy"}
```

Si todas las fechas tienen día y mes ≤ 12, el formato es ambiguo. Pregunta al
humano; no adivines. `profile` marca estas fechas.

Si una columna trae números de 5 dígitos entre 20000 y 80000, puede ser una
fecha serial de Excel exportada como número. `profile` lo advierte. Confirma
con el humano antes de declararla `DATE`.

### IDs y códigos

Un SKU `00123` no es el número 123. Si lo conviertes a entero, pierdes los
ceros y rompes las relaciones. Declara `TEXT` para:

- SKUs;
- códigos postales;
- números de factura;
- identificadores con ceros a la izquierda;
- teléfonos.

### Nulos disfrazados

Además del vacío, aparecen:

- `N/A`, `NA`, `n/a`
- `NULL`, `null`
- `-`, `--`
- `sin dato`, `s/d`

`profile` los cuenta. El mapping puede declarar `null_values` para cambiar la
lista. No conviertas `0` en nulo: cero suele ser un valor válido.

### Duplicados

Hay dos tipos:

1. **Fila exactamente duplicada**: todos los campos iguales.
2. **Clave de negocio duplicada**: mismo SKU/fecha/factura, pero otros valores.

No elimines duplicados sin saber cuál es la clave. Si una dimensión tiene el
mismo SKU con dos nombres distintos, es un conflicto de datos: detente y
pregunta.

### Inyección de fórmulas

Un CSV puede contener celdas que empiezan con `=`, `+`, `-` o `@`. Si luego se
abren en Excel, pueden interpretarse como fórmulas. Al exportar resultados,
prefija esos valores con un apóstrofo o evita exportar texto no confiable como
fórmula. Esta versión no exporta XLSX; si el usuario lo pide, es una tarea
aparte.

---

## Reglas de tipos

| Tipo en mapping | Tipo SQLite | Conversión | Ejemplo |
|---|---|---|---|
| `TEXT` | `TEXT` | Recorta espacios; conserva ceros | `00123` |
| `INTEGER` | `INTEGER` | Entero, sin decimales | `12` |
| `REAL` | `REAL` | Número decimal | `12.5` |
| `MONEY` | `INTEGER` | Centavos, redondeo half-up | `$1,234.56` → `123456` |
| `DATE` | `TEXT` | ISO `YYYY-MM-DD` | `31/01/2026` → `2026-01-31` |
| `BOOLEAN` | `INTEGER` | `0` o `1` | `sí` → `1` |
| `FK` | `INTEGER` | Resuelve la clave natural a `id` | `SKU` → `productos.id` |

Reglas adicionales:

- `MONEY` siempre se guarda en centavos, aunque el nombre de la columna no
  termine en `_centavos`.
- `DATE` no guarda hora. Si el CSV trae hora, se descarta al guardar la fecha;
  si la hora es importante, crea una columna `TEXT` con el timestamp completo.
- `BOOLEAN` acepta `1/0`, `si/no`, `sí/no`, `true/false`, `verdadero/falso`.
- `required: true` hace que un valor vacío o inválido rechace la fila.
- Las columnas de una `key` siempre son obligatorias.
- Los `id` y `_source_row` son reservados.

---

## Especificación de mapping.json

### Estructura general

```json
{
  "source": {
    "path": "data/raw/ventas.csv",
    "encoding": "utf-8",
    "delimiter": ";",
    "header_row": 1,
    "footer_rows": 0
  },
  "locale": {
    "decimal": ",",
    "thousands": ".",
    "date": "dd/mm/yyyy"
  },
  "null_values": ["", "n/a", "-", "sin dato"],
  "tables": []
}
```

### `source`

| Campo | Obligatorio | Descripción |
|---|---|---|
| `path` | Sí | CSV inicial, relativo al proyecto o absoluto. `init --csv` puede reemplazarlo |
| `encoding` | No | Si falta, se detecta |
| `delimiter` | No | Si falta, se detecta |
| `header_row` | No | Fila lógica del encabezado, 1-based. Default: 1 |
| `footer_rows` | No | Filas finales a excluir. Default: 0 |

`source.path` se usa solo en `init`. Después de importar, el CSV archivado queda
en `data/imports/` y la base es la fuente de verdad.

### `locale`

| Campo | Default | Descripción |
|---|---|---|
| `decimal` | `"."` | Separador decimal |
| `thousands` | `","` | Separador de miles |
| `date` | `"yyyy-mm-dd"` | Formato de fecha, acepta `dd/mm/yyyy` o `%d/%m/%Y` |
| `true_values` | ver guía | Valores que significan verdadero |
| `false_values` | ver guía | Valores que significan falso |

### `tables`

Cada tabla tiene:

```json
{
  "name": "ventas",
  "kind": "fact",
  "key": [],
  "columns": [
    {"name": "fecha", "source": "Fecha", "type": "DATE", "required": true}
  ]
}
```

| Campo | Obligatorio | Descripción |
|---|---|---|
| `name` | Sí | snake_case ASCII, único |
| `kind` | Sí | `dimension` o `fact` |
| `key` | Dimensión: sí. Hechos: no | Clave natural/única de la tabla |
| `columns` | Sí | Al menos una columna |

`key` en una tabla de hechos es opcional y solo debe declararse si existe una
combinación realmente única (por ejemplo `ticket_id + linea`). No uses las
medidas (`cantidad`, `total`) como clave: dos ventas legítimas podrían coincidir
y una de ellas se rechazaría. Si la declaras, se agrega `UNIQUE` y los
duplicados exactos se omiten; los conflictos se rechazan.

### Columnas

```json
{
  "name": "total_centavos",
  "source": "Total",
  "type": "MONEY",
  "required": true
}
```

| Campo | Obligatorio | Descripción |
|---|---|---|
| `name` | Sí | Nombre destino, snake_case, único |
| `source` | Sí, salvo `source_index` | Encabezado exacto (sin espacios externos) |
| `source_index` | Sí, salvo `source` | Posición 1-based; útil con encabezados duplicados |
| `type` | Sí | `TEXT`, `INTEGER`, `REAL`, `MONEY`, `DATE`, `BOOLEAN`, `FK` |
| `required` | No | Si falta y no es clave, default `false`; las FK default `true` |
| `references` | Solo FK | `"tabla.columna"` de una dimensión |

### FKs

Una FK apunta a la **clave natural** de una dimensión, no a su `id`:

```json
{"name": "producto_id", "source": "SKU", "type": "FK",
 "references": "productos.sku"}
```

El importador busca el `id` de `productos` cuyo `sku` coincide. Si no existe,
rechaza la fila. La dimensión referenciada debe tener una clave de una sola
columna; si es compuesta, no se puede usar como destino de FK en esta versión.

### Orden de tablas

No importa el orden en el JSON. El importador procesa dimensiones primero,
resolviendo dependencias, y luego hechos. No se permiten ciclos ni FKs entre
hechos.

### Ejemplo completo

```json
{
  "source": {
    "path": "data/raw/ventas_enero.csv",
    "header_row": 3,
    "footer_rows": 2
  },
  "locale": {"decimal": ",", "thousands": ".", "date": "dd/mm/yyyy"},
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
        {"name": "precio_centavos", "source": "Precio", "type": "MONEY", "required": true},
        {"name": "total_centavos", "source": "Total", "type": "MONEY", "required": true}
      ]
    }
  ]
}
```

---

## Mapping y esquema real

`mapping.json` es el modelo semántico que se usa en `init`: grano, tablas,
columnas, tipos y FKs.

`schema/schema.sql` es la estructura real de la base. Al principio coinciden,
pero después de un DDL con `mutate` pueden diferir.

Después de cambiar la estructura:

```bash
uv run python scripts/tabular.py schema --check
uv run python scripts/tabular.py schema --write
uv run python scripts/tabular.py verify --integrity
```

- `schema --check` compara mapping contra la base y muestra tablas/columnas
  faltantes, extra o con tipo distinto.
- `schema --write` regenera `schema.sql` desde la base.
- Si `--check` falla, actualiza `mapping.json` o documenta la diferencia.
- No asumas que el mapping sigue siendo la verdad después de un DDL.

## Criterios de normalización

### Primero define el grano

Pregunta: **¿qué representa una fila?**

- una línea de venta;
- una venta completa;
- un producto;
- un resumen mensual.

El grano determina la tabla de hechos. No lo cambies a mitad del diseño.

### Cuándo crear una dimensión

Crea una tabla aparte cuando:

- un atributo se repite muchas veces (`Categoría` en 50,000 filas);
- ese atributo tiene columnas propias (nombre, descripción, impuesto);
- se usa como clave de relación;
- corregir un valor debería hacerse en un solo lugar.

Ejemplo:

```text
categorias(id, nombre)
productos(id, sku, nombre, categoria_id)
ventas(id, fecha, producto_id, cantidad, total_centavos)
```

### Cuándo no normalizar

No crees una tabla para:

- una columna que solo tiene un valor;
- una etiqueta sin atributos ni relaciones;
- una fecha desglosada en año/mes/día (SQLite puede calcularla);
- valores que nunca se gestionan por separado.

Sobre-normalizar complica las consultas sin mejorar la calidad.

### Claves

- Cada dimensión necesita una clave natural única (`sku`, `nombre`).
- Se agrega un `id INTEGER PRIMARY KEY` interno.
- La clave natural lleva `UNIQUE` y `NOT NULL`.
- Los hechos referencian el `id` de la dimensión.
- En CRUD puedes usar el `id` o la clave natural, pero el `id` es el
  identificador estable de la fila.
- No uses el nombre visible como FK si puede cambiar; usa un código estable.

### Duplicados

- La deduplicación ocurre en `init`.
- Dimensiones: el mismo key con los mismos valores se ignora; con valores
  distintos es un conflicto y se rechaza.
- Hechos: si declaras `key`, los duplicados exactos se omiten; los conflictos
  se rechazan. Si no declaras `key`, se cargan todas las filas.
- Después de `init`, los cambios se hacen con `mutate`; no hay una segunda
  importación que vuelva a deduplicar.
- Todo rechazo de `init` queda en `reports/rechazos.csv`.

### Dinero

- Guarda centavos como `INTEGER`.
- No compares `REAL` con `=`.
- Formatea al presentar, no al almacenar.
- Si el CSV trae impuestos o descuentos, decide si el total ya los incluye y
  documéntalo.

### Fechas

- Guarda ISO `YYYY-MM-DD`.
- No guardes fechas como `01/02/2026`; impide ordenar y comparar.
- Si la fecha es ambigua, pregunta antes de cargar.
- Para análisis mensual, usa `substr(fecha, 1, 7)` o `strftime('%Y-%m', fecha)`.

---

## Consultas y vistas

### Producto más vendido

```sql
SELECT p.nombre AS producto, SUM(v.cantidad) AS unidades
FROM ventas AS v
JOIN productos AS p ON p.id = v.producto_id
GROUP BY p.id, p.nombre
ORDER BY unidades DESC, producto ASC
LIMIT 10;
```

### Ventas por mes

```sql
SELECT substr(v.fecha, 1, 7) AS mes,
       SUM(v.total_centavos) / 100.0 AS total
FROM ventas AS v
GROUP BY mes
ORDER BY mes;
```

### Ventas por categoría

```sql
SELECT c.nombre AS categoria,
       SUM(v.total_centavos) / 100.0 AS total
FROM ventas AS v
JOIN productos AS p ON p.id = v.producto_id
JOIN categorias AS c ON c.id = p.categoria_id
GROUP BY c.id, c.nombre
ORDER BY total DESC;
```

### Productos sin ventas

```sql
SELECT p.sku, p.nombre
FROM productos AS p
LEFT JOIN ventas AS v ON v.producto_id = p.id
WHERE v.id IS NULL;
```

### Vista reutilizable

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

`init` aplica todas las vistas de `sql/views/*.sql` después de importar. Si una
vista falla, la importación se aborta.

Después de `init`, la base es la fuente de verdad. Para crear o cambiar una
vista usa `mutate` (por ejemplo `DROP VIEW` + `CREATE VIEW`), y luego:

```bash
uv run python scripts/tabular.py schema --write
```

### Consultas guardadas

Guarda SQL reutilizable en `sql/queries/<nombre>.sql` y ejecútalo con:

```bash
uv run python scripts/tabular.py query --file sql/queries/ventas_mes.sql
```

### Buenos hábitos

- Usa `GROUP BY` con la clave y el nombre para evitar duplicados.
- No uses `SELECT *` en consultas de reporte.
- Redondea al presentar: `ROUND(total_centavos / 100.0, 2)`.
- Para rankings, agrega un desempate (`ORDER BY total DESC, nombre ASC`).
- Si una consulta devuelve muchas filas, agrega `LIMIT` o pide al usuario un
  filtro.

---

## Windows

La instalación, el scaffolding PowerShell y las notas de archivos bloqueados,
OneDrive, rutas largas y WSL están en `setup.md`.

## Límites prácticos

Esta versión lee el CSV completo en memoria para perfilar e importar. Para
archivos de más de ~1 GB o decenas de millones de filas, avisa al usuario y
divide el archivo antes de usar la skill.

Después de `init`, el crecimiento normal es por `mutate`; los backups completos
solo se hacen para DDL, operaciones destructivas o cuando se pide `--backup`.

## Errores frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| `Solo se aceptan archivos .csv` | Se subió XLSX | Pedir CSV UTF-8 |
| Primer encabezado con `\ufeff` | BOM | Usar encoding `utf-8-sig` |
| Todo cae en una columna | Delimitador incorrecto | Perfilar o declarar `;` |
| Precios enormes | Coma decimal interpretada como miles | Ajustar `locale.decimal` |
| SKUs sin ceros | Se declararon `INTEGER` | Declarar `TEXT` |
| Fechas invertidas | Formato `mm/dd` vs `dd/mm` | Preguntar y ajustar `locale.date` |
| FKs huérfanas | Dimensión no cargada o clave con espacios | Revisar claves y `rechazos.csv` |
| Totales inflados | Fila `TOTAL` incluida | Declarar `footer_rows` |
| `UNIQUE constraint failed` | Clave duplicada en `init` | Revisar duplicados y decidir |
| `Ya existe una base` | Se intentó `init` dos veces | Usar `mutate` o `init --force` |
| `schema --check` falla | DDL sin actualizar mapping | Actualizar `mapping.json` o `schema.sql` |
| `mutate` pide `--confirm-delete` | `DELETE` sin confirmación extra | Mostrar filas y confirmar |
| `mutate` pide `--allow-irreversible` | Sin undo ni backup | Hacer `--backup` o cancelar |
| `verify --integrity` falla | FK, metadata o corrupción | No entregar; revisar y restaurar backup |
