"""Build drill.db from schema.sql + data/questions.json.

Run:  python3 migrate.py

This REBUILDS the database from scratch (schema.sql drops and recreates every
table), so any recorded attempts are wiped. Run it once after cloning, or
whenever you want a clean slate.

After loading, it verifies the result rather than assuming it worked:
  * row counts match the JSON (questions, and 4 options per question)
  * every question has exactly one correct option
  * a random sample of questions, read BACK out of SQLite via a JOIN, matches
    the source JSON field for field
"""
import json
import os
import random
import sys

from db import get_conn, DB_PATH, SCHEMA_PATH, BASE_DIR

QUESTIONS_PATH = os.path.join(BASE_DIR, "data", "questions.json")

# Topic metadata carried over from the original app (labels + sampling scale).
CATEGORIES = [
    # code,   label,                       short,       scale
    ("flow",  "Statement Flow",            "Flow",      1.0),
    ("lbo",   "LBO & Debt",                "LBO",       1.0),
    ("stmt",  "3-Statement Lines",         "Stmt",      1.0),
    ("ev",    "EV & Equity Value",         "EV",        1.0),
    ("mult",  "Multiples & Comps",         "Multiples", 1.0),
    ("dcf",   "DCF",                       "DCF",       1.0),
    ("ma",    "M&A / Accretion-Dilution",  "M&A",       1.1),
    ("restr", "Restructuring",             "RX",        0.9),
    ("ind",   "Industry Knowledge",        "Industry",  0.45),
]

SPOT_CHECK_SAMPLE = 25


def load_questions():
    with open(QUESTIONS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build(conn, questions):
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())

    conn.executemany(
        "INSERT INTO categories (code, label, short_label, sort_order, sample_scale) "
        "VALUES (?, ?, ?, ?, ?)",
        [(code, label, short, i, scale) for i, (code, label, short, scale) in enumerate(CATEGORIES)],
    )

    q_rows, opt_rows = [], []
    for order, q in enumerate(questions):
        q_rows.append((
            q["id"], q["cat"], q["difficulty"], q["estSec"], q["q"], q["explain"],
            q.get("tag"), q.get("source"), order,
        ))
        for idx, text in enumerate(q["opts"]):
            opt_rows.append((q["id"], idx, text, 1 if idx == q["correct"] else 0))

    conn.executemany(
        "INSERT INTO questions (id, category, difficulty, est_seconds, question_text, "
        "explanation, concept_tag, source_ref, bank_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        q_rows,
    )
    conn.executemany(
        "INSERT INTO options (question_id, option_index, option_text, is_correct) "
        "VALUES (?, ?, ?, ?)",
        opt_rows,
    )
    conn.commit()


def read_back(conn, qid):
    """Rebuild one question in the source JSON shape, purely from SQLite."""
    q = conn.execute(
        "SELECT id, category, difficulty, est_seconds, question_text, explanation, "
        "concept_tag, source_ref FROM questions WHERE id = ?", (qid,)
    ).fetchone()
    opts = conn.execute(
        "SELECT option_text, is_correct FROM options WHERE question_id = ? "
        "ORDER BY option_index", (qid,)
    ).fetchall()
    out = {
        "id": q["id"], "cat": q["category"], "difficulty": q["difficulty"],
        "estSec": q["est_seconds"], "q": q["question_text"],
        "opts": [o["option_text"] for o in opts],
        "correct": [i for i, o in enumerate(opts) if o["is_correct"]][0],
        "explain": q["explanation"],
    }
    if q["concept_tag"] is not None:
        out["tag"] = q["concept_tag"]
    if q["source_ref"] is not None:
        out["source"] = q["source_ref"]
    return out


def verify(conn, questions):
    problems = []
    n_q = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    n_o = conn.execute("SELECT COUNT(*) FROM options").fetchone()[0]
    expected_opts = sum(len(q["opts"]) for q in questions)
    if n_q != len(questions):
        problems.append("questions: %d in DB vs %d in JSON" % (n_q, len(questions)))
    if n_o != expected_opts:
        problems.append("options: %d in DB vs %d expected" % (n_o, expected_opts))

    bad = conn.execute(
        "SELECT question_id, SUM(is_correct) AS n_correct, COUNT(*) AS n_opts "
        "FROM options GROUP BY question_id HAVING n_correct <> 1 OR n_opts <> 4"
    ).fetchall()
    for r in bad:
        problems.append("question %s has %d correct / %d options" % tuple(r))

    rng = random.Random()
    sample = rng.sample(questions, min(SPOT_CHECK_SAMPLE, len(questions)))
    for q in sample:
        if read_back(conn, q["id"]) != q:
            problems.append("spot-check mismatch on question %s" % q["id"])

    per_cat = conn.execute(
        "SELECT c.label, COUNT(q.id) AS n FROM categories c "
        "LEFT JOIN questions q ON q.category = c.code "
        "GROUP BY c.code ORDER BY c.sort_order"
    ).fetchall()
    return problems, n_q, n_o, per_cat, len(sample)


def main():
    questions = load_questions()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = get_conn()
    build(conn, questions)
    problems, n_q, n_o, per_cat, n_checked = verify(conn, questions)
    conn.close()

    print("Built %s" % DB_PATH)
    print("  questions: %d   options: %d" % (n_q, n_o))
    for label, n in per_cat:
        print("    %-28s %3d" % (label, n))
    if problems:
        print("\nVERIFICATION FAILED:")
        for p in problems:
            print("  - " + p)
        return 1
    print("  verified: counts match, 1 correct option per question, "
          "%d random questions read back identical to source" % n_checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
