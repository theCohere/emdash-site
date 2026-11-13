#!/usr/bin/env python3
"""Convert a D1 sqlite3_cli_only export into SQL importable into a new D1.

Runs entirely locally. Supports ordinary tables and EmDash's internally stored
FTS5 tables. Virtual-table shadow storage is rebuilt through normal FTS5 SQL.
The original dump is preserved; only a new output file is written.
"""

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import tempfile
from pathlib import Path


def progress(message):
    print(message, file=sys.stderr, flush=True)


def unistr_compat(value):
    """Decode SQLite 3.50 Unicode escapes on older Python SQLite builds."""
    if value is None:
        return None
    text = value.decode("utf-8") if isinstance(value, bytes) else str(value)
    result = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            result.append(text[index])
            index += 1
            continue
        index += 1
        if index == len(text):
            raise ValueError("Incomplete unistr escape")
        marker = text[index]
        if marker == "\\":
            result.append("\\")
            index += 1
            continue
        length = {"u": 4, "U": 8, "+": 6}.get(marker, 4)
        if marker in ("u", "U", "+"):
            index += 1
        digits = text[index:index + length]
        if len(digits) != length or not re.fullmatch(r"[0-9a-fA-F]+", digits):
            raise ValueError("Invalid unistr escape")
        codepoint = int(digits, 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("Invalid Unicode codepoint in unistr")
        result.append(chr(codepoint))
        index += length
    return "".join(result)


def enable_dump_functions(connection):
    try:
        connection.execute("SELECT unistr('')")
    except sqlite3.OperationalError:
        connection.create_function("unistr", 1, unistr_compat, deterministic=True)


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    if isinstance(value, str):
        if "\x00" in value:
            return "CAST(X'" + value.encode("utf-8").hex() + "' AS TEXT)"
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, float) and math.isinf(value):
        return "-9e999" if value < 0 else "9e999"
    return repr(value)


def database_tables(connection):
    return {
        row[1]: row[2]
        for row in connection.execute("PRAGMA main.table_list")
        if row[0] == "main"
        and not row[1].startswith(("sqlite_", "_cf_"))
        and row[2] in ("table", "virtual")
    }


def row_columns(connection, name):
    columns = [
        row[1]
        for row in connection.execute("PRAGMA main.table_xinfo(" + identifier(name) + ")")
        if row[6] == 0
    ]
    # Preserve SQLite rowids: EmDash's search triggers refer to content rowids.
    for alias in ("_rowid_", "rowid", "oid"):
        if alias.lower() in {column.lower() for column in columns}:
            continue
        try:
            connection.execute(
                "SELECT " + identifier(alias) + " FROM " + identifier(name) + " LIMIT 0"
            )
        except sqlite3.OperationalError:
            continue
        # Double-quoted string literals can mask a missing rowid. Probe unquoted.
        try:
            connection.execute("SELECT " + alias + " FROM " + identifier(name) + " LIMIT 0")
        except sqlite3.OperationalError:
            continue
        return [alias] + columns
    return columns


def ordered_tables(connection, tables):
    """Put referenced tables before their dependents without deferred FKs."""
    dependencies = {
        name: {
            row[2]
            for row in connection.execute("PRAGMA main.foreign_key_list(" + identifier(name) + ")")
            if row[2] != name and row[2] in tables
        }
        for name in tables
    }
    pending = set(tables)
    result = []
    while pending:
        ready = [name for name in tables if name in pending and not dependencies[name] & pending]
        if not ready:
            raise ValueError("Cyclic table foreign keys need separate handling: " + ", ".join(sorted(pending)))
        result.extend(ready)
        pending.difference_update(ready)
    return result


def ordered_rows(connection, name, columns):
    """Order self-referencing records using their actual SQLite FK matches."""
    names = ",".join(map(identifier, columns))
    query = "SELECT " + names + " FROM " + identifier(name)
    foreign_keys = {}
    for row in connection.execute("PRAGMA main.foreign_key_list(" + identifier(name) + ")"):
        if row[2] == name:
            foreign_keys.setdefault(row[0], []).append(row)
    if not foreign_keys:
        return connection.execute(query)
    rows = connection.execute(query).fetchall()
    positions = {row: index for index, row in enumerate(rows)}
    column_positions = {column.lower(): index for index, column in enumerate(columns)}
    primary_key = [row[1] for row in sorted(
        (row for row in connection.execute("PRAGMA main.table_info(" + identifier(name) + ")") if row[5]),
        key=lambda row: row[5],
    )]
    dependencies = {index: set() for index in range(len(rows))}
    for index, row in enumerate(rows):
        for parts in foreign_keys.values():
            parts = sorted(parts, key=lambda part: part[1])
            if any(part[3].lower() not in column_positions for part in parts):
                raise ValueError("Self-reference on a generated column needs separate handling: " + name)
            values = [row[column_positions[part[3].lower()]] for part in parts]
            if any(value is None for value in values):
                continue
            referenced = [part[4] or primary_key[part[1]] for part in parts]
            parent = connection.execute(
                query + " WHERE " + " AND ".join(identifier(column) + "=?" for column in referenced),
                values,
            ).fetchone()
            if parent is None:
                raise ValueError("Missing self-referenced parent: " + name)
            parent_index = positions[parent]
            if parent_index != index:
                dependencies[index].add(parent_index)
    pending = set(range(len(rows)))
    result = []
    while pending:
        ready = [index for index in sorted(pending) if not dependencies[index] & pending]
        if not ready:
            raise ValueError("Cyclic self-referencing rows need separate handling: " + name)
        result.extend(rows[index] for index in ready)
        pending.difference_update(ready)
    return result


def rows_digest(hashes):
    return hashlib.sha256("".join(sorted(hashes)).encode("ascii")).hexdigest()


def convert(input_path, output_path):
    if output_path.exists():
        raise ValueError("Output already exists; choose a new filename")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = output_path.with_name(output_path.name + ".partial")
    if partial_path.exists():
        raise ValueError("Partial output already exists; choose a new filename")

    with tempfile.TemporaryDirectory(prefix="d1-convert-") as directory:
        source_path = Path(directory) / "source.sqlite"
        progress("[1/4] Restoring exported SQL locally...")
        with sqlite3.connect(source_path) as connection:
            enable_dump_functions(connection)
            connection.executescript(input_path.read_text(encoding="utf-8-sig"))
        # Reopen so SQLite recognizes virtual-table entries from writable_schema.
        source = sqlite3.connect(source_path)
        enable_dump_functions(source)
        source.execute("PRAGMA query_only=ON")
        try:
            tables = database_tables(source)
            schema = source.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE sql IS NOT NULL ORDER BY rowid"
            ).fetchall()
            virtual_tables = []
            for kind, name, table, sql in schema:
                if kind != "table" or tables.get(name) != "virtual":
                    continue
                if not re.search(r"\bUSING\s+fts5\s*\(", sql, re.IGNORECASE):
                    raise ValueError("Unsupported virtual-table module: " + name)
                if re.search(r"\bcontent\s*=", sql, re.IGNORECASE):
                    raise ValueError("External/contentless FTS5 needs separate handling: " + name)
                virtual_tables.append(name)

            progress(f"[2/4] Converting {len(tables)} tables, including {len(virtual_tables)} FTS5 tables...")
            table_order = ordered_tables(source, tables)
            expected_counts = {}
            expected_digests = {}
            with partial_path.open("x", encoding="utf-8", newline="\n") as output:
                def emit(sql):
                    if len(sql.encode("utf-8")) > 95_000:
                        raise ValueError(
                            "A SQL statement exceeds the safe D1 size; "
                            "this database needs a chunked migration"
                        )
                    output.write(sql.rstrip(";\n") + ";\n")

                output.write("-- Converted locally; includes data, schema, FTS5 and triggers.\n")
                output.write("-- Parent tables and rows precede children; foreign keys remain immediate.\n")
                # Remote import may commit batches, resetting deferred-FK state.
                table_schema = {name: sql for kind, name, table, sql in schema if kind == "table"}
                for name in table_order:
                    emit(table_schema[name])
                for kind, name, table, sql in schema:
                    if kind == "index" and table in tables:
                        emit(sql)

                for table_index, name in enumerate(table_order, 1):
                    columns = row_columns(source, name)
                    names = ",".join(map(identifier, columns))
                    prefix = "INSERT INTO " + identifier(name) + "(" + names + ") VALUES("
                    count = 0
                    hashes = []
                    for row in ordered_rows(source, name, columns):
                        emit(prefix + ",".join(map(literal, row)) + ")")
                        hashes.append(hashlib.sha256(repr(row).encode("utf-8")).hexdigest())
                        count += 1
                    expected_counts[name] = count
                    expected_digests[name] = rows_digest(hashes)
                    if table_index % 10 == 0 or table_index == len(tables):
                        progress(f"  Converted {table_index}/{len(tables)} tables")

                # Preserve AUTOINCREMENT high-water marks, including deleted IDs.
                if source.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='sqlite_sequence'"
                ).fetchone():
                    for name, sequence in source.execute("SELECT name,seq FROM sqlite_sequence"):
                        if name in tables:
                            emit("DELETE FROM sqlite_sequence WHERE name=" + literal(name))
                            emit("INSERT INTO sqlite_sequence(name,seq) VALUES("
                                 + literal(name) + "," + literal(sequence) + ")")

                # Restore FTS5 options without writing its shadow tables directly.
                for name in virtual_tables:
                    for key, value in source.execute(
                        "SELECT k,v FROM " + identifier(name + "_config") + " WHERE k<>'version'"
                    ):
                        emit("INSERT INTO " + identifier(name) + "(" + identifier(name)
                             + ",rank) VALUES(" + literal(key) + "," + literal(value) + ")")

                # Triggers go last so loading rows does not duplicate search rows
                # or run application triggers during the restore.
                for kind, name, table, sql in schema:
                    if kind == "view" or (kind == "trigger" and table in tables):
                        emit(sql)

            # Validate with immediate foreign keys and an autocommit for each
            # statement, so correctness does not rely on remote batch boundaries.
            progress("[3/4] Validating immediate foreign keys, row values and FTS5...")
            with sqlite3.connect(Path(directory) / "verify.sqlite") as target:
                enable_dump_functions(target)
                target.execute("PRAGMA foreign_keys=ON")
                target.executescript(partial_path.read_text(encoding="utf-8"))
                for name, expected in expected_counts.items():
                    count = target.execute("SELECT count(*) FROM " + identifier(name)).fetchone()[0]
                    if count != expected:
                        raise ValueError("Row-count mismatch: " + name)
                    columns = row_columns(source, name)
                    names = ",".join(map(identifier, columns))
                    hashes = [hashlib.sha256(repr(row).encode("utf-8")).hexdigest()
                              for row in target.execute("SELECT " + names + " FROM " + identifier(name))]
                    if rows_digest(hashes) != expected_digests[name]:
                        raise ValueError("Row-value mismatch: " + name)
                for name in virtual_tables:
                    target.execute("INSERT INTO " + identifier(name) + "(" + identifier(name)
                                   + ") VALUES('integrity-check')")
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("Converted database failed integrity_check")
                if target.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Converted database failed foreign_key_check")
            partial_path.rename(output_path)
            progress("[4/4] Conversion and validation complete")
            return {"output": str(output_path), "tables": len(tables),
                    "fts5_tables": len(virtual_tables), "rows": sum(expected_counts.values()),
                    "validation": "per-statement restore, row counts and values, FTS5 integrity, foreign keys"}
        finally:
            source.close()
            if partial_path.exists():
                partial_path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="D1 sqlite3_cli_only SQL dump")
    parser.add_argument("output", type=Path, help="New D1-compatible SQL output file")
    args = parser.parse_args()
    try:
        result = convert(args.input.resolve(), args.output.resolve())
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, "Conversion failed: " + str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
