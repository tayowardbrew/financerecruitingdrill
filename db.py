"""SQLite data layer for the technical drill.

Uses stdlib sqlite3 only (no ORM). The DB file lives alongside this module and
is built by migrate.py.

The analytical SQL lives in queries.sql, not in Python strings. Each query in
that file starts with a `-- name: some_name` line; load_queries() splits the
file on those markers so app.py runs exactly the SQL you can read in the file.
"""
import os
import re
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DRILL_DB", os.path.join(BASE_DIR, "drill.db"))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
QUERIES_PATH = os.path.join(BASE_DIR, "queries.sql")

# Questions per round, matching the original app.
ROUND_SIZE = 10

_NAME_RE = re.compile(r"^--\s*name:\s*([a-z0-9_]+)\s*$", re.MULTILINE)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_queries(path=QUERIES_PATH):
    """Parse queries.sql into {name: sql_text}.

    Everything from one `-- name:` marker up to the next belongs to that query,
    including its explanatory comments (handy for showing on the dashboard).
    """
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    markers = list(_NAME_RE.finditer(text))
    queries = {}
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        body = text[m.end():end].strip()
        # Drop the decorative divider comment that introduces the NEXT query.
        body = re.sub(r"\n-- =+\s*$", "", body).strip()
        queries[m.group(1)] = body
    return queries


def run_query(conn, name, params=None, queries=None):
    """Run a named query from queries.sql and return a list of dicts.

    Every query is written with :named parameters that fall back to sensible
    defaults via COALESCE, so unspecified params are simply passed as NULL.
    """
    queries = queries or load_queries()
    sql = queries[name]
    wanted = set(re.findall(r":([a-z_]+)", _strip_comments(sql)))
    bound = {k: (params or {}).get(k) for k in wanted}
    return [dict(r) for r in conn.execute(sql, bound).fetchall()]


def _strip_comments(sql):
    return re.sub(r"--[^\n]*", "", sql)
