/* Spenddat UI — vanilla JS, no build step. */
"use strict";

/* ------------------------------------------------------------------ utils */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const money = (m) => (m == null ? "—" : `$${(m / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const moneyShort = (m) => (m == null ? "—" : m % 100 === 0 ? `$${(m / 100).toLocaleString()}` : money(m));
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
const humanCat = (c) => (c || "").replace(/_/g, " ");
const vname = (id) => (typeof S !== "undefined" && S.summary?.vendor_names?.[id]) || (id || "").replace(/_/g, " ");

function ago(iso) {
  if (!iso) return "";
  const d = new Date(iso), s = (Date.now() - d.getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 6) return `${Math.round(s / 86400)}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const clock = (iso) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const until = (iso) => {
  const m = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  return m <= 0 ? "expiring" : m < 60 ? `${m} min left` : `${Math.round(m / 60)} h left`;
};

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Spenddat": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

function toast(text, tone = "good") {
  const t = document.createElement("div");
  t.className = `toast glass ${tone}`;
  t.innerHTML = `${tone === "good" ? ICON.check : ICON.alert}<span>${esc(text)}</span>`;
  $("#toasts").append(t);
  setTimeout(() => { t.style.transition = "opacity .4s"; t.style.opacity = "0"; }, 2600);
  setTimeout(() => t.remove(), 3100);
}

/* ------------------------------------------------------------------ icons */
const svg = (d, extra = "") => `<svg viewBox="0 0 24 24" class="ic" ${extra}>${d}</svg>`;
const ICON = {
  check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>'),
  x: svg('<path d="M6 6l12 12M18 6L6 18"/>'),
  alert: svg('<path d="M12 8v5M12 16.5v.5"/><path d="M10.3 3.8L2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.8a2 2 0 0 0-3.4 0z"/>'),
  clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  bag: svg('<path d="M6 7h12l1 13H5zM9 7a3 3 0 0 1 6 0"/>'),
  sparkle: svg('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>'),
  shield: svg('<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>'),
  chart: svg('<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'),
  list: svg('<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>'),
  gift: svg('<path d="M4 11h16v10H4zM3 7h18v4H3zM12 7v14M12 7S10.5 3 8 3.5 8 7 12 7zm0 0s1.5-4 4-3.5S16 7 12 7z"/>'),
  plug: svg('<path d="M9 2v5M15 2v5M6 7h12v4a6 6 0 0 1-12 0zM12 17v5"/>'),
  pen: svg('<path d="M4 20l4.5-1 10-10a2.1 2.1 0 0 0-3-3l-10 10zM13.5 7.5l3 3"/>'),
  book: svg('<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2zM4 19V5"/>'),
  home: svg('<path d="M3 11l9-7 9 7v9H3zM9 20v-6h6v6"/>'),
  drop: svg('<path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z"/>'),
  repeat: svg('<path d="M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3"/>'),
  store: svg('<path d="M3 9l1.5-5h15L21 9M4 9v11h16V9M3 9h18M9 20v-6h6v6"/>'),
  send: svg('<path d="M12 19V5M5 12l7-7 7 7"/>'),
};
const CAT_ICON = {
  electronics_accessories: ICON.plug, office_supplies: ICON.pen, books: ICON.book, household: ICON.home,
  personal_care: ICON.drop, gift_cards: ICON.gift, subscriptions: ICON.repeat,
};

/* ------------------------------------------------------------------ vocab */
const STATE = {
  completed: ["Purchased", "s-mint"],
  pending_approval: ["Awaiting you", "s-amber"],
  executing: ["In progress", "s-violet"],
  needs_reconciliation: ["Checking", "s-amber"],
  denied: ["Denied", "s-rose"],
  approval_denied: ["Declined by you", "s-rose"],
  approval_expired: ["Expired", "s-slate"],
  needs_clarification: ["Needs info", "s-sky"],
  rejected: ["Unsupported", "s-slate"],
  failed: ["Failed", "s-red"],
  blocked_by_circuit_breaker: ["Hard limit", "s-red"],
  received: ["Received", "s-slate"],
};
const statusChip = (s) => { const [l, c] = STATE[s] || [s, "s-slate"]; return `<span class="status ${c}"><i></i>${esc(l)}</span>`; };

const REASON = {
  price_cap_exceeded: "Over per-purchase cap", category_cap_exceeded: "Over category cap",
  daily_cap_exceeded: "Daily budget", weekly_cap_exceeded: "Weekly budget",
  vendor_not_preapproved: "New store", category_not_preapproved: "New category",
  ambiguous_product_match: "Several matches", no_product_match: "No match",
  missing_shipping_address: "No address", missing_price: "No price",
  user_max_price_exceeded: "Above your limit", price_anomaly: "Unusual price",
  velocity_exceeded: "Buying fast", vendor_blocklisted: "Blocked store",
  category_denied: "Restricted category", currency_unsupported: "Currency",
  inconsistent_total: "Inconsistent total", malformed_price: "Bad price", malformed_quantity: "Bad quantity",
};
const reasonCode = (detail) => (detail || "").split(":")[0];
const reasonText = (detail) => { const i = (detail || "").indexOf(": "); return i > -1 ? detail.slice(i + 2) : detail; };
const reasonTag = (detail) => {
  const code = reasonCode(detail);
  const bad = ["vendor_blocklisted", "category_denied"].includes(code);
  return `<span class="tag ${bad ? "bad" : "warn"}" title="${esc(reasonText(detail))}">${esc(REASON[code] || code)}</span>`;
};

const EVENT = {
  request_received: ["Request received", ""], intent_parsed: ["Understood request", ""],
  candidates_found: ["Searched the store", ""], item_resolved: ["Priced the item", ""],
  resolution_failed: ["Couldn't price item", "warn"], policy_decided: ["Policy decision", ""],
  approval_requested: ["Asked you to approve", "warn"], approval_decided: ["Your decision", ""],
  approval_expired: ["Approval timed out", "bad"], item_reconfirmed: ["Re-confirmed price", ""],
  execution_started: ["Checkout started", ""], payment_credential_issued: ["Scoped payment token issued", ""],
  execution_succeeded: ["Order placed", "good"], payment_credential_revoked: ["Payment token revoked", "good"],
  payment_credential_revoke_failed: ["Token revoke failed", "bad"], execution_failed: ["Purchase failed", "bad"],
  execution_outcome_unknown: ["Outcome unknown", "warn"], reconciliation_checked: ["Checked with store", ""],
  needs_reconciliation: ["Needs reconciliation", "warn"], transaction_denied: ["Denied by policy", "bad"],
  transaction_denied_by_human: ["Declined by you", "bad"], transaction_expired: ["Auto-denied (timeout)", "bad"],
  clarification_requested: ["Asked for details", "warn"], request_rejected: ["Request rejected", "bad"],
  vendor_unavailable: ["Store unavailable", "bad"], circuit_breaker_tripped: ["Hard limit tripped", "bad"],
};
function eventDetail(e) {
  const p = e.payload || {};
  switch (e.type) {
    case "request_received": return `“${p.request_text}”`;
    case "intent_parsed": {
      const r = p.result || {};
      return r.kind === "clarification" ? r.question : r.kind === "unsupported" ? r.explanation
        : `${r.quantity || 1} × ${r.product_description} · ${humanCat(r.category)} · ${r.vendor}${r.max_price_minor ? ` · max ${money(r.max_price_minor)}` : ""}`;
    }
    case "candidates_found": return `${(p.candidates || []).length} candidate(s)${p.candidates?.[0] ? `, best: ${p.candidates[0].title}` : ""}`;
    case "item_resolved": case "item_reconfirmed": return p.item ? `${p.item.title} · ${money(p.item.total_minor)} incl. shipping` : "";
    case "policy_decided": return `${p.outcome}${p.reason_codes?.length ? " · " + p.reason_codes.map((c) => REASON[c] || c).join(", ") : " · all checks passed"} · policy ${String(p.policy_version || "").split("#")[0]}`;
    case "approval_requested": return `expires ${clock(p.expires_at)}`;
    case "approval_decided": return `${cap(String(p.action).replace(/_/g, " "))} by ${p.decided_by}${p.modification?.max_total_minor ? ` · up to ${money(p.modification.max_total_minor)}` : ""}${p.modification?.quantity ? ` · qty ${p.modification.quantity}` : ""}`;
    case "execution_started": return `${p.basis === "human_approval" ? "Approved by you" : "Autonomous (policy ALLOW)"} · ceiling ${money(p.ceiling_minor)}`;
    case "payment_credential_issued": return `Capped at ${money(p.max_amount_minor)}, single seller, expires ${clock(p.expires_at)}${p.livemode ? " · LIVE" : ""}`;
    case "execution_succeeded": return `Order ${p.receipt?.order_id} · ${money(p.receipt?.amount_minor)}`;
    case "execution_failed": return `${p.stage}: ${p.error}`;
    case "clarification_requested": return p.question;
    case "request_rejected": return p.explanation;
    case "circuit_breaker_tripped": return p.detail;
    case "transaction_denied_by_human": return `by ${p.decided_by}`;
    default: return "";
  }
}

/* ------------------------------------------------------------------ state */
const S = { summary: null, txns: [], approvals: [], filter: "all", q: "" };

/* ------------------------------------------------------------------ routing */
function setView(v) {
  const view = ["chat", "stores"].includes(v) ? v : "dashboard";
  $("#view-dashboard").hidden = view !== "dashboard";
  $("#view-chat").hidden = view !== "chat";
  $("#view-stores").hidden = view !== "stores";
  $(".segmented").dataset.active = view;
  $$(".segmented button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  if (view === "chat") setTimeout(() => $("#input").focus(), 50);
  else if (view === "stores") loadStores(true);
  else refresh();
}
$$(".segmented button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$$("[data-goto]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.goto)));
window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
$("#bell").addEventListener("click", () => {
  setView("dashboard");
  setTimeout(() => $(".approvals-card").scrollIntoView({ behavior: "smooth", block: "center" }), 60);
});

/* ------------------------------------------------------------------ dashboard */
async function refresh() {
  try {
    const [summary, txns, approvals] = await Promise.all([
      api("/api/summary"), api("/api/transactions?mode=current&limit=400"), api("/api/approvals"),
    ]);
    Object.assign(S, { summary, txns, approvals });
    renderChrome(); renderStats(); renderChart(); renderDonut(); renderFilters(); renderTable();
    renderApprovals(); renderGuardrails();
  } catch (e) {
    toast(`Couldn't load data: ${e.message}`, "bad");
  }
}

function renderChrome() {
  const s = S.summary;
  const mp = $("#mode-pill");
  mp.classList.toggle("live", s.mode === "live");
  mp.querySelector("span").textContent = s.mode === "live" ? "LIVE · real money" : "Sandbox";
  const ap = $("#audit-pill");
  ap.classList.toggle("ok", s.audit.ok); ap.classList.toggle("bad", !s.audit.ok);
  ap.querySelector("span").textContent = s.audit.ok ? `Audit verified · ${s.audit.events}` : "Audit TAMPERED";
  ap.title = s.audit.ok ? "Every audit event's hash chain checks out" : s.audit.error;
  const n = s.pending_approvals;
  $("#bell-count").textContent = n;
  $("#bell").classList.toggle("has", n > 0);
  const storeCount = Object.keys(s.vendor_names || {}).length;
  $("#agent-sub").textContent = `${s.mode === "live" ? "LIVE" : "Sandbox"} · ${storeCount} store${storeCount === 1 ? "" : "s"} · ${s.llm}`;
  $("#agent-sub").title = Object.values(s.vendor_names || {}).join(", ");
  const h = new Date().getHours();
  $("#hello-eyebrow").textContent = `${h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"} · ${s.mode === "live" ? "Live" : "Sandbox"} mode`;
  $("#foot").textContent = `Spenddat · policy ${s.policy.version} · stores: ${Object.values(s.vendor_names || {}).join(", ")}${s.ship_to ? ` · ships to ${s.ship_to}` : ""}`;
}

function ring(pct, label, grad) {
  const r = 30, c = 2 * Math.PI * r, p = Math.max(0, Math.min(1, pct));
  const id = `g${Math.random().toString(36).slice(2, 8)}`;
  return `<svg class="ring" viewBox="0 0 76 76" role="img" aria-label="${Math.round(pct * 100)}%">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1">${grad}</linearGradient></defs>
    <circle class="track" cx="38" cy="38" r="${r}" fill="none" stroke-width="8"/>
    <circle class="bar" cx="38" cy="38" r="${r}" fill="none" stroke="url(#${id})" stroke-width="8" stroke-linecap="round"
      stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - p)}" transform="rotate(-90 38 38)"/>
    <text x="38" y="43" text-anchor="middle" style="${label.length > 4 ? "font-size:12.5px" : ""}">${esc(label)}</text></svg>`;
}
const GRAD_V = '<stop offset="0" stop-color="#c4b5fd"/><stop offset=".6" stop-color="#8b5cf6"/><stop offset="1" stop-color="#d946ef"/>';
const GRAD_HOT = '<stop offset="0" stop-color="#fcd34d"/><stop offset="1" stop-color="#fb7185"/>';

function renderStats() {
  const s = S.summary, pol = s.policy;
  const dayPct = s.spend.day_minor / pol.daily_minor, weekPct = s.spend.week_minor / pol.weekly_minor;
  $("#stat-day").innerHTML = `${ring(dayPct, `${Math.round(dayPct * 100)}%`, dayPct > 0.85 ? GRAD_HOT : GRAD_V)}
    <div class="stat-body"><div class="label">Spent today</div><div class="value">${moneyShort(s.spend.day_minor)}</div>
    <div class="meta">of <b>${moneyShort(pol.daily_minor)}</b> daily budget</div></div>`;
  $("#stat-week").innerHTML = `${ring(weekPct, `${Math.round(weekPct * 100)}%`, weekPct > 0.85 ? GRAD_HOT : GRAD_V)}
    <div class="stat-body"><div class="label">This week</div><div class="value">${moneyShort(s.spend.week_minor)}</div>
    <div class="meta">of <b>${moneyShort(pol.weekly_minor)}</b> weekly budget</div></div>`;

  const done = S.txns.filter((t) => t.state === "completed");
  const auto = done.filter((t) => t.autonomous).length;
  const pct = done.length ? Math.round((auto / done.length) * 100) : 0;
  $("#stat-auto").innerHTML = `<div class="stat-body"><div class="label">Handled autonomously</div>
    <div class="value">${pct}<small>%</small></div><div class="auto-bar"><i style="width:${pct}%"></i></div>
    <div class="meta"><b>${auto}</b> of ${done.length} purchases needed no human</div></div>`;

  const n = s.pending_approvals, el = $("#stat-pending");
  el.classList.toggle("warn", n > 0); el.classList.add("clickable");
  el.innerHTML = `<div class="pending-icon ${n ? "" : "calm"}">${n ? ICON.clock : ICON.check}</div>
    <div class="stat-body"><div class="label">Awaiting your approval</div><div class="value">${n}</div>
    <div class="meta">${n ? "Tap to review" : "Nothing needs you right now"}</div></div>`;
  el.onclick = () => $(".approvals-card").scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderChart() {
  const days = 14, today = new Date(); today.setHours(0, 0, 0, 0);
  const buckets = [...Array(days)].map((_, i) => {
    const d = new Date(today); d.setDate(d.getDate() - (days - 1 - i));
    return { d, auto: 0, human: 0, n: 0 };
  });
  for (const t of S.txns) {
    if (!["completed", "executing", "needs_reconciliation"].includes(t.state) || !t.committed_at) continue;
    const d = new Date(t.committed_at); d.setHours(0, 0, 0, 0);
    const b = buckets.find((x) => x.d.getTime() === d.getTime());
    if (!b) continue;
    b[t.autonomous ? "auto" : "human"] += t.amount_minor || 0; b.n++;
  }
  const el = $("#spend-chart");
  const W = Math.max(320, el.clientWidth || 760), H = Math.max(240, el.clientHeight || 240);
  const P = { l: 46, r: 10, t: 16, b: 28 };
  const cap = S.summary.policy.daily_minor;
  const peak = Math.max(cap * 1.12, ...buckets.map((b) => b.auto + b.human)) / 100;
  const step = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000].find((s) => peak / s <= 5) || 2000;
  const max = Math.ceil(peak / step) * step * 100;
  const bw = (W - P.l - P.r) / days, y = (v) => P.t + (H - P.t - P.b) * (1 - v / max);
  const ticks = [...Array(Math.round(max / 100 / step) + 1)].map((_, i) => i * step * 100);
  let g = `<defs>
    <linearGradient id="ba" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#c4b5fd"/><stop offset="1" stop-color="#7c3aed"/></linearGradient>
    <linearGradient id="bh" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f0abfc"/><stop offset="1" stop-color="#c026d3"/></linearGradient></defs>`;
  for (const t of ticks) g += `<line class="gridline" x1="${P.l}" x2="${W - P.r}" y1="${y(t)}" y2="${y(t)}"/><text class="axis" x="${P.l - 8}" y="${y(t) + 4}" text-anchor="end">$${t / 100}</text>`;
  g += `<line class="cap" x1="${P.l}" x2="${W - P.r}" y1="${y(cap)}" y2="${y(cap)}"/><text class="cap-label" x="${P.l + 8}" y="${y(cap) - 7}">daily cap ${moneyShort(cap)}</text>`;
  buckets.forEach((b, i) => {
    const x = P.l + i * bw + bw * 0.2, w = bw * 0.6, rad = Math.min(7, w / 2);
    const hA = (H - P.t - P.b) * (b.auto / max), hH = (H - P.t - P.b) * (b.human / max);
    const base = H - P.b;
    g += `<g class="bar-g" data-i="${i}">`;
    if (b.auto) g += `<rect x="${x}" y="${base - hA}" width="${w}" height="${Math.max(hA, 2)}" rx="${b.human ? 2 : rad}" fill="url(#ba)"/>`;
    if (b.human) g += `<rect x="${x}" y="${base - hA - hH - (b.auto ? 2 : 0)}" width="${w}" height="${Math.max(hH, 2)}" rx="${rad}" fill="url(#bh)"/>`;
    if (!b.auto && !b.human) g += `<rect x="${x}" y="${base - 3}" width="${w}" height="3" rx="1.5" fill="rgba(255,255,255,.08)"/>`;
    g += `<rect class="hit" x="${P.l + i * bw}" y="${P.t}" width="${bw}" height="${H - P.t - P.b}"/></g>`;
    if (i % 2 === (days - 1) % 2) g += `<text class="axis" x="${x + w / 2}" y="${H - 8}" text-anchor="middle">${i === days - 1 ? "Today" : b.d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily spend for the last 14 days">${g}</svg>`;
  let tip = $(".chart-tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "chart-tip glass"; tip.hidden = true; document.body.append(tip); }
  $$(".bar-g", el).forEach((node) => {
    node.addEventListener("mouseenter", () => {
      const b = buckets[+node.dataset.i];
      tip.innerHTML = `<b>${b.d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}</b>
        <div class="row"><span>Autonomous</span><span>${money(b.auto)}</span></div>
        <div class="row"><span>Approved by you</span><span>${money(b.human)}</span></div>
        <div class="row"><span>Purchases</span><span>${b.n}</span></div>`;
      tip.hidden = false;
    });
    node.addEventListener("mousemove", (e) => { tip.style.left = `${e.clientX}px`; tip.style.top = `${e.clientY}px`; });
    node.addEventListener("mouseleave", () => { tip.hidden = true; });
  });
}

function renderDonut() {
  const c = { ALLOW: 0, ESCALATE: 0, DENY: 0 };
  for (const t of S.txns) if (t.outcome in c) c[t.outcome]++;
  const total = c.ALLOW + c.ESCALATE + c.DENY;
  const parts = [
    ["ALLOW", "Allowed automatically", "#a78bfa", c.ALLOW],
    ["ESCALATE", "Escalated to you", "#fcd34d", c.ESCALATE],
    ["DENY", "Denied by policy", "#fb7185", c.DENY],
  ];
  const r = 70, C = 2 * Math.PI * r; let off = 0, arcs = "";
  for (const [, , color, n] of parts) {
    if (!n) continue;
    const len = (n / total) * C, gap = total > n ? 4 : 0;
    arcs += `<circle cx="95" cy="95" r="${r}" fill="none" stroke="${color}" stroke-width="18" stroke-linecap="round"
      stroke-dasharray="${Math.max(len - gap, 0.1)} ${C}" stroke-dashoffset="${-off}" transform="rotate(-90 95 95)"
      style="filter:drop-shadow(0 0 8px ${color}66)"/>`;
    off += len;
  }
  $("#donut").innerHTML = `<svg class="donut" viewBox="0 0 190 190" role="img" aria-label="Decision mix">
      <circle cx="95" cy="95" r="${r}" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="18"/>${arcs}
      <text class="center-n" x="95" y="98" text-anchor="middle">${total}</text>
      <text class="center-l" x="95" y="118" text-anchor="middle">decisions</text></svg>
    <div class="donut-legend">${parts.map(([, l, color, n]) => `<div class="row"><span><i class="dot" style="background:${color};box-shadow:0 0 8px ${color}"></i>${l}</span><b>${n}</b></div>`).join("")}</div>`;
}

const FILTERS = [
  ["all", "All", () => true],
  ["completed", "Purchased", (t) => t.state === "completed"],
  ["pending", "Awaiting you", (t) => t.state === "pending_approval"],
  ["denied", "Denied", (t) => ["denied", "approval_denied", "approval_expired", "rejected"].includes(t.state)],
  ["other", "Other", (t) => ["needs_clarification", "failed", "blocked_by_circuit_breaker", "needs_reconciliation", "executing"].includes(t.state)],
];
function renderFilters() {
  $("#filters").innerHTML = FILTERS.map(([k, l, f]) =>
    `<button class="chip" role="tab" data-f="${k}" aria-selected="${S.filter === k}">${l}<span class="n">${S.txns.filter(f).length}</span></button>`).join("");
  $$("#filters .chip").forEach((b) => b.addEventListener("click", () => { S.filter = b.dataset.f; renderFilters(); renderTable(); }));
}

function txnRow(t, compact = false) {
  const title = t.title || t.request_text;
  const meta = compact ? `${ago(t.created_at)}` :
    `${t.vendor ? esc(vname(t.vendor)) + " · " : ""}${t.category ? esc(humanCat(t.category)) + " · " : ""}${ago(t.created_at)}${t.approval?.decided_by && t.state === "completed" ? " · approved by you" : t.autonomous ? " · autonomous" : ""}`;
  const amtDim = t.state !== "completed";
  return `<div class="tr" role="row" tabindex="0" data-txn="${esc(t.txn_id)}">
    <div class="glyph">${CAT_ICON[t.category] || ICON.bag}</div>
    <div class="main"><div class="title">${esc(title)}</div><div class="meta">${meta}</div></div>
    ${statusChip(t.state)}
    <div class="amt ${amtDim ? "dim" : ""}">${t.amount_minor ? money(t.amount_minor) : "—"}</div></div>`;
}
function bindRows(root) {
  $$(".tr[data-txn]", root).forEach((r) => {
    const open = () => openSheet(r.dataset.txn);
    r.addEventListener("click", open);
    r.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}
function renderTable() {
  const f = FILTERS.find(([k]) => k === S.filter)[2];
  const q = S.q.toLowerCase();
  const rows = S.txns.filter(f).filter((t) => !q || [t.title, t.request_text, t.vendor, t.category, t.txn_id, t.order_id].join(" ").toLowerCase().includes(q));
  $("#history-sub").textContent = `${S.txns.length} transaction${S.txns.length === 1 ? "" : "s"} in ${S.summary.mode} mode`;
  $("#txn-table").innerHTML = rows.length ? rows.map((t) => txnRow(t)).join("") :
    `<div class="empty">${S.txns.length ? "Nothing matches." : "No transactions yet. Ask the agent to buy something."}</div>`;
  bindRows($("#txn-table"));
}
$("#search").addEventListener("input", (e) => { S.q = e.target.value; renderTable(); });

function approvalCard(a, { inChat = false } = {}) {
  const it = a.item || a.presented?.item;
  const reasons = a.reasons || a.presented?.reasons || [];
  const alts = a.alternatives || a.presented?.alternatives || [];
  const id = a.approval_id;
  const inner = `
    <div class="appr-top"><div><div class="t">${esc(it.title)}</div>
      <div class="v">${esc(vname(it.vendor))} · SKU ${esc(it.sku)} · qty ${it.quantity}</div></div>
      <div class="price">${money(it.total_minor)}</div></div>
    <div class="reasons">${reasons.map(reasonTag).join("")}</div>
    <p class="why">${reasons.map((r) => esc(reasonText(r))).join(" · ")}${alts.length ? `<br>Other matches: ${alts.map(esc).join("; ")}` : ""}</p>
    <div class="actions">
      <button class="btn primary sm" data-act="approve">${ICON.check}Approve</button>
      <button class="btn sm" data-act="limit">Approve up to…</button>
      <button class="btn danger sm" data-act="deny">Deny</button>
      <span class="expires">${ICON.clock.replace('class="ic"', 'class="ic" style="width:13px;height:13px;vertical-align:-2px"')} ${until(a.expires_at)}</span>
    </div>
    <div class="limit-form"><div class="money-input"><span>$</span><input type="number" min="0.01" step="0.01" inputmode="decimal" value="${(it.total_minor / 100).toFixed(2)}" aria-label="Maximum total in dollars"></div>
      <button class="btn primary sm" data-act="approve-limit">Approve at this limit</button></div>`;
  return inChat
    ? `<div class="rcard approval glass" data-approval="${esc(id)}"><div class="rhead"><span class="ttl">Needs your approval</span>${statusChip("pending_approval")}</div>${inner}</div>`
    : `<div class="appr" data-approval="${esc(id)}">${inner}</div>`;
}

function bindApproval(el, onDone) {
  const id = el.dataset.approval;
  const busy = (b) => $$("button", el).forEach((x) => (x.disabled = b));
  const run = async (body) => {
    busy(true);
    try {
      const res = await api(`/api/approvals/${encodeURIComponent(id)}`, body);
      onDone(res.blocks || [], body);
    } catch (e) { toast(e.message, "bad"); busy(false); }
  };
  $("[data-act=approve]", el).addEventListener("click", () => run({ action: "approve" }));
  $("[data-act=deny]", el).addEventListener("click", () => run({ action: "deny" }));
  $("[data-act=limit]", el).addEventListener("click", () => $(".limit-form", el).classList.toggle("open"));
  $("[data-act=approve-limit]", el).addEventListener("click", () => {
    const v = Math.round(parseFloat($(".limit-form input", el).value) * 100);
    if (!(v > 0)) return toast("Enter a positive amount", "bad");
    run({ action: "approve", max_total_minor: v });
  });
}

function renderApprovals() {
  const el = $("#approvals");
  if (!S.approvals.length) {
    el.innerHTML = `<div class="calm-state"><div class="pending-icon calm">${ICON.check}</div>
      <div>You're all caught up.<br>In-policy purchases run on their own.</div></div>`;
    return;
  }
  el.innerHTML = S.approvals.map((a) => approvalCard(a)).join("");
  $$("[data-approval]", el).forEach((card) => bindApproval(card, (blocks) => {
    const b = blocks.find((x) => x.type === "receipt") ? "Approved and purchased" : blocks[0]?.text || "Done";
    toast(b, blocks.some((x) => x.tone === "warn" || x.type === "status") ? "bad" : "good");
    refresh();
  }));
}

function gauge(label, used, limit, hint, hard = false) {
  const pct = limit ? Math.min(100, (used / limit) * 100) : 0;
  return `<div class="gauge ${hard ? "hard" : ""}"><div class="top"><span>${esc(label)}${hard ? '<span class="hard-tag">HARD</span>' : ""}</span><b>${used != null ? `${moneyShort(used)} / ` : ""}${moneyShort(limit)}</b></div>
    ${used != null ? `<div class="bar"><i class="${pct > 85 ? "hot" : ""}" style="width:${pct}%"></i></div>` : ""}
    <div class="hint">${esc(hint)}</div></div>`;
}
function renderGuardrails() {
  const s = S.summary, p = s.policy, h = s.hard_limits;
  $("#policy-version").textContent = `Policy ${p.version.split("#")[0]} · hash ${p.version.split("#")[1] || ""}`;
  const cats = Object.entries(p.per_category_minor).map(([c, v]) => gauge(cap(humanCat(c)), null, v, "Per purchase, this category"));
  $("#guardrails").innerHTML = [
    gauge("Daily budget", s.spend.day_minor, p.daily_minor, "Rolling 24 hours · over this escalates"),
    gauge("Weekly budget", s.spend.week_minor, p.weekly_minor, "Rolling 7 days · over this escalates"),
    gauge("Per purchase", null, p.per_transaction_minor, "Any single order above this escalates"),
    ...cats,
    gauge("Absolute daily ceiling", s.spend.day_minor, h.daily_minor, "Circuit breaker · no approval can exceed it", true),
    gauge("Absolute per-order ceiling", null, h.transaction_minor, "Circuit breaker · no approval can exceed it", true),
  ].join("") + `<div class="guard-foot" style="grid-column:1/-1">
      <span>Unusual price: ±${p.price_deviation_pct}% vs this item's average</span><span>·</span>
      <span>Velocity: over ${p.velocity.max_purchases} buys / ${p.velocity.window_minutes} min</span><span>·</span>
      <span>Approvals auto-deny after ${p.approval_timeout_minutes} min</span></div>
    <div class="guard-foot" style="grid-column:1/-1">${p.allowed_categories.map((c) => `<span class="tag">${esc(humanCat(c))}</span>`).join("")}
      ${p.denied_categories.map((c) => `<span class="tag bad">${esc(humanCat(c))}</span>`).join("")}</div>`;
}

/* ------------------------------------------------------------------ detail sheet */
async function openSheet(txnId) {
  const sheet = $("#sheet");
  sheet.innerHTML = `<div class="empty">Loading…</div>`;
  sheet.classList.add("open"); sheet.setAttribute("aria-hidden", "false"); $("#scrim").hidden = false;
  try {
    const t = await api(`/api/transactions/${encodeURIComponent(txnId)}`);
    sheet.innerHTML = `
      <button class="close" aria-label="Close">${ICON.x}</button>
      ${statusChip(t.state)}
      <h3>${esc(t.title || t.request_text)}</h3>
      <div class="sub">“${esc(t.request_text)}”</div>
      <div class="amount-big">${t.amount_minor ? money(t.amount_minor) : ""}${t.state === "completed" ? "" : `<span class="sub" style="font-size:14px;font-weight:500;margin-left:${t.amount_minor ? "10px" : "0"}">${t.amount_minor ? "quoted · not charged" : "Nothing charged"}</span>`}</div>
      <dl class="kv">
        <dt>Decision</dt><dd>${esc(t.outcome || "—")}${t.autonomous ? " · executed autonomously" : ""}</dd>
        ${t.approval ? `<dt>Approval</dt><dd>${esc(t.approval.status)}${t.approval.decided_by ? ` by ${esc(t.approval.decided_by)}` : ""}</dd>` : ""}
        <dt>Store</dt><dd>${esc(t.vendor ? vname(t.vendor) : "—")}</dd>
        <dt>Category</dt><dd>${esc(humanCat(t.category) || "—")}</dd>
        ${t.order_id ? `<dt>Order</dt><dd><code>${esc(t.order_id)}</code></dd>` : ""}
        <dt>Mode</dt><dd>${esc(t.mode)}</dd>
        <dt>Transaction</dt><dd><code>${esc(t.txn_id)}</code></dd>
      </dl>
      ${t.reasons.length ? `<div class="section-t">Why</div><div class="reason-list">${t.reasons.map((r) => `<div class="reason">${reasonTag(r)} &nbsp;${esc(reasonText(r))}</div>`).join("")}</div>` : ""}
      <div class="section-t">Audit trail</div>
      <ol class="timeline">${t.events.map((e) => {
        const [label, tone] = EVENT[e.type] || [e.type, ""];
        const det = eventDetail(e);
        return `<li><span class="node ${tone}"></span>
          <div class="ev"><span>${esc(label)}</span><time>${esc(clock(e.ts))}</time></div>
          ${det ? `<div class="evd">${esc(det)}</div>` : ""}
          <details><summary>#${e.seq} · <span class="hash">${esc(e.hash)}…</span></summary><pre>${esc(JSON.stringify(e.payload, null, 2))}</pre></details></li>`;
      }).join("")}</ol>`;
    $(".close", sheet).addEventListener("click", closeSheet);
  } catch (e) { sheet.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
function closeSheet() {
  const sheet = $("#sheet"); sheet.classList.remove("open"); sheet.setAttribute("aria-hidden", "true"); $("#scrim").hidden = true;
}
$("#scrim").addEventListener("click", closeSheet);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });

/* ------------------------------------------------------------------ chat */
const SUGGEST = [
  [ICON.bag, "Buy a clear phone case", "for my iPhone 15, under $25", "Buy a clear phone case for my iPhone 15 under $25"],
  [ICON.sparkle, "Buy adidas Sambas", "white, size 8.5, under $100", "Buy adidas sambas if they are under $100 size 8.5 in white"],
  [ICON.gift, "Buy a $50 gift card", "see how restricted items are handled", "Buy a $50 gift card"],
  [ICON.chart, "How much have I spent?", "today and this week, against caps", "How much have I spent?"],
  [ICON.clock, "What's waiting for me?", "pending approvals", "What's pending?"],
  [ICON.shield, "What are my limits?", "the rules I follow", "What are my limits?"],
];
$("#suggestions").innerHTML = SUGGEST.map(([ic, b, s, q]) =>
  `<button class="suggestion" data-q="${esc(q)}"><span class="k">${ic}</span><span><b>${esc(b)}</b><span>${esc(s)}</span></span></button>`).join("");
$$(".suggestion").forEach((b) => b.addEventListener("click", () => send(b.dataset.q)));

const chat = { cid: store.get("spenddat.cid", null), log: store.get("spenddat.log", []), busy: false };
const input = $("#input"), msgs = $("#messages");

function autosize() { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; }
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(input.value); }
});
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
$("#new-chat").addEventListener("click", () => {
  chat.cid = null; chat.log = []; store.del("spenddat.cid"); store.del("spenddat.log");
  $$(".msg", msgs).forEach((m) => m.remove()); $("#welcome").hidden = false; input.focus();
});

function scrollDown() { requestAnimationFrame(() => { msgs.scrollTop = msgs.scrollHeight; }); }
const now = () => new Date().toISOString();
const stamp = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

function renderBlock(b) {
  switch (b.type) {
    case "text":
      return `<div class="bubble glass ${b.tone || ""}">${esc(b.text)}</div>`;
    case "receipt":
      return `<div class="rcard receipt glass"><div class="stripe"></div>
        <div class="rhead"><span class="ttl">${esc(b.title)}</span>${statusChip("completed")}</div>
        <div class="big">${money(b.amount_minor)}</div>
        <div class="lines">
          <div><span>${b.quantity} × ${money(b.unit_minor)}</span><b>${money((b.unit_minor || 0) * (b.quantity || 1))}</b></div>
          <div><span>Shipping</span><b>${money(b.shipping_minor)}</b></div>
          <div><span>Tax</span><b>${money(b.tax_minor)}</b></div>
          <div class="total"><span>Total charged</span><b>${money(b.amount_minor)}</b></div></div>
        <div class="foot-row"><span>${esc(vname(b.vendor))} · order ${esc(b.order_id)} · ${b.basis === "human_approval" ? "approved by you" : "autonomous"}${b.mode === "live" ? " · LIVE" : ""}</span>
          <button class="linkish" data-open="${esc(b.txn_id)}">Audit trail →</button></div></div>`;
    case "approval":
      return approvalCard(b, { inChat: true });
    case "denied":
      return `<div class="rcard denied glass"><div class="rhead"><span class="ttl">I can't buy ${esc(b.title || "that")}</span>${statusChip("denied")}</div>
        <div style="display:flex;gap:12px;align-items:flex-start"><div class="x">${ICON.x}</div>
        <div style="font-size:14px;color:var(--ink-2)">Your policy rules this out, so it doesn't go to approval.
        ${b.reasons.map((r) => `<div class="reason">${reasonTag(r)} &nbsp;${esc(reasonText(r))}</div>`).join("")}</div></div>
        <div class="foot-row" style="margin-top:12px"><span></span><button class="linkish" data-open="${esc(b.txn_id)}">Audit trail →</button></div></div>`;
    case "status":
      return `<div class="rcard glass"><div class="rhead"><span class="ttl">${esc((STATE[b.state] || [b.state])[0])}</span>${statusChip(b.state)}</div>
        <div style="font-size:14px;color:var(--ink-2)">${esc(b.text)}</div>
        <div class="foot-row" style="margin-top:10px"><span></span><button class="linkish" data-open="${esc(b.txn_id)}">Audit trail →</button></div></div>`;
    case "spend": {
      const p = b.policy, h = b.hard_limits;
      return `<div class="meters glass">${gauge("Today", b.spend.day_minor, p.daily_minor, "Policy daily budget")}
        ${gauge("This week", b.spend.week_minor, p.weekly_minor, "Policy weekly budget")}
        ${gauge("Absolute daily ceiling", b.spend.day_minor, h.daily_minor, "Circuit breaker", true)}</div>`;
    }
    case "policy": {
      const p = b.policy, h = b.hard_limits;
      const rows = [
        ["Per purchase", money(p.per_transaction_minor)], ["Daily budget", money(p.daily_minor)],
        ["Weekly budget", money(p.weekly_minor)],
        ...Object.entries(p.per_category_minor).map(([c, v]) => [cap(humanCat(c)), money(v)]),
        ["Hard per-order ceiling", money(h.transaction_minor)], ["Hard daily ceiling", money(h.daily_minor)],
      ];
      return `<div class="rcard glass"><div class="rhead"><span class="ttl">Guardrails</span><span class="tag">${esc(p.version.split("#")[0])}</span></div>
        <div class="lines">${rows.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">${p.denied_categories.map((c) => `<span class="tag bad">${esc(humanCat(c))}</span>`).join("")}</div>
        <div class="done-note">Over a cap, a new store, several matches, or an unusual price → I ask you. Restricted categories or blocked stores → I decline.</div></div>`;
    }
    case "list":
      return b.transactions.length
        ? `<div class="mini-list glass">${b.transactions.map((t) => txnRow(t, true)).join("")}</div>`
        : `<div class="bubble glass">No transactions yet.</div>`;
    default:
      return `<div class="bubble glass">${esc(JSON.stringify(b))}</div>`;
  }
}

function addMessage(role, blocks, ts = now(), persist = true) {
  $("#welcome").hidden = true;
  const m = document.createElement("div");
  m.className = `msg ${role}`;
  const body = role === "user"
    ? `<div class="bubble">${esc(blocks)}</div>`
    : blocks.map(renderBlock).join("");
  m.innerHTML = `${role === "agent" ? '<span class="orb xs" aria-hidden="true"></span>' : ""}<div class="col">${body}<span class="stamp">${stamp(ts)}</span></div>`;
  msgs.append(m);
  $$("[data-open]", m).forEach((b) => b.addEventListener("click", () => openSheet(b.dataset.open)));
  bindRows(m);
  $$(".rcard[data-approval]", m).forEach((card) => {
    if (chat.log.some((e) => e.decided === card.dataset.approval)) return markDecided(card, "Already decided");
    bindApproval(card, (res, body) => {
      markDecided(card, body.action === "deny" ? "You denied this." : "You approved this.");
      chat.log.push({ decided: card.dataset.approval });
      addMessage("agent", res);
      refreshQuiet();
    });
  });
  if (persist) {
    chat.log.push({ role, blocks, ts });
    store.set("spenddat.log", chat.log.slice(-200));
  }
  scrollDown();
}
function markDecided(card, note) {
  card.classList.add("decided");
  $$("button", card).forEach((b) => (b.disabled = true));
  if (!$(".done-note", card)) card.insertAdjacentHTML("beforeend", `<div class="done-note">${esc(note)}</div>`);
}

async function send(text) {
  text = (text || "").trim();
  if (!text || chat.busy) return;
  chat.busy = true; $("#send").disabled = true;
  input.value = ""; autosize();
  addMessage("user", text);
  const typing = document.createElement("div");
  typing.className = "msg agent";
  typing.innerHTML = `<span class="orb xs"></span><div class="col"><div class="bubble glass typing"><i></i><i></i><i></i></div></div>`;
  msgs.append(typing); scrollDown();
  try {
    const res = await api("/api/chat", { conversation_id: chat.cid, message: text });
    chat.cid = res.conversation_id; store.set("spenddat.cid", chat.cid);
    typing.remove();
    addMessage("agent", res.blocks.length ? res.blocks : [{ type: "text", text: "…" }]);
    refreshQuiet();
  } catch (e) {
    typing.remove();
    addMessage("agent", [{ type: "text", tone: "warn", text: `Something went wrong: ${e.message}` }]);
  } finally {
    chat.busy = false; $("#send").disabled = false; input.focus();
  }
}

async function refreshQuiet() {
  try {
    const s = await api("/api/summary");
    S.summary = s; renderChrome();
  } catch { /* ignore */ }
}

let rz, lastSize = "";
new ResizeObserver(() => {
  const el = $("#spend-chart"), size = `${el.clientWidth}x${el.clientHeight}`;
  if (size === lastSize || !S.summary || $("#view-dashboard").hidden) return;
  lastSize = size; clearTimeout(rz); rz = setTimeout(renderChart, 60);
}).observe($("#spend-chart"));

/* ------------------------------------------------------------------ stores */
const ST = { data: null, adding: false, addType: "acp", confirm: null };
const MONO = [
  "linear-gradient(135deg,#a78bfa,#6d28d9)", "linear-gradient(135deg,#f0abfc,#c026d3)",
  "linear-gradient(135deg,#7dd3fc,#4f46e5)", "linear-gradient(135deg,#fda4af,#be185d)",
  "linear-gradient(135deg,#6ee7b7,#0f766e)", "linear-gradient(135deg,#fcd34d,#c2410c)",
];
const hashIdx = (str, n) => [...str].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7) % n;
const TYPE_LABEL = { acp: "Agent checkout", reference: "Demo store", stub: "Test store", shopify_web: "Shopify · browser" };

async function loadStores(check = false) {
  try {
    const q = check === true ? "?check=1" : check ? `?check=${encodeURIComponent(check)}` : "";
    ST.data = await api(`/api/stores${q}`);
    renderStores();
  } catch (e) { toast(`Couldn't load stores: ${e.message}`, "bad"); }
}

function storeCard(st) {
  const h = st.health;
  const health = !st.enabled ? `<span class="health"><i></i>Turned off</span>`
    : !st.loaded ? `<span class="health bad"><i></i>Not loaded</span>`
    : h == null ? `<span class="health"><i></i>${st.type === "shopify_web" ? "Not checked yet (opens Chrome)" : "Checking…"}</span>`
    : h.ok ? `<span class="health ok"><i></i>Connected${h.products != null ? ` · ${h.products} products` : ""}${h.tools != null ? ` · ${h.tools} agent tools` : ""}</span>`
    : `<span class="health bad" title="${esc(h.error)}"><i></i>Unreachable</span>`;
  const where = st.type === "shopify_web"
    ? `<div>Store <code>${esc(st.store_url || "—")}</code></div>
       <div>Pays with your saved Shop Pay card · you approve every order${ST.data.mode === "sandbox" && !st.test_store ? " · <b>sandbox: dry run, no real orders</b>" : st.test_store ? " · development store (test orders)" : ""}</div>`
    : st.type === "acp"
    ? `<div>Checkout <code>${esc(st.base_url || "—")}</code></div>
       ${st.secret_env ? `<div>API key from <code>${esc(st.secret_env)}</code></div>` : ""}`
    : st.catalog ? `<div>Bundled catalog <code>${esc(typeof st.catalog === "string" ? st.catalog : "custom")}</code> · fake payments</div>` : "";
  const confirming = ST.confirm === st.id;
  return `<article class="glass store ${st.enabled ? "" : "off"}" data-store="${esc(st.id)}">
    <div class="store-top">
      <div class="monogram" style="background:${MONO[hashIdx(st.id, MONO.length)]}">${esc(st.name.slice(0, 1).toUpperCase())}</div>
      <div class="store-id"><b>${esc(st.name)}</b><span>${esc(st.id)} · ${esc(TYPE_LABEL[st.type] || st.type)}</span></div>
      <button class="switch" role="switch" aria-checked="${st.enabled}" aria-label="Enable ${esc(st.name)}" data-act="toggle"></button>
    </div>
    <div class="store-badges">${health}</div>
    <div class="trust-seg" role="group" aria-label="Trust level">
      ${[["auto", "Auto-buy"], ["ask", "Ask me"], ["blocked", "Blocked"]].map(([k, l]) => {
        const locked = k === "auto" && st.type === "shopify_web";
        return `<button data-trust="${k}" aria-pressed="${st.trust === k}" ${locked ? 'disabled title="Shopify requires your approval for every order"' : ""}>${l}</button>`;
      }).join("")}
    </div>
    <div class="store-meta">${where}</div>
    <div class="store-actions">
      <button class="btn ghost sm" data-act="test">${ICON.repeat}Test connection</button>
      ${confirming
        ? `<span class="confirm-row">Remove ${esc(st.name)}? <button class="btn danger sm" data-act="remove-yes">Remove</button><button class="btn ghost sm" data-act="remove-no">Cancel</button></span>`
        : `<button class="btn ghost sm" data-act="remove">Remove</button>`}
    </div>
  </article>`;
}

function renderStores() {
  const d = ST.data;
  if (!d.editable) {
    $("#store-list").innerHTML = `<div class="glass card empty">Store editing isn't available in this mode.</div>`;
    $("#add-store-btn").hidden = true;
    return;
  }
  $("#stores-eyebrow").textContent = `Stores · ${d.mode === "live" ? "Live" : "Sandbox"} mode${d.demo ? " · demo" : ""}`;
  $("#store-list").innerHTML = d.stores.length ? d.stores.map(storeCard).join("")
    : `<div class="glass card empty">No stores yet. Add one to start shopping.</div>`;
  $("#stores-foot").textContent = `Stores file: ${d.vendors_file} · trust levels live in your policy: ${d.policy_file}. Every change is recorded in the audit log.`;
  $$("#store-list .store").forEach(bindStore);
  renderAddStore();
}

async function storeChange(path, body, okMsg) {
  try {
    await api(path, body);
    toast(okMsg);
    ST.confirm = null;
    await loadStores(true);
    refreshQuiet();
  } catch (e) { toast(e.message, "bad"); await loadStores(false); }
}

function bindStore(card) {
  const id = card.dataset.store;
  const st = ST.data.stores.find((x) => x.id === id);
  $("[data-act=toggle]", card).addEventListener("click", () =>
    storeChange(`/api/stores/${id}`, { enabled: !st.enabled }, `${st.name} ${st.enabled ? "turned off" : "turned on"}`));
  $$(".trust-seg button:not(:disabled)", card).forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.trust === st.trust) return;
    const label = { auto: "can auto-buy", ask: "will ask you first", blocked: "is blocked" }[b.dataset.trust];
    storeChange(`/api/stores/${id}`, { trust: b.dataset.trust }, `${st.name} ${label}`);
  }));
  $("[data-act=test]", card).addEventListener("click", async () => {
    if (st.type === "shopify_web") toast(`Opening ${st.name} in Chrome…`);
    await loadStores(id);
    const now = ST.data.stores.find((x) => x.id === id);
    toast(now?.health?.ok ? `${st.name} is reachable` : `${st.name}: ${now?.health?.error || "not loaded"}`, now?.health?.ok ? "good" : "bad");
  });
  $("[data-act=remove]", card)?.addEventListener("click", () => { ST.confirm = id; renderStores(); });
  $("[data-act=remove-no]", card)?.addEventListener("click", () => { ST.confirm = null; renderStores(); });
  $("[data-act=remove-yes]", card)?.addEventListener("click", () =>
    storeChange(`/api/stores/${id}/remove`, {}, `${st.name} removed`));
}

function renderAddStore() {
  const box = $("#add-store");
  box.hidden = !ST.adding;
  if (!ST.adding) return;
  const d = ST.data;
  const t = ST.addType;
  const types = [
    ["acp", "Agent checkout store", "A shop that accepts AI-agent purchases (Agentic Commerce Protocol). Needs its checkout URL and an API key."],
    ...(d.demo ? [["reference", "Demo store", "A bundled test shop with a ready-made catalog and fake payments."]] : []),
    ["shopify_web", "Shopify store", "Any Shopify store. Uses the agent tools Shopify publishes in the browser; pays with your saved Shop Pay card; you approve every order."],
  ];
  box.innerHTML = `<header class="card-head"><div><h2>Add a store</h2><p class="sub">New stores start on “Ask me”, so nothing is bought there without you until you say so.</p></div>
      <button class="btn ghost sm" data-act="close">${ICON.x}Close</button></header>
    <div class="type-pick">${types.map(([k, b, s]) =>
      `<button class="type-opt" data-type="${k}" aria-pressed="${t === k}"><b>${b}</b><span>${s}</span></button>`).join("")}</div>
    <form id="store-form" class="form-grid" autocomplete="off">
      <label class="field"><span>Store name</span><input name="name" required maxlength="60" placeholder="${t === "reference" ? "Kicks Outlet" : "e.g. Sneaker Street"}"></label>
      <label class="field"><span>Trust</span><select name="trust"><option value="ask">Ask me (recommended)</option>${t === "shopify_web" ? "" : '<option value="auto">Auto-buy</option>'}<option value="blocked">Blocked</option></select></label>
      ${t === "shopify_web" ? `
        <label class="field full"><span>Store address <small>(the shop's home page)</small></span><input name="store_url" type="url" required placeholder="https://shop.example.com"></label>
        ${d.mode === "sandbox" ? `<label class="field full check"><span><input type="checkbox" name="test_store" value="1"> This is a Shopify <b>development</b> store (its orders aren't real). Otherwise sandbox mode stops before placing the order.</span></label>` : ""}` : t === "reference" ? `
        <label class="field full"><span>Catalog</span><select name="catalog">${d.catalogs.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select></label>` : `
        <label class="field full"><span>Checkout URL <small>(the store's ACP base URL)</small></span><input name="base_url" type="url" required placeholder="https://shop.example.com/acp"></label>
        <label class="field full"><span>Product feed URL</span><input name="feed_url" type="url" required placeholder="https://shop.example.com/acp/feed.json"></label>
        <label class="field"><span>Seller payment profile <small>(Stripe, profile_…)</small></span><input name="seller_profile" required placeholder="profile_…"></label>
        <label class="field"><span>API key secret name <small>(optional)</small></span><input name="api_key_secret" placeholder="auto: &lt;store_id&gt;_api_key"></label>`}
    </form>
    <div class="form-foot">
      <p>${t === "shopify_web" ? `Before the first purchase run <code>spenddat browser login</code> once and sign in to Shop Pay with a saved card. Chrome opens when Spenddat shops here.` : t === "acp" ? `API keys are never saved by this page. After adding, put the key in your <code>.env</code> file under the name shown on the store card, then restart.` : "Demo stores use fake payments and never charge anything."}</p>
      <button class="btn primary" data-act="save">${ICON.check}Add store</button>
    </div>`;
  $("[data-act=close]", box).addEventListener("click", () => { ST.adding = false; renderAddStore(); });
  $$(".type-opt:not(:disabled)", box).forEach((b) => b.addEventListener("click", () => { ST.addType = b.dataset.type; renderAddStore(); }));
  $("[data-act=save]", box).addEventListener("click", async () => {
    const f = $("#store-form");
    if (!f.reportValidity()) return;
    const body = Object.fromEntries(new FormData(f).entries());
    body.type = t;
    if (!body.api_key_secret) delete body.api_key_secret;
    if (t === "shopify_web") body.test_store = body.test_store === "1";
    await storeChange("/api/stores", body, `${body.name} added`);
    if (!$("#store-list").textContent.includes(body.name)) return;
    ST.adding = false; renderAddStore();
  });
  setTimeout(() => $("input[name=name]", box)?.focus(), 30);
}

$("#add-store-btn").addEventListener("click", () => {
  ST.adding = !ST.adding;
  if (ST.adding) ST.addType = ST.data?.demo ? "reference" : "acp";
  renderAddStore();
  if (ST.adding) $("#add-store").scrollIntoView({ behavior: "smooth", block: "center" });
});

/* restore previous conversation */
for (const e of chat.log.filter((x) => x.role)) addMessage(e.role, e.blocks, e.ts, false);

/* ------------------------------------------------------------------ boot */
setView(location.hash.slice(1) || "dashboard");
refreshQuiet();
setInterval(() => { if (!document.hidden && !$("#view-dashboard").hidden) refresh(); else refreshQuiet(); }, 8000);
