"""Tests for queries.sql, run against small hand-built fixtures.

Run:  python3 -m unittest discover tests

Each test builds a throwaway database from schema.sql + data/questions.json,
inserts a known attempt history, and checks a query's output against the
answer worked out by hand. Stdlib only (unittest + sqlite3).
"""
import os
import sys
import tempfile
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import migrate  # noqa: E402
from db import get_conn, load_queries, run_query  # noqa: E402

QUERIES = load_queries()


class QueryTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = get_conn(self.path)
        migrate.build(self.conn, migrate.load_questions())

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def q(self, name, **params):
        return run_query(self.conn, name, params, QUERIES)

    def answer(self, qid, correct, day, hhmm="12:00", seconds=30.0):
        """Record an attempt. `day`/`hhmm` are local time, stored as UTC like the app does."""
        right = self.conn.execute(
            "SELECT option_index FROM options WHERE question_id = ? AND is_correct = 1", (qid,)
        ).fetchone()[0]
        chosen = right if correct else (right + 1) % 4
        self.conn.execute(
            "INSERT INTO attempts (question_id, selected_option_index, is_correct, answered_at, "
            "time_taken_seconds) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', ?, 'utc'), ?)",
            (qid, chosen, int(correct), "%s %s:00" % (day, hhmm), seconds),
        )
        self.conn.commit()


class StreakTests(QueryTestCase):
    def test_empty_history(self):
        s = self.q("streak_summary", as_of="2026-10-07")[0]
        self.assertEqual((s["current_streak"], s["longest_streak"]), (0, 0))

    def test_gaps_and_islands(self):
        # Islands: Sep 1-3 (3 days), Sep 5 (1), Sep 8-11 (4), Oct 6-7 (2).
        days = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-05",
                "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
                "2026-10-06", "2026-10-07"]
        for d in days:
            self.answer("f1", True, d)
        self.answer("f2", False, "2026-09-09", "18:00")   # 2nd attempt same day: still 1 day
        s = self.q("streak_summary", as_of="2026-10-07")[0]
        self.assertEqual(s["current_streak"], 2)
        self.assertEqual(s["longest_streak"], 4)
        self.assertEqual((s["longest_start"], s["longest_end"]), ("2026-09-08", "2026-09-11"))
        self.assertEqual(s["days_practised"], 10)
        hist = self.q("streak_history", as_of="2026-10-07")
        self.assertEqual([h["length_days"] for h in hist], [2, 4, 1, 3])
        self.assertEqual([h["rest_days_before"] for h in hist], [24, 2, 1, None])

    def test_today_not_done_yet_keeps_streak(self):
        for d in ["2026-10-04", "2026-10-05", "2026-10-06"]:
            self.answer("f1", True, d)
        s = self.q("streak_summary", as_of="2026-10-07")[0]
        self.assertEqual(s["current_streak"], 3)      # yesterday still counts
        self.assertEqual(s["practised_today"], 0)

    def test_missed_two_days_breaks_streak(self):
        for d in ["2026-10-01", "2026-10-02", "2026-10-03"]:
            self.answer("f1", True, d)
        s = self.q("streak_summary", as_of="2026-10-07")[0]
        self.assertEqual(s["current_streak"], 0)
        self.assertEqual(s["longest_streak"], 3)

    def test_streak_across_month_boundary(self):
        for d in ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"]:
            self.answer("f1", True, d)
        s = self.q("streak_summary", as_of="2026-10-02")[0]
        self.assertEqual(s["current_streak"], 4)

    def test_late_night_session_counts_toward_local_day(self):
        # 23:30 local is the next calendar day in UTC; it must still count as the local day.
        self.answer("f1", True, "2026-10-05", "23:30")
        self.answer("f1", True, "2026-10-06", "23:30")
        s = self.q("streak_summary", as_of="2026-10-06")[0]
        self.assertEqual(s["current_streak"], 2)


class AccuracyTests(QueryTestCase):
    def test_weak_topics_order_and_last12(self):
        # DCF: 20 attempts, the 8 OLDEST wrong, the 12 most recent right -> all-time 60%, last-12 100%.
        for i in range(20):
            self.answer("dcf%d" % (i % 10 + 1), i >= 8, "2026-10-01", "10:%02d" % i)
        # M&A: 4 attempts, 1 right -> 25% / 25%.
        for i in range(4):
            self.answer("ma%d" % (i + 1), i == 0, "2026-10-01", "11:%02d" % i)
        rows = self.q("weak_topics")
        by_cat = {r["category"]: r for r in rows}
        self.assertEqual(by_cat["dcf"]["accuracy_pct"], 60.0)
        self.assertEqual(by_cat["dcf"]["last12_pct"], 100.0)
        self.assertEqual(by_cat["ma"]["last12_pct"], 25.0)
        self.assertEqual(rows[0]["category"], "ma")                 # worst first
        self.assertIsNone(rows[-1]["last12_pct"])                   # untested topics last
        self.assertEqual(by_cat["dcf"]["questions_seen"], 10)       # no fan-out double count
        self.assertEqual(by_cat["dcf"]["bank_size"], 39)

    def test_daily_trend_fills_gaps_and_weights_by_volume(self):
        for i in range(10):
            self.answer("f1", i < 9, "2026-10-01", "09:%02d" % i)  # 9/10
        self.answer("f2", False, "2026-10-04")                      # 0/1, two rest days between
        rows = self.q("accuracy_trend_daily")
        self.assertEqual([r["day"] for r in rows],
                         ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"])
        self.assertEqual(rows[1]["attempts"], 0)
        # Rolling 7d on Oct 4 = 9 / 11 = 81.8%, not the average of 90% and 0%.
        self.assertEqual(rows[-1]["rolling_7d_pct"], 81.8)

    def test_blocks(self):
        for i in range(25):
            self.answer("s1", i % 5 != 0, "2026-10-01", "%02d:%02d" % (8 + i // 60, i % 60))
        rows = self.q("accuracy_by_block")
        self.assertEqual([r["attempts"] for r in rows], [10, 10, 5])
        self.assertEqual([r["block_pct"] for r in rows], [80.0, 80.0, 80.0])
        self.assertEqual(rows[-1]["cumulative_pct"], 80.0)

    def test_hardest_question_and_favourite_distractor(self):
        right = self.conn.execute(
            "SELECT option_index FROM options WHERE question_id='ev7' AND is_correct=1").fetchone()[0]
        wrong = (right + 2) % 4
        for i, pick in enumerate([wrong, wrong, right]):
            self.conn.execute(
                "INSERT INTO attempts (question_id, selected_option_index, is_correct, answered_at) "
                "VALUES ('ev7', ?, ?, ?)", (pick, int(pick == right), "2026-10-0%dT12:00:00Z" % (i + 1)))
        self.conn.commit()
        top = self.q("hardest_questions")[0]
        text = self.conn.execute(
            "SELECT option_text FROM options WHERE question_id='ev7' AND option_index=?", (wrong,)
        ).fetchone()[0]
        self.assertEqual(top["question_id"], "ev7")
        self.assertEqual(top["miss_rate_pct"], 66.7)
        self.assertEqual(top["most_picked_wrong_answer"], text)

    def test_integrity_check_is_clean_then_catches_tampering(self):
        self.answer("f1", True, "2026-10-01")
        self.assertEqual(self.q("integrity_check"), [])
        self.conn.execute("UPDATE attempts SET is_correct = 0")
        self.assertEqual(len(self.q("integrity_check")), 1)


class NextRoundTests(QueryTestCase):
    def assertRoundShape(self, rows):
        ids = [r["question_id"] for r in rows]
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10)
        per_topic = Counter(r["category"] for r in rows)
        self.assertLessEqual(max(per_topic.values()), 3)
        self.assertGreaterEqual(len(per_topic), 5)
        self.assertLessEqual(sum(r["reason"] == "review" for r in rows), 2)

    def test_fresh_user_gets_a_mixed_round(self):
        for _ in range(20):                       # random jitter: check repeatedly
            rows = self.q("next_round", as_of="2026-10-07")
            self.assertRoundShape(rows)
            self.assertTrue(all(r["reason"] == "new" for r in rows))
            self.assertTrue(all(r["difficulty"] == 1 for r in rows))   # ease in

    def test_weak_topic_gets_more_slots_and_reviews_capped(self):
        # Miss 12 M&A questions yesterday; ace 12 questions in every other topic.
        for i in range(1, 13):
            self.answer("ma%d" % i, False, "2026-10-06", "09:%02d" % i)
        others = self.conn.execute(
            "SELECT category, id FROM questions WHERE category <> 'ma' ORDER BY bank_order").fetchall()
        seen = Counter()
        for cat, qid in others:
            if seen[cat] < 12:
                seen[cat] += 1
                self.answer(qid, True, "2026-10-06", "10:%02d" % (sum(seen.values()) % 60))
        ma_slots = Counter()
        for _ in range(20):
            rows = self.q("next_round", as_of="2026-10-07")
            self.assertRoundShape(rows)
            ma_slots[sum(r["category"] == "ma" for r in rows)] += 1
        self.assertEqual(set(ma_slots), {3})       # weakest topic always maxes its 3 slots

    def test_full_round_when_reviews_dominate(self):
        # Regression test: miss every Foundational question in every topic yesterday,
        # so review candidates outscore everything else. The round must still be
        # full (10 questions) with at most 2 reviews.
        rows = self.conn.execute("SELECT id FROM questions WHERE difficulty = 1").fetchall()
        for i, (qid,) in enumerate(rows):
            self.answer(qid, False, "2026-10-06", "%02d:%02d" % (8 + i // 60, i % 60))
        for _ in range(20):
            self.assertRoundShape(self.q("next_round", as_of="2026-10-07"))

    def test_nothing_from_today_while_alternatives_exist(self):
        self.answer("f1", False, "2026-10-07", "08:00")
        for _ in range(20):
            ids = [r["question_id"] for r in self.q("next_round", as_of="2026-10-07")]
            self.assertNotIn("f1", ids)

    def test_focus_drill_and_exhausted_topic(self):
        for qid in ["ind%d" % i for i in range(1, 20) if i != 7]:   # all 18 industry Qs, today
            self.answer(qid, True, "2026-10-07", "08:00")
        rows = self.q("next_round", as_of="2026-10-07", focus="ind")
        self.assertEqual(len(rows), 10)                 # falls back to repeats, never short
        self.assertTrue(all(r["category"] == "ind" for r in rows))


if __name__ == "__main__":
    unittest.main()
