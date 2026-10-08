"""Fill drill.db with SYNTHETIC practice history so the dashboard has data.

Run:  python3 seed_demo.py           (after python3 migrate.py)

This wipes the rounds and attempts tables and replaces them with ~8 weeks of
simulated practice. It is demo data: a made-up learner, not real results.
Skip it (or re-run migrate.py afterwards) if you want to track your own
practice from a clean slate.

How the simulation works:
  * Practice days are chosen at random (about 70% of days), with the last few
    days forced on so there is a live "current streak" to show.
  * Each round is selected by the real `next_round` query from queries.sql,
    run "as of" that simulated day. So the demo also exercises the adaptive
    SQL end to end.
  * The simulated learner has a starting skill level per topic that improves
    slowly with practice. Harder questions are less likely to be answered
    correctly. When wrong, the learner tends to fall for the same distractor,
    so the "most-picked wrong answer" analysis has a pattern to find.
"""
import random
import sys
from datetime import date, datetime, time, timedelta, timezone

from db import get_conn, load_queries, run_query, ROUND_SIZE

SEED = 20261007
DAYS = 56
PRACTICE_PROBABILITY = 0.7
FORCED_RECENT_DAYS = 6          # guarantees a current streak ending today

# Starting skill per topic (probability of a correct Applied-level answer).
START_SKILL = {
    "flow": 0.66, "lbo": 0.50, "stmt": 0.68, "ev": 0.55, "mult": 0.60,
    "dcf": 0.40, "ma": 0.36, "restr": 0.46, "ind": 0.80,
}
LEARNING_RATE = 0.005           # skill gained per attempt in a topic
SKILL_CAP = 0.93


def p_correct(skill, difficulty, reason):
    p = skill - 0.13 * (difficulty - 2)
    if reason == "review":
        p += 0.08                 # you've seen the explanation before
    return max(0.08, min(0.96, p))


def to_utc_iso(local_dt):
    """Naive local datetime -> ISO-8601 UTC string, as the app stores it."""
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    rng = random.Random(SEED)
    conn = get_conn()
    queries = load_queries()
    if conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0:
        print("No questions loaded. Run `python3 migrate.py` first.")
        return 1

    conn.execute("DELETE FROM attempts")
    conn.execute("DELETE FROM rounds")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('attempts', 'rounds')")

    # Per-question "favourite distractor" ordering, fixed for the whole run.
    opts = {}
    for row in conn.execute("SELECT question_id, option_index, is_correct FROM options"):
        opts.setdefault(row["question_id"], []).append((row["option_index"], row["is_correct"]))
    distractors = {}
    for qid, rows in opts.items():
        wrong = [i for i, c in rows if not c]
        rng.shuffle(wrong)
        distractors[qid] = wrong

    skill = dict(START_SKILL)
    today = date.today()
    first_day = today - timedelta(days=DAYS - 1)
    n_rounds = n_attempts = 0

    for offset in range(DAYS):
        day = first_day + timedelta(days=offset)
        forced = (today - day).days < FORCED_RECENT_DAYS
        if not forced and rng.random() > PRACTICE_PROBABILITY:
            continue

        clock = datetime.combine(day, time(rng.choice([7, 8, 12, 19, 20, 21]), rng.randint(0, 50)))
        rounds_today = 2 if rng.random() < 0.3 else 1
        for _ in range(rounds_today):
            if clock.date() != day:      # a late second round would spill past midnight
                break
            focus = rng.choice(["dcf", "ma", "lbo"]) if rng.random() < 0.15 else None
            picks = run_query(conn, "next_round",
                              {"as_of": day.isoformat(), "focus": focus,
                               "round_size": ROUND_SIZE}, queries)
            cur = conn.execute(
                "INSERT INTO rounds (started_at, focus_category, question_count) VALUES (?, ?, ?)",
                (to_utc_iso(clock), focus, len(picks)),
            )
            round_id = cur.lastrowid
            n_rounds += 1
            for pick in picks:
                qid, cat = pick["question_id"], pick["category"]
                est = conn.execute("SELECT est_seconds FROM questions WHERE id = ?", (qid,)).fetchone()[0]
                correct = rng.random() < p_correct(skill[cat], pick["difficulty"], pick["reason"])
                if correct:
                    chosen = [i for i, c in opts[qid] if c][0]
                else:
                    chosen = rng.choices(distractors[qid], weights=[5, 2, 1])[0]
                taken = round(est * rng.uniform(0.55, 1.7), 1)
                clock += timedelta(seconds=taken + rng.uniform(5, 25))
                conn.execute(
                    "INSERT INTO attempts (question_id, round_id, selected_option_index, "
                    "is_correct, answered_at, time_taken_seconds) VALUES (?, ?, ?, ?, ?, ?)",
                    (qid, round_id, chosen, int(correct), to_utc_iso(clock), taken),
                )
                skill[cat] = min(SKILL_CAP, skill[cat] + LEARNING_RATE)
                n_attempts += 1
            clock += timedelta(hours=rng.randint(2, 5))
        conn.commit()

    conn.close()
    print("Seeded SYNTHETIC demo history: %d rounds, %d attempts over %d days ending %s"
          % (n_rounds, n_attempts, DAYS, today.isoformat()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
