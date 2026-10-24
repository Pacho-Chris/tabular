# Guía de datos CSV

## Ciclo de vida

```text
CSV
  → csvsqlite check    (reporte temporal)
  → csvsqlite init     (plantilla.yaml)
  → csvsqlite build    (db + _imports + _changes + schema.sql + CSV archivado)
  → eliminar csvsqlite.py del proyecto
  → tabular query/mutate/history/schema/verify
```

- El CSV es el punto de partida, no la fuente de verdad permanente.
- Después de `build`, SQLite es la base.
- `plantilla.yaml` documenta el esquema; `schema.sql` es la estructura real.
- `tabular` no lee CSV ni parsea la plantilla.

## Encoding

Puede venir en:

- `utf-8-sig` (BOM);
- `utf-8`;
- `cp1252` / Windows-1252;
- `latin-1`;
- `utf-16`.

`csvsqlite check` detecta encoding automáticamente. Si hay caracteres raros,
fuerza `-e` con el valor correcto.

## Delimitador

No siempre es coma:

- `,` estándar;
- `;` en exportaciones de Excel en español;
- tabulación;
- `|`.

`check` detecta el delimitador. Si el archivo tiene comillas con comas dentro,
el lector CSV las respeta.

## Encabezados y pies

`csvsqlite` asume la primera fila como encabezado, salvo que uses:

```bash
--header-row 3
--footer-rows 1
```

- Encabezados con espacios: se recortan.
- Encabezados vacíos o duplicados: bloquean el análisis.
- Filas de totales al final: usa `--footer-rows`.

## Campos entre comillas

Un campo puede contener el delimitador o saltos de línea:

```csv
id,descripcion
1,"Laptop, 15 pulgadas"
2,"Texto con
salto de línea"
```

No uses `split(",")` ni `split(";")`; el lector CSV de Python respeta comillas.

## Filas irregulares

- Filas con más columnas: se descartan y bloquean.
- Filas con menos columnas: se descartan y bloquean.
- Filas vacías al final: se ignoran.
- Filas de totales: se excluyen con `--footer-rows`.

## Nulos

`csvsqlite` reconoce por defecto:

```text
null, none, n/a, #n/d, #n/a, s/n, vacío
```

Puedes agregar más:

```bash
--nulos "#n/d,#n/a,sin dato,-"
```

No conviertas `0` en nulo: cero suele ser un valor válido.

## Números y notación científica

Formatos posibles:

- `1234.56`
- `1,234.56`
- `1.234,56`
- `$1,234.56`
- `(1,234.56)` para negativo
- `5.82E-11` (residuo de Excel)

`check` detecta notación científica y `build` la convierte sin corromper el
valor. En dinero, los residuos como `5.82E-11` se redondean a `0` centavos.

## Fechas

Formatos posibles:

- `2026-01-31`
- `31/01/2026`
- `01/31/2026`
- `31-01-2026`
- `31.01.2026`
- `2026-01-31 14:30:00`

Si todas las fechas tienen día y mes ≤ 12, el formato es ambiguo. `check` lo
advierten y pide confirmación. No adivines `dd/mm` vs `mm/dd`.

Las fechas se guardan como texto ISO `YYYY-MM-DD` (o `DATETIME`).

## IDs y códigos

Un SKU `00123` no es el número 123. Si lo conviertes a entero, pierdes los
ceros. Declara `TEXT` para:

- SKUs;
- códigos postales;
- números de factura;
- identificadores con ceros iniciales;
- teléfonos.

## Dinero

El dinero se guarda en **centavos enteros**:

```text
286.96  → 28696
-25.00  → -2500
0.005   → redondeo a centavos
```

La IA debe preguntar antes:

> “`Total` parece dinero. ¿Confirmas que es USD y que lo guardemos en centavos?”

Si el humano acepta, en `plantilla.yaml`:

```yaml
nombre_db: total_centavos
tipo: MONEY
escala: centavos
```

Si no acepta, se deja como `DECIMAL` o `TEXT`.

No uses `REAL` para dinero si necesitas exactitud.

## Columnas mixtas y falsos positivos

`check` marca una columna como mixta si encuentra tipos distintos. A veces es
un falso positivo: una columna de texto como `Nro Factura` o `COMENTARIOS FC`
puede contener números.

Si la columna es realmente texto:

```bash
--forzar-texto "Nro Factura"
--forzar-texto "COMENTARIOS FC"
```

Esto no borra datos: solo le dice al análisis que la columna es `TEXT`.

## Separar tablas

Una columna repetitiva puede extraerse a una tabla relacionada:

```yaml
separar_en: categorias
columna_tabla_separada: nombre
```

La tabla principal guarda `categoria_id` y se crea:

```sql
categorias(id INTEGER PRIMARY KEY, nombre TEXT NOT NULL UNIQUE)
```

Es opcional. Si no se separa, todo queda en la tabla principal.

No separes automáticamente: pregunta si aporta claridad o si el usuario prefiere
una tabla plana.

## Plantilla y esquema

- `plantilla.yaml`: documentación editable antes de `build`.
- `schema/schema.sql`: DDL real generado desde SQLite.
- `tabular schema`: introspección real.
- `tabular schema --write`: regenera `schema.sql`.

Si se cambia la estructura después de `build`, actualiza la plantilla y ejecuta
`schema --write`.

## Errores frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| Solo se aceptan CSV | Se subió XLSX | Pedir CSV UTF-8 |
| Todo cae en una columna | Delimitador incorrecto | Usar `-d ';'` o `auto` |
| Encoding raro | BOM o cp1252 | Usar `-e utf-8-sig` o `auto` |
| Columnas mixtas | Texto con números | `--forzar-texto` |
| Fechas invertidas | `dd/mm` vs `mm/dd` | Preguntar y ajustar |
| Precios enormes | Coma decimal mal leída | Revisar plantilla y `MONEY` |
| SKUs sin ceros | Se declararon `INTEGER` | Declarar `TEXT` |
| Fila TOTAL como dato | No se usó footer | `--footer-rows 1` |
| `build` no escribe DB | Errores de validación | Leer reporte de errores |
| `tabular mutate` falla | No existe `_changes` | La base no fue creada por csvsqlite |
| `schema.sql` viejo | DDL sin actualizar | `tabular schema --write` |
