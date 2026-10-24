#!/usr/bin/env python3
"""tabular — runtime pequeño para consultar y mutar una base SQLite.

No lee CSV ni crea la base. La base la construye csvsqlite.py una sola vez.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

VERSION = "0.4.0-rc1"
BACKUP_RETENTION = 10
UNDO_RETENTION = 100


class TabularError(Exception):
    """Error controlado."""


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def configure_console() -> None:
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
            "Se requiere SQLite 3.37 o superior."
        )


def replace_with_retry(source: Path, target: Path, attempts: int = 5, delay: float = 0.5) -> None:
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
        "No se pudo reemplazar el archivo. Cierra Excel, DB Browser o cualquier "
        "programa que tenga abierto el archivo destino."
    ) from last_error


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return value or "cambio"


def relative_to_root(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def format_value(value: Any, max_width: int = 60) -> str:
    if value is None:
        text = "NULL"
    else:
        text = str(value).replace("\n", "\\n").replace("\r", "").replace("|", "\\|")
    if len(text) > max_width:
        text = text[:max_width] if max_width <= 3 else text[: max_width - 3] + "..."
    return text


def dedupe_headers(headers: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    result: list[str] = []
    for header in headers:
        name = str(header)
        counts[name] = counts.get(name, 0) + 1
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
    lines = [
        "| " + " | ".join(str(h)[:max_width].ljust(widths[i]) for i, h in enumerate(headers)) + " |",
        "|" + "|".join("-" * (width + 2) for width in widths) + "|",
    ]
    for row in rendered:
        cells = [row[i] if i < len(row) else "" for i in range(len(headers))]
        lines.append("| " + " | ".join(cells[i].ljust(widths[i]) for i in range(len(headers))) + " |")
    return "\n".join(lines)


def resolve_db(db_arg: str | None) -> Path:
    if db_arg:
        path = Path(db_arg).expanduser()
        return path.resolve()
    candidates = sorted(Path.cwd().glob("db/*.sqlite"))
    if len(candidates) == 1:
        return candidates[0].resolve()
    if not candidates:
        raise TabularError("No encontré una base SQLite en db/. Usa --db.")
    names = ", ".join(path.name for path in candidates)
    raise TabularError(f"Hay varias bases ({names}). Indica cuál usar con --db.")


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise TabularError(f"No existe la base: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass
    return conn


def connect_readwrite(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise TabularError(f"No existe la base: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def dump_schema_sql(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "WHEN 'view' THEN 2 WHEN 'trigger' THEN 3 ELSE 4 END, name"
    ).fetchall()
    lines = [
        f"-- Esquema real de la base (tabular {VERSION})",
        "-- No editar a mano: usa `schema --write`.",
        "",
        "PRAGMA foreign_keys = ON;",
        "",
    ]
    for _type, _name, sql in rows:
        lines.append(sql.rstrip(";") + ";")
        lines.append("")
    return "\n".join(lines)


def write_schema_sql(conn: sqlite3.Connection, root: Path) -> Path:
    path = root / "schema" / "schema.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_schema_sql(conn), encoding="utf-8")
    return path


def prune_backups(backup_dir: Path, keep: int) -> None:
    if keep < 1:
        return
    files = sorted(backup_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def backup_database(db_path: Path, root: Path, reason: str, keep: int = BACKUP_RETENTION) -> Path:
    if not db_path.exists():
        raise TabularError(f"No existe la base para respaldar: {db_path}")
    backup_dir = root / "db" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_dir / f"{slugify(db_path.stem)}_{timestamp}_{slugify(reason)}.sqlite"
    counter = 2
    while target.exists():
        target = backup_dir / f"{slugify(db_path.stem)}_{timestamp}_{slugify(reason)}_{counter}.sqlite"
        counter += 1
    try:
        with closing(sqlite3.connect(db_path)) as source, closing(
            sqlite3.connect(target)
        ) as destination:
            source.backup(destination)
        with closing(sqlite3.connect(target)) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise TabularError("El backup no pasó integrity_check.")
    except Exception:
        if target.exists():
            target.unlink()
        raise
    prune_backups(backup_dir, keep)
    return target


def verify_integrity(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    checks.append(
        {
            "name": "integrity_check",
            "passed": bool(integrity and integrity[0] == "ok"),
            "detail": "ok" if integrity and integrity[0] == "ok" else str(integrity[0] if integrity else ""),
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
        if exists:
            count = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            checks.append(
                {
                    "name": f"meta:filas:{table_name}",
                    "passed": count >= 1,
                    "detail": f"{count} filas",
                }
            )
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for table in tables:
        name = table["name"]
        count = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(name)}").fetchone()[0]
        checks.append({"name": f"tabla:{name}", "passed": True, "detail": f"{count} filas"})
    return checks


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
        raise TabularError("El SQL quedó incompleto.")
    if not statements:
        raise TabularError("No hay sentencias SQL.")
    return statements


def strip_comments(sql: str) -> str:
    return re.sub(r"^\s*(--[^\n]*\n|/\*.*?\*/)*", "", sql, flags=re.DOTALL).strip()


def leading_keyword(statement: str) -> str:
    match = re.match(r"([A-Za-z]+)", strip_comments(statement))
    return match.group(1).upper() if match else ""


def classify_statement(statement: str) -> str:
    keyword = leading_keyword(statement)
    upper = strip_comments(statement).upper()
    if keyword in {"SELECT", "PRAGMA", "ATTACH", "DETACH", "VACUUM", "EXPLAIN"}:
        raise TabularError(f"`{keyword}` no se permite en mutate. Usa `query`.")
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
        raise TabularError("WITH sin DML no es una mutación. Usa `query`.")
    raise TabularError(f"Sentencia no soportada en mutate: {keyword or statement[:40]!r}")


def table_of_statement(statement: str) -> str | None:
    clean = strip_comments(statement)
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
    clean = strip_comments(statement).rstrip().rstrip(";")
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
    clean = strip_comments(sql).lstrip().upper()
    if not clean.startswith("SELECT"):
        raise TabularError("--capture-sql debe ser una consulta SELECT.")
    return [dict(row) for row in conn.execute(sql).fetchall()]


def ensure_returning(statement: str) -> str:
    clean = statement.strip().rstrip(";").rstrip()
    if re.search(r"\bRETURNING\b", clean, flags=re.IGNORECASE):
        return statement
    return clean + " RETURNING *;"


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
            f"DELETE FROM {quote_ident(table)} WHERE id IN ("
            + ", ".join(sql_literal(value) for value in ids)
            + ");\n"
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
    while any((directory / f"{candidate}{suffix}").exists() for suffix in (".sql", "_undo.sql", ".md")):
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
    lines = [
        "# Mutación SQL",
        "",
        f"- Modo: **{mode}**",
        f"- Tipo: `{', '.join(kinds)}`",
        f"- Filas afectadas: {sum(result['rowcounts'])}",
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


def write_change_report(path: Path, *args: Any) -> None:
    path.write_text(mutation_summary(*args), encoding="utf-8")


def prune_undo_history(conn: sqlite3.Connection, root: Path, keep: int) -> None:
    if keep < 1:
        return
    rows = conn.execute(
        'SELECT id, undo_path FROM "_changes" WHERE undo_path IS NOT NULL ORDER BY id DESC'
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
    first_line = " ".join(strip_comments(statement).split())
    if len(first_line) > 90:
        first_line = first_line[:87] + "..."
    return f"{kind}: {first_line}"


def cmd_query(args: argparse.Namespace) -> int:
    db_path = resolve_db(args.db)
    sql = args.sql
    if args.file:
        path = Path(args.file).expanduser()
        if not path.exists():
            raise TabularError(f"No existe el SQL: {path}")
        sql = path.read_text(encoding="utf-8-sig")
    if not sql or not sql.strip():
        raise TabularError("Indica una consulta o usa --file.")
    clean = strip_comments(sql)
    if not re.match(r"^(select|with|explain)\b", clean, re.IGNORECASE):
        raise TabularError("Solo se permiten SELECT, WITH o EXPLAIN.")
    if args.limit < 0:
        raise TabularError("--limit no puede ser negativo.")
    with closing(connect_readonly(db_path)) as conn:
        deadline = time.monotonic() + max(args.timeout, 1)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        try:
            cursor = conn.execute(sql)
            rows = cursor.fetchall() if args.limit == 0 else cursor.fetchmany(args.limit + 1)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise TabularError("La consulta superó el tiempo límite.") from exc
            raise TabularError(f"Error SQL: {exc}") from exc
        truncated = args.limit != 0 and len(rows) > args.limit
        if truncated:
            rows = rows[: args.limit]
        headers = dedupe_headers([d[0] for d in (cursor.description or [])])

    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fmt = "csv" if args.format == "table" else args.format
        if fmt == "json":
            out_path.write_text(
                json.dumps([dict(zip(headers, row)) for row in rows], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        elif fmt == "csv":
            with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(headers)
                writer.writerows(rows)
        else:
            raise TabularError("--out solo admite csv o json.")
        if truncated:
            eprint(f"[{len(rows)} filas escritas; usa --limit 0 para todo]")
        print(f"Resultado escrito en {out_path}")
        return 0

    if args.format == "json":
        print(json.dumps([dict(zip(headers, row)) for row in rows], ensure_ascii=False, indent=2))
    elif args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(headers)
        writer.writerows(rows)
    else:
        print(format_table(headers, rows))
    if truncated:
        eprint(f"[{len(rows)} filas mostradas; usa --limit 0 o un filtro]")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    db_path = resolve_db(args.db)
    if args.write:
        with closing(connect_readonly(db_path)) as conn:
            path = write_schema_sql(conn, Path.cwd())
        print(f"Esquema escrito en {path}")
        return 0
    with closing(connect_readonly(db_path)) as conn:
        objects = conn.execute(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END, name"
        ).fetchall()
        data: list[dict[str, Any]] = []
        for obj in objects:
            item: dict[str, Any] = {"name": obj["name"], "type": obj["type"], "ddl": obj["sql"]}
            if obj["type"] == "table":
                item["rows"] = conn.execute(
                    f"SELECT COUNT(*) FROM {quote_ident(obj['name'])}"
                ).fetchone()[0]
                item["columns"] = [
                    dict(c) for c in conn.execute(f"PRAGMA table_info({quote_ident(obj['name'])})")
                ]
                item["foreign_keys"] = [
                    dict(fk)
                    for fk in conn.execute(f"PRAGMA foreign_key_list({quote_ident(obj['name'])})")
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
    print(format_table(
        ["Nombre", "Tipo", "Filas", "Columnas"],
        [[item["name"], item["type"], item.get("rows", ""), len(item.get("columns", []))] for item in data],
    ))
    for item in data:
        if item["type"] != "table":
            continue
        print()
        print(f"## {item['name']}")
        print()
        print(format_table(
            ["Columna", "Tipo", "NOT NULL", "PK", "Default"],
            [[c["name"], c["type"], "sí" if c["notnull"] else "no", "sí" if c["pk"] else "no", c["dflt_value"]] for c in item["columns"]],
        ))
        if item["foreign_keys"]:
            print()
            print("**Relaciones:**")
            for fk in item["foreign_keys"]:
                print(f"- `{fk['from']}` -> `{fk['table']}.{fk['to']}`")
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
        print(format_table(
            ["Chequeo", "Resultado", "Detalle"],
            [[c["name"], "OK" if c["passed"] else "FALLA", c["detail"]] for c in checks],
        ))
        print()
        print(f"Resultado: {'OK' if not failed else 'FALLA'} ({len(checks) - len(failed)}/{len(checks)} chequeos)")
    return 0 if not failed else 2


def cmd_history(args: argparse.Namespace) -> int:
    if args.limit < 1:
        raise TabularError("--limit debe ser mayor que cero.")
    db_path = resolve_db(args.db)
    with closing(connect_readonly(db_path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '_changes'"
        ).fetchone()
        if not exists:
            raise TabularError("No existe _changes. ¿La base fue creada por csvsqlite?")
        if args.show:
            row = conn.execute('SELECT * FROM "_changes" WHERE id = ?', (args.show,)).fetchone()
            if not row:
                raise TabularError(f"No existe el cambio {args.show}.")
            if args.format == "json":
                print(json.dumps(dict(row), ensure_ascii=False, indent=2))
                return 0
            print(f"# Cambio {row['id']}")
            print()
            print(f"- Fecha: {row['executed_at']}")
            print(f"- Tipo: `{row['kind']}`")
            print(f"- Filas afectadas: {row['affected_rows']}")
            print(f"- Reversible: {'sí' if row['reversible'] else 'no'}")
            if row["undo_path"]:
                undo = Path(row["undo_path"])
                if not undo.is_absolute():
                    undo = Path.cwd() / undo
                print(f"- Undo: `{row['undo_path']}` ({'existe' if undo.exists() else 'no disponible'})")
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
    print(format_table(
        ["ID", "Fecha", "Tipo", "Filas", "Reversible", "Resumen"],
        [[r["id"], r["executed_at"], r["kind"], r["affected_rows"], "sí" if r["reversible"] else "no", r["summary"]] for r in data],
        max_width=50,
    ))
    return 0


def cmd_mutate(args: argparse.Namespace) -> int:
    db_path = resolve_db(args.db)
    root = Path.cwd().resolve()
    if args.sql and args.file:
        raise TabularError("Usa --sql o --file, no ambos.")
    if not args.sql and not args.file:
        raise TabularError("Debes indicar --sql o --file.")
    if args.file:
        path = Path(args.file).expanduser()
        if not path.exists():
            raise TabularError(f"No existe el SQL: {path}")
        sql_text = path.read_text(encoding="utf-8-sig")
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
        raise TabularError("--capture-sql solo aplica a un UPDATE único.")

    destructive = any(kind == "schema_destructive" for kind in kinds)
    schema_change = any(kind in {"schema", "schema_destructive"} for kind in kinds)
    delete_change = any(
        kind == "data_delete"
        or (leading_keyword(statement) == "WITH" and "DELETE" in statement.upper())
        for statement, kind in zip(statements, kinds)
    )
    full_table = any(kind == "data_full" for kind in kinds)
    complex_change = any(kind == "complex" for kind in kinds)

    with closing(connect_readwrite(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = execute_mutation_statements(conn, statements, kinds, args.capture_sql)
        finally:
            conn.rollback()

    reversible, undo_sql = compute_undo(statements, kinds, result)
    warnings: list[str] = []
    if delete_change:
        warnings.append("Es un DELETE. Pide confirmación explícita.")
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
        print(mutation_summary(
            "dry-run (rollback)", sql_text, kinds, result, reversible,
            root / "reports" / "changes" / "<se generará>_undo.sql" if reversible else None,
            None, warnings,
        ))
        print("\nNo se aplicó nada. Confirma con --confirm.")
        return 0

    if delete_change and not args.confirm_delete:
        raise TabularError("DELETE requiere --confirm-delete además de --confirm.")
    if full_table and not args.allow_full_table:
        raise TabularError("UPDATE/DELETE sin WHERE requiere --allow-full-table.")
    if destructive and not args.allow_destructive:
        raise TabularError("DROP/RENAME requiere --allow-destructive.")
    need_backup = args.backup or schema_change or destructive
    if not reversible and not need_backup and not args.allow_irreversible:
        raise TabularError("Operación irreversible sin backup. Usa --backup o --allow-irreversible.")

    backup_path = backup_database(
        db_path, root, "pre-schema" if schema_change else "pre-mutate", keep=args.keep_backups
    ) if need_backup else None
    changes_dir = root / "reports" / "changes"
    changes_dir.mkdir(parents=True, exist_ok=True)
    base = unique_change_base(changes_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{kinds[0]}")
    sql_path = changes_dir / f"{base}.sql"
    undo_path = changes_dir / f"{base}_undo.sql" if reversible and undo_sql else None
    report_path = changes_dir / f"{base}.md"
    schema_before_path = root / "schema" / "history" / f"schema_{base}_before.sql" if schema_change else None
    schema_after_path = root / "schema" / "history" / f"schema_{base}_after.sql" if schema_change else None
    if schema_before_path:
        schema_before_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        sql_path.write_text(sql_text.rstrip() + "\n", encoding="utf-8")
        if schema_before_path and result["before_schema"] is not None:
            schema_before_path.write_text(result["before_schema"], encoding="utf-8")
        if schema_after_path and result["after_schema"] is not None:
            schema_after_path.write_text(result["after_schema"], encoding="utf-8")
        with closing(connect_readwrite(db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = execute_mutation_statements(conn, statements, kinds, args.capture_sql)
                reversible, undo_sql = compute_undo(statements, kinds, result)
                if not reversible and not need_backup and not args.allow_irreversible:
                    raise TabularError("Operación irreversible sin backup.")
                if reversible and undo_path is None:
                    undo_path = changes_dir / f"{base}_undo.sql"
                if not reversible and undo_path is not None:
                    if undo_path.exists():
                        undo_path.unlink()
                    undo_path = None
                if undo_path and undo_sql:
                    undo_path.write_text(
                        "-- Undo generado por tabular. Revísalo antes de aplicarlo.\n" + undo_sql,
                        encoding="utf-8",
                    )
                if schema_before_path and result["before_schema"] is not None:
                    schema_before_path.write_text(result["before_schema"], encoding="utf-8")
                if schema_after_path and result["after_schema"] is not None:
                    schema_after_path.write_text(result["after_schema"], encoding="utf-8")
                summary = history_summary(statements[0], kinds[0]) if len(statements) == 1 else f"{len(statements)} sentencias: " + ", ".join(kinds)
                cursor = conn.execute(
                    'INSERT INTO "_changes" '
                    '("executed_at", "kind", "sql_text", "summary", "affected_rows", '
                    '"reversible", "undo_path", "backup_path", "schema_before", '
                    '"schema_after", "tool_version") '
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        kinds[0] if len(set(kinds)) == 1 else "mixed",
                        sql_text,
                        summary,
                        sum(result["rowcounts"]),
                        1 if reversible else 0,
                        relative_to_root(undo_path, root),
                        relative_to_root(backup_path, root),
                        relative_to_root(schema_before_path, root),
                        relative_to_root(schema_after_path, root),
                        VERSION,
                    ),
                )
                change_id = cursor.lastrowid
                write_change_report(
                    report_path, "confirmado", sql_text, kinds, result, reversible,
                    undo_path, backup_path, warnings,
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
        print(mutation_summary(
            "confirmado", sql_text, kinds, result, reversible, undo_path, backup_path, warnings
        ))
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


def cmd_version(args: argparse.Namespace) -> int:
    print(f"tabular {VERSION}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tabular",
        description="Runtime de consulta y CRUD para una base SQLite creada por csvsqlite.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query", help="Ejecuta una consulta de solo lectura")
    query.add_argument("sql", nargs="?", default=None)
    query.add_argument("--file", default=None)
    query.add_argument("--db", default=None)
    query.add_argument("--format", choices=["table", "json", "csv"], default="table")
    query.add_argument("--limit", type=int, default=200)
    query.add_argument("--timeout", type=int, default=15)
    query.add_argument("--out", default=None)
    query.set_defaults(func=cmd_query)

    mutate = sub.add_parser("mutate", help="Ejecuta DML/DDL con rollback por defecto")
    mutate.add_argument("--sql", default=None)
    mutate.add_argument("--file", default=None)
    mutate.add_argument("--db", default=None)
    mutate.add_argument("--capture-sql", default=None)
    mutate.add_argument("--confirm", action="store_true")
    mutate.add_argument("--confirm-delete", action="store_true")
    mutate.add_argument("--allow-irreversible", action="store_true")
    mutate.add_argument("--allow-destructive", action="store_true")
    mutate.add_argument("--allow-full-table", action="store_true")
    mutate.add_argument("--backup", action="store_true")
    mutate.add_argument("--keep-history", type=int, default=UNDO_RETENTION)
    mutate.add_argument("--keep-backups", type=int, default=BACKUP_RETENTION)
    mutate.set_defaults(func=cmd_mutate)

    history = sub.add_parser("history", help="Muestra el historial de cambios")
    history.add_argument("--db", default=None)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--show", type=int, default=None)
    history.add_argument("--format", choices=["table", "json"], default="table")
    history.set_defaults(func=cmd_history)

    schema = sub.add_parser("schema", help="Muestra tablas, columnas y DDL")
    schema.add_argument("--db", default=None)
    schema.add_argument("--write", action="store_true", help="Regenera schema/schema.sql")
    schema.add_argument("--format", choices=["table", "json", "sql"], default="table")
    schema.set_defaults(func=cmd_schema)

    verify = sub.add_parser("verify", help="Verifica la integridad de la base")
    verify.add_argument("--db", default=None)
    verify.add_argument("--integrity", action="store_true")
    verify.add_argument("--format", choices=["table", "json"], default="table")
    verify.set_defaults(func=cmd_verify)

    version = sub.add_parser("version", help="Muestra la versión")
    version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"query", "mutate", "history", "schema", "verify"}:
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
