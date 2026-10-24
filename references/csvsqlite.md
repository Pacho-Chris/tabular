# csvsqlite: herramienta temporal

`csvsqlite.py` analiza un CSV, genera una plantilla y construye la base SQLite.
Se usa una vez por proyecto y luego se elimina del proyecto.

## Comandos

```bash
python scripts/csvsqlite.py check "datos.csv" -o reports/check_datos.md
python scripts/csvsqlite.py init "datos.csv" --tabla datos -o plantilla.yaml
python scripts/csvsqlite.py build "datos.csv" -s plantilla.yaml -o db/datos.sqlite
```

La detección de delimitador y encoding es automática. También se puede forzar:

```bash
python scripts/csvsqlite.py check "datos.csv" -d ';' -e utf-8-sig
```

## `check`

```bash
python scripts/csvsqlite.py check "datos.csv" -o reports/check_datos.md
```

Detecta:

- encoding y delimitador;
- fila de encabezado;
- columnas mixtas;
- fechas, horas y fechas ambiguas;
- notación científica;
- moneda, porcentaje, códigos y ceros iniciales;
- nulos;
- filas descartadas por distinto número de columnas;
- cabeceras vacías o duplicadas.

Opciones útiles:

```bash
--header-row 3
--footer-rows 1
--nulos "#n/d,#n/a,sin dato"
--forzar-texto "Nro Factura"
--forzar-texto "COMENTARIOS FC"
```

`--forzar-texto` es importante cuando una columna de texto contiene números y
el detector la marca como mixta. No oculta datos: solo le dice al análisis que
la columna es texto.

`check` no modifica el CSV. Solo genera el reporte.

## `init`

```bash
python scripts/csvsqlite.py init "datos.csv" --tabla datos -o plantilla.yaml
```

Genera una plantilla con:

- nombre de la tabla;
- delimitador, encoding, header/footer y archivo de origen;
- una entrada por columna:
  - `nombre`: cabecera original;
  - `nombre_db`: nombre en SQLite;
  - `tipo`: `TEXT`, `INTEGER`, `REAL`, `DECIMAL`, `MONEY`, `BOOLEAN`,
    `DATE`, `DATETIME`, `TIME`;
  - `nullable`;
  - `clave_primaria`, `unico`, `indice`;
  - `formato_fuente` para fechas;
  - `mapeo` para booleanos;
  - `escala: centavos` para `MONEY`.

Por defecto no separa tablas ni crea `enum`: la base parte plana y genérica.
La IA y el humano deciden qué separar.

## Separar una tabla

Para extraer valores repetidos a una tabla relacionada:

```yaml
- nombre: EMPRESA
  nombre_db: empresa
  tipo: TEXT
  nullable: false
  separar_en: empresas
  columna_tabla_separada: nombre
```

`build` crea:

```sql
empresas(id INTEGER PRIMARY KEY, nombre TEXT NOT NULL UNIQUE)
```

y en la tabla principal:

```sql
empresa_id INTEGER NOT NULL REFERENCES empresas(id)
```

Es opcional. Si no se usa `separar_en`, la columna queda en la tabla principal.

## Dinero en centavos

Para una columna que el humano confirmó como dinero:

```yaml
- nombre: Total
  nombre_db: total_centavos
  tipo: MONEY
  nullable: false
  escala: centavos
```

`build` convierte con `Decimal`:

- `286.96` → `28696`
- `-25.00` → `-2500`
- `5.82E-11` → `0`

Solo se convierte si el humano confirmó que la columna es dinero. Si no, se
deja como `DECIMAL` o `TEXT` según corresponda.

## Claves e índices

- `clave_primaria: true`: clave natural única. Se implementa como `UNIQUE`;
  el `id` interno sigue siendo la PK.
- `unico: true`: restricción `UNIQUE`.
- `indice: true`: índice para búsquedas.
- No uses las medidas como clave natural.

## `build`

```bash
python scripts/csvsqlite.py build "datos.csv" \
  -s plantilla.yaml \
  -o db/datos.sqlite \
  --raiz . \
  --nulos "#n/d,#n/a"
```

Crea:

- tabla principal con `id INTEGER PRIMARY KEY` y `_source_row`;
- tablas relacionadas si hay `separar_en`;
- `_imports` con CSV, archivo, SHA-256, plantilla, conteos y versión;
- `_changes` con la operación `init`;
- `schema/schema.sql`;
- CSV archivado en `data/imports/AAAA-MM-DD_nombre.csv` como solo lectura;
- `PRAGMA foreign_keys=ON`.

Opciones:

```bash
--archivar-dir data/imports
--no-archivar
--schema-dir schema
--force
```

Si hay errores de validación, no escribe la base; genera un reporte temporal
con fila, columna, valor y motivo.

## Después del build

```bash
rm scripts/csvsqlite.py
```

Se conservan:

- `plantilla.yaml` como documentación;
- `data/imports/...csv`;
- `db/datos.sqlite`;
- `schema/schema.sql`;
- `scripts/tabular.py`.

La base es la fuente de verdad. El CSV no se vuelve a importar.

## Límites

- Una tabla principal por CSV.
- Una tabla separada por columna; no agrupa varias columnas en una misma
  tabla relacionada.
- No hace migraciones ni CRUD; eso es `tabular.py`.
- No edita el CSV; solo valida y construye.
- El reporte de `check` es una guía: un bloqueante puede ser un falso positivo
  de una columna de texto con números.
