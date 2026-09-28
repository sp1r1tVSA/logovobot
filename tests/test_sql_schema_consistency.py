"""Every literal SQL query in the application must compile against the real schema.

A query naming a missing column or an un-joined alias only fails when it runs,
and several sites wrap the call in a broad ``except`` — so the bug is either a
crash in production («История игр»: ``no such column: u1.telegram_id``) or a
silent no-op (notifications written to ``message`` instead of ``body``).
``EXPLAIN`` compiles each statement against the schema ``init_db()`` builds
without executing it, which catches both kinds up front.

Only plain string literals are checked; f-strings and queries assembled in
variables interpolate identifiers from whitelists and are left out.
"""
import ast
import os
import re
import sqlite3

import pytest

import database

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".claude", ".git", "tests", "venv", ".venv", "node_modules", "__pycache__"}

# Scratch tables that exist only while a migration rebuilds the real one.
MIGRATION_TEMP_TABLES = ("rounds_v3", "user_bets_migrate_cashed_out", "division_topics__migration", "round_reminders_v2")

STATEMENT = re.compile(r"\s*(SELECT|WITH|INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def _literal_queries():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            rel = os.path.relpath(path, ROOT)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("execute", "executemany") and node.args):
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and STATEMENT.match(arg.value):
                    yield f"{rel}:{node.lineno}", arg.value


QUERIES = list(_literal_queries())


def test_scanner_finds_queries():
    # Guards against the scan silently matching nothing after a refactor.
    assert len(QUERIES) > 500


@pytest.mark.parametrize("sql", [q[1] for q in QUERIES], ids=[q[0] for q in QUERIES])
def test_query_compiles_against_schema(sql):
    if any(t in sql for t in MIGRATION_TEMP_TABLES):
        pytest.skip("migration scratch table")
    conn = database.get_connection()
    named = re.findall(r"(?<![:\w]):(\w+)", sql)
    params = {k: None for k in named} if named else [None] * sql.count("?")
    try:
        conn.execute("EXPLAIN " + sql, params)
    except sqlite3.ProgrammingError:
        pass  # binding-count mismatch from '?' inside string literals — not a schema issue
