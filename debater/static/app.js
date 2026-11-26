const thread = document.getElementById("thread");
const chatScroll = document.getElementById("chatScroll");
const input = document.getElementById("input");
const form = document.getElementById("composer");
const fileInput = document.getElementById("fileInput");
const previews = document.getElementById("previews");
const setup = document.getElementById("setup");
const searchWarn = document.getElementById("searchWarn");
const phaseChip = document.getElementById("phaseChip");
const watchBtn = document.getElementById("watchBtn");
const startBtn = document.getElementById("startBtn");
const chamberLog = document.getElementById("chamberLog");
const cardPanel = document.getElementById("cardPanel");
const scoreDock = document.getElementById("scoreDock");
const historyEl = document.getElementById("history");
const sidebar = document.getElementById("sidebar");
const menuBtn = document.getElementById("menuBtn");
const newChatBtn = document.getElementById("newChatBtn");
const agentPanel = document.getElementById("agentPanel");
const activity = document.getElementById("activity");
const liveStatus = document.getElementById("liveStatus");
const closePanelBtn = document.getElementById("closePanelBtn");
const toasts = document.getElementById("toasts");

const GREETING =
  "Tell me the decision you are weighing — the situation, the alternatives, or just the problem. If you don't have criteria, I'll check reviews and comparisons and propose them.";

let sessionId = null;
let pendingImages = [];
let busy = false;
let chamberRunning = false;
const inflight = new Set();
const notifiedDone = new Set();
const watchIds = new Set();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function addBubble(role, text, thumbs) {
  const wrap = el("article", `bubble ${role}`);
  wrap.appendChild(el("p", "who", role === "user" ? "You" : "Guide"));
  wrap.appendChild(el("p", "", text));
  if (thumbs && thumbs.length) {
    const row = el("div", "thumbs");
    thumbs.forEach((src) => {
      const img = document.createElement("img");
      img.src = src;
      img.alt = "Attached";
      row.appendChild(img);
    });
    wrap.appendChild(row);
  }
  thread.appendChild(wrap);
  scrollChat();
}

function addThink(text) {
  if (!text) return;
  let box = thread.querySelector(".bubble.think.live");
  if (!box) {
    box = el("article", "bubble think live");
    box.appendChild(el("p", "who", "Thinking"));
    box.appendChild(el("ul", "think-list"));
    thread.appendChild(box);
  }
  const list = box.querySelector(".think-list");
  const last = list.lastElementChild;
  if (last && last.textContent === text) return;
  list.appendChild(el("li", "", text));
  scrollChat();
}

function finishThink() {
  const box = thread.querySelector(".bubble.think.live");
  if (box) box.classList.remove("live");
}

function showToast(title, body, onClick) {
  if (!toasts) return;
  const btn = el("button", "toast");
  btn.type = "button";
  btn.appendChild(el("strong", "", title));
  if (body) btn.appendChild(el("span", "", body));
  btn.addEventListener("click", () => {
    btn.remove();
    if (onClick) onClick();
  });
  toasts.appendChild(btn);
  setTimeout(() => btn.remove(), 12000);
}

function notifyDebateDone(item) {
  if (!item || !item.id || notifiedDone.has(item.id)) return;
  notifiedDone.add(item.id);
  watchIds.delete(item.id);
  const title = item.title || "Decision analysis finished";
  const body = "Scores are ready. Click to open.";
  showToast(title, body, () => openSession(item.id).catch(() => {}));
  if (typeof Notification !== "undefined" && Notification.permission === "granted") {
    try {
      const note = new Notification("Debater — analysis finished", { body: title });
      note.onclick = () => {
        window.focus();
        openSession(item.id).catch(() => {});
      };
    } catch (_err) {
      /* ignore blocked notifications */
    }
  }
}

function askNotifyPermission() {
  if (typeof Notification === "undefined") return;
  if (Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}

function scrollChat() {
  if (!chatScroll) return;
  const pin = () => {
    chatScroll.scrollTop = chatScroll.scrollHeight;
  };
  pin();
  requestAnimationFrame(() => {
    pin();
    requestAnimationFrame(pin);
  });
}

function setPhase(phase) {
  const labels = {
    interview: "Interview",
    chamber: "Deliberation",
    debate: "Deliberation",
    deliberation: "Deliberation",
    scored: "Decision",
    decided: "Decision",
  };
  const key = String(phase || "").toLowerCase();
  phaseChip.textContent =
    labels[key] || (key ? key.charAt(0).toUpperCase() + key.slice(1) : "Interview");
}

/* ---- tolerant readers -------------------------------------------------- */
/* The backend payload names move; every reader below accepts the legacy key
   and the plausible newer ones, and falls back to an em dash. */

const EMPTY = "—";
const NA = "N/A";

const TEXT_KEYS = [
  "name",
  "title",
  "label",
  "statement",
  "text",
  "criterion",
  "requirement",
  "option",
  "finding",
  "description",
  "summary",
  "value",
];

function firstValue(source, keys) {
  if (!source || typeof source !== "object") return undefined;
  for (const key of keys) {
    const value = source[key];
    if (value === undefined || value === null) continue;
    if (typeof value === "string" && !value.trim()) continue;
    if (Array.isArray(value) && !value.length) continue;
    if (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length) continue;
    return value;
  }
  return undefined;
}

function textOf(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(textOf).filter(Boolean).join(", ");
  const inner = firstValue(value, TEXT_KEYS);
  return inner === undefined ? "" : textOf(inner);
}

function listOf(value) {
  if (value === undefined || value === null) return [];
  if (Array.isArray(value)) return value.filter((item) => item !== null && item !== undefined && item !== "");
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (typeof value === "object") return Object.entries(value).map(([name, inner]) => ({ name, value: inner }));
  return [value];
}

function fieldText(source, keys) {
  return textOf(firstValue(source, keys));
}

function fieldList(source, keys) {
  return listOf(firstValue(source, keys));
}

function numberOf(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const match = value.replace(/,/g, "").match(/-?\d+(\.\d+)?/);
    if (match) return Number(match[0]);
  }
  if (value && typeof value === "object") {
    const inner = firstValue(value, ["score", "value", "percent", "weight", "number"]);
    if (inner !== undefined && typeof inner !== "object") return numberOf(inner);
  }
  return null;
}

function formatNumber(value) {
  if (value === null || value === undefined) return NA;
  const rounded = Math.round(value * 1000) / 1000;
  return String(rounded);
}

function briefOf(source) {
  return (
    firstValue(source, ["use_case", "decision_brief", "brief", "decision_card", "card"]) || null
  );
}

function reportOf(source) {
  return (
    firstValue(source, ["scores", "report", "score_report", "final_report", "decision_report"]) ||
    null
  );
}

function optionsOf(source) {
  return fieldList(source, ["options", "option_scores", "alternatives", "candidates", "choices"]);
}

function criteriaOf(source) {
  return fieldList(source, [
    "criteria",
    "weights",
    "priorities",
    "criteria_weights",
    "weighting",
    "dimensions",
  ]);
}

function criterionName(entry) {
  return (
    fieldText(entry, ["criterion", "criterion_name", "name", "dimension", "label", "title"]) ||
    textOf(entry)
  );
}

function rawWeight(entry) {
  if (entry && typeof entry === "object") {
    const found = firstValue(entry, ["weight", "percent", "importance", "priority", "share", "value"]);
    if (found !== undefined) return found;
    return null;
  }
  return entry === undefined ? null : entry;
}

function weightLabel(value) {
  if (value === null || value === undefined || value === "") return EMPTY;
  if (typeof value === "string" && value.includes("%")) return value.trim();
  const num = numberOf(value);
  if (num === null) return textOf(value) || EMPTY;
  return num > 1 ? `${formatNumber(num)}%` : formatNumber(num);
}

/* percent weights (40) and fractions (0.4) both reduce to a fraction */
function weightFraction(value) {
  const num = numberOf(value);
  if (num === null) return null;
  return num > 1 ? num / 100 : num;
}

function confidenceText(value) {
  if (value === null || value === undefined || value === "") return "";
  const num = typeof value === "number" ? value : null;
  if (num !== null) return num <= 1 ? `${Math.round(num * 100)}%` : formatNumber(num);
  return textOf(value);
}

function statusText(value) {
  if (typeof value === "boolean") return value ? "meets" : "fails";
  return textOf(value);
}

function chipsRow(pairs) {
  const row = el("div", "chips");
  pairs
    .filter(([, value]) => value)
    .forEach(([label, value]) => {
      const chip = el("span", "chip");
      if (label) chip.appendChild(el("em", "", label));
      chip.appendChild(document.createTextNode(String(value)));
      row.appendChild(chip);
    });
  return row.childElementCount ? row : null;
}

/* ---- decision brief ---------------------------------------------------- */

let currentBrief = null;

function briefRow(label, value) {
  const row = el("div", "brief-row");
  row.appendChild(el("span", "brief-label", label));
  const body = el("div", "brief-value");
  const items = Array.isArray(value) ? value.filter(Boolean) : value ? [value] : [];
  if (!items.length) {
    body.appendChild(el("span", "muted", EMPTY));
  } else {
    items.forEach((item) => body.appendChild(el("span", "brief-item", item)));
  }
  row.appendChild(body);
  return row;
}

function requirementLabel(entry) {
  const name =
    fieldText(entry, ["requirement", "name", "text", "statement", "label"]) || textOf(entry);
  const level = fieldText(entry, ["level", "severity", "type", "kind", "priority"]);
  return level ? `${name} (${level})` : name;
}

function renderBrief(card) {
  currentBrief = card && typeof card === "object" ? card : null;
  cardPanel.innerHTML = "";
  if (!currentBrief) {
    cardPanel.appendChild(
      el("p", "muted", "The decision brief fills in as Guide learns what you are deciding.")
    );
    return;
  }
  const decision = fieldText(currentBrief, [
    "decision",
    "subject",
    "question",
    "decision_statement",
    "topic",
    "title",
  ]);
  const context = [
    fieldText(currentBrief, ["user", "who"]),
    fieldText(currentBrief, ["context", "situation", "job", "background"]),
  ]
    .filter(Boolean)
    .filter((value, index, all) => all.indexOf(value) === index);
  const statusQuo = fieldText(currentBrief, ["status_quo", "current", "baseline", "current_choice"]);
  const options = optionsOf(currentBrief).map((item) => {
    const name = fieldText(item, ["name", "option", "title", "label"]) || textOf(item);
    const note = fieldText(item, ["description", "summary", "note"]);
    return note && note !== name ? `${name} — ${note}` : name;
  });
  const requirements = fieldList(currentBrief, [
    "requirements",
    "must_haves",
    "musts",
    "hard_requirements",
    "needs",
  ]).map(requirementLabel);
  const preferences = fieldList(currentBrief, [
    "preferences",
    "nice_to_haves",
    "wants",
    "soft_requirements",
  ]).map(textOf);
  const constraints = fieldList(currentBrief, [
    "constraints",
    "limits",
    "limitations",
    "boundaries",
    "deal_breakers",
  ]).map(textOf);
  const criteria = criteriaOf(currentBrief).map((entry) => {
    const weight = weightLabel(rawWeight(entry));
    const name = criterionName(entry);
    const source = fieldText(entry, ["source"]);
    const label = weight === EMPTY ? name : `${name} · ${weight}`;
    return source ? `${label} — ${source}` : label;
  });
  const criteriaOrigin = fieldText(currentBrief, ["criteria_origin"]);
  const weightsOrigin = fieldText(currentBrief, ["weights_origin"]);
  const missing = fieldList(currentBrief, [
    "missing_fields",
    "missing",
    "unknowns",
    "gaps",
    "open_questions",
  ]).map(textOf);

  const rows = [
    ["Decision", decision],
    ["Context / user", context],
  ];
  if (statusQuo) rows.push(["Current situation", statusQuo]);
  rows.push(
    ["Options", options],
    ["Requirements", requirements],
    ["Preferences", preferences],
    ["Constraints", constraints],
    ["Criteria & weights", criteria],
    ["Missing", missing.length ? missing : "None"]
  );
  if (criteriaOrigin === "evidence") {
    rows.push(["Criteria source", "Taken from reviews and comparisons, not from you"]);
  }
  if (weightsOrigin === "equal_default") {
    rows.push(["Weight source", "Equal split, because you didn't rank them"]);
  }
  rows.forEach(([label, value]) => cardPanel.appendChild(briefRow(label, value)));
}

function setActivity(text) {
  const line = text || "Waiting for you to talk to Guide.";
  activity.textContent = line;
  liveStatus.textContent = line;
}

function setStartState(state) {
  if (state === "hidden") {
    startBtn.classList.add("hidden");
    startBtn.disabled = false;
    startBtn.textContent = "Start deliberation";
    return;
  }
  startBtn.classList.remove("hidden");
  if (state === "running") {
    startBtn.disabled = true;
    startBtn.textContent = "Deliberation is running — wait here";
    return;
  }
  startBtn.disabled = false;
  startBtn.textContent = state === "retry" ? "Try deliberation again" : "Start deliberation";
}

function scrollChamber() {
  chamberLog.scrollTop = chamberLog.scrollHeight;
}

function claimNode(claim) {
  const box = el("div", "log-entry");
  const id = fieldText(claim, ["id", "claim_id", "ref"]);
  const statement =
    fieldText(claim, ["statement", "text", "claim", "content", "assertion"]) || textOf(claim);
  box.appendChild(el("p", "meta", id ? `${id}: ${statement}` : statement));
  const chips = chipsRow([
    ["type", fieldText(claim, ["claim_type", "type", "kind", "category"])],
    ["status", fieldText(claim, ["status", "final_status", "verdict", "state", "resolution"])],
    ["confidence", confidenceText(firstValue(claim, ["confidence", "confidence_level", "certainty"]))],
    [
      "source quality",
      fieldText(claim, ["source_quality", "evidence_quality", "source_strength"]),
    ],
    ["", claim && claim.load_bearing ? "load-bearing" : ""],
  ]);
  if (chips) box.appendChild(chips);
  const assumptions = fieldList(claim, ["assumptions", "assumption", "assumed", "caveats"])
    .map(textOf)
    .filter(Boolean);
  if (assumptions.length) {
    box.appendChild(el("p", "meta sub", `Assumptions: ${assumptions.join("; ")}`));
  }
  const evidence = fieldText(claim, ["evidence", "support", "citation", "sources", "evidence_ids"]);
  const source = fieldText(claim, ["source", "citation_url", "url"]);
  if (evidence && evidence !== statement) {
    box.appendChild(el("p", "meta sub", `Evidence: ${evidence}${source ? ` (${source})` : ""}`));
  } else if (source) {
    box.appendChild(el("p", "meta sub", `Source: ${source}`));
  }
  const counter = fieldText(claim, ["counterargument", "counter", "objection"]);
  if (counter) box.appendChild(el("p", "meta sub", `Counterargument: ${counter}`));
  return box;
}

function challengeNode(challenge) {
  const box = el("div", "log-entry");
  const kind = fieldText(challenge, ["kind", "type", "challenge_type"]) || "challenge";
  const target = fieldText(challenge, ["target_id", "target", "claim_id", "target_claim"]);
  const attack =
    fieldText(challenge, ["attack", "text", "statement", "challenge", "summary"]) || textOf(challenge);
  box.appendChild(el("p", "meta", target ? `${kind} → ${target}: ${attack}` : `${kind}: ${attack}`));
  const chips = chipsRow([
    ["status", fieldText(challenge, ["final_status", "status", "resolution", "outcome", "verdict"])],
    ["confidence", confidenceText(firstValue(challenge, ["confidence", "confidence_level"]))],
  ]);
  if (chips) box.appendChild(chips);
  const evidence = fieldText(challenge, ["evidence", "citation"]);
  const source = fieldText(challenge, ["source", "url"]);
  if (evidence) box.appendChild(el("p", "meta sub", `Evidence: ${evidence}${source ? ` (${source})` : ""}`));
  const response = fieldText(challenge, ["response", "rebuttal"]);
  if (response) box.appendChild(el("p", "meta sub", `Response: ${response}`));
  const counter = fieldText(challenge, ["counter_response", "counter"]);
  if (counter) box.appendChild(el("p", "meta sub", `Counter-response: ${counter}`));
  return box;
}

function resolutionNode(resolution) {
  const box = el("div", "log-entry");
  const id = fieldText(resolution, ["claim_id", "id", "target_id"]);
  const status = fieldText(resolution, ["final_status", "status", "verdict"]);
  box.appendChild(el("p", "meta", id ? `${id} resolved` : "Claim resolved"));
  const chips = chipsRow([
    ["status", status],
    ["confidence", confidenceText(firstValue(resolution, ["confidence", "confidence_level"]))],
  ]);
  if (chips) box.appendChild(chips);
  const rationale = fieldText(resolution, ["rationale", "why", "reason", "note"]);
  if (rationale) box.appendChild(el("p", "meta sub", rationale));
  const evidence = fieldText(resolution, ["evidence", "citation"]);
  const source = fieldText(resolution, ["source", "url"]);
  if (evidence) box.appendChild(el("p", "meta sub", `Evidence: ${evidence}${source ? ` (${source})` : ""}`));
  return box;
}

function addChamber(event) {
  const item = el("li", event.speaker || "");
  item.appendChild(el("p", "who", `${event.speaker} · ${event.kind}`));
  item.appendChild(el("p", "", event.text || ""));
  listOf(firstValue(event, ["claims", "claim_list"])).forEach((claim) => {
    item.appendChild(claimNode(claim));
  });
  listOf(firstValue(event, ["challenges", "challenge_list"])).forEach((challenge) => {
    item.appendChild(challengeNode(challenge));
  });
  listOf(firstValue(event, ["resolutions", "claim_resolutions"])).forEach((resolution) => {
    item.appendChild(resolutionNode(resolution));
  });
  chamberLog.appendChild(item);
  scrollChamber();
}

/* ---- final decision report -------------------------------------------- */

function optionLabel(option, index) {
  return (
    fieldText(option, ["name", "option", "title", "label", "alternative"]) ||
    textOf(option) ||
    `Option ${(index || 0) + 1}`
  );
}

function truthyFlag(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  const text = String(value || "").trim().toLowerCase();
  if (!text) return false;
  return !/^(no|none|false|unchanged|stable|same)/.test(text);
}

function statusClass(status) {
  const text = String(status || "").toLowerCase();
  if (/meet|pass|yes|satisf|ok\b|true|clear/.test(text)) return "ok";
  if (/fail|miss|violat|block|false|^no\b|reject/.test(text)) return "bad";
  if (/partial|unknown|unclear|maybe|risk|assum/.test(text)) return "warn";
  return "";
}

function reportSection(title) {
  const node = el("section", "report-section");
  node.appendChild(el("h3", "section-title", title));
  return node;
}

function bulletList(items) {
  const list = el("ul", "report-list");
  items.filter(Boolean).forEach((item) => list.appendChild(el("li", "", item)));
  return list.childElementCount ? list : null;
}

/* listOf turns {option: verdict} maps into {name, value} pairs; unwrap the verdict */
function unwrapCell(cell) {
  if (cell && typeof cell === "object" && "name" in cell && "value" in cell && Object.keys(cell).length === 2) {
    return cell.value;
  }
  return cell;
}

function cellOf(entry) {
  if (entry === null || entry === undefined) return { status: "", note: "", confidence: "" };
  if (typeof entry !== "object") {
    return { status: statusText(entry), note: "", confidence: "" };
  }
  return {
    status: statusText(
      firstValue(entry, ["status", "verdict", "result", "met", "meets", "satisfied", "assessment"])
    ),
    note: fieldText(entry, ["note", "detail", "rationale", "why", "comment", "reason", "evidence"]),
    confidence: confidenceText(firstValue(entry, ["confidence", "confidence_level"])),
  };
}

function requirementMatrix(report, options) {
  const order = [];
  const columns = [];
  const rows = new Map();
  /* the same requirement can arrive per option and again in a bare verdict map;
     keep whichever source carried the rationale and confidence */
  const put = (requirement, option, cell) => {
    const name = requirement || "Requirement";
    if (!rows.has(name)) {
      rows.set(name, new Map());
      order.push(name);
    }
    if (option && !columns.includes(option)) columns.push(option);
    const key = option || "";
    const prior = rows.get(name).get(key);
    rows.get(name).set(
      key,
      prior
        ? {
            status: prior.status || cell.status,
            note: prior.note || cell.note,
            confidence: prior.confidence || cell.confidence,
          }
        : cell
    );
  };
  options.forEach((option, index) => {
    const name = optionLabel(option, index);
    fieldList(option, [
      "requirement_satisfaction",
      "requirement_results",
      "requirement_matrix",
      "requirement_analysis",
      "requirements",
      "matrix",
    ]).forEach((entry) => {
      put(
        fieldText(entry, ["requirement", "name", "criterion", "label", "text"]) || textOf(entry),
        name,
        cellOf(entry)
      );
    });
  });
  const raw = firstValue(report, [
    "requirement_matrix",
    "requirement_analysis",
    "requirement_results",
    "requirements_matrix",
    "requirements",
  ]);
  if (Array.isArray(raw)) {
    /* [{requirement, options: [{option, status}]}] or [{requirement, status}] */
    raw.forEach((entry) => {
      const name =
        fieldText(entry, ["requirement", "name", "criterion", "label", "text"]) || textOf(entry);
      const perOption = fieldList(entry, ["options", "results", "option_results", "by_option", "cells"]);
      if (perOption.length && typeof perOption[0] === "object") {
        perOption.forEach((cell) =>
          put(name, fieldText(cell, ["option", "name", "option_name", "alternative"]), cellOf(unwrapCell(cell)))
        );
      } else {
        put(name, "", cellOf(entry));
      }
    });
  } else if (raw && typeof raw === "object") {
    /* nested verdict maps come either way round: {option: {requirement: v}}
       or {requirement: {option: v}} */
    const optionNames = new Set(options.map((option, index) => optionLabel(option, index).toLowerCase()));
    const requirementNames = new Set(order.map((name) => name.toLowerCase()));
    fieldList(currentBrief, ["requirements", "must_haves", "musts", "needs"]).forEach((entry) =>
      requirementNames.add(textOf(entry).toLowerCase())
    );
    Object.entries(raw).forEach(([outer, value]) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        put(outer, "", cellOf(value));
        return;
      }
      const inner = Object.keys(value).map((key) => key.toLowerCase());
      const outerIsOption =
        optionNames.has(outer.toLowerCase()) ||
        (!requirementNames.has(outer.toLowerCase()) && inner.some((key) => requirementNames.has(key)));
      Object.entries(value).forEach(([key, verdict]) => {
        if (outerIsOption) put(key, outer, cellOf(verdict));
        else put(outer, key, cellOf(verdict));
      });
    });
  }
  return { order, columns, rows };
}

function matrixNode(matrix) {
  const scroll = el("div", "matrix-scroll");
  const grid = el("div", "matrix");
  const count = matrix.columns.length;
  grid.style.gridTemplateColumns = `minmax(150px, 1.4fr) repeat(${count}, minmax(130px, 1fr))`;
  grid.style.minWidth = `${150 + count * 130}px`;
  grid.appendChild(el("div", "matrix-head", "Requirement"));
  matrix.columns.forEach((column) => grid.appendChild(el("div", "matrix-head", column)));
  matrix.order.forEach((name) => {
    grid.appendChild(el("div", "matrix-cell matrix-row-label", name));
    matrix.columns.forEach((column) => {
      const cell = matrix.rows.get(name).get(column);
      const box = el("div", "matrix-cell");
      if (!cell) {
        box.appendChild(el("span", "muted", EMPTY));
      } else {
        box.appendChild(
          el("span", `status ${statusClass(cell.status || "unknown")}`, cell.status || "unknown")
        );
        if (cell.confidence) box.appendChild(el("span", "muted", `confidence ${cell.confidence}`));
        if (cell.note) box.appendChild(el("span", "muted", cell.note));
      }
      grid.appendChild(box);
    });
  });
  scroll.appendChild(grid);
  return scroll;
}

function criterionRows(option) {
  return fieldList(option, [
    "criterion_breakdown",
    "criteria",
    "criterion_scores",
    "criteria_scores",
    "breakdown",
    "dimensions",
    "scores",
  ]);
}

function scoreGrid(option, weightBook) {
  const rows = criterionRows(option);
  const scroll = el("div", "matrix-scroll");
  const grid = el("div", "score-grid");
  ["Criterion", "Weight", "Score", "Confidence", "Weight × score"].forEach((head) =>
    grid.appendChild(el("div", "matrix-head", head))
  );
  rows.forEach((row) => {
    const name = criterionName(row);
    /* newer criterion rows carry no weight; the brief holds it */
    const weightRaw =
      rawWeight(row) === null ? weightBook.get(name.toLowerCase()) ?? null : rawWeight(row);
    const scoreRaw = firstValue(row, ["score", "raw_score", "value", "rating", "points"]);
    const score = numberOf(scoreRaw);
    const fraction = weightFraction(weightRaw);
    const given = numberOf(
      firstValue(row, ["weighted_contribution", "contribution", "weighted_score", "weighted"])
    );
    const computed = fraction !== null && score !== null ? fraction * score : null;
    const reported = given !== null && computed !== null && Math.abs(given - computed) > 0.05;
    const arithmetic =
      computed !== null
        ? `${formatNumber(fraction)} × ${formatNumber(score)} = ${formatNumber(computed)}${
            reported ? ` (reported ${formatNumber(given)})` : ""
          }`
        : given !== null
        ? formatNumber(given)
        : NA;
    grid.appendChild(el("div", "matrix-cell matrix-row-label", name || EMPTY));
    grid.appendChild(el("div", "matrix-cell", weightLabel(weightRaw)));
    grid.appendChild(
      el("div", "matrix-cell", score !== null ? formatNumber(score) : textOf(scoreRaw) || NA)
    );
    grid.appendChild(
      el(
        "div",
        "matrix-cell",
        confidenceText(firstValue(row, ["confidence", "confidence_level", "certainty"])) || NA
      )
    );
    grid.appendChild(el("div", "matrix-cell", arithmetic));
    const rationale = fieldText(row, ["rationale", "why", "reason", "note", "justification"]);
    const evidence = fieldList(row, [
      "evidence_claim_ids",
      "evidence_ids",
      "claim_ids",
      "evidence",
      "claims",
      "supporting_claims",
    ])
      .map(textOf)
      .filter(Boolean);
    if (rationale || evidence.length) {
      const note = el("div", "matrix-note");
      if (rationale) note.appendChild(el("span", "", rationale));
      if (evidence.length) note.appendChild(el("span", "muted", `Evidence: ${evidence.join(", ")}`));
      grid.appendChild(note);
    }
  });
  scroll.appendChild(grid);
  return rows.length ? scroll : null;
}

function renderReport(report) {
  scoreDock.innerHTML = "";
  if (!report) {
    scoreDock.classList.add("hidden");
    return;
  }
  scoreDock.classList.remove("hidden");
  const brief = currentBrief || {};
  const options = optionsOf(report);

  const decisionSection = reportSection("Decision");
  const decision =
    fieldText(report, ["decision", "question", "subject", "decision_statement", "topic", "title"]) ||
    fieldText(brief, ["decision", "subject", "question", "decision_statement", "topic", "title"]);
  const headline = fieldText(report, ["message", "summary", "headline", "overview"]);
  const conclusion = fieldText(report, ["conclusion", "final_conclusion", "verdict", "closing"]);
  decisionSection.appendChild(el("p", "", decision || "Decision not captured in the brief."));
  if (headline && headline !== decision && headline !== conclusion) {
    decisionSection.appendChild(el("p", "report-note", headline));
  }
  scoreDock.appendChild(decisionSection);

  const priorities = criteriaOf(report).length ? criteriaOf(report) : criteriaOf(brief);
  const weightBook = new Map();
  priorities.forEach((entry) => {
    const name = criterionName(entry);
    const weight = rawWeight(entry);
    if (name && weight !== null && weight !== undefined) weightBook.set(name.toLowerCase(), weight);
  });
  if (priorities.length) {
    const criteriaOrigin = fieldText(brief, ["criteria_origin"]);
    const weightsOrigin = fieldText(brief, ["weights_origin"]);
    const section = reportSection(
      criteriaOrigin === "evidence" ? "Criteria from reviews" : "Your priorities"
    );
    if (criteriaOrigin === "evidence") {
      section.appendChild(
        el("p", "report-note", "These criteria came from reviews and comparisons, not from you.")
      );
    }
    if (weightsOrigin === "equal_default") {
      section.appendChild(
        el("p", "report-note", "Weights are an equal split because you didn't rank these.")
      );
    }
    const chips = chipsRow(
      priorities.map((entry) => [criterionName(entry), weightLabel(rawWeight(entry))])
    );
    if (chips) section.appendChild(chips);
    const notes = priorities
      .map((entry) => {
        const note = fieldText(entry, ["note", "why", "rationale", "detail"]);
        return note ? `${criterionName(entry)}: ${note}` : "";
      })
      .filter(Boolean);
    const list = bulletList(notes);
    if (list) section.appendChild(list);
    scoreDock.appendChild(section);
  }

  const optionEntries = options.length ? options : optionsOf(brief);
  if (optionEntries.length) {
    const section = reportSection("Options");
    const list = bulletList(
      optionEntries.map((option, index) => {
        const name = optionLabel(option, index);
        const note = fieldText(option, ["description", "what_it_is", "detail"]);
        return note && note !== name ? `${name} — ${note}` : name;
      })
    );
    if (list) section.appendChild(list);
    scoreDock.appendChild(section);
  }

  const matrix = requirementMatrix(report, options);
  if (matrix.order.length) {
    const section = reportSection("Requirement analysis");
    if (matrix.columns.length) {
      section.appendChild(matrixNode(matrix));
    } else {
      const list = bulletList(
        matrix.order.map((name) => {
          const cell = matrix.rows.get(name).get("") || {};
          const tail = [cell.status, cell.note].filter(Boolean).join(" — ");
          return tail ? `${name}: ${tail}` : name;
        })
      );
      if (list) section.appendChild(list);
    }
    scoreDock.appendChild(section);
  }

  const summaries = options
    .map((option, index) => {
      const name = optionLabel(option, index);
      const why = fieldText(option, ["why", "summary", "rationale", "assessment", "verdict_note"]);
      const evidence = fieldList(option, ["evidence", "key_evidence", "evidence_summary", "claims"])
        .map(textOf)
        .filter(Boolean);
      if (!why && !evidence.length) return null;
      return { name, why, evidence };
    })
    .filter(Boolean);
  if (summaries.length) {
    const section = reportSection("Evidence and option summaries");
    summaries.forEach((entry) => {
      const card = el("article", "report-card");
      card.appendChild(el("strong", "", entry.name));
      if (entry.why) card.appendChild(el("p", "", entry.why));
      if (entry.evidence.length) {
        const list = bulletList(entry.evidence);
        if (list) card.appendChild(list);
      }
      section.appendChild(card);
    });
    scoreDock.appendChild(section);
  }

  const scoring = reportSection("Comparative scoring");
  if (!options.length) {
    scoring.appendChild(el("p", "muted", "No option scores were returned."));
  }
  options.forEach((option, index) => {
    const card = el("article", "report-card");
    const head = el("div", "score-head");
    head.appendChild(el("strong", "", optionLabel(option, index)));
    const overall = numberOf(
      firstValue(option, ["overall", "overall_score", "total", "weighted_total", "final_score", "score"])
    );
    head.appendChild(el("span", "score-total", `Evaluation score ${overall === null ? NA : formatNumber(overall)}`));
    card.appendChild(head);
    const grid = scoreGrid(option, weightBook);
    if (grid) card.appendChild(grid);
    else card.appendChild(el("p", "muted", "No criterion breakdown was returned for this option."));
    scoring.appendChild(card);
  });
  scoreDock.appendChild(scoring);

  const confidences = options
    .map((option, index) => {
      const value = confidenceText(
        firstValue(option, ["confidence", "confidence_level", "certainty"])
      );
      const speculative = option && (option.speculation || option.speculative) ? " · speculation flagged" : "";
      if (!value && !speculative) return null;
      return [optionLabel(option, index), `${value || NA}${speculative}`];
    })
    .filter(Boolean);
  if (confidences.length) {
    const section = reportSection("Option confidence");
    const chips = chipsRow(confidences);
    if (chips) section.appendChild(chips);
    scoreDock.appendChild(section);
  }

  const tradeoffs = fieldList(report, [
    "key_tradeoffs",
    "key_trade_offs",
    "tradeoffs",
    "trade_offs",
    "tensions",
  ]).map((entry) => {
    const text =
      fieldText(entry, ["tradeoff", "trade_off", "text", "statement", "description", "summary"]) ||
      textOf(entry);
    const between = fieldText(entry, ["between", "axis", "options"]);
    return between && !text.includes(between) ? `${between}: ${text}` : text;
  });
  if (tradeoffs.length) {
    const section = reportSection("Key trade-offs");
    const list = bulletList(tradeoffs);
    if (list) section.appendChild(list);
    scoreDock.appendChild(section);
  }

  const scenarios = fieldList(report, [
    "sensitivity",
    "sensitivity_scenarios",
    "sensitivity_analysis",
    "scenarios",
    "what_if",
  ]);
  if (scenarios.length) {
    const section = reportSection("Sensitivity");
    scenarios.forEach((scenario, index) => {
      const card = el("article", "report-card");
      card.appendChild(
        el(
          "strong",
          "",
          fieldText(scenario, ["name", "scenario", "label", "title", "description"]) ||
            `Scenario ${index + 1}`
        )
      );
      const weights = criteriaOf(scenario);
      const chips = chipsRow(
        weights.map((entry) => [criterionName(entry), weightLabel(rawWeight(entry))])
      );
      if (chips) card.appendChild(chips);
      const leader = fieldText(scenario, [
        "leading_option",
        "leader",
        "winner",
        "top_option",
        "preferred_option",
        "result",
        "outcome",
      ]);
      const changedRaw = firstValue(scenario, [
        "changes_leader",
        "changes_winner",
        "leader_changed",
        "flips",
        "changed",
        "rank_change",
      ]);
      const stableRaw = firstValue(scenario, ["stable", "unchanged", "same_as_baseline", "holds"]);
      let marker = "";
      if (changedRaw !== undefined) marker = truthyFlag(changedRaw) ? "changes the leader" : "no change";
      else if (stableRaw !== undefined) marker = truthyFlag(stableRaw) ? "no change" : "changes the leader";
      const line = el("p", "report-note");
      line.appendChild(document.createTextNode(`Leading option: ${leader || NA}`));
      if (marker) {
        line.appendChild(document.createTextNode(" "));
        line.appendChild(
          el("span", `status ${marker === "no change" ? "ok" : "warn"}`, marker)
        );
      }
      card.appendChild(line);
      const totals = fieldList(scenario, ["totals", "option_totals", "scores"]).map((entry) => {
        const value = unwrapCell(entry);
        const total = numberOf(typeof value === "object" ? firstValue(value, ["total", "score"]) : value);
        return [
          fieldText(entry, ["option", "name", "option_name"]) || textOf(entry),
          total === null ? NA : formatNumber(total),
        ];
      });
      const totalChips = chipsRow(totals);
      if (totalChips) card.appendChild(totalChips);
      const note = fieldText(scenario, ["note", "detail", "rationale", "why", "comment"]);
      if (note) card.appendChild(el("p", "muted", note));
      section.appendChild(card);
    });
    scoreDock.appendChild(section);
  }

  const redTeam = fieldList(report, [
    "red_team_findings",
    "red_team",
    "adversarial_findings",
    "red_team_review",
  ]);
  if (redTeam.length) {
    const section = reportSection("Red-team findings");
    redTeam.forEach((entry) => {
      const card = el("article", "report-card");
      const scope = [
        fieldText(entry, ["option", "option_name"]),
        fieldText(entry, ["criterion", "dimension"]),
      ]
        .filter(Boolean)
        .join(" · ");
      if (scope) card.appendChild(el("strong", "", scope));
      const text =
        fieldText(entry, [
          "overturning_evidence",
          "finding",
          "text",
          "statement",
          "issue",
          "risk",
          "attack",
          "summary",
        ]) || textOf(entry);
      if (text) card.appendChild(el("p", "", text));
      const chips = chipsRow([
        ["severity", fieldText(entry, ["severity", "impact", "level"])],
        ["status", fieldText(entry, ["status", "final_status"])],
        ["confidence", confidenceText(firstValue(entry, ["confidence", "confidence_level"]))],
        ["source quality", fieldText(entry, ["source_quality", "evidence_quality"])],
        ["source", fieldText(entry, ["source", "url"])],
        ["claims", fieldList(entry, ["claim_ids", "evidence_claim_ids"]).map(textOf).join(", ")],
      ]);
      if (chips) card.appendChild(chips);
      const response = fieldText(entry, ["resolution", "response", "mitigation", "answer"]);
      if (response) card.appendChild(el("p", "report-note", response));
      section.appendChild(card);
    });
    scoreDock.appendChild(section);
  }

  const unresolved = fieldList(report, [
    "unresolved_assumptions",
    "unresolved",
    "assumptions",
    "open_questions",
    "unknowns",
  ])
    .map(textOf)
    .filter(Boolean);
  options.forEach((option, index) => {
    fieldList(option, ["unresolved_assumptions", "unresolved", "assumptions"]).forEach((entry) => {
      const text = textOf(entry);
      if (text) unresolved.push(`${optionLabel(option, index)}: ${text}`);
    });
  });
  if (unresolved.length) {
    const section = reportSection("Unresolved assumptions");
    const list = bulletList(unresolved);
    if (list) section.appendChild(list);
    scoreDock.appendChild(section);
  }

  const recommendation = fieldText(report, [
    "recommendation",
    "final_recommendation",
    "next_step",
    "next_steps",
    "advice",
  ]);
  const leading = fieldText(report, ["winner", "leading_option", "preferred_option"]);
  const closing = reportSection("Conclusion and recommendation");
  if (conclusion) closing.appendChild(el("p", "", conclusion));
  if (recommendation) closing.appendChild(el("p", "", recommendation));
  if (leading) closing.appendChild(el("p", "report-note", `Leading option: ${leading}`));
  if (!conclusion && !recommendation && !leading) {
    closing.appendChild(el("p", "muted", headline || "No conclusion was returned."));
  }
  const reportConfidence = confidenceText(firstValue(report, ["confidence", "confidence_level"]));
  const closingChips = chipsRow([["overall confidence", reportConfidence]]);
  if (closingChips) closing.appendChild(closingChips);
  scoreDock.appendChild(closing);
  scrollChat();
}

function openPanel() {
  agentPanel.classList.add("open");
}

function closePanel() {
  agentPanel.classList.remove("open");
}

function setBusy(next) {
  busy = next;
  form.querySelector("[type=submit]").disabled = next;
  input.disabled = next;
  if (chamberRunning) {
    setStartState("running");
  } else if (!startBtn.classList.contains("hidden")) {
    startBtn.disabled = next;
  }
}

function resetThread(showGreeting) {
  thread.innerHTML = "";
  chamberLog.innerHTML = "";
  renderReport(null);
  renderBrief(null);
  setActivity("Waiting for you to talk to Guide.");
  setPhase("interview");
  chamberRunning = false;
  setStartState("hidden");
  if (showGreeting) addBubble("guide", GREETING);
}

function renderHistory(sessions) {
  historyEl.innerHTML = "";
  (sessions || []).forEach((item) => {
    const title = item.title || "New decision";
    const label = item.running || item.phase === "chamber" ? `${title} · running` : title;
    const btn = el("button", item.id === sessionId ? "active" : "", label);
    btn.type = "button";
    btn.dataset.id = item.id;
    btn.addEventListener("click", () => openSession(item.id));
    historyEl.appendChild(btn);
  });
}

async function refreshHistory() {
  const data = await fetch("api/sessions").then((r) => r.json());
  renderHistory(data.sessions || []);
}

function applySnapshot(snapshot) {
  sessionId = snapshot.id;
  resetThread(false);
  const messages = snapshot.messages || [];
  if (!messages.length) {
    addBubble("guide", GREETING);
  } else {
    messages.forEach((item) => addBubble(item.role === "user" ? "user" : "guide", item.text));
  }
  renderBrief(briefOf(snapshot));
  const snapshotReport = reportOf(snapshot);
  const running = Boolean(snapshot.running) || (snapshot.phase === "chamber" && !snapshotReport);
  if (running) {
    watchIds.add(snapshot.id);
    setPhase("chamber");
    chamberRunning = true;
    setStartState("running");
    (snapshot.chamber_log || []).forEach((event) => {
      addChamber(event);
      if (event.kind === "progress") addThink(event.text);
    });
    setActivity("Deliberation is still running. You can stay or switch chats.");
    return;
  }
  setPhase(snapshot.phase);
  chamberRunning = false;
  if (snapshotReport) renderReport(snapshotReport);
  if (snapshot.ready_to_run && !snapshotReport) setStartState("ready");
  else setStartState("hidden");
  (snapshot.chamber_log || []).forEach(addChamber);
  if (snapshot.chamber_log && snapshot.chamber_log.length) {
    setActivity("Last deliberation activity is in the agents panel.");
  }
}

async function openSession(id) {
  setBusy(false);
  const snapshot = await fetch(`api/session/${id}`).then((r) => {
    if (!r.ok) throw new Error("Chat not found");
    return r.json();
  });
  applySnapshot(snapshot);
  sidebar.classList.remove("open");
  await refreshHistory();
  input.focus();
}

async function startNewChat() {
  const snapshot = await fetch("api/session", { method: "POST" }).then((r) => r.json());
  sessionId = snapshot.id;
  resetThread(true);
  sidebar.classList.remove("open");
  await refreshHistory();
  input.focus();
}

function fileToPayload(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result);
      const comma = result.indexOf(",");
      resolve({
        preview: result,
        mime_type: file.type || "image/jpeg",
        data: comma >= 0 ? result.slice(comma + 1) : result,
      });
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function loadImages(fileList) {
  const files = [...fileList].slice(0, 2);
  pendingImages = await Promise.all(files.map(fileToPayload));
  previews.innerHTML = "";
  pendingImages.forEach((img) => {
    const node = document.createElement("img");
    node.src = img.preview;
    node.alt = "Pending attachment";
    previews.appendChild(node);
  });
}

async function parseSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const line = part
        .split("\n")
        .filter((row) => row.startsWith("data:"))
        .map((row) => row.slice(5).trim())
        .join("");
      if (!line) continue;
      onEvent(JSON.parse(line));
    }
  }
}

function applyEvent(event) {
  const sid = event.session_id;
  if (sid && sid !== sessionId) {
    if (event.type === "progress" || event.type === "phase" || event.type === "chamber") {
      watchIds.add(sid);
    }
    if (event.type === "scores" || (event.type === "done" && reportOf(event.snapshot))) {
      const snap = event.snapshot || {};
      notifyDebateDone({
        id: sid,
        title: snap.title || "Decision analysis finished",
      });
    }
    refreshHistory();
    return;
  }
  if (event.type === "guide") {
    finishThink();
    addBubble("guide", event.message);
    renderBrief(briefOf(event));
    setPhase(event.phase);
    if (!chamberRunning) {
      if (event.ready_to_run) setStartState("ready");
      else setStartState("hidden");
    }
  }
  if (event.type === "status" || event.type === "progress") {
    setActivity(event.text);
    addThink(event.text);
    if (event.type === "status" || /debate|deliberat|planning|search|opening|challeng|scor/i.test(event.text || "")) {
      chamberRunning = true;
      setPhase("chamber");
      setStartState("running");
      if (sid) watchIds.add(sid);
    }
  }
  if (event.type === "phase") setPhase(event.phase);
  if (event.type === "chamber") {
    addChamber(event);
    if (event.kind === "progress") addThink(event.text);
    if (event.kind !== "progress") setActivity(`${event.speaker} posted a ${event.kind}.`);
    setPhase(event.kind === "score" ? "scored" : "chamber");
    if (event.kind === "score") {
      chamberRunning = false;
      setStartState("hidden");
      finishThink();
    } else {
      chamberRunning = true;
      setStartState("running");
    }
  }
  if (event.type === "scores") {
    renderReport(reportOf(event));
    setPhase("scored");
    setActivity("Scoring complete.");
    chamberRunning = false;
    setStartState("hidden");
    finishThink();
    notifyDebateDone({
      id: sid || sessionId,
      title: document.querySelector("#history button.active")?.textContent || "Decision analysis finished",
    });
  }
  if (event.type === "error") {
    addBubble("guide", event.message);
    chamberRunning = false;
    setStartState("retry");
    finishThink();
    setActivity("Gemini timed out or failed. Press Try the debate again once.");
  }
  if (event.type === "done" && event.snapshot) {
    renderBrief(briefOf(event.snapshot));
    setPhase(event.snapshot.phase);
    chamberRunning = event.snapshot.phase === "chamber" || event.snapshot.running;
    const doneReport = reportOf(event.snapshot);
    if (doneReport) {
      renderReport(doneReport);
      setStartState("hidden");
      finishThink();
      notifyDebateDone({
        id: event.snapshot.id || sid || sessionId,
        title: event.snapshot.title || "Decision analysis finished",
      });
    } else if (chamberRunning) {
      setStartState("running");
    } else if (event.snapshot.ready_to_run) {
      setStartState("retry");
    } else {
      setStartState("hidden");
    }
    refreshHistory();
  }
}

async function send({ text, confirmRun }) {
  const message = (text || "").trim();
  const images = pendingImages.map(({ mime_type, data }) => ({ mime_type, data }));
  const thumbs = pendingImages.map((img) => img.preview);
  if (inflight.has(sessionId) || (busy && sessionId && inflight.has(sessionId))) return;
  if (!sessionId) {
    if (message || thumbs.length) addBubble("user", message || "Attached a photo", thumbs);
    addBubble(
      "guide",
      "Add GEMINI_API_KEY to .env from Google AI Studio, then restart the server."
    );
    return;
  }
  if (!message && !images.length && !confirmRun) return;

  if (message || thumbs.length) addBubble("user", message || "Attached a photo", thumbs);
  pendingImages = [];
  previews.innerHTML = "";
  input.value = "";
  resizeInput();
  const owned = sessionId;
  inflight.add(owned);
  if (confirmRun) {
    chamberRunning = true;
    setStartState("running");
    setActivity("Planning the deliberation. You can switch chats.");
    addThink("Planning research and the next evidence checks.");
    watchIds.add(owned);
    askNotifyPermission();
  }
  setBusy(true);
  scrollChat();

  try {
    const response = await fetch("api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: owned,
        message,
        images,
        confirm_run: Boolean(confirmRun),
      }),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ detail: response.statusText }));
      if (sessionId === owned) addBubble("guide", err.detail || "Request failed.");
      return;
    }
    await parseSse(response, (event) => {
      if (!event.session_id) event.session_id = owned;
      applyEvent(event);
    });
    await refreshHistory();
  } catch (err) {
    if (sessionId === owned) {
      addBubble("guide", err.message || "Network error.");
      chamberRunning = false;
      setStartState("retry");
    }
  } finally {
    inflight.delete(owned);
    if (sessionId === owned) {
      setBusy(false);
      input.focus();
      scrollChat();
    }
  }
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = `${Math.min(Math.max(input.scrollHeight, 112), 260)}px`;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  send({ text: input.value, confirmRun: false });
});
startBtn.addEventListener("click", () => {
  if (chamberRunning || startBtn.disabled || inflight.has(sessionId)) return;
  send({ text: "Start deliberation", confirmRun: true });
});
watchBtn.addEventListener("click", () => agentPanel.classList.toggle("open"));
closePanelBtn.addEventListener("click", closePanel);
newChatBtn.addEventListener("click", () => startNewChat().catch((err) => addBubble("guide", err.message)));
menuBtn.addEventListener("click", () => sidebar.classList.toggle("open"));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closePanel();
    sidebar.classList.remove("open");
  }
});
fileInput.addEventListener("change", () => loadImages(fileInput.files));
input.addEventListener("input", resizeInput);
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

function renderSearch(state) {
  if (!state || state.state !== "unavailable") {
    searchWarn.classList.add("hidden");
    return;
  }
  searchWarn.textContent = "";
  searchWarn.appendChild(
    el("span", "", state.detail || "Google Search is unavailable for this key.")
  );
  searchWarn.appendChild(document.createTextNode(" "));
  const link = el("a", "", "Enable billing in Google AI Studio");
  link.href = "https://aistudio.google.com/apikey";
  link.target = "_blank";
  link.rel = "noreferrer";
  searchWarn.appendChild(link);
  searchWarn.appendChild(document.createTextNode(" to turn search on."));
  searchWarn.classList.remove("hidden");
}

async function watchSearch(initial) {
  let state = initial;
  renderSearch(state);
  for (let i = 0; i < 12 && state && state.state === "checking"; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    try {
      state = await fetch("api/search").then((r) => r.json());
    } catch (_err) {
      return;
    }
    renderSearch(state);
  }
}

async function boot() {
  resetThread(true);
  resizeInput();
  const health = await fetch("api/health").then((r) => r.json());
  if (!health.has_key) {
    setup.classList.remove("hidden");
    return;
  }
  watchSearch(health.search);
  const listed = await fetch("api/sessions").then((r) => r.json());
  const first = (listed.sessions || [])[0];
  if (first) {
    await openSession(first.id);
  } else {
    await startNewChat();
  }
  input.focus();
  setInterval(syncBackground, 4000);
}

async function syncBackground() {
  try {
    const data = await fetch("api/sessions").then((r) => r.json());
    const sessions = data.sessions || [];
    renderHistory(sessions);
    for (const item of sessions) {
      if (item.running || item.phase === "chamber") watchIds.add(item.id);
      if (watchIds.has(item.id) && item.phase === "scored" && !item.running) {
        notifyDebateDone(item);
        if (item.id === sessionId && scoreDock.classList.contains("hidden")) {
          const snap = await fetch(`api/session/${item.id}`).then((r) => r.json());
          applySnapshot(snap);
        }
      }
    }
  } catch (_err) {
    /* keep the UI usable if history poll fails */
  }
}

boot();
