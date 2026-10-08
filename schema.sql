-- =============================================================================
-- Technical Drill: SQLite schema
--
-- Five tables:
--   categories  - the 9 topics (Statement Flow, LBO, DCF, ...)
--   questions   - one row per question, FK -> categories
--   options     - one row per answer choice (4 per question), FK -> questions
--   rounds      - one row per 10-question round served to the user
--   attempts    - append-only log: one row per answer given, FK -> questions
--
-- Design notes
-- * Answer choices live in their own table rather than as a JSON blob on the
--   question. That keeps the data queryable: "which wrong answer is chosen
--   most often?" is a plain JOIN instead of string parsing.
-- * attempts is an append-only event log. Every statistic (accuracy, streaks,
--   which questions are due for review) is DERIVED from it with SQL at read
--   time. Nothing is stored as a running counter that could drift out of sync.
-- * attempts.is_correct is technically derivable (join selected_option_index to
--   options.is_correct). It is stored anyway, set by the server at write time,
--   because nearly every analytical query needs it and it keeps them readable.
--   queries.sql includes an integrity check that confirms the two agree.
-- * Timestamps are stored as ISO-8601 UTC text ('2026-10-07T14:03:00Z'), the
--   format SQLite's date functions understand. Queries convert to local time
--   with date(answered_at, 'localtime') when they group by calendar day.
-- =============================================================================

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS attempts;
DROP TABLE IF EXISTS rounds;
DROP TABLE IF EXISTS options;
DROP TABLE IF EXISTS questions;
DROP TABLE IF EXISTS categories;

-- -----------------------------------------------------------------------------
-- categories: lookup table for the 9 topics.
-- sample_scale mirrors the original app's per-topic weighting (Industry
-- Knowledge is non-technical, so it is served less often).
-- -----------------------------------------------------------------------------
CREATE TABLE categories (
    code          TEXT PRIMARY KEY,               -- 'flow', 'lbo', ...
    label         TEXT NOT NULL UNIQUE,           -- 'Statement Flow'
    short_label   TEXT NOT NULL,                  -- 'Flow'
    sort_order    INTEGER NOT NULL,
    sample_scale  REAL NOT NULL DEFAULT 1.0 CHECK (sample_scale > 0)
);

-- -----------------------------------------------------------------------------
-- questions: the question bank.
-- id keeps the original string ids ('f1', 'dcf14') so a question can be traced
-- back to the source file. concept_tag / source_ref are NULL for the original
-- 79 questions, which were written before those fields existed.
-- -----------------------------------------------------------------------------
CREATE TABLE questions (
    id             TEXT PRIMARY KEY,
    category       TEXT NOT NULL REFERENCES categories(code),
    difficulty     INTEGER NOT NULL CHECK (difficulty IN (1, 2, 3)),  -- 1 Foundational, 2 Applied, 3 Advanced
    est_seconds    INTEGER NOT NULL CHECK (est_seconds > 0),
    question_text  TEXT NOT NULL,
    explanation    TEXT NOT NULL,
    concept_tag    TEXT,
    source_ref     TEXT,
    bank_order     INTEGER NOT NULL UNIQUE            -- position in the original bank
);

CREATE INDEX idx_questions_category ON questions(category, difficulty);

-- -----------------------------------------------------------------------------
-- options: answer choices. option_index is the ORIGINAL position (0-3); the
-- UI shuffles display order, but attempts always record the original index.
-- -----------------------------------------------------------------------------
CREATE TABLE options (
    question_id   TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    option_index  INTEGER NOT NULL CHECK (option_index BETWEEN 0 AND 3),
    option_text   TEXT NOT NULL,
    is_correct    INTEGER NOT NULL DEFAULT 0 CHECK (is_correct IN (0, 1)),
    PRIMARY KEY (question_id, option_index)
);

-- A partial unique index: at most ONE row per question may have is_correct = 1.
-- (migrate.py separately checks that every question has exactly one.)
CREATE UNIQUE INDEX idx_options_one_correct ON options(question_id) WHERE is_correct = 1;

-- -----------------------------------------------------------------------------
-- rounds: a 10-question set served to the user. focus_category is NULL for a
-- mixed adaptive round, or a category code for a single-topic drill.
-- -----------------------------------------------------------------------------
CREATE TABLE rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    focus_category  TEXT REFERENCES categories(code),
    question_count  INTEGER NOT NULL CHECK (question_count > 0)
);

-- -----------------------------------------------------------------------------
-- attempts: one row per answered question. Append-only.
-- -----------------------------------------------------------------------------
CREATE TABLE attempts (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id            TEXT NOT NULL REFERENCES questions(id),
    round_id               INTEGER REFERENCES rounds(id),
    selected_option_index  INTEGER CHECK (selected_option_index BETWEEN 0 AND 3),
    is_correct             INTEGER NOT NULL CHECK (is_correct IN (0, 1)),
    answered_at            TEXT NOT NULL,                     -- ISO-8601 UTC
    time_taken_seconds     REAL CHECK (time_taken_seconds >= 0),
    FOREIGN KEY (question_id, selected_option_index)
        REFERENCES options(question_id, option_index)
);

CREATE INDEX idx_attempts_question ON attempts(question_id, answered_at);
CREATE INDEX idx_attempts_answered ON attempts(answered_at);
CREATE INDEX idx_attempts_round    ON attempts(round_id);
