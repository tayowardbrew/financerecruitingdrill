"""Flask backend for the technical drill.

Run:  python3 app.py   (serves on http://127.0.0.1:5000)

Pages
  /        the quiz (single-page UI; questions come from SQLite via the API)
  /stats   the analytics dashboard: every panel is a named query from queries.sql,
           shown alongside the SQL that produced it

JSON API
  GET  /api/categories         topics with question counts
  POST /api/rounds             build the next round (adaptive, or {"focus": "dcf"})
  POST /api/attempts           record an answer; the server marks it right/wrong
  GET  /api/stats              every dashboard query's results as JSON
  GET  /api/health             status + row counts

Stdlib sqlite3 only. No ORM. No build tooling.
"""
import os
import random

from flask import Flask, abort, jsonify, render_template, request

from db import DB_PATH, ROUND_SIZE, get_conn, load_queries, now_iso, run_query

app = Flask(__name__)

# Order and copy for the dashboard panels. Each key is a query name in queries.sql.
DASHBOARD = [
    ("weak_topics", "Weakest topics first",
     "Accuracy per topic over your last 12 answers in it (what the drill adapts to) and all-time, "
     "plus coverage of the question bank."),
    ("accuracy_trend_daily", "Accuracy by day",
     "Each calendar day's accuracy, with a rolling 7-day figure weighted by volume. Rest days stay "
     "in the series."),
    ("accuracy_by_block", "Accuracy every 10 answers",
     "Your history cut into fixed blocks of 10 attempts, with a 3-block rolling average and "
     "cumulative accuracy."),
    ("streak_history", "Streaks",
     "Every run of consecutive practice days, found with the gaps-and-islands technique."),
    ("hardest_questions", "Hardest questions",
     "Highest miss rate (2+ attempts), your time against the estimate, and the wrong answer you "
     "pick most."),
    ("next_round", "Next round preview",
     "What the adaptive selection query would serve right now, and why. Re-runs with fresh "
     "random jitter on every page load."),
    ("round_history", "Round history", "Score and time for each round played."),
    ("integrity_check", "Data integrity check",
     "Assertions that should return nothing. An empty table means the data is consistent."),
]


def _queries():
    # Re-read queries.sql on every request so edits show up on refresh
    # without restarting the server (handy while iterating on the SQL).
    return load_queries()


def _require_db():
    if not os.path.exists(DB_PATH):
        abort(503, description="drill.db not found. Run `python3 migrate.py` first.")


# --- pages ------------------------------------------------------------------

@app.route("/")
def index():
    _require_db()
    return render_template("index.html")


@app.route("/stats")
def stats_page():
    _require_db()
    queries = _queries()
    conn = get_conn()
    try:
        overview = run_query(conn, "overview", {}, queries)[0]
        streak = run_query(conn, "streak_summary", {}, queries)[0]
        panels = []
        for name, title, blurb in DASHBOARD:
            rows = run_query(conn, name, {}, queries)
            panels.append({
                "name": name, "title": title, "blurb": blurb, "rows": rows,
                "columns": list(rows[0].keys()) if rows else [], "sql": queries[name],
            })
    finally:
        conn.close()
    return render_template(
        "stats.html", overview=overview, streak=streak, panels=panels,
        overview_sql=queries["overview"], streak_sql=queries["streak_summary"],
    )


# --- API --------------------------------------------------------------------

@app.route("/api/categories")
def api_categories():
    _require_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT c.code, c.label, c.short_label, COUNT(q.id) AS question_count "
            "FROM categories c LEFT JOIN questions q ON q.category = c.code "
            "GROUP BY c.code ORDER BY c.sort_order"
        ).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/rounds", methods=["POST"])
def api_new_round():
    """Pick the next round with the `next_round` query and return the questions.

    The correct answer and explanation are deliberately NOT sent here; the client
    only learns them after submitting an answer to /api/attempts.
    """
    _require_db()
    payload = request.get_json(silent=True) or {}
    focus = payload.get("focus") or None
    conn = get_conn()
    try:
        if focus is not None and not conn.execute(
                "SELECT 1 FROM categories WHERE code = ?", (focus,)).fetchone():
            return jsonify({"error": "Unknown category %r" % focus}), 400

        picks = run_query(conn, "next_round", {"focus": focus, "round_size": ROUND_SIZE}, _queries())
        random.shuffle(picks)  # the query orders by priority; serve in random order
        cur = conn.execute(
            "INSERT INTO rounds (started_at, focus_category, question_count) VALUES (?, ?, ?)",
            (now_iso(), focus, len(picks)),
        )
        round_id = cur.lastrowid
        conn.commit()

        questions = []
        for p in picks:
            q = conn.execute(
                "SELECT q.id, q.category, c.label AS topic, q.difficulty, q.est_seconds, "
                "q.question_text FROM questions q JOIN categories c ON c.code = q.category "
                "WHERE q.id = ?", (p["question_id"],)
            ).fetchone()
            opts = conn.execute(
                "SELECT option_index, option_text FROM options WHERE question_id = ? "
                "ORDER BY option_index", (p["question_id"],)
            ).fetchall()
            item = dict(q)
            item["reason"] = p["reason"]
            item["options"] = [{"index": o["option_index"], "text": o["option_text"]} for o in opts]
            questions.append(item)
    finally:
        conn.close()
    return jsonify({"round_id": round_id, "focus": focus, "questions": questions})


@app.route("/api/attempts", methods=["POST"])
def api_record_attempt():
    """Record one answer. Correctness is decided here from the options table,
    never trusted from the browser."""
    _require_db()
    payload = request.get_json(silent=True) or {}
    qid = payload.get("question_id")
    selected = payload.get("selected_option_index")
    round_id = payload.get("round_id")
    taken = payload.get("time_taken_seconds")

    if (not isinstance(qid, str) or isinstance(selected, bool)
            or not isinstance(selected, int) or not 0 <= selected <= 3):
        return jsonify({"error": "question_id (string) and selected_option_index (0-3) are required"}), 400
    if isinstance(taken, bool) or not isinstance(taken, (int, float)) or taken < 0:
        taken = None
    elif taken > 3600:
        taken = 3600.0  # tab left open; cap so one stray value can't wreck averages

    conn = get_conn()
    try:
        chosen = conn.execute(
            "SELECT is_correct FROM options WHERE question_id = ? AND option_index = ?",
            (qid, selected),
        ).fetchone()
        if chosen is None:
            return jsonify({"error": "Unknown question %r" % qid}), 404
        if round_id is not None and not conn.execute(
                "SELECT 1 FROM rounds WHERE id = ?", (round_id,)).fetchone():
            round_id = None

        conn.execute(
            "INSERT INTO attempts (question_id, round_id, selected_option_index, is_correct, "
            "answered_at, time_taken_seconds) VALUES (?, ?, ?, ?, ?, ?)",
            (qid, round_id, selected, chosen["is_correct"], now_iso(), taken),
        )
        conn.commit()

        q = conn.execute(
            "SELECT explanation, concept_tag, source_ref FROM questions WHERE id = ?", (qid,)
        ).fetchone()
        correct_index = conn.execute(
            "SELECT option_index FROM options WHERE question_id = ? AND is_correct = 1", (qid,)
        ).fetchone()[0]
    finally:
        conn.close()
    return jsonify({
        "is_correct": bool(chosen["is_correct"]),
        "correct_option_index": correct_index,
        "explanation": q["explanation"],
        "concept_tag": q["concept_tag"],
        "source_ref": q["source_ref"],
    })


@app.route("/api/stats")
def api_stats():
    _require_db()
    queries = _queries()
    names = ["overview", "streak_summary"] + [name for name, _, _ in DASHBOARD]
    conn = get_conn()
    try:
        out = {name: run_query(conn, name, {}, queries) for name in names}
    finally:
        conn.close()
    return jsonify(out)


@app.route("/api/health")
def api_health():
    if not os.path.exists(DB_PATH):
        return jsonify({"status": "no database", "hint": "run python3 migrate.py"}), 503
    conn = get_conn()
    try:
        counts = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                  for t in ("categories", "questions", "options", "rounds", "attempts")}
    finally:
        conn.close()
    return jsonify({"status": "ok", "db": os.path.basename(DB_PATH), "rows": counts})


@app.errorhandler(503)
def no_db(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": e.description}), 503
    return "<p style='font-family:sans-serif'>%s</p>" % e.description, 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
