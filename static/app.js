"use strict";
/* =======================================================================
   Technical Drill: quiz front end.

   Adapted from the original single-file app. The big change: there is no
   hardcoded question bank and no localStorage state. Questions come from
   SQLite via the Flask API, every answer is POSTed to /api/attempts (where the
   server decides right/wrong), and all stats are read back from /api/stats,
   which runs the queries in queries.sql.
   ======================================================================= */

const CAT_ICON = {
  flow: "\u{1F501}", lbo: "\u{1F3E6}", stmt: "\u{1F4CA}", ev: "⚖️", mult: "\u{1F4D0}",
  dcf: "⏳", ma: "\u{1F91D}", restr: "\u{1F6DF}", ind: "\u{1F3DB}️"
};
const DIFF_LABEL = { 1: "Foundational", 2: "Applied", 3: "Advanced" };
const LONG_SEC = 180;

let categories = [];      // [{code, label, short_label, question_count}]
let stats = null;         // /api/stats payload
let session = null;       // the round in progress
let view = "home";
let timerHandle = null;

/* ---------------- helpers ---------------- */
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function fmtClock(sec) { sec = Math.max(0, Math.round(sec)); return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0"); }
function fmtMin(sec) { return "~" + Math.max(1, Math.round(sec / 60)) + " min"; }
function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}
function accColor(p) { return p === null ? "var(--faint)" : p >= 80 ? "var(--green-deep)" : p >= 50 ? "var(--amber-dk)" : "var(--red-dk)"; }
function accBarColor(p) { return p >= 80 ? "var(--green)" : p >= 50 ? "var(--amber)" : "var(--red)"; }
function catLabel(code) { const c = categories.find(x => x.code === code); return c ? c.label : code; }
function catShort(code) { const c = categories.find(x => x.code === code); return c ? c.short_label : code; }

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("Request failed: " + res.status));
  return data;
}
async function refreshStats() { stats = await api("/api/stats"); }
function showError(err) {
  document.getElementById("main").innerHTML = `<section class="panel"><div class="notice">${esc(err.message || err)}</div></section>`;
}

/* ---------------- sidebar ---------------- */
function renderSidebar() {
  const o = stats.overview[0], s = stats.streak_summary[0];
  const pct = Math.round(o.questions_seen / o.bank_size * 100);
  document.getElementById("sideProgress").innerHTML = `
    <div class="overall" title="Questions answered at least once">
      <div class="overall-top"><span class="overall-lab">Bank coverage</span><span class="overall-pct">${pct}%</span></div>
      <div class="bigbar"><div style="width:${pct}%"></div></div>
      <div class="overall-sub">${o.questions_seen} of ${o.bank_size} questions seen</div>
    </div>`;
  document.getElementById("sideStats").innerHTML = `
    <div class="stat"><div class="ic streak">\u{1F525}</div><div><div class="lab">Streak</div><div class="val">${s.current_streak}<small> day${s.current_streak === 1 ? "" : "s"}</small></div></div></div>
    <div class="stat"><div class="ic acc">\u{1F3AF}</div><div><div class="lab">All-time accuracy</div><div class="val">${o.overall_accuracy_pct === null ? "&mdash;" : o.overall_accuracy_pct + "%"}</div></div></div>
    <div class="stat"><div class="ic done">\u{1F4DA}</div><div><div class="lab">Rounds played</div><div class="val">${o.rounds_played}</div></div></div>`;
}

/* ---------------- home ---------------- */
function greeting(streak) {
  if (streak === 0) return "Ready to drill?";
  if (streak >= 7) return "On a serious roll.";
  if (streak >= 3) return "Back at it, nice streak.";
  return "Welcome back.";
}

function renderHome() {
  view = "home";
  stopTimer();
  const streak = stats.streak_summary[0].current_streak;
  const topics = stats.weak_topics;
  const tested = topics.filter(t => t.attempts > 0);
  const weakest = tested[0];

  const leanHtml = !weakest ? `
      <div class="lean"><div class="wic">\u{1F44B}</div><div>
        <div class="wlab">First run</div>
        <div class="wtxt">No results yet. <span>Play a round and the drill starts adapting to where you're weakest.</span></div>
      </div></div>` : `
      <div class="lean"><div class="wic">\u{1F3AF}</div><div>
        <div class="wlab">Weakest right now</div>
        <div class="wtxt">${esc(weakest.topic)} <span>&middot; ${weakest.last12_pct}% over your last 12 answers &middot; the next round gives it extra slots</span></div>
      </div></div>`;

  const rankHtml = tested.length ? tested.map((t, i) => `
      <div class="rank-row">
        <div class="rank-n">${i + 1}</div>
        <div class="topic-chip c-${t.category}" style="width:30px;height:30px;font-size:.95rem;border-radius:9px;">${CAT_ICON[t.category]}</div>
        <div style="min-width:0">
          <div class="rank-name">${esc(t.topic)}</div>
          <div class="minibar"><div style="width:${t.last12_pct}%;background:${accBarColor(t.last12_pct)}"></div></div>
          <div class="rank-meta">${t.attempts - t.correct} miss${t.attempts - t.correct === 1 ? "" : "es"} all-time &middot; ${t.attempts} answered</div>
        </div>
        <div class="rank-pct" style="color:${accColor(t.last12_pct)}">${t.last12_pct}%<div class="rank-meta">last 12</div></div>
      </div>`).join("") : `<div class="empty">Topics will be ranked here once you've answered a few questions.</div>`;

  const hard = stats.hardest_questions.slice(0, 7);
  const missHtml = hard.length ? hard.map(h => `
      <details class="miss">
        <summary>
          <span class="miss-count">missed ${h.misses}/${h.attempts}</span>
          <span><span class="miss-txt">${esc(h.concept.length > 90 ? h.concept.slice(0, 87) + "..." : h.concept)}</span><br><span class="miss-sub">${CAT_ICON[categories.find(c => c.short_label === h.topic)?.code] || ""} ${esc(h.topic)} &middot; ${h.question_id}</span></span>
        </summary>
        <div class="miss-body">${h.most_picked_wrong_answer ? `<b>Your most-picked wrong answer:</b> ${esc(h.most_picked_wrong_answer)}` : ""}</div>
      </details>`).join("") : `<div class="empty">Nothing missed twice yet. Questions you keep getting wrong will show up here.</div>`;

  const byCat = {}; topics.forEach(t => { byCat[t.category] = t; });
  const cards = categories.map(c => {
    const t = byCat[c.code];
    return `
      <button type="button" class="topic-card" data-drill="${c.code}" aria-label="Drill ${esc(c.label)}">
        <div class="topic-head">
          <div class="topic-chip c-${c.code}">${CAT_ICON[c.code]}</div>
          <div><div class="topic-name">${esc(c.label)}</div><div class="topic-count">${c.question_count} questions</div></div>
        </div>
        <div class="bar-row"><div class="bar-lab">Coverage</div><div class="bar-track"><div class="bar-fill cov" style="width:${t.coverage_pct}%"></div></div><div class="bar-pct">${Math.round(t.coverage_pct)}%</div></div>
        <div class="bar-row"><div class="bar-lab">Last 12</div><div class="bar-track"><div class="bar-fill acc" style="width:${t.last12_pct || 0}%"></div></div><div class="bar-pct">${t.last12_pct === null ? "&mdash;" : Math.round(t.last12_pct) + "%"}</div></div>
        <div class="topic-foot">
          <span class="topic-alltime">All-time: ${t.accuracy_pct === null ? "not tested" : Math.round(t.accuracy_pct) + "% of " + t.attempts}</span>
          <span class="drill-pill">Drill &rarr;</span>
        </div>
      </button>`;
  }).join("");

  document.getElementById("main").innerHTML = `
    <section class="panel">
      <div class="eyebrow">${streak > 0 ? "\u{1F525} " + streak + " day streak &middot; keep it going" : "Daily technicals"}</div>
      <h1 class="page-title">${greeting(streak)}</h1>
      <p class="page-lead">A 10-question round across ${categories.length} topics, chosen by a SQL query that reads your answer history: harder where you're strong, more reps where you're weak, and questions you missed come back for review.</p>
      ${leanHtml}
      <div class="cta-row">
        <button type="button" class="play" id="playBtn">${tested.length ? "Start today's drill" : "Play now"} <span aria-hidden="true">&rarr;</span></button>
        <a class="cta-note" href="/stats#next_round">See how the next round is picked &rarr;</a>
      </div>
    </section>
    <section class="panel">
      <h2 class="section-h">What you're getting wrong</h2>
      <p class="section-sub">Topics ranked by recent accuracy (last 12 answers per topic), plus the questions with the highest miss rate.</p>
      <div class="diag-grid">
        <div class="diag-card"><div class="diag-title">\u{1F4C9} Weakest topics first</div>${rankHtml}</div>
        <div class="diag-card"><div class="diag-title">\u{1F9E9} Most-missed questions</div>${missHtml}</div>
      </div>
    </section>
    <section class="panel">
      <h2 class="section-h">Your topics</h2>
      <p class="section-sub">Tap a topic to drill 10 questions from it alone. Coverage = share of that topic's questions seen at least once.</p>
      <div class="topics">${cards}</div>
    </section>`;

  document.getElementById("playBtn").onclick = () => startRound(null);
  document.querySelectorAll("[data-drill]").forEach(el => { el.onclick = () => startRound(el.dataset.drill); });
}

/* ---------------- quiz ---------------- */
async function startRound(focus) {
  try {
    const round = await api("/api/rounds", { focus });
    session = {
      roundId: round.round_id, focus: round.focus, set: round.questions,
      orders: round.questions.map(q => shuffle(q.options.map(o => o.index))),
      idx: 0, correctCount: 0, catTotals: {}, catCorrect: {}, missed: [],
      estSec: round.questions.reduce((s, q) => s + q.est_seconds, 0),
      startTs: Date.now(), endTs: null, answered: false, qStart: Date.now()
    };
    round.questions.forEach(q => { session.catTotals[q.category] = (session.catTotals[q.category] || 0) + 1; session.catCorrect[q.category] = session.catCorrect[q.category] || 0; });
    renderQuestion();
    window.scrollTo(0, 0);
  } catch (err) { showError(err); }
}

function startTimer() {
  stopTimer();
  const tick = () => {
    const el = document.getElementById("timer");
    if (!el || !session) return stopTimer();
    el.innerHTML = "⏱ " + fmtClock((Date.now() - session.startTs) / 1000) + " <small>/ est. " + fmtMin(session.estSec) + "</small>";
  };
  tick();
  timerHandle = setInterval(tick, 1000);
}
function stopTimer() { if (timerHandle) { clearInterval(timerHandle); timerHandle = null; } }

function renderQuestion() {
  view = "quiz";
  if (session.idx >= session.set.length) { session.endTs = session.endTs || Date.now(); return renderSummary(); }
  const q = session.set[session.idx];
  const pct = Math.round(session.idx / session.set.length * 100);
  const long = q.est_seconds > LONG_SEC;
  session.answered = false;
  session.qStart = Date.now();
  document.getElementById("main").innerHTML = `
    <section class="panel">
      <div class="quiz-top">
        <div class="qcount">Q ${session.idx + 1} / ${session.set.length}</div>
        <div class="qprog-track"><div class="qprog-fill" style="width:${pct}%"></div></div>
        <div class="timer" id="timer" aria-live="off"></div>
      </div>
      <div class="qtags">
        <span class="tag c-${q.category}">${CAT_ICON[q.category]} ${esc(q.topic)}</span>
        <span class="tag tag-plain">${DIFF_LABEL[q.difficulty]}</span>
        <span class="tag tag-plain">${long ? "⏱ long &middot; " : ""}~${q.est_seconds >= 90 ? Math.round(q.est_seconds / 60 * 2) / 2 + " min" : q.est_seconds + "s"}</span>
        ${q.reason === "review" ? `<span class="tag tag-review">↻ Review: missed before</span>` : ""}
        ${session.focus ? `<span class="tag tag-plain">Topic drill</span>` : ""}
      </div>
      <div class="qtext">${esc(q.question_text)}</div>
      <div id="opts"></div>
      <div class="explain" id="explainBox"></div>
      <div class="btnrow" id="nextRow" style="display:none;">
        <button type="button" class="play blue" id="nextBtn">${session.idx + 1 >= session.set.length ? "See results" : "Next question"} <span aria-hidden="true">&rarr;</span></button>
      </div>
      <div class="btnrow" style="margin-top:10px;"><button type="button" class="textlink" id="quitBtn">Exit round</button></div>
    </section>`;
  const optsDiv = document.getElementById("opts");
  const textByIndex = {}; q.options.forEach(o => { textByIndex[o.index] = o.text; });
  session.orders[session.idx].forEach((orig, disp) => {
    const btn = document.createElement("button");
    btn.className = "opt"; btn.type = "button"; btn.dataset.orig = orig;
    btn.innerHTML = `<span class="mk" aria-hidden="true">${"ABCD"[disp]}</span><span>${esc(textByIndex[orig])}</span>`;
    btn.onclick = () => answerQuestion(q, orig, btn, optsDiv);
    optsDiv.appendChild(btn);
  });
  document.getElementById("nextBtn").onclick = () => { session.idx++; renderQuestion(); window.scrollTo(0, 0); };
  document.getElementById("quitBtn").onclick = async () => { session = null; await goHome(); };
  startTimer();
}

async function answerQuestion(q, chosenOrig, btnEl, optsDiv) {
  if (session.answered) return;
  session.answered = true;
  const buttons = [...optsDiv.querySelectorAll(".opt")];
  buttons.forEach(b => { b.disabled = true; });
  let res;
  try {
    res = await api("/api/attempts", {
      question_id: q.id, selected_option_index: chosenOrig, round_id: session.roundId,
      time_taken_seconds: Math.round((Date.now() - session.qStart) / 100) / 10
    });
  } catch (err) {
    session.answered = false;
    buttons.forEach(b => { b.disabled = false; });
    alert("Couldn't save that answer: " + err.message);
    return;
  }
  const correctBtn = buttons.find(b => Number(b.dataset.orig) === res.correct_option_index);
  if (res.is_correct) {
    btnEl.classList.add("correct"); btnEl.querySelector(".mk").textContent = "✓";
    session.correctCount++; session.catCorrect[q.category]++;
  } else {
    btnEl.classList.add("incorrect"); btnEl.querySelector(".mk").textContent = "✕";
    correctBtn.classList.add("reveal"); correctBtn.querySelector(".mk").textContent = "✓";
    session.missed.push({ q, correctText: q.options.find(o => o.index === res.correct_option_index).text, explanation: res.explanation });
  }
  const box = document.getElementById("explainBox");
  box.classList.add("show");
  box.innerHTML = `<span class="verdict ${res.is_correct ? "ok" : "no"}">${res.is_correct ? "Correct." : "Not quite."}</span> ${esc(res.explanation)}
    <div class="explain-foot"><span>${res.source_ref ? "Source: " + esc(res.source_ref) : "Original drill question"} &middot; ${esc(q.id)}</span></div>`;
  document.getElementById("nextRow").style.display = "flex";
}

/* ---------------- summary ---------------- */
async function renderSummary() {
  view = "summary";
  stopTimer();
  try { await refreshStats(); renderSidebar(); } catch (err) { /* summary still renders from session */ }
  const total = session.set.length, score = session.correctCount;
  const pct = total ? Math.round(score / total * 100) : 0;
  const cls = pct >= 80 ? "good" : pct >= 50 ? "mid" : "low";
  const sec = Math.round((session.endTs - session.startTs) / 1000);
  const emoji = pct >= 80 ? "\u{1F389}" : pct >= 50 ? "\u{1F4AA}" : "\u{1F4D8}";
  const headline = pct >= 80 ? "Strong round." : pct >= 50 ? "Solid work." : "Good reps, review below.";
  const last12 = {}; (stats ? stats.weak_topics : []).forEach(t => { last12[t.category] = t.last12_pct; });
  const catCards = categories.filter(c => session.catTotals[c.code]).map(c => {
    const cc = session.catCorrect[c.code], ct = session.catTotals[c.code], ra = Math.round(cc / ct * 100);
    const recent = last12[c.code] ?? null;
    let tcls = "trend-flat", tico = "→", trend = "on your average";
    if (recent !== null) {
      if (ra > recent + 5) { tcls = "trend-up"; tico = "↑"; trend = "above your last-12"; }
      else if (ra < recent - 5) { tcls = "trend-down"; tico = "↓"; trend = "below your last-12"; }
    }
    return `<div class="cb"><div class="cbn">${CAT_ICON[c.code]} ${esc(c.short_label)}</div><div class="cbv">${cc}/${ct}</div>
      <div class="cbt" style="color:${accColor(ra)}">${ra}%</div><div class="cbt ${tcls}">${tico} ${trend}</div></div>`;
  }).join("");
  const missedHtml = session.missed.length ? `
    <section class="panel">
      <h2 class="section-h">Review what you missed (${session.missed.length})</h2>
      <p class="section-sub">These come back as review questions in a later round (not today), until you answer them correctly.</p>
      ${session.missed.map(m => `
        <div class="missed-item">
          <div class="missed-q">${esc(m.q.question_text)}</div>
          <div class="missed-a">✓ ${esc(m.correctText)}</div>
          <div class="missed-explain">${esc(m.explanation)}</div>
        </div>`).join("")}
    </section>` : "";
  document.getElementById("main").innerHTML = `
    <section class="panel summary-hero">
      <div class="celebrate">${emoji}</div>
      <div class="score-big ${cls}">${score}/${total}</div>
      <div class="score-cap">${headline} That's ${pct}% ${session.focus ? "on " + esc(catLabel(session.focus)) : "this round"} in ${fmtClock(sec)} (estimate was ${fmtMin(session.estSec)}).</div>
      <div class="catbreak">${catCards}</div>
      <div class="btnrow" style="justify-content:center;margin-top:24px;">
        <button type="button" class="play" id="againBtn">${session.focus ? "Another " + esc(catShort(session.focus)) + " set" : "Extra practice set"}</button>
        <button type="button" class="ghost" id="homeBtn">Back to home</button>
        <a class="ghost" href="/stats">View stats &amp; SQL</a>
      </div>
    </section>
    ${missedHtml}`;
  const focus = session.focus;
  document.getElementById("againBtn").onclick = () => startRound(focus);
  document.getElementById("homeBtn").onclick = async () => { session = null; await goHome(); };
}

/* ---------------- boot ---------------- */
async function goHome() {
  try {
    await refreshStats();
    renderSidebar();
    renderHome();
    window.scrollTo(0, 0);
  } catch (err) { showError(err); }
}

(async function boot() {
  try {
    categories = await api("/api/categories");
    await goHome();
  } catch (err) { showError(err); }
})();
