#!/usr/bin/env python3
"""tabular: convierte un CSV en un proyecto SQLite reproducible.

Uso:
    python scripts/tabular.py profile data/raw/ventas.csv
    python scripts/tabular.py load schema/mapping.json --dry-run
    python scripts/tabular.py load schema/mapping.json
    python scripts/tabular.py verify --mapping schema/mapping.json
    python scripts/tabular.py query "SELECT ..."
    python scripts/tabular.py schema
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

VERSION = "0.3.0-rc1"
csv.field_size_limit(10 * 1024 * 1024)

DEFAULT_NULLS = ["", "na", "n/a", "null", "none", "-", "--", "sin dato", "s/d"]
TRUE_VALUES = {"1", "si", "sí", "true", "verdadero", "yes", "y", "x"}
FALSE_VALUES = {"0", "no", "false", "falso", "n"}
ALLOWED_TYPES = {"TEXT", "INTEGER", "REAL", "MONEY", "DATE", "BOOLEAN", "FK"}
RESERVED_COLUMNS = {"id", "_source_row", "_row_hash", "_import_id", "_change_id"}
RESERVED_TABLES = {"_ingest", "_imports", "_changes"}

UNDO_RETENTION = 100
BACKUP_RETENTION = 10

DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
]


class TabularError(Exception):
    """Error controlado de la herramienta."""


class ConversionError(Exception):
    """Error al convertir un valor de una fila."""


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def configure_console() -> None:
    """Evita UnicodeEncodeError en consolas Windows con code page antigua."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def ensure_sqlite_supported() -> None:
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise TabularError(
            f"SQLite {sqlite3.sqlite_version} es demasiado antiguo. "
            "Se requiere SQLite 3.37 o superior para las tablas STRICT. "
            "Actualiza Python o usa una versión de Python que incluya un SQLite reciente."
        )


def replace_with_retry(
    source: Path, target: Path, attempts: int = 5, delay: float = 0.5
) -> None:
    """En Windows un antivirus o una app abierta puede bloquear os.replace."""
    last_error: PermissionError | None = None
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    raise TabularError(
        "No se pudo reemplazar la base de datos. "
        "Cierra Excel, DB Browser o cualquier programa que tenga abierto el archivo "
        f"{target.name} y vuelve a intentarlo."
    ) from last_error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return value or "datos"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def format_value(value: Any, max_width: int = 60) -> str:
    if value is None:
        text = "NULL"
    else:
        text = str(value).replace("\n", "\\n").replace("\r", "").replace("|", "\\|")
    if len(text) > max_width:
        text = text[:max_width] if max_width <= 3 else text[: max_width - 3] + "..."
    return text


def dedupe_headers(headers: list[str]) -> list[str]:
    counts: Counter[str] = Counter()
    result: list[str] = []
    for header in headers:
        name = str(header)
        counts[name] += 1
        result.append(name if counts[name] == 1 else f"{name}_{counts[name]}")
    return result


def format_table(headers: list[str], rows: list[list[Any]], max_width: int = 60) -> str:
    if not headers:
        return "(sin columnas)"
    rendered = [[format_value(value, max_width) for value in row] for row in rows]
    widths = []
    for index, header in enumerate(headers):
        width = len(str(header))
        for row in rendered:
            if index < len(row):
                width = max(width, len(row[index]))
        widths.append(min(width, max_width))
    lines = []
    lines.append("| " + " | ".join(str(h)[:max_width].ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    lines.append("|" + "|".join("-" * (width + 2) for width in widths) + "|")
    for row in rendered:
        cells = []
        for index in range(len(headers)):
            value = row[index] if index < len(row) else ""
            cells.append(value.ljust(widths[index]))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def ensure_csv(path: Path) -> None:
    if not path.exists():
        raise TabularError(f"No existe el archivo: {path}")
    if path.suffix.lower() != ".csv":
        raise TabularError(
            "Solo se aceptan archivos .csv. "
            "Si tienes una hoja de cálculo, guárdala como CSV UTF-8 y vuelve a intentarlo. "
            f"Archivo recibido: {path.name}"
        )


def resolve_root(mapping_path: Path) -> Path:
    mapping_path = mapping_path.resolve()
    if mapping_path.parent.name == "schema":
        return mapping_path.parent.parent
    return Path.cwd().resolve()


def resolve_source_path(raw_path: str, root: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    candidate = root / path
    if candidate.exists():
        return candidate
    return (Path.cwd() / path).resolve()


def default_db_path(root: Path) -> Path:
    return root / "db" / f"{slugify(root.name)}.sqlite"


def resolve_db(db_arg: str | None, root: Path | None = None) -> Path:
    if db_arg:
        path = Path(db_arg).expanduser()
        if not path.is_absolute():
            path = (root or Path.cwd()) / path
        return path.resolve()
    base = (root or Path.cwd()).resolve()
    candidates = sorted((base / "db").glob("*.sqlite"))
    candidates += sorted((base / "db").glob("*.db"))
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise TabularError(
            f"No encontré una base SQLite en {base / 'db'}. Usa --db o ejecuta load primero."
        )
    names = ", ".join(path.name for path in candidates)
    raise TabularError(f"Hay varias bases ({names}). Indica cuál usar con --db.")


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise TabularError(f"No existe la base: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise TabularError(f"No se pudo abrir la base: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    return conn


def connect_readwrite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_database_absent(db_path: Path, force: bool) -> None:
    if not db_path.exists():
        return
    if force:
        return
    raise TabularError(
        f"Ya existe una base en {db_path}. La base es la fuente de verdad: "
        "usa `mutate` para cambiarla o `init --force` para recrearla con backup."
    )


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def make_readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD)
    except OSError as exc:
        raise TabularError(f"No se pudo marcar como solo lectura {path}: {exc}") from exc


def archive_source(source_path: Path, root: Path, archive_dir: str) -> Path:
    target_dir = Path(archive_dir).expanduser()
    if not target_dir.is_absolute():
        target_dir = root / target_dir
    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    source_path = source_path.resolve()
    if source_path.parent == target_dir:
        make_readonly(source_path)
        return source_path

    safe_stem = re.sub(r'[<>:"/\\|?*]+', "_", source_path.stem).strip(" .") or "import"
    date_prefix = date.today().isoformat()
    target = target_dir / f"{date_prefix}_{safe_stem}.csv"
    counter = 2
    while target.exists():
        target = target_dir / f"{date_prefix}_{safe_stem}_{counter}.csv"
        counter += 1
    try:
        shutil.copy2(source_path, target)
    except OSError as exc:
        raise TabularError(f"No se pudo archivar el CSV: {exc}") from exc
    make_readonly(target)
    return target


def prune_backups(backup_dir: Path, keep: int) -> None:
    if keep < 1:
        return
    files = sorted(
        backup_dir.glob("*.sqlite"), key=lambda item: item.stat().st_mtime, reverse=True
    )
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def backup_database(
    db_path: Path, root: Path, reason: str, keep: int = BACKUP_RETENTION
) -> Path:
    if not db_path.exists():
        raise TabularError(f"No existe la base para respaldar: {db_path}")
    backup_dir = root / "db" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_reason = slugify(reason)
    target = backup_dir / f"{slugify(db_path.stem)}_{timestamp}_{safe_reason}.sqlite"
    counter = 2
    while target.exists():
        target = backup_dir / (
            f"{slugify(db_path.stem)}_{timestamp}_{safe_reason}_{counter}.sqlite"
        )
        counter += 1
    try:
        with closing(sqlite3.connect(db_path)) as source, closing(
            sqlite3.connect(target)
        ) as destination:
            source.backup(destination)
        with closing(sqlite3.connect(target)) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise TabularError(f"El backup no pasó integrity_check: {target}")
    except Exception:
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise
    prune_backups(backup_dir, keep)
    return target


def dump_schema_sql(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type "
        "WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 "
        "WHEN 'trigger' THEN 3 ELSE 4 END, name"
    ).fetchall()
    lines = [
        f"-- Esquema real generado por tabular {VERSION}",
        "-- No editar a mano: usa `schema --write` o `mutate`.",
        "",
        "PRAGMA foreign_keys = ON;",
        "",
    ]
    for row in rows:
        sql = (row["sql"] or "").strip()
        if not sql:
            continue
        lines.append(sql.rstrip(";") + ";")
        lines.append("")
    return "\n".join(lines)


def write_schema_sql(conn: sqlite3.Connection, root: Path) -> Path:
    path = root / "schema" / "schema.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_schema_sql(conn), encoding="utf-8")
    return path


def expected_sql_type(column_type: str) -> str:
    return {
        "TEXT": "TEXT",
        "INTEGER": "INTEGER",
        "REAL": "REAL",
        "MONEY": "INTEGER",
        "DATE": "TEXT",
        "BOOLEAN": "INTEGER",
        "FK": "INTEGER",
    }[column_type]


def check_mapping_drift(conn: sqlite3.Connection, mapping: dict[str, Any]) -> list[dict[str, str]]:
    drift: list[dict[str, str]] = []
    expected_tables: dict[str, dict[str, str]] = {}
    for table in mapping["tables"]:
        columns = {"id": "INTEGER"}
        for column in table["columns"]:
            columns[column["name"]] = expected_sql_type(column["type"])
        if table["kind"] == "fact":
            columns["_source_row"] = "INTEGER"
        expected_tables[table["name"]] = columns

    db_tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if row["name"] not in {"_imports", "_changes"}
    }
    for table_name, expected in expected_tables.items():
        if table_name not in db_tables:
            drift.append(
                {
                    "type": "tabla_faltante",
                    "table": table_name,
                    "detail": "existe en mapping pero no en la base",
                }
            )
            continue
        actual = {
            row["name"]: str(row["type"]).upper()
            for row in conn.execute(
                f"PRAGMA table_info({quote_ident(table_name)})"
            ).fetchall()
        }
        for column_name, column_type in expected.items():
            if column_name not in actual:
                drift.append(
                    {
                        "type": "columna_faltante",
                        "table": table_name,
                        "detail": f"{column_name} ({column_type})",
                    }
                )
            elif actual[column_name] != column_type:
                drift.append(
                    {
                        "type": "tipo_distinto",
                        "table": table_name,
                        "detail": (
                            f"{column_name}: mapping {column_type}, base {actual[column_name]}"
                        ),
                    }
                )
        for column_name in sorted(set(actual) - set(expected)):
            drift.append(
                {
                    "type": "columna_extra",
                    "table": table_name,
                    "detail": column_name,
                }
            )
    for table_name in sorted(db_tables - set(expected_tables)):
        drift.append(
            {
                "type": "tabla_extra",
                "table": table_name,
                "detail": "existe en la base pero no en mapping",
            }
        )
    return drift


def strip_sql_comments(sql: str) -> str:
    return re.sub(r"^\s*(--[^\n]*\n|/\*.*?\*/)*", "", sql, flags=re.DOTALL).strip()


def leading_keyword(statement: str) -> str:
    clean = strip_sql_comments(statement)
    match = re.match(r"([A-Za-z]+)", clean)
    return match.group(1).upper() if match else ""


def split_sql_statements(sql: str) -> list[str]:
    text = sql.strip()
    if not text:
        raise TabularError("El SQL está vacío.")
    if not text.endswith(";"):
        text += "\n;"
    statements: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement.strip(";").strip():
                statements.append(statement)
            buffer = ""
    if buffer.strip(";").strip():
        raise TabularError("El SQL quedó incompleto; revisa comillas y paréntesis.")
    if not statements:
        raise TabularError("No hay sentencias SQL para ejecutar.")
    return statements


def classify_statement(statement: str) -> str:
    keyword = leading_keyword(statement)
    upper = strip_sql_comments(statement).upper()
    if keyword in {"SELECT", "PRAGMA", "ATTACH", "DETACH", "VACUUM", "EXPLAIN"}:
        raise TabularError(
            f"`{keyword}` no se permite en mutate. Usa `query` para lecturas."
        )
    if keyword in {"INSERT", "REPLACE"}:
        return "data"
    if keyword == "UPDATE":
        return "data_full" if not re.search(r"\bWHERE\b", upper) else "data"
    if keyword == "DELETE":
        return "data_full" if not re.search(r"\bWHERE\b", upper) else "data_delete"
    if keyword == "CREATE":
        return "schema"
    if keyword == "ALTER":
        return "schema_destructive" if re.search(r"\b(DROP|RENAME)\b", upper) else "schema"
    if keyword == "DROP":
        return "schema_destructive"
    if keyword == "WITH":
        if re.search(r"\bDELETE\b", upper):
            return "data_full"
        if re.search(r"\bUPDATE\b", upper):
            return "data_full"
        if re.search(r"\bINSERT\b", upper):
            return "data"
        raise TabularError(
            "WITH sin INSERT/UPDATE/DELETE no es una mutación. Usa `query`."
        )
    raise TabularError(f"Sentencia no soportada en mutate: {keyword or statement[:40]!r}")


def table_of_statement(statement: str) -> str | None:
    clean = strip_sql_comments(statement)
    identifier = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
    patterns = [
        rf"^\s*INSERT\s+(?:OR\s+\w+\s+)?INTO\s+({identifier})",
        rf"^\s*REPLACE\s+INTO\s+({identifier})",
        rf"^\s*UPDATE\s+({identifier})",
        rf"^\s*DELETE\s+FROM\s+({identifier})",
    ]
    for pattern in patterns:
        match = re.match(pattern, clean, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip('"')
    return None


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"


def build_update_capture_sql(statement: str) -> str | None:
    clean = strip_sql_comments(statement).rstrip().rstrip(";")
    identifier = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
    match = re.match(
        rf"^\s*UPDATE\s+({identifier})\s+SET\s+(.+)$",
        clean,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    table = match.group(1).strip('"')
    rest = re.sub(r"\bRETURNING\b.*$", "", match.group(2), flags=re.IGNORECASE | re.DOTALL)
    where_match = re.search(r"\bWHERE\b(.+)$", rest, flags=re.IGNORECASE | re.DOTALL)
    if where_match:
        return f"SELECT * FROM {quote_ident(table)} WHERE {where_match.group(1)}"
    return f"SELECT * FROM {quote_ident(table)}"


def fetch_rows(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    clean = strip_sql_comments(sql).lstrip().upper()
    if not clean.startswith("SELECT"):
        raise TabularError("--capture-sql debe ser una consulta SELECT.")
    cursor = conn.execute(sql)
    return [dict(row) for row in cursor.fetchall()]


def generate_undo_sql(action: str, table: str, rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return f"-- Sin filas afectadas en {table}.\n"
    columns = list(rows[0].keys())
    quoted_columns = ", ".join(quote_ident(column) for column in columns)
    if action == "delete":
        values = [
            "(" + ", ".join(sql_literal(row.get(column)) for column in columns) + ")"
            for row in rows
        ]
        return (
            f"INSERT INTO {quote_ident(table)} ({quoted_columns}) VALUES "
            + ", ".join(values)
            + ";\n"
        )
    if "id" not in columns:
        return None
    if action == "insert":
        ids = [row.get("id") for row in rows]
        if any(value is None for value in ids):
            return None
        return (
            f"DELETE FROM {quote_ident(table)} WHERE "
            + "id IN (" + ", ".join(sql_literal(value) for value in ids) + ");\n"
        )
    if action == "update":
        statements = []
        for row in rows:
            assignments = ", ".join(
                f"{quote_ident(column)} = {sql_literal(row.get(column))}"
                for column in columns
                if column != "id"
            )
            statements.append(
                f"UPDATE {quote_ident(table)} SET {assignments} "
                f"WHERE id = {sql_literal(row.get('id'))};"
            )
        return "\n".join(statements) + "\n"
    return None


def prune_undo_history(conn: sqlite3.Connection, root: Path, keep: int) -> None:
    if keep < 1:
        return
    rows = conn.execute(
        'SELECT id, undo_path FROM "_changes" '
        "WHERE undo_path IS NOT NULL ORDER BY id DESC"
    ).fetchall()
    for row in rows[keep:]:
        path = Path(row["undo_path"])
        if not path.is_absolute():
            path = root / path
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        conn.execute(
            'UPDATE "_changes" SET undo_path = NULL, reversible = 0 WHERE id = ?',
            (row["id"],),
        )
    conn.commit()


def history_summary(statement: str, kind: str) -> str:
    first_line = " ".join(strip_sql_comments(statement).split())
    if len(first_line) > 90:
        first_line = first_line[:87] + "..."
    return f"{kind}: {first_line}"


def detect_encoding(path: Path) -> str:
    raw = path.read_bytes()[: 1024 * 1024]
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError as exc:
            # Un corte al final del bloque puede partir un carácter multibyte.
            if exc.start >= len(raw) - 4:
                return encoding
    return "latin-1"


def validate_encoding(path: Path, encoding: str) -> None:
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            handle.read(1024 * 1024)
    except (LookupError, UnicodeDecodeError) as exc:
        raise TabularError(
            f"El archivo no se puede leer con encoding {encoding!r}: {exc}"
        ) from exc


def detect_delimiter(path: Path, encoding: str) -> str:
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            sample = handle.read(64 * 1024)
    except (OSError, UnicodeDecodeError) as exc:
        raise TabularError(f"No se pudo leer {path}: {exc}") from exc

    best: tuple[float, str] | None = None
    for delimiter in (",", ";", "\t", "|"):
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
        except csv.Error:
            continue
        rows = [row for row in rows[:40] if any(cell.strip() for cell in row)]
        if len(rows) < 2:
            continue
        counts = [len(row) for row in rows]
        mode, _ = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        consistency = sum(1 for count in counts if count == mode) / len(counts)
        score = consistency * 100 + min(mode, 20)
        if best is None or score > best[0]:
            best = (score, delimiter)
    return best[1] if best else ","


def read_all_records(
    path: Path, encoding: str, delimiter: str, max_rows: int | None = None
) -> list[tuple[int, list[str]]]:
    records: list[tuple[int, list[str]]] = []
    try:
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            for row in reader:
                records.append((reader.line_num, row))
                if max_rows and len(records) >= max_rows:
                    break
    except (csv.Error, UnicodeDecodeError) as exc:
        raise TabularError(f"No se pudo leer el CSV: {exc}") from exc
    return records


def detect_header_row(records: list[tuple[int, list[str]]]) -> int:
    best: tuple[int, int] | None = None
    for index, (_, row) in enumerate(records[:15]):
        nonempty = [cell.strip() for cell in row if cell.strip()]
        if len(nonempty) < 2:
            continue
        score = (len(nonempty), -index)
        if best is None or score > best[0]:
            best = (score, index + 1)
    return best[1] if best else 1


def detect_footer_rows(data: list[tuple[int, list[str]]]) -> tuple[int, list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    candidate_lines: set[int] = set()
    footer_rows = 0
    for offset in range(1, min(6, len(data)) + 1):
        line_number, row = data[-offset]
        cells = [cell.strip() for cell in row]
        nonempty = [cell for cell in cells if cell]
        first = nonempty[0].lower() if nonempty else ""
        looks_total = any(
            word in first for word in ("total", "subtotal", "suma", "gran total")
        )
        if looks_total:
            footer_rows = offset
            candidate_lines.add(line_number)
            candidates.append(
                {
                    "offset_from_end": offset,
                    "line": line_number,
                    "first_cell": nonempty[0] if nonempty else "",
                }
            )
            break
    for offset in range(1, min(4, len(data)) + 1):
        line_number, row = data[-offset]
        if line_number in candidate_lines:
            continue
        cells = [cell.strip() for cell in row]
        nonempty = [cell for cell in cells if cell]
        if len(nonempty) < 2:
            candidates.append(
                {
                    "offset_from_end": offset,
                    "line": line_number,
                    "first_cell": nonempty[0] if nonempty else "",
                    "reason": "pocas celdas",
                }
            )
    return footer_rows, candidates


def normalize_cells(row: list[str], width: int) -> tuple[list[str], str | None]:
    cells = [cell.strip() for cell in row]
    if len(cells) < width:
        return cells + [""] * (width - len(cells)), None
    if len(cells) > width:
        extra = cells[width:]
        if any(cell for cell in extra):
            return cells[:width], f"{len(cells)} columnas; se esperaban {width}"
        return cells[:width], None
    return cells, None


# ---------------------------------------------------------------------------
# Conversión de valores
# ---------------------------------------------------------------------------


def is_null(value: str, null_values: set[str]) -> bool:
    return value.strip().lower() in null_values


def normalize_date_format(fmt: str) -> str:
    if "%" in fmt:
        return fmt
    tokens = {
        "yyyy": "%Y",
        "yy": "%y",
        "dd": "%d",
        "mm": "%m",
        "hh": "%H",
        "mi": "%M",
        "ss": "%S",
    }
    return re.sub(
        r"yyyy|yy|dd|mm|hh|mi|ss",
        lambda match: tokens[match.group(0)],
        fmt.lower(),
    )


def parse_date_any(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    return None


def parse_date_value(value: str, date_format: str | None) -> date:
    text = value.strip()
    formats: list[str] = []
    if date_format:
        formats.append(normalize_date_format(date_format))
    formats.extend(fmt for fmt in DATE_FORMATS if fmt not in formats)
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    if re.fullmatch(r"\d{5}", text):
        serial = int(text)
        if 20000 <= serial <= 80000:
            return date(1899, 12, 30) + timedelta(days=serial)
    raise ConversionError(f"fecha inválida: {value!r}")


def is_ambiguous_date(value: str) -> bool:
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-]\d{2,4}", value.strip())
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return first <= 12 and second <= 12 and first != second


def normalize_numeric_text(value: str, decimal: str, thousands: str) -> str:
    if decimal == thousands:
        raise TabularError("locale.decimal y locale.thousands no pueden ser iguales")
    text = value.strip().replace("\u00a0", " ").replace(" ", "")
    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1]
    text = re.sub(r"[^\d,.\-+]", "", text)
    if thousands:
        text = text.replace(thousands, "")
    if decimal and decimal != ".":
        text = text.replace(decimal, ".")
    if text.count(".") > 1:
        raise ConversionError(f"número con varios separadores decimales: {value!r}")
    if text in {"", ".", "-", "+"}:
        raise ConversionError(f"número inválido: {value!r}")
    if negative_parentheses:
        text = "-" + text.lstrip("-")
    return text


def looks_numeric(value: str) -> bool:
    """True solo si el valor parece un número, no texto con dígitos."""
    text = value.strip()
    if not text:
        return False
    text = text.replace("(", "").replace(")", "")
    text = re.sub(
        r"[$€£¥]|\b(?:MXN|USD|EUR|COP|ARS|CLP|PEN)\b|%",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace("\u00a0", "").replace(" ", "")
    return bool(re.fullmatch(r"[+-]?[\d.,]+", text)) and any(
        character.isdigit() for character in text
    )


def parse_decimal(value: str, decimal: str, thousands: str) -> Decimal:
    text = normalize_numeric_text(value, decimal, thousands)
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ConversionError(f"número inválido: {value!r}") from exc


def parse_integer(value: str, decimal: str, thousands: str) -> int:
    number = parse_decimal(value, decimal, thousands)
    if number != number.to_integral_value():
        raise ConversionError(f"se esperaba un entero: {value!r}")
    return int(number)


def parse_money_cents(value: str, decimal: str, thousands: str) -> int:
    number = parse_decimal(value, decimal, thousands)
    cents = (number * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents)


def parse_boolean(value: str, true_values: set[str], false_values: set[str]) -> int:
    text = value.strip().lower()
    if text in true_values:
        return 1
    if text in false_values:
        return 0
    raise ConversionError(f"valor booleano inválido: {value!r}")


def convert_value(
    raw: str,
    column: dict[str, Any],
    locale: dict[str, Any],
    null_values: set[str],
) -> Any:
    text = raw.strip()
    if is_null(text, null_values):
        if column.get("required"):
            raise ConversionError("valor obligatorio vacío")
        return None

    column_type = column["type"]
    if column_type == "TEXT":
        return text
    if column_type == "INTEGER":
        return parse_integer(text, locale["decimal"], locale["thousands"])
    if column_type == "REAL":
        return float(parse_decimal(text, locale["decimal"], locale["thousands"]))
    if column_type == "MONEY":
        return parse_money_cents(text, locale["decimal"], locale["thousands"])
    if column_type == "DATE":
        return parse_date_value(text, locale.get("date")).isoformat()
    if column_type == "BOOLEAN":
        return parse_boolean(text, locale["true_values"], locale["false_values"])
    if column_type == "FK":
        return text
    raise ConversionError(f"tipo no soportado: {column_type}")


# ---------------------------------------------------------------------------
# Perfilado
# ---------------------------------------------------------------------------


def infer_type(values: list[str], null_values: set[str]) -> tuple[str, list[str]]:
    issues: list[str] = []
    non_null = [value for value in values if not is_null(value, null_values)]
    if not non_null:
        return "text", ["columna vacía"]

    leading_zero = any(re.fullmatch(r"0\d+", value.strip()) for value in non_null)
    if leading_zero:
        issues.append("ceros iniciales: declarar TEXT")

    if all(re.fullmatch(r"\d{5}", value.strip()) for value in non_null) and all(
        20000 <= int(value.strip()) <= 80000 for value in non_null
    ):
        issues.append("posible fecha serial de Excel: verificar antes de declarar DATE")

    has_currency = any(
        re.search(r"[$€£¥]|\b(?:MXN|USD|EUR|COP|ARS|CLP|PEN)\b", value, re.IGNORECASE)
        for value in non_null
    )
    has_percent = any("%" in value for value in non_null)
    if has_percent:
        issues.append("posible porcentaje: definir si se guarda como fracción o base 100")

    parsed_dates = [parse_date_any(value) for value in non_null]
    if all(parsed_dates):
        ambiguous = sum(1 for value in non_null if is_ambiguous_date(value))
        if ambiguous:
            issues.append(f"{ambiguous} fechas ambiguas dd/mm vs mm/dd")
        return "date", issues
    date_hits = sum(1 for parsed in parsed_dates if parsed is not None)
    if date_hits and date_hits >= len(non_null) * 0.5:
        invalid = len(non_null) - date_hits
        if invalid:
            issues.append(f"{invalid} fecha(s) inválida(s)")
        ambiguous = sum(1 for value in non_null if is_ambiguous_date(value))
        if ambiguous:
            issues.append(f"{ambiguous} fechas ambiguas dd/mm vs mm/dd")
        return "date", issues

    lower_values = {value.strip().lower() for value in non_null}
    if lower_values <= (TRUE_VALUES | FALSE_VALUES):
        return "boolean", issues

    if not all(looks_numeric(value) for value in non_null):
        if any(looks_numeric(value) for value in non_null):
            issues.append("tipos mixtos: texto y números")
        return "text", issues

    comma_decimal = any(
        re.search(r"\d,\d{1,2}(?:\D|$)", value) for value in non_null
    )
    dot_decimal = any(
        re.search(r"\d\.\d{1,2}(?:\D|$)", value) for value in non_null
    )
    if comma_decimal and not dot_decimal:
        numeric_locales = [(",", "."), (".", ",")]
    else:
        numeric_locales = [(".", ","), (",", ".")]
    for decimal, thousands in numeric_locales:
        try:
            numbers = [parse_decimal(value, decimal, thousands) for value in non_null]
        except (ConversionError, TabularError):
            continue
        if leading_zero:
            return "text", issues
        if has_currency:
            issues.append("moneda detectada: declarar MONEY")
            return "money", issues
        if any(re.search(r"[^\d\s.,()+\-]", value) for value in non_null):
            issues.append("contiene símbolos o texto además del número")
        if all(number == number.to_integral_value() for number in numbers):
            if decimal == ",":
                issues.append("posible coma decimal")
            return "integer", issues
        if decimal == ",":
            issues.append("posible coma decimal")
        return "number", issues

    if any("," in value and "." in value for value in non_null):
        issues.append("mezcla de coma y punto decimal")
    return "text", issues


def profile_column(
    name: str,
    source_name: str,
    index: int,
    values: list[str],
    null_values: set[str],
) -> dict[str, Any]:
    null_count = sum(1 for value in values if not value.strip())
    non_null = [value for value in values if not is_null(value, null_values)]
    distinct_values = list(dict.fromkeys(non_null))
    inferred, issues = infer_type(values, null_values)
    null_like = len(values) - len(non_null)
    if null_like:
        issues.append(f"{null_like} valores nulos")
    if null_like > null_count:
        issues.append(f"{null_like - null_count} nulos disfrazados")
    return {
        "name": name,
        "source_name": source_name,
        "index": index,
        "nulls": null_count,
        "null_like": null_like,
        "distinct": len(distinct_values),
        "examples": distinct_values[:5],
        "inferred": inferred,
        "issues": issues,
    }


def build_profile(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    path = Path(args.input).expanduser().resolve()
    ensure_csv(path)

    if args.max_rows is not None and args.max_rows < 1:
        raise TabularError("--max-rows debe ser mayor que cero.")
    encoding = args.encoding or detect_encoding(path)
    validate_encoding(path, encoding)
    delimiter = args.delimiter or detect_delimiter(path, encoding)
    if len(delimiter) != 1:
        raise TabularError("El delimitador debe ser un solo carácter.")

    records = read_all_records(path, encoding, delimiter, max_rows=args.max_rows)
    if not records:
        raise TabularError("El CSV está vacío.")

    header_row = args.header_row or detect_header_row(records)
    if header_row < 1 or header_row > len(records):
        raise TabularError(f"header_row fuera de rango: {header_row}")
    header = [cell.lstrip("\ufeff").strip() for cell in records[header_row - 1][1]]
    if not any(header):
        raise TabularError("La fila de encabezado está vacía.")

    data = records[header_row:]
    while data and all(not cell.strip() for cell in data[-1][1]):
        data.pop()

    suggested_footer, footer_candidates = detect_footer_rows(data)
    footer_rows = args.footer_rows if args.footer_rows is not None else suggested_footer
    if footer_rows < 0:
        raise TabularError("footer_rows no puede ser negativo.")
    if footer_rows:
        if footer_rows >= len(data):
            raise TabularError("footer_rows es mayor o igual que las filas de datos.")
        data = data[:-footer_rows]

    data_rows = 0
    blank_rows = 0
    ragged_rows = 0
    normalized_rows: list[list[str]] = []
    for _, row in data:
        if len(row) != len(header):
            ragged_rows += 1
        cells, _problem = normalize_cells(row, len(header))
        if all(not cell for cell in cells):
            blank_rows += 1
            continue
        data_rows += 1
        normalized_rows.append(cells)

    header_counts = Counter(header)
    seen_headers: Counter[str] = Counter()
    labels: list[str] = []
    for index, raw_name in enumerate(header):
        name = raw_name or f"col_{index + 1}"
        if header_counts[raw_name] > 1:
            seen_headers[name] += 1
            labels.append(f"{name} [{seen_headers[name]}]")
        else:
            labels.append(name)
    columns = [
        profile_column(
            labels[index],
            header[index] or f"col_{index + 1}",
            index + 1,
            [row[index] for row in normalized_rows],
            set(DEFAULT_NULLS),
        )
        for index in range(len(header))
    ]

    duplicate_rows = 0
    if normalized_rows:
        counts = Counter(tuple(row) for row in normalized_rows)
        duplicate_rows = sum(count - 1 for count in counts.values() if count > 1)

    candidate_keys: list[str] = []
    dimension_candidates: list[dict[str, Any]] = []
    for index, column in enumerate(columns):
        values = [row[index] for row in normalized_rows]
        non_null = [value for value in values if not is_null(value, set(DEFAULT_NULLS))]
        if data_rows and len(non_null) == data_rows and column["distinct"] == data_rows:
            candidate_keys.append(column["name"])
        if column["inferred"] == "text" and 1 < column["distinct"] <= 1000:
            repetition = 1 - (column["distinct"] / data_rows) if data_rows else 0
            if repetition >= 0.5:
                dimension_candidates.append(
                    {
                        "column": column["name"],
                        "distinct": column["distinct"],
                        "repetition_ratio": round(repetition, 4),
                    }
                )

    duplicate_headers = sorted(
        name for name, count in header_counts.items() if name and count > 1
    )
    empty_headers = [index + 1 for index, name in enumerate(header) if not name]
    comma_decimal = any(
        any("coma decimal" in issue for issue in column["issues"])
        for column in columns
    )
    locale_suggested = (
        {"decimal": ",", "thousands": "."}
        if comma_decimal
        else {"decimal": ".", "thousands": ","}
    )

    recommendations: list[str] = [f"Usar header_row={header_row}."]
    recommendations.append(
        "Locale numérico sugerido: decimal "
        f"'{locale_suggested['decimal']}' y miles '{locale_suggested['thousands']}'."
    )
    if duplicate_headers:
        recommendations.append(
            "Encabezados duplicados ("
            + ", ".join(duplicate_headers)
            + "): usa source_index en el mapping."
        )
    if empty_headers:
        recommendations.append(
            "Encabezados vacíos en las posiciones "
            + ", ".join(str(position) for position in empty_headers)
            + ": usa source_index."
        )
    if footer_rows:
        recommendations.append(
            f"Excluir {footer_rows} fila(s) final(es) con footer_rows={footer_rows}."
        )
    if duplicate_rows:
        recommendations.append(
            f"Hay {duplicate_rows} fila(s) duplicadas; define la clave de negocio antes de deduplicar."
        )
    if candidate_keys:
        recommendations.append("Candidatos a clave: " + ", ".join(candidate_keys) + ".")
    if dimension_candidates:
        recommendations.append(
            "Candidatos a dimensión: "
            + ", ".join(item["column"] for item in dimension_candidates)
            + "."
        )
    for column in columns:
        for issue in column["issues"]:
            if "fecha" in issue or "ceros" in issue or "coma decimal" in issue:
                recommendations.append(f"{column['name']}: {issue}.")
                break
    ambiguous_columns = [
        column["name"]
        for column in columns
        if any("fechas ambiguas" in issue for issue in column["issues"])
    ]
    if ambiguous_columns:
        recommendations.append(
            "Confirma el formato de fecha (dd/mm vs mm/dd) para: "
            + ", ".join(ambiguous_columns)
            + "."
        )

    profile = {
        "tool": "tabular",
        "version": VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "path": str(path),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "sampled": bool(args.max_rows and len(records) >= args.max_rows),
        },
        "csv": {
            "encoding": encoding,
            "delimiter": delimiter,
            "header_row": header_row,
            "header": header,
            "duplicate_headers": duplicate_headers,
            "empty_headers": empty_headers,
            "data_rows": data_rows,
            "blank_rows": blank_rows,
            "ragged_rows": ragged_rows,
            "footer_rows_suggested": suggested_footer,
            "footer_candidates": footer_candidates,
        },
        "columns": columns,
        "locale_suggested": locale_suggested,
        "findings": {
            "duplicate_rows": duplicate_rows,
            "candidate_keys": candidate_keys,
            "dimension_candidates": dimension_candidates,
        },
        "recommendations": recommendations,
    }

    out_path = Path(args.out).expanduser()
    write_json(out_path, profile)

    lines = [
        "# Perfil del CSV",
        "",
        f"- Archivo: `{path}`",
        f"- Encoding: `{encoding}`",
        f"- Delimitador: `{delimiter}`",
        f"- Encabezado: fila {header_row}",
        f"- Filas de datos: {data_rows}",
        f"- Filas en blanco: {blank_rows}",
        f"- Filas irregulares: {ragged_rows}",
        f"- Filas duplicadas: {duplicate_rows}",
        f"- Footer sugerido: {suggested_footer}",
        "",
        "## Columnas",
        "",
    ]
    table_rows = [
        [
            column["name"],
            column["inferred"],
            column["null_like"],
            column["distinct"],
            ", ".join(str(value) for value in column["examples"][:3]),
            "; ".join(column["issues"]),
        ]
        for column in columns
    ]
    lines.append(
        format_table(
            ["Columna", "Tipo sugerido", "Nulos", "Distintos", "Ejemplos", "Alertas"],
            table_rows,
            max_width=40,
        )
    )
    if candidate_keys:
        lines.extend(["", "**Candidatos a clave:** " + ", ".join(candidate_keys)])
    if dimension_candidates:
        lines.extend(
            [
                "",
                "**Candidatos a dimensión:** "
                + ", ".join(item["column"] for item in dimension_candidates),
            ]
        )
    if recommendations:
        lines.extend(["", "## Recomendaciones", ""])
        lines.extend(f"- {item}" for item in recommendations)
    lines.extend(["", f"Perfil escrito en `{out_path}`."])
    return profile, "\n".join(lines)


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def load_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise TabularError(f"No existe el mapping: {path}")
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise TabularError(f"mapping.json inválido: {exc}") from exc
    if not isinstance(data, dict):
        raise TabularError("mapping.json debe contener un objeto JSON.")

    source = data.get("source")
    if not isinstance(source, dict) or not source.get("path"):
        raise TabularError("mapping.source.path es obligatorio.")
    if not str(source["path"]).lower().endswith(".csv"):
        raise TabularError("mapping.source.path debe apuntar a un archivo .csv.")

    locale = data.get("locale") or {}
    decimal = str(locale.get("decimal", "."))
    thousands = str(locale.get("thousands", ","))
    if len(decimal) != 1 or len(thousands) != 1:
        raise TabularError("locale.decimal y locale.thousands deben ser un carácter.")
    if decimal == thousands:
        raise TabularError("locale.decimal y locale.thousands no pueden ser iguales.")
    true_values_raw = locale.get("true_values")
    false_values_raw = locale.get("false_values")
    if true_values_raw is not None and not isinstance(true_values_raw, list):
        raise TabularError("locale.true_values debe ser una lista.")
    if false_values_raw is not None and not isinstance(false_values_raw, list):
        raise TabularError("locale.false_values debe ser una lista.")
    true_values = (
        {str(value).lower() for value in true_values_raw}
        if true_values_raw is not None
        else set(TRUE_VALUES)
    )
    false_values = (
        {str(value).lower() for value in false_values_raw}
        if false_values_raw is not None
        else set(FALSE_VALUES)
    )
    normalized_locale = {
        "decimal": decimal,
        "thousands": thousands,
        "date": locale.get("date"),
        "true_values": true_values,
        "false_values": false_values,
    }

    null_values_raw = data.get("null_values")
    if null_values_raw is None:
        null_values = set(DEFAULT_NULLS)
    else:
        if not isinstance(null_values_raw, list):
            raise TabularError("null_values debe ser una lista.")
        null_values = {""}
        null_values.update(str(value).strip().lower() for value in null_values_raw)

    tables_raw = data.get("tables")
    if not isinstance(tables_raw, list) or not tables_raw:
        raise TabularError("mapping.tables debe ser una lista con al menos una tabla.")

    tables: list[dict[str, Any]] = []
    names: set[str] = set()
    for table_raw in tables_raw:
        if not isinstance(table_raw, dict):
            raise TabularError("Cada tabla debe ser un objeto JSON.")
        name = str(table_raw.get("name", "")).strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise TabularError(
                f"Nombre de tabla inválido: {name!r}. Usa snake_case ASCII."
            )
        if name in RESERVED_TABLES or name in names:
            raise TabularError(f"Nombre de tabla reservado o duplicado: {name}")
        names.add(name)
        kind = str(table_raw.get("kind", "")).lower()
        if kind not in {"dimension", "fact"}:
            raise TabularError(f"Tabla {name}: kind debe ser 'dimension' o 'fact'.")
        key_raw = table_raw.get("key", [])
        if key_raw is None:
            key_raw = []
        if not isinstance(key_raw, list):
            raise TabularError(f"Tabla {name}: key debe ser una lista.")
        key = [str(value) for value in key_raw]
        columns_raw = table_raw.get("columns")
        if not isinstance(columns_raw, list) or not columns_raw:
            raise TabularError(f"Tabla {name}: columns debe tener al menos una columna.")
        columns: list[dict[str, Any]] = []
        column_names: set[str] = set()
        for column_raw in columns_raw:
            if not isinstance(column_raw, dict):
                raise TabularError(f"Tabla {name}: cada columna debe ser un objeto.")
            column_name = str(column_raw.get("name", "")).strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", column_name):
                raise TabularError(
                    f"Tabla {name}: nombre de columna inválido {column_name!r}. Usa snake_case ASCII."
                )
            if column_name in RESERVED_COLUMNS or column_name in column_names:
                raise TabularError(
                    f"Tabla {name}: columna reservada o duplicada {column_name!r}."
                )
            column_names.add(column_name)
            column_type = str(column_raw.get("type", "")).upper()
            if column_type not in ALLOWED_TYPES:
                raise TabularError(
                    f"Tabla {name}.{column_name}: tipo inválido {column_type!r}. "
                    f"Usa {', '.join(sorted(ALLOWED_TYPES))}."
                )
            source_name = column_raw.get("source")
            source_index = column_raw.get("source_index")
            if source_name is None and source_index is None:
                raise TabularError(
                    f"Tabla {name}.{column_name}: declara source o source_index."
                )
            if source_index is not None:
                try:
                    source_index = int(source_index)
                except (TypeError, ValueError) as exc:
                    raise TabularError(
                        f"Tabla {name}.{column_name}: source_index debe ser entero."
                    ) from exc
                if source_index < 1:
                    raise TabularError(
                        f"Tabla {name}.{column_name}: source_index empieza en 1."
                    )
            reference = None
            if column_type == "FK":
                raw_reference = str(column_raw.get("references", "")).strip()
                if "." not in raw_reference:
                    raise TabularError(
                        f"Tabla {name}.{column_name}: references debe ser 'tabla.columna'."
                    )
                target_table, target_column = raw_reference.split(".", 1)
                reference = (target_table, target_column)
            elif column_raw.get("references"):
                raise TabularError(
                    f"Tabla {name}.{column_name}: references solo aplica a tipo FK."
                )
            if "required" in column_raw:
                required = bool(column_raw["required"])
            else:
                required = column_type == "FK" or column_name in key
            columns.append(
                {
                    "name": column_name,
                    "source": str(source_name).strip() if source_name is not None else None,
                    "source_index": source_index,
                    "type": column_type,
                    "required": required,
                    "references": reference,
                }
            )
        if kind == "dimension" and not key:
            raise TabularError(f"La dimensión {name} necesita key.")
        if len(set(key)) != len(key):
            raise TabularError(f"Tabla {name}: la key tiene columnas duplicadas.")
        for key_column in key:
            if key_column not in column_names:
                raise TabularError(f"Tabla {name}: key {key_column!r} no existe.")
        for column in columns:
            if column["name"] in key:
                column["required"] = True
        tables.append({"name": name, "kind": kind, "key": key, "columns": columns})

    by_name = {table["name"]: table for table in tables}
    for table in tables:
        for column in table["columns"]:
            if column["type"] != "FK":
                continue
            target_table_name, target_column_name = column["references"]
            target_table = by_name.get(target_table_name)
            if target_table is None:
                raise TabularError(
                    f"{table['name']}.{column['name']}: tabla referenciada inexistente {target_table_name!r}."
                )
            if target_table["kind"] != "dimension":
                raise TabularError(
                    f"{table['name']}.{column['name']}: las FK solo pueden apuntar a dimensiones."
                )
            target_columns = {item["name"] for item in target_table["columns"]}
            if target_column_name not in target_columns:
                raise TabularError(
                    f"{table['name']}.{column['name']}: columna referenciada inexistente "
                    f"{target_table_name}.{target_column_name}."
                )
            if target_table["key"] != [target_column_name]:
                raise TabularError(
                    f"{table['name']}.{column['name']}: la FK debe apuntar a la clave natural "
                    f"de una sola columna de {target_table_name}."
                )

    try:
        header_row = int(source.get("header_row", 1))
        footer_rows = int(source.get("footer_rows", 0))
    except (TypeError, ValueError) as exc:
        raise TabularError("source.header_row y source.footer_rows deben ser enteros.") from exc
    if header_row < 1:
        raise TabularError("source.header_row empieza en 1.")
    if footer_rows < 0:
        raise TabularError("source.footer_rows no puede ser negativo.")

    ordered = order_tables(tables, by_name)
    return {
        "source": {
            "path": str(source["path"]),
            "encoding": source.get("encoding"),
            "delimiter": source.get("delimiter"),
            "header_row": header_row,
            "footer_rows": footer_rows,
        },
        "locale": normalized_locale,
        "null_values": null_values,
        "tables": ordered,
        "table_by_name": {table["name"]: table for table in ordered},
    }


def order_tables(
    tables: list[dict[str, Any]], by_name: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    dimensions = [table for table in tables if table["kind"] == "dimension"]
    facts = [table for table in tables if table["kind"] == "fact"]
    dependencies: dict[str, set[str]] = {}
    for table in dimensions:
        deps: set[str] = set()
        for column in table["columns"]:
            if column["type"] == "FK":
                target = column["references"][0]
                if target == table["name"]:
                    raise TabularError(f"La dimensión {table['name']} se referencia a sí misma.")
                deps.add(target)
        dependencies[table["name"]] = deps

    ordered: list[dict[str, Any]] = []
    remaining = {table["name"]: table for table in dimensions}
    while remaining:
        ready = [
            name
            for name, table in remaining.items()
            if dependencies[name] <= {item["name"] for item in ordered}
        ]
        if not ready:
            raise TabularError(
                "Hay un ciclo de dependencias entre dimensiones: "
                + ", ".join(sorted(remaining))
            )
        for name in sorted(ready):
            ordered.append(remaining.pop(name))
    for table in facts:
        for column in table["columns"]:
            if column["type"] == "FK":
                target = column["references"][0]
                if by_name[target]["kind"] != "dimension":
                    raise TabularError(
                        f"{table['name']}.{column['name']}: una FK de hechos debe apuntar a una dimensión."
                    )
        ordered.append(table)
    return ordered


def resolve_source_columns(mapping: dict[str, Any], header: list[str]) -> None:
    for table in mapping["tables"]:
        for column in table["columns"]:
            if column["source_index"] is not None:
                index = column["source_index"] - 1
                if index < 0 or index >= len(header):
                    raise TabularError(
                        f"{table['name']}.{column['name']}: source_index "
                        f"{column['source_index']} está fuera de rango ({len(header)} columnas)."
                    )
                column["source_index0"] = index
                continue
            matches = [
                index for index, name in enumerate(header) if name == column["source"]
            ]
            if not matches:
                raise TabularError(
                    f"{table['name']}.{column['name']}: no existe la columna "
                    f"{column['source']!r} en el CSV."
                )
            if len(matches) > 1:
                raise TabularError(
                    f"{table['name']}.{column['name']}: la columna "
                    f"{column['source']!r} está duplicada; usa source_index."
                )
            column["source_index0"] = matches[0]


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------


def source_rows_for_load(
    mapping: dict[str, Any], root: Path, csv_override: Path | None = None
) -> tuple[Path, str, str, list[str], list[tuple[int, list[str]]], int]:
    source = mapping["source"]
    if csv_override is not None:
        path = csv_override if csv_override.is_absolute() else root / csv_override
        path = path.resolve()
    else:
        path = resolve_source_path(source["path"], root)
    ensure_csv(path)
    encoding = source.get("encoding") or detect_encoding(path)
    validate_encoding(path, encoding)
    delimiter = source.get("delimiter") or detect_delimiter(path, encoding)
    if len(delimiter) != 1:
        raise TabularError("El delimitador debe ser un solo carácter.")
    header_row = int(source.get("header_row", 1))
    footer_rows = int(source.get("footer_rows", 0))
    records = read_all_records(path, encoding, delimiter)
    if not records:
        raise TabularError("El CSV está vacío.")
    if header_row < 1 or header_row > len(records):
        raise TabularError(f"header_row fuera de rango: {header_row}")
    header = [cell.lstrip("\ufeff").strip() for cell in records[header_row - 1][1]]
    if not any(header):
        raise TabularError("La fila de encabezado está vacía.")
    data = records[header_row:]
    while data and all(not cell.strip() for cell in data[-1][1]):
        data.pop()
    if footer_rows:
        if footer_rows >= len(data):
            raise TabularError("footer_rows es mayor o igual que las filas de datos.")
        data = data[:-footer_rows]
    return path, encoding, delimiter, header, data, header_row


def prepare_load(
    mapping: dict[str, Any],
    root: Path,
    allow_rejects: bool = False,
    csv_override: Path | None = None,
) -> dict[str, Any]:
    path, encoding, delimiter, header, data, header_row = source_rows_for_load(
        mapping, root, csv_override=csv_override
    )
    resolve_source_columns(mapping, header)
    header_width = len(header)

    rejects: list[dict[str, Any]] = []
    rejected_rows: set[int] = set()
    structurally_bad: dict[int, str] = {}
    normalized: list[tuple[int, list[str]]] = []
    blank_rows = 0
    for source_row, raw_row in data:
        cells, problem = normalize_cells(raw_row, header_width)
        if problem:
            structurally_bad[source_row] = problem
            rejects.append(
                {
                    "fila_csv": source_row,
                    "columna": "(fila)",
                    "valor_original": " | ".join(raw_row),
                    "motivo": problem,
                    "tabla_destino": "(ninguna)",
                }
            )
            rejected_rows.add(source_row)
            continue
        if all(not cell for cell in cells):
            blank_rows += 1
            continue
        normalized.append((source_row, cells))

    source_rows = len(normalized) + len(structurally_bad)
    if source_rows == 0:
        raise TabularError("El CSV no tiene filas de datos después del encabezado.")
    rows_by_table: dict[str, list[dict[str, Any]]] = {
        table["name"]: [] for table in mapping["tables"]
    }
    duplicate_counts: dict[str, int] = {table["name"]: 0 for table in mapping["tables"]}
    next_id = {table["name"]: 1 for table in mapping["tables"]}
    lookups: dict[tuple[str, tuple[Any, ...]], int] = {}
    business_keys: dict[tuple[str, tuple[Any, ...]], tuple[Any, ...]] = {}

    def add_reject(
        source_row: int,
        table_name: str,
        column_name: str,
        raw_value: str,
        reason: str,
    ) -> None:
        rejects.append(
            {
                "fila_csv": source_row,
                "columna": column_name,
                "valor_original": str(raw_value),
                "motivo": reason,
                "tabla_destino": table_name,
            }
        )
        rejected_rows.add(source_row)

    for table in mapping["tables"]:
        table_name = table["name"]
        key_columns = table["key"]
        for source_row, cells in normalized:
            if source_row in rejected_rows:
                continue
            values: dict[str, Any] = {}
            row_errors: list[tuple[str, str, str]] = []
            for column in table["columns"]:
                raw = cells[column["source_index0"]]
                try:
                    if column["type"] == "FK":
                        if is_null(raw, mapping["null_values"]):
                            if column["required"]:
                                raise ConversionError("valor obligatorio vacío")
                            value = None
                        else:
                            target_table_name, target_column_name = column["references"]
                            target_table = mapping["table_by_name"][target_table_name]
                            target_column = next(
                                item
                                for item in target_table["columns"]
                                if item["name"] == target_column_name
                            )
                            key_value = convert_value(
                                raw,
                                {**target_column, "required": True},
                                mapping["locale"],
                                mapping["null_values"],
                            )
                            lookup_key = (target_table_name, (key_value,))
                            value = lookups.get(lookup_key)
                            if value is None:
                                raise ConversionError(
                                    f"no existe {target_table_name}.{target_column_name} = {key_value!r}"
                                )
                    else:
                        value = convert_value(
                            raw, column, mapping["locale"], mapping["null_values"]
                        )
                    values[column["name"]] = value
                except (ConversionError, TabularError) as exc:
                    row_errors.append((column["name"], raw, str(exc)))

            if row_errors:
                for column_name, raw, reason in row_errors:
                    add_reject(source_row, table_name, column_name, raw, reason)
                continue

            if key_columns:
                key_values = tuple(values.get(name) for name in key_columns)
                if any(value is None for value in key_values):
                    add_reject(
                        source_row,
                        table_name,
                        ", ".join(key_columns),
                        "",
                        "clave incompleta",
                    )
                    continue
                business_key = (table_name, key_values)
                comparable = tuple(
                    (name, values.get(name))
                    for name in values
                    if name not in key_columns
                )
                if table["kind"] == "dimension":
                    if business_key in business_keys:
                        if business_keys[business_key] == comparable:
                            duplicate_counts[table_name] += 1
                            continue
                        add_reject(
                            source_row,
                            table_name,
                            ", ".join(key_columns),
                            " | ".join(str(value) for value in key_values),
                            "clave duplicada con valores distintos",
                        )
                        continue
                    business_keys[business_key] = comparable
                else:
                    if business_key in business_keys:
                        if business_keys[business_key] == comparable:
                            duplicate_counts[table_name] += 1
                            continue
                        add_reject(
                            source_row,
                            table_name,
                            ", ".join(key_columns),
                            " | ".join(str(value) for value in key_values),
                            "clave de negocio duplicada con valores distintos",
                        )
                        continue
                    business_keys[business_key] = comparable

            values["id"] = next_id[table_name]
            next_id[table_name] += 1
            if table["kind"] == "fact":
                values["_source_row"] = source_row
            rows_by_table[table_name].append(values)
            if table["kind"] == "dimension":
                key_values = tuple(values.get(name) for name in key_columns)
                lookups[(table_name, key_values)] = values["id"]

    # Control de totales por tabla, calculado desde las filas preparadas.
    sums: dict[str, dict[str, Any]] = {}
    for table in mapping["tables"]:
        table_name = table["name"]
        rows = rows_by_table[table_name]
        table_sums: dict[str, Any] = {}
        if rows:
            frame = pd.DataFrame(rows)
            for column in table["columns"]:
                if column["name"] not in frame.columns:
                    continue
                if column["type"] in {"INTEGER", "MONEY"}:
                    table_sums[column["name"]] = int(frame[column["name"]].fillna(0).sum())
                elif column["type"] == "REAL":
                    table_sums[column["name"]] = round(
                        float(frame[column["name"]].fillna(0).sum()), 6
                    )
        sums[table_name] = table_sums

    accepted_rows = source_rows - len(rejected_rows)
    return {
        "source_path": path,
        "encoding": encoding,
        "delimiter": delimiter,
        "header": header,
        "header_row": header_row,
        "source_rows": source_rows,
        "blank_rows": blank_rows,
        "rejected_rows": len(rejected_rows),
        "accepted_rows": max(accepted_rows, 0),
        "rejects": rejects,
        "rows_by_table": rows_by_table,
        "duplicate_counts": duplicate_counts,
        "sums": sums,
        "blocking": bool(rejects) and not allow_rejects,
        "mapping": mapping,
    }


def build_schema(mapping: dict[str, Any]) -> str:
    lines: list[str] = [
        f"-- Esquema generado por tabular {VERSION}",
        "-- No editar a mano: usa `schema --write` o `mutate`.",
        "",
        "PRAGMA foreign_keys = ON;",
        "",
    ]
    for table in mapping["tables"]:
        columns = ['  "id" INTEGER PRIMARY KEY']
        for column in table["columns"]:
            sql_type = {
                "TEXT": "TEXT",
                "INTEGER": "INTEGER",
                "REAL": "REAL",
                "MONEY": "INTEGER",
                "DATE": "TEXT",
                "BOOLEAN": "INTEGER",
                "FK": "INTEGER",
            }[column["type"]]
            definition = f'  {quote_ident(column["name"])} {sql_type}'
            if column["required"]:
                definition += " NOT NULL"
            if column["type"] == "FK":
                target_table, _ = column["references"]
                definition += f' REFERENCES {quote_ident(target_table)}("id")'
            columns.append(definition)
        if table["kind"] == "fact":
            columns.append('  "_source_row" INTEGER NOT NULL')
        if table["key"]:
            columns.append(
                "  UNIQUE ("
                + ", ".join(quote_ident(name) for name in table["key"])
                + ")"
            )
        lines.append(f"CREATE TABLE {quote_ident(table['name'])} (")
        lines.append(",\n".join(columns))
        lines.append(") STRICT;")
        lines.append("")
    for table in mapping["tables"]:
        for column in table["columns"]:
            if column["type"] == "FK":
                index_name = f"idx_{table['name']}_{column['name']}"
                lines.append(
                    f"CREATE INDEX {quote_ident(index_name)} "
                    f"ON {quote_ident(table['name'])} ({quote_ident(column['name'])});"
                )
    lines.extend(
        [
            "",
            'CREATE TABLE "_imports" (',
            '  "id" INTEGER PRIMARY KEY,',
            '  "kind" TEXT NOT NULL,',
            '  "source_path" TEXT NOT NULL,',
            '  "archived_path" TEXT NOT NULL,',
            '  "source_sha256" TEXT NOT NULL,',
            '  "mapping_sha256" TEXT NOT NULL,',
            '  "imported_at" TEXT NOT NULL,',
            '  "source_rows" INTEGER NOT NULL,',
            '  "rejected_rows" INTEGER NOT NULL,',
            '  "details_json" TEXT NOT NULL,',
            '  "tool_version" TEXT NOT NULL',
            ") STRICT;",
            "",
            'CREATE TABLE "_changes" (',
            '  "id" INTEGER PRIMARY KEY,',
            '  "executed_at" TEXT NOT NULL,',
            '  "kind" TEXT NOT NULL,',
            '  "sql_text" TEXT NOT NULL,',
            '  "summary" TEXT NOT NULL,',
            '  "affected_rows" INTEGER NOT NULL,',
            '  "reversible" INTEGER NOT NULL,',
            '  "undo_path" TEXT,',
            '  "backup_path" TEXT,',
            '  "schema_before" TEXT,',
            '  "schema_after" TEXT,',
            '  "mapping_sha256" TEXT,',
            '  "tool_version" TEXT NOT NULL',
            ") STRICT;",
        ]
    )
    return "\n".join(lines) + "\n"


def apply_schema(conn: sqlite3.Connection, ddl: str) -> None:
    conn.executescript(ddl)


def insert_rows(
    conn: sqlite3.Connection,
    mapping: dict[str, Any],
    prepared: dict[str, Any],
) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    for table in mapping["tables"]:
        table_name = table["name"]
        rows = prepared["rows_by_table"][table_name]
        if not rows:
            continue
        column_names = ["id"] + [column["name"] for column in table["columns"]]
        if table["kind"] == "fact":
            column_names.append("_source_row")
        placeholders = ", ".join("?" for _ in column_names)
        sql = (
            f"INSERT INTO {quote_ident(table_name)} "
            f"({', '.join(quote_ident(name) for name in column_names)}) "
            f"VALUES ({placeholders})"
        )
        values = [[row.get(name) for name in column_names] for row in rows]
        conn.executemany(sql, values)


def record_init(
    conn: sqlite3.Connection,
    prepared: dict[str, Any],
    root: Path,
    source_hash: str,
    mapping_hash: str,
    archived_path: Path,
    backup_path: Path | None,
) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    counts = {
        table["name"]: len(prepared["rows_by_table"][table["name"]])
        for table in prepared["mapping"]["tables"]
    }
    source_text = relative_to_root(prepared["source_path"], root)
    archived_text = relative_to_root(archived_path, root)
    backup_text = relative_to_root(backup_path, root) if backup_path else None
    conn.execute(
        'INSERT INTO "_imports" '
        '("kind", "source_path", "archived_path", "source_sha256", '
        '"mapping_sha256", "imported_at", "source_rows", "rejected_rows", '
        '"details_json", "tool_version") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "init",
            source_text,
            archived_text,
            source_hash,
            mapping_hash,
            now,
            prepared["source_rows"],
            prepared["rejected_rows"],
            json.dumps(counts, ensure_ascii=False, sort_keys=True),
            VERSION,
        ),
    )
    conn.execute(
        'INSERT INTO "_changes" '
        '("executed_at", "kind", "sql_text", "summary", "affected_rows", '
        '"reversible", "undo_path", "backup_path", "schema_before", '
        '"schema_after", "mapping_sha256", "tool_version") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            "init",
            f"init {prepared['source_path'].name}",
            f"Importación inicial de {prepared['source_path'].name}",
            0,
            0,
            None,
            backup_text,
            None,
            None,
            mapping_hash,
            VERSION,
        ),
    )
    conn.commit()


def apply_views(conn: sqlite3.Connection, root: Path) -> list[str]:
    view_dir = root / "sql" / "views"
    applied: list[str] = []
    # Las vistas son derivadas: se recrean en cada carga para evitar definiciones viejas.
    for view in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'view'"
    ).fetchall():
        conn.execute(f"DROP VIEW IF EXISTS {quote_ident(view[0])}")
    if not view_dir.exists():
        conn.commit()
        return applied
    for path in sorted(view_dir.glob("*.sql")):
        sql = path.read_text(encoding="utf-8-sig")
        try:
            conn.executescript(sql)
        except sqlite3.Error as exc:
            raise TabularError(f"Error al aplicar la vista {path.name}: {exc}") from exc
        applied.append(path.name)
    conn.commit()
    return applied


def verify_source(
    conn: sqlite3.Connection,
    mapping: dict[str, Any],
    prepared: dict[str, Any],
    allow_rejects: bool = False,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for table in mapping["tables"]:
        table_name = table["name"]
        expected_rows = prepared["rows_by_table"][table_name]
        actual = conn.execute(
            f"SELECT COUNT(*) FROM {quote_ident(table_name)}"
        ).fetchone()[0]
        checks.append(
            {
                "name": f"filas:{table_name}",
                "passed": actual == len(expected_rows),
                "detail": f"esperadas {len(expected_rows)}, encontradas {actual}",
            }
        )
        for column_name, expected_sum in prepared["sums"][table_name].items():
            actual_sum = conn.execute(
                f"SELECT SUM({quote_ident(column_name)}) FROM {quote_ident(table_name)}"
            ).fetchone()[0]
            actual_sum = 0 if actual_sum is None else actual_sum
            if isinstance(expected_sum, float):
                passed = abs(float(actual_sum) - expected_sum) <= 0.000001
                detail = (
                    f"esperada {expected_sum:.6g}, "
                    f"encontrada {float(actual_sum):.6g}"
                )
            else:
                passed = int(actual_sum) == int(expected_sum)
                detail = f"esperada {expected_sum}, encontrada {actual_sum}"
            checks.append(
                {
                    "name": f"suma:{table_name}.{column_name}",
                    "passed": passed,
                    "detail": detail,
                }
            )

    if prepared["rejected_rows"] and not allow_rejects:
        checks.append(
            {
                "name": "filas_rechazadas",
                "passed": False,
                "detail": f"{prepared['rejected_rows']} filas rechazadas",
            }
        )
    return checks


def verify_integrity(
    conn: sqlite3.Connection, require_metadata: bool = True
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    integrity_ok = bool(integrity and integrity[0] == "ok")
    checks.append(
        {
            "name": "integrity_check",
            "passed": integrity_ok,
            "detail": "ok" if integrity_ok else str(integrity[0] if integrity else "sin resultado"),
        }
    )

    fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    checks.append(
        {
            "name": "foreign_key_check",
            "passed": not fk_errors,
            "detail": "0 huérfanas" if not fk_errors else f"{len(fk_errors)} huérfanas",
        }
    )

    if require_metadata:
        for table_name in ("_imports", "_changes"):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            checks.append(
                {
                    "name": f"meta:tabla:{table_name}",
                    "passed": bool(exists),
                    "detail": "presente" if exists else "faltante",
                }
            )

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_imports'"
        ).fetchone():
            count = conn.execute('SELECT COUNT(*) FROM "_imports"').fetchone()[0]
            checks.append(
                {
                    "name": "meta:registros:_imports",
                    "passed": count >= 1,
                    "detail": f"{count} registros",
                }
            )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_changes'"
        ).fetchone():
            count = conn.execute('SELECT COUNT(*) FROM "_changes"').fetchone()[0]
            checks.append(
                {
                    "name": "meta:registros:_changes",
                    "passed": count >= 1,
                    "detail": f"{count} registros",
                }
            )

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for table in tables:
        name = table["name"]
        count = conn.execute(
            f"SELECT COUNT(*) FROM {quote_ident(name)}"
        ).fetchone()[0]
        checks.append(
            {"name": f"tabla:{name}", "passed": True, "detail": f"{count} filas"}
        )
    return checks


def rejects_csv_text(rejects: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["fila_csv", "columna", "valor_original", "motivo", "tabla_destino"],
        lineterminator="\n",
    )
    writer.writeheader()
    for reject in sorted(
        rejects, key=lambda item: (item["fila_csv"], item["tabla_destino"], item["columna"])
    ):
        writer.writerow(reject)
    return output.getvalue()


def write_rejects(path: Path, rejects: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rejects:
        if path.exists():
            path.unlink()
        return
    path.write_text(rejects_csv_text(rejects), encoding="utf-8")


def summary_markdown(
    prepared: dict[str, Any],
    checks: list[dict[str, Any]],
    mode: str,
    db_path: Path | None,
    archive_path: Path | None = None,
    backup_path: Path | None = None,
) -> str:
    lines = [
        "# Importación CSV -> SQLite",
        "",
        f"- Modo: **{mode}**",
        f"- Fuente: `{prepared['source_path']}`",
        f"- Encoding: `{prepared['encoding']}`",
        f"- Delimitador: `{prepared['delimiter']}`",
        f"- Encabezado: fila {prepared['header_row']}",
        f"- Filas fuente: {prepared['source_rows']}",
        f"- Filas aceptadas: {prepared['accepted_rows']}",
        f"- Filas rechazadas: {prepared['rejected_rows']}",
    ]
    if archive_path is not None:
        lines.append(f"- CSV archivado: `{archive_path}`")
    if backup_path is not None:
        lines.append(f"- Backup previo: `{backup_path}`")
    if db_path is not None:
        lines.append(f"- Base: `{db_path}`")
    lines.extend(["", "## Tablas", ""])
    table_rows = []
    for table in prepared["mapping"]["tables"]:
        name = table["name"]
        table_rows.append(
            [
                name,
                table["kind"],
                len(prepared["rows_by_table"][name]),
                prepared["duplicate_counts"][name],
            ]
        )
    lines.append(
        format_table(["Tabla", "Tipo", "Filas", "Duplicados omitidos"], table_rows)
    )
    if prepared["sums"]:
        sum_rows = []
        for table_name, sums in prepared["sums"].items():
            for column_name, value in sums.items():
                sum_rows.append([table_name, column_name, value])
        if sum_rows:
            lines.extend(["", "## Totales de control", ""])
            lines.append(format_table(["Tabla", "Columna", "Suma"], sum_rows))
    if prepared["rejects"]:
        lines.extend(["", "## Rechazos", ""])
        lines.append(
            format_table(
                ["Fila CSV", "Columna", "Valor", "Motivo", "Tabla"],
                [
                    [
                        reject["fila_csv"],
                        reject["columna"],
                        reject["valor_original"][:60],
                        reject["motivo"],
                        reject["tabla_destino"],
                    ]
                    for reject in prepared["rejects"][:20]
                ],
                max_width=50,
            )
        )
        if len(prepared["rejects"]) > 20:
            lines.append(f"\n... y {len(prepared['rejects']) - 20} rechazos más.")
        lines.append("\nDetalle completo en `reports/rechazos.csv`.")
    if checks:
        lines.extend(["", "## Verificación", ""])
        lines.append(
            format_table(
                ["Chequeo", "Resultado", "Detalle"],
                [
                    [check["name"], "OK" if check["passed"] else "FALLA", check["detail"]]
                    for check in checks
                ],
            )
        )
    if prepared["blocking"]:
        lines.extend(
            [
                "",
                (
                    "**Hay filas rechazadas.** Corrige el mapping o revisa "
                    "`reports/rechazos.csv`. Para cargar solo las filas válidas, "
                    "usa `--allow-rejects` de forma explícita."
                ),
            ]
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def cmd_profile(args: argparse.Namespace) -> int:
    _profile, summary = build_profile(args)
    print(summary)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    mapping_path = Path(args.mapping).expanduser().resolve()
    root = resolve_root(mapping_path)
    mapping = load_mapping(mapping_path)
    mapping_hash = sha256_file(mapping_path)
    db_path = resolve_db(args.db, root) if args.db else default_db_path(root)
    ensure_database_absent(db_path, args.force)
    csv_override = Path(args.csv).expanduser() if args.csv else None
    prepared = prepare_load(
        mapping, root, allow_rejects=args.allow_rejects, csv_override=csv_override
    )

    reports_dir = root / "reports"
    reject_path = reports_dir / "rechazos.csv"
    if not args.no_report:
        write_rejects(reject_path, prepared["rejects"])

    if prepared["blocking"]:
        print(summary_markdown(prepared, [], "init (abortado)", db_path))
        raise TabularError(
            "La importación se abortó porque hay filas rechazadas. "
            "Revisa reports/rechazos.csv o usa --allow-rejects."
        )
    if prepared["accepted_rows"] == 0:
        raise TabularError(
            "Todas las filas fueron rechazadas; no se creó una base vacía. "
            "Revisa reports/rechazos.csv y corrige el mapping."
        )

    source_hash = sha256_file(prepared["source_path"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = db_path.with_name(db_path.name + ".tmp")
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(temp_path) + suffix)
        if candidate.exists():
            candidate.unlink()

    archive_path: Path | None = None
    backup_path: Path | None = None
    try:
        with closing(connect_readwrite(temp_path)) as conn:
            apply_schema(conn, build_schema(mapping))
            insert_rows(conn, mapping, prepared)
            apply_views(conn, root)

            source_checks = verify_source(
                conn, mapping, prepared, allow_rejects=args.allow_rejects
            )
            pre_checks = verify_integrity(conn, require_metadata=False)
            failed = [
                check for check in source_checks + pre_checks if not check["passed"]
            ]
            if failed:
                raise TabularError(
                    "La verificación previa falló: "
                    + "; ".join(f"{item['name']} ({item['detail']})" for item in failed)
                )

            if args.force and db_path.exists():
                backup_path = backup_database(db_path, root, "pre-init-force")
            archive_path = archive_source(prepared["source_path"], root, args.archive_dir)
            if sha256_file(archive_path) != source_hash:
                raise TabularError(
                    "El CSV archivado no coincide con el original; no se registró la importación."
                )
            record_init(
                conn,
                prepared,
                root,
                source_hash,
                mapping_hash,
                archive_path,
                backup_path,
            )
            post_checks = verify_integrity(conn, require_metadata=True)
            failed = [check for check in post_checks if not check["passed"]]
            if failed:
                raise TabularError(
                    "La verificación posterior a la importación falló: "
                    + "; ".join(f"{item['name']} ({item['detail']})" for item in failed)
                )

        replace_with_retry(temp_path, db_path)
        for suffix in ("-wal", "-shm"):
            for base in (temp_path, db_path):
                candidate = Path(str(base) + suffix)
                if candidate.exists():
                    candidate.unlink()

        with closing(connect_readonly(db_path)) as conn:
            write_schema_sql(conn, root)

        if args.delete_source and prepared["source_path"].resolve() != archive_path.resolve():
            try:
                prepared["source_path"].unlink()
            except OSError as exc:
                eprint(f"Aviso: no se pudo borrar el CSV original: {exc}")
    except Exception:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(temp_path) + suffix)
            if candidate.exists():
                try:
                    candidate.unlink()
                except OSError:
                    pass
        raise

    checks = source_checks + post_checks
    print(
        summary_markdown(
            prepared,
            checks,
            "init",
            db_path,
            archive_path=archive_path,
            backup_path=backup_path,
        )
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    db_path = resolve_db(args.db)
    with closing(connect_readonly(db_path)) as conn:
        checks = verify_integrity(conn)

    failed = [check for check in checks if not check["passed"]]
    if args.format == "json":
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        print("# Verificación de integridad")
        print()
        print(
            format_table(
                ["Chequeo", "Resultado", "Detalle"],
                [
                    [check["name"], "OK" if check["passed"] else "FALLA", check["detail"]]
                    for check in checks
                ],
            )
        )
        print()
        print(
            f"Resultado: {'OK' if not failed else 'FALLA'} "
            f"({len(checks) - len(failed)}/{len(checks)} chequeos)"
        )
    return 0 if not failed else 2


def cmd_query(args: argparse.Namespace) -> int:
    db_path = resolve_db(args.db)
    sql = args.sql
    if args.file:
        file_path = Path(args.file).expanduser()
        if not file_path.exists():
            raise TabularError(f"No existe el archivo SQL: {file_path}")
        sql = file_path.read_text(encoding="utf-8-sig")
    if not sql or not sql.strip():
        raise TabularError("Debes indicar una consulta SQL o usar --file.")

    clean_sql = re.sub(r"^\s*(--[^\n]*\n|/\*.*?\*/)*", "", sql, flags=re.DOTALL).strip()
    if not re.match(r"^(select|with|explain)\b", clean_sql, re.IGNORECASE):
        raise TabularError("Solo se permiten consultas SELECT, WITH o EXPLAIN.")
    if args.limit < 0:
        raise TabularError("--limit no puede ser negativo.")

    with closing(connect_readonly(db_path)) as conn:
        deadline = time.monotonic() + max(args.timeout, 1)

        def progress() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(progress, 10000)
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchall() if args.limit == 0 else cursor.fetchmany(args.limit + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise TabularError("La consulta superó el tiempo límite.") from exc
            raise TabularError(f"Error SQL: {exc}") from exc
        except sqlite3.Error as exc:
            raise TabularError(f"Error SQL: {exc}") from exc

        truncated = args.limit != 0 and len(rows) > args.limit
        if truncated:
            rows = rows[: args.limit]
        headers = dedupe_headers(
            [description[0] for description in (cursor.description or [])]
        )

    output_format = args.format
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if output_format == "table":
            output_format = "csv"
        if output_format == "json":
            out_path.write_text(
                json.dumps([dict(zip(headers, row)) for row in rows], ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
        elif output_format == "csv":
            with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                writer.writerows(rows)
        else:
            raise TabularError("--out solo admite formatos csv o json.")
        if truncated:
            eprint(f"[{len(rows)} filas escritas; usa --limit 0 para exportar todo]")
        print(f"Resultado escrito en {out_path}")
        return 0

    if output_format == "json":
        print(json.dumps([dict(zip(headers, row)) for row in rows], ensure_ascii=False, indent=2))
    elif output_format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        writer.writerows(rows)
    else:
        print(format_table(headers, rows))
    if truncated:
        eprint(f"[{len(rows)} filas mostradas; usa --limit 0 o un filtro para ver más]")
    elif args.limit and len(rows) == args.limit:
        eprint(f"[{len(rows)} filas; puede haber más]")
    return 0


def ensure_returning(statement: str) -> str:
    clean = statement.strip().rstrip(";").rstrip()
    if re.search(r"\bRETURNING\b", clean, flags=re.IGNORECASE):
        return statement
    return clean + " RETURNING *;"


def execute_mutation_statements(
    conn: sqlite3.Connection,
    statements: list[str],
    kinds: list[str],
    capture_sql: str | None,
) -> dict[str, Any]:
    schema_change = any(kind in {"schema", "schema_destructive"} for kind in kinds)
    before_schema = dump_schema_sql(conn) if schema_change else None
    captures: list[dict[str, Any] | None] = []
    rowcounts: list[int] = []

    for statement, kind in zip(statements, kinds):
        keyword = leading_keyword(statement)
        table = table_of_statement(statement)
        capture: dict[str, Any] | None = None

        if kind in {"data", "data_full", "data_delete"} and keyword == "UPDATE":
            old_rows: list[dict[str, Any]] | None = None
            if capture_sql:
                old_rows = fetch_rows(conn, capture_sql)
            else:
                auto_capture = build_update_capture_sql(statement)
                if auto_capture:
                    try:
                        old_rows = fetch_rows(conn, auto_capture)
                    except sqlite3.Error:
                        old_rows = None
            cursor = conn.execute(statement)
            if re.search(r"\bRETURNING\b", statement, flags=re.IGNORECASE):
                count = len(cursor.fetchall())
            else:
                count = max(cursor.rowcount, 0)
            if old_rows is not None and table:
                capture = {"action": "update", "table": table, "rows": old_rows}
        elif kind in {"data", "data_full", "data_delete"} and keyword == "DELETE":
            rows: list[dict[str, Any]] | None = None
            try:
                cursor = conn.execute(ensure_returning(statement))
                rows = [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                cursor = conn.execute(statement)
                rows = None
            count = len(rows) if rows is not None else max(cursor.rowcount, 0)
            if rows is not None and table:
                capture = {"action": "delete", "table": table, "rows": rows}
        elif kind in {"data", "data_full"} and keyword in {"INSERT", "REPLACE"}:
            rows = None
            upper_statement = statement.upper()
            if (
                keyword == "INSERT"
                and "OR REPLACE" not in upper_statement
                and "ON CONFLICT" not in upper_statement
            ):
                try:
                    cursor = conn.execute(ensure_returning(statement))
                    rows = [dict(row) for row in cursor.fetchall()]
                except sqlite3.OperationalError:
                    cursor = conn.execute(statement)
                    rows = None
            else:
                cursor = conn.execute(statement)
            count = len(rows) if rows is not None else max(cursor.rowcount, 0)
            if rows is not None and table:
                capture = {"action": "insert", "table": table, "rows": rows}
        else:
            cursor = conn.execute(statement)
            count = max(cursor.rowcount, 0)

        captures.append(capture)
        rowcounts.append(count)

    after_schema = dump_schema_sql(conn) if schema_change else None
    return {
        "captures": captures,
        "rowcounts": rowcounts,
        "before_schema": before_schema,
        "after_schema": after_schema,
    }


def compute_undo(
    statements: list[str], kinds: list[str], result: dict[str, Any]
) -> tuple[bool, str | None]:
    if len(statements) != 1 or kinds[0] not in {"data", "data_full", "data_delete"}:
        return False, None
    capture = result["captures"][0] if result["captures"] else None
    if not capture:
        return False, None
    if capture["action"] == "update" and len(capture["rows"]) != result["rowcounts"][0]:
        return False, None
    undo = generate_undo_sql(capture["action"], capture["table"], capture["rows"])
    if undo is None:
        return False, None
    return True, undo


def unique_change_base(directory: Path, base: str) -> str:
    candidate = base
    counter = 2
    while any(
        (directory / f"{candidate}{suffix}").exists()
        for suffix in (".sql", "_undo.sql", ".md")
    ):
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def mutation_summary(
    mode: str,
    sql_text: str,
    kinds: list[str],
    result: dict[str, Any],
    reversible: bool,
    undo_path: Path | None,
    backup_path: Path | None,
    warnings: list[str],
) -> str:
    affected = sum(result["rowcounts"])
    lines = [
        "# Mutación SQL",
        "",
        f"- Modo: **{mode}**",
        f"- Tipo: `{', '.join(kinds)}`",
        f"- Filas afectadas: {affected}",
        f"- Reversible: {'sí' if reversible else 'no'}",
    ]
    if undo_path is not None:
        lines.append(f"- Undo: `{undo_path}`")
    if backup_path is not None:
        lines.append(f"- Backup: `{backup_path}`")
    if warnings:
        lines.extend(["", "## Advertencias", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(["", "## SQL", "", "```sql", sql_text.strip(), "```"])
    return "\n".join(lines)


def write_change_report(
    path: Path,
    mode: str,
    sql_text: str,
    kinds: list[str],
    result: dict[str, Any],
    reversible: bool,
    undo_path: Path | None,
    backup_path: Path | None,
    warnings: list[str],
) -> None:
    path.write_text(
        mutation_summary(
            mode, sql_text, kinds, result, reversible, undo_path, backup_path, warnings
        ),
        encoding="utf-8",
    )


def cmd_mutate(args: argparse.Namespace) -> int:
    db_path = resolve_db(args.db)
    root = Path.cwd().resolve()
    if args.sql and args.file:
        raise TabularError("Usa --sql o --file, no ambos.")
    if not args.sql and not args.file:
        raise TabularError("Debes indicar --sql o --file.")
    if args.file:
        file_path = Path(args.file).expanduser()
        if not file_path.exists():
            raise TabularError(f"No existe el archivo SQL: {file_path}")
        sql_text = file_path.read_text(encoding="utf-8-sig")
    else:
        sql_text = args.sql
    if args.keep_history < 1 or args.keep_backups < 1:
        raise TabularError("--keep-history y --keep-backups deben ser mayores que cero.")

    statements = split_sql_statements(sql_text)
    kinds = [classify_statement(statement) for statement in statements]
    if args.capture_sql and (
        len(statements) != 1
        or kinds[0] not in {"data", "data_full"}
        or leading_keyword(statements[0]) != "UPDATE"
    ):
        raise TabularError("--capture-sql solo aplica a una única sentencia UPDATE.")

    destructive = any(kind == "schema_destructive" for kind in kinds)
    schema_change = any(kind in {"schema", "schema_destructive"} for kind in kinds)
    delete_change = any(
        kind == "data_delete"
        or (
            leading_keyword(statement) == "WITH"
            and "DELETE" in statement.upper()
        )
        for statement, kind in zip(statements, kinds)
    )
    full_table = any(kind == "data_full" for kind in kinds)
    complex_change = any(kind == "complex" for kind in kinds)

    with closing(connect_readwrite(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = execute_mutation_statements(
                conn, statements, kinds, args.capture_sql
            )
        finally:
            conn.rollback()

    reversible, undo_sql = compute_undo(statements, kinds, result)
    warnings: list[str] = []
    if delete_change:
        warnings.append("Es un DELETE. El agente debe pedir confirmación explícita.")
    if full_table:
        warnings.append("UPDATE/DELETE sin WHERE: afecta toda la tabla.")
    if destructive:
        warnings.append("DDL destructivo: requiere --allow-destructive y backup.")
    elif schema_change:
        warnings.append("DDL: se hará backup y snapshot del esquema al confirmar.")
    if complex_change:
        warnings.append("Sentencia compleja: no hay undo automático.")
    if not reversible and not schema_change:
        warnings.append("No hay undo automático; usa --backup o --allow-irreversible.")

    if not args.confirm:
        undo_preview = root / "reports" / "changes" / "<se generará al confirmar>_undo.sql" if reversible else None
        print(
            mutation_summary(
                "dry-run (rollback)",
                sql_text,
                kinds,
                result,
                reversible,
                undo_preview,
                None,
                warnings,
            )
        )
        print("\nNo se aplicó nada. Revisa las advertencias y confirma con --confirm.")
        return 0

    if delete_change and not args.confirm_delete:
        raise TabularError(
            "DELETE requiere confirmación adicional: usa --confirm-delete además de --confirm."
        )
    if full_table and not args.allow_full_table:
        raise TabularError(
            "UPDATE/DELETE sin WHERE requiere --allow-full-table."
        )
    if destructive and not args.allow_destructive:
        raise TabularError(
            "DDL destructivo (DROP/RENAME) requiere --allow-destructive."
        )
    need_backup = args.backup or schema_change or destructive
    if not reversible and not need_backup and not args.allow_irreversible:
        raise TabularError(
            "La operación no es reversible y no hay backup. "
            "Usa --backup o --allow-irreversible de forma explícita."
        )

    backup_path: Path | None = None
    if need_backup:
        backup_path = backup_database(
            db_path, root, "pre-schema" if schema_change else "pre-mutate",
            keep=args.keep_backups,
        )

    changes_dir = root / "reports" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    base = unique_change_base(
        changes_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{kinds[0]}"
    )
    sql_path = changes_dir / f"{base}.sql"
    undo_path = changes_dir / f"{base}_undo.sql" if reversible and undo_sql else None
    report_path = changes_dir / f"{base}.md"
    schema_before_path = None
    schema_after_path = None
    if schema_change:
        history_dir = root / "schema" / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        schema_before_path = history_dir / f"schema_{base}_before.sql"
        schema_after_path = history_dir / f"schema_{base}_after.sql"

    try:
        sql_path.write_text(sql_text.rstrip() + "\n", encoding="utf-8")
        if undo_path is not None and undo_sql is not None:
            undo_path.write_text(
                "-- Undo generado por tabular. Revísalo antes de aplicarlo.\n"
                + undo_sql,
                encoding="utf-8",
            )
        if schema_before_path is not None and result["before_schema"] is not None:
            schema_before_path.write_text(result["before_schema"], encoding="utf-8")
        if schema_after_path is not None and result["after_schema"] is not None:
            schema_after_path.write_text(result["after_schema"], encoding="utf-8")

        with closing(connect_readwrite(db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = execute_mutation_statements(
                    conn, statements, kinds, args.capture_sql
                )
                reversible, undo_sql = compute_undo(statements, kinds, result)
                if not reversible and not need_backup and not args.allow_irreversible:
                    raise TabularError(
                        "La operación no es reversible y no hay backup. "
                        "Usa --backup o --allow-irreversible de forma explícita."
                    )
                if reversible and undo_path is None:
                    undo_path = changes_dir / f"{base}_undo.sql"
                if not reversible and undo_path is not None:
                    if undo_path.exists():
                        try:
                            undo_path.unlink()
                        except OSError:
                            pass
                    undo_path = None
                if undo_sql is None and not schema_change and "No hay undo automático" not in " ".join(warnings):
                    warnings.append("No hay undo automático para esta operación.")
                if schema_change:
                    warnings.append(
                        "La estructura cambió. Ejecuta `schema --check` y actualiza "
                        "mapping.json si es necesario."
                    )
                if undo_path is not None and undo_sql is not None:
                    undo_path.write_text(
                        "-- Undo generado por tabular. Revísalo antes de aplicarlo.\n"
                        + undo_sql,
                        encoding="utf-8",
                    )
                if schema_before_path is not None and result["before_schema"] is not None:
                    schema_before_path.write_text(result["before_schema"], encoding="utf-8")
                if schema_after_path is not None and result["after_schema"] is not None:
                    schema_after_path.write_text(result["after_schema"], encoding="utf-8")

                mapping_path = root / "schema" / "mapping.json"
                mapping_hash = sha256_file(mapping_path) if mapping_path.exists() else None
                summary = (
                    history_summary(statements[0], kinds[0])
                    if len(statements) == 1
                    else f"{len(statements)} sentencias: " + ", ".join(kinds)
                )
                cursor = conn.execute(
                    'INSERT INTO "_changes" '
                    '("executed_at", "kind", "sql_text", "summary", "affected_rows", '
                    '"reversible", "undo_path", "backup_path", "schema_before", '
                    '"schema_after", "mapping_sha256", "tool_version") '
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        kinds[0] if len(set(kinds)) == 1 else "mixed",
                        sql_text,
                        summary,
                        sum(result["rowcounts"]),
                        1 if reversible else 0,
                        relative_to_root(undo_path, root) if undo_path else None,
                        relative_to_root(backup_path, root) if backup_path else None,
                        relative_to_root(schema_before_path, root) if schema_before_path else None,
                        relative_to_root(schema_after_path, root) if schema_after_path else None,
                        mapping_hash,
                        VERSION,
                    ),
                )
                change_id = cursor.lastrowid
                write_change_report(
                    report_path,
                    "confirmado",
                    sql_text,
                    kinds,
                    result,
                    reversible,
                    undo_path,
                    backup_path,
                    warnings,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        if schema_change:
            with closing(connect_readonly(db_path)) as conn:
                write_schema_sql(conn, root)
        with closing(connect_readwrite(db_path)) as conn:
            prune_undo_history(conn, root, args.keep_history)

        print(
            mutation_summary(
                "confirmado",
                sql_text,
                kinds,
                result,
                reversible,
                undo_path,
                backup_path,
                warnings,
            )
        )
        print(f"\nCambio registrado en `_changes` id={change_id}.")
    except Exception:
        for path in (sql_path, undo_path, report_path, schema_before_path, schema_after_path):
            if path is not None and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        raise
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    if args.limit < 1:
        raise TabularError("--limit debe ser mayor que cero.")
    db_path = resolve_db(args.db)
    with closing(connect_readonly(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_changes'"
        ).fetchone()
        if not exists:
            raise TabularError("No existe la tabla _changes en esta base.")
        if args.show:
            row = conn.execute(
                'SELECT * FROM "_changes" WHERE id = ?', (args.show,)
            ).fetchone()
            if not row:
                raise TabularError(f"No existe el cambio {args.show}.")
            if args.format == "json":
                print(json.dumps(dict(row), ensure_ascii=False, indent=2))
                return 0
            print("# Cambio " + str(row["id"]))
            print()
            print(f"- Fecha: {row['executed_at']}")
            print(f"- Tipo: `{row['kind']}`")
            print(f"- Filas afectadas: {row['affected_rows']}")
            print(f"- Reversible: {'sí' if row['reversible'] else 'no'}")
            if row["undo_path"]:
                undo_path = Path(row["undo_path"])
                if not undo_path.is_absolute():
                    undo_path = Path.cwd() / undo_path
                print(f"- Undo: `{row['undo_path']}` ({'existe' if undo_path.exists() else 'no disponible'})")
            if row["backup_path"]:
                print(f"- Backup: `{row['backup_path']}`")
            if row["schema_before"]:
                print(f"- Esquema antes: `{row['schema_before']}`")
            if row["schema_after"]:
                print(f"- Esquema después: `{row['schema_after']}`")
            print()
            print("```sql")
            print(row["sql_text"].strip())
            print("```")
            return 0

        rows = conn.execute(
            'SELECT id, executed_at, kind, affected_rows, reversible, summary '
            'FROM "_changes" ORDER BY id DESC LIMIT ?',
            (args.limit,),
        ).fetchall()
    data = [dict(row) for row in rows]
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print("# Historial de cambios")
    print()
    print(
        format_table(
            ["ID", "Fecha", "Tipo", "Filas", "Reversible", "Resumen"],
            [
                [
                    row["id"],
                    row["executed_at"],
                    row["kind"],
                    row["affected_rows"],
                    "sí" if row["reversible"] else "no",
                    row["summary"],
                ]
                for row in data
            ],
            max_width=50,
        )
    )
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    mapping_path = Path(args.mapping).expanduser().resolve() if args.mapping else None
    root = resolve_root(mapping_path) if mapping_path else Path.cwd().resolve()
    db_path = resolve_db(args.db, root)

    if args.write:
        with closing(connect_readonly(db_path)) as conn:
            path = write_schema_sql(conn, root)
        print(f"Esquema escrito en {path}")
        return 0

    if args.check:
        if mapping_path is None:
            mapping_path = root / "schema" / "mapping.json"
        if not mapping_path.exists():
            raise TabularError(
                f"No existe el mapping: {mapping_path}. Usa --mapping o crea schema/mapping.json."
            )
        mapping = load_mapping(mapping_path)
        with closing(connect_readonly(db_path)) as conn:
            drift = check_mapping_drift(conn, mapping)
        if args.format == "json":
            print(json.dumps(drift, ensure_ascii=False, indent=2))
        else:
            print("# Diferencias entre mapping y base")
            print()
            if not drift:
                print("Sin diferencias: el mapping coincide con la base.")
            else:
                print(
                    format_table(
                        ["Tipo", "Tabla", "Detalle"],
                        [
                            [item["type"], item["table"], item["detail"]]
                            for item in drift
                        ],
                    )
                )
        return 0 if not drift else 2

    with closing(connect_readonly(db_path)) as conn:
        objects = conn.execute(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
        ).fetchall()
        data: list[dict[str, Any]] = []
        for obj in objects:
            item: dict[str, Any] = {
                "name": obj["name"],
                "type": obj["type"],
                "ddl": obj["sql"],
            }
            if obj["type"] == "table":
                item["rows"] = conn.execute(
                    f"SELECT COUNT(*) FROM {quote_ident(obj['name'])}"
                ).fetchone()[0]
                item["columns"] = [
                    dict(column)
                    for column in conn.execute(
                        f"PRAGMA table_info({quote_ident(obj['name'])})"
                    ).fetchall()
                ]
                item["foreign_keys"] = [
                    dict(fk)
                    for fk in conn.execute(
                        f"PRAGMA foreign_key_list({quote_ident(obj['name'])})"
                    ).fetchall()
                ]
            data.append(item)

    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.format == "sql":
        for item in data:
            if item["ddl"]:
                print(item["ddl"].rstrip() + ";")
                print()
        return 0

    print(f"# Esquema de `{db_path.name}`")
    print()
    table_rows = [
        [item["name"], item["type"], item.get("rows", ""), len(item.get("columns", []))]
        for item in data
    ]
    print(format_table(["Nombre", "Tipo", "Filas", "Columnas"], table_rows))
    for item in data:
        if item["type"] != "table":
            continue
        print()
        print(f"## {item['name']}")
        print()
        print(
            format_table(
                ["Columna", "Tipo", "NOT NULL", "PK", "Default"],
                [
                    [
                        column["name"],
                        column["type"],
                        "sí" if column["notnull"] else "no",
                        "sí" if column["pk"] else "no",
                        column["dflt_value"],
                    ]
                    for column in item["columns"]
                ],
            )
        )
        if item["foreign_keys"]:
            print()
            print("**Relaciones:**")
            for fk in item["foreign_keys"]:
                print(
                    f"- `{fk['from']}` -> `{fk['table']}.{fk['to']}`"
                )
    print()
    print("## DDL")
    print()
    print("```sql")
    for item in data:
        if item["ddl"]:
            print(item["ddl"].rstrip() + ";")
            print()
    print("```")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"tabular {VERSION}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tabular",
        description="Convierte un CSV en un proyecto SQLite reproducible.",
    )
    parser.add_argument("--version", action="version", version=f"tabular {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile = subparsers.add_parser("profile", help="Analiza un CSV y genera schema/profile.json")
    profile.add_argument("input", help="Ruta al archivo .csv")
    profile.add_argument("--out", default="schema/profile.json", help="Ruta del perfil JSON")
    profile.add_argument("--header-row", type=int, default=None, help="Fila del encabezado (1-based)")
    profile.add_argument("--footer-rows", type=int, default=None, help="Filas finales a excluir")
    profile.add_argument("--delimiter", default=None, help="Delimitador (si no se detecta)")
    profile.add_argument("--encoding", default=None, help="Encoding (si no se detecta)")
    profile.add_argument("--max-rows", type=int, default=None, help="Limita filas para el perfil")
    profile.set_defaults(func=cmd_profile)

    init = subparsers.add_parser(
        "init", help="Importa el CSV una sola vez y crea la base"
    )
    init.add_argument("mapping", help="Ruta a schema/mapping.json")
    init.add_argument("--csv", default=None, help="CSV a importar (reemplaza source.path)")
    init.add_argument("--db", default=None, help="Ruta de la base SQLite")
    init.add_argument(
        "--archive-dir", default="data/imports", help="Directorio para archivar el CSV"
    )
    init.add_argument(
        "--delete-source",
        action="store_true",
        help="Borra el CSV original después de archivar y verificar",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Recrea una base existente con backup previo",
    )
    init.add_argument(
        "--allow-rejects", action="store_true", help="Carga filas válidas y omite rechazos"
    )
    init.add_argument(
        "--no-report", action="store_true", help="No escribe reports/rechazos.csv"
    )
    init.set_defaults(func=cmd_init)

    verify = subparsers.add_parser("verify", help="Verifica la integridad de la base")
    verify.add_argument("--db", default=None, help="Ruta de la base SQLite")
    verify.add_argument(
        "--integrity",
        action="store_true",
        help="Verifica integridad, FKs y metadatos (modo por defecto)",
    )
    verify.add_argument("--format", choices=["table", "json"], default="table")
    verify.set_defaults(func=cmd_verify)

    query = subparsers.add_parser("query", help="Ejecuta una consulta de solo lectura")
    query.add_argument("sql", nargs="?", default=None, help="Consulta SQL")
    query.add_argument("--file", default=None, help="Archivo .sql con la consulta")
    query.add_argument("--db", default=None, help="Ruta de la base SQLite")
    query.add_argument("--format", choices=["table", "json", "csv"], default="table")
    query.add_argument("--limit", type=int, default=200, help="Máximo de filas (0 = sin límite)")
    query.add_argument("--timeout", type=int, default=15, help="Timeout en segundos")
    query.add_argument("--out", default=None, help="Archivo de salida CSV o JSON")
    query.set_defaults(func=cmd_query)

    mutate = subparsers.add_parser(
        "mutate", help="Ejecuta DML/DDL en una transacción, con rollback por defecto"
    )
    mutate.add_argument("--sql", default=None, help="SQL a ejecutar")
    mutate.add_argument("--file", default=None, help="Archivo .sql a ejecutar")
    mutate.add_argument("--db", default=None, help="Ruta de la base SQLite")
    mutate.add_argument(
        "--capture-sql",
        default=None,
        help="SELECT para capturar el antes de un UPDATE y generar undo",
    )
    mutate.add_argument("--confirm", action="store_true", help="Aplica el cambio")
    mutate.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Confirmación adicional obligatoria para DELETE",
    )
    mutate.add_argument(
        "--allow-irreversible",
        action="store_true",
        help="Permite aplicar sin undo y sin backup",
    )
    mutate.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Permite DROP/RENAME en DDL",
    )
    mutate.add_argument(
        "--allow-full-table",
        action="store_true",
        help="Permite UPDATE/DELETE sin WHERE",
    )
    mutate.add_argument(
        "--backup", action="store_true", help="Fuerza backup completo antes de aplicar"
    )
    mutate.add_argument(
        "--keep-history",
        type=int,
        default=UNDO_RETENTION,
        help="Cantidad de undo files a conservar",
    )
    mutate.add_argument(
        "--keep-backups",
        type=int,
        default=BACKUP_RETENTION,
        help="Cantidad de backups completos a conservar",
    )
    mutate.set_defaults(func=cmd_mutate)

    history = subparsers.add_parser("history", help="Muestra el historial de cambios")
    history.add_argument("--db", default=None, help="Ruta de la base SQLite")
    history.add_argument("--limit", type=int, default=20, help="Cantidad de cambios")
    history.add_argument("--show", type=int, default=None, help="Muestra un cambio por ID")
    history.add_argument("--format", choices=["table", "json"], default="table")
    history.set_defaults(func=cmd_history)

    schema = subparsers.add_parser("schema", help="Muestra tablas, columnas y DDL")
    schema.add_argument("--db", default=None, help="Ruta de la base SQLite")
    schema.add_argument("--mapping", default=None, help="Ruta a schema/mapping.json")
    schema.add_argument(
        "--check", action="store_true", help="Compara mapping contra la base"
    )
    schema.add_argument(
        "--write", action="store_true", help="Regenera schema/schema.sql desde la base"
    )
    schema.add_argument("--format", choices=["table", "json", "sql"], default="table")
    schema.set_defaults(func=cmd_schema)

    version = subparsers.add_parser("version", help="Muestra la versión")
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"init", "verify", "query", "schema", "mutate", "history"}:
            ensure_sqlite_supported()
        return int(args.func(args))
    except TabularError as exc:
        eprint(f"Error: {exc}")
        return 2
    except sqlite3.Error as exc:
        eprint(f"Error SQLite: {exc}")
        return 2
    except OSError as exc:
        eprint(f"Error de archivo: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("Interrumpido.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
