# Instalación y creación del proyecto

## Requisitos

- `uv` instalado y en el PATH.
- Python 3.12 gestionado por uv. No hace falta instalarlo a mano.
- pandas en el entorno del proyecto (`uv add pandas`).
- SQLite viene incluido en Python. No instales el CLI.
- Git es opcional para el proyecto generado.

## Instalar uv

Linux/macOS:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell:

```powershell
winget install --id=astral-sh.uv -e
```

o:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Cierra y vuelve a abrir la terminal. Comprueba:

```bash
uv --version
```

## Instalar la skill

Copia la carpeta completa en una de estas rutas:

```text
~/.agents/skills/tabular/
~/.config/opencode/skills/tabular/
```

En Windows:

```text
C:\Users\<usuario>\.agents\skills\tabular\
C:\Users\<usuario>\.config\opencode\skills\tabular\
```

Debe quedar:

```text
tabular/
├── SKILL.md
├── references/
│   ├── setup.md
│   ├── data-guide.md
│   ├── mutate.md
│   └── templates.md
└── scripts/
    └── tabular.py
```

No copies solo `SKILL.md`: sin `scripts/tabular.py` la skill no ejecuta nada.
Reinicia OpenCode si no aparece en la lista de skills.

`uv init` puede normalizar el nombre del paquete en `pyproject.toml`
(`ventas_crud` → `ventas-crud`). La carpeta y la base conservan el nombre que
elijas; no afecta a `tabular.py`.

## Crear el proyecto

### Linux, macOS o Git Bash

```bash
SKILL_DIR="$HOME/.agents/skills/tabular"
PROJECT="ventas_enero"
CSV="/ruta/al/ventas_enero.csv"

uv init --bare --python 3.12 --name "$PROJECT" "$PROJECT"
cd "$PROJECT"
uv python pin 3.12
uv add pandas

mkdir -p data/raw data/imports db/backups schema/history \
  sql/views sql/queries functions reports/changes scripts

cp "$SKILL_DIR/scripts/tabular.py" scripts/tabular.py
cp "$CSV" "data/raw/$(basename "$CSV")"

cat > .gitignore <<'EOF'
.venv/
__pycache__/
*.pyc
db/*.sqlite
db/*.sqlite-*
db/backups/
EOF
```

### Windows PowerShell

```powershell
$SkillDir = Join-Path $env:USERPROFILE ".agents\skills\tabular"
$Project  = "ventas_enero"
$Csv      = "C:\datos\ventas_enero.csv"

uv init --bare --python 3.12 --name $Project $Project
Set-Location $Project
uv python pin 3.12
uv add pandas

$dirs = @(
  "data\raw", "data\imports", "db\backups", "schema\history",
  "sql\views", "sql\queries", "functions", "reports\changes", "scripts"
)
foreach ($dir in $dirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Copy-Item (Join-Path $SkillDir "scripts\tabular.py") "scripts\tabular.py"
Copy-Item $Csv (Join-Path "data\raw" (Split-Path $Csv -Leaf))

@"
.venv/
__pycache__/
*.pyc
db/*.sqlite
db/*.sqlite-*
db/backups/
"@ | Set-Content -Encoding ascii .gitignore
```

## Estructura generada

```text
ventas_enero/
├── pyproject.toml / uv.lock / .python-version / .gitignore
├── README.md
├── data/
│   ├── raw/                 # CSV original mientras corre init
│   └── imports/             # CSV archivado, solo lectura
├── db/
│   ├── ventas_enero.sqlite
│   └── backups/             # backups de estructura o --backup
├── schema/
│   ├── profile.json
│   ├── mapping.json
│   ├── schema.sql
│   └── history/             # snapshots de esquema
├── sql/
│   ├── views/
│   └── queries/
├── functions/
├── reports/
│   ├── ANALISIS.md
│   ├── rechazos.csv        # solo si init rechazó filas
│   └── changes/             # SQL, undo y reportes de mutate
├── scripts/tabular.py
├── FUNCTIONS.md
└── README.md
```

## Backups y retención

- `init --force`: backup antes de recrear.
- `mutate` con DDL o `--backup`: backup antes de aplicar.
- `mutate` DML normal: undo log, no backup completo.
- Backups en `db/backups/`, últimos 10 por defecto.
- Undo en `reports/changes/`, últimos 100 por defecto.
- Snapshots de esquema en `schema/history/`.

No hay borrado automático de `_changes`; el historial de operaciones es texto y
no pesa.

## Notas de Windows

- Cierra Excel, DB Browser o cualquier programa que tenga abierto el CSV o la
  base antes de `init` o `mutate`.
- Evita OneDrive para el proyecto; puede bloquear `.venv`, la base y los
  archivos temporales.
- Usa una ruta corta, por ejemplo `C:\datos\ventas_enero`.
- No uses `sqlite3`; usa `tabular.py query` o `schema`.
- Para el hash usa `Get-FileHash archivo.csv -Algorithm SHA256`.
- Si usas WSL, instala la skill en el home de Linux y evita `/mnt/c` para
  `.venv` y la base.
- Windows Terminal o PowerShell 7 muestran mejor UTF-8.

## Comprobar la instalación

```bash
uv run python scripts/tabular.py version
uv run python scripts/tabular.py profile data/raw/ventas_enero.csv
```
