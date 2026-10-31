# tabular

Skill para analizar y limpiar **cualquier CSV**, crear una base **SQLite** y
dejar un runtime **CRUD** para trabajar con esa base como fuente de verdad. Sin
pandas, sin editar el CSV original.

```text
csvsqlite.py   → andamio temporal: check → init → build
plantilla.yaml → documentación del esquema (se conserva)
proyecto.sqlite → base definitiva (se conserva)
tabular.py     → runtime permanente: query / mutate / history / schema / verify
CSV            → se conserva archivado
```

`csvsqlite.py` se usa una vez y se elimina del proyecto. `tabular.py` se queda.

## Requisitos

- [uv](https://docs.astral.sh/uv/) (gestiona Python y dependencias).
- Python 3.12+ y SQLite.

Solo acepta entrada **CSV** (UTF-8). Si traes `.xlsx`/`.xls`, exporta la hoja a
CSV UTF-8.

## Instalación

Copia (o crea un enlace a) la carpeta `tabular/` dentro de tu directorio de
skills del agente:

```bash
ln -s "$(pwd)/tabular" ~/.agents/skills/tabular
```

La skill queda disponible como `tabular`.

## Flujo

1. **Validar el CSV** (temporal):
   ```bash
   python scripts/csvsqlite.py check "datos.csv" -o reports/check_datos.md
   ```
2. **Preguntar al humano** lo necesario (tabla, dinero/centavos, fechas, separar
   tablas, filas de totales).
3. **Generar la plantilla**:
   ```bash
   python scripts/csvsqlite.py init "datos.csv" --tabla datos -o plantilla.yaml
   ```
4. **Construir la base**:
   ```bash
   python scripts/csvsqlite.py build "datos.csv" -s plantilla.yaml \
     -o db/datos.sqlite --raiz .
   ```
5. **Trabajo diario con `tabular.py`**:
   ```bash
   python scripts/tabular.py query "SELECT ..."
   python scripts/tabular.py schema
   python scripts/tabular.py verify --integrity
   python scripts/tabular.py history
   python scripts/tabular.py mutate --sql "UPDATE datos SET ... WHERE id=1"      # dry-run (rollback)
   python scripts/tabular.py mutate --sql "UPDATE datos SET ... WHERE id=1" --confirm
   ```

## Documentación

- `SKILL.md` — flujo obligatorio para el agente.
- `references/setup.md` — uv, proyecto, Windows, estructura.
- `references/csvsqlite.md` — check/init/build, plantilla y separación de tablas.
- `references/tabular.md` — query/mutate/history/schema/verify.
- `references/data-guide.md` — trampas CSV, tipos, dinero, errores.
- `references/templates.md` — `ANALISIS.md` y funciones con `META`.

## Licencia

MIT. Ver `LICENSE`.
