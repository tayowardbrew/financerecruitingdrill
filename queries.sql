-- =============================================================================
-- Technical Drill: analytical queries
--
-- Every statistic in the app comes from the queries in this file. app.py loads
-- them by name (each block starts with a "-- name: ..." line) and runs them
-- as-is, so what you read here is exactly what powers the dashboard.
--
-- Tables used (see schema.sql):
--   attempts(id, question_id, round_id, selected_option_index, is_correct,
--            answered_at, time_taken_seconds)      <- the append-only answer log
--   questions(id, category, difficulty, est_seconds, question_text, ...)
--   options(question_id, option_index, option_text, is_correct)
--   categories(code, label, short_label, sort_order, sample_scale)
--   rounds(id, started_at, focus_category, question_count)
--
-- Parameters use SQLite's :name syntax. Each one falls back to a default via
-- COALESCE, so passing NULL (or nothing) is always safe:
--   :as_of        'YYYY-MM-DD' to treat as "today" (default: today, local time)
--   :focus        a category code, to restrict to one topic (default: all)
--   :round_size   questions per round (default 10)
--   :block_size   attempts per block in the trend query (default 10)
--   :min_attempts minimum attempts before a question is ranked (default 2)
--   :limit        max rows returned (default varies per query)
--
-- Contents
--   1. weak_topics            Accuracy per topic, worst first
--   2. accuracy_trend_daily   Daily accuracy + rolling 7-day accuracy
--   3. accuracy_by_block      Accuracy per block of 10 attempts, rolling + cumulative
--   4. streak_summary         Current and longest daily streak (gaps-and-islands)
--   5. streak_history         Every streak, newest first
--   6. hardest_questions      Highest miss rate + the wrong answer people pick
--   7. next_round             Adaptive question selection for the next round
--   8. round_history          Score per round played
--   9. overview               Headline numbers for the dashboard
--  10. integrity_check        Data-quality assertions (should return no rows)
-- =============================================================================


-- =============================================================================
-- name: weak_topics
-- -----------------------------------------------------------------------------
-- WHAT: For every topic: attempts, all-time accuracy, accuracy over the most
-- recent 12 answers in that topic, and coverage (share of the topic's
-- questions seen at least once). Sorted weakest first.
--
-- WHY IT'S WRITTEN THIS WAY:
-- * "Last 12" is what the drill adapts to, because all-time accuracy is slow
--   to move once you have hundreds of answers. ROW_NUMBER() numbers each
--   topic's attempts from newest (1) to oldest; inside AVG(), a CASE keeps only
--   rows numbered 1-12 and turns the rest into NULL, which AVG ignores.
-- * AVG(is_correct) works as an accuracy rate because is_correct is 0 or 1.
-- * Coverage is computed in its own CTE. Doing it in the main query would join
--   attempts twice and inflate the counts (the classic "fan-out" bug).
-- * Starting FROM categories with a LEFT JOIN keeps topics you haven't
--   attempted yet in the output (with NULL accuracy) instead of dropping them.
-- * ORDER BY last12_pct IS NULL puts untested topics last; SQLite would
--   otherwise sort NULLs first in ascending order.
-- -----------------------------------------------------------------------------
WITH ranked AS (
    SELECT q.category,
           a.is_correct,
           ROW_NUMBER() OVER (
               PARTITION BY q.category
               ORDER BY a.answered_at DESC, a.id DESC
           ) AS recency_rank
    FROM attempts a
    JOIN questions q ON q.id = a.question_id
),
coverage AS (
    SELECT q.category,
           COUNT(DISTINCT q.id)          AS bank_size,
           COUNT(DISTINCT a.question_id) AS questions_seen
    FROM questions q
    LEFT JOIN attempts a ON a.question_id = q.id
    GROUP BY q.category
)
SELECT c.code                                         AS category,
       c.label                                        AS topic,
       COUNT(r.is_correct)                            AS attempts,
       COALESCE(SUM(r.is_correct), 0)                 AS correct,
       ROUND(100.0 * AVG(r.is_correct), 1)            AS accuracy_pct,
       ROUND(100.0 * AVG(CASE WHEN r.recency_rank <= 12
                              THEN r.is_correct END), 1) AS last12_pct,
       cv.questions_seen,
       cv.bank_size,
       ROUND(100.0 * cv.questions_seen / cv.bank_size, 1) AS coverage_pct
FROM categories c
JOIN coverage cv     ON cv.category = c.code
LEFT JOIN ranked r   ON r.category  = c.code
GROUP BY c.code, c.label, cv.questions_seen, cv.bank_size
ORDER BY last12_pct IS NULL, last12_pct ASC, attempts DESC;


-- =============================================================================
-- name: accuracy_trend_daily
-- -----------------------------------------------------------------------------
-- WHAT: One row per calendar day from your first practice day to your last:
-- that day's attempts and accuracy, plus a rolling 7-day accuracy.
--
-- WHY IT'S WRITTEN THIS WAY:
-- * A recursive CTE (calendar) generates every date in the range, including
--   days with no practice. Without it, "ROWS BETWEEN 6 PRECEDING" would mean
--   "the last 7 days you practised", not "the last 7 calendar days", and a
--   week off would silently disappear from the trend.
-- * The rolling figure is SUM(correct) / SUM(attempts) over the window, NOT
--   the average of the daily percentages. Averaging percentages would give a
--   2-question day the same weight as a 30-question day.
-- * NULLIF(..., 0) avoids dividing by zero for a 7-day window with no attempts.
-- * The named WINDOW clause (w) avoids repeating the frame definition.
-- * date(answered_at, 'localtime') converts the stored UTC timestamp to your
--   local calendar day, so a late-night session counts toward the right day.
-- -----------------------------------------------------------------------------
WITH RECURSIVE
daily AS (
    SELECT date(answered_at, 'localtime') AS day,
           COUNT(*)                       AS attempts,
           SUM(is_correct)                AS correct
    FROM attempts
    GROUP BY day
),
calendar(day) AS (
    SELECT MIN(day) FROM daily HAVING MIN(day) IS NOT NULL
    UNION ALL
    SELECT date(day, '+1 day') FROM calendar
    WHERE day < (SELECT MAX(day) FROM daily)
)
SELECT cal.day,
       COALESCE(d.attempts, 0)                         AS attempts,
       COALESCE(d.correct, 0)                          AS correct,
       ROUND(100.0 * d.correct / d.attempts, 1)        AS day_accuracy_pct,
       SUM(COALESCE(d.attempts, 0)) OVER w             AS attempts_7d,
       ROUND(100.0 * SUM(COALESCE(d.correct, 0)) OVER w
             / NULLIF(SUM(COALESCE(d.attempts, 0)) OVER w, 0), 1) AS rolling_7d_pct
FROM calendar cal
LEFT JOIN daily d ON d.day = cal.day
WINDOW w AS (ORDER BY cal.day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
ORDER BY cal.day;


-- =============================================================================
-- name: accuracy_by_block
-- -----------------------------------------------------------------------------
-- WHAT: Splits your full answer history into consecutive blocks of 10 attempts
-- (block 1 = your first 10 answers ever) and shows each block's accuracy, a
-- rolling average over the last 3 blocks, and cumulative accuracy to date.
--
-- WHY IT'S WRITTEN THIS WAY:
-- * Day-based trends are noisy when practice volume varies; fixed-size blocks
--   compare like with like. ROW_NUMBER() numbers every attempt in order, and
--   integer division (n - 1) / 10 turns 1-10 into block 0, 11-20 into block 1...
-- * The rolling and cumulative columns put window functions AROUND aggregates,
--   e.g. SUM(SUM(is_correct)) OVER (...). SQL evaluates GROUP BY first, then
--   windows run over the grouped rows, so the inner SUM is "this block" and
--   the outer SUM adds up blocks. The windows are ordered by MIN(n), the first
--   attempt number in each block, which is an aggregate and so is valid after
--   GROUP BY.
-- * Rolling/cumulative accuracy again divides summed correct by summed
--   attempts, so a short final block can't skew the figure.
-- -----------------------------------------------------------------------------
WITH numbered AS (
    SELECT a.*,
           ROW_NUMBER() OVER (ORDER BY a.answered_at, a.id) AS n
    FROM attempts a
)
SELECT (n - 1) / COALESCE(:block_size, 10) + 1           AS block,
       MIN(n)                                            AS from_attempt,
       MAX(n)                                            AS to_attempt,
       MIN(date(answered_at, 'localtime'))               AS from_day,
       MAX(date(answered_at, 'localtime'))               AS to_day,
       COUNT(*)                                          AS attempts,
       ROUND(100.0 * AVG(is_correct), 1)                 AS block_pct,
       ROUND(100.0 * SUM(SUM(is_correct)) OVER rolling
                   / SUM(COUNT(*))        OVER rolling, 1) AS rolling_3_block_pct,
       ROUND(100.0 * SUM(SUM(is_correct)) OVER running
                   / SUM(COUNT(*))        OVER running, 1) AS cumulative_pct
FROM numbered
GROUP BY block
WINDOW rolling AS (ORDER BY MIN(n) ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
       running AS (ORDER BY MIN(n) ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
ORDER BY block;


-- =============================================================================
-- name: streak_summary
-- -----------------------------------------------------------------------------
-- WHAT: Your current streak and your longest-ever streak, counted in
-- consecutive calendar days with at least one attempt. A streak still counts
-- as "current" if your last practice was yesterday (today isn't over yet),
-- matching the original app.
--
-- WHY IT'S WRITTEN THIS WAY ("gaps and islands"):
-- Each run of consecutive practice days is an "island". The trick to find them
-- without a loop: number the practice days in order with ROW_NUMBER(), then
-- subtract that number from the date itself.
--
--     day          julianday   row_number   julianday - row_number
--     2026-10-01     ...41        1            ...40   <- island A
--     2026-10-02     ...42        2            ...40   <- island A
--     2026-10-03     ...43        3            ...40   <- island A
--     (gap)
--     2026-10-06     ...46        4            ...42   <- island B
--     2026-10-07     ...47        5            ...42   <- island B
--
-- Within an unbroken run, the date and the row number both go up by 1 each
-- row, so their difference stays constant. Any gap bumps the date forward
-- without bumping the row number, which changes the difference. Grouping by
-- that difference gives one row per streak, and COUNT(*) is its length.
--
-- * SELECT DISTINCT on the day matters: three attempts on one day must count
--   as one practice day, or the row numbers would run ahead of the dates.
-- * :as_of lets you ask "what was my streak on date X?" (the test suite uses
--   it); attempts after that date are ignored.
-- -----------------------------------------------------------------------------
WITH
params AS (
    SELECT COALESCE(:as_of, date('now', 'localtime')) AS today
),
practice_days AS (
    SELECT DISTINCT date(answered_at, 'localtime') AS day
    FROM attempts
    WHERE date(answered_at, 'localtime') <= (SELECT today FROM params)
),
islands AS (
    SELECT day,
           julianday(day) - ROW_NUMBER() OVER (ORDER BY day) AS island_key
    FROM practice_days
),
streaks AS (
    SELECT MIN(day)  AS start_day,
           MAX(day)  AS end_day,
           COUNT(*)  AS length_days
    FROM islands
    GROUP BY island_key
)
SELECT
    COALESCE((SELECT s.length_days
              FROM streaks s, params p
              WHERE s.end_day >= date(p.today, '-1 day')), 0) AS current_streak,
    COALESCE((SELECT MAX(length_days) FROM streaks), 0)      AS longest_streak,
    (SELECT start_day FROM streaks
     ORDER BY length_days DESC, end_day DESC LIMIT 1)         AS longest_start,
    (SELECT end_day FROM streaks
     ORDER BY length_days DESC, end_day DESC LIMIT 1)         AS longest_end,
    (SELECT COUNT(*) FROM practice_days)                      AS days_practised,
    (SELECT COUNT(*) FROM practice_days
     WHERE day = (SELECT today FROM params))                  AS practised_today;


-- =============================================================================
-- name: streak_history
-- -----------------------------------------------------------------------------
-- WHAT: Every streak (island) you've had, newest first, flagging the longest
-- and the current one. Same gaps-and-islands technique as streak_summary;
-- see the explanation there.
-- -----------------------------------------------------------------------------
WITH
params AS (
    SELECT COALESCE(:as_of, date('now', 'localtime')) AS today
),
practice_days AS (
    SELECT DISTINCT date(answered_at, 'localtime') AS day
    FROM attempts
    WHERE date(answered_at, 'localtime') <= (SELECT today FROM params)
),
islands AS (
    SELECT day,
           julianday(day) - ROW_NUMBER() OVER (ORDER BY day) AS island_key
    FROM practice_days
),
streaks AS (
    SELECT MIN(day) AS start_day, MAX(day) AS end_day, COUNT(*) AS length_days
    FROM islands
    GROUP BY island_key
)
SELECT s.start_day,
       s.end_day,
       s.length_days,
       CAST(julianday(s.start_day)
            - julianday(LAG(s.end_day) OVER (ORDER BY s.start_day)) - 1
            AS INTEGER)                                       AS rest_days_before,
       s.length_days = MAX(s.length_days) OVER ()             AS is_longest,
       s.end_day >= date(p.today, '-1 day')                   AS is_current
FROM streaks s
CROSS JOIN params p
ORDER BY s.start_day DESC
LIMIT COALESCE(:limit, 20);


-- =============================================================================
-- name: hardest_questions
-- -----------------------------------------------------------------------------
-- WHAT: The questions you miss most often (by miss rate), with your average
-- time against the estimate, and the specific wrong answer you pick most.
--
-- WHY IT'S WRITTEN THIS WAY:
-- * HAVING filters on the aggregate (attempt count) after grouping. A question
--   missed once out of one attempt is a 100% miss rate but tells you little,
--   so questions need :min_attempts (default 2) to qualify.
-- * The "most common wrong answer" uses ROW_NUMBER() over a GROUP BY: count
--   how often each wrong option was picked per question, rank them, keep
--   rank 1. Ordering by option_index as a tie-break makes the result
--   deterministic when two wrong answers are tied.
-- * Then a JOIN back to options turns that option index into its actual text.
--   This is the payoff of storing options as rows instead of a JSON blob:
--   "which distractor fools me?" is a JOIN, not string parsing.
-- -----------------------------------------------------------------------------
WITH per_question AS (
    SELECT question_id,
           COUNT(*)                 AS attempts,
           SUM(1 - is_correct)      AS misses,
           AVG(time_taken_seconds)  AS avg_seconds
    FROM attempts
    GROUP BY question_id
    HAVING COUNT(*) >= COALESCE(:min_attempts, 2)
),
wrong_picks AS (
    SELECT question_id,
           selected_option_index,
           COUNT(*) AS times_picked,
           ROW_NUMBER() OVER (
               PARTITION BY question_id
               ORDER BY COUNT(*) DESC, selected_option_index
           ) AS pick_rank
    FROM attempts
    WHERE is_correct = 0 AND selected_option_index IS NOT NULL
    GROUP BY question_id, selected_option_index
)
SELECT q.id                                         AS question_id,
       c.short_label                                AS topic,
       q.difficulty,
       COALESCE(q.concept_tag, q.question_text)     AS concept,
       pq.attempts,
       pq.misses,
       ROUND(100.0 * pq.misses / pq.attempts, 1)    AS miss_rate_pct,
       ROUND(pq.avg_seconds)                        AS avg_seconds,
       q.est_seconds,
       o.option_text                                AS most_picked_wrong_answer,
       wp.times_picked
FROM per_question pq
JOIN questions q        ON q.id = pq.question_id
JOIN categories c       ON c.code = q.category
LEFT JOIN wrong_picks wp ON wp.question_id = q.id AND wp.pick_rank = 1
LEFT JOIN options o     ON o.question_id = wp.question_id
                       AND o.option_index = wp.selected_option_index
WHERE pq.misses > 0
ORDER BY miss_rate_pct DESC, pq.attempts DESC, q.id
LIMIT COALESCE(:limit, 10);


-- =============================================================================
-- name: next_round
-- -----------------------------------------------------------------------------
-- WHAT: Picks the questions for your next round. This is the app's adaptive
-- engine, rewritten from JavaScript into a single SQL query. It:
--   1. weights each topic by weakness (lower last-12 accuracy = more slots),
--   2. sets a target difficulty per topic (acing it -> Advanced; struggling ->
--      Foundational),
--   3. prefers never-seen questions and brings back questions you got wrong
--      last time,
--   4. avoids repeating anything answered today or answered correctly in your
--      last ~15 rounds (150 answers),
--   5. caps any one topic at 3 of the 10 slots and guarantees at least 5
--      different topics in a mixed round,
--   6. limits "review" questions (missed last time) to 2 per round, so a bad
--      day doesn't turn the next round into nothing but re-tests.
-- With :focus set, it builds a single-topic drill instead (no topic cap).
--
-- WHY IT'S WRITTEN THIS WAY:
-- * Every input is derived from the attempts log on the fly with window
--   functions (topic recency, question recency, global recency). No counters
--   are stored, so there is nothing to fall out of sync.
-- * The weights reproduce the original app's formulas: topic weight
--   = 0.6 + (1 - accuracy) x 3.0 (100% -> 0.6, 0% -> 3.6; 1.6 if untested),
--   scaled per topic; question weight = 1.6 if unseen, 2.2 if missed last time.
-- * Penalties are multipliers, not hard filters. If you drill a small topic
--   until it runs out, it gracefully falls back to the least-bad repeats
--   instead of returning fewer than 10 questions.
-- * The original app used weighted random sampling. Here each score is
--   multiplied by a random factor between 0.75 and 1.25 ("jitter") so rounds
--   vary while stronger candidates still usually win. random() returns a
--   64-bit integer, so dividing by 2^64 and adding 0.5 maps it to [0, 1).
-- * "scored" is declared AS MATERIALIZED so SQLite computes it once. Without
--   that hint the optimiser may inline the CTE and call random() again for
--   each reference, so a question's rank and its displayed score could
--   disagree.
-- * Diminishing returns per topic: the final ranking divides each score by its
--   rank within the topic (2nd pick from a topic counts half, 3rd a third), so
--   one very weak topic can't swallow the whole round. The first ORDER BY term
--   reserves a slot for the best question from each of the top 5 topics.
-- -----------------------------------------------------------------------------
WITH
params AS (
    SELECT COALESCE(:as_of, date('now', 'localtime')) AS today,
           :focus                                     AS focus
),
history AS (
    SELECT a.question_id,
           q.category,
           a.is_correct,
           a.answered_at,
           ROW_NUMBER() OVER (PARTITION BY q.category
                              ORDER BY a.answered_at DESC, a.id DESC) AS topic_recency,
           ROW_NUMBER() OVER (PARTITION BY a.question_id
                              ORDER BY a.answered_at DESC, a.id DESC) AS question_recency,
           ROW_NUMBER() OVER (ORDER BY a.answered_at DESC, a.id DESC) AS global_recency
    FROM attempts a
    JOIN questions q ON q.id = a.question_id
),
topic_form AS (
    SELECT c.code AS category,
           c.sample_scale,
           AVG(CASE WHEN h.topic_recency <= 12 THEN h.is_correct END) AS last12_acc,
           COUNT(h.question_id)                                       AS n_attempts
    FROM categories c
    LEFT JOIN history h ON h.category = c.code
    GROUP BY c.code, c.sample_scale
),
topic_plan AS (
    SELECT category,
           sample_scale * CASE WHEN last12_acc IS NULL THEN 1.6
                               ELSE 0.6 + (1 - last12_acc) * 3.0 END AS topic_weight,
           CASE WHEN n_attempts < 3     THEN 1   -- not enough data: ease in
                WHEN last12_acc >= 0.8  THEN 3   -- acing it: push harder
                WHEN last12_acc >= 0.6  THEN 2   -- solid: balanced
                ELSE 1                           -- shaky or struggling: basics
           END AS target_difficulty
    FROM topic_form
),
question_state AS (
    SELECT question_id,
           MAX(CASE WHEN question_recency = 1 THEN is_correct END) AS last_result,
           MAX(date(answered_at, 'localtime'))                     AS last_day,
           MIN(global_recency)                                     AS answers_ago
    FROM history
    GROUP BY question_id
),
scored AS MATERIALIZED (
    SELECT q.id,
           q.category,
           q.difficulty,
           tp.target_difficulty,
           CASE WHEN qs.question_id IS NULL THEN 'new'
                WHEN qs.last_result = 0     THEN 'review'
                ELSE 'repeat' END AS reason,
           tp.topic_weight
             * CASE WHEN qs.question_id IS NULL THEN 1.6     -- never seen
                    WHEN qs.last_result = 0     THEN 2.2     -- missed last time
                    ELSE 0.25 END                            -- seen and got right
             * CASE WHEN qs.last_day >= p.today THEN 0.01 ELSE 1 END     -- answered today
             * CASE WHEN qs.last_result = 1 AND qs.answers_ago <= 150
                    THEN 0.1 ELSE 1 END                                  -- cooldown
             / (1 + ABS(q.difficulty - tp.target_difficulty))            -- off-target level
             * (0.75 + 0.5 * (random() / 18446744073709551616.0 + 0.5)) -- jitter
           AS score
    FROM questions q
    JOIN topic_plan tp            ON tp.category = q.category
    CROSS JOIN params p
    LEFT JOIN question_state qs   ON qs.question_id = q.id
    WHERE p.focus IS NULL OR q.category = p.focus
),
review_capped AS (
    -- Keep only the 2 best review candidates. This MUST happen before the
    -- per-topic ranking below: if it ran afterwards, reviews occupying a
    -- topic's top 3 would be thrown away with nothing from that topic moving
    -- up to replace them, and the round would come back short.
    SELECT *
    FROM (SELECT s.*,
                 ROW_NUMBER() OVER (PARTITION BY reason = 'review'
                                    ORDER BY score DESC) AS reason_rank
          FROM scored s)
    WHERE reason <> 'review' OR reason_rank <= 2
),
ranked AS (
    SELECT rc.*,
           ROW_NUMBER() OVER (PARTITION BY category ORDER BY score DESC) AS topic_rank,
           MAX(score)   OVER (PARTITION BY category)                     AS topic_best
    FROM review_capped rc
),
ordered AS (
    SELECT r.*,
           DENSE_RANK() OVER (ORDER BY topic_best DESC, category) AS topic_order
    FROM ranked r
)
SELECT o.id                         AS question_id,
       o.category,
       c.label                      AS topic,
       o.difficulty,
       o.target_difficulty,
       o.reason,
       ROUND(o.score, 3)            AS score,
       o.topic_rank
FROM ordered o
JOIN categories c ON c.code = o.category
CROSS JOIN params p
WHERE p.focus IS NOT NULL OR o.topic_rank <= 3
ORDER BY CASE WHEN p.focus IS NULL AND o.topic_rank = 1 AND o.topic_order <= 5
              THEN 0 ELSE 1 END,
         o.score / o.topic_rank DESC
LIMIT COALESCE(:round_size, 10);


-- =============================================================================
-- name: round_history
-- -----------------------------------------------------------------------------
-- WHAT: One row per round played: date, mode, score and time taken.
--
-- WHY IT'S WRITTEN THIS WAY:
-- * LEFT JOIN to categories because a mixed round has no focus category
--   (focus_category IS NULL); COALESCE then labels it 'Mixed'.
-- * HAVING COUNT(a.id) > 0 hides rounds that were served but abandoned before
--   a single answer was given.
-- -----------------------------------------------------------------------------
SELECT r.id                                       AS round_id,
       date(r.started_at, 'localtime')            AS day,
       COALESCE(c.short_label || ' drill', 'Mixed') AS mode,
       COUNT(a.id)                                AS answered,
       r.question_count,
       SUM(a.is_correct)                          AS correct,
       ROUND(100.0 * AVG(a.is_correct), 1)        AS score_pct,
       CAST(ROUND(SUM(a.time_taken_seconds)) AS INTEGER) AS seconds
FROM rounds r
LEFT JOIN categories c ON c.code = r.focus_category
LEFT JOIN attempts a   ON a.round_id = r.id
GROUP BY r.id, r.started_at, c.short_label, r.question_count
HAVING COUNT(a.id) > 0
ORDER BY r.started_at DESC, r.id DESC
LIMIT COALESCE(:limit, 30);


-- =============================================================================
-- name: overview
-- -----------------------------------------------------------------------------
-- WHAT: Headline numbers for the top of the dashboard.
-- WHY: Scalar subqueries keep this to one row from several independent
-- aggregates without joining tables that would multiply each other's rows.
-- -----------------------------------------------------------------------------
SELECT (SELECT COUNT(*) FROM attempts)                          AS total_attempts,
       (SELECT ROUND(100.0 * AVG(is_correct), 1) FROM attempts) AS overall_accuracy_pct,
       (SELECT COUNT(DISTINCT question_id) FROM attempts)       AS questions_seen,
       (SELECT COUNT(*) FROM questions)                         AS bank_size,
       (SELECT COUNT(DISTINCT round_id) FROM attempts)          AS rounds_played,
       (SELECT ROUND(AVG(time_taken_seconds)) FROM attempts)    AS avg_seconds_per_answer;


-- =============================================================================
-- name: integrity_check
-- -----------------------------------------------------------------------------
-- WHAT: Data-quality assertions. Each branch returns a row only if something
-- is wrong, so an empty result means the data is consistent.
-- WHY: attempts.is_correct is stored for convenience even though it could be
-- derived from the chosen option (see schema.sql). This query proves the
-- stored value and the derived value agree, and that every question has
-- exactly four options with exactly one correct.
-- -----------------------------------------------------------------------------
SELECT 'attempt ' || a.id || ' is_correct disagrees with options table' AS problem
FROM attempts a
JOIN options o ON o.question_id = a.question_id
              AND o.option_index = a.selected_option_index
WHERE a.is_correct <> o.is_correct
UNION ALL
SELECT 'question ' || question_id || ' has ' || COUNT(*) || ' options and '
       || SUM(is_correct) || ' correct'
FROM options
GROUP BY question_id
HAVING COUNT(*) <> 4 OR SUM(is_correct) <> 1
UNION ALL
SELECT 'question ' || q.id || ' has no options'
FROM questions q
WHERE NOT EXISTS (SELECT 1 FROM options o WHERE o.question_id = q.id);
