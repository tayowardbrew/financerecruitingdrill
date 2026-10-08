"""Extract the question bank from the original single-file drill app into JSON.

The earlier version of this tool was one self-contained HTML file with the
question bank hardcoded as a JavaScript array (`const QUESTIONS = [...]`).
This script pulls that array out and writes it to data/questions.json, which
is what migrate.py loads into SQLite.

You only need this if you have the original HTML file. The extracted JSON is
committed to the repo, so a fresh clone can go straight to `python3 migrate.py`.

Usage:
    python3 extract_questions.py path/to/original_drill.html

How it works: the array is a JavaScript object literal, which is *almost* JSON.
The differences are unquoted keys (`id:` instead of `"id":`), comments, and
trailing commas. A small tokenizer walks the text, copies strings through
untouched, drops comments, quotes bare keys and removes trailing commas. The
result is then parsed with the standard json module, so a malformed entry fails
loudly instead of being silently skipped.
"""
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE_DIR, "data", "questions.json")

START_MARKER = "const QUESTIONS = ["
FIELD_ORDER = ["id", "cat", "difficulty", "estSec", "tag", "source",
               "q", "opts", "correct", "explain"]


def slice_array(src):
    """Return the text of the QUESTIONS array literal, brackets included."""
    start = src.find(START_MARKER)
    if start == -1:
        raise ValueError("Could not find 'const QUESTIONS = [' in the file.")
    i = start + len(START_MARKER) - 1  # position of the opening '['
    depth, in_str = 0, False
    j = i
    while j < len(src):
        ch = src[j]
        if in_str:
            if ch == "\\":
                j += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif src.startswith("//", j):
            j = src.index("\n", j)
            continue
        elif src.startswith("/*", j):
            j = src.index("*/", j) + 2
            continue
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    raise ValueError("Unterminated QUESTIONS array.")


def js_literal_to_json(text):
    """Convert a JS object/array literal (double-quoted strings only) to JSON text."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':                                   # copy string verbatim
            j = i + 1
            while text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):                  # line comment
            i = text.find("\n", i)
            i = n if i == -1 else i
        elif text.startswith("/*", i):                  # block comment
            i = text.index("*/", i) + 2
        elif ch.isalpha() or ch == "_":                 # bare identifier: a key
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[i:])
            word = m.group(0)
            if word in ("true", "false", "null"):
                out.append(word)
            else:
                out.append('"%s"' % word)
            i += len(word)
        else:
            out.append(ch)
            i += 1
    joined = "".join(out)
    # Remove trailing commas before a closing bracket/brace.
    return re.sub(r",(\s*[\]}])", r"\1", joined)


def validate(questions):
    """Fail loudly on anything migrate.py would choke on."""
    ids = set()
    for q in questions:
        for key in ("id", "cat", "difficulty", "estSec", "q", "opts", "correct", "explain"):
            if key not in q:
                raise ValueError("Question %r is missing %r" % (q.get("id"), key))
        if q["id"] in ids:
            raise ValueError("Duplicate question id %r" % q["id"])
        ids.add(q["id"])
        if not 0 <= q["correct"] < len(q["opts"]):
            raise ValueError("Question %r has an out-of-range correct index" % q["id"])
        if q["difficulty"] not in (1, 2, 3):
            raise ValueError("Question %r has difficulty %r" % (q["id"], q["difficulty"]))


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    with open(argv[1], "r", encoding="utf-8") as fh:
        src = fh.read()
    questions = json.loads(js_literal_to_json(slice_array(src)))
    validate(questions)
    # Stable key order so the committed JSON diffs cleanly.
    ordered = [{k: q[k] for k in FIELD_ORDER if k in q} for q in questions]
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("Extracted %d questions -> %s" % (len(ordered), OUT_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
