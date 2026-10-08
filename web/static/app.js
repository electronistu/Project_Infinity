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
  combatRoster: [], // live combatants (name, hp, ac, conditions, sheet lines) for tooltips
  combatOrder: [],  // authoritative initiative order (names)
  combatInitiative: {}, // name -> {roll, modifier, total}
  mechanicsLines: [], // engine-composed Mechanics block for the current turn
  worldsMeta: [],  // enriched /api/worlds entries
  activeSaveName: null,
  endAfterSave: false,
  imagesEnabled: false,                 // opt-in; portrait + storyline scene images
  imageStatus: { available: false },    // server capability (/api/images/status)
  imageModels: [],                      // [{id,label}] offered for portraits + scenes
  iconModels: [],                       // [{id,label}] offered for sheet icons (adds the local engine)
  models: [],                           // [{id,label,provider}] offered by /api/models
  imageModel: "",                       // in-story (portrait + scenes) model id
  iconModel: "",                        // sheet-icon model id
  imageStyle: "",                       // optional player art style for portraits + scenes
  thinkEnabled: false,                  // thinking pane SHOWN (display-only; the model always thinks)
  iconUrls: {},                         // "kind/slug" -> url for shared sheet icons
  sheetMode: "icons",                   // "icons" | "generate" | "text"
  lastStats: null,                      // last rendered sheet (for icon diffing)
  iconGenBusy: false,                   // one on-the-fly generation run at a time
  iconGenEpoch: 0,                      // bumped on session change to cancel a run
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
    .replace(/\{\{_CONTINUE_EXECUTION\}\}/g, "");
}

function fmtNum(n) { return Number(n || 0).toLocaleString(); }

function mdToHtml(raw) {
  const lines = stripTokens(raw).split("\n");
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  for (const rawLine of lines) {
    let line = escapeHtml(rawLine)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
    if (/^\s*[-*]\s+/.test(rawLine)) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push("<li>" + line.replace(/^\s*[-*]\s+/, "") + "</li>");
      continue;
    }
    closeList();
    const h = rawLine.match(/^(#{1,6})\s+/);
    if (h) {
      const lvl = Math.min(6, h[1].length) + 1;
      out.push(`<h${lvl}>` + line.replace(/^#+\s+/, "") + `</h${lvl}>`);
      continue;
    }
    if (rawLine.trim() === "") continue;
    out.push("<p>" + line + "</p>");
  }
  closeList();
  return out.join("\n");
}

function scrollToBottom(force) {
  const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 160;
  if (force || nearBottom) transcript.scrollTop = transcript.scrollHeight;
}

/* ── small DOM builders ───────────────────────────────────── */

function addPlayer(text) {
  finalizeTurnBubble();
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

/* One GM bubble per turn (not per assistant message): a turn can span several
   model rounds (tool calls, thinking-only retries, resumes), all of which nest
   into this single bubble so the player sees one "Game Master" heading. */
function createTurnBubble(label) {
  const el = document.createElement("div");
  el.className = "msg gm";
  const head = document.createElement("div");
  head.className = "msg-head";
  head.textContent = label === "awakening" ? "Game Master · awakening" : "Game Master";
  const think = document.createElement("details");
  think.className = "thinking";
  think.innerHTML = `<summary>thinking</summary><div class="thinking-body"></div>`;
  const flow = document.createElement("div");
  flow.className = "msg-flow";
  el.appendChild(head);
  el.appendChild(think);
  el.appendChild(flow);
  transcript.appendChild(el);
  scrollToBottom(true);
  return {
    el,
    think,
    thinkBody: think.querySelector(".thinking-body"),
    flow,
    thinking: "",
    seg: null,      // current narrative .msg-body
    segRaw: "",     // its raw markdown source
    hasNarrative: false,
  };
}

function beginSegment(tb) {
  if (!tb.seg) {
    tb.seg = document.createElement("div");
    tb.seg.className = "msg-body";
    tb.flow.appendChild(tb.seg);
    tb.segRaw = "";
  }
  return tb.seg;
}

function endSegment(tb) {
  if (!tb.seg) return;
  tb.seg.innerHTML = mdToHtml(tb.segRaw);
  tagCombatantNames(tb.seg);
  tagInitiative(tb.seg);
  tb.seg = null;
  tb.segRaw = "";
}

/* Wrap any known combatant name in the rendered message so hovering it shows the
   live sheet. Names are matched longest-first; existing spans/code are skipped. */
function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function tagCombatantNames(root) {
  const names = (state.combatRoster || [])
    .filter((c) => c && c.name && !c.is_player)
    .map((c) => String(c.name))
    .sort((a, b) => b.length - a.length);
  if (!root || !names.length) return;
  const known = new Set(names);
  const re = new RegExp("(" + names.map(escapeRegExp).join("|") + ")", "g");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p || p.closest(".combatant") || p.closest("pre") || p.closest("code") ||
          p.closest(".tool-block") || p.closest("button") || p.closest("select")) {
        return NodeFilter.FILTER_REJECT;
      }
      return node.nodeValue && node.nodeValue.trim()
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) nodes.push(n);
  for (const node of nodes) {
    const parts = node.nodeValue.split(re);
    if (parts.length < 2) continue;
    const frag = document.createDocumentFragment();
    for (const part of parts) {
      if (!part) continue;
      if (known.has(part)) {
        const span = document.createElement("span");
        span.className = "combatant";
        span.setAttribute("data-combatant", part);
        span.textContent = part;
        frag.appendChild(span);
      } else {
        frag.appendChild(document.createTextNode(part));
      }
    }
    node.parentNode.replaceChild(frag, node);
  }
}

/* Make the engine's "Initiative Order" label green and hoverable (the order is a tooltip). */
function tagInitiative(root) {
  if (!root || !(state.combatOrder || []).length) return;
  const phrase = "Initiative Order";
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p || p.closest(".initiative") || p.closest("pre") || p.closest("code") ||
          p.closest(".tool-block") || p.closest("button") || p.closest("select")) {
        return NodeFilter.FILTER_REJECT;
      }
      return node.nodeValue && node.nodeValue.includes(phrase)
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) nodes.push(n);
  for (const node of nodes) {
    const parts = node.nodeValue.split(phrase);
    const frag = document.createDocumentFragment();
    parts.forEach((part, i) => {
      if (i > 0) {
        const span = document.createElement("span");
        span.className = "initiative";
        span.setAttribute("data-initiative", "1");
        span.textContent = phrase;
        frag.appendChild(span);
      }
      if (part) frag.appendChild(document.createTextNode(part));
    });
    node.parentNode.replaceChild(frag, node);
  }
}

/* The engine composes the Mechanics block; append it once, at the end of the turn's narrative. */
function appendMechanics(tb) {
  const lines = (state.mechanicsLines || []).map((l) => String(l)).filter((l) => l.trim() !== "");
  state.mechanicsLines = [];
  if (!tb || !lines.length) return;
  const panel = document.createElement("div");
  panel.className = "mechanics";
  panel.innerHTML = '<div class="mech-title">Mechanics</div>'
    + lines.map((l) => '<div class="mech-line">' + escapeHtml(l) + "</div>").join("");
  tb.flow.appendChild(panel);
  tb.hasNarrative = true;
  tagCombatantNames(panel);
  tagInitiative(panel);
}

function updateThinkingLabel() {
  const el = $("thinking-label");
  if (!el) return;
  el.textContent = state.thinkEnabled ? "thinking on" : "thinking off";
  el.classList.toggle("on", state.thinkEnabled);
}

function finalizeTurnBubble() {
  const tb = state.cur;
  if (!tb) return;
  endSegment(tb);
  const hasThinking = !!tb.thinking.trim();
  if (hasThinking) {
    console.debug(`[thinking] captured ${tb.thinking.length} chars this turn`);
  } else if (state.thinkEnabled) {
    // Keep the pane visible so an empty turn is obvious rather than silent.
    tb.thinkBody.textContent = "(no thinking this turn)";
  }
  if (!hasThinking && !state.thinkEnabled) tb.think.remove();
  if (!tb.hasNarrative && !hasThinking && !state.thinkEnabled) {
    // Nothing the toggles could show except bare tool calls: keep those at the
    // top level (for a tools-on player) and drop the empty shell.
    tb.flow.querySelectorAll(".tool-block").forEach((block) => transcript.insertBefore(block, tb.el));
    tb.el.remove();
  }
  state.cur = null;
}

function addToolCall(name, args, parent) {
  const el = document.createElement("div");
  el.className = "tool-block";
  el.innerHTML =
    `<div class="tool-head">tool · ${escapeHtml(name)}</div>` +
    `<pre class="tool-args">${escapeHtml(JSON.stringify(args || {}))}</pre>` +
    `<div class="tool-gm-wrap" hidden><div class="tool-gm-label">GM sees</div>` +
    `<pre class="tool-gm"></pre></div>` +
    `<pre class="tool-result"></pre>`;
  (parent || transcript).appendChild(el);
  scrollToBottom();
  return el;
}

function fillToolResult(el, text, isError, gmText) {
  if (!el) return;
  if (isError) el.classList.add("error");
  el.querySelector(".tool-result").textContent = text || "";
  // The trimmed view is what the GM actually read — shown separately (green), and
  // only when it differs from the full result (pass-through mechanics are identical).
  const gm = gmText == null ? "" : String(gmText);
  const wrap = el.querySelector(".tool-gm-wrap");
  if (wrap && gm && gm !== String(text || "")) {
    wrap.hidden = false;
    el.querySelector(".tool-gm").textContent = gm;
  }
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

function row(k, v, desc, iconKey) {
  const d = document.createElement("div");
  d.className = "row";
  const kk = document.createElement("span");
  kk.className = "k";
  const icon = iconImg(iconKey, "row-icon");
  const iconUrl = (state.sheetMode === "text" || !iconKey) ? null : state.iconUrls[iconKey];
  if (icon) kk.appendChild(icon);
  kk.appendChild(document.createTextNode(k));
  if (iconUrl || desc) {
    if (iconUrl) {
      kk.setAttribute("data-icon", iconUrl);
      kk.setAttribute("data-name", (v === null || v === undefined || v === "") ? String(k) : String(v));
    }
    if (desc) kk.setAttribute("data-desc", desc);
    kk.setAttribute("tabindex", "0");
    kk.setAttribute("aria-describedby", "item-tooltip");
  }
  const vv = document.createElement("span");
  vv.className = "v";
  vv.textContent = (v === null || v === undefined || v === "") ? "—" : String(v);
  d.appendChild(kk);
  d.appendChild(vv);
  return d;
}

/* Shared sheet icon: an <img> when the server says the key exists, else null
   (callers fall back to plain text — no broken images). */
function iconImg(iconKey, cls) {
  if (state.sheetMode === "text") return null;
  const url = iconKey ? state.iconUrls[iconKey] : null;
  if (!url) return null;
  const img = document.createElement("img");
  img.className = cls || "chip-icon";
  img.src = url;
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  return img;
}

/* Which icon family the sheet draws from — a property of the icon MODEL, never of the
   game mode. The server applies the same rule (familyForModel) when it writes icons, so
   the two never disagree. */
function iconFamily() {
  return String(state.iconModel || "").toLowerCase().startsWith("local") ? "local" : "gemini";
}

async function loadIconIndex() {
  try {
    const data = await fetch(`/api/icons/index?family=${iconFamily()}`).then((r) => r.json());
    state.iconUrls = (data && data.icons) || {};
  } catch (e) {
    state.iconUrls = {};
  }
}

/* ── character-sheet mode + on-the-fly icon generation ─────── */

const SHEET_MODE_KEY = "infinity.sheet.mode";

function loadSheetMode() {
  let mode = "icons";
  try { mode = localStorage.getItem(SHEET_MODE_KEY) || "icons"; } catch (e) { /* ignore */ }
  return (mode === "generate" || mode === "text") ? mode : "icons";
}

function applySheetModeUI() {
  document.querySelectorAll(".sheet-mode-input").forEach((el) => {
    el.checked = (el.value === state.sheetMode);
  });
}

function initSheetMode() {
  state.sheetMode = loadSheetMode();
  applySheetModeUI();
}

function sessionActive() {
  return !!(state.sessionId && state.connected && state.ready);
}

function setSheetMode(mode) {
  state.sheetMode = (mode === "generate" || mode === "text") ? mode : "icons";
  try { localStorage.setItem(SHEET_MODE_KEY, state.sheetMode); } catch (e) { /* ignore */ }
  applySheetModeUI();
  if (sessionActive() && state.lastStats) {
    renderStats(state.lastStats);
    if (state.sheetMode === "generate") autoGenerateIcons(state.lastStats);
  }
}

/* Every {name, icon} entry a sheet carries that has no cached icon yet. */
function collectIconItems(node, out) {
  if (Array.isArray(node)) {
    node.forEach((n) => collectIconItems(n, out));
    return out;
  }
  if (node && typeof node === "object") {
    if (node.icon && node.name && !state.iconUrls[node.icon]) {
      const key = node.icon;
      if (!out.has(key)) {
        out.set(key, { key, name: String(node.name), detail: node.icon_detail || "" });
      }
    }
    Object.keys(node).forEach((k) => collectIconItems(node[k], out));
  }
  return out;
}

function missingIconItems(d) {
  const out = collectIconItems(d, new Map());
  const add = (key, name) => {
    if (key && name && !state.iconUrls[key] && !out.has(key)) {
      out.set(key, { key, name: String(name), detail: "" });
    }
  };
  // Consumables are a {name: count} map with a parallel {name: key} icon map.
  const icons = (d && d.consumable_icons) || {};
  Object.keys(icons).forEach((name) => add(icons[name], name));
  // Reputation carries kingdom_icon/faction_icon rather than a single icon.
  ((d && d.reputation) || []).forEach((r) => {
    add(r.faction_icon, r.faction);
    add(r.kingdom_icon, r.category);
  });
  return Array.from(out.values());
}

async function postIcons(items) {
  const res = await fetch("/api/icons", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items, model: state.iconModel || undefined }),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) throw new Error(data.detail || "HTTP " + res.status);
  return data;
}

async function autoGenerateIcons(d) {
  if (!sessionActive()) return;
  if (state.sheetMode !== "generate" || state.iconGenBusy) return;
  if (!state.imageStatus.available) return;
  const items = missingIconItems(d);
  if (!items.length) return;

  // Tie this run to the session it started in; cancel it if that changes.
  const epoch = state.iconGenEpoch;
  const sid = state.sessionId;
  const character = (d && d.character && d.character.name) ? d.character.name : state.activeSaveName;
  const cancelled = () => epoch !== state.iconGenEpoch || sid !== state.sessionId
    || state.sheetMode !== "generate" || !state.imageStatus.available || !sessionActive();

  state.iconGenBusy = true;
  let queue = items.slice();
  let generated = 0;
  setStatus(`generating ${items.length} icon${items.length > 1 ? "s" : ""}…`);

  try {
    while (queue.length) {
      if (cancelled()) break;
      const batch = queue.splice(0, 6);
      let data;
      try {
        data = await postIcons(batch);
      } catch (err) {
        if (cancelled()) break;
        if (window.confirm(`Icon generation failed:\n${err.message}\n\nRetry?`)) {
          queue = batch.concat(queue);
          continue;
        }
        addError("Icon generation cancelled: " + String(err.message || err));
        break;
      }
      state.iconUrls = Object.assign({}, state.iconUrls, data.icons || {});
      generated += (data.generated || []).length;
      if (cancelled()) break;
      const failed = data.failed || [];
      if (failed.length) {
        const byKey = {};
        batch.forEach((b) => { byKey[b.key] = b; });
        const again = failed.map((f) => byKey[f.key]).filter(Boolean);
        const err = (failed[0] && failed[0].error) || "unknown error";
        if (window.confirm(`Icon generation failed for ${failed.length} item(s):\n${err}\n\nRetry?`)) {
          queue = again.concat(queue);
          continue;
        }
        addError("Icon generation cancelled.");
        break;
      }
    }
  } finally {
    state.iconGenBusy = false;
  }

  if (cancelled()) {
    if (state.ready) setStatus("Awaiting your action");
    return;
  }
  if (generated && state.lastStats) renderStats(state.lastStats);
  if (generated) {
    addSystem(`Generated ${generated} icon${generated > 1 ? "s" : ""} for ${character || "this character"}.`);
  }
  if (state.ready) setStatus("Awaiting your action");
}

/* ── hover to enlarge an image (reuses the SAME <img> node) ─────────────────
   Scene action images are one-shot (served once, then deleted) and portraits are
   cache-busted, so a second <img> would re-fetch — and 404 for scenes. The
   enlarged view IS the same node, lifted to the viewport with `position: fixed`
   while hovered (which also escapes the transcript's scroll clipping). */

/* `position: fixed` resolves against the nearest ancestor that establishes a
   containing block (a transform/filter/perspective/will-change/contain), not
   always the viewport. Return that ancestor's viewport origin to correct for. */
function fixedOrigin(el) {
  let node = el && el.parentElement;
  while (node && node !== document.body) {
    const cs = getComputedStyle(node);
    if (cs.transform !== "none" || cs.filter !== "none" || cs.perspective !== "none"
        || (cs.willChange || "").indexOf("transform") !== -1
        || /paint|layout|strict|content/.test(cs.contain || "")) {
      const r = node.getBoundingClientRect();
      return { x: r.left, y: r.top };
    }
    node = node.parentElement;
  }
  return { x: 0, y: 0 };
}

function attachHoverZoom(container, img, boundsSel) {
  if (!container || !img || !window.matchMedia) return;
  if (!window.matchMedia("(hover: hover)").matches) return;
  const PAD = 10;
  let zoomed = false;

  function restore() {
    if (!zoomed) return;
    zoomed = false;
    img.classList.remove("zoomed");
    img.style.removeProperty("left");
    img.style.removeProperty("top");
    img.style.removeProperty("width");
    img.style.removeProperty("height");
    container.classList.remove("zooming");
    container.style.removeProperty("height");
  }

  function enlarge(ev) {
    if (zoomed || !img.complete || !img.naturalWidth) return;
    const rect = img.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const boundsEl = boundsSel ? document.querySelector(boundsSel) : null;
    const b = (boundsEl || document.documentElement).getBoundingClientRect();
    const maxW = Math.max(120, b.width - PAD * 2);
    const maxH = Math.max(80, b.height - PAD * 2);
    const ar = img.naturalHeight / img.naturalWidth;
    let w = Math.min(maxW, img.naturalWidth);   // never upscale past native
    let h = w * ar;
    if (h > maxH) { h = maxH; w = h / ar; }     // full frame, natural aspect
    // Centre on the cursor and clamp inside the bounds: the box then always
    // contains the cursor, so the pointer never leaves it (no flicker).
    let left = Math.min(Math.max(ev.clientX - w / 2, b.left + PAD), b.right - PAD - w);
    let top = Math.min(Math.max(ev.clientY - h / 2, b.top + PAD), b.bottom - PAD - h);
    const origin = fixedOrigin(img);
    // The img leaves the flow — hold the container's box so text does not reflow.
    container.style.height = container.getBoundingClientRect().height + "px";
    container.classList.add("zooming");
    img.classList.add("zoomed");
    img.style.width = w + "px";
    img.style.height = h + "px";
    img.style.left = (left - origin.x) + "px";
    img.style.top = (top - origin.y) + "px";
    zoomed = true;
  }

  container.addEventListener("pointerenter", enlarge);
  container.addEventListener("pointerleave", restore);
  // The node can be swapped in place (portrait regen) — collapse the zoom then.
  img.addEventListener("load", restore);
}

/* ── storyline scene images (session-only; opt-in) ─────────── */

function sceneFigure(evt) {
  const fig = document.createElement("figure");
  fig.className = "scene-figure loading";
  const img = document.createElement("img");
  const path = Array.isArray(evt.place)
    ? evt.place.map((s) => String(s || "").trim()).filter(Boolean)
    : [];
  const label = path.join(" — ");
  img.alt = label ? `Scene: ${label}` : "Scene illustration";
  img.decoding = "async";
  fig.appendChild(img);
  // Caption: the area (never the kingdom), then the place path, then time of day,
  // then weather. Every empty part is dropped; no caption when none are set.
  const area = String(evt.area || "").trim();
  const place = label;
  const meta = [evt.time_of_day, evt.weather]
    .map((v) => String(v || "").trim()).filter(Boolean);
  const caption = [area, place, ...meta].filter(Boolean).join(" · ");
  if (caption) {
    const cap = document.createElement("figcaption");
    cap.className = "scene-caption";
    cap.textContent = caption;
    fig.appendChild(cap);
  }
  attachHoverZoom(fig, img, "#transcript");
  return { fig, img };
}

function attachSceneFigure(fig) {
  if (state.cur) {
    // Reserve the float at the top of the turn so the narrative wraps around it.
    state.cur.flow.insertBefore(fig, state.cur.flow.firstChild);
    state.cur.hasNarrative = true;
    state.cur.el.classList.add("has-narrative");
  } else {
    transcript.appendChild(fig);
  }
  scrollToBottom(true);
}

async function requestSceneImage(evt, fig, img) {
  const payload = {
    session_id: evt.session_id || state.sessionId,
    description: evt.description || "",
    mood: evt.mood || "",
    kind: evt.kind || "story",
    kingdom: evt.kingdom || "",
    area: evt.area || "",
    place: Array.isArray(evt.place) ? evt.place : [],
    time_of_day: evt.time_of_day || "",
    weather: evt.weather || "",
    characters: evt.characters || {},
    establishing: evt.establishing || "",
    main_npcs: evt.main_npcs || [],
    npcs: evt.npcs || [],
    seed_change: evt.seed_change || "",
    active_effects: evt.active_effects || [],
    equipped: evt.equipped || undefined,
    model: state.imageModel || undefined,
    style: state.imageStyle || undefined,
  };
  try {
    const res = await fetch("/api/scene", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
    const action = data.action || data;  // v2 response nests the visible image
    img.onload = () => { fig.classList.remove("loading"); fig.classList.add("loaded"); };
    img.onerror = () => { fig.classList.remove("loading"); fig.classList.add("error"); };
    // `created` changes on every (re)generation, so the image refreshes.
    img.src = action.url + "?t=" + encodeURIComponent(action.created || Date.now());
  } catch (err) {
    fig.classList.remove("loading");
    fig.classList.add("error");
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "scene-retry";
    retry.textContent = "retry";
    retry.onclick = () => {
      retry.remove();
      fig.classList.remove("error");
      fig.classList.add("loading");
      requestSceneImage(evt, fig, img);
    };
    fig.appendChild(retry);
    addError("Scene image: " + String(err.message || err));
  }
}

function maybeGenerateScene(evt) {
  if (!evt || !(evt.description || "").trim()) return;
  if (!state.imagesEnabled || !state.imageStatus.available) return;
  const { fig, img } = sceneFigure(evt);
  attachSceneFigure(fig);
  requestSceneImage(evt, fig, img);
}

function descTag(item) {
  const name = typeof item === "string" ? item : (item && item.name) ? item.name : JSON.stringify(item);
  const desc = (item && typeof item === "object" && item.description) ? item.description : "";
  const weightLb = (item && typeof item === "object" && typeof item.weight === "number") ? item.weight : null;
  // Weights ride along in the tooltip so the sheet stays quiet about it (own line, so
  // multi-line stat lines never glue the weight onto the last one).
  const tip = [desc, weightLb === null ? "" : `${weightLb} lb`].filter(Boolean).join("\n");
  const iconKey = (item && typeof item === "object" && item.icon) ? item.icon : null;
  const prepared = !!(item && typeof item === "object" && item.prepared);
  const equipped = !!(item && typeof item === "object" && item.equipped);
  const slotLabel = equipped ? (item.slot || "Equipped") : "";
  const s = document.createElement("span");
  const icon = iconImg(iconKey, "chip-icon");
  if (icon) {
    // Icon-only chip: the image is the whole label; its tooltip shows the full
    // icon plus the name and description.
    s.className = "tag has-desc icon-only" + (prepared ? " is-prepared" : "")
      + (equipped ? " is-equipped" : "");
    s.appendChild(icon);
    s.setAttribute("data-icon", state.iconUrls[iconKey]);
    s.setAttribute("data-name", equipped ? `${name} · ${slotLabel}` : name);
    if (tip) s.setAttribute("data-desc", tip);
    s.setAttribute("tabindex", "0");
    s.setAttribute("aria-describedby", "item-tooltip");
    s.setAttribute("aria-label", prepared ? `${name} (prepared)`
      : (equipped ? `${name} (${slotLabel.toLowerCase()})` : name));
    return s;
  }
  s.className = "tag" + (tip ? " has-desc" : "") + (prepared ? " is-prepared" : "")
    + (equipped ? " is-equipped" : "");
  s.appendChild(document.createTextNode(name));
  if (prepared || equipped) {
    // The accent marker is the visual cue; the tooltip names it.
    s.setAttribute("data-name", prepared ? "Prepared" : slotLabel);
    s.setAttribute("tabindex", "0");
    s.setAttribute("aria-describedby", "item-tooltip");
    s.setAttribute("aria-label", prepared ? `${name} (prepared)` : `${name} (${slotLabel.toLowerCase()})`);
  }
  if (tip) {
    s.setAttribute("data-desc", tip);
    s.setAttribute("tabindex", "0");
    s.setAttribute("aria-describedby", "item-tooltip");
  }
  return s;
}

function tagList(items) {
  const d = document.createElement("div");
  (items || []).forEach((t) => d.appendChild(descTag(t)));
  return d;
}

/* Consumable as an icon-only tile with a count badge (text chip until an icon
   exists, so nothing is lost). */
function consumableTile(name, count, iconKey) {
  const img = iconImg(iconKey, "tile-icon");
  if (!img) return descTag({ name: `${name} ×${count}` });
  const tile = document.createElement("div");
  tile.className = "consumable-tile";
  tile.appendChild(img);
  const badge = document.createElement("span");
  badge.className = "tile-count";
  badge.textContent = String(count);
  tile.appendChild(badge);
  tile.setAttribute("data-icon", state.iconUrls[iconKey]);
  tile.setAttribute("data-name", name);
  tile.setAttribute("tabindex", "0");
  tile.setAttribute("aria-describedby", "item-tooltip");
  tile.setAttribute("aria-label", `${name} (${count})`);
  return tile;
}

/* Icon-only tile with a value badge (stats, ability scores, HP/AC/…). */
function valueTile(name, value, iconKey, tip) {
  const img = iconImg(iconKey, "tile-icon");
  if (!img) return row(name, value);
  const tile = document.createElement("div");
  tile.className = "stat-tile";
  tile.appendChild(img);
  const badge = document.createElement("span");
  badge.className = "tile-count";
  badge.textContent = (value === null || value === undefined || value === "") ? "—" : String(value);
  tile.appendChild(badge);
  tile.setAttribute("data-icon", state.iconUrls[iconKey]);
  tile.setAttribute("data-name", tip || name);
  tile.setAttribute("tabindex", "0");
  tile.setAttribute("aria-describedby", "item-tooltip");
  tile.setAttribute("aria-label", `${name} ${value}`);
  return tile;
}

function tileRow(tiles) {
  const d = document.createElement("div");
  d.className = "stat-tiles";
  (tiles || []).forEach((t) => d.appendChild(t));
  return d;
}

// A labelled group of chips (small label above, chips with hover tooltips below).
/* Carrying capacity (SRD 5.1): load, encumbrance status and the penalties it
   currently applies. Native `title` is safe here — the custom tooltip only
   binds to [data-icon]/[data-name]/[data-desc] elements. */
function carryLine(carry) {
  if (!carry) return null;
  const label = carry.status === "heavily_encumbered" ? "Heavily encumbered"
    : carry.status === "encumbered" ? "Encumbered" : "Unencumbered";
  const wrap = document.createElement("div");
  wrap.className = "carry-line" + (carry.status === "unencumbered" ? "" : " is-" + carry.status);
  const load = document.createElement("span");
  load.className = "carry-load";
  load.textContent = `${carry.carried} / ${carry.capacity} lb`;
  const status = document.createElement("span");
  status.className = "carry-status";
  status.textContent = label;
  wrap.appendChild(load);
  wrap.appendChild(status);
  const bits = [
    `Encumbered at ${carry.thresholds.encumbered} lb, heavily encumbered at ${carry.thresholds.heavily_encumbered} lb`,
    `Push, drag or lift ${carry.push_drag_lift} lb`,
  ];
  if (carry.speed_penalty) bits.push(`Speed ${carry.speed_penalty} ft slower`);
  if (carry.status === "heavily_encumbered") {
    bits.push("Disadvantage on STR/DEX/CON checks, attack rolls and saving throws");
  }
  if (carry.capacity_multiplier && carry.capacity_multiplier !== 1) {
    bits.push(`Capacity ×${carry.capacity_multiplier} (effect)`);
  }
  if (carry.coin_weight) bits.push(`Includes ${carry.coin_weight} lb of coins`);
  if (carry.unweighed && carry.unweighed.length) {
    bits.push(`${carry.unweighed.length} item(s) with no known weight`);
  }
  wrap.title = bits.join(" · ");
  return wrap;
}

// Equipped items (SRD 5.1): what the two hands hold and what is worn. Sits
// beside the carrying line in the Inventory card. The tooltip is about HANDS
// only — the armour-class derivation belongs on the Armor Class tile.
function handsLine(equip) {
  if (!equip || !equip.derived_from_equipped) return null;
  const main = equip.main_hand || null;
  const off = equip.off_hand || null;
  const wrap = document.createElement("div");
  wrap.className = "carry-line";
  const load = document.createElement("span");
  load.className = "carry-load";
  load.textContent = (main || off) ? `Hands: ${main || "—"} · ${off || "—"}` : "Hands: both free";
  const status = document.createElement("span");
  status.className = "carry-status";
  status.textContent = equip.armor ? equip.armor : "No armour";
  wrap.appendChild(load);
  wrap.appendChild(status);
  const free = equip.hands_free;
  const bits = [
    `Main hand: ${main || "free"}`,
    `Off hand: ${off || "free"}`,
    free === 0 ? "Both hands busy — no free hand" : `${free} hand${free === 1 ? "" : "s"} free`,
  ];
  (equip.warnings || []).forEach((w) => bits.push(w.message || w.code));
  wrap.title = bits.join(" · ");
  return wrap;
}

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

// Spell slots as pips: lit (gold) = available, dim outline = spent.
function slotPips(levels) {
  const wrap = document.createElement("div");
  wrap.className = "slots";
  (levels || []).forEach((s) => {
    const row = document.createElement("div");
    row.className = "slot-row";
    row.title = `Level ${s.level} slots: ${s.remaining}/${s.max}`;
    const k = document.createElement("span");
    k.className = "slot-k";
    k.textContent = "L" + s.level;
    const pips = document.createElement("span");
    pips.className = "pips";
    for (let i = 0; i < s.max; i++) {
      const p = document.createElement("span");
      p.className = "pip" + (i < s.remaining ? "" : " spent");
      pips.appendChild(p);
    }
    row.appendChild(k);
    row.appendChild(pips);
    wrap.appendChild(row);
  });
  return wrap;
}

/* ── item description tooltip ────────────────────────────── */

const tooltipEl = $("item-tooltip");
let tooltipTarget = null;
const TIP_SEL = "[data-icon],[data-name],[data-desc],[data-combatant],[data-initiative]";

function showInitiativeTooltip(target) {
  tooltipTarget = target;
  tooltipEl.innerHTML = "";
  const n = document.createElement("div");
  n.className = "tooltip-name";
  n.textContent = "Initiative Order";
  tooltipEl.appendChild(n);
  const d = document.createElement("div");
  d.className = "tooltip-desc tooltip-sheet";
  const init = state.combatInitiative || {};
  d.textContent = (state.combatOrder || []).map((name, i) => {
    const info = init[name] || {};
    let suffix = "";
    if (info.total != null) {
      suffix = " — " + info.total;
      if (info.roll != null && info.modifier != null) suffix += ` (${info.roll} + ${info.modifier})`;
    }
    return `${i + 1}. ${name}${suffix}`;
  }).join("\n");
  tooltipEl.appendChild(d);
  tooltipEl.classList.remove("hidden");
  positionTooltip(target);
}

function showCombatantTooltip(target, name) {
  const c = (state.combatRoster || []).find((x) => x && x.name === name) || {};
  tooltipTarget = target;
  tooltipEl.innerHTML = "";
  const n = document.createElement("div");
  n.className = "tooltip-name";
  n.textContent = name;
  tooltipEl.appendChild(n);
  const d = document.createElement("div");
  d.className = "tooltip-desc tooltip-sheet";
  const head = [c.role ? String(c.role) : "", c.hp ? "HP " + c.hp : "",
                c.ac != null ? "AC " + c.ac : "",
                c.speed != null ? "Speed " + c.speed : "",
                c.cr != null ? "CR " + c.cr : ""].filter(Boolean).join("  ");
  const lines = head ? [head] : [];
  if (Array.isArray(c.conditions) && c.conditions.length) {
    lines.push("Conditions: " + c.conditions.join(", "));
  }
  // sheet lines: ["Name — role", "  HP … AC … Speed …", …detail lines]. The name and the
  // static vitals are already shown above, so only the detail lines are appended.
  if (Array.isArray(c.sheet)) {
    for (const line of c.sheet.slice(2)) {
      const text = String(line).trim();
      if (text) lines.push(text);
    }
  }
  d.textContent = lines.join("\n");
  tooltipEl.appendChild(d);
  tooltipEl.classList.remove("hidden");
  positionTooltip(target);
}

function showTooltip(target) {
  if (target.hasAttribute("data-initiative")) { showInitiativeTooltip(target); return; }
  const combatant = target.getAttribute("data-combatant");
  if (combatant) { showCombatantTooltip(target, combatant); return; }
  const url = target.getAttribute("data-icon");
  const name = target.getAttribute("data-name");
  const desc = target.getAttribute("data-desc");
  if (!url && !name && !desc) return;
  tooltipTarget = target;
  tooltipEl.innerHTML = "";
  if (url) {
    const img = document.createElement("img");
    img.className = "tooltip-icon";
    img.src = url;
    img.alt = "";
    tooltipEl.appendChild(img);
  }
  if (name) {
    const n = document.createElement("div");
    n.className = "tooltip-name";
    n.textContent = name;
    tooltipEl.appendChild(n);
  }
  if (desc) {
    const d = document.createElement("div");
    d.className = "tooltip-desc";
    d.textContent = desc;
    tooltipEl.appendChild(d);
  }
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
    const el = e.target.closest(TIP_SEL);
    if (el) showTooltip(el);
  });
  container.addEventListener("pointerout", (e) => {
    const el = e.target.closest(TIP_SEL);
    if (el && !el.contains(e.relatedTarget)) hideTooltip();
  });
  container.addEventListener("focusin", (e) => {
    const el = e.target.closest(TIP_SEL);
    if (el) showTooltip(el);
  });
  container.addEventListener("focusout", hideTooltip);
  container.addEventListener("click", (e) => {
    const el = e.target.closest(TIP_SEL);
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
  $("sheet-title").textContent = c.name || "Character";
  statsBox.appendChild(card("Character", [
    tagList([
      { name: c.race, description: c.race_desc, icon: c.race_icon },
      { name: c.character_class, description: c.character_class_desc, icon: c.class_icon },
      { name: c.background, description: c.background_desc, icon: c.background_icon },
      { name: c.alignment, description: "", icon: c.alignment_icon },
    ]),
    tileRow([
      valueTile("Level", c.level, c.level_icon, `Level ${c.level}`),
      valueTile("Gold", c.gold, c.gold_icon, `Gold ${c.gold}`),
      valueTile("Experience", c.xp, c.xp_icon, `Experience ${c.xp}`),
    ]),
  ]));

  const cb = d.combat || {};
  const prof = (cb.proficiency_bonus === null || cb.proficiency_bonus === undefined || cb.proficiency_bonus === "")
    ? "—" : "+" + cb.proficiency_bonus;
  const combatTiles = [
    valueTile("Hit Points", `${cb.hp_current}/${cb.hp_max}`, cb.hp_icon, `Hit Points ${cb.hp_current}/${cb.hp_max}`),
    valueTile("Armor Class", cb.armor_class, cb.ac_icon,
      cb.ac_breakdown || `Armor Class ${cb.armor_class}`),
    valueTile("Speed", cb.speed, cb.speed_icon,
      cb.speed_penalty
        ? `Speed ${cb.speed} ft — base ${cb.speed_base} ft, −${cb.speed_penalty} ft from encumbrance`
        : `Speed ${cb.speed} ft`),
    valueTile("Proficiency", prof, cb.proficiency_icon, `Proficiency Bonus ${prof}`),
    valueTile("Hit Dice", `${cb.hit_dice_count}d${cb.hit_dice_size}`, cb.hit_dice_icon,
      `Hit Dice ${cb.hit_dice_count}d${cb.hit_dice_size}`),
  ];
  if (cb.temporary_hit_points) {
    combatTiles.push(valueTile("Temporary Hit Points", cb.temporary_hit_points, cb.temp_hp_icon,
      `Temporary Hit Points ${cb.temporary_hit_points}`));
  }
  statsBox.appendChild(card("Combat", [hpBar(cb.hp_current, cb.hp_max), tileRow(combatTiles)]));

  // SRD 5.1 exhaustion / concentration / death saves are engine state the player must see.
  const combatNotes = [];
  if (cb.exhaustion) {
    const exh = [
      "",
      "Level 1: disadvantage on ability checks",
      "Level 2: speed halved",
      "Level 3: disadvantage on attack rolls and saving throws",
      "Level 4: HP maximum halved",
      "Level 5: speed 0",
      "Level 6: death",
    ];
    combatNotes.push(row("Exhaustion", `${cb.exhaustion}/6`, exh[cb.exhaustion] || ""));
  }
  if (cb.concentration) {
    combatNotes.push(row("Concentration", cb.concentration,
      "Taking damage forces a CON save (DC 10 or half the damage)."));
  }
  const ds = cb.death_saves || {};
  if (ds.successes || ds.failures) {
    combatNotes.push(row("Death Saves",
      `${ds.successes || 0}/3 successes \u00b7 ${ds.failures || 0}/3 failures`));
  }
  if (combatNotes.length) statsBox.appendChild(card("Condition", combatNotes));

  if (d.stats && d.stats.length) {
    const tiles = d.stats.map((a) => {
      const label = a.name || a.key;
      const mod = (a.modifier === null || a.modifier === undefined || a.modifier === "") ? "" : ` (${a.modifier})`;
      let note = "";
      if (a.base_value !== undefined && a.base_value !== null && a.base_value !== a.value) {
        note = ` — set by ${a.source || "a worn item"} (base ${a.base_value})`;
      }
      return valueTile(label, a.value, a.icon, `${label} ${a.value}${mod}${note}`);
    });
    statsBox.appendChild(card("Ability Scores", [tileRow(tiles)]));
  }

  const sp = d.spellcasting;
  if (sp) {
    const kids = [
      descTag({ name: sp.ability, description: "", icon: sp.ability_icon }),
      tileRow([
        valueTile("Spell Save DC", sp.dc, sp.dc_icon, `Spell Save DC ${sp.dc}`),
        valueTile("Spell Attack", sp.attack_modifier == null ? "—" : "+" + sp.attack_modifier, sp.attack_icon,
          `Spell Attack ${sp.attack_modifier == null ? "—" : "+" + sp.attack_modifier}`),
      ]),
    ];
    if (sp.cantrips && sp.cantrips.length) kids.push(field("Cantrips", sp.cantrips));
    if (sp.spells_known && sp.spells_known.length) kids.push(field("Known", sp.spells_known));
    if (sp.spellbook && sp.spellbook.length) {
      // One list: prepared spells are marked, never repeated in a second field.
      // Prepared casters have no spellbook at all, so the same list is labelled
      // "Prepared" for them. A missing spellbook item greys the whole list.
      const label = sp.has_spellbook ? "Spellbook" : "Prepared";
      const book = field(sp.spellbook_missing ? `${label} — unavailable` : label, sp.spellbook);
      if (sp.spellbook_missing) {
        book.classList.add("unavailable");
        const note = document.createElement("div");
        note.className = "field-note";
        note.textContent = "The spellbook is missing from the inventory — " +
          "these spells cannot be used until it is recovered.";
        book.appendChild(note);
      }
      kids.push(book);
    }
    if (sp.slot_levels && sp.slot_levels.length) {
      kids.push(slotPips(sp.slot_levels));
    }
    statsBox.appendChild(card("Spellcasting", kids));
  }

  const p = d.proficiencies || {};
  const profKids = [];
  if (p.saves && p.saves.length) profKids.push(field("Saves", p.saves));
  if (p.weapons && p.weapons.length) profKids.push(field("Weapons", p.weapons));
  if (p.armor && p.armor.length) profKids.push(field("Armor", p.armor));
  if (p.features && p.features.length) profKids.push(field("Features", p.features));
  if (p.languages && p.languages.length) profKids.push(field("Languages", p.languages));
  if (profKids.length) statsBox.appendChild(card("Proficiencies", profKids));

  // SRD 5.1 engine-derived skill modifiers (proficient/Expertise) + passive scores.
  const dskills = (d.skills || []).filter((s) => s.proficient || s.expertise);
  const passive = d.passive || {};
  if (dskills.length || Object.keys(passive).length) {
    const kids = [];
    dskills.forEach((s) => {
      const mod = (s.modifier >= 0 ? "+" : "") + s.modifier;
      const tip = `${s.name} (${s.ability}) ${mod}` +
        (s.expertise ? " \u2014 Expertise (double proficiency)" : "");
      kids.push(row(s.name + (s.expertise ? " (Expertise)" : ""), mod, tip));
    });
    const pv = Object.entries(passive).map(
      ([k, v]) => `${k.charAt(0).toUpperCase()}${k.slice(1)} ${v}`);
    if (pv.length) kids.push(row("Passive", pv.join(" \u00b7 "),
      "Passive score = 10 + the derived modifier (Perception, Investigation, Insight)."));
    statsBox.appendChild(card("Skills", kids));
  }

  if (d.inventory && d.inventory.length) {
    const kids = [carryLine(d.carrying), handsLine(d.equipment), tagList(d.inventory)].filter(Boolean);
    statsBox.appendChild(card("Inventory", kids));
  }
  if (d.consumables && Object.keys(d.consumables).length) {
    const icons = d.consumable_icons || {};
    const tiles = document.createElement("div");
    tiles.className = "consumable-tiles";
    Object.entries(d.consumables).forEach(([k, v]) => tiles.appendChild(consumableTile(k, v, icons[k])));
    statsBox.appendChild(card("Consumables", [tiles]));
  }
  if (d.active_effects && d.active_effects.length) {
    const kids = [];
    d.active_effects.forEach((e) => {
      kids.push(row(e.name, "", e.description || "", e.icon));
      (e.rows || []).forEach((r) => kids.push(row("  " + r.field, r.value)));
    });
    statsBox.appendChild(card("Active Effects", kids));
  }
  if (d.reputation && d.reputation.length) {
    const wrap = document.createElement("div");
    d.reputation.forEach((r) => {
      const entries = (r.entries || []).map((en) => {
        const nm = (en && en.name) ? en.name : String(en);
        return nm + ((en && en.description) ? " — " + en.description : "");
      }).join("\n");
      const factionUrl = r.faction_icon ? state.iconUrls[r.faction_icon] : null;
      const kingdomUrl = r.kingdom_icon ? state.iconUrls[r.kingdom_icon] : null;
      wrap.appendChild(descTag({
        name: `${r.category} · ${r.faction}`,
        description: entries,
        // Prefer a generated faction icon; fall back to the realm crest.
        icon: (factionUrl && r.faction_icon) || (kingdomUrl && r.kingdom_icon) || r.faction_icon || r.kingdom_icon,
      }));
    });
    statsBox.appendChild(card("Reputation", [wrap]));
  }
}

/* ── composer / status ────────────────────────────────────── */

function setStatus(t) { statusEl.textContent = t; }
function updateTurn() { const el = $("turn-label"); if (el) el.textContent = "turn " + state.turn; }

// The Device's counter: turns left before it fires. Quiet on purpose -- one word and a
// number, in the same voice as "turn 3". Absent until the engine reports a cadence, and
// absent again if the Device ever stops firing on its own.
function updateDevice(c) {
  const el = $("device-label");
  if (!el || !c) return;
  const left = c.turns_until;
  if (left === null || left === undefined) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = "device " + left;
  el.classList.toggle("device-warn", !!c.warning);
}
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
  // Drop events that raced in from a session we have already left.
  if (evt && evt.session_id && evt.session_id !== state.sessionId) return;
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
      updateDevice(evt.cadence);
      state.activeSaveName = (evt.world || "").replace(/\.player$/i, "");
      state.cur = null;
      updateSheetPortrait();
      addSystem(`Session ready · ${evt.tools ? evt.tools.length : 0} engine tools · ${evt.model}`);
      break;

    case "assistant_start":
      if (evt.label === "timeline") {
        state.cur = null; // not shown as chat; timeline surfaces via its own event
      } else if (!state.cur) {
        // One GM bubble per turn: continuation rounds (tool calls, thinking-only
        // retries, resumes) reuse the current bubble instead of opening a new one.
        state.cur = createTurnBubble(evt.label);
      }
      break;

    case "thinking_delta":
      if (state.cur) {
        state.cur.thinking += evt.text;
        state.cur.thinkBody.textContent = state.cur.thinking;
        state.cur.hadThinking = true;
        if (state.thinkEnabled) state.cur.think.open = true;
      }
      break;

    case "narrative_delta":
      if (state.cur) {
        const tb = state.cur;
        beginSegment(tb);
        tb.segRaw += evt.text;
        // Never let the protocol marker reach the streaming preview.
        const visible = stripTokens(tb.segRaw);
        if (!tb.hasNarrative && visible.trim()) {
          tb.hasNarrative = true;
          tb.el.classList.add("has-narrative");
        }
        tb.seg.textContent = visible;
      }
      scrollToBottom();
      break;

    case "assistant_end":
      // One round of the turn is done; render its narrative segment but keep
      // the turn bubble open for later rounds (tools / the final answer).
      // Use the model's complete round text as the source of truth, not the deltas.
      if (state.cur && typeof evt.text === "string" && evt.text.trim()) {
        state.cur.segRaw = evt.text;
      }
      if (state.cur) endSegment(state.cur);
      scrollToBottom();
      break;

    case "tool_call":
      if (state.cur) endSegment(state.cur);
      state.lastTool = addToolCall(evt.name, evt.arguments, state.cur ? state.cur.flow : transcript);
      break;

    case "tool_result":
      fillToolResult(state.lastTool, evt.text, evt.is_error, evt.gm_text);
      state.lastTool = null;
      break;

    case "combat_roster":
      state.combatRoster = Array.isArray(evt.combatants) ? evt.combatants : [];
      if (Array.isArray(evt.order)) state.combatOrder = evt.order.map(String);
      if (evt.initiative && typeof evt.initiative === "object") state.combatInitiative = evt.initiative;
      break;

    case "mechanics":
      state.mechanicsLines = Array.isArray(evt.lines) ? evt.lines : [];
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
      appendMechanics(state.cur);
      finalizeTurnBubble();
      state.ready = true;
      setStatus("Awaiting your action");
      updateComposer();
      requestStats();
      break;

    case "turn_end":
      appendMechanics(state.cur);
      finalizeTurnBubble();
      if (evt.turn != null) { state.turn = evt.turn; updateTurn(); }
      setStatus("Awaiting your action");
      if (state.lastCommandType === "action") requestStats();
      break;

    case "stats":
      state.lastStats = evt.data;
      renderStats(evt.data);
      autoGenerateIcons(evt.data);
      break;

    case "scene_request":
      maybeGenerateScene(evt);
      break;

    case "cadence":
      updateDevice(evt);
      break;

    case "notice":
      addSystem(`${evt.title ? evt.title + ": " : ""}${evt.text || ""}`);
      break;

    case "timeline":
      addTimeline(evt.entry);
      break;

    case "saved":
      state.activeSaveName = evt.name || state.activeSaveName;
      addSystem(`Saved to ${evt.save || (evt.name + ".player")}`);
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
  ws.onopen = () => {
    state.connected = true;
    setConn("connected", "on");
    updateComposer();
    setStatus("Loading…");
  };
  ws.onclose = () => { state.connected = false; setConn("disconnected", "off"); updateComposer(); setStatus("Disconnected"); };
  ws.onerror = () => setConn("error", "err");
  ws.onmessage = (e) => { try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); } };
}

function showStartError(msg) {
  const el = $("start-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function startSession(save) {
  const model = $("model-select").value;
  // Gemini ignores custom sampling (its sampling is fixed to the model's optimal
  // defaults), so its temperature input is disabled and no value is sent.
  const tempInput = $("temp-input");
  const temperature = tempInput.disabled ? null : parseFloat(tempInput.value);
  if (state.sessionId) {
    try { await fetch(`/api/sessions/${state.sessionId}`, { method: "DELETE" }); } catch (e) { /* ignore */ }
  }
  if (state.ws) { try { state.ws.close(); } catch (e) { /* ignore */ } state.ws = null; }
  state.connected = false; state.ready = false; state.busy = false;
  state.lastStats = null;      // drop the previous character's sheet
  state.combatRoster = [];     // drop the previous fight's tooltips
  state.combatOrder = [];
  state.combatInitiative = {};
  state.mechanicsLines = [];
  state.iconGenEpoch += 1;     // cancel any in-flight generation run
  transcript.innerHTML = "";
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ save, model, temperature, think: true, scene_images: !!state.imagesEnabled }),
  });
  if (!res.ok) throw new Error("HTTP " + res.status + " — " + (await res.text()));
  const data = await res.json();
  state.sessionId = data.session_id;
  state.model = data.model;
  state.contextWindow = data.context_window || 0;
  state.activeSaveName = (data.world || save).replace(/\.player$/i, "");
  $("world-label").textContent = data.world || save;
  $("model-label").textContent = data.model || model;
  $("ctx-label").textContent = `context 0 / ${fmtNum(state.contextWindow)}`;
  updateCtxMeter(0, state.contextWindow);
  $("start-overlay").classList.add("hidden");
  updateComposer();
  connect();
}

async function begin() {
  $("begin").disabled = true;
  const save = $("world-select").value;
  const wanted = confirmPortrait(save);
  try {
    await startSession(save);
    if (wanted) generatePortrait(save);
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
  updateStartPortrait();
  return files;
}

async function loadModels() {
  const m = await fetch("/api/models").then((x) => x.json());
  const sel = $("model-select");
  sel.innerHTML = "";
  (m.models || []).forEach((x) => {
    const o = document.createElement("option");
    o.value = x.id; o.textContent = x.label || x.id;
    if (x.available === false) { o.disabled = true; o.textContent += " — needs GEMINI_API_KEY"; }
    if (x.id === m.default) o.selected = true;
    sel.appendChild(o);
  });
  $("temp-input").value = (m.default_temperature != null) ? m.default_temperature : 1.0;
  state.models = m.models || [];
  updateTemperatureControl();

  state.imageModels = m.image_models || [];
  state.iconModels = m.icon_models || state.imageModels;
  const imageDefault = m.default_image_model || "";
  const iconDefault = m.default_icon_model || "";
  document.querySelectorAll(".image-model-select").forEach((el) => {
    el.innerHTML = "";
    const desired = (state.imageStatus && state.imageStatus.thinking_level) || "";
    state.imageModels.forEach((x) => {
      const o = document.createElement("option");
      o.value = x.id; o.textContent = x.label || x.id;
      // The pinned thinking level is model-specific: NB2.1 takes medium, Lite does
      // not, and Pro takes none. Flag what each choice will actually use.
      const levels = Array.isArray(x.thinking_levels) ? x.thinking_levels : null;
      if (levels) {
        if (!levels.length) {
          o.textContent += " — no thinking level";
        } else if (desired && !levels.includes(desired)) {
          o.textContent += ` — thinking: ${x.thinking_level || levels[0]}`;
        }
        o.title = levels.length
          ? `Thinking levels: ${levels.join(", ")}`
          : "This model has no thinking-level setting";
      }
      el.appendChild(o);
    });
    el.dataset.default = imageDefault;
  });
  document.querySelectorAll(".icon-model-select").forEach((el) => {
    el.innerHTML = "";
    state.iconModels.forEach((x) => {
      const o = document.createElement("option");
      o.value = x.id; o.textContent = x.label || x.id;
      el.appendChild(o);
    });
    el.dataset.default = iconDefault;
  });
  const pick = (id, fallback) => (id && state.imageModels.some((x) => x.id === id)) ? id : fallback;
  const pickIcon = (id, fallback) => (id && state.iconModels.some((x) => x.id === id)) ? id : fallback;
  state.imageModel = pick(state.imageModel, imageDefault);
  state.iconModel = pickIcon(state.iconModel, iconDefault);
  applyModelSelects();
}

/* Gemini 3.x ignores custom sampling (temperature/top_p/top_k); the model uses
   its optimal defaults and a per-model `thinking_level`. Disable the temperature
   control for Gemini models so the UI does not imply it has an effect. */
function updateTemperatureControl() {
  const input = $("temp-input");
  const spec = (state.models || []).find((x) => x.id === $("model-select").value);
  const gemini = !!spec && spec.provider === "gemini";
  input.disabled = gemini;
  const label = input.closest("label");
  if (label) {
    label.title = gemini
      ? "Gemini uses its own optimal sampling; thinking effort is set per model"
      : "Sampling temperature (Ollama models)";
    label.classList.toggle("disabled", gemini);
  }
}

/* ── image generation (opt-in; cached portraits) ─────────── */

const IMAGES_KEY = "infinity.images.enabled";
const IMAGE_MODEL_KEY = "infinity.image.model";
const ICON_MODEL_KEY = "infinity.icon.model";
const IMAGE_STYLE_KEY = "infinity.image.style";
const LEGACY_SCENES_KEY = "infinity.scenes.enabled";

function worldByFile(file) {
  return (state.worldsMeta || []).find((w) => w.file === file) || null;
}

/* Cache-buster for portrait URLs: the portrait file's mtime changes on
   regenerate, whereas the save mtime does not. */
function portraitBust(w) {
  return encodeURIComponent((w && (w.portrait_modified || w.modified)) || 0);
}

function loadImagesPref() {
  // The old "story images" key was merged into this single option.
  let on = false;
  try {
    on = localStorage.getItem(IMAGES_KEY) === "on"
      || localStorage.getItem(LEGACY_SCENES_KEY) === "on";
    localStorage.setItem(IMAGES_KEY, on ? "on" : "off");
    localStorage.removeItem(LEGACY_SCENES_KEY);
  } catch (e) { return on; }
  return on;
}

function loadModelPref(key) {
  try { return localStorage.getItem(key) || ""; } catch (e) { return ""; }
}

function setModelPref(key, value) {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch (e) { /* ignore */ }
}

function applyImagesToggles() {
  document.querySelectorAll(".images-toggle").forEach((el) => {
    el.checked = !!state.imagesEnabled;
    el.disabled = !state.imageStatus.available;
    const label = el.closest("label");
    if (label) {
      label.title = state.imageStatus.available
        ? "Paint the character portrait and illustrate the story's key moments"
        : "Image generation unavailable — set GEMINI_API_KEY on the server";
    }
  });
  document.querySelectorAll('.sheet-mode-input[value="generate"]').forEach((el) => {
    el.disabled = !state.imageStatus.available;
    const label = el.closest("label");
    if (label) {
      label.title = state.imageStatus.available
        ? "Generate a shared icon for anything the GM adds, and reuse it across characters"
        : "Icon generation unavailable — set GEMINI_API_KEY on the server";
    }
  });
  applyModelSelects();
}

function applyModelSelects() {
  document.querySelectorAll(".image-model-select").forEach((el) => {
    if (state.imageModel) el.value = state.imageModel;
    el.disabled = !state.imageStatus.available;
  });
  document.querySelectorAll(".icon-model-select").forEach((el) => {
    if (state.iconModel) el.value = state.iconModel;
    el.disabled = !state.imageStatus.available;
  });
  document.querySelectorAll(".image-style-input").forEach((el) => {
    el.value = state.imageStyle || "";
    el.disabled = !state.imageStatus.available;
  });
}

function setImagesEnabled(on) {
  state.imagesEnabled = !!on;
  try { localStorage.setItem(IMAGES_KEY, state.imagesEnabled ? "on" : "off"); } catch (e) { /* ignore */ }
  applyImagesToggles();
}

function setImageModel(value) {
  state.imageModel = value || "";
  setModelPref(IMAGE_MODEL_KEY, state.imageModel);
  applyModelSelects();
}

function setIconModel(value) {
  state.iconModel = value || "";
  setModelPref(ICON_MODEL_KEY, state.iconModel);
  applyModelSelects();
}

function setImageStyle(value) {
  const next = String(value || "").trim().slice(0, 300);
  const changed = next !== state.imageStyle;
  state.imageStyle = next;
  setModelPref(IMAGE_STYLE_KEY, state.imageStyle);
  applyModelSelects();
  // A changed style redraws the portrait now; visited places redraw from their seed on
  // the next visit, and new action images use it immediately.
  if (changed && state.connected && state.imageStatus.available) {
    regeneratePortrait();
  }
}

async function initImages() {
  state.imagesEnabled = loadImagesPref();
  state.imageModel = loadModelPref(IMAGE_MODEL_KEY);
  state.iconModel = loadModelPref(ICON_MODEL_KEY);
  state.imageStyle = loadModelPref(IMAGE_STYLE_KEY);
  try {
    state.imageStatus = await fetch("/api/images/status").then((r) => r.json());
  } catch (e) {
    state.imageStatus = { available: false };
  }
  applyImagesToggles();
}

function updateStartPortrait() {
  const panel = $("start-portrait");
  const img = $("start-portrait-img");
  const meta = $("start-portrait-meta");
  const w = worldByFile($("world-select").value);
  if (w && w.portrait) {
    img.src = w.portrait + "?t=" + portraitBust(w);
    img.alt = (w.character || "Character") + " portrait";
    meta.textContent = [w.character, w.class, w.level != null ? "L" + w.level : "",
                        w.difficulty === "easy" ? "Easy" : (w.difficulty === "hard" ? "Hard" : ""),
                        w.mode === "time_traveler" ? "Time Traveler" : (w.mode ? "Classic" : "")]
      .filter(Boolean).join(" · ");
    panel.classList.remove("hidden");
  } else {
    panel.classList.add("hidden");
    img.removeAttribute("src");
    meta.textContent = "";
  }
}

function updateSheetPortrait() {
  const panel = $("sheet-portrait");
  const img = $("sheet-portrait-img");
  const regen = $("portrait-regen");
  const stem = state.activeSaveName;
  const w = stem ? worldByFile(stem + ".player") : null;
  if (!state.connected || !w || !w.portrait) {
    panel.classList.add("hidden");
    img.removeAttribute("src");
    if (regen) regen.hidden = true;
    return;
  }
  img.onerror = () => panel.classList.add("hidden");
  img.src = w.portrait + "?t=" + portraitBust(w);
  panel.classList.remove("hidden");
  if (regen) {
    regen.hidden = !state.imageStatus.available;
    regen.disabled = false;
  }
}

function confirmPortrait(save) {
  if (!save || !state.imagesEnabled || !state.imageStatus.available) return false;
  const w = worldByFile(save);
  if (w && w.portrait) return false;
  const label = (w && w.character) ? w.character : "this character";
  return window.confirm(`Generate a portrait for ${label}?\nThis contacts Google Gemini and takes a few seconds.`);
}

async function generatePortrait(save) {
  const panel = $("start-portrait");
  if (panel) {
    panel.classList.add("busy");
    panel.classList.remove("hidden");
    $("start-portrait-meta").textContent = "painting portrait…";
  }
  if ($("start-overlay").classList.contains("hidden")) setStatus("painting portrait…");
  try {
    const res = await fetch("/api/portrait", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ save, model: state.imageModel || undefined, style: state.imageStyle || undefined }),
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    await loadWorlds();
    updateStartPortrait();
    updateSheetPortrait();
    addSystem("Portrait ready.");
  } catch (err) {
    addError("Portrait: " + String(err.message || err));
    updateStartPortrait();
  } finally {
    if (panel) panel.classList.remove("busy");
    if (state.ready) setStatus("Awaiting your action");
  }
}

/* The current live sheet (a dump_player_db snapshot) mapped to the portrait
   payload, so a regenerate reflects the character's present level/gear. */
function portraitPlayerPayload() {
  const s = state.lastStats || {};
  const c = s.character || {};
  const inventory = (s.inventory || [])
    .map((i) => (typeof i === "string" ? i : (i && i.name)))
    .filter(Boolean);
  // Only equipped gear goes to the image: the prompt must never imply a hood or
  // cloak the character is not actually wearing.
  const eq = s.equipment || {};
  const equipped = {
    armor: eq.armor || null,
    hands: (eq.hands || []).map((h) => (h && h.name) || null),
    worn: (eq.worn || []).map((w) => (w && w.name) || null),
  };
  return {
    race: c.race || "",
    character_class: c.character_class || "",
    background: c.background || "",
    alignment: c.alignment || "",
    gender: c.gender || "",
    level: c.level,
    inventory,
    equipped,
  };
}

async function regeneratePortrait() {
  if (!state.connected || !state.imageStatus.available) return;
  const stem = state.activeSaveName;
  if (!stem) return;
  const btn = $("portrait-regen");
  const save = stem.toLowerCase().endsWith(".player") ? stem : stem + ".player";
  if (btn) { btn.disabled = true; btn.classList.add("busy"); }
  setStatus("repainting portrait…");
  try {
    const res = await fetch("/api/portrait", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ save, force: true, player: portraitPlayerPayload(), model: state.imageModel || undefined, style: state.imageStyle || undefined }),
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    await loadWorlds();
    updateSheetPortrait();
    addSystem("Character portrait regenerated.");
  } catch (err) {
    addError("Portrait: " + String(err.message || err));
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove("busy"); }
    if (state.ready) setStatus("Awaiting your action");
  }
}

/* ── save / load ─────────────────────────────────────────── */

function openSave() {
  if (!state.connected) { addError("Not connected to a session."); return; }
  // Save is in place: the world's own name owns its images, so it is never
  // renamed here (a new stem would orphan output/images/{stem}/).
  $("save-target").textContent = state.activeSaveName ? `${state.activeSaveName}.player` : "—";
  $("save-error").classList.add("hidden");
  $("save-overlay").classList.remove("hidden");
}
function closeSave() { $("save-overlay").classList.add("hidden"); }
function showSaveError(msg) { const el = $("save-error"); el.textContent = msg; el.classList.remove("hidden"); }

function confirmSave() {
  if (!state.connected) { showSaveError("Not connected."); return; }
  send({ type: "save" });
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
    const diff = w.difficulty === "easy" ? "Easy" : (w.difficulty === "hard" ? "Hard" : "");
    const game = w.mode === "time_traveler" ? "Time Traveler" : (w.mode ? "Classic" : "");
    const who = w.character ? `${w.character} — ${w.class || "?"} L${w.level == null ? "?" : w.level}${diff ? " · " + diff : ""}${game ? " · " + game : ""}` : "unknown character";
    const when = w.modified ? new Date(w.modified * 1000).toLocaleString() : "";
    const thumb = w.portrait
      ? `<img class="load-thumb" src="${escapeHtml(w.portrait)}?t=${portraitBust(w)}" alt="" />`
      : `<span class="load-thumb empty"></span>`;
    btn.innerHTML =
      thumb +
      `<span class="load-text">` +
      `<span class="load-file">${escapeHtml(w.file)}</span>` +
      `<span class="load-meta">${escapeHtml(who)}${when ? " · " + escapeHtml(when) : ""}</span>` +
      `</span>`;
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
  const wanted = confirmPortrait(file);
  try {
    await startSession(file);
    setStatus(`Loaded ${file}`);
    if (wanted) generatePortrait(file);
  } catch (err) {
    addError("Load failed: " + String(err.message || err));
  }
}

async function deleteWorld(file) {
  if (!file) return;
  if (!window.confirm(`Delete "${file}"?\nThis removes the .player and .timeline. It cannot be undone.`)) return;
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
  state.lastStats = null;      // never generate icons for a closed character
  state.combatRoster = [];     // never tooltip a closed fight
  state.combatOrder = [];
  state.combatInitiative = {};
  state.mechanicsLines = [];
  state.iconGenEpoch += 1;     // cancel any in-flight generation run
  transcript.innerHTML = "";
  statsBox.innerHTML = '<div class="muted">Start a session to load your sheet.</div>';
  setConn("disconnected", "off");
  setStatus("Not connected");
  $("world-label").textContent = "no world";
  $("model-label").textContent = "no model";
  $("ctx-label").textContent = "context —";
  updateCtxMeter(0, 0);
  $("start-overlay").classList.remove("hidden");
  $("sheet-portrait").classList.add("hidden");
  $("sheet-portrait-img").removeAttribute("src");
  $("sheet-title").textContent = "Character";
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
    ok.textContent = `Forged ${terminal.name} — ${terminal.race} ${terminal.character_class} L${terminal.level} → ${terminal.player}`;
    $("create-transcript").appendChild(ok);
    const worlds = await loadWorlds();
    if (worlds.includes(terminal.player)) $("world-select").value = terminal.player;
    updateStartPortrait();
    $("start-error").classList.add("hidden");
    setTimeout(closeCreate, 1100);
    if (confirmPortrait(terminal.player)) setTimeout(() => generatePortrait(terminal.player), 1200);
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
  $("end-session").addEventListener("click", openEnd);
  $("end-cancel").addEventListener("click", closeEnd);
  $("end-discard").addEventListener("click", endWithoutSaving);
  $("end-save").addEventListener("click", saveAndEnd);
  bindTooltips(document.body);
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
    // Display-only: the model always thinks; this just shows/hides the panes.
    state.thinkEnabled = !!e.target.checked;
    transcript.classList.toggle("show-thinking", state.thinkEnabled);
    updateThinkingLabel();
  });
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("sheet-toggle").addEventListener("click", toggleSheet);
  $("sheet-close").addEventListener("click", closeSheet);
  $("portrait-regen").addEventListener("click", regeneratePortrait);
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
  $("world-select").addEventListener("change", updateStartPortrait);
  $("model-select").addEventListener("change", updateTemperatureControl);
  document.querySelectorAll(".images-toggle").forEach((el) => {
    el.addEventListener("change", (e) => setImagesEnabled(e.target.checked));
  });
  document.querySelectorAll(".image-model-select").forEach((el) => {
    el.addEventListener("change", (e) => setImageModel(e.target.value));
  });
  document.querySelectorAll(".icon-model-select").forEach((el) => {
    el.addEventListener("change", (e) => setIconModel(e.target.value));
  });
  document.querySelectorAll(".image-style-input").forEach((el) => {
    el.addEventListener("change", (e) => setImageStyle(e.target.value));
  });
  document.querySelectorAll(".sheet-mode-input").forEach((el) => {
    el.addEventListener("change", (e) => { if (e.target.checked) setSheetMode(e.target.value); });
  });
  initSheetMode();
  // Thinking is off by default; read the checkbox into state so session creation
  // and socket-open both honour it.
  state.thinkEnabled = !!$("toggle-thinking").checked;
  transcript.classList.toggle("show-thinking", state.thinkEnabled);
  updateThinkingLabel();
  $("save-cancel").addEventListener("click", closeSave);
  $("save-confirm").addEventListener("click", confirmSave);
  $("load-cancel").addEventListener("click", closeLoad);

  try {
    await initImages();
    await loadIconIndex();
    const worlds = await Promise.all([loadWorlds(), loadModels()]).then((r) => r[0]);
    if (!worlds.length) {
      $("begin").disabled = true;
      showStartError("No saves found in output/. Forge a new character.");
    }
  } catch (err) {
    showStartError("Could not reach the server: " + err);
  }
}

initTheme();
init();
