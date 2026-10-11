/* Securifi web UI.
 *
 * A thin client over the operation registry: every action calls POST /api/v1/ops/<operation>,
 * and every form is generated from that operation's JSON schema. The UI therefore has no
 * business logic and always matches what the API, MCP server and CLI can do.
 */
"use strict";

const state = { key: null, me: null, ops: [], opsByName: {}, info: {}, noteholders: {} };
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------------------------------------------------------------- utilities

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function frag(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content;
}
const fmt = {
  money(v) {
    if (v === null || v === undefined || v === "") return "–";
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : esc(v);
  },
  whole(v) {
    if (v === null || v === undefined || v === "") return "–";
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 0 }) : esc(v);
  },
  pct(v, dp = 2) {
    if (v === null || v === undefined || v === "") return "–";
    const n = Number(v);
    return Number.isFinite(n) ? (n * 100).toFixed(dp) + "%" : esc(v);
  },
  num(v, dp = 2) {
    if (v === null || v === undefined || v === "") return "–";
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: dp }) : esc(v);
  },
  date(v) { return v ? esc(String(v).slice(0, 10)) : "–"; },
  time(v) { return v ? esc(String(v).replace("T", " ").slice(0, 19)) : "–"; },
  bool(v) { return v ? '<span class="badge ok">Yes</span>' : '<span class="badge muted">No</span>'; },
  pass(v) { return v ? '<span class="badge ok">Pass</span>' : '<span class="badge bad">Breach</span>'; },
  status(v) {
    const cls = { finalised: "ok", approved: "ok", active: "ok", success: "ok", matched: "ok", loaded: "ok",
      calculated: "warn", open: "muted", dry_run: "warn", superseded: "muted", cleared: "muted",
      rejected: "bad", denied: "bad", failed: "bad", invalid: "bad", unmatched: "warn" }[v] || "";
    return `<span class="badge ${cls}">${esc(v)}</span>`;
  },
  json(v) { return `<code>${esc(typeof v === "string" ? v : JSON.stringify(v))}</code>`; },
};

function toast(message, bad = false) {
  const t = $("#toast");
  t.textContent = message;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), bad ? 7000 : 3500);
}

function storage(action, key, value) {
  try {
    if (action === "get") return sessionStorage.getItem(key);
    if (action === "set") sessionStorage.setItem(key, value);
    if (action === "del") sessionStorage.removeItem(key);
  } catch (_) { /* storage unavailable */ }
  return null;
}

// ---------------------------------------------------------------- API

class OpFailure extends Error {
  constructor(error) {
    super(error?.message || "request failed");
    this.error = error || {};
  }
}

async function raw(op, params = {}, { dryRun = false } = {}) {
  const res = await fetch(`/api/v1/ops/${op}${dryRun ? "?dry_run=true" : ""}`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${state.key}`, "Content-Type": "application/json", "X-Securifi-Client": "ui" },
    body: JSON.stringify(params),
  });
  if (res.status === 401) { logout(); throw new OpFailure({ message: "session expired" }); }
  return res.json();
}

async function call(op, params = {}, opts = {}) {
  const body = await raw(op, params, opts);
  if (!body.ok) throw new OpFailure(body.error);
  return body.data;
}

function describeError(err) {
  const e = err.error || {};
  let msg = e.message || err.message;
  if (Array.isArray(e.details)) msg += ": " + e.details.map(d => typeof d === "string" ? d : `${d.field || ""} ${d.message || JSON.stringify(d)}`).join("; ");
  return msg;
}

async function openReport(id) {
  const res = await fetch(`/api/v1/reports/${id}/html`, { headers: { Authorization: `Bearer ${state.key}` } });
  if (!res.ok) return toast("Could not open report", true);
  const url = URL.createObjectURL(new Blob([await res.text()], { type: "text/html" }));
  window.open(url, "_blank", "noopener");
}

// ---------------------------------------------------------------- tables

function table(rows, columns, { onRow, empty = "Nothing to show" } = {}) {
  if (!rows || !rows.length) return frag(`<div class="empty">${esc(empty)}</div>`);
  const head = columns.map(c => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => `<tr data-i="${i}" class="${onRow ? "clickable" : ""}">${columns.map(c => {
    const v = typeof c.value === "function" ? c.value(r) : r[c.key];
    const out = c.fmt ? c.fmt(v, r) : esc(v ?? "–");
    return `<td class="${c.num ? "num" : ""}">${out}</td>`;
  }).join("")}</tr>`).join("");
  const f = frag(`<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
  if (onRow) $$("tr[data-i]", f).forEach(tr => tr.addEventListener("click", () => onRow(rows[Number(tr.dataset.i)])));
  return f;
}

function panel(title, content, actions = []) {
  const p = frag(`<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2><div class="actions"></div></div><div class="body"></div></section>`);
  const acts = $(".actions", p);
  actions.forEach(a => {
    const b = document.createElement("button");
    b.className = a.kind || "secondary";
    b.textContent = a.label;
    b.addEventListener("click", a.onClick);
    acts.appendChild(b);
  });
  const body = $(".body", p);
  if (content instanceof Node) body.appendChild(content); else body.innerHTML = content ?? "";
  return p;
}

function cards(items) {
  return frag(`<div class="cards">${items.map(i => `<div class="card"><div class="label">${esc(i.label)}</div><div class="value">${i.value}</div>${i.sub ? `<div class="sub">${i.sub}</div>` : ""}</div>`).join("")}</div>`);
}

// ---------------------------------------------------------------- schema-driven forms

function resolve(schema, root) {
  if (schema && schema.$ref) return resolve(root.$defs?.[schema.$ref.split("/").pop()] || {}, root);
  return schema || {};
}

function fieldKind(prop, root) {
  let s = resolve(prop, root);
  let nullable = false;
  if (s.anyOf) {
    const options = s.anyOf.map(o => resolve(o, root)).filter(o => o.type !== "null");
    nullable = options.length < s.anyOf.length;
    const types = options.map(o => o.type);
    if (types.includes("number") && types.includes("string")) return { kind: "decimal", nullable, s: options[0] };
    if (types.includes("integer") && types.includes("string")) return { kind: "text", nullable, s: options[0] };
    s = options[0] || {};
  }
  if (s.enum) return { kind: "enum", options: s.enum, nullable, s };
  if (s.type === "boolean") return { kind: "bool", nullable, s };
  if (s.type === "integer") return { kind: "int", nullable, s };
  if (s.type === "number") return { kind: "decimal", nullable, s };
  if (s.type === "string" && s.format === "date") return { kind: "date", nullable, s };
  if (s.type === "string" && s.format === "date-time") return { kind: "datetime", nullable, s };
  if (s.type === "array") {
    const item = resolve(s.items, root);
    if (["string", "number", "integer"].includes(item.type) || item.anyOf) return { kind: "list", nullable, s };
    return { kind: "json", nullable, s };
  }
  if (s.type === "object" || s.properties || s.additionalProperties) return { kind: "json", nullable, s };
  return { kind: "text", nullable, s };
}

function buildForm(opName, { prefill = {}, hidden = [] } = {}) {
  const op = state.opsByName[opName];
  const schema = op.input_schema;
  const required = new Set(schema.required || []);
  const fields = [];
  const form = document.createElement("form");
  form.className = "form";
  form.noValidate = true;
  Object.entries(schema.properties || {}).forEach(([name, prop]) => {
    const kind = fieldKind(prop, schema);
    const title = prop.title || name;
    const desc = prop.description || kind.s.description || "";
    const value = prefill[name] !== undefined ? prefill[name] : prop.default;
    if (hidden.includes(name)) { fields.push({ name, kind, hiddenValue: prefill[name] }); return; }
    const wrap = document.createElement("label");
    let input;
    if (kind.kind === "bool") {
      wrap.className = "check";
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!value;
      wrap.append(input, document.createTextNode(title));
      if (desc) wrap.append(frag(`<span class="desc">${esc(desc)}</span>`));
    } else {
      wrap.append(frag(`<span>${esc(title)}${required.has(name) ? " *" : ""}</span>`));
      if (kind.kind === "enum") {
        input = document.createElement("select");
        input.innerHTML = (required.has(name) && value !== undefined ? "" : '<option value=""></option>') +
          kind.options.map(o => `<option ${String(o) === String(value) ? "selected" : ""}>${esc(o)}</option>`).join("");
      } else if (kind.kind === "json" || name === "content" || name === "expression" || name === "applies_when") {
        input = document.createElement("textarea");
        input.value = value === undefined || value === null ? "" : (kind.kind === "json" ? JSON.stringify(value, null, 2) : String(value));
        if (name === "content") {
          input.rows = 8;
          const file = document.createElement("input");
          file.type = "file";
          file.accept = ".csv,.txt,.tsv";
          file.addEventListener("change", async () => {
            if (file.files[0]) {
              input.value = await file.files[0].text();
              const src = form.querySelector('[data-name="source_name"]');
              if (src && !src.value) src.value = file.files[0].name;
            }
          });
          wrap.append(file);
        } else if (kind.kind !== "json") {
          input.rows = 2;
        }
      } else {
        input = document.createElement("input");
        input.type = kind.kind === "date" ? "date" : kind.kind === "int" ? "number" : kind.kind === "datetime" ? "datetime-local" : "text";
        if (kind.kind === "decimal") input.inputMode = "decimal";
        if (value !== undefined && value !== null) input.value = Array.isArray(value) ? value.join(", ") : value;
        if (kind.kind === "list") input.placeholder = "comma separated";
      }
      wrap.append(input);
      if (desc) wrap.append(frag(`<span class="desc">${esc(desc)}</span>`));
    }
    input.dataset.name = name;
    form.append(wrap);
    fields.push({ name, kind, input });
  });

  form.collect = () => {
    const out = {};
    for (const f of fields) {
      if (f.hiddenValue !== undefined) { out[f.name] = f.hiddenValue; continue; }
      if (!f.input) continue;
      const k = f.kind.kind;
      if (k === "bool") { if (f.input.checked || prefill[f.name] !== undefined) out[f.name] = f.input.checked; continue; }
      const v = f.input.value.trim();
      if (v === "") continue;
      if (k === "int") out[f.name] = parseInt(v, 10);
      else if (k === "json") {
        try { out[f.name] = JSON.parse(v); } catch (_) { throw new Error(`${f.name}: invalid JSON`); }
      } else if (k === "list") out[f.name] = v.split(",").map(x => x.trim()).filter(Boolean);
      else if (f.name === "period" && /^\d+$/.test(v)) out[f.name] = parseInt(v, 10);
      else out[f.name] = v;
    }
    return out;
  };
  return form;
}

function modal(title, content) {
  $("#modal-title").textContent = title;
  const body = $("#modal-body");
  body.replaceChildren(content);
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modal-body").replaceChildren(); }

function opForm(opName, { prefill = {}, hidden = [], title, onDone, submitLabel } = {}) {
  const op = state.opsByName[opName];
  if (!op) return toast(`Unknown operation ${opName}`, true);
  if (!op.allowed) return toast(`Your role cannot run ${opName} (needs ${op.permission})`, true);
  const container = document.createElement("div");
  container.append(frag(`<p class="muted">${esc(op.description || op.summary)}</p><p class="muted mono">${esc(op.name)} · ${esc(op.permission)}</p>`));
  const form = buildForm(opName, { prefill, hidden });
  const result = document.createElement("div");
  const actions = frag(`<div class="form-actions">${op.mutating ? '<button type="button" class="secondary" data-act="dry">Dry run</button>' : ""}<button type="submit" class="primary">${esc(submitLabel || (op.mutating ? "Save" : "Run"))}</button></div>`);
  form.append(actions);
  container.append(form, result);
  const run = async (dryRun) => {
    let params;
    try { params = form.collect(); } catch (e) { return toast(e.message, true); }
    $$("button", form).forEach(b => b.disabled = true);
    try {
      const body = await raw(opName, params, { dryRun });
      if (!body.ok) throw new OpFailure(body.error);
      if (dryRun) {
        result.replaceChildren(frag(`<h3>Dry run result (nothing was saved)</h3><pre>${esc(JSON.stringify(body.data, null, 2))}</pre>`));
      } else {
        closeModal();
        toast(`${op.summary}: done`);
        onDone && onDone(body.data);
      }
    } catch (e) {
      result.replaceChildren(frag(`<p class="error">${esc(describeError(e))}</p>`));
    } finally {
      $$("button", form).forEach(b => b.disabled = false);
    }
  };
  form.addEventListener("submit", e => { e.preventDefault(); run(false); });
  const dry = $('[data-act="dry"]', form);
  if (dry) dry.addEventListener("click", () => run(true));
  modal(title || op.summary, container);
}

function showJson(title, data) {
  modal(title, frag(`<pre>${esc(JSON.stringify(data, null, 2))}</pre>`));
}

// ---------------------------------------------------------------- pages

function setMain(...nodes) {
  const main = $("#main");
  main.replaceChildren(...nodes);
}

function loading() { setMain(frag('<p class="muted">Loading…</p>')); }

async function pageFacilities() {
  loading();
  const rows = await call("facility.list");
  const head = frag(`<div class="page-head"><div><h1>Facilities</h1><p class="muted">Warehouse facilities and trusts in this deployment</p></div><div class="actions"></div></div>`);
  const btn = document.createElement("button");
  btn.className = "primary";
  btn.textContent = "New facility";
  btn.onclick = () => opForm("facility.create", { onDone: d => location.hash = `#/facility/${d.code}/overview` });
  $(".actions", head).append(btn);
  setMain(head, panel("All facilities", table(rows, [
    { key: "code", label: "Code", fmt: v => `<strong>${esc(v)}</strong>` },
    { key: "name", label: "Name" },
    { key: "facility_type", label: "Type" },
    { key: "status", label: "Status", fmt: fmt.status },
    { key: "currency", label: "Ccy" },
    { key: "facility_limit", label: "Limit", num: true, fmt: fmt.whole },
    { key: "total_commitment", label: "Commitments", num: true, fmt: fmt.whole },
    { key: "total_outstanding", label: "Outstanding", num: true, fmt: fmt.whole },
    { key: "revolving_period_end", label: "Revolving to", fmt: fmt.date },
  ], { onRow: r => location.hash = `#/facility/${r.code}/overview`, empty: "No facilities yet. Create one, or run `securifi demo`." })));
}

const TABS = [
  ["overview", "Overview"], ["register", "Note register"], ["data", "Data"], ["compliance", "Eligibility & tests"],
  ["periods", "Periods & month end"], ["accounts", "Bank accounts"], ["waterfall", "Waterfall"], ["reports", "Reports"], ["setup", "Setup"],
];

async function pageFacility(code, tab = "overview") {
  loading();
  const overview = await call("facility.get", { facility_id: code });
  const f = overview.facility;
  const head = frag(`<div class="page-head"><div><p class="muted"><a href="#/facilities">Facilities</a> / ${esc(f.code)}</p>
    <h1>${esc(f.name)} ${fmt.status(f.status)}</h1><p class="muted">${esc(f.facility_type)} · ${esc(f.currency)} · Trustee: ${esc(f.trustee || "–")} · Servicer: ${esc(f.servicer || "–")}</p></div></div>`);
  const tabs = frag(`<div class="tabs">${TABS.map(([k, l]) => `<button data-tab="${k}" class="${k === tab ? "active" : ""}">${esc(l)}</button>`).join("")}</div>`);
  $$("button", tabs).forEach(b => b.onclick = () => location.hash = `#/facility/${f.code}/${b.dataset.tab}`);
  const body = document.createElement("div");
  setMain(head, tabs, body);
  const ctx = { f, overview, fid: f.id, reload: () => pageFacility(code, tab) };
  try {
    await (FACILITY_TABS[tab] || FACILITY_TABS.overview)(body, ctx);
  } catch (e) {
    body.replaceChildren(frag(`<p class="error">${esc(describeError(e))}</p>`));
  }
}

function btn(label, onClick, kind = "secondary") { return { label, onClick, kind }; }

const FACILITY_TABS = {
  async overview(body, { f, overview, fid }) {
    const classes = overview.note_classes;
    const commit = classes.reduce((a, c) => a + Number(c.commitment), 0);
    const out = classes.reduce((a, c) => a + Number(c.outstanding), 0);
    const lc = overview.latest_compliance;
    body.append(cards([
      { label: "Facility limit", value: fmt.whole(f.facility_limit) },
      { label: "Commitments", value: fmt.whole(commit) },
      { label: "Outstanding", value: fmt.whole(out), sub: commit ? fmt.pct(out / commit, 1) + " utilised" : "" },
      { label: "Adjusted eligible", value: lc ? fmt.whole(lc.summary.adjusted_eligible_balance) : "–", sub: lc ? `as at ${fmt.date(lc.as_of_date)}` : "no compliance run" },
      { label: "BB deficiency", value: lc ? fmt.whole(lc.summary.borrowing_base_deficiency) : "–", sub: lc && Number(lc.summary.borrowing_base_deficiency) > 0 ? '<span class="badge bad">Cure required</span>' : "" },
      { label: "Active events", value: String(overview.active_events.length), sub: overview.active_events.map(e => esc(e.code)).join(", ") },
    ]));
    body.append(panel("Note classes", table(classes, [
      { key: "name", label: "Class", fmt: v => `<strong>${esc(v)}</strong>` },
      { key: "rank", label: "Rank", num: true },
      { key: "commitment", label: "Commitment", num: true, fmt: fmt.whole },
      { key: "outstanding", label: "Outstanding", num: true, fmt: fmt.money },
      { label: "Coupon", value: c => c.rate_type === "fixed" ? `${fmt.pct(c.fixed_rate)} fixed` : `${esc(c.base_rate_index || "")} + ${fmt.num(c.margin_bps, 0)}bps`, fmt: v => v },
      { key: "advance_rate", label: "Advance rate", num: true, fmt: v => v == null ? "–" : fmt.pct(v, 1) },
      { key: "day_count", label: "Day count" },
    ], { empty: "No note classes" })));
    const grid = frag('<div class="grid two"></div>');
    const g = $(".grid", grid);
    const np = overview.next_period;
    g.append(panel("Next period", np ? `<dl class="kv"><dt>Period</dt><dd>${np.sequence} ${fmt.status(np.status)}</dd><dt>Collection period</dt><dd>${fmt.date(np.collection_start)} – ${fmt.date(np.collection_end)}</dd><dt>Interest period</dt><dd>${fmt.date(np.interest_start)} – ${fmt.date(np.interest_end)}</dd><dt>Determination</dt><dd>${fmt.date(np.determination_date)}</dd><dt>Payment date</dt><dd><strong>${fmt.date(np.payment_date)}</strong></dd></dl>` : '<p class="muted">No open periods</p>'));
    g.append(panel("Latest compliance", lc ? `<dl class="kv"><dt>As at</dt><dd>${fmt.date(lc.as_of_date)}</dd><dt>Eligible loans</dt><dd>${lc.summary.eligible_loans} of ${lc.summary.total_loans}</dd><dt>Eligible balance</dt><dd>${fmt.money(lc.summary.eligible_balance)}</dd><dt>Excess concentrations</dt><dd>${fmt.money(lc.summary.excess_concentration)}</dd><dt>Tests</dt><dd>${lc.summary.tests_passed} passed, ${lc.summary.tests_failed} breached</dd></dl>` : '<p class="muted">Run compliance from the Eligibility tab</p>'));
    body.append(grid);
    if (overview.accounts.length) body.append(panel("Bank accounts", table(overview.accounts, [
      { key: "code", label: "Code" }, { key: "name", label: "Name" }, { key: "account_type", label: "Type" }, { key: "bank_name", label: "Bank" },
    ])));
  },

  async register(body, { f, fid, overview, reload }) {
    const [pos, entries, holders] = await Promise.all([call("register.positions", { facility_id: fid }), call("register.entries", { facility_id: fid }), call("noteholder.list")]);
    const hname = Object.fromEntries(holders.map(h => [h.id, h.short_name]));
    const cname = Object.fromEntries(overview.note_classes.map(c => [c.id, c.name]));
    const pre = { prefill: { facility_id: fid }, hidden: ["facility_id"], onDone: reload };
    body.append(frag('<div class="actions" style="margin-bottom:12px"></div>'));
    const acts = body.lastElementChild;
    [["Record commitment", "register.commit"], ["Drawdown", "register.drawdown"], ["Repayment", "register.repay"], ["Transfer", "register.transfer"]].forEach(([l, op], i) => {
      const b = document.createElement("button"); b.className = i === 1 ? "primary" : "secondary"; b.textContent = l; b.onclick = () => opForm(op, pre); acts.append(b);
    });
    const nb = document.createElement("button"); nb.className = "secondary"; nb.textContent = "New noteholder"; nb.onclick = () => opForm("noteholder.create", { onDone: reload }); acts.append(nb);
    pos.classes.forEach(c => body.append(panel(`Class ${c.note_class} — outstanding ${fmt.money(c.outstanding)} of ${fmt.whole(c.class_commitment)}`, table(c.holders, [
      { key: "noteholder", label: "Noteholder" },
      { key: "commitment", label: "Commitment", num: true, fmt: fmt.money },
      { key: "outstanding", label: "Outstanding", num: true, fmt: fmt.money },
      { label: "Undrawn", num: true, value: h => Number(h.commitment) - Number(h.outstanding), fmt: fmt.money },
      { key: "share", label: "Share", num: true, fmt: v => fmt.pct(v, 2) },
    ], { empty: "No holders recorded" }))));
    body.append(panel("Register ledger", table(entries.slice().reverse().slice(0, 300), [
      { key: "effective_date", label: "Date", fmt: fmt.date },
      { key: "note_class_id", label: "Class", fmt: v => esc(cname[v] || v) },
      { key: "noteholder_id", label: "Noteholder", fmt: v => esc(hname[v] || v) },
      { key: "entry_type", label: "Type" },
      { key: "amount", label: "Amount", num: true, fmt: fmt.money },
      { key: "reference", label: "Reference" },
      { key: "notes", label: "Notes" },
    ], { empty: "No register entries" })));
  },

  async data(body, { fid, reload }) {
    const [batches, mappings] = await Promise.all([call("data.batches", { facility_id: fid }), call("mapping.list", { facility_id: fid })]);
    const pre = { prefill: { facility_id: fid }, hidden: ["facility_id"], onDone: reload };
    body.append(panel("Ingestion batches", table(batches, [
      { key: "dataset", label: "Dataset" }, { key: "as_of_date", label: "As at", fmt: fmt.date }, { key: "source_name", label: "Source" },
      { key: "accepted_count", label: "Accepted", num: true }, { key: "rejected_count", label: "Rejected", num: true, fmt: v => v ? `<span class="badge bad">${v}</span>` : "0" },
      { key: "issue_count", label: "Issues", num: true }, { key: "status", label: "Status", fmt: fmt.status }, { key: "created_at", label: "Loaded", fmt: fmt.time },
    ], { onRow: async r => { const b = await call("data.batch_get", { batch_id: r.id }); modal(`Batch ${b.id}`, panel("Data quality issues", table(b.issues, [{ key: "row", label: "Row", num: true }, { key: "field", label: "Field" }, { key: "message", label: "Issue" }, { key: "severity", label: "Severity", fmt: fmt.status }], { empty: "No issues" }))); }, empty: "No data loaded" }),
      [btn("Load loan tape", () => opForm("data.ingest_loan_tape", pre), "primary"), btn("Load collections", () => opForm("data.ingest_collections", pre)), btn("New mapping", () => opForm("mapping.create", { prefill: { facility_id: fid }, onDone: reload }))]));
    body.append(panel("Data mappings", table(mappings, [{ key: "name", label: "Name" }, { key: "dataset", label: "Dataset" }, { key: "facility_id", label: "Scope", fmt: v => v ? "This facility" : "Shared" }], { onRow: m => showJson(m.name, m.config), empty: "No mappings; columns are auto-matched to canonical names" })));

    const explorer = frag(`<div><div class="filters"><label style="flex:1">Filter expression<input id="loan-filter" placeholder="property_type == 'office' and ltv > 0.6"></label><button class="primary" id="loan-run">Query</button></div><div id="loan-out"></div></div>`);
    body.append(panel("Loan explorer", explorer));
    const runQuery = async () => {
      const out = $("#loan-out");
      try {
        const res = await call("data.loans", { facility_id: fid, filter: $("#loan-filter").value || null, limit: 500 });
        out.replaceChildren(frag(`<p class="muted">${res.count} loans · tape as at ${fmt.date(res.as_of_date)}</p>`), table(res.loans, [
          { key: "loan_id", label: "Loan" }, { key: "borrower_group", label: "Sponsor" }, { key: "property_type", label: "Property" },
          { key: "property_state", label: "State" }, { key: "loan_stage", label: "Stage" }, { key: "current_balance", label: "Balance", num: true, fmt: fmt.money },
          { key: "ltv", label: "LTV", num: true, fmt: v => fmt.pct(v, 1) }, { key: "icr", label: "ICR", num: true, fmt: v => fmt.num(v) },
          { key: "remaining_term_months", label: "Term (m)", num: true, fmt: v => fmt.num(v, 1) }, { key: "arrears_days", label: "Arrears", num: true },
        ], { onRow: l => showJson(`Loan ${l.loan_id}`, l) }));
      } catch (e) { out.replaceChildren(frag(`<p class="error">${esc(describeError(e))}</p>`)); }
    };
    $("#loan-run").onclick = runQuery;
    $("#loan-filter").addEventListener("keydown", e => { if (e.key === "Enter") runQuery(); });
    if (batches.some(b => b.dataset === "loan_tape")) runQuery();
  },

  async compliance(body, { fid, reload }) {
    const [runs, criteria, tests] = await Promise.all([call("compliance.list", { facility_id: fid, limit: 10 }), call("criterion.list", { facility_id: fid }), call("test.list", { facility_id: fid })]);
    const pre = { prefill: { facility_id: fid }, hidden: ["facility_id"], onDone: reload };
    body.append(frag('<div class="actions" style="margin-bottom:12px"></div>'));
    const acts = body.lastElementChild;
    [["Run compliance", "compliance.run", "primary"], ["Add criterion", "criterion.create"], ["Add portfolio test", "test.create"], ["Try an expression", "rules.try_expression"], ["Borrowing base certificate", "report.borrowing_base_certificate"]].forEach(([l, op, k]) => {
      const b = document.createElement("button"); b.className = k || "secondary"; b.textContent = l;
      b.onclick = () => opForm(op, { ...pre, onDone: op === "rules.try_expression" ? d => showJson("Expression preview", d) : op.startsWith("report") ? d => openReport(d.id) : reload });
      acts.append(b);
    });
    if (runs.length) {
      const run = await call("compliance.get", { run_id: runs[0].id });
      const s = run.summary;
      body.append(frag(`<h3>Latest run · as at ${fmt.date(run.as_of_date)} · ${esc(run.purpose)}</h3>`));
      body.append(cards([
        { label: "Pool balance", value: fmt.whole(s.total_balance), sub: `${s.total_loans} loans` },
        { label: "Eligible", value: fmt.whole(s.eligible_balance), sub: `${s.eligible_loans} loans` },
        { label: "Excess concentrations", value: fmt.whole(s.excess_concentration) },
        { label: "Adjusted eligible", value: fmt.whole(s.adjusted_eligible_balance) },
        { label: "Tests", value: `${s.tests_passed} / ${s.tests_passed + s.tests_failed}`, sub: s.tests_failed ? `<span class="badge bad">${s.tests_failed} breached</span>` : '<span class="badge ok">All pass</span>' },
        { label: "BB deficiency", value: fmt.whole(s.borrowing_base_deficiency) },
      ]));
      if (run.borrowing_base) body.append(panel("Borrowing base", table(run.borrowing_base.classes, [
        { key: "class", label: "Class" }, { key: "advance_rate", label: "Advance rate", num: true, fmt: v => v == null ? "–" : fmt.pct(v, 1) },
        { key: "commitment", label: "Commitment", num: true, fmt: fmt.whole }, { key: "max_funding", label: "Max funding", num: true, fmt: fmt.money },
        { key: "outstanding", label: "Outstanding", num: true, fmt: fmt.money }, { key: "available_to_draw", label: "Available", num: true, fmt: fmt.money },
        { key: "deficiency", label: "Deficiency", num: true, fmt: v => Number(v) > 0 ? `<span class="badge bad">${fmt.money(v)}</span>` : fmt.money(v) },
      ])));
      const isRatio = t => ["concentration", "filtered_concentration", "top_n_concentration"].includes(t.test_type) || (t.test_type === "largest_exposure" && t.limit && Number(t.limit.max) <= 1);
      body.append(panel("Portfolio parameters", table(run.test_results, [
        { key: "code", label: "Code" }, { key: "name", label: "Test" }, { key: "treatment", label: "Treatment" },
        { label: "Value", num: true, value: t => t, fmt: t => t.error ? `<span class="badge bad">error</span>` : isRatio(t) || (t.test_type === "weighted_average" && Number(t.value) < 1.5 && t.details?.field === "ltv") ? fmt.pct(t.value) : fmt.num(t.value, 4) },
        { label: "Limit", value: t => t, fmt: t => { const l = t.limit || {}; const show = v => isRatio(t) || t.details?.field === "ltv" ? fmt.pct(v) : fmt.num(v, 4); return [l.min != null ? `≥ ${show(l.min)}` : "", l.max != null ? `≤ ${show(l.max)}` : ""].filter(Boolean).join(" ") || "–"; } },
        { key: "excess_amount", label: "Excess", num: true, fmt: fmt.money },
        { key: "passed", label: "Result", fmt: fmt.pass },
        { key: "clause_ref", label: "Clause" },
      ], { onRow: t => showJson(`${t.code} ${t.name}`, t) })));
      const inel = await call("compliance.loan_results", { run_id: run.id, only_ineligible: true });
      body.append(panel(`Ineligible loans (${inel.length})`, table(inel, [
        { key: "loan_id", label: "Loan" }, { key: "balance", label: "Balance", num: true, fmt: fmt.money },
        { key: "failures", label: "Failed criteria", fmt: v => v.map(x => `<div><strong>${esc(x.code)}</strong> ${esc(x.name || "")} <span class="muted">— ${esc(x.reason)}</span></div>`).join("") },
      ], { empty: "All loans eligible" })));
    } else {
      body.append(panel("Compliance", '<p class="muted">No compliance runs yet. Load a loan tape and run compliance.</p>'));
    }
    body.append(panel("Eligibility criteria", table(criteria, [
      { key: "code", label: "Code" }, { key: "name", label: "Criterion" }, { key: "expression", label: "Expression", fmt: fmt.json },
      { key: "applies_when", label: "Applies when", fmt: v => v ? fmt.json(v) : "All loans" }, { key: "severity", label: "Severity" },
      { key: "clause_ref", label: "Clause" }, { key: "version", label: "v", num: true },
    ], { onRow: c => opForm("criterion.update", { prefill: { facility_id: fid, code: c.code, expression: c.expression, applies_when: c.applies_when, name: c.name }, hidden: ["facility_id"], onDone: reload }), empty: "No criteria" })));
    body.append(panel("Portfolio tests", table(tests, [
      { key: "code", label: "Code" }, { key: "name", label: "Test" }, { key: "test_type", label: "Type" }, { key: "config", label: "Config", fmt: fmt.json },
      { key: "treatment", label: "Treatment" }, { key: "event_code", label: "Event" }, { key: "clause_ref", label: "Clause" },
    ], { onRow: t => opForm("test.update", { prefill: { facility_id: fid, code: t.code, config: t.config }, hidden: ["facility_id"], onDone: reload }), empty: "No portfolio tests" })));
  },

  async periods(body, ctx) {
    const { fid, f } = ctx;
    const periods = await call("period.list", { facility_id: fid });
    const current = periods.find(p => p.status !== "finalised") || periods[periods.length - 1];
    const selected = Number(ctx.period || (current && current.sequence) || 0);
    body.append(panel("Periods", table(periods, [
      { key: "sequence", label: "#", num: true }, { label: "Collection period", value: p => `${p.collection_start} – ${p.collection_end}` },
      { label: "Interest period", value: p => `${p.interest_start} – ${p.interest_end}` }, { key: "determination_date", label: "Determination", fmt: fmt.date },
      { key: "payment_date", label: "Payment", fmt: v => `<strong>${fmt.date(v)}</strong>` }, { key: "status", label: "Status", fmt: fmt.status },
    ], { onRow: p => { ctx.period = p.sequence; body.replaceChildren(); FACILITY_TABS.periods(body, ctx); }, empty: "No periods; generate a schedule in Setup" })));
    if (!selected) return;
    const period = periods.find(p => p.sequence === selected);
    const runs = await call("period_run.list", { facility_id: fid, period: selected });
    const reloadHere = () => { body.replaceChildren(); FACILITY_TABS.periods(body, ctx); };
    const pre = { prefill: { facility_id: fid, period: selected }, hidden: ["facility_id"], onDone: reloadHere };
    const live = runs.find(r => ["calculated", "approved", "finalised"].includes(r.status));
    const actions = [];
    if (period.status !== "finalised") {
      actions.push(btn("Load collections", () => opForm("data.ingest_collections", pre)));
      actions.push(btn(live ? "Recalculate" : "Calculate", () => opForm("period_run.calculate", pre), "primary"));
    }
    if (live && live.status === "calculated") {
      actions.push(btn("Approve", () => opForm("period_run.approve", { prefill: { run_id: live.id }, hidden: ["run_id"], onDone: reloadHere }), "primary"));
      actions.push(btn("Reject", () => opForm("period_run.reject", { prefill: { run_id: live.id }, hidden: ["run_id"], onDone: reloadHere })));
    }
    if (live && live.status === "approved") actions.push(btn("Finalise", () => opForm("period_run.finalise", { prefill: { run_id: live.id }, hidden: ["run_id"], onDone: reloadHere }), "danger"));
    if (live) actions.push(btn("Investor report", () => opForm("report.investor", { prefill: { facility_id: fid, period: selected }, hidden: ["facility_id"], onDone: d => openReport(d.id) })));
    body.append(panel(`Period ${selected} · payment ${period.payment_date}`, table(runs, [
      { key: "run_number", label: "Run", num: true }, { key: "status", label: "Status", fmt: fmt.status }, { key: "prepared_at", label: "Prepared", fmt: fmt.time },
      { key: "approved_by", label: "Approved by" }, { key: "finalised_at", label: "Finalised", fmt: fmt.time },
      { key: "warnings", label: "Warnings", fmt: v => v.length ? `<span class="badge warn">${v.length}</span>` : "" },
    ], { empty: "Not calculated yet" }), actions));
    if (!live) return;
    const run = await call("period_run.get", { run_id: live.id });
    const o = run.outputs;
    if (o.warnings.length) body.append(panel("Warnings", `<ul>${o.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`));
    body.append(cards(Object.entries(o.available_funds).map(([k, v]) => ({ label: `Available ${k}`, value: fmt.money(v) }))));
    body.append(panel("Note interest", table(o.interest, [
      { key: "class", label: "Class" }, { key: "days", label: "Days", num: true }, { key: "average_balance", label: "Avg balance", num: true, fmt: fmt.money },
      { key: "base_rate", label: "Base", num: true, fmt: v => fmt.pct(v, 4) }, { key: "margin", label: "Margin", num: true, fmt: v => fmt.pct(v, 2) },
      { key: "all_in_rate", label: "All-in", num: true, fmt: v => fmt.pct(v, 4) }, { key: "interest", label: "Interest", num: true, fmt: fmt.money },
      { key: "carried_forward", label: "Carried fwd", num: true, fmt: fmt.money }, { key: "total_due", label: "Total due", num: true, fmt: fmt.money },
    ])));
    body.append(panel("Fees", table(o.fees, [
      { key: "code", label: "Code" }, { key: "name", label: "Fee" }, { key: "payee", label: "Payee" }, { key: "basis", label: "Basis" },
      { key: "net", label: "Net", num: true, fmt: fmt.money }, { key: "tax", label: "GST/tax", num: true, fmt: fmt.money },
      { key: "carried_forward", label: "Carried fwd", num: true, fmt: fmt.money }, { key: "total_due", label: "Total due", num: true, fmt: fmt.money },
    ], { empty: "No fees" })));
    body.append(panel("Priority of payments", table(o.waterfall.lines, [
      { key: "waterfall", label: "Waterfall" }, { key: "step", label: "#", num: true }, { key: "label", label: "Item" },
      { key: "available_before", label: "Available", num: true, fmt: fmt.money }, { key: "due", label: "Due", num: true, fmt: fmt.money },
      { key: "paid", label: "Paid", num: true, fmt: v => `<strong>${fmt.money(v)}</strong>` },
      { key: "shortfall", label: "Shortfall", num: true, fmt: v => Number(v) > 0 ? `<span class="badge bad">${fmt.money(v)}</span>` : "–" },
      { key: "skipped", label: "Note", fmt: v => v ? `<span class="muted">${esc(v)}</span>` : "" },
    ])));
    body.append(panel("Noteholder distributions", table(o.holder_payments, [
      { key: "note_class", label: "Class" }, { key: "noteholder", label: "Noteholder" }, { key: "interest_due", label: "Interest due", num: true, fmt: fmt.money },
      { key: "interest_paid", label: "Interest paid", num: true, fmt: fmt.money }, { key: "principal_paid", label: "Principal paid", num: true, fmt: fmt.money },
    ])));
    body.append(panel("Note balances", table(o.note_balances, [
      { key: "note_class", label: "Class" }, { key: "opening", label: "Before payment", num: true, fmt: fmt.money },
      { key: "principal_paid", label: "Principal paid", num: true, fmt: fmt.money }, { key: "closing", label: "After payment", num: true, fmt: fmt.money },
    ])));
  },

  async accounts(body, { fid, reload }) {
    const status = await call("reconciliation.status", { facility_id: fid });
    const pre = { prefill: { facility_id: fid }, hidden: ["facility_id"], onDone: reload };
    body.append(panel("Trust bank accounts", table(status, [
      { key: "account", label: "Code" }, { key: "name", label: "Account" }, { key: "account_type", label: "Type" },
      { key: "bank_balance", label: "Bank balance", num: true, fmt: fmt.money }, { key: "last_statement_balance", label: "Statement balance", num: true, fmt: fmt.money },
      { key: "statement_difference", label: "Difference", num: true, fmt: v => v != null && Number(v) !== 0 ? `<span class="badge bad">${fmt.money(v)}</span>` : fmt.money(v) },
      { key: "reconciled", label: "Reconciled", fmt: v => v ? '<span class="badge ok">Yes</span>' : '<span class="badge warn">Open items</span>' },
    ], { empty: "No accounts" }), [btn("Import statement", () => opForm("bank.import_statement", pre), "primary"), btn("Auto-match", () => opForm("reconciliation.auto_match", { ...pre, onDone: d => { toast(`${d.count} items matched`); reload(); } })), btn("Expected cash flow", () => opForm("cashflow.add_expected", pre)), btn("Add account", () => opForm("account.create", pre))]));
    status.forEach(a => {
      if (a.reconciled) return;
      const g = frag('<div class="grid two"></div>');
      $(".grid", g).append(
        panel(`${a.account}: expected, not yet seen (${fmt.money(a.unmatched_expected_total)})`, table(a.unmatched_expected, [
          { key: "expected_date", label: "Date", fmt: fmt.date }, { key: "description", label: "Description" }, { key: "amount", label: "Amount", num: true, fmt: fmt.money },
        ], { empty: "None", onRow: e => opForm("reconciliation.match", { prefill: { expected_id: e.id }, onDone: reload }) })),
        panel(`${a.account}: bank lines not matched (${fmt.money(a.unmatched_transactions_total)})`, table(a.unmatched_transactions, [
          { key: "transaction_date", label: "Date", fmt: fmt.date }, { key: "description", label: "Narrative" }, { key: "reference", label: "Ref" }, { key: "amount", label: "Amount", num: true, fmt: fmt.money },
          { key: "id", label: "Id", fmt: v => `<span class="mono muted">${esc(v)}</span>` },
        ], { empty: "None", onRow: t => opForm("reconciliation.ignore", { prefill: { transaction_id: t.id }, onDone: reload }) })),
      );
      body.append(g);
    });
  },

  async waterfall(body, { fid, reload }) {
    let wf = null;
    try { wf = await call("waterfall.get", { facility_id: fid }); } catch (e) { if (e.error?.code !== "not_found") throw e; }
    const edit = () => {
      const form = frag(`<form class="form"><label>Definition (JSON)<textarea id="wf-def" rows="22"></textarea></label><label>Available funds (JSON)<textarea id="wf-af" rows="6"></textarea></label><label>Notes<input id="wf-notes"></label><p class="error" id="wf-err"></p><div class="form-actions"><button class="primary" type="submit">Publish new version</button></div></form>`);
      $("#wf-def", form).value = JSON.stringify(wf ? wf.definition : { order: ["income", "principal"], waterfalls: { income: [], principal: [] } }, null, 2);
      $("#wf-af", form).value = JSON.stringify(wf ? wf.available_funds : null, null, 2);
      $("form", form).addEventListener("submit", async e => {
        e.preventDefault();
        try {
          const af = JSON.parse($("#wf-af").value || "null");
          await call("waterfall.set", { facility_id: fid, definition: JSON.parse($("#wf-def").value), available_funds: af, notes: $("#wf-notes").value || null });
          closeModal(); toast("Waterfall published"); reload();
        } catch (err) { $("#wf-err").textContent = err instanceof OpFailure ? describeError(err) : err.message; }
      });
      modal("Edit waterfall", form);
    };
    if (!wf) { body.append(panel("Waterfall", '<p class="muted">No priority of payments defined.</p>', [btn("Define waterfall", edit, "primary")])); return; }
    body.append(frag(`<p class="muted">Version ${wf.version} · published ${fmt.time(wf.created_at)} ${wf.notes ? "· " + esc(wf.notes) : ""}</p>`));
    const order = wf.definition.order || Object.keys(wf.definition.waterfalls);
    order.forEach((name, i) => body.append(panel(`${name[0].toUpperCase() + name.slice(1)} waterfall`, table(wf.definition.waterfalls[name].map((s, n) => ({ n: n + 1, ...s })), [
      { key: "n", label: "#", num: true }, { key: "type", label: "Step" },
      { label: "Applies to", value: s => s.fee || (s.classes || [s.class]).filter(Boolean).join(", ") || s.account || s.to || s.payee || "" },
      { key: "label", label: "Label" }, { label: "Detail", value: s => s.target || s.limit || s.amount || s.mode || "", fmt: v => v ? fmt.json(v) : "" },
      { key: "when", label: "Condition", fmt: v => v ? fmt.json(v) : "Always" }, { key: "clause_ref", label: "Clause" },
    ]), i === 0 ? [btn("Edit / publish new version", edit, "primary")] : [])));
    body.append(panel("Available funds mapping", `<pre>${esc(JSON.stringify(wf.available_funds, null, 2))}</pre>`));
  },

  async reports(body, { fid, reload }) {
    const reports = await call("report.list", { facility_id: fid });
    body.append(panel("Reports", table(reports, [
      { key: "title", label: "Report" }, { key: "report_type", label: "Type" }, { key: "is_final", label: "Final", fmt: fmt.bool },
      { key: "generated_at", label: "Generated", fmt: fmt.time }, { key: "content_hash", label: "SHA-256", fmt: v => `<span class="mono muted">${esc(String(v).slice(0, 12))}…</span>` },
    ], { onRow: r => openReport(r.id), empty: "No reports generated" }), [
      btn("Investor report", () => opForm("report.investor", { prefill: { facility_id: fid }, hidden: ["facility_id"], onDone: d => { reload(); openReport(d.id); } }), "primary"),
      btn("Borrowing base certificate", () => opForm("report.borrowing_base_certificate", { prefill: { facility_id: fid }, hidden: ["facility_id"], onDone: d => { reload(); openReport(d.id); } })),
    ]));
  },

  async setup(body, { f, fid, overview, reload }) {
    const pre = { prefill: { facility_id: fid }, hidden: ["facility_id"], onDone: reload };
    const events = await call("event.list", { facility_id: fid });
    body.append(panel("Facility", `<dl class="kv">${["trust_name", "trustee", "security_trustee", "trust_manager", "originator", "servicer", "closing_date", "revolving_period_end", "final_maturity_date", "day_count", "business_day_convention"].map(k => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(f[k] ?? "–")}</dd>`).join("")}<dt>settings</dt><dd>${fmt.json(f.settings)}</dd><dt>variables</dt><dd>${fmt.json(f.variables)}</dd></dl>`,
      [btn("Edit facility", () => opForm("facility.update", pre)), btn("Add note class", () => opForm("note_class.create", pre)), btn("Generate periods", () => opForm("period.generate", pre))]));
    body.append(panel("Fees", table(overview.fees, [
      { key: "code", label: "Code" }, { key: "name", label: "Fee" }, { key: "payee", label: "Payee" }, { key: "calculation", label: "Calculation" },
      { key: "basis", label: "Basis" }, { key: "rate_bps", label: "Rate (bps)", num: true }, { key: "amount", label: "Amount", num: true, fmt: fmt.money },
      { key: "tax_rate", label: "Tax", num: true, fmt: v => v == null ? "–" : fmt.pct(v, 0) }, { key: "active", label: "Active", fmt: fmt.bool },
    ], { onRow: x => opForm("fee.update", { prefill: { facility_id: fid, code: x.code }, hidden: ["facility_id"], onDone: reload }), empty: "No fees" }), [btn("Add fee", () => opForm("fee.create", pre))]));
    body.append(panel("Trigger events", table(events, [
      { key: "code", label: "Code" }, { key: "name", label: "Event" }, { key: "status", label: "Status", fmt: fmt.status },
      { key: "declared_on", label: "Declared", fmt: fmt.date }, { key: "cleared_on", label: "Cleared", fmt: fmt.date }, { key: "source", label: "Source" },
    ], { onRow: e => e.status === "active" && opForm("event.clear", { prefill: { facility_id: fid, code: e.code }, hidden: ["facility_id"], onDone: reload }), empty: "No events" }), [btn("Declare event", () => opForm("event.declare", pre))]));
    body.append(panel("Legal documents", table(overview.documents, [
      { key: "title", label: "Document" }, { key: "doc_type", label: "Type" }, { key: "version", label: "Version" }, { key: "executed_date", label: "Executed", fmt: fmt.date }, { key: "id", label: "Id", fmt: v => `<span class="mono muted">${esc(v)}</span>` },
    ], { empty: "No documents" }), [btn("Add document", () => opForm("document.add", pre))]));
    body.append(panel("Base rate fixings", table(await call("rates.list", { limit: 24 }), [
      { key: "index_name", label: "Index" }, { key: "fixing_date", label: "Date", fmt: fmt.date }, { key: "rate", label: "Rate", num: true, fmt: v => fmt.pct(v, 4) }, { key: "source", label: "Source" },
    ], { empty: "No fixings" }), [btn("Record fixing", () => opForm("rates.set_fixing", { onDone: reload }))]));
  },
};

async function pageConsole(selected) {
  const ops = state.ops;
  const wrap = frag(`<div class="page-head"><div><h1>Operations console</h1><p class="muted">Every operation in the registry — the same set exposed over the API, MCP and CLI.</p></div></div>
    <div class="split"><section class="panel"><input id="op-search" placeholder="Search operations"><div class="op-list" id="op-list" style="margin-top:10px"></div></section><section class="panel" id="op-detail"><p class="muted">Select an operation</p></section></div>`);
  setMain(wrap);
  const list = $("#op-list");
  const render = (q = "") => {
    list.replaceChildren(...ops.filter(o => !q || (o.name + o.summary).toLowerCase().includes(q.toLowerCase())).map(o => {
      const b = document.createElement("button");
      b.className = o.name === selected ? "active" : "";
      b.innerHTML = `<span class="op-name">${esc(o.name)}</span> ${o.mutating ? '<span class="badge warn">write</span>' : ""} ${o.allowed ? "" : '<span class="badge muted">no access</span>'}<span class="op-sum">${esc(o.summary)}</span>`;
      b.onclick = () => { location.hash = `#/console/${o.name}`; };
      return b;
    }));
  };
  render();
  $("#op-search").addEventListener("input", e => render(e.target.value));
  if (!selected || !state.opsByName[selected]) return;
  const op = state.opsByName[selected];
  const detail = $("#op-detail");
  detail.replaceChildren(frag(`<h2 class="mono">${esc(op.name)}</h2><p>${esc(op.summary)}</p>${op.description ? `<p class="muted" style="white-space:pre-wrap">${esc(op.description)}</p>` : ""}
    <p><span class="badge">${esc(op.permission)}</span> ${op.mutating ? '<span class="badge warn">changes data · supports dry run</span>' : '<span class="badge ok">read only</span>'} ${op.allowed ? "" : '<span class="badge bad">your role cannot run this</span>'}</p>
    <p class="muted">REST: <code>POST /api/v1/ops/${esc(op.name)}</code> · MCP tool: <code>${esc(op.name.replace(/\./g, "_"))}</code> · CLI: <code>securifi call ${esc(op.name)}</code></p>`));
  const form = buildForm(op.name);
  form.append(frag(`<div class="form-actions">${op.mutating ? '<button type="button" class="secondary" data-act="dry">Dry run</button>' : ""}<button class="primary" type="submit" ${op.allowed ? "" : "disabled"}>Execute</button></div>`));
  const out = document.createElement("div");
  detail.append(form, out);
  const exec = async dryRun => {
    let params;
    try { params = form.collect(); } catch (e) { return toast(e.message, true); }
    const body = await raw(op.name, params, { dryRun });
    out.replaceChildren(frag(`<h3>${body.ok ? (body.dry_run ? "Dry run result" : "Result") : "Error"} <span class="muted mono">audit ${esc(body.audit_id || "")}</span></h3><pre>${esc(JSON.stringify(body.ok ? body.data : body.error, null, 2))}</pre>`));
  };
  form.addEventListener("submit", e => { e.preventDefault(); exec(false); });
  const dry = $('[data-act="dry"]', form);
  if (dry) dry.onclick = () => exec(true);
  detail.append(frag(`<h3>Input schema</h3><pre>${esc(JSON.stringify(op.input_schema, null, 2))}</pre>`));
}

async function pageAudit() {
  const head = frag(`<div class="page-head"><div><h1>Audit log</h1><p class="muted">Every operation from every entry point, hash-chained and tamper evident.</p></div><div class="actions"><button class="secondary" id="verify">Verify chain</button></div></div>
    <div class="filters"><label>Operation<input id="f-op" placeholder="e.g. period_run.approve"></label>
    <label>Channel<select id="f-ch"><option value="">All</option><option>ui</option><option>api</option><option>mcp</option><option>cli</option><option>system</option></select></label>
    <label>Outcome<select id="f-out"><option value="">All</option><option>success</option><option>dry_run</option><option>denied</option><option>invalid</option><option>failed</option></select></label>
    <label class="check"><input type="checkbox" id="f-mut"> Changes only</label><button class="primary" id="f-go">Apply</button></div><div id="audit-out"></div>`);
  setMain(head);
  const load = async () => {
    const out = $("#audit-out");
    try {
      const rows = await call("audit.query", { operation: $("#f-op").value || null, channel: $("#f-ch").value || null, outcome: $("#f-out").value || null, mutating_only: $("#f-mut").checked, limit: 300 });
      out.replaceChildren(panel(`${rows.length} events`, table(rows, [
        { key: "seq", label: "#", num: true }, { key: "occurred_at", label: "Time (UTC)", fmt: fmt.time }, { key: "actor_label", label: "Actor" },
        { key: "channel", label: "Channel", fmt: v => `<span class="badge">${esc(v)}</span>` }, { key: "operation", label: "Operation", fmt: v => `<span class="mono">${esc(v)}</span>` },
        { key: "outcome", label: "Outcome", fmt: fmt.status }, { key: "error", label: "Detail", fmt: v => v ? `<span class="muted">${esc(String(v).slice(0, 120))}</span>` : "" },
      ], { onRow: e => showJson(`Audit event ${e.seq}`, e) })));
    } catch (e) { out.replaceChildren(frag(`<p class="error">${esc(describeError(e))}</p>`)); }
  };
  $("#f-go").onclick = load;
  $("#verify").onclick = async () => {
    try {
      const r = await call("audit.verify");
      toast(r.valid ? `Audit chain intact (${r.events_checked} events)` : `Audit chain BROKEN at event ${r.first_invalid_seq ?? "?"}`, !r.valid);
    } catch (e) { toast(describeError(e), true); }
  };
  load();
}

async function pageAdmin() {
  loading();
  let users = [], keys = [];
  try { [users, keys] = await Promise.all([call("admin.user_list"), call("admin.apikey_list")]); } catch (e) {
    setMain(frag(`<h1>Users &amp; keys</h1><p class="error">${esc(describeError(e))}</p>`)); return;
  }
  const roles = await call("admin.roles");
  const byUser = Object.fromEntries(users.map(u => [u.id, u.email]));
  setMain(frag('<div class="page-head"><div><h1>Users &amp; keys</h1><p class="muted">People, AI agents and integrations. Every key is bound to a role; keys can be narrowed further with scopes.</p></div></div>'),
    panel("Users", table(users, [
      { key: "email", label: "Email" }, { key: "name", label: "Name" }, { key: "role", label: "Role", fmt: v => `<span class="badge">${esc(v)}</span>` },
      { key: "is_service_account", label: "Agent / service", fmt: fmt.bool }, { key: "facility_scope", label: "Facilities", fmt: v => v ? esc(v.join(", ")) : "All" },
      { key: "active", label: "Active", fmt: fmt.bool },
    ], { onRow: u => opForm("admin.apikey_create", { prefill: { user_id: u.id }, hidden: ["user_id"], title: `Issue API key for ${u.email}`, submitLabel: "Issue key", onDone: d => { showJson("New API key — copy it now, it will not be shown again", d); } }) }),
      [btn("New user / agent", () => opForm("admin.user_create", { onDone: pageAdmin }), "primary")]),
    panel("API keys", table(keys, [
      { key: "name", label: "Name" }, { key: "user_id", label: "User", fmt: v => esc(byUser[v] || v) }, { key: "prefix", label: "Prefix", fmt: v => `<span class="mono">${esc(v)}…</span>` },
      { key: "scopes", label: "Scopes", fmt: v => v ? esc(v.join(", ")) : "Role default" }, { key: "last_used_at", label: "Last used", fmt: fmt.time },
      { key: "revoked_at", label: "Status", fmt: v => v ? '<span class="badge bad">revoked</span>' : '<span class="badge ok">active</span>' },
    ], { onRow: k => !k.revoked_at && opForm("admin.apikey_revoke", { prefill: { key_id: k.id }, hidden: ["key_id"], title: `Revoke key "${k.name}"`, submitLabel: "Revoke", onDone: pageAdmin }) })),
    panel("Roles", table(roles.roles, [{ key: "role", label: "Role" }, { key: "description", label: "Description" }, { key: "permissions", label: "Permissions", fmt: v => esc(v.join(", ")) }])));
}

// ---------------------------------------------------------------- shell & routing

async function route() {
  if (!state.me) return;
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  $$("#nav a").forEach(a => a.classList.toggle("active", a.dataset.nav === (parts[0] === "facility" ? "facilities" : parts[0] || "facilities")));
  try {
    if (parts[0] === "facility" && parts[1]) await pageFacility(decodeURIComponent(parts[1]), parts[2]);
    else if (parts[0] === "console") await pageConsole(parts[1]);
    else if (parts[0] === "audit") await pageAudit();
    else if (parts[0] === "admin") await pageAdmin();
    else await pageFacilities();
  } catch (e) {
    setMain(frag(`<p class="error">${esc(describeError(e))}</p>`));
  }
}

async function login(key) {
  state.key = key;
  const res = await fetch("/api/v1/operations", { headers: { Authorization: `Bearer ${key}` } });
  if (!res.ok) throw new Error("Invalid API key");
  state.ops = await res.json();
  state.opsByName = Object.fromEntries(state.ops.map(o => [o.name, o]));
  state.me = await call("auth.whoami");
  state.info = await (await fetch("/api/v1/info")).json();
  storage("set", "securifi.key", key);
  $("#who-label").textContent = state.me.label;
  $("#who-role").textContent = state.me.role;
  $("#org").textContent = state.info.organisation || "";
  $("#login").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  route();
}

function logout() {
  storage("del", "securifi.key");
  state.key = null; state.me = null;
  $("#shell").classList.add("hidden");
  $("#login").classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", () => {
  $("#login-form").addEventListener("submit", async e => {
    e.preventDefault();
    $("#login-error").textContent = "";
    try { await login($("#login-key").value.trim()); } catch (err) { $("#login-error").textContent = err.message; }
  });
  $("#logout").onclick = logout;
  $("#modal-close").onclick = closeModal;
  $("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });
  window.addEventListener("hashchange", route);
  const saved = storage("get", "securifi.key");
  if (saved) login(saved).catch(logout); else $("#login").classList.remove("hidden");
});
