# Technical Drill: SQL Edition

A daily quiz for PE / investment banking technical interview prep (statement
flow, LBOs, 3-statement modelling, EV, multiples, DCF, M&A, restructuring,
industry knowledge), rebuilt on a **SQLite database with the analytics
written in SQL**.

Built with Python Flask + SQLite (stdlib `sqlite3`, no ORM) + plain
HTML/CSS/vanilla JS. No build step.

## Where this came from (and what's new)

The question bank (311 multiple-choice questions) and the quiz front end come
from **an earlier version of this tool**: a single self-contained HTML file
with the questions hardcoded in a JavaScript array and all progress kept in
the browser's `localStorage`, with the stats and adaptive logic written in
JavaScript.

**This rebuild is about the data layer.** The questions now live in a
normalised relational schema, every answer is logged to an `attempts` table,
and the statistics and adaptive question selection that used to be JavaScript
are now SQL queries in [`queries.sql`](./queries.sql). The quiz UI is adapted
from the original (same look, rewired to fetch from the Flask API instead of
the hardcoded array). The question content itself is unchanged.

## Run locally

```bash
cd tayo-technical-drill-sql
python3 -m pip install -r requirements.txt   # Flask only
python3 migrate.py                           # build drill.db from schema.sql + data/questions.json
python3 seed_demo.py                         # OPTIONAL: synthetic demo history (see below)
python3 app.py                               # serves http://127.0.0.1:5000
```

Open http://127.0.0.1:5000 to drill, and http://127.0.0.1:5000/stats for the
dashboard. Change the port with `PORT=8080 python3 app.py`.

- `migrate.py` **rebuilds the database from scratch**, so it wipes any answers
  you've logged. It also checks its own work: row counts match the source, every
  question has exactly one correct option, and a random sample of 25 questions
  read back out of SQLite matches the source JSON field for field.
- `seed_demo.py` fills the database with **synthetic** practice history (about 8
  weeks of a simulated learner) so the dashboard has something to show. It's
  made-up data, not real results. To track your own practice instead, just
  skip it (or re-run `migrate.py` to clear it).
- `drill.db` is generated and gitignored. Nothing to download.

Run the tests with `python3 -m unittest discover tests` (stdlib only).

## The schema

See [`schema.sql`](./schema.sql) for the full DDL with comments.

| Table | One row per | Key columns |
|-------|-------------|-------------|
| `categories` | topic (9) | `code` PK, `label`, `sample_scale` |
| `questions` | question (311) | `id` PK, `category` FK, `difficulty` (1-3), `est_seconds`, `question_text`, `explanation`, `concept_tag`, `source_ref` |
| `options` | answer choice (4 per question, 1,244 total) | (`question_id`, `option_index`) PK, `option_text`, `is_correct` |
| `rounds` | 10-question round served | `id` PK, `started_at`, `focus_category` FK (NULL = mixed round) |
| `attempts` | answer given | `id` PK, `question_id` FK, `round_id` FK, `selected_option_index`, `is_correct`, `answered_at`, `time_taken_seconds` |

Design choices worth calling out:

- **Answer choices are rows, not a JSON blob.** That's what makes "which wrong
  answer do I keep picking?" a simple JOIN.
- **A partial unique index** (`... ON options(question_id) WHERE is_correct = 1`)
  makes it impossible to store two correct answers for one question.
- **`attempts` is an append-only event log.** The original app kept running
  counters (misses per question, per-topic totals) that had to be updated in
  step. Here nothing is stored as a counter: every statistic is derived from
  the log at read time, so nothing can drift out of sync.
- **The server decides right or wrong.** The browser sends which option was
  picked; Flask looks up `options.is_correct`. The correct answer isn't sent to
  the browser until after you answer.
- `attempts.is_correct` is a deliberate, documented denormalisation (it could be
  derived from the chosen option). The `integrity_check` query proves the two
  always agree.

## The queries

All in [`queries.sql`](./queries.sql), each with a comment block explaining
what it does and why it's written that way. The Flask app loads them **by
name** and runs them unchanged, and the `/stats` dashboard shows the SQL under
each panel, so the file is the single source of truth.

| # | Query | What it answers | SQL techniques |
|---|-------|-----------------|----------------|
| 1 | `weak_topics` | Accuracy per topic, worst first, over all-time and your last 12 answers in that topic, plus coverage | `GROUP BY`, `LEFT JOIN` to keep untested topics, `ROW_NUMBER()` per topic for the last-12 window, conditional aggregation (`AVG(CASE ...)`), a separate CTE to avoid join fan-out |
| 2 | `accuracy_trend_daily` | Daily accuracy and a rolling 7-day accuracy | **Recursive CTE** to generate a calendar (so rest days aren't skipped), `SUM() OVER` with a named `WINDOW` frame, volume-weighted rather than averaging percentages |
| 3 | `accuracy_by_block` | Accuracy per block of 10 answers, 3-block rolling and cumulative | `ROW_NUMBER()` + integer-division bucketing, **window functions over aggregates** (`SUM(SUM(x)) OVER ...`) |
| 4 | `streak_summary` | Current and longest daily streak | **Gaps and islands**: `julianday(day) - ROW_NUMBER()` is constant within a run of consecutive days, so grouping by it yields each streak |
| 5 | `streak_history` | Every streak, with rest days between them | Same technique plus `LAG()` and `MAX() OVER ()` |
| 6 | `hardest_questions` | Highest miss rate, time against estimate, and the wrong answer you pick most | `HAVING`, ranking inside groups with `ROW_NUMBER()`, joining back to `options` to turn an index into text |
| 7 | `next_round` | **The adaptive engine**: which 10 questions to serve next | Nine chained CTEs: per-topic weakness weighting, target difficulty from accuracy bands, never-seen and review boosts, cooldowns, a review cap, a 3-per-topic cap with diminishing returns, a minimum of 5 topics, and `AS MATERIALIZED` so random jitter is evaluated once |
| 8 | `round_history` | Score and time per round | `LEFT JOIN` + `COALESCE` for mixed rounds, `HAVING` to hide abandoned rounds |
| 9 | `overview` | Headline numbers | Scalar subqueries |
| 10 | `integrity_check` | Data-quality assertions (empty result = healthy) | `UNION ALL` of anomaly checks, `NOT EXISTS` |

### How the streak query works

The hardest one. Number your practice days in order, then subtract that number
from the date:

| day | row_number | date minus row_number |
|-----|-----------:|-----------------------|
| Oct 1 | 1 | Sep 30 |
| Oct 2 | 2 | Sep 30 |
| Oct 3 | 3 | Sep 30 |
| *(two days off)* | | |
| Oct 6 | 4 | Oct 2 |
| Oct 7 | 5 | Oct 2 |

Inside an unbroken run, the date and the row number both go up by one, so the
difference stays the same. A gap moves the date forward without moving the
row number, so the difference changes. `GROUP BY` that difference and each
group is one streak, with `COUNT(*)` as its length. No loops, one pass. The
current streak is whichever island ends today or yesterday (today's practice
may not have happened yet, which matches the original app's behaviour).

### How the adaptive selection maps to the original

`next_round` reimplements the original JavaScript selection logic in SQL:

| Original JS behaviour | In `next_round` |
|---|---|
| Topic weight `0.6 + (1 - accuracy) x 3.0`, 1.6 if untested, scaled per topic | `topic_plan` CTE, same formula |
| Difficulty bands by last-12 accuracy (acing: push harder; struggling: basics) | `target_difficulty`, penalising distance from it |
| Prefer never-seen questions (x1.6); missed questions resurface | `reason` = new / review / repeat with matching multipliers |
| No repeats the same day; 15-round cooldown | Multiplier penalties for answered-today and answered-correctly-in-last-150 |
| Up to 2 review slots per round | `review_capped` CTE |
| Max 3 per topic, min 5 topics per mixed round | `topic_rank <= 3`, reserved slots for the top 5 topics |
| Weighted random sampling | Score x random jitter (0.75-1.25) |

Simplifications, stated plainly: the original's guaranteed difficulty mix per
round and its decaying per-question "review boost" counter aren't reproduced
exactly. The SQL version uses "was the latest attempt at this question wrong?"
in place of the counter. Penalties are soft multipliers, so a fully drilled
topic falls back to the least-recently-seen repeats instead of returning a
short round.

## Example output (synthetic demo data)

From one run of `seed_demo.py`. Numbers change from run to run because the
demo uses `next_round`'s random jitter.

`weak_topics` (worst first by last-12 accuracy):

| topic | attempts | accuracy_pct | last12_pct | coverage_pct |
|---|---|---|---|---|
| DCF | 77 | 55.8 | 75.0 | 100.0 |
| 3-Statement Lines | 57 | 75.4 | 75.0 | 100.0 |
| Restructuring | 55 | 60.0 | 75.0 | 100.0 |
| Multiples & Comps | 53 | 71.7 | 75.0 | 100.0 |
| EV & Equity Value | 58 | 69.0 | 83.3 | 100.0 |
| LBO & Debt | 93 | 72.0 | 91.7 | 100.0 |
| M&A / Accretion-Dilution | 77 | 63.6 | 91.7 | 100.0 |
| Statement Flow | 53 | 77.4 | 100.0 | 100.0 |
| Industry Knowledge | 17 | 100.0 | 100.0 | 94.4 |

M&A shows why the last-12 window matters: 63.6% all-time, but 91.7% recently,
so the drill has stopped prioritising it.

`streak_summary`:

| current_streak | longest_streak | longest_start | longest_end | days_practised |
|---|---|---|---|---|
| 7 | 9 | 2026-08-21 | 2026-08-29 | 43 |

`streak_history` (most recent five):

| start_day | end_day | length_days | rest_days_before | is_longest | is_current |
|---|---|---|---|---|---|
| 2026-10-01 | 2026-10-07 | 7 | 2 | 0 | 1 |
| 2026-09-26 | 2026-09-28 | 3 | 2 | 0 | 0 |
| 2026-09-20 | 2026-09-23 | 4 | 1 | 0 | 0 |
| 2026-09-18 | 2026-09-18 | 1 | 1 | 0 | 0 |
| 2026-09-10 | 2026-09-16 | 7 | 1 | 0 | 0 |

`hardest_questions` (top three):

| question_id | topic | concept | attempts | miss_rate_pct | most_picked_wrong_answer |
|---|---|---|---|---|---|
| dcf31 | DCF | Future capex in a DCF | 3 | 100.0 | $100 |
| ma34 | M&A | Contribution analysis | 3 | 100.0 | 50/50 is correct because it's a merger of equals |
| mult27 | Multiples | Private company discount | 3 | 100.0 | Precedent multiples are already lower |

`next_round` (DCF is weakest, so two DCF review questions lead; five topics get a reserved slot):

| question_id | topic | difficulty | target_difficulty | reason | score | topic_rank |
|---|---|---|---|---|---|---|
| dcf12 | DCF | 2 | 2 | review | 3.698 | 1 |
| s43 | 3-Statement Lines | 2 | 2 | repeat | 0.411 | 1 |
| mult9 | Multiples & Comps | 2 | 2 | repeat | 0.404 | 1 |
| restr20 | Restructuring | 2 | 2 | repeat | 0.3 | 1 |
| ev16 | EV & Equity Value | 2 | 3 | repeat | 0.161 | 1 |
| dcf22 | DCF | 2 | 2 | review | 3.663 | 2 |
| mult4 | Multiples & Comps | 2 | 2 | repeat | 0.4 | 2 |
| s10 | 3-Statement Lines | 2 | 2 | repeat | 0.397 | 2 |
| ma17 | M&A / Accretion-Dilution | 2 | 3 | repeat | 0.145 | 1 |
| ind18 | Industry Knowledge | 1 | 3 | new | 0.139 | 1 |

## Tests

`tests/test_queries.py` builds a throwaway database for each test, inserts a
hand-built attempt history and checks the query output against answers worked
out by hand. It covers:

- **Streaks:** empty history, multiple islands with same-day duplicates,
  "today not done yet", a two-day gap breaking the streak, a month boundary,
  and a late-night session that is the next day in UTC but the same local day.
- **Accuracy:** last-12 vs all-time, worst-first ordering, the coverage
  fan-out trap, rest days in the daily trend, volume-weighted rolling accuracy,
  block bucketing, and the favourite-distractor join.
- **Selection:** round shape (10 distinct questions, 3 or fewer per topic, 5 or
  more topics, 2 or fewer reviews), the weakest topic getting extra slots,
  nothing repeated the same day, an exhausted single-topic drill, and a
  regression test for a bug where review-heavy histories produced short rounds.
- **Integrity:** the check is clean, then catches a tampered row.

## Project structure

```
tayo-technical-drill-sql/
├── schema.sql              # DDL: 5 tables, constraints, indexes (commented)
├── queries.sql             # The analytical SQL: 10 named, commented queries
├── migrate.py              # Builds drill.db from schema.sql + data/questions.json, then verifies it
├── seed_demo.py            # Optional synthetic practice history for the dashboard
├── extract_questions.py    # One-off: pulls the question array out of the original HTML app
├── db.py                   # sqlite3 connection + loader that reads queries.sql by name
├── app.py                  # Flask: quiz page, /stats dashboard, JSON API
├── data/questions.json     # The question bank (extracted from the original app)
├── templates/              # base.html, index.html (quiz), stats.html (dashboard)
├── static/                 # style.css (from the original app + dashboard styles), app.js (quiz UI)
├── tests/test_queries.py   # unittest suite for the queries
└── requirements.txt        # Flask only
```

## API

- `POST /api/rounds` with `{}` (adaptive mixed round) or `{"focus": "dcf"}`
  (single-topic drill). Runs `next_round`, logs a `rounds` row and returns 10
  questions with their options, without the answers.
- `POST /api/attempts` with `{"question_id": "dcf14", "selected_option_index": 1,
  "round_id": 3, "time_taken_seconds": 42.5}`. Marks the answer on the server,
  logs it, and returns `is_correct`, `correct_option_index` and the explanation.
- `GET /api/stats`: every dashboard query's results as JSON.
- `GET /api/categories`, `GET /api/health`.

## Not carried over from the original

- The "report an issue" question-flagging feature.
- Importing progress saved in the original app's browser storage. History
  starts fresh in SQLite.
