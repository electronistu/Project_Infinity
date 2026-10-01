"use strict";

/* Project Infinity — web client (vanilla JS, no build step). */

const state = {
  ws: null,
  sessionId: null,
  model: null,
  contextWindow: 0,
  contextTokens: 0,
  turn: 0,
  busy: false,
  connected: false,
  ready: false,
  lastCommandType: null,
  cur: null,       // current streaming assistant bubble
  lastTool: null,  // last tool block awaiting a result
  worldsMeta: [],  // enriched /api/worlds entries
  activeSaveName: null,
  endAfterSave: false,
};

const $ = (id) => document.getElementById(id);
const transcript = $("transcript");
const statsBox = $("stats");
const input = $("input");
const sendBtn = $("send");
const statusEl = $("status");

/* ── text helpers ─────────────────────────────────────────── */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function stripTokens(s) {
  return String(s)
    .replace(/\{\{_NEED_AN_OTHER_PROMPT\}\}/g, "")
    .replace(/\{\{_NEED_ANOTHER_PROMPT\}\}/g, "")
    .replace(/\{\{_CONTINUE_EXECUTION\}\}/g, "")
    .replace(/\{\{_SYNC_DATABASE\}\}/g, "");
}

function fmtNum(n) { return Number(n || 0).toLocaleString(); }

function mdToHtml(raw) {
  const lines = stripTokens(raw).split("\n");
  const out = [];
  let inList = false;
  let inMech = false;
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  const closeMech = () => { if (inMech) { out.push("</div>"); inMech = false; } };
  for (const rawLine of lines) {
    let line = escapeHtml(rawLine)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
    if (/^\s*\*{0,2}\s*mechanics:?\s*\*{0,2}\s*$/i.test(rawLine)) {
      closeList(); closeMech();
      out.push('<div class="mechanics"><div class="mech-title">Mechanics</div>');
      inMech = true;
      continue;
    }
    if (/^\s*[-*]\s+/.test(rawLine)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + line.replace(/^\s*[-*]\s+/, "") + "</li>");
      continue;
    }
    closeList();
    const h = rawLine.match(/^(#{1,6})\s+/);
    if (h) {
      closeMech();
      const lvl = Math.min(6, h[1].length) + 1;
      out.push(`<h${lvl}>` + line.replace(/^#+\s+/, "") + `</h${lvl}>`);
      continue;
    }
    if (rawLine.trim() === "") continue;
    closeMech();
    out.push("<p>" + line + "</p>");
  }
  closeList();
  closeMech();
  return out.join("\n");
}

function scrollToBottom(force) {
  const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 160;
  if (force || nearBottom) transcript.scrollTop = transcript.scrollHeight;
}

/* ── small DOM builders ───────────────────────────────────── */

function addPlayer(text) {
  const el = document.createElement("div");
  el.className = "msg player";
  el.innerHTML = `<div class="msg-head">You</div><div class="msg-body"></div>`;
  el.querySelector(".msg-body").innerHTML = mdToHtml(text);
  transcript.appendChild(el);
  scrollToBottom(true);
}

function addSystem(text) {
  const el = document.createElement("div");
  el.className = "system";
  el.textContent = text;
  transcript.appendChild(el);
  scrollToBottom();
}

function addError(text) {
  const el = document.createElement("div");
  el.className = "error-msg";
  el.textContent = text;
  transcript.appendChild(el);
  scrollToBottom(true);
}

function addTimeline(entry) {
  const el = document.createElement("div");
  el.className = "timeline-note";
  el.textContent = "Timeline checkpoint saved\n\n" + entry;
  transcript.appendChild(el);
  scrollToBottom();
}

function createBubble(label) {
  const el = document.createElement("div");
  el.className = "msg gm";
  const head = document.createElement("div");
  head.className = "msg-head";
  head.textContent = label === "awakening" ? "Game Master · awakening" : "Game Master";
  const think = document.createElement("details");
  think.className = "thinking";
  think.innerHTML = `<summary>thinking</summary><div class="thinking-body"></div>`;
  const body = document.createElement("div");
  body.className = "msg-body";
  el.appendChild(head);
  el.appendChild(think);
  el.appendChild(body);
  transcript.appendChild(el);
  scrollToBottom(true);
  return { el, body, think, thinkBody: think.querySelector(".thinking-body"), raw: "", thinking: "" };
}

function addToolCall(name, args) {
  const el = document.createElement("div");
  el.className = "tool-block";
  el.innerHTML =
    `<div class="tool-head">tool · ${escapeHtml(name)}</div>` +
    `<pre class="tool-args">${escapeHtml(JSON.stringify(args || {}))}</pre>` +
    `<pre class="tool-result"></pre>`;
  transcript.appendChild(el);
  scrollToBottom();
  return el;
}

function fillToolResult(el, text, isError) {
  if (!el) return;
  if (isError) el.classList.add("error");
  el.querySelector(".tool-result").textContent = text || "";
}

/* ── stats rendering ──────────────────────────────────────── */

/* Foldable sheet sections. Every section starts collapsed; the player's
   choices persist in localStorage as a global UI preference (slug-keyed by
   title, so they survive the sidebar rebuild on every refresh). */

const SHEET_KEY = "infinity.sheet.collapsed";
let collapsedSet = null;     // Set<string> of collapsed section slugs
let collapsedSaved = false;  // did localStorage hold a value yet?
let cardSeq = 0;             // unique ids for aria-controls

function sectionSlug(title) {
  return String(title).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

function loadCollapsed() {
  if (collapsedSet) return;
  collapsedSet = new Set();
  try {
    const raw = localStorage.getItem(SHEET_KEY);
    if (raw !== null) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        arr.forEach((s) => collapsedSet.add(String(s)));
        collapsedSaved = true;
      }
    }
  } catch (e) { /* ignore */ }
}

// No saved preference yet -> everything collapsed; otherwise remember per section.
function isCollapsed(slug) {
  loadCollapsed();
  return collapsedSaved ? collapsedSet.has(slug) : true;
}

function persistCollapsed() {
  collapsedSaved = true;
  try { localStorage.setItem(SHEET_KEY, JSON.stringify([...collapsedSet])); } catch (e) { /* ignore */ }
}

function applyCollapsed(cardEl, collapsed) {
  collapsedSet.add(cardEl.dataset.section);
  if (!collapsed) collapsedSet.delete(cardEl.dataset.section);
  cardEl.classList.toggle("collapsed", collapsed);
  const btn = cardEl.querySelector(".card-toggle");
  if (btn) btn.setAttribute("aria-expanded", String(!collapsed));
}

function toggleSection(cardEl) {
  applyCollapsed(cardEl, !cardEl.classList.contains("collapsed"));
  persistCollapsed();
}

function setAllCollapsed(collapsed) {
  statsBox.querySelectorAll(".card").forEach((c) => applyCollapsed(c, collapsed));
  persistCollapsed();
}

function card(title, children) {
  const slug = sectionSlug(title);
  const collapsed = isCollapsed(slug);

  const c = document.createElement("div");
  c.className = "card" + (collapsed ? " collapsed" : "");
  c.dataset.section = slug;

  const bodyId = "sheet-body-" + (++cardSeq);
  const h = document.createElement("h3");
  h.className = "card-head";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "card-toggle";
  btn.setAttribute("aria-expanded", String(!collapsed));
  btn.setAttribute("aria-controls", bodyId);
  const label = document.createElement("span");
  label.className = "card-title";
  label.textContent = title;
  const chev = document.createElement("span");
  chev.className = "card-chevron";
  chev.setAttribute("aria-hidden", "true");
  btn.appendChild(label);
  btn.appendChild(chev);
  btn.addEventListener("click", () => toggleSection(c));
  h.appendChild(btn);
  c.appendChild(h);

  const body = document.createElement("div");
  body.className = "card-body";
  body.id = bodyId;
  const inner = document.createElement("div");
  inner.className = "card-body-inner";
  children.forEach((x) => inner.appendChild(x));
  body.appendChild(inner);
  c.appendChild(body);

  return c;
}

function row(k, v, desc) {
  const d = document.createElement("div");
  d.className = "row";
  const kk = document.createElement("span");
  kk.className = "k";
  kk.textContent = k;
  if (desc) {
    kk.setAttribute("data-desc", desc);
    kk.setAttribute("tabindex", "0");
    kk.setAttribute("title", desc);
  }
  const vv = document.createElement("span");
  vv.className = "v";
  vv.textContent = (v === null || v === undefined || v === "") ? "—" : String(v);
  d.appendChild(kk);
  d.appendChild(vv);
  return d;
}

function descTag(item) {
  const name = typeof item === "string" ? item : (item && item.name) ? item.name : JSON.stringify(item);
  const desc = (item && typeof item === "object" && item.description) ? item.description : "";
  const s = document.createElement("span");
  s.className = "tag" + (desc ? " has-desc" : "");
  s.textContent = name;
  if (desc) {
    s.setAttribute("data-desc", desc);
    s.setAttribute("tabindex", "0");
    s.setAttribute("title", desc);
  }
  return s;
}

function tagList(items) {
  const d = document.createElement("div");
  (items || []).forEach((t) => d.appendChild(descTag(t)));
  return d;
}

// A labelled group of chips (small label above, chips with hover tooltips below).
function field(label, items) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const k = document.createElement("div");
  k.className = "field-k";
  k.textContent = label;
  wrap.appendChild(k);
  wrap.appendChild(tagList(items));
  return wrap;
}

/* ── item description tooltip ────────────────────────────── */

const tooltipEl = $("item-tooltip");
let tooltipTarget = null;

function showTooltip(target) {
  const text = target.getAttribute("data-desc");
  if (!text) return;
  tooltipTarget = target;
  tooltipEl.textContent = text;
  tooltipEl.classList.remove("hidden");
  positionTooltip(target);
}
function positionTooltip(target) {
  const r = target.getBoundingClientRect();
  const t = tooltipEl.getBoundingClientRect();
  let top = r.top - t.height - 8;
  if (top < 8) top = Math.min(window.innerHeight - t.height - 8, r.bottom + 8);
  let left = r.left + r.width / 2 - t.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8));
  tooltipEl.style.top = Math.max(8, top) + "px";
  tooltipEl.style.left = left + "px";
}
function hideTooltip() {
  tooltipEl.classList.add("hidden");
  tooltipTarget = null;
}
function bindTooltips(container) {
  container.addEventListener("pointerover", (e) => {
    const el = e.target.closest("[data-desc]");
    if (el) showTooltip(el);
  });
  container.addEventListener("pointerout", (e) => {
    const el = e.target.closest("[data-desc]");
    if (el && !el.contains(e.relatedTarget)) hideTooltip();
  });
  container.addEventListener("focusin", (e) => {
    const el = e.target.closest("[data-desc]");
    if (el) showTooltip(el);
  });
  container.addEventListener("focusout", hideTooltip);
  container.addEventListener("click", (e) => {
    const el = e.target.closest("[data-desc]");
    if (!el) { hideTooltip(); return; }
    if (tooltipTarget === el && !tooltipEl.classList.contains("hidden")) hideTooltip();
    else showTooltip(el);
  });
}

function hpBar(cur, max) {
  const wrap = document.createElement("div");
  wrap.className = "hpbar";
  const fill = document.createElement("div");
  fill.className = "hpbar-fill";
  const c = Number(cur), m = Number(max);
  const pct = (isFinite(c) && isFinite(m) && m > 0) ? Math.max(0, Math.min(100, (c / m) * 100)) : 0;
  fill.style.width = pct + "%";
  if (pct <= 25) fill.classList.add("low");
  else if (pct <= 50) fill.classList.add("mid");
  wrap.appendChild(fill);
  return wrap;
}

function renderStats(d) {
  statsBox.innerHTML = "";
  if (!d || !d.character) {
    statsBox.innerHTML = '<div class="muted">No sheet data.</div>';
    return;
  }
  const c = d.character || {};
  statsBox.appendChild(card("Character", [
    row("Name", c.name), row("Race", c.race), row("Class", c.character_class),
    row("Level", c.level), row("Background", c.background), row("Alignment", c.alignment),
    row("Gold", c.gold), row("XP", c.xp),
  ]));

  const cb = d.combat || {};
  const prof = (cb.proficiency_bonus === null || cb.proficiency_bonus === undefined || cb.proficiency_bonus === "")
    ? "—" : "+" + cb.proficiency_bonus;
  const combat = [
    row("HP", `${cb.hp_current}/${cb.hp_max}`),
    row("AC", cb.armor_class), row("Speed", cb.speed),
    row("Proficiency", prof), row("Hit Dice", `${cb.hit_dice_count}d${cb.hit_dice_size}`),
  ];
  if (cb.temporary_hit_points) combat.push(row("Temp HP", cb.temporary_hit_points));
  statsBox.appendChild(card("Combat", [hpBar(cb.hp_current, cb.hp_max), ...combat]));

  if (d.stats && d.stats.length) {
    const grid = document.createElement("div");
    grid.className = "abilities";
    d.stats.forEach((a) => {
      const el = document.createElement("div");
      el.className = "ability";
      el.innerHTML = `<div class="a-k">${escapeHtml(a.key)}</div>` +
        `<div class="a-v">${escapeHtml(String(a.value))}</div>` +
        `<div class="a-m">${escapeHtml(String(a.modifier == null ? "" : a.modifier))}</div>`;
      grid.appendChild(el);
    });
    statsBox.appendChild(card("Ability Scores", [grid]));
  }

  const sp = d.spellcasting;
  if (sp) {
    const kids = [row("Ability", sp.ability), row("Save DC", sp.dc),
                  row("Attack", sp.attack_modifier == null ? "—" : "+" + sp.attack_modifier)];
    if (sp.cantrips && sp.cantrips.length) kids.push(field("Cantrips", sp.cantrips));
    if (sp.spells_known && sp.spells_known.length) kids.push(field("Known", sp.spells_known));
    if (sp.spellbook && sp.spellbook.length) kids.push(field("Spellbook", sp.spellbook));
    if (sp.spells_prepared && sp.spells_prepared.length) kids.push(field("Prepared", sp.spells_prepared));
    if (sp.slots && Object.keys(sp.slots).length) {
      kids.push(row("Slots", Object.entries(sp.slots).map(([k, v]) => `L${k}:${v}`).join("  ")));
    }
    statsBox.appendChild(card("Spellcasting", kids));
  }

  const p = d.proficiencies || {};
  const profKids = [];
  if (p.skills && p.skills.length) profKids.push(field("Skills", p.skills));
  if (p.saves && p.saves.length) profKids.push(field("Saves", p.saves));
  if (p.weapons && p.weapons.length) profKids.push(field("Weapons", p.weapons));
  if (p.armor && p.armor.length) profKids.push(field("Armor", p.armor));
  if (p.features && p.features.length) profKids.push(field("Features", p.features));
  if (p.languages && p.languages.length) profKids.push(field("Languages", p.languages));
  if (profKids.length) statsBox.appendChild(card("Proficiencies", profKids));

  if (d.inventory && d.inventory.length) statsBox.appendChild(card("Inventory", [tagList(d.inventory)]));
  if (d.consumables && Object.keys(d.consumables).length) {
    statsBox.appendChild(card("Consumables", Object.entries(d.consumables).map(([k, v]) => row(k, v))));
  }
  if (d.active_effects && d.active_effects.length) {
    const kids = [];
    d.active_effects.forEach((e) => {
      kids.push(row(e.name, ""));
      (e.rows || []).forEach((r) => kids.push(row("  " + r.field, r.value)));
    });
    statsBox.appendChild(card("Active Effects", kids));
  }
  if (d.reputation && d.reputation.length) {
    const kids = [];
    d.reputation.forEach((r) => {
      kids.push(row(`${r.category} · ${r.faction}`, ""));
      (r.entries || []).forEach((en) => kids.push(row("  " + ((en && en.name) || en), "", (en && en.description) || "")));
    });
    statsBox.appendChild(card("Reputation", kids));
  }
}

/* ── composer / status ────────────────────────────────────── */

function setStatus(t) { statusEl.textContent = t; }
function updateTurn() { const el = $("turn-label"); if (el) el.textContent = "turn " + state.turn; }
function setConn(t, cls) { const el = $("conn"); el.textContent = t; el.className = "conn " + cls; }

function updateCtxMeter(tokens, window) {
  const fill = $("ctx-fill");
  if (!fill) return;
  const w = Number(window) || 0;
  const t = Number(tokens) || 0;
  const pct = w > 0 ? Math.max(0, Math.min(100, (t / w) * 100)) : 0;
  fill.style.width = pct + "%";
}

/* ── theme (dark default, light optional) ────────────────── */

const THEME_KEY = "infinity-theme";
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, document.documentElement.dataset.theme); } catch (e) { /* ignore */ }
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* ignore */ }
  applyTheme(saved === "light" ? "light" : "dark");
}
function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
}

/* ── mobile character-sheet drawer ───────────────────────── */

function openSheet() {
  $("sidebar").classList.add("open");
  const b = $("sheet-backdrop");
  b.hidden = false;
  requestAnimationFrame(() => b.classList.add("open"));
}
function closeSheet() {
  $("sidebar").classList.remove("open");
  const b = $("sheet-backdrop");
  b.classList.remove("open");
  setTimeout(() => { b.hidden = true; }, 200);
}
function toggleSheet() {
  if ($("sidebar").classList.contains("open")) closeSheet(); else openSheet();
}

function updateComposer() {
  const enabled = state.connected && state.ready && !state.busy;
  input.disabled = !enabled;
  sendBtn.disabled = !enabled;
  $("refresh-stats").disabled = !(state.connected && state.ready);
  $("sync-open").disabled = !(state.connected && state.ready);
  $("end-session").disabled = !state.sessionId;
}

function send(msg) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(msg));
}

function requestStats() {
  if (!state.connected || !state.ready) return;
  state.lastCommandType = "stats";
  send({ type: "stats" });
}

function requestSync() {
  if (!state.connected || !state.ready) return;
  state.lastCommandType = "sync";
  send({ type: "sync" });
}

function submitInput() {
  const text = input.value.trim();
  if (!text) return;
  addPlayer(text);
  state.lastCommandType = "action";
  send({ type: "action", text });
  input.value = "";
  input.focus();
}

/* ── event handling ───────────────────────────────────────── */

function handleEvent(evt) {
  switch (evt.type) {
    case "ready":
      state.model = evt.model;
      state.contextWindow = evt.context_window || 0;
      $("world-label").textContent = evt.world || "";
      $("model-label").textContent = evt.model || "";
      $("ctx-label").textContent = `context 0 / ${fmtNum(state.contextWindow)}`;
      updateCtxMeter(0, state.contextWindow);
      state.turn = evt.turn || 0;
      updateTurn();
      state.activeSaveName = (evt.world || "").replace(/\.wwf$/i, "");
      addSystem(`Session ready · ${evt.tools ? evt.tools.length : 0} engine tools · ${evt.model}`);
      break;

    case "assistant_start":
      if (evt.label === "timeline" || evt.label === "sync") {
        state.cur = null; // not shown as chat; timeline surfaces via its own event
      } else {
        state.cur = createBubble(evt.label);
      }
      break;

    case "thinking_delta":
      if (state.cur) {
        state.cur.thinking += evt.text;
        state.cur.thinkBody.textContent = state.cur.thinking;
      }
      break;

    case "narrative_delta":
      if (state.cur) {
        state.cur.raw += evt.text;
        state.cur.body.textContent = stripTokens(state.cur.raw);
      }
      scrollToBottom();
      break;

    case "assistant_end":
      if (state.cur) {
        state.cur.body.innerHTML = mdToHtml(state.cur.raw);
        if (!state.cur.thinking.trim()) state.cur.think.remove();
        if (!stripTokens(state.cur.raw).trim() && !state.cur.thinking.trim()) state.cur.el.remove();
        state.cur = null;
      }
      scrollToBottom();
      break;

    case "tool_call":
      state.lastTool = addToolCall(evt.name, evt.arguments);
      break;

    case "tool_result":
      fillToolResult(state.lastTool, evt.text, evt.is_error);
      state.lastTool = null;
      break;

    case "context":
      state.contextTokens = evt.tokens || 0;
      $("ctx-label").textContent = `context ${fmtNum(evt.tokens)} / ${fmtNum(evt.window)}`;
      updateCtxMeter(evt.tokens, evt.window);
      break;

    case "paused":
      setStatus("synchronizing with the engine…");
      break;

    case "awakening_end":
      state.ready = true;
      setStatus("Awaiting your action");
      updateComposer();
      requestStats();
      break;

    case "turn_end":
      if (evt.turn != null) { state.turn = evt.turn; updateTurn(); }
      setStatus("Awaiting your action");
      if (state.lastCommandType === "action") requestStats();
      break;

    case "stats":
      renderStats(evt.data);
      break;

    case "notice":
      addSystem(`${evt.title ? evt.title + ": " : ""}${evt.text || ""}`);
      break;

    case "timeline":
      addTimeline(evt.entry);
      break;

    case "saved":
      state.activeSaveName = evt.name || state.activeSaveName;
      addSystem(`Saved as ${evt.wwf || (evt.name + ".wwf")}`);
      loadWorlds().catch(() => {});
      if (state.endAfterSave) { state.endAfterSave = false; finishEnd(); }
      break;

    case "busy":
      state.busy = !!evt.value;
      updateComposer();
      setStatus(state.busy ? "GM is thinking…" : (state.ready ? "Awaiting your action" : "Loading…"));
      break;

    case "error":
      addError(evt.message || "unknown error");
      break;

    case "fatal":
      addError("Fatal: " + (evt.message || "unknown"));
      setStatus("Session failed");
      break;

    case "closed":
      state.ready = false;
      updateComposer();
      setStatus("Session closed");
      break;

    default:
      break;
  }
}

/* ── bootstrap ────────────────────────────────────────────── */

function connect() {
  setConn("connecting", "off");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${state.sessionId}`);
  state.ws = ws;
  ws.onopen = () => { state.connected = true; setConn("connected", "on"); updateComposer(); setStatus("Loading…"); };
  ws.onclose = () => { state.connected = false; setConn("disconnected", "off"); updateComposer(); setStatus("Disconnected"); };
  ws.onerror = () => setConn("error", "err");
  ws.onmessage = (e) => { try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
}

function showStartError(msg) {
  const el = $("start-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function startSession(wwf) {
  const model = $("model-select").value;
  const temperature = parseFloat($("temp-input").value);
  if (state.sessionId) {
    try { await fetch(`/api/sessions/${state.sessionId}`, { method: "DELETE" }); } catch (e) { /* ignore */ }
  }
  if (state.ws) { try { state.ws.close(); } catch (e) { /* ignore */ } state.ws = null; }
  state.connected = false; state.ready = false; state.busy = false;
  transcript.innerHTML = "";
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wwf, model, temperature }),
  });
  if (!res.ok) throw new Error("HTTP " + res.status + " — " + (await res.text()));
  const data = await res.json();
  state.sessionId = data.session_id;
  state.model = data.model;
  state.contextWindow = data.context_window || 0;
  state.activeSaveName = (data.world || wwf).replace(/\.wwf$/i, "");
  $("world-label").textContent = data.world || wwf;
  $("model-label").textContent = data.model || model;
  $("ctx-label").textContent = `context 0 / ${fmtNum(state.contextWindow)}`;
  updateCtxMeter(0, state.contextWindow);
  $("start-overlay").classList.add("hidden");
  updateComposer();
  connect();
}

async function begin() {
  $("begin").disabled = true;
  try {
    await startSession($("world-select").value);
  } catch (err) {
    $("begin").disabled = false;
    $("start-overlay").classList.remove("hidden");
    showStartError(String(err.message || err));
  }
}

/* ── world / model lists ─────────────────────────────────── */

async function loadWorlds() {
  const r = await fetch("/api/worlds").then((x) => x.json());
  const saves = Array.isArray(r.saves) ? r.saves : [];
  const list = saves.length
    ? saves.map((w) => (typeof w === "string" ? { file: w } : w))
    : (r.worlds || []).map((w) => (typeof w === "string" ? { file: w } : w));
  state.worldsMeta = list;
  const files = list.map((w) => String(w.file == null ? "" : w.file)).filter(Boolean);
  const sel = $("world-select");
  const current = sel.value;
  sel.innerHTML = "";
  files.forEach((f) => {
    const o = document.createElement("option");
    o.value = f; o.textContent = f;
    sel.appendChild(o);
  });
  if (current && files.includes(current)) sel.value = current;
  const hasWorlds = files.length > 0;
  $("world-delete").disabled = !hasWorlds;
  $("begin").disabled = !hasWorlds;
  return files;
}

async function loadModels() {
  const m = await fetch("/api/models").then((x) => x.json());
  const sel = $("model-select");
  sel.innerHTML = "";
  (m.models || []).forEach((x) => {
    const o = document.createElement("option");
    o.value = x.id; o.textContent = x.label || x.id;
    if (x.id === m.default) o.selected = true;
    sel.appendChild(o);
  });
  $("temp-input").value = (m.default_temperature != null) ? m.default_temperature : 1.0;
}

/* ── save / load ─────────────────────────────────────────── */

function openSave() {
  if (!state.connected) { addError("Not connected to a session."); return; }
  $("save-name").value = state.activeSaveName || "";
  $("save-error").classList.add("hidden");
  $("save-overlay").classList.remove("hidden");
  setTimeout(() => { const el = $("save-name"); el.focus(); if (el.select) el.select(); }, 0);
}
function closeSave() { $("save-overlay").classList.add("hidden"); }
function showSaveError(msg) { const el = $("save-error"); el.textContent = msg; el.classList.remove("hidden"); }

function saveFileExists(name) {
  const trimmed = name.trim();
  if (!trimmed) return false;
  const file = (trimmed.toLowerCase().endsWith(".wwf") ? trimmed : trimmed + ".wwf").toLowerCase();
  return (state.worldsMeta || []).some((w) => (w.file || "").toLowerCase() === file);
}

function confirmSave() {
  const name = $("save-name").value.trim();
  if (!name) { showSaveError("Enter a name."); return; }
  if (!state.connected) { showSaveError("Not connected."); return; }
  if (saveFileExists(name) && !window.confirm(`"${name}" already exists. Overwrite it?`)) return;
  send({ type: "save", name });
  closeSave();
}

async function openLoad() {
  try { await loadWorlds(); } catch (e) { /* ignore */ }
  renderLoadList();
  $("load-overlay").classList.remove("hidden");
}
function closeLoad() { $("load-overlay").classList.add("hidden"); }

function renderLoadList() {
  const box = $("load-list");
  box.innerHTML = "";
  const list = (state.worldsMeta || []).slice().sort((a, b) => (b.modified || 0) - (a.modified || 0));
  if (!list.length) { box.innerHTML = '<div class="muted">No saved games found.</div>'; return; }
  list.forEach((w) => {
    const row = document.createElement("div");
    row.className = "load-row";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "load-item";
    const who = w.character ? `${w.character} — ${w.class || "?"} L${w.level == null ? "?" : w.level}` : "unknown character";
    const when = w.modified ? new Date(w.modified * 1000).toLocaleString() : "";
    btn.innerHTML =
      `<span class="load-file">${escapeHtml(w.file)}</span>` +
      `<span class="load-meta">${escapeHtml(who)}${when ? " · " + escapeHtml(when) : ""}</span>`;
    btn.onclick = () => loadGame(w.file);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "load-del";
    del.title = "Delete this save";
    del.textContent = "✕";
    del.onclick = (e) => { e.stopPropagation(); deleteWorld(w.file); };

    row.appendChild(btn);
    row.appendChild(del);
    box.appendChild(row);
  });
}

async function loadGame(file) {
  if (state.sessionId && !window.confirm(`Load "${file}"? The current session will be closed.`)) return;
  closeLoad();
  try {
    await startSession(file);
    setStatus(`Loaded ${file}`);
  } catch (err) {
    addError("Load failed: " + String(err.message || err));
  }
}

async function deleteWorld(file) {
  if (!file) return;
  if (!window.confirm(`Delete "${file}"?\nThis removes the .wwf, .player and .timeline. It cannot be undone.`)) return;
  try {
    const res = await fetch(`/api/worlds/${encodeURIComponent(file)}`, { method: "DELETE" });
    if (!res.ok) throw new Error("HTTP " + res.status + " — " + (await res.text()));
    const data = await res.json();
    addSystem(`Deleted ${data.deleted}`);
    await loadWorlds();
    if (!$("load-overlay").classList.contains("hidden")) renderLoadList();
  } catch (err) {
    addError("Delete failed: " + String(err.message || err));
  }
}

function openEnd() {
  if (!state.sessionId) { resetToHome(); return; }
  $("end-overlay").classList.remove("hidden");
}
function closeEnd() { $("end-overlay").classList.add("hidden"); }

function saveAndEnd() {
  closeEnd();
  if (!state.connected) { finishEnd(); return; }
  state.endAfterSave = true;
  setStatus("saving before ending…");
  send({ type: "save" });
}

function endWithoutSaving() {
  closeEnd();
  finishEnd();
}

async function finishEnd() {
  const sid = state.sessionId;
  if (sid) {
    try { await fetch(`/api/sessions/${sid}`, { method: "DELETE" }); } catch (e) { /* ignore */ }
  }
  if (state.ws) { try { state.ws.close(); } catch (e) { /* ignore */ } state.ws = null; }
  resetToHome();
}

function resetToHome() {
  state.sessionId = null;
  state.connected = false;
  state.ready = false;
  state.busy = false;
  state.activeSaveName = null;
  state.endAfterSave = false;
  transcript.innerHTML = "";
  statsBox.innerHTML = '<div class="muted">Start a session to load your sheet.</div>';
  setConn("disconnected", "off");
  setStatus("Not connected");
  $("world-label").textContent = "no world";
  $("model-label").textContent = "no model";
  $("ctx-label").textContent = "context —";
  updateCtxMeter(0, 0);
  $("start-overlay").classList.remove("hidden");
  hideTooltip();
  updateComposer();
  loadWorlds().catch(() => {});
}

/* ── character creation wizard ───────────────────────────── */

const createState = { id: null, step: 0, current: null, busy: false };

function openCreate() {
  $("create-overlay").classList.remove("hidden");
  $("create-transcript").innerHTML = "";
  $("create-controls").innerHTML = "";
  $("create-prompt").textContent = "Starting the Forge…";
  $("create-error").classList.add("hidden");
  $("create-submit").disabled = true;
  createState.id = null; createState.step = 0; createState.current = null; createState.busy = false;
  startCreation();
}

function closeCreate() {
  $("create-overlay").classList.add("hidden");
}

async function startCreation() {
  try {
    const res = await fetch("/api/creation", { method: "POST" });
    if (!res.ok) throw new Error("HTTP " + res.status + " — " + (await res.text()));
    handleCreateResponse(await res.json());
  } catch (err) {
    showCreateError(String(err.message || err));
  }
}

function handleCreateResponse(res) {
  createState.id = res.creation_id;
  (res.steps || []).forEach(appendCreateStep);
  if (res.terminal) { finishCreate(res.terminal); return; }
  const prompts = (res.steps || []).filter((s) => s.type === "prompt");
  if (prompts.length) renderCreatePrompt(prompts[prompts.length - 1]);
}

function appendCreateStep(step) {
  if (step.type !== "notice") return;
  const el = document.createElement("div");
  el.className = "wizard-note";
  el.textContent = step.text;
  $("create-transcript").appendChild(el);
  $("create-transcript").scrollTop = $("create-transcript").scrollHeight;
}

function renderCreatePrompt(step) {
  createState.current = step;
  createState.step += 1;
  $("create-progress").textContent = `step ${createState.step}`;
  $("create-prompt").textContent = step.prompt;
  const controls = $("create-controls");
  controls.innerHTML = "";
  $("create-error").classList.add("hidden");

  if (step.kind === "text" || step.kind === "number") {
    const inp = document.createElement("input");
    inp.id = "create-input";
    inp.type = step.kind === "number" ? "number" : "text";
    if (step.kind === "number") { inp.min = step.min; inp.max = step.max; }
    if (step.max_length) inp.maxLength = step.max_length;
    if (step.default !== undefined && step.default !== null) inp.value = step.default;
    controls.appendChild(inp);
    setTimeout(() => { inp.focus(); if (inp.select) inp.select(); }, 0);
  } else if (step.kind === "single") {
    (step.options || []).forEach((opt) => {
      const lab = document.createElement("label");
      lab.className = "opt";
      lab.innerHTML = `<input type="radio" name="c-single" value="${escapeHtml(opt.id)}"> <span>${escapeHtml(opt.label)}</span>`;
      controls.appendChild(lab);
    });
  } else if (step.kind === "multi") {
    (step.options || []).forEach((opt) => {
      const checked = (step.default_checked || []).includes(opt.id) ? "checked" : "";
      const lab = document.createElement("label");
      lab.className = "opt";
      lab.innerHTML = `<input type="checkbox" name="c-multi" value="${escapeHtml(opt.id)}" ${checked}> <span>${escapeHtml(opt.label)}</span>`;
      controls.appendChild(lab);
    });
    if (step.min_choices || step.max_choices != null) {
      const hint = document.createElement("div");
      hint.className = "wizard-hint";
      const lo = step.min_choices ? `at least ${step.min_choices}` : "";
      const hi = step.max_choices != null ? `at most ${step.max_choices}` : "";
      hint.textContent = "select " + [lo, hi].filter(Boolean).join(", ");
      controls.appendChild(hint);
    }
  } else if (step.kind === "pointbuy") {
    renderPointBuy(step);
  }
  if (step.error) showCreateError(step.error);
  if (step.kind !== "pointbuy") $("create-submit").disabled = false;
}

/* ── point-buy: all six abilities at once ─────────────────── */

function pbCost(step, score) {
  const costs = step.costs || {};
  const c = costs[String(score)];
  return c === undefined ? 0 : Number(c);
}

function pbSpent(step, alloc) {
  return (step.abilities || []).reduce((sum, a) => sum + pbCost(step, alloc[a.key]), 0);
}

function renderPointBuy(step) {
  const controls = $("create-controls");
  const min = step.min != null ? step.min : 8;
  const max = step.max != null ? step.max : 15;
  const budget = step.budget != null ? step.budget : 27;
  const alloc = {};
  (step.abilities || []).forEach((a) => {
    const d = Number(step.default && step.default[a.key]);
    alloc[a.key] = isFinite(d) ? d : min;
  });

  const wrap = document.createElement("div");
  wrap.className = "wizard-pointbuy";
  const rows = [];
  const remainingEl = document.createElement("div");
  remainingEl.className = "pb-remaining";

  const remaining = () => budget - pbSpent(step, alloc);

  function refresh() {
    rows.forEach(({ a, input, dec, inc, finalEl }) => {
      if (document.activeElement !== input) input.value = alloc[a.key];
      finalEl.textContent = "\u2192 " + (alloc[a.key] + (a.bonus || 0));
      finalEl.title = a.bonus ? `+${a.bonus} racial bonus` : "no racial bonus";
      dec.disabled = alloc[a.key] <= min;
      const next = alloc[a.key] + 1;
      const gain = next <= max ? pbCost(step, next) - pbCost(step, alloc[a.key]) : Infinity;
      inc.disabled = next > max || gain > remaining();
    });
    const rem = remaining();
    remainingEl.textContent = `${rem} / ${budget} points remaining`;
    remainingEl.classList.toggle("done", rem === 0);
    $("create-submit").disabled = rem !== 0;
  }

  (step.abilities || []).forEach((a) => {
    const row = document.createElement("div");
    row.className = "pb-row";

    const label = document.createElement("span");
    label.className = "pb-k";
    label.textContent = a.label;

    const dec = document.createElement("button");
    dec.type = "button"; dec.className = "pb-btn"; dec.textContent = "\u2212";
    dec.setAttribute("aria-label", `decrease ${a.label}`);

    const input = document.createElement("input");
    input.type = "number";
    input.className = "pb-val";
    input.dataset.ability = a.key;
    input.min = min; input.max = max;
    input.setAttribute("aria-label", a.label);

    const inc = document.createElement("button");
    inc.type = "button"; inc.className = "pb-btn"; inc.textContent = "+";
    inc.setAttribute("aria-label", `increase ${a.label}`);

    const finalEl = document.createElement("span");
    finalEl.className = "pb-final";

    dec.addEventListener("click", () => { alloc[a.key] = Math.max(min, alloc[a.key] - 1); refresh(); });
    inc.addEventListener("click", () => { alloc[a.key] = Math.min(max, alloc[a.key] + 1); refresh(); });
    input.addEventListener("input", () => {
      const v = parseInt(input.value, 10);
      if (isNaN(v)) return;
      alloc[a.key] = Math.max(min, Math.min(max, v));
      refresh();
    });
    input.addEventListener("blur", refresh);

    row.append(label, dec, input, inc, finalEl);
    wrap.appendChild(row);
    rows.push({ a, input, dec, inc, finalEl });
  });

  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "ghost pb-reset";
  reset.textContent = "reset";
  reset.addEventListener("click", () => {
    (step.abilities || []).forEach((a) => { alloc[a.key] = min; });
    refresh();
  });
  wrap.appendChild(reset);
  wrap.appendChild(remainingEl);

  controls.appendChild(wrap);
  refresh();
}

function collectCreateAnswer() {
  const step = createState.current;
  if (!step) return null;
  if (step.kind === "text" || step.kind === "number") {
    return $("create-input").value;
  }
  if (step.kind === "single") {
    const checked = document.querySelector('input[name="c-single"]:checked');
    return checked ? checked.value : null;
  }
  if (step.kind === "multi") {
    return Array.from(document.querySelectorAll('input[name="c-multi"]:checked')).map((c) => c.value);
  }
  if (step.kind === "pointbuy") {
    const alloc = {};
    document.querySelectorAll('#create-controls input[data-ability]').forEach((inp) => {
      alloc[inp.dataset.ability] = parseInt(inp.value, 10);
    });
    return alloc;
  }
  return null;
}

function describeAnswer(step, value) {
  if (step.kind === "single") {
    const opt = (step.options || []).find((o) => o.id === String(value));
    return opt ? opt.label : String(value);
  }
  if (step.kind === "multi") {
    return (Array.isArray(value) ? value : []).map((v) => {
      const opt = (step.options || []).find((x) => x.id === String(v));
      return opt ? opt.label : v;
    }).join(", ");
  }
  if (step.kind === "pointbuy") {
    return (step.abilities || []).map((a) => `${a.label} ${value[a.key]}`).join(", ");
  }
  return String(value);
}

async function submitCreateAnswer() {
  const step = createState.current;
  if (!step || createState.busy) return;
  if ($("create-submit").disabled) return;
  const value = collectCreateAnswer();
  if (value === null) { showCreateError("Please make a selection."); return; }
  createState.busy = true;
  $("create-submit").disabled = true;
  const row = document.createElement("div");
  row.className = "wizard-choice";
  row.textContent = `${step.prompt} → ${describeAnswer(step, value)}`;
  $("create-transcript").appendChild(row);
  $("create-transcript").scrollTop = $("create-transcript").scrollHeight;
  try {
    const res = await fetch(`/api/creation/${createState.id}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status + " — " + (await res.text()));
    handleCreateResponse(await res.json());
  } catch (err) {
    showCreateError(String(err.message || err));
    $("create-submit").disabled = false;
  } finally {
    createState.busy = false;
  }
}

async function cancelCreate() {
  if (createState.id) {
    try { await fetch(`/api/creation/${createState.id}/cancel`, { method: "POST" }); } catch (e) { /* ignore */ }
  }
  closeCreate();
}

function showCreateError(msg) {
  const el = $("create-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function finishCreate(terminal) {
  $("create-submit").disabled = true;
  if (terminal.type === "done") {
    const ok = document.createElement("div");
    ok.className = "wizard-done";
    ok.textContent = `Forged ${terminal.name} — ${terminal.race} ${terminal.character_class} L${terminal.level} → ${terminal.wwf}`;
    $("create-transcript").appendChild(ok);
    const worlds = await loadWorlds();
    if (worlds.includes(terminal.wwf)) $("world-select").value = terminal.wwf;
    $("start-error").classList.add("hidden");
    setTimeout(closeCreate, 1100);
  } else if (terminal.type === "cancelled") {
    showCreateError("Creation cancelled.");
  } else {
    showCreateError(terminal.message || "Creation failed.");
    if (terminal.traceback) console.error(terminal.traceback);
  }
}

/* ── bootstrap ────────────────────────────────────────────── */

async function init() {
  $("begin").addEventListener("click", begin);
  $("open-create").addEventListener("click", openCreate);
  $("create-cancel").addEventListener("click", cancelCreate);
  $("create-submit").addEventListener("click", submitCreateAnswer);
  $("create-controls").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitCreateAnswer(); }
  });
  sendBtn.addEventListener("click", submitInput);
  $("refresh-stats").addEventListener("click", requestStats);
  $("collapse-all").addEventListener("click", () => setAllCollapsed(true));
  $("expand-all").addEventListener("click", () => setAllCollapsed(false));
  $("sync-open").addEventListener("click", requestSync);
  $("end-session").addEventListener("click", openEnd);
  $("end-cancel").addEventListener("click", closeEnd);
  $("end-discard").addEventListener("click", endWithoutSaving);
  $("end-save").addEventListener("click", saveAndEnd);
  bindTooltips(statsBox);
  window.addEventListener("scroll", hideTooltip, true);
  window.addEventListener("resize", hideTooltip);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitInput(); }
  });
  $("toggle-tools").addEventListener("change", (e) => {
    transcript.classList.toggle("show-tools", e.target.checked);
    send({ type: "flags", verbose: e.target.checked });
  });
  $("toggle-thinking").addEventListener("change", (e) => {
    transcript.classList.toggle("show-thinking", e.target.checked);
  });
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("sheet-toggle").addEventListener("click", toggleSheet);
  $("sheet-close").addEventListener("click", closeSheet);
  $("sheet-backdrop").addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    hideTooltip();
    if (!$("save-overlay").classList.contains("hidden")) closeSave();
    else if (!$("load-overlay").classList.contains("hidden")) closeLoad();
    else if (!$("end-overlay").classList.contains("hidden")) closeEnd();
    else if ($("sidebar").classList.contains("open")) closeSheet();
  });
  $("save-open").addEventListener("click", openSave);
  $("load-open").addEventListener("click", openLoad);
  $("world-delete").addEventListener("click", () => deleteWorld($("world-select").value));
  $("save-cancel").addEventListener("click", closeSave);
  $("save-confirm").addEventListener("click", confirmSave);
  $("load-cancel").addEventListener("click", closeLoad);
  $("save-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); confirmSave(); }
  });

  try {
    const worlds = await Promise.all([loadWorlds(), loadModels()]).then((r) => r[0]);
    if (!worlds.length) {
      $("begin").disabled = true;
      showStartError("No .wwf worlds found in output/. Forge a new character.");
    }
  } catch (err) {
    showStartError("Could not reach the server: " + err);
  }
}

initTheme();
init();
