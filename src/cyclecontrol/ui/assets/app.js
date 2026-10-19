(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const safeUrl = (u) => (/^https:\/\//.test(u || "") ? u : "");
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };

  const PHASES = [
    ["refine", "Refine"], ["plan", "Plan"], ["breakdown", "Break"], ["execute", "Build"],
    ["gates", "Gates"], ["review", "Review"], ["pr", "PR"], ["ci", "CI"], ["human", "You"],
  ];

  let state = null;
  let activity = { feed: [], question: null, unavailable: false };
  let filter = store.get("filter", "mine");
  let startAs = store.get("startAs", "plan");
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
    if (!res.ok) throw new Error(data.error || res.statusText);
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

  const phaseIndex = (phase) => PHASES.findIndex(([key]) => key === phase);
  const session = () => (state ? state.sessions.find((s) => s.id === selected) : null);
  const outcomeOf = (s) => s.outcome || s.health;
  const dotClass = (o) => ({ working: "work", waiting: "wait", done: "done", failed: "err" })[o] || "done";

  function strip(s) {
    const at = phaseIndex(s.phase);
    return PHASES.map((_, i) => `<i class="${i < at ? "d" : i === at ? "c" : ""}"></i>`).join("");
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

  function commentChip(s) {
    const c = s.comments;
    if (!c) return "";
    const pickup = c.waiting && !c.needsYou ? `<div class="chip pickup" data-comments="${esc(s.id)}"><span class="pulse"></span>picking up ${c.waiting} review comment${c.waiting === 1 ? "" : "s"}</div>` : "";
    const poll = pollLine(c) ? `<div class="chip dim" data-comments="${esc(s.id)}" title="Click for details">${esc(pollLine(c))}</div>` : "";
    const lane = c.lane ? `<div class="chip lane k-${esc(c.lane.replace(/[^a-z]+/g, "-"))}" data-comments="${esc(s.id)}">${esc(c.lane)}</div>` : "";
    if (!c.addressed && !c.needsYou && !c.waiting) return lane + poll;
    if (pickup) return `${lane}${pickup}<div class="chip" data-comments="${esc(s.id)}">addressed ${c.addressed} · ${c.waiting} waiting</div>${poll}`;
    const needs = c.needsYou ? ` · <b>${c.needsYou} needs you</b>` : "";
    const waiting = c.waiting ? ` · ${c.waiting} waiting` : "";
    return `${lane}<div class="chip" data-comments="${esc(s.id)}" title="Show each comment, what was done and the reply">addressed ${c.addressed}${needs}${waiting}</div>${poll}`;
  }

  function agentCard(s) {
    const o = outcomeOf(s);
    const label = (PHASES[phaseIndex(s.phase)] || [s.phase, s.phase === "starting" ? "starting" : s.phase])[1];
    let right = "";
    if (s.approved) right = "<span>queued by you</span>";
    else if (o === "waiting") right = '<span class="needs">needs you</span>';
    else if (o === "failed") right = "<span>failed</span>";
    else if (o === "done") right = "<span>done</span>";
    else if (s.activity) right = `<span class="live">${esc(s.activity)}</span>`;
    return `<div class="agent ${selected === s.id ? "sel" : ""} ${o === "waiting" ? "waiting" : ""}" data-open="${esc(s.id)}">
      <div class="a-top"><span class="dot ${dotClass(o)}"></span><span class="id">${esc(s.id)}</span><span class="t" title="${esc(s.title)}">${esc(s.title)}</span></div>
      <div class="strip">${strip(s)}</div>
      <div class="a-meta"><b>${esc(label)}</b>${right}</div>
      ${commentChip(s)}
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
    const busy = starting.has(i.id);
    const pri = i.priority === 1 ? "ur" : i.priority === 2 ? "hi" : "";
    const resume = i.hasWorktree && i.startable !== false ? `<button class="go alt" data-resume="${esc(i.id)}" ${busy ? "disabled" : ""}>Resume</button>` : "";
    return `<div class="row">
      <i class="pri ${pri}"></i>
      <a class="id" href="${esc(safeUrl(i.url))}" target="_blank">${esc(i.id)}</a>
      <span class="t" title="${esc(i.title)}">${esc(i.title)}</span>
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
    const slots = state.maxBuilders ? ` · ${state.slotsUsed}/${state.maxBuilders} builders` : "";
    $("cycle-meta").textContent = c ? `${c.days_left}d left · ${c.done}/${c.total}${slots}` : "";
    $("burn").style.right = c && c.total ? `${100 - Math.round((c.done / c.total) * 100)}%` : "100%";
    document.querySelectorAll("#filter button").forEach((b) => b.classList.toggle("on", b.dataset.filter === filter));
    document.querySelectorAll("#startas button").forEach((b) => b.classList.toggle("on", b.dataset.startas === startAs));
    const err = $("error");
    err.hidden = !(state.error || message);
    err.textContent = message || state.error;

    const rank = { waiting: 0, working: 1, failed: 2, done: 3 };
    const agents = [...state.sessions].sort((a, b) => rank[outcomeOf(a)] - rank[outcomeOf(b)]);
    const groups = (state.groups || []).filter((g) => filter === "team" || g.isMine);
    const inGroup = new Set((state.groups || []).flatMap((g) => [g.id, ...g.children.map((c) => c.id)]));
    const issuesById = Object.fromEntries(state.issues.map((i) => [i.id, i]));
    const sessionsById = Object.fromEntries(state.sessions.map((x) => [x.id, x]));
    const queue = state.queue || [];
    const shown = state.issues.filter((i) => i.isOpen && !i.hasSession && !i.isQueued && !inGroup.has(i.id) && (filter === "team" || i.isMine));
    const progress = shown.filter((i) => i.isStarted);
    const next = shown.filter((i) => !i.isStarted);
    const section = (title, rows, html, empty) =>
      `<div class="sec">${title}<span class="n">${rows.length}</span></div>` + (rows.length ? html : `<div class="none">${empty}</div>`);
    const queueRow = (q) => `<div class="row"><i class="pri k-queued"></i><span class="id">${esc(q.id)}</span><span class="t" title="${esc(q.title)}">${esc(q.title)}</span><span class="tag">#${q.position}</span><span class="go-group"><button class="go off" data-cancelq="${esc(q.id)}" title="Remove from the queue">Cancel</button></span></div>`;
    $("list").innerHTML =
      section("Agents", agents, agents.map(agentCard).join(""), "No builders running.") +
      (queue.length ? section("Queued", queue, queue.map(queueRow).join(""), "") : "") +
      (groups.length ? section("Stacks", groups, groups.map((g) => groupBlock(g, issuesById, sessionsById)).join(""), "") : "") +
      section("In progress", progress, progress.map(issueRow).join(""), "Nothing in progress.") +
      section("Up next", next, next.map(issueRow).join(""), "Nothing queued.");
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
    pill.className = `pill ${dotClass(o)}`;
    pill.className = `pill ${s.approved ? "done" : dotClass(o)}`;
    pill.textContent = s.approved ? "Queued by you" : { working: "Working", waiting: "Needs you", failed: "Failed", done: "Done" }[o];
    $("m-linear").href = safeUrl(s.url);
    $("m-pr").hidden = !s.prUrl;
    $("m-pr").href = safeUrl(s.prUrl);
    const at = phaseIndex(s.phase);
    $("m-steps").innerHTML = PHASES.map(([, label], i) => `<div class="st ${i < at ? "d" : i === at ? "c" : ""} ${i === at && o !== "waiting" ? "work" : ""}"><i></i><span>${label}</span></div>`).join("");
    const c = s.comments || { items: [], needsYou: 0 };
    const showComments = c.items.length || c.lane || c.hasPr;
    const cnt = c.needsYou ? `<span class="cnt">· ${c.needsYou} needs you</span>` : "";
    const tabs = [["activity", "Activity", ""], ...(showComments ? [["comments", `Comments${c.items.length ? " · " + c.items.length : ""}`, cnt]] : []), ["terminal", "Terminal", ""]];
    $("tabs").innerHTML = tabs.map(([key, label, cnt]) => `<button data-tab="${key}" class="${tab === key ? "on" : ""}">${label}${cnt}</button>`).join("");
    const banner = $("m-banner");
    const cm = s.comments;
    const plural = (n) => (n === 1 ? "" : "s");
    if (cm && tab !== "comments" && cm.needsYou) {
      banner.className = "banner need";
      banner.innerHTML = `<span class="pulse"></span><b>${cm.needsYou} review comment${plural(cm.needsYou)} need${cm.needsYou === 1 ? "s" : ""} you</b><span class="muted">${cm.addressed} addressed</span><button class="ghost" data-comments="${esc(s.id)}">Open comments</button>`;
      banner.hidden = false;
    } else if (cm && tab !== "comments" && cm.waiting) {
      const who = (cm.authors || []).map((a) => "@" + a).join(", ");
      banner.className = "banner work";
      banner.innerHTML = `<span class="pulse"></span><b>Picking up ${cm.waiting} review comment${plural(cm.waiting)}${who ? " from " + esc(who) : ""}</b><span class="muted">${cm.addressed} addressed so far</span><button class="ghost" data-comments="${esc(s.id)}">Open comments</button>`;
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
    $("say").placeholder = `Answer or instruct ${s.id}…`;
    $("composer").hidden = tab === "terminal";
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
    if (s.comments && s.comments.needsYou) {
      parts.push(`<div class="ask"><b>Review comments</b><div class="md"><p>${s.comments.needsYou} comment${s.comments.needsYou === 1 ? "" : "s"} need${s.comments.needsYou === 1 ? "s" : ""} you. The builder drafted a reply for each.</p></div><div class="actions"><button class="send" data-comments="${esc(s.id)}">Open comments</button></div></div>`);
    }
    if (s.attention) {
      parts.push(`<div class="ask"><b>Rebase conflict</b><div class="md"><p>${esc(s.attention)}</p><p>The builder has not been told. Resolve it yourself in <code>${esc(s.worktree)}</code>, then press the button.</p></div><div class="actions"><button class="send" data-resolved="${esc(s.id)}">I resolved it</button></div></div>`);
    } else if (s.prUrl) {
      const busy = approving.has(s.id);
      const done = s.approved
        ? `<p class="okline">You added mergequeue${s.slackLink ? ` and posted in Slack: <a href="${esc(safeUrl(s.slackLink))}" target="_blank" rel="noopener">message</a>` : ""}.</p>`
        : `<div class="actions"><button class="ghost" data-changes="${esc(s.id)}">Request changes</button><button class="send" data-approve="${esc(s.id)}" ${busy ? "disabled" : ""}>${busy ? "Approving…" : confirming === s.id ? "Confirm approve" : "Approve"}</button></div>`;
      parts.push(`<div class="ask pr ${s.approved ? "approved" : ""}"><b>${s.approved ? "Queued by you" : "PR ready for you"}</b><p>${rich(s.prUrl)}</p>${done}</div>`);
    } else if (q || waiting) {
      const raw = q ? q.text : s.latest || "The agent is idle and waiting for your input.";
      const text = q && q.kind === "text" && q.options.length ? raw.split("\n").filter((l) => !/^\s*\d+[.)]\s/.test(l)).join("\n").trim() : raw;
      const opts = (q ? q.options : []).map((o) => `<button class="opt" data-opt="${esc(o.key)}" data-kind="${esc(q.kind)}"><kbd>${esc(o.key)}</kbd>${esc(o.label)}</button>`).join("");
      parts.push(`<div class="ask"><b>${q && q.kind === "plan" ? "Plan approval" : "Waiting on you"}</b><div class="md">${window.renderMarkdown(text)}</div>${opts ? `<div class="opts">${opts}</div>` : ""}</div>`);
    }
    if (!parts.length) parts.push(`<div class="notice">Nothing from the agent yet.</div>`);
    return `<div class="tl">${parts.join("")}</div>`;
  }

  const STATE_TEXT = { fixed: "fixed", replied: "replied", skipped: "skipped", needs_you: "needs you", posted: "reply posted", dismissed: "dismissed", queued: "queued for the builder", delivered: "with the builder" };

  const linkShas = (text, repoUrl) =>
    repoUrl ? String(text).replace(/\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b/g, (sha) => `[${sha}](${repoUrl}/commit/${sha})`) : String(text);

  function commentsHtml(s) {
    const c = s.comments || { items: [], addressed: 0, needsYou: 0, waiting: 0 };
    const explain = c.hasPr ? `<div class="c-poll"><span>${esc(pollLine(c))}</span><span class="muted">Polls GitHub every ${Math.round((c.intervalSeconds || 300) / 60)} min for review comments on PR #${c.prNumber}. New ones go to the builder when it is idle.</span><button class="ghost" data-checknow="1">${checking ? "Checking…" : "Check now"}</button></div>` : "";
    const st = c.status;
    const approval = st ? `<span class="${st.approved ? "ok" : "amber"}">${st.approved ? "approved by " + st.approvers.join(", ") : st.blocking.length ? "changes requested by " + st.blocking.join(", ") : "not approved"}</span> · CI ${esc((st.ci || "unknown").toLowerCase())} · ${st.unresolved} unresolved` : "";
    const requeue = c.requeue ? `<div class="c-sum">${c.requeue.status === "requeued" ? "Re-queued" : "Not re-queued"}${c.requeue.reasons && c.requeue.reasons.length ? ": " + esc(c.requeue.reasons.join("; ")) : ""}</div>` : "";
    const head = explain + ((approval || c.lane) ? `<div class="c-sum">${c.lane ? `<b class="ok">${esc(c.lane)}</b> · ` : ""}${approval}</div>${requeue}` : "");
    if (!c.items.length) return `<div class="tl">${head}<div class="notice">No review comments yet.</div></div>`;
    const rows = c.items.map((it) => {
      const place = it.path ? `${esc(it.path)}${it.line ? ":" + it.line : ""}` : "";
      const where = place ? ` · ${it.url ? `<a href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener">${place}</a>` : place}` : "";
      const link = it.url ? `<a class="gh" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener">GitHub ↗</a>` : "";
      const label = it.state === "posted" && it.resolved ? "reply posted · resolved" : STATE_TEXT[it.state] || it.state;
      const head = `<div class="c-head"><b>@${esc(it.author)}</b><span class="mono muted">${esc(it.kind)}${where}</span>${link}<span class="tag k-${esc(it.state)}">${esc(label)}</span></div>`;
      const body = `<details class="c-body"><summary>${esc(String(it.body).split("\n")[0].slice(0, 110))}</summary><div class="md">${window.renderMarkdown(it.body)}</div></details>`;
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
      if (!term.host || term.id !== s.id) {
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

  const term = { xterm: null, fit: null, ws: null, id: "", host: null };

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
    $("setup").hidden = state.configured;
    $("app").hidden = !state.configured;
    if (!state.configured) return;
    const s = session();
    const justWaiting = s && outcomeOf(s) === "waiting" && !wasWaiting[s.id];
    state.sessions.forEach((x) => { wasWaiting[x.id] = outcomeOf(x) === "waiting"; });

    const rail = JSON.stringify([state.cycle, state.issues, state.sessions.map((x) => [x.id, x.title, x.phase, outcomeOf(x), x.activity, x.approved, x.comments]), state.error, filter, startAs, selected, [...starting], message, state.groups, state.queue, [...expanded], state.slotsUsed]);
    if (rail !== railJson) { railJson = rail; renderRail(); }
    const head = JSON.stringify([s && [s.id, s.title, s.phase, outcomeOf(s), s.prUrl, s.approved, s.attention, s.comments, checking, confirming, [...approving], tab], tab, selected]);
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
    bodyJson = "";
    headJson = "";
    railJson = "";
    activity = { feed: [], question: null, unavailable: false };
    refreshActivity().then(() => { const b = $("body"); b.scrollTop = b.scrollHeight; if (session() && outcomeOf(session()) === "waiting") $("say").focus(); });
    render();
  }

  async function start(id, resume, base) {
    starting.add(id);
    message = "";
    render();
    try {
      state = await api("/api/start", { issueId: id, mode: startAs, resume, base: base || "" });
      if (state.sessions.some((x) => x.id === id)) select(id);
      else if ((state.queue || []).some((q) => q.id === id)) message = `${id} is queued: ${state.slotsUsed}/${state.maxBuilders} builders are running.`;
    } catch (e) {
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
    const t = e.target.closest("[data-open],[data-start],[data-resume],[data-startready],[data-toggle],[data-cancelq],[data-filter],[data-startas],[data-tab],[data-key],[data-opt],[data-approve],[data-changes],[data-resolved],[data-comments],[data-checknow],[data-postreply],[data-dismiss],#m-stop,#send,summary");
    if (!t) return;
    const d = t.dataset;
    if (t.tagName === "SUMMARY") {
      const det = t.parentElement;
      requestAnimationFrame(() => { det.open ? openTools.add(det.dataset.key) : openTools.delete(det.dataset.key); });
      return;
    }
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
    if (d.startas) { startAs = d.startas; store.set("startAs", startAs); return render(); }
    if (d.tab) { tab = d.tab; bodyJson = ""; headJson = ""; return render(); }
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
      message = /not found/i.test(e.message) ? "Restart Cycle Control to enable Approve." : e.message;
    } finally {
      approving.delete(id);
      railJson = "";
      bodyJson = "";
      render();
    }
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

  async function poll() {
    try {
      state = await api("/api/state");
      render();
      await refreshActivity();
    } catch {}
  }
  poll();
  setInterval(poll, 1500);
  setInterval(() => { railJson = ""; bodyJson = ""; render(); }, 20000);
})();
