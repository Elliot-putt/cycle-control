(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const safeUrl = (u) => (/^https:\/\//.test(u || "") ? u : "");
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) {
      try { localStorage.setItem(k, JSON.stringify(v)); } catch {}
      if (["filter", "startAs", "agentView"].includes(k)) {
        fetch("/api/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [k]: v }) }).catch(() => {});
      }
    },
  };

  const PHASES = [
    ["refine", "Refine"], ["plan", "Plan"], ["breakdown", "Break"], ["execute", "Build"],
    ["gates", "Gates"], ["review", "Review"], ["pr", "PR"], ["ci", "CI"], ["human", "You"],
  ];

  let state = null;
  let activity = { feed: [], question: null, unavailable: false };
  let filter = store.get("filter", "mine");
  let startAs = store.get("startAs", "plan");
  let agentView = store.get("agentView", "open") === "open" ? "open" : "done";
  let selected = store.get("selected", "");
  let tab = "activity";
  let starting = new Set();
  let message = "";
  let wasWaiting = {};
  let openTools = new Set();
  let railJson = "";
  let headJson = "";
  let bodyJson = "";

  const api = async (path, body) => {
    const res = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { data });
    return data;
  };

  const URL_RE = /https:\/\/[^\s<>"')\]]+/g;
  const rich = (text) => {
    const raw = String(text ?? "");
    let out = "";
    let last = 0;
    for (const m of raw.matchAll(URL_RE)) {
      const url = m[0].replace(/[.,;:!?]+$/, "");
      out += esc(raw.slice(last, m.index)) + `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`;
      last = m.index + url.length;
    }
    return out + esc(raw.slice(last));
  };

  let expanded = new Set(store.get("expanded", []));
  let drafts = {};
  let checking = false;
  let confirming = "";
  let confirmTimer = null;
  let approving = new Set();
  let waking = new Set();

  const phaseIndex = (phase) => PHASES.findIndex(([key]) => key === phase);
  const session = () => (state ? state.sessions.find((s) => s.id === selected) : null);
  const outcomeOf = (s) => s.outcome || s.health;
  const dotClass = (o) => ({ working: "work", waiting: "wait", done: "done", failed: "err", ended: "err", sleeping: "done", finished: "done" })[o] || "done";

  const MERGE_LABELS = ["mergequeue", "Merge Queue: Good to Go"];
  // Approved here means "in the merge queue": Cycle's own flag, or the PR already carries the merge label.
  const approvedOf = (s) => !!s.approved || !!(s.comments && s.comments.status && (s.comments.status.labels || []).some((l) => MERGE_LABELS.includes(l)));
  const noQueueOf = (s) => !!(s.comments && s.comments.noQueue);
  const isMerged = (s) => !!(s.comments && s.comments.status && s.comments.status.merged);

  function strip(s) {
    const at = phaseIndex(s.phase);
    const merged = isMerged(s);
    const bars = PHASES.map((_, i) => `<i class="${merged || i < at ? "d" : i === at ? "c" : ""}"></i>`);
    return bars.join("") + `<i class="${merged ? "m" : ""}"></i>`;
  }

  const ago = (t) => {
    if (!t) return "";
    const sec = Math.max(0, Math.round(Date.now() / 1000 - t));
    return sec < 60 ? "just now" : sec < 3600 ? `${Math.round(sec / 60)}m ago` : `${Math.round(sec / 3600)}h ago`;
  };

  function pollLine(c) {
    if (!c || !c.hasPr) return "";
    if (!c.enabled) return "comment polling is off";
    if (!c.checkedAt) return "comments: not checked yet";
    const parts = [`comments checked ${ago(c.checkedAt)}`, c.lastNew ? `${c.lastNew} new` : "none new"];
    if (c.deliveredAt) parts.push(`sent to builder ${ago(c.deliveredAt)}`);
    return parts.join(" · ");
  }

  const HELD_AFTER_SECONDS = 600;
  const whenText = (t) => {
    const d = new Date(t * 1000);
    const today = new Date().toDateString() === d.toDateString();
    const hm = d.toTimeString().slice(0, 5);
    return today ? hm : `${d.toLocaleDateString(undefined, { weekday: "short" })} ${hm}`;
  };
  let awayFrom = store.get("seenAt", 0);
  const markSeen = () => store.set("seenAt", Math.floor(Date.now() / 1000));
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) markSeen();
    else { awayFrom = store.get("seenAt", 0); renderJournal(true); }
  });
  window.addEventListener("pagehide", markSeen);
  setInterval(() => { if (!document.hidden) markSeen(); }, 60000);

  let journalJson = "";
  function renderJournal(force) {
    const entries = (state && state.journal) || [];
    const json = JSON.stringify([entries, selected, awayFrom]);
    if (!force && json === journalJson) return;
    journalJson = json;
    const box = $("empty");
    if (!entries.length) { box.innerHTML = "Start an issue, or select an agent to see what it needs."; box.classList.remove("journal"); return; }
    const fresh = entries.filter((e) => e.at > awayFrom);
    const older = entries.filter((e) => e.at <= awayFrom).slice(0, 40);
    const row = (e) => `<div class="j-row${e.at > awayFrom ? " new" : ""}"><span class="j-at mono">${esc(whenText(e.at))}</span><span class="j-body">${e.issue ? `<a data-open="${esc(e.issue)}">${esc(e.title)}</a>` : esc(e.title)}${e.message ? `<span class="muted"> · ${esc(e.message)}</span>` : ""}</span></div>`;
    box.classList.add("journal");
    box.innerHTML = `<div class="j-h">While you were away<span class="n">${fresh.length}</span></div>` +
      (fresh.length ? fresh.map(row).join("") : `<div class="none">Nothing happened since you last looked.</div>`) +
      (older.length ? `<div class="j-h dim">Earlier</div>${older.map(row).join("")}` : "");
  }

  function commentChip(s) {
    const c = s.comments;
    if (!c) return "";
    const heldFor = c.heldSince ? Date.now() / 1000 - c.heldSince : 0;
    const held = heldFor > HELD_AFTER_SECONDS && outcomeOf(s) === "working";
    const pickup = c.waiting && !c.needsYou
      ? held
        ? `<div class="chip held" data-comments="${esc(s.id)}" title="The builder is mid-work; the feedback goes in when it pauses">${c.waiting} review comment${c.waiting === 1 ? "" : "s"} held ${esc(ago(c.heldSince))} · builder busy</div>`
        : `<div class="chip pickup" data-comments="${esc(s.id)}"><span class="pulse"></span>picking up ${c.waiting} review comment${c.waiting === 1 ? "" : "s"}</div>`
      : "";
    const poll = pollLine(c) ? `<div class="chip dim" data-comments="${esc(s.id)}" title="Click for details">${esc(pollLine(c))}</div>` : "";
    const lane = (c.lane ? `<div class="chip lane k-${esc(c.lane.replace(/[^a-z]+/g, "-"))}" data-comments="${esc(s.id)}">${esc(c.lane)}</div>` : "") + (c.blockedNote ? `<div class="chip dim" data-comments="${esc(s.id)}">${esc(c.blockedNote)}</div>` : "");
    if (!c.addressed && !c.needsYou && !c.waiting) return lane + poll;
    if (pickup) return `${lane}${pickup}<div class="chip" data-comments="${esc(s.id)}">addressed ${c.addressed} · ${c.waiting} waiting</div>${poll}`;
    const needs = c.needsYou ? ` · <b>${c.needsYou} needs you</b>` : "";
    const waiting = c.waiting ? ` · ${c.waiting} waiting` : "";
    return `${lane}<div class="chip" data-comments="${esc(s.id)}" title="Show each comment, what was done and the reply">addressed ${c.addressed}${needs}${waiting}</div>${poll}`;
  }

  // Which part of the Agents list a builder sits in: what needs Elliot, what is moving, what waits on others.
  function agentBucket(s) {
    const o = outcomeOf(s);
    const c = s.comments || {};
    if (o === "failed" || o === "ended") return "needs";
    if (c.needsYou > 0 || s.attention) return "needs";
    if (c.waiting > 0 || o === "working") return "working";
    if (s.phase === "human" && !approvedOf(s) && !s.slackLink && !isMerged(s)) return "needs";
    if (approvedOf(s) || s.slackLink || isMerged(s) || o === "sleeping" || s.phase === "human") return "parked";
    return "working";
  }

  const stackOf = (s) => (s.comments && s.comments.stack) || [];
  function stackChip(s) {
    const st = stackOf(s);
    if (!st.length) return "";
    const merged = st.filter((p) => p.merged).length;
    return `<div class="a-meta"><span class="${merged === st.length ? "stk-m" : merged ? "stk-p" : "needs"}">${merged} of ${st.length} PRs merged</span></div>`;
  }

  function agentCard(s) {
    const o = outcomeOf(s);
    const label = (PHASES[phaseIndex(s.phase)] || [s.phase, s.phase === "starting" ? "starting" : s.phase])[1];
    let right = "";
    if (s.comments && s.comments.stage) right = `<span class="${s.comments.stage.tone === "need" ? "needs" : ""}">${esc(s.comments.stage.headline.slice(0, 34))}</span>`;
    else if (approvedOf(s)) right = `<span>${noQueueOf(s) ? "you posted it in Slack" : "you added mergequeue"}</span>`;
    else if (s.slackLink) right = "<span>you posted it in Slack</span>";
    else if (o === "waiting") right = '<span class="needs">needs you</span>';
    else if (o === "failed") right = "<span>failed</span>";
    else if (o === "sleeping") right = "<span>sleeping</span>";
    else if (o === "ended") right = "<span>ended</span>";
    else if (o === "finished") right = "<span>finished</span>";
    else if (o === "done") right = "<span>done</span>";
    else if (s.activity) right = `<span class="live">${esc(s.activity)}</span>`;
    return `<div class="agent ${selected === s.id ? "sel" : ""} ${o === "waiting" ? "waiting" : ""}" data-open="${esc(s.id)}">
      <div class="a-top"><span class="dot ${isMerged(s) ? "merged" : dotClass(o)}"></span><span class="id">${esc(s.id)}</span><span class="t" title="${esc(s.title)}">${esc(s.title)}</span></div>
      <div class="strip">${strip(s)}</div>
      <div class="a-meta"><b>${esc(label)}</b>${right}</div>
      ${stackChip(s)}
      ${commentChip(s)}
      <button class="nuke" data-nuke="${esc(s.id)}" title="Tear this issue down: builder, PR, assignment, worktree">Nuke</button>
    </div>`;
  }

  function startButton(i, base) {
    const busy = starting.has(i.id);
    if (i.isQueued) return '<span class="tag">queued</span>';
    const off = i.startable === false;
    const attrs = off ? `disabled title="${esc(i.startReason)}"` : busy ? "disabled" : "";
    const baseAttr = base ? ` data-base="${esc(base)}"` : "";
    const button = `<button class="go ${off ? "off" : ""}" data-start="${esc(i.id)}"${baseAttr} ${attrs}>${busy ? "Starting…" : "Start"}</button>`;
    return off ? `<span class="tip" title="${esc(i.startReason)}">${button}</span>` : button;
  }

  function issueRow(i) {
    if (i.blockedBy.length) i = { ...i, startable: false, startReason: `Blocked by ${i.blockedBy.join(", ")}.` };
    const busy = starting.has(i.id);
    const pri = i.priority === 1 ? "ur" : i.priority === 2 ? "hi" : "";
    const resume = i.hasWorktree && i.startable !== false ? `<button class="go alt" data-resume="${esc(i.id)}" ${busy ? "disabled" : ""}>Resume</button>` : "";
    return `<div class="row">
      <i class="pri ${pri}"></i>
      <a class="id" href="${esc(safeUrl(i.url))}" target="_blank">${esc(i.id)}</a>
      <span class="t" title="${esc(i.title)}">${esc(i.title)}</span>
      ${i.blockedBy.length ? `<span class="tag" title="${esc(i.startReason)}">blocked</span>` : ""}
      ${i.inCycle ? "" : '<span class="off" title="Not in this cycle">○</span>'}
      <span class="go-group">${resume}${startButton(i)}</span>
    </div>`;
  }

  const KIND_TEXT = { done: "done", pr_open: "PR open", running: "running", queued: "queued", ready: "ready", blocked: "blocked" };

  function childRow(c, issuesById) {
    const issue = issuesById[c.id] || { id: c.id, startable: c.startable, startReason: c.startReason };
    const tag = c.kind === "pr_open" && c.prNumber ? `PR #${c.prNumber}` : c.kind === "blocked" ? `after ${c.waitingOn.join(", ")}` : KIND_TEXT[c.kind];
    const action = c.kind === "ready" ? startButton({ ...issue, startable: c.startable, startReason: c.startReason, isQueued: false }, c.base) : "";
    const link = c.kind === "pr_open" && c.prUrl ? `<a class="tag link" href="${esc(safeUrl(c.prUrl))}" target="_blank">${esc(tag)}</a>` : `<span class="tag k-${c.kind}">${esc(tag)}</span>`;
    return `<div class="row child">
      <i class="pri k-${c.kind}"></i>
      <a class="id" href="${esc(safeUrl(c.url))}" target="_blank">${esc(c.id)}</a>
      <span class="t" title="${esc(c.title)}">${esc(c.title)}</span>
      ${link}
      <span class="go-group">${action}</span>
    </div>`;
  }

  function groupBlock(g, issuesById, sessionsById) {
    const open = expanded.has(g.id);
    const segments = g.children.map((c) => {
      const s = sessionsById[c.id];
      const cls = s && outcomeOf(s) === "waiting" ? "w" : { done: "d", pr_open: "p", running: "c", queued: "q", ready: "r", blocked: "" }[c.kind];
      return `<i class="${cls}" title="${esc(c.id + " · " + KIND_TEXT[c.kind])}"></i>`;
    }).join("");
    const reason = g.startReason || (g.readyCount ? "" : "No sub-issue is ready to start.");
    const readyButton = `<button class="go ${reason ? "off" : ""}" data-startready="${esc(g.id)}" ${reason ? `disabled title="${esc(reason)}"` : ""}>${starting.has(g.id) ? "Starting…" : `Start ready${g.readyCount ? " (" + g.readyCount + ")" : ""}`}</button>`;
    const startReady = reason ? `<span class="tip" title="${esc(reason)}">${readyButton}</span>` : readyButton;
    return `<div class="group ${open ? "open" : ""}">
      <div class="row g-head" data-toggle="${esc(g.id)}">
        <span class="chev">${open ? "▾" : "▸"}</span>
        <a class="id" href="${esc(safeUrl(g.url))}" target="_blank">${esc(g.id)}</a>
        <span class="t" title="${esc(g.title)}">${esc(g.title)}</span>
        <span class="g-count">${g.doneCount}/${g.children.length}</span>
        <span class="go-group">${startReady}</span>
      </div>
      <div class="strip g-strip">${segments}</div>
      ${open ? g.children.map((c) => childRow(c, issuesById)).join("") : ""}
    </div>`;
  }

  function renderRail() {
    const c = state.cycle;
    $("cycle-name").textContent = c ? `Cycle ${c.number}` : "Cycle";
    const slots = state.maxBuilders ? ` · ${state.slotsUsed}/${state.maxBuilders} builders · ${state.liveBuilders || 0}/${state.maxLiveBuilders || 0} live` : "";
    $("cycle-meta").textContent = c ? `${c.days_left}d left · ${c.done}/${c.total}${slots}` : "";
    $("burn").style.right = c && c.total ? `${100 - Math.round((c.done / c.total) * 100)}%` : "100%";
    document.querySelectorAll("#filter button").forEach((b) => b.classList.toggle("on", b.dataset.filter === filter));
    document.querySelectorAll("#startas button").forEach((b) => b.classList.toggle("on", b.dataset.startas === startAs));
    const err = $("error");
    err.hidden = !(state.error || message);
    err.textContent = message || state.error;

    const everyAgent = [...state.sessions];
    const doneCount = everyAgent.filter((a) => a.finished).length;
    document.querySelectorAll("#agentview button").forEach((b) => {
      b.classList.toggle("on", b.dataset.agentview === agentView);
      b.textContent = `${b.dataset.agentview === "open" ? "Open" : "Done"} ${b.dataset.agentview === "open" ? everyAgent.length - doneCount : doneCount}`;
    });
    const nukes = Object.values(state.nukes || {});
    const nukedIds = new Set(nukes.map((n) => n.id));
    const agents = everyAgent.filter((a) => (agentView === "done") === !!a.finished && !nukedIds.has(a.id));
    const groups = (state.groups || []).filter((g) => filter === "team" || g.isMine);
    const inGroup = new Set((state.groups || []).flatMap((g) => [g.id, ...g.children.map((c) => c.id)]));
    const issuesById = Object.fromEntries(state.issues.map((i) => [i.id, i]));
    const sessionsById = Object.fromEntries(state.sessions.map((x) => [x.id, x]));
    const queue = state.queue || [];
    const shown = state.issues.filter((i) => i.isOpen && !i.hasSession && !i.isQueued && !inGroup.has(i.id) && !nukedIds.has(i.id) && (filter === "team" || i.isMine));
    const progress = shown.filter((i) => i.isStarted);
    const blocked = shown.filter((i) => !i.isStarted && i.blockedBy.length);
    const next = shown.filter((i) => !i.isStarted && !i.blockedBy.length);
    const section = (title, rows, html, empty) =>
      `<div class="sec">${title}<span class="n">${rows.length}</span></div>` + (rows.length ? html : `<div class="none">${empty}</div>`);
    const queueRow = (q) => `<div class="row"><i class="pri k-queued"></i><span class="id">${esc(q.id)}</span><span class="t" title="${esc(q.title)}">${esc(q.title)}</span><span class="tag">#${q.position}</span><span class="go-group"><button class="go off" data-cancelq="${esc(q.id)}" title="Remove from the queue">Cancel</button></span></div>`;
    if (agentView === "done") {
      $("list").innerHTML = section("Done", agents, agents.map(agentCard).join(""), "Nothing merged or closed yet. Done builders are forgotten after two weeks.");
      return;
    }
    const bucket = { needs: [], working: [], parked: [] };
    for (const a of agents) bucket[agentBucket(a)].push(a);
    const sub = (title, rows, cls) => rows.length ? `<div class="sub ${cls}">${title}<span class="n">${rows.length}</span></div>` : "";
    const parkedOpen = expanded.has("__parked");
    const parkedShown = parkedOpen ? bucket.parked : bucket.parked.filter((a) => a.id === selected);
    const parkedHead = bucket.parked.length
      ? `<div class="sub s-parked" data-toggle="__parked" title="Waiting on reviewers, nothing for you to do">${parkedOpen ? "▾" : "▸"} Parked<span class="n">${bucket.parked.length}</span>${parkedOpen ? "" : `<span class="ids">${bucket.parked.map((a) => esc(a.id)).join(" ")}</span>`}</div>`
      : "";
    const agentHtml = nukes.map(nukeCard).join("") +
      sub("Needs you", bucket.needs, "s-needs") + bucket.needs.map(agentCard).join("") +
      sub("Working", bucket.working, "s-work") + bucket.working.map(agentCard).join("") +
      parkedHead + parkedShown.map(agentCard).join("");
    $("list").innerHTML =
      section("Agents", [...nukes, ...agents], agentHtml, "No builders running.") +
      (queue.length ? section("Queued", queue, queue.map(queueRow).join(""), "") : "") +
      (groups.length ? section("Stacks", groups, groups.map((g) => groupBlock(g, issuesById, sessionsById)).join(""), "") : "") +
      section("In progress", progress, progress.map(issueRow).join(""), "Nothing in progress.") +
      section("Up next", next, next.map(issueRow).join(""), "Nothing queued.") +
      (blocked.length ? section("Blocked", blocked, blocked.map(issueRow).join(""), "") : "");
  }

  function renderHead() {
    const s = session();
    $("empty").hidden = !!s;
    $("panel").hidden = !s;
    if (!s) return;
    const o = outcomeOf(s);
    $("m-id").textContent = s.id;
    $("m-title").textContent = s.title;
    $("m-title").title = s.title;
    const pill = $("m-pill");
    const stageNow = s.comments && s.comments.stage;
    pill.className = `pill ${stageNow && stageNow.tone === "merged" ? "merged" : stageNow ? { need: "wait", work: "work", wait: "work", done: "done" }[stageNow.tone] : dotClass(o)}`;
    pill.textContent = o === "sleeping" ? "Sleeping" : o === "finished" && !isMerged(s) ? "Closed" : stageNow ? { need: "Needs you", work: "In progress", wait: "Waiting", done: "Done", merged: "Merged" }[stageNow.tone] : { working: "Working", waiting: "Needs you", failed: "Failed", ended: "Ended", done: "Done" }[o];
    $("m-linear").href = safeUrl(s.url);
    $("m-pr").hidden = !s.prUrl;
    $("m-pr").href = safeUrl(s.prUrl);
    const at = phaseIndex(s.phase);
    const merged = isMerged(s);
    $("m-steps").innerHTML = PHASES.map(([, label], i) => `<div class="st ${merged || i < at ? "d" : i === at ? "c" : ""} ${!merged && i === at && o !== "waiting" ? "work" : ""}"><i></i><span>${label}</span></div>`).join("") + `<div class="st ${merged ? "m" : ""}"><i></i><span>Merged</span></div>`;
    const c = s.comments || { items: [], needsYou: 0 };
    const showComments = c.items.length || c.lane || c.hasPr;
    const cnt = c.needsYou ? `<span class="cnt">· ${c.needsYou} needs you</span>` : "";
    const tabs = [["activity", "Activity", ""], ...(showComments ? [["comments", `Comments${c.items.length ? " · " + c.items.length : ""}`, cnt]] : []), ["terminal", "Terminal", ""]];
    $("tabs").innerHTML = tabs.map(([key, label, cnt]) => `<button data-tab="${key}" class="${tab === key ? "on" : ""}">${label}${cnt}</button>`).join("");
    const banner = $("m-banner");
    const stage = s.comments && s.comments.stage;
    if (stage) {
      const st = s.comments.status || {};
      const facts = [
        st.approved ? `approved by ${(st.approvers || []).map((a) => "@" + a).join(", ")}` : "not approved",
        `${st.unresolved || 0} open thread${st.unresolved === 1 ? "" : "s"}`,
        `CI ${String(st.ci || "unknown").toLowerCase()}`,
        (st.labels || []).length ? `labels: ${st.labels.join(", ")}` : "no labels",
      ].join(" · ");
      const pulse = stage.tone === "work" || stage.tone === "need" ? '<span class="pulse"></span>' : "";
      const button = tab !== "comments" && s.comments.items.length ? `<button class="ghost" data-comments="${esc(s.id)}">Open comments</button>` : "";
      banner.className = `banner stage ${stage.tone}`;
      banner.innerHTML = `<div class="stage-main">${pulse}<b>${esc(stage.headline)}</b><span class="muted">${esc(stage.next)}</span>${button}</div><div class="stage-facts">${esc(facts)}</div>`;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
    $("say").placeholder = `Answer or instruct ${s.id}…`;
    $("composer").hidden = tab === "terminal" || o === "finished";
  }

  function activityHtml(s) {
    if (activity.unavailable) {
      return `<div class="notice">The activity feed needs the updated server. Restart Cycle Control to load it. The Terminal tab still works.</div>`;
    }
    const parts = [];
    const q = activity.question;
    const feed = q && q.kind === "text" ? activity.feed.slice(0, activity.feed.map((e) => e.kind).lastIndexOf("say")) .concat(activity.feed.slice(activity.feed.map((e) => e.kind).lastIndexOf("say") + 1)) : activity.feed;
    for (const e of feed) {
      if (e.kind === "say") {
        parts.push(`<div class="say"><span class="when">${esc(e.at)}</span><div class="md">${window.renderMarkdown(e.text)}</div></div>`);
      } else {
        const key = e.summary + (e.calls[0] ? e.calls[0].name + e.calls[0].target : "");
        const calls = e.calls.map((c) => `<div class="tool ${c.ok ? "" : "err"}">${esc(c.name)}${c.target ? " · " + esc(c.target) : ""} ${c.result ? `<span class="${c.ok ? "ok" : "bad"}">${esc(c.result)}</span>` : ""}</div>`).join("");
        parts.push(`<details class="tools ${e.failed ? "hasfail" : ""}" data-key="${esc(key)}" ${openTools.has(key) ? "open" : ""}><summary>${esc(e.summary)}</summary>${calls}</details>`);
      }
    }
    const waiting = outcomeOf(s) === "waiting";
    if (["sleeping", "ended", "failed"].includes(outcomeOf(s)) || (outcomeOf(s) === "finished" && !isMerged(s))) {
      const busy = waking.has(s.id);
      const o = outcomeOf(s);
      const finished = o === "finished";
      const title = { sleeping: "Sleeping", ended: "Ended", failed: "Crashed", finished: "Closed without merging" }[o];
      const why = finished ? "Its PR was closed without merging, so the builder was ended and will not restart on launch. It leaves the panel two weeks after closing." : o === "sleeping" ? "It was only waiting, so its process was ended to free memory. Its conversation is saved." : o === "ended" ? "Its process quit (Ctrl-C twice does that). Its conversation is saved." : "Its process crashed. Its conversation is saved.";
      const how = finished ? "Wake it only if you still need it." : "Sending a message wakes it with that message; new review feedback wakes it on its own.";
      parts.push(`<div class="ask"><b>${title}</b><div class="md"><p>${why} ${how}</p></div><div class="actions"><button class="send" data-wake="${esc(s.id)}" ${busy ? "disabled" : ""}>${busy ? "Waking…" : "Wake"}</button></div></div>`);
    }
    if (s.comments && s.comments.needsYou) {
      parts.push(`<div class="ask"><b>Review comments</b><div class="md"><p>${s.comments.needsYou} comment${s.comments.needsYou === 1 ? "" : "s"} need${s.comments.needsYou === 1 ? "s" : ""} you. The builder drafted a reply for each.</p></div><div class="actions"><button class="send" data-comments="${esc(s.id)}">Open comments</button></div></div>`);
    }
    if (s.attention) {
      parts.push(`<div class="ask"><b>Rebase conflict</b><div class="md"><p>${esc(s.attention)}</p><p>The builder has not been told. Resolve it yourself in <code>${esc(s.worktree)}</code>, then press the button.</p></div><div class="actions"><button class="send" data-resolved="${esc(s.id)}">I resolved it</button></div></div>`);
    } else if (s.prUrl || knownPrUrl(s)) {
      const prUrl = s.prUrl || knownPrUrl(s);
      const busy = approving.has(s.id);
      const posted = !approvedOf(s) && s.slackLink ? `<p class="okline">You posted it in Slack yourself: <a href="${esc(safeUrl(s.slackLink))}" target="_blank" rel="noopener">message</a>. Approve now only adds the merge-queue label.</p>` : "";
      const done = approvedOf(s)
        ? `<p class="okline">${noQueueOf(s) ? "This repo has no merge queue, so no label was added" : "You added mergequeue"}${s.slackLink ? `${noQueueOf(s) ? "; you posted it in Slack" : " and posted in Slack"}: <a href="${esc(safeUrl(s.slackLink))}" target="_blank" rel="noopener">message</a>` : ""}.</p>`
        : `${posted}<div class="actions"><button class="ghost" data-changes="${esc(s.id)}">Request changes</button><button class="send" data-approve="${esc(s.id)}" ${busy ? "disabled" : ""}>${busy ? "Approving…" : confirming === s.id ? "Confirm approve" : "Approve"}</button></div>`;
      parts.push(`<div class="ask pr ${approvedOf(s) ? "approved" : ""}"><b>${approvedOf(s) ? (noQueueOf(s) ? "Posted in Slack by you, merge it yourself once approved" : "Added to the merge queue by you") : s.slackLink ? "Posted in Slack by you, not queued yet" : "PR needs you: review it and approve"}</b><p>${rich(prUrl)}</p>${done}</div>`);
    } else if (q || waiting) {
      const raw = q ? q.text : s.latest || "The agent is idle and waiting for your input.";
      const text = q && q.kind === "text" && q.options.length ? raw.split("\n").filter((l) => !/^\s*\d+[.)]\s/.test(l)).join("\n").trim() : raw;
      const opts = (q ? q.options : []).map((o) => `<button class="opt" data-opt="${esc(o.key)}" data-kind="${esc(q.kind)}"><kbd>${esc(o.key)}</kbd>${esc(o.label)}</button>`).join("");
      parts.push(`<div class="ask"><b>${q && q.kind === "plan" ? "Plan approval" : "Waiting on you"}</b><div class="md">${window.renderMarkdown(text)}</div>${opts ? `<div class="opts">${opts}</div>` : ""}</div>`);
    }
    const stack = stackOf(s);
    if (stack.length) {
      const rows = stack.map((p) => `<p class="sp ${p.merged ? "m" : p.open ? "o" : ""}"><i></i>${p.merged ? "Merged" : p.open ? "Still open" : "Closed"} · <a href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener">#${p.number}</a></p>`).join("");
      parts.push(`<div class="ask pr"><b>${stack.filter((p) => p.merged).length} of ${stack.length} PRs merged</b>${rows}<p class="okline">This ticket is done when every PR in the stack is merged.</p></div>`);
    }
    if (isMerged(s)) {
      const at = s.comments.status.mergedAt;
      const when = at ? new Date(at).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
      parts.push(`<div class="ask pr merged"><b>Merged${when ? " · " + esc(when) : ""}</b><p>${rich(s.prUrl)}</p><p class="okline">The PR is on develop. Nothing more to do here.</p></div>`);
    }
    if (!parts.length) parts.push(`<div class="notice">Nothing from the agent yet.</div>`);
    return `<div class="tl">${parts.join("")}</div>`;
  }

  const STATE_TEXT = { fixed: "fixed", replied: "replied", skipped: "skipped", needs_you: "needs you", posted: "reply posted", answered: "answered to the builder", dismissed: "dismissed", queued: "queued for the builder", delivered: "with the builder" };
  const BUILDER_KINDS = { ci: "CI failing", conflict: "Merge conflict" };

  function builderCard(it, s) {
    const prUrl = safeUrl(it.url);
    const links = [
      prUrl ? `<a class="gh" href="${esc(prUrl)}" target="_blank" rel="noopener">PR ↗</a>` : "",
      prUrl && it.kind === "ci" ? `<a class="gh" href="${esc(prUrl)}/checks" target="_blank" rel="noopener">Checks ↗</a>` : "",
    ].join("");
    const head = `<div class="c-head"><b>${esc(BUILDER_KINDS[it.kind])}</b><span class="mono muted">${esc(String(it.body).split("\n")[0].slice(0, 90))}</span>${links}<span class="tag k-needs_you">needs you</span></div>`;
    const says = it.reply ? `<b>Builder says</b><div class="says md">${window.renderMarkdown(it.reply)}</div>` : `<b>Builder says</b><div class="says muted">The builder left no note.</div>`;
    const raised = `<details class="c-body"><summary>What was raised</summary><div class="md">${window.renderMarkdown(it.body)}</div></details>`;
    const opts = (it.options || []).map((o) => `<button class="opt" data-gowith="${esc(it.key)}" data-n="${esc(o.key)}" data-label="${esc(o.label)}" title="${esc(o.label)}"><kbd>${esc(o.key)}</kbd>Go with ${esc(o.key)}</button>`).join("");
    const draft = drafts[it.key] ?? "";
    return `<div class="ask c-card builder-ask">${head}${says}${raised}${opts ? `<div class="opts">${opts}</div>` : ""}<b>Tell the builder what to do</b><textarea class="draft" data-draft="${esc(it.key)}" rows="3" placeholder="e.g. Go with 2, and rerun the browser suite first">${esc(draft)}</textarea><div class="actions"><button class="send" data-answer="${esc(it.key)}">Send to builder</button><button class="ghost" data-terminal="${esc(s.id)}">Open terminal</button><button class="ghost" data-dismiss="${esc(it.key)}" title="Ignore this failing commit; a new one raises it again">Dismiss</button></div></div>`;
  }

  const linkShas = (text, repoUrl) =>
    repoUrl ? String(text).replace(/\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b/g, (sha) => `[${sha}](${repoUrl}/commit/${sha})`) : String(text);

  function commentsHtml(s) {
    const c = s.comments || { items: [], addressed: 0, needsYou: 0, waiting: 0 };
    const explain = c.hasPr ? `<div class="c-poll"><span>${esc(pollLine(c))}</span><span class="muted">Polls GitHub every ${Math.round((c.intervalSeconds || 300) / 60)} min for review comments on PR #${c.prNumber}. New ones go to the builder when it is idle.</span><button class="ghost" data-checknow="1">${checking ? "Checking…" : "Check now"}</button></div>` : "";
    const st = c.status;
    const approval = st ? `<span class="${st.approved ? "ok" : "amber"}">${st.approved ? "approved by " + st.approvers.join(", ") : st.blocking.length ? "changes requested by " + st.blocking.join(", ") : "not approved"}</span> · CI ${esc((st.ci || "unknown").toLowerCase())} · ${st.unresolved} unresolved` : "";
    const blocked = c.blockedNote ? `<div class="c-sum amber">${esc(c.blockedNote)}</div>` : "";
    const requeue = c.requeue ? `<div class="c-sum">${c.requeue.status === "requeued" ? "Re-queued" : "Not re-queued"}${c.requeue.reasons && c.requeue.reasons.length ? ": " + esc(c.requeue.reasons.join("; ")) : ""}</div>` : "";
    const head = explain + ((approval || c.lane) ? `<div class="c-sum">${c.lane ? `<b class="ok">${esc(c.lane)}</b> · ` : ""}${approval}</div>${blocked}${requeue}` : "");
    if (!c.items.length) return `<div class="tl">${head}<div class="notice">No review comments yet.</div></div>`;
    const rows = c.items.map((it) => {
      const place = it.path ? `${esc(it.path)}${it.line ? ":" + it.line : ""}` : "";
      const where = place ? ` · ${it.url ? `<a href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener">${place}</a>` : place}` : "";
      const link = it.url ? `<a class="gh" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener">GitHub ↗</a>` : "";
      const base = it.state === "posted" && it.resolved ? "reply posted · resolved" : STATE_TEXT[it.state] || it.state;
      const label = it.githubResolved && it.state !== "needs_you" ? `${base} · resolved on GitHub` : base;
      const head = `<div class="c-head"><b>@${esc(it.author)}</b><span class="mono muted">${esc(it.kind)}${where}</span>${link}<span class="tag k-${esc(it.state)}">${esc(label)}</span></div>`;
      const body = `<details class="c-body"><summary>${esc(String(it.body).split("\n")[0].slice(0, 110))}</summary><div class="md">${window.renderMarkdown(it.body)}</div></details>`;
      if (it.state === "needs_you" && BUILDER_KINDS[it.kind]) return builderCard(it, s);
      if (it.state === "needs_you") {
        const draft = drafts[it.key] ?? it.reply ?? "";
        return `<div class="ask c-card">${head}${body}<b>Drafted reply</b><textarea class="draft" data-draft="${esc(it.key)}" rows="4">${esc(draft)}</textarea><div class="actions"><button class="send" data-postreply="${esc(it.key)}" data-resolve="1" title="Post the reply and resolve the thread">Post &amp; resolve</button><button class="ghost" data-postreply="${esc(it.key)}" title="Post the reply and leave the thread open">Post reply</button><button class="ghost" data-dismiss="${esc(it.key)}">Dismiss</button></div></div>`;
      }
      const yours = it.postedUrl ? `<a class="gh" href="${esc(safeUrl(it.postedUrl))}" target="_blank" rel="noopener">Your reply ↗</a>` : "";
      const reply = it.reply ? `<div class="c-reply"><span class="muted">Reply ${yours}</span><div class="md">${window.renderMarkdown(linkShas(it.reply, c.repoUrl))}</div></div>` : "";
      return `<div class="c-card">${head}${body}${reply}</div>`;
    }).join("");
    const needs = c.needsYou ? ` · <b class="amber">${c.needsYou} needs you</b>` : "";
    return `<div class="tl">${head}<div class="c-sum">addressed ${c.addressed}${needs}${c.waiting ? ` · ${c.waiting} waiting` : ""}</div>${rows}</div>`;
  }

  function renderBody() {
    const s = session();
    if (!s) { closeTerminal(); return; }
    const body = $("body");
    body.classList.toggle("term-body", tab === "terminal");
    if (tab === "terminal") {
      const alive = !["sleeping", "ended", "failed", "finished"].includes(outcomeOf(s));
      if (!term.host || term.id !== s.id || (alive && !term.alive)) {
        term.alive = alive;
        body.innerHTML = '<div class="term" id="term"></div>';
        openTerminal(s.id);
      }
      return;
    }
    closeTerminal();
    const html = tab === "comments" ? commentsHtml(s) : activityHtml(s);
    if (html === bodyJson) return;
    const pinned = body.scrollTop + body.clientHeight >= body.scrollHeight - 40;
    const fresh = bodyJson === "";
    bodyJson = html;
    body.innerHTML = html;
    if (tab === "comments") {
      if (fresh) body.scrollTop = 0;
    } else if (pinned) body.scrollTop = body.scrollHeight;
  }

  const term = { xterm: null, fit: null, ws: null, id: "", host: null, alive: false };

  function openTerminal(id) {
    closeTerminal();
    term.id = id;
    term.host = $("term");
    term.xterm = new window.Terminal({
      cursorBlink: true, fontSize: 13,
      fontFamily: '"JetBrains Mono", Menlo, monospace',
      theme: { background: "#04070c", foreground: "#d9f3ff", cursor: "#3fd4ff", selectionBackground: "#3fd4ff44" },
    });
    term.fit = new window.FitAddon.FitAddon();
    term.xterm.loadAddon(term.fit);
    term.xterm.open(term.host);
    const send = (msg) => term.ws && term.ws.readyState === 1 && term.ws.send(JSON.stringify(msg));
    const proto = location.protocol === "https:" ? "wss" : "ws";
    term.ws = new WebSocket(`${proto}://${location.host}/ws/terminal/${encodeURIComponent(id)}`);
    term.ws.binaryType = "arraybuffer";
    term.ws.onopen = () => { term.fit.fit(); send({ type: "resize", rows: term.xterm.rows, cols: term.xterm.cols }); };
    term.ws.onmessage = (e) => term.xterm.write(new Uint8Array(e.data));
    term.xterm.onData((data) => send({ type: "input", data }));
    term.xterm.attachCustomKeyEventHandler((e) => {
      if (e.key !== "Enter" || !e.ctrlKey || e.shiftKey || e.altKey || e.metaKey) return true;
      if (e.type === "keydown") send({ type: "input", data: "\x1b[13;5u" });
      return false;
    });
    term.xterm.onResize(({ rows, cols }) => send({ type: "resize", rows, cols }));
    term.xterm.focus();
  }

  function closeTerminal() {
    if (term.ws) term.ws.close();
    if (term.xterm) term.xterm.dispose();
    term.ws = term.xterm = term.fit = term.host = null;
    term.id = "";
  }
  window.addEventListener("resize", () => term.fit && term.fit.fit());

  function render() {
    if (!state) return;
    nukeWatch();
    $("setup").hidden = state.configured;
    $("app").hidden = !state.configured;
    if (!state.configured) return;
    const s = session();
    renderJournal(false);
    const justWaiting = s && outcomeOf(s) === "waiting" && !wasWaiting[s.id];
    state.sessions.forEach((x) => { wasWaiting[x.id] = outcomeOf(x) === "waiting"; });

    const rail = JSON.stringify([state.nukes, state.cycle, state.issues, state.sessions.map((x) => [x.id, x.title, x.phase, outcomeOf(x), x.activity, x.approved, x.comments]), state.error, filter, startAs, agentView, selected, [...starting], message, state.groups, state.queue, [...expanded], state.slotsUsed]);
    if (rail !== railJson) { railJson = rail; renderRail(); }
    const head = JSON.stringify([s && [s.id, s.title, s.phase, outcomeOf(s), s.prUrl, s.approved, s.attention, s.comments, checking, confirming, [...approving], [...waking], tab], tab, selected]);
    if (head !== headJson) { headJson = head; renderHead(); }
    renderBody();
    if (justWaiting && tab !== "terminal") $("say").focus();
  }

  async function send(payload) {
    const s = session();
    if (!s) return;
    try { await api(`/api/sessions/${encodeURIComponent(s.id)}/send`, payload); } catch (e) { message = e.message; render(); }
    await refreshActivity();
  }

  function select(id) {
    selected = id;
    store.set("selected", id);
    tab = "activity";
    const picked = state && state.sessions.find((s) => s.id === id);
    if (picked && ["sleeping", "ended"].includes(outcomeOf(picked))) wake(id);
    bodyJson = "";
    headJson = "";
    railJson = "";
    activity = { feed: [], question: null, unavailable: false };
    refreshActivity().then(() => { const b = $("body"); b.scrollTop = b.scrollHeight; if (session() && outcomeOf(session()) === "waiting") $("say").focus(); });
    render();
  }

  async function start(id, resume, base, force) {
    starting.add(id);
    message = "";
    render();
    try {
      state = await api("/api/start", { issueId: id, mode: startAs, resume, base: base || "", force: !!force });
      if (state.sessions.some((x) => x.id === id)) select(id);
      else if ((state.queue || []).some((q) => q.id === id)) message = `${id} is queued: ${state.slotsUsed}/${state.maxBuilders} builders are running.`;
    } catch (e) {
      if (e.data?.notYours && !force && confirm(`${e.message} Start a builder on it anyway?`)) {
        starting.delete(id);
        return start(id, resume, base, true);
      }
      message = `Start ${id}: ${e.message}`;
    } finally {
      starting.delete(id);
      railJson = "";
      render();
    }
  }

  async function startReady(parentId) {
    starting.add(parentId);
    message = "";
    render();
    try {
      state = await api(`/api/groups/${encodeURIComponent(parentId)}/start-ready`, { mode: startAs });
    } catch (e) {
      message = /not found/i.test(e.message) ? "Restart Cycle Control to enable Start ready." : `Start ready ${parentId}: ${e.message}`;
    } finally {
      starting.delete(parentId);
      railJson = "";
      render();
    }
  }

  document.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-open],[data-start],[data-resume],[data-startready],[data-toggle],[data-cancelq],[data-filter],[data-agentview],[data-startas],[data-tab],[data-key],[data-opt],[data-approve],[data-changes],[data-resolved],[data-comments],[data-checknow],[data-postreply],[data-dismiss],[data-wake],[data-answer],[data-gowith],[data-terminal],[data-nuke],[data-nukego],[data-nukeclose],[data-nukeretry],[data-nukedismiss],#m-stop,#m-nuke,#send,summary");
    if (!t) return;
    const d = t.dataset;
    if (t.tagName === "SUMMARY") {
      const det = t.parentElement;
      requestAnimationFrame(() => { det.open ? openTools.add(det.dataset.key) : openTools.delete(det.dataset.key); });
      return;
    }
    if (d.nukeretry) { toasted.delete(d.nukeretry); return nukeAsk(d.nukeretry); }
    if (d.nukedismiss) { state = await api(`/api/nukes/${encodeURIComponent(d.nukedismiss)}/dismiss`, {}); railJson = ""; return render(); }
    if (d.nuke) return nukeAsk(d.nuke);
    if (d.nukego) return nukeGo(d.nukego);
    if (d.nukeclose !== undefined) return nukeClose();
    if (t.id === "m-nuke") return nukeAsk(selected);
    if (d.open) return select(d.open);
    if (d.toggle) {
      if (e.target.closest("a, button")) return;
      expanded.has(d.toggle) ? expanded.delete(d.toggle) : expanded.add(d.toggle);
      store.set("expanded", [...expanded]);
      railJson = "";
      return render();
    }
    if (d.startready) return startReady(d.startready);
    if (d.cancelq) {
      state = await api(`/api/queue/${encodeURIComponent(d.cancelq)}/cancel`, {});
      railJson = "";
      return render();
    }
    if (d.start) return start(d.start, false, d.base);
    if (d.resume) return start(d.resume, true);
    if (d.filter) { filter = d.filter; store.set("filter", filter); return render(); }
    if (d.agentview) { agentView = d.agentview; store.set("agentView", agentView); railJson = ""; return render(); }
    if (d.startas) { startAs = d.startas; store.set("startAs", startAs); return render(); }
    if (d.tab) {
      tab = d.tab; bodyJson = ""; headJson = "";
      const picked = d.tab === "terminal" && state && state.sessions.find((s) => s.id === selected);
      if (picked && ["sleeping", "ended", "failed"].includes(outcomeOf(picked)) && !waking.has(selected)) wake(selected);
      return render();
    }
    if (d.checknow) {
      checking = true; bodyJson = ""; render();
      try { state = await api("/api/comments/poll", {}); } catch (e) { message = e.message; }
      checking = false; bodyJson = ""; railJson = "";
      return render();
    }
    if (d.comments) {
      selected = d.comments;
      store.set("selected", selected);
      tab = "comments";
      bodyJson = ""; headJson = ""; railJson = "";
      return render();
    }
    if (d.terminal) {
      selected = d.terminal; store.set("selected", selected); tab = "terminal"; bodyJson = ""; headJson = "";
      const picked = state && state.sessions.find((s) => s.id === selected);
      if (picked && ["sleeping", "ended", "failed"].includes(outcomeOf(picked)) && !waking.has(selected)) wake(selected);
      return render();
    }
    if (d.answer || d.gowith) {
      const key = d.answer || d.gowith;
      const text = d.gowith ? `Go with option ${d.n}: ${d.label}` : (drafts[key] ?? (document.querySelector(`[data-draft="${CSS.escape(key)}"]`) || {}).value ?? "");
      try {
        state = await api(`/api/sessions/${encodeURIComponent(selected)}/comments/${encodeURIComponent(key)}/answer`, { text });
        delete drafts[key];
        if (state.woken === "queued") message = `${selected} is queued to wake: ${state.liveBuilders}/${state.maxLiveBuilders} builders are live. It gets your answer when it starts.`;
      } catch (e) {
        message = /not found/i.test(e.message) ? "Restart Cycle Control to enable answering the builder." : e.message;
      }
      bodyJson = ""; railJson = "";
      return render();
    }
    if (d.postreply || d.dismiss) {
      const key = d.postreply || d.dismiss;
      const path = d.postreply ? "reply" : "dismiss";
      try {
        state = await api(`/api/sessions/${encodeURIComponent(selected)}/comments/${encodeURIComponent(key)}/${path}`, d.postreply ? { body: drafts[key] ?? (document.querySelector(`[data-draft="${CSS.escape(key)}"]`) || {}).value ?? "", resolve: t.dataset.resolve === "1" } : {});
        delete drafts[key];
      } catch (e) {
        message = /not found/i.test(e.message) ? "Restart Cycle Control to enable comment replies." : e.message;
      }
      bodyJson = ""; railJson = "";
      return render();
    }
    if (d.resolved) {
      state = await api(`/api/sessions/${encodeURIComponent(d.resolved)}/clear-attention`, {});
      bodyJson = "";
      return render();
    }
    if (d.changes) return requestChanges();
    if (d.approve) return approve(d.approve);
    if (d.wake) return wake(d.wake);
    if (d.key) return send({ key: d.key });
    if (d.opt) return d.kind === "text" ? send({ text: d.opt }) : send({ key: d.opt });
    if (t.id === "m-stop") { state = await api(`/api/sessions/${encodeURIComponent(selected)}/stop`, {}); return render(); }
    if (t.id === "send") return submitSay();
  });

  function requestChanges() {
    tab = "activity";
    bodyJson = "";
    headJson = "";
    render();
    const input = $("say");
    input.value = "We need to change some of the code: ";
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }


  // The builder's own record of its PR can lag (it only lands with `phase.sh human <url>`); the comment watcher
  // finds the PR by branch, so use that when the builder has not said yet.
  function knownPrUrl(s) {
    const c = s.comments;
    if (!c || !c.hasPr || !c.repoUrl || !c.prNumber) return "";
    return `${c.repoUrl}/pull/${c.prNumber}`;
  }
  async function approve(id) {
    if (confirming !== id) {
      confirming = id;
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => { confirming = ""; bodyJson = ""; render(); }, 4000);
      bodyJson = "";
      return render();
    }
    confirming = "";
    approving.add(id);
    message = "";
    bodyJson = "";
    render();
    try {
      state = await api(`/api/sessions/${encodeURIComponent(id)}/approve`, {});
    } catch (e) {
      message = /^(404 )?Not Found$/i.test(e.message) ? "Restart Cycle Control to enable Approve." : `Approve failed: ${e.message}`;
    } finally {
      approving.delete(id);
      railJson = "";
      bodyJson = "";
      render();
    }
  }

  async function wake(id) {
    waking.add(id);
    message = "";
    bodyJson = "";
    render();
    try {
      state = await api(`/api/sessions/${encodeURIComponent(id)}/wake`, {});
      if ((state.queue || []).some((q) => q.id === id)) message = `${id} is queued: ${state.liveBuilders}/${state.maxLiveBuilders} builders are live. It wakes when a slot frees.`;
    } catch (e) {
      message = /not found/i.test(e.message) ? "Restart Cycle Control to enable Wake." : `Wake ${id}: ${e.message}`;
    } finally {
      waking.delete(id);
      railJson = "";
      bodyJson = "";
      render();
    }
  }

  let nuking = null;

  function nukeRender() {
    const m = $("modal");
    if (!nuking) { m.hidden = true; m.innerHTML = ""; return; }
    const n = nuking;
    let body = "";
    if (n.loading) body = "<p>Checking what would be lost…</p>";
    else if (n.error) body = `<p class="err-text">${esc(n.error)}</p>`;
    else if (n.steps) {
      const NAMES = { builder: "Builder", pr: "PR", linear: "Linear", worktree: "Worktree", board: "Board row" };
      body = `<ul>${n.steps.map((st) => `<li class="${st.ok ? (st.skipped ? "skip" : "ok") : "bad"}">${st.ok ? (st.skipped ? "–" : "✓") : "✗"} ${esc(NAMES[st.step] || st.step)}: ${esc(st.detail)}</li>`).join("")}</ul>`;
    } else {
      const p = n.preview;
      const items = [];
      items.push(p.builder ? `End the ${esc(p.builder)} builder and forget it` : p.queued ? "Remove it from the queue" : "No builder to end");
      items.push(p.pr && p.pr.state === "OPEN" ? `Close PR #${p.pr.number}` : p.pr ? `PR #${p.pr.number} is already ${esc(p.pr.state.toLowerCase())}, left alone` : p.prError ? `Could not look up the PR: ${esc(p.prError)}` : "No PR to close");
      items.push(n.unassign === false ? `Leave ${esc(p.id)} assigned on Linear` : `Unassign ${esc(p.id)} on Linear (status unchanged)`);
      if (p.worktree) items.push(`Stop containers and remove the worktree ${esc(p.worktree)}`);
      else if (p.worktreeRefused) items.push(`Worktree refused: ${esc(p.worktreeRefused)}`);
      else items.push("No worktree to remove");
      items.push("Remove its row from the board");
      const loss = [];
      if (p.uncommitted) loss.push(`<li class="loss">${p.uncommitted} uncommitted change${p.uncommitted === 1 ? "" : "s"} would be lost</li>`);
      if (p.unpushed) loss.push(`<li class="loss">${p.unpushed} unpushed commit${p.unpushed === 1 ? "" : "s"} would be lost</li>`);
      body = `<ul>${items.map((i) => `<li>${i}</li>`).join("")}${loss.join("")}</ul>
        <label class="switch"><input type="checkbox" data-nukeunassign="1" ${n.unassign === false ? "" : "checked"}> Unassign from ticket</label>`;
    }
    const done = !!n.steps;
    const actions = done || n.error
      ? `<div class="actions"><button class="ghost" data-nukeclose="1">Close</button></div>`
      : `<div class="actions"><button class="ghost" data-nukeclose="1">Cancel</button><button class="send danger" data-nukego="${esc(n.id)}" ${n.loading || n.busy ? "disabled" : ""}>${n.busy ? "Nuking…" : `Nuke ${esc(n.id)}`}</button></div>`;
    m.innerHTML = `<div class="box"><h2>${done ? "Nuked" : "Nuke"} ${esc(n.id)}</h2><p class="muted">${esc(n.title || "")}</p>${body}${actions}</div>`;
    m.hidden = false;
  }

  async function nukeAsk(id) {
    const s = state && state.sessions.find((x) => x.id === id);
    nuking = { id, title: s ? s.title : "", loading: true, unassign: true };
    nukeRender();
    try {
      nuking.preview = await api(`/api/sessions/${encodeURIComponent(id)}/nuke-preview`);
    } catch (e) {
      nuking.error = /not found/i.test(e.message) ? "Restart Cycle Control to enable Nuke." : e.message;
    }
    nuking.loading = false;
    nukeRender();
  }

  async function nukeGo(id) {
    if (!nuking || nuking.id !== id) return;
    const unassign = nuking.unassign !== false;
    nukeClose();
    if (selected === id) { selected = ""; store.set("selected", ""); }
    try {
      state = await api(`/api/sessions/${encodeURIComponent(id)}/nuke`, { unassign });
    } catch (e) {
      message = `Nuke ${id}: ${e.message}`;
    }
    railJson = ""; headJson = ""; bodyJson = "";
    render();
  }

  document.addEventListener("change", (e) => {
    if (e.target.matches("[data-nukeunassign]") && nuking) { nuking.unassign = e.target.checked; nukeRender(); }
  });

  const toasted = new Set();
  function toast(text, bad) {
    const box = $("toasts");
    const el = document.createElement("div");
    el.className = `toast ${bad ? "bad" : ""}`;
    el.textContent = text;
    box.appendChild(el);
    setTimeout(() => el.remove(), bad ? 12000 : 5000);
  }

  function nukeCard(n) {
    const steps = n.steps.map((st) => `<span class="${st.ok ? (st.skipped ? "skip" : "ok") : "bad"}">${esc(st.step)}</span>`).join(" ");
    const now = n.current ? `<span class="live">${esc(n.current)}…</span>` : "";
    const failed = n.done && n.failed.length
      ? `<div class="chip nuke-bad" title="${esc(n.failed.map((f) => `${f.step}: ${f.detail}`).join("\n"))}">Nuke incomplete: ${esc(n.failed.map((f) => f.step).join(", "))}</div>
         <div class="nuke-why">${n.failed.map((f) => `<div><b>${esc(f.step)}</b> ${esc(f.detail)}</div>`).join("")}</div>
         <div class="actions"><button class="go" data-nukeretry="${esc(n.id)}">Retry nuke</button><button class="go off" data-nukedismiss="${esc(n.id)}">Dismiss</button></div>`
      : "";
    return `<div class="agent nuking ${n.done ? "failed" : ""}">
      <div class="a-top"><span class="dot err"></span><span class="id">${esc(n.id)}</span><span class="t" title="${esc(n.title)}">${esc(n.title)}</span></div>
      <div class="a-meta"><b>${n.done ? "Nuke failed" : "Nuking…"}</b>${now}</div>
      <div class="nuke-steps">${steps}</div>
      ${failed}
    </div>`;
  }

  function nukeWatch() {
    for (const n of Object.values(state.nukes || {})) {
      if (!n.done || toasted.has(n.id)) continue;
      toasted.add(n.id);
      if (n.ok) {
        toast(`${n.id} nuked`);
        api(`/api/nukes/${encodeURIComponent(n.id)}/dismiss`, {}).catch(() => {});
      } else {
        toast(`${n.id} nuke incomplete: ${n.failed.map((f) => `${f.step} (${f.detail})`).join("; ")}`, true);
      }
    }
  }

  function nukeClose() {
    nuking = null;
    nukeRender();
  }

  function submitSay() {
    const input = $("say");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    send({ text });
  }
  document.addEventListener("input", (e) => {
    const area = e.target.closest("[data-draft]");
    if (area) drafts[area.dataset.draft] = area.value;
  });

  $("say").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitSay(); } });

  $("setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      state = await api("/api/settings", { linearApiKey: $("setup-key").value });
      $("setup-error").textContent = state.error || "";
      render();
    } catch (err) {
      $("setup-error").textContent = err.message;
    }
  });

  async function refreshActivity() {
    if (!selected || !session()) return;
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(selected)}/activity`);
      activity = { ...data, unavailable: false };
    } catch (e) {
      activity = { feed: [], question: null, unavailable: true };
    }
    render();
  }

  let assetsVersion = "";
  async function poll() {
    try {
      state = await api("/api/state");
      // The app's own files changed (an edit or an update): load them instead of running the old ones.
      if (state.assets && assetsVersion && state.assets !== assetsVersion) return location.reload();
      assetsVersion = state.assets || assetsVersion;
      render();
      await refreshActivity();
    } catch {}
  }
  // The server remembers these across restarts; the page's own storage is only a first guess.
  fetch("/api/prefs").then((r) => r.json()).then((p) => {
    if (p.filter) filter = p.filter;
    if (p.startAs) startAs = p.startAs;
    if (p.agentView) agentView = p.agentView === "open" ? "open" : "done";
    railJson = ""; bodyJson = "";
    if (state) render();
  }).catch(() => {});
  poll();
  setInterval(poll, 1500);
  setInterval(() => { railJson = ""; bodyJson = ""; render(); }, 20000);
})();
