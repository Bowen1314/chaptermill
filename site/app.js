"use strict";

// Tiny DOM helper: h("div", {class: "x"}, "text", child, ...). Text is always set as text.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function ts(sec) {
  sec = Math.max(0, Math.floor(sec));
  const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}
const mins = (s) => `${Math.round(s / 60)} min`;
const hours = (s) => `${(s / 3600).toFixed(1)} h`;
const usd = (x, d = 4) => `$${Number(x).toFixed(d)}`;
const num = (x) => Number(x).toLocaleString("en-US");

let DATA = null;
let current = null; // {audio, marks: [{start, el}]}

function kpis(run, chaos) {
  const box = document.getElementById("kpis");
  const items = [
    [`${run.episodes} episodes`, `${hours(run.audio_s)} of audio, ${num(run.words)} words transcribed`],
    [usd(run.usd, 3), `paid for open-weight model calls (${usd(run.usd_per_audio_hour, 3)} per audio hour)`],
    [`${(run.audio_s / run.transcribe_s).toFixed(1)}× real time`, "Whisper base.en on 2 threads of an i3-6100"],
  ];
  if (chaos) {
    items.push([`${chaos.kills} kills`, `${chaos.ledger.duplicate_completions} model replies bought twice`]);
  }
  box.replaceChildren(...items.map(([b, s]) => h("div", { class: "kpi" }, h("b", {}, b), h("span", {}, s))));
}

function runNote(run) {
  const box = document.getElementById("runnote");
  if (!box || !run.first_run_failed || !run.first_run_failed.length) return;
  const keys = run.first_run_failed.map((k) => k.toUpperCase()).join(" and ");
  box.replaceChildren(h("p", { class: "muted" },
    `Honest note: the first run finished ${run.episodes - run.first_run_failed.length} of ${run.episodes}. For ${keys} (both over 30 minutes) the agent spent all ten of its turns reading the transcript window by window and never submitted. I added a warning three turns before the limit and re-ran just those two: every transcript chunk and the first nine model replies of each came from cache, and only the final turn was new. `,
    `Total paid for all nine, including those wasted turns: ${usd(run.usd, 3)} for ${num(run.completions)} model replies, none bought twice.`));
}

function episodeList() {
  const nav = document.getElementById("eplist");
  nav.replaceChildren(
    ...DATA.episodes.map((ep) =>
      h("button", { type: "button", "data-key": ep.key, onclick: () => show(ep.key, true) },
        ep.title.replace(/^HPR\d+:\s*/, ""),
        h("small", {}, `${ep.key.toUpperCase()} · ${ep.author || "HPR"} · ${mins(ep.stats.duration_s)}`))
    )
  );
}

function jumpButton(start, ...content) {
  return h("button", { type: "button", class: "jump", onclick: () => seek(start) }, ...content);
}

function seek(start) {
  if (!current) return;
  const a = current.audio;
  const go = () => { a.currentTime = start; a.play().catch(() => {}); };
  if (a.readyState >= 1) go();
  else { a.preload = "metadata"; a.addEventListener("loadedmetadata", go, { once: true }); a.load(); }
}

function highlight() {
  if (!current) return;
  const t = current.audio.currentTime;
  for (const group of [current.chapterMarks, current.txMarks]) {
    let active = null;
    for (const m of group) if (m.start <= t) active = m;
    for (const m of group) m.el.classList.toggle("now", m === active);
  }
}

function show(key, push) {
  const ep = DATA.episodes.find((e) => e.key === key) || DATA.episodes[0];
  for (const b of document.querySelectorAll("#eplist button")) b.setAttribute("aria-current", String(b.dataset.key === ep.key));
  if (push) history.replaceState(null, "", `#${ep.key}`);
  const n = ep.notes, s = ep.stats;
  const audio = h("audio", { controls: true, preload: "none", src: ep.audio_url });
  const chapterMarks = n.chapters.map((c) => ({ start: c.start, el: jumpButton(c.start, h("span", { class: "ts" }, ts(c.start)), h("span", {}, c.title)) }));
  const quotes = n.quotes.map((q) => h("li", {}, jumpButton(q.start, h("q", {}, q.text), h("span", { class: "ts" }, `at ${ts(q.start)}`))));
  const files = ep.files.map((f) => h("a", { href: `episodes/${ep.key}/${f.name}`, download: `${ep.key}-${f.name}` }, f.label));
  const made = [
    ["Audio", mins(s.duration_s)],
    ["Transcription", `${mins(s.processing_s)} on CPU, ${s.chunks} chunks`],
    ["Agent turns", `${s.turns} (${s.tool_calls} tool calls)`],
    ["Rejected by validator", s.rejections ? `${s.rejections}×, then fixed` : "0"],
    ["Tokens", `${num(s.prompt_tokens)} in / ${num(s.completion_tokens)} out`],
    ["Model cost", s.llm_cached_turns ? `${usd(s.usd_list)} (${s.llm_cached_turns} of ${s.turns} replies reused from an earlier run's cache)` : usd(s.usd_list ?? s.usd_spent)],
  ];
  const ag = DATA.agreement && DATA.agreement.episodes[ep.key];
  if (ag) made.push(["Differs from HPR's own transcript", `${ag.difference_pct}% of words`]);
  const txBox = h("div", { class: "txbox" }, h("p", { class: "muted" }, "Loading…"));
  const txMarks = [];
  const tx = h("details", { class: "tx", ontoggle: async (e) => {
    if (!e.target.open || txBox.dataset.loaded) return;
    txBox.dataset.loaded = "1";
    try {
      const doc = await (await fetch(`episodes/${ep.key}/transcript.json`)).json();
      txBox.replaceChildren(...doc.segments.map((seg) => {
        const el = jumpButton(seg.start, h("span", { class: "ts" }, ts(seg.start)), h("span", {}, seg.text));
        txMarks.push({ start: seg.start, el });
        return el;
      }));
    } catch (err) {
      txBox.replaceChildren(h("p", { class: "muted" }, "Could not load the transcript."));
    }
  } }, h("summary", {}, `Full transcript (${num(s.words)} words, machine-made)`), txBox);

  document.getElementById("detail").replaceChildren(
    h("h3", {}, ep.title),
    h("p", { class: "byline" }, `${ep.author ? `Host: ${ep.author} · ` : ""}${ep.published.replace(/ \d\d:\d\d:\d\d.*$/, "")} · `, h("a", { href: ep.page_url }, "episode page")),
    audio,
    h("h3", {}, "Summary"), h("p", {}, n.summary),
    h("h3", {}, "Chapters"), h("ol", { class: "chapters" }, chapterMarks.map((m) => h("li", {}, m.el))),
    h("h3", {}, "Pull quotes"), h("p", { class: "muted" }, "Each one was found word for word in the transcript before it was accepted."),
    h("ul", { class: "quotes" }, quotes),
    h("h3", {}, "Topics"), h("ul", { class: "topics" }, n.topics.map((t) => h("li", {}, t))),
    h("h3", {}, "Download"), h("div", { class: "files" }, files),
    h("h3", {}, "How this episode was made"),
    h("dl", { class: "made" }, made.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v)))),
    tx,
    h("p", { class: "attrib" }, ep.attribution)
  );
  if (current) current.audio.pause();
  current = { audio, chapterMarks, txMarks };
  audio.addEventListener("timeupdate", highlight);
}

function chaosSection() {
  const box = document.getElementById("chaos");
  const c = DATA.chaos, cmp = DATA.compare, run = DATA.run;
  if (!c) {
    box.replaceChildren(h("p", { class: "muted" }, "The chaos run has not been published yet."));
    return;
  }
  const L = c.ledger;
  const retried = Object.entries(c.retried_attempts_by_activity || {}).sort((a, b) => b[1] - a[1]);
  const kills = c.events.filter((e) => e.action === "kill");
  const intro = h("p", {},
    `A second catalog run of ${c.episodes.length} episodes, on the same box, while a script SIGKILLed the worker ${c.kills} times at random moments `,
    `(${c.server_kills ? `and the Temporal server itself ${c.server_kills === 1 ? "once" : `${c.server_kills} times`}` : "never the server"}), `,
    `and the worker injected failures on purpose: ${c.faults_human}. Total wall clock ${mins(c.wall_clock_s)}.`);
  const facts = h("div", { class: "tablewrap" }, h("table", {},
    h("tbody", {},
      h("tr", {}, h("th", {}, "Episodes finished"), h("td", {}, `${c.episodes.filter((e) => e.status === "done").length} of ${c.episodes.length}`)),
      h("tr", {}, h("th", {}, "Transcripts identical to the undisturbed run"), h("td", { class: cmp && cmp.all_identical ? "good" : "" },
        cmp ? (cmp.all_identical ? `Yes, segment for segment (${num(cmp.segments)} segments)` : `No: ${cmp.summary}`) : "not compared")),
      h("tr", {}, h("th", {}, "Model replies received"), h("td", {}, `${L.completions_ok} (${L.unique_completions} distinct requests)`)),
      h("tr", {}, h("th", {}, "Model replies bought twice"), h("td", { class: L.duplicate_completions === 0 ? "good" : "" }, String(L.duplicate_completions))),
      h("tr", {}, h("th", {}, "Requests in flight when the worker died"), h("td", {}, L.sent_without_outcome ? `${L.sent_without_outcome} (the reply died with the process and the retry asked again; the provider may have billed ${L.sent_without_outcome === 1 ? "it" : "them"} anyway)` : "0")),
      h("tr", {}, h("th", {}, "Injected model API 503s / real HTTP errors"), h("td", {}, `${L.injected_faults} / ${Object.values(L.http_errors || {}).reduce((a, b) => a + b, 0)}`)),
      h("tr", {}, h("th", {}, "Workflow tasks that timed out"), h("td", {}, String(c.workflow_task_timeouts))),
      h("tr", {}, h("th", {}, "Model cost of the chaos run"), h("td", {}, usd(L.usd))),
    )));
  const retryTable = retried.length ? h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Activity"), h("th", {}, "Retried attempts"), h("th", {}, "Scheduled"))),
    h("tbody", {}, retried.map(([name, n]) => h("tr", {}, h("td", {}, h("code", {}, name)), h("td", {}, String(n)), h("td", {}, String((c.activities_scheduled || {})[name] || "")))))
  )) : null;
  const timeline = h("ul", { class: "timeline" }, c.events.map((e) => {
    let text = e.action;
    if (e.action === "kill") {
      const st = Object.entries(e.stages || {}).map(([k, v]) => `${k}: ${v}`).join("; ");
      text = `kill #${e.n}${e.server_too ? " (worker and Temporal server)" : " (worker)"} — ${st || "between steps"}`;
    } else if (e.action === "worker_restarted") text = "worker started again";
    else if (e.action === "server_restarted") text = "Temporal server started again";
    else if (e.action === "start") text = `catalog started: ${e.only}`;
    else if (e.action === "closed") text = `catalog closed: ${e.status}`;
    return h("li", { class: e.action === "kill" ? "kill" : "" }, h("span", { class: "ts" }, `+${ts(e.t)}`), " ", text);
  }));
  const clean = run.catalog_ids ? h("p", { class: "muted" },
    `For comparison, the undisturbed run still recorded ${run.workflow_task_timeouts} workflow-task timeouts and ${run.workflow_task_failures} workflow-task failure: the box is a shared home server whose disk was saturated by another service at the time, so the Temporal server sometimes handed out a task after the worker had stopped waiting for it, and once the worker was starved for over 2 seconds mid-task. Temporal retried each one; nothing had to be restarted by hand.`) : null;
  box.replaceChildren(intro, facts,
    h("h3", {}, "Retries Temporal did for me"), retryTable || h("p", { class: "muted" }, "None recorded."),
    h("h3", {}, "Timeline"), h("details", {}, h("summary", {}, `${kills.length} kills, in order`), timeline), clean);
}

function costSection() {
  const run = DATA.run, np = DATA.nebius_price;
  const perHour = run.usd_per_audio_hour;
  const backlog = 150; // hours: e.g. 300 episodes of 30 minutes
  const speed = run.transcribe_s / run.audio_s;
  const pace = run.wall_clock_s ? run.wall_clock_s / run.audio_s : null;
  const ours = h("p", {},
    `This run paid ${usd(run.usd, 3)} in model calls for ${hours(run.audio_s)} of audio, or ${usd(perHour, 3)} per audio hour at Nebius list prices for ${np.model} ($${np.input_per_m.toFixed(2)} in / $${np.output_per_m.toFixed(2)} out per million tokens). `,
    `Transcription is free and local; on this machine it runs at ${(1 / speed).toFixed(1)}× real time, so a ${backlog}-hour back catalog (300 half-hour episodes) is about ${Math.round(backlog * speed)} hours of transcription${pace ? ` (about ${Math.round(backlog * pace)} hours of wall clock at this run's pace, agent included)` : ""} and about ${usd(perHour * backlog, 2)} of model calls. I did not measure the electricity.`);
  const table = h("div", { class: "tablewrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Hosted service"), h("th", {}, "Price"), h("th", {}, "What it includes"))),
    h("tbody", {}, DATA.prices.map((p) => h("tr", {}, h("td", {}, h("a", { href: p.url }, p.name)), h("td", {}, p.price), h("td", {}, p.allowance))))));
  const note = h("p", { class: "muted" }, "Prices read from the vendors' pricing pages on 2026-10-15. Those tools do more than chaptermill (editors, clips, social posts, a team UI); the point is only that the transcription and writing-up of an archive is a cost you can bring close to zero with open models, and own.");
  document.getElementById("costs").replaceChildren(ours, table, note);
}

async function main() {
  try {
    DATA = await (await fetch("data.json")).json();
  } catch (err) {
    document.getElementById("detail").textContent = "Could not load data.json.";
    return;
  }
  kpis(DATA.run, DATA.chaos);
  runNote(DATA.run);
  episodeList();
  show(location.hash.slice(1), false);
  chaosSection();
  costSection();
  window.addEventListener("hashchange", () => {
    const key = location.hash.slice(1);
    if (DATA.episodes.some((e) => e.key === key)) show(key, false);
  });
}
main();
