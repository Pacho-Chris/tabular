# Instalación y estructura del proyecto

## Requisitos

- `uv` para obtener Python 3.12 y ejecutar el código.
- Python 3.12 gestionado por uv.
- SQLite viene incluido en Python.
- No se necesita pandas, openpyxl ni el CLI de SQLite.
- Git es opcional para el proyecto.

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

Reinicia la terminal y comprueba:

```bash
uv --version
```

## Instalar la skill

La carpeta completa debe quedar en:

```text
~/.agents/skills/tabular/
~/.config/opencode/skills/tabular/
```

En Windows:

```text
C:\Users\<usuario>\.agents\skills\tabular\
C:\Users\<usuario>\.config\opencode\skills\tabular\
```

Contenido:

```text
tabular/
├── SKILL.md
├── references/
│   ├── setup.md
│   ├── csvsqlite.md
│   ├── tabular.md
│   ├── data-guide.md
│   └── templates.md
└── scripts/
    ├── csvsqlite.py
    └── tabular.py
```

`csvsqlite.py` es temporal en cada proyecto. `tabular.py` se queda.

## Crear el proyecto

### Linux, macOS o Git Bash

```bash
SKILL_DIR="$HOME/.agents/skills/tabular"
PROJECT="datos"
CSV="/ruta/al/datos.csv"

uv init --bare --python 3.12 --name "$PROJECT" "$PROJECT"
cd "$PROJECT"
uv python pin 3.12

mkdir -p data/imports db/backups schema/history \
  sql/views sql/queries functions reports/changes scripts

# csvsqlite solo se necesita durante la construcción
cp "$SKILL_DIR/scripts/csvsqlite.py" scripts/csvsqlite.py
# tabular se queda en el proyecto
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
$Project  = "datos"
$Csv      = "C:\datos\datos.csv"

uv init --bare --python 3.12 --name $Project $Project
Set-Location $Project
uv python pin 3.12

$dirs = @(
  "data\raw", "data\imports", "db\backups", "schema\history",
  "sql\views", "sql\queries", "functions", "reports\changes", "scripts"
)
foreach ($dir in $dirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

Copy-Item (Join-Path $SkillDir "scripts\csvsqlite.py") "scripts\csvsqlite.py"
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

`uv` no necesita dependencias: ambos scripts usan solo la librería estándar.
Se puede ejecutar con:

```bash
uv run python scripts/csvsqlite.py ...
uv run python scripts/tabular.py ...
```

o directamente con `python`, si ya está disponible.

`uv init` puede normalizar el nombre del paquete (`datos` → `datos`), pero la
carpeta y la base conservan el nombre que elijas.

## Estructura final

```text
proyecto/
├── pyproject.toml / uv.lock / .python-version / .gitignore
├── README.md
├── data/
│   ├── raw/                 # CSV mientras se construye
│   └── imports/             # CSV archivado, solo lectura
├── db/
│   ├── datos.sqlite
│   └── backups/
├── plantilla.yaml
├── schema/
│   ├── schema.sql
│   └── history/
├── scripts/
│   └── tabular.py           # csvsqlite se elimina al terminar
├── sql/
│   ├── views/
│   └── queries/
├── functions/
├── FUNCTIONS.md
├── reports/
│   ├── ANALISIS.md
│   ├── check_datos.md
│   └── changes/
└── .gitignore
```

## Notas de Windows

- Cierra Excel, DB Browser o cualquier programa que tenga abierto el CSV o la
  base antes de construir o mutar.
- Evita OneDrive para el proyecto; puede bloquear `.venv`, la base y los
  temporales.
- Usa una ruta corta, por ejemplo `C:\datos\proyecto`.
- Para el hash usa `Get-FileHash archivo.csv -Algorithm SHA256`.
- Windows Terminal o PowerShell 7 muestran mejor UTF-8.
- Si usas WSL, instala la skill en el home de Linux y evita `/mnt/c` para
  `.venv` y la base.
