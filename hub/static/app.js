/* Finance Automation Hub frontend.
 *
 * Talks only to the hub's own /api. Providers, their availability and their
 * "real vs simulated" labels all come from the server, so adding or enabling a
 * provider needs no change here. Everything rendered from run data is escaped:
 * journal text is model output and must be treated as untrusted.
 */
"use strict";

const state = { user: null, meta: null, providers: [], fixture: null, runId: null, timer: null,
                launch: { provider: null, scenario: "clean" } };
const BUSY = new Set(["queued", "running", "validating", "approved", "posting"]);
const STATUS_TEXT = {
  queued: "Queued", running: "Running", validating: "Validating", awaiting_approval: "Awaiting approval",
  approved: "Approved", posting: "Posting", posted: "Posted", posting_failed: "Posting failed",
  failed: "Failed", validation_failed: "Validation failed", rejected: "Rejected",
};
const STATUS_TONE = {
  posted: "good", awaiting_approval: "warn", failed: "bad", validation_failed: "bad",
  posting_failed: "bad", rejected: "idle",
};
const CHECKLIST_TEXT = { not_started: "Not started", in_progress: "In progress",
                         awaiting_review: "Awaiting review", complete: "Complete", blocked: "Needs attention" };

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (value) => {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : esc(value);
};
const when = (iso) => (iso ? new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "medium" }) : "-");

async function api(path, options = {}) {
  const headers = { "X-Hub-User": state.user, ...(options.body ? { "Content-Type": "application/json" } : {}) };
  const response = await fetch(path, { ...options, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.error?.message || body.detail?.[0]?.msg || `Request failed (${response.status})`);
    error.info = body.error || {};
    throw error;
  }
  return body;
}

function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = bad ? "toast bad" : "toast";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 6000);
}

function modeBadge(mode, label) {
  if (mode === "real_ai") return `<span class="badge real">Real AI${label ? " · " + esc(label) : ""}</span>`;
  if (mode === "simulated") return `<span class="badge sim">Simulated${label ? " · " + esc(label) : ""}</span>`;
  return `<span class="badge off">Not connected</span>`;
}
const statusPill = (status) =>
  `<span class="pill ${STATUS_TONE[status] || ""}">${esc(STATUS_TEXT[status] || status)}</span>`;
const userName = (id) => state.meta.users.find((u) => u.id === id)?.name || id || "-";

/* ------------------------------------------------------------------ navigation */

function show(view) {
  clearTimeout(state.timer);
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== `view-${view}`;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === view || (view === "run" && tab.dataset.view === "runs")));
  }
  ({ checklist: renderChecklist, launch: renderLaunch, runs: renderRuns, run: renderRun, audit: renderAudit })[view]()
    .catch((error) => toast(error.message, true));
}
function openRun(id) { state.runId = id; show("run"); }

/* ------------------------------------------------------------------ checklist */

async function renderChecklist() {
  const data = await api("/api/checklist");
  const done = data.items.filter((i) => i.status === "complete").length;
  $("view-checklist").innerHTML = `
    <div class="row spread">
      <div><h2>Month-end checklist</h2>
        <p class="lede">${esc(data.entity)} · period ${esc(data.period)} · ${done} of ${data.items.length} complete</p></div>
      <button class="btn" data-go="launch">Run a workflow</button>
    </div>
    <div class="card table-wrap"><table>
      <thead><tr><th>Task</th><th>Type</th><th>Status</th><th>Detail</th><th>Run</th></tr></thead>
      <tbody>${data.items.map((item) => `
        <tr>
          <td><span class="status-dot ${esc(item.status)}"></span>${esc(item.title)}</td>
          <td>${item.kind === "automated" ? '<span class="pill">Automated workflow</span>' : '<span class="muted">Manual · not automated here</span>'}</td>
          <td>${esc(CHECKLIST_TEXT[item.status] || item.status)}</td>
          <td class="muted">${esc(item.note || "")}</td>
          <td>${item.run_id ? `<button class="btn link mono" data-run="${esc(item.run_id)}">${esc(item.run_id)}</button>` : ""}</td>
        </tr>`).join("")}</tbody>
    </table></div>
    <p class="small muted">Only the derivatives line is driven by a workflow. Its status is recomputed from the run records each time a run changes state; the other lines are static placeholders.</p>`;
  if (data.items.some((i) => i.status === "in_progress")) state.timer = setTimeout(() => show("checklist"), 2000);
}

/* ------------------------------------------------------------------ launch */

async function renderLaunch() {
  state.providers = (await api("/api/providers")).providers;
  state.fixture = state.fixture || await api("/api/fixtures/derivatives");
  const available = state.providers.filter((p) => p.available);
  if (!available.some((p) => p.id === state.launch.provider)) state.launch.provider = available[0]?.id || null;
  const canPrepare = state.meta.users.find((u) => u.id === state.user).roles.includes("preparer");

  $("view-launch").innerHTML = `
    <div><h2>Derivatives revaluation</h2>
      <p class="lede">Send a fictional valuation report and trial balance to a provider, get journal proposals back, and have every amount recomputed independently before anyone can approve it.</p></div>
    <div class="card">
      <h3>1 · Who does the work</h3>
      <div class="providers">${state.providers.map((p) => `
        <label class="provider ${p.available ? "" : "unavailable"} ${p.id === state.launch.provider ? "selected" : ""}">
          <div><input type="radio" name="provider" value="${esc(p.id)}" ${p.available ? "" : "disabled"} ${p.id === state.launch.provider ? "checked" : ""}><strong>${esc(p.label)}</strong></div>
          <div>${modeBadge(p.execution_mode, p.model)}</div>
          <p class="small">${esc(p.description)}</p>
          ${p.available ? "" : `<p class="small"><strong>Unavailable:</strong> ${esc(p.unavailable_reason)}</p>`}
        </label>`).join("")}</div>
      <div id="scenario-wrap" ${state.launch.provider === "demo" ? "" : "hidden"}>
        <label class="field">Demo scenario
          <select class="input" id="scenario">${state.meta.demo_scenarios.map((s) =>
            `<option value="${esc(s.id)}" ${s.id === state.launch.scenario ? "selected" : ""}>${esc(s.label)}</option>`).join("")}</select>
        </label>
        <p class="small muted">The two wrong scenarios exist to show the validator rejecting a bad proposal.</p>
      </div>
    </div>
    <div class="card">
      <h3>2 · Source documents (fictional)</h3>
      <div class="grid-2">
        <label class="field">Derivatives valuation report (JSON)<textarea id="report" spellcheck="false"></textarea></label>
        <label class="field">Trial balance (JSON)<textarea id="tb" spellcheck="false"></textarea></label>
      </div>
      <button class="btn link" id="reset-fixture">Reset to the sample documents</button>
    </div>
    <div class="card">
      <h3>3 · Start</h3>
      <div class="banner sim"><strong>Ledger posting is simulated.</strong>${esc(state.meta.ledger.system)}</div>
      <div class="row">
        <button class="btn" id="start" ${canPrepare && state.launch.provider ? "" : "disabled"}>Start run</button>
        <span class="small muted">${canPrepare ? "You will need a different person to approve the result." : "Your role cannot start runs. Switch to a preparer."}</span>
      </div>
    </div>`;
  $("report").value = JSON.stringify(state.fixture.report, null, 2);
  $("tb").value = JSON.stringify(state.fixture.trial_balance, null, 2);

  for (const radio of document.querySelectorAll('input[name="provider"]')) {
    radio.addEventListener("change", () => { state.launch.provider = radio.value; keepEdits(); renderLaunch(); });
  }
  $("scenario")?.addEventListener("change", (e) => { state.launch.scenario = e.target.value; });
  $("reset-fixture").addEventListener("click", async () => { state.fixture = null; await renderLaunch(); });
  $("start").addEventListener("click", startRun);
}

function keepEdits() {
  try {
    state.fixture = { report: JSON.parse($("report").value), trial_balance: JSON.parse($("tb").value) };
  } catch { /* keep the last parseable version */ }
}

async function startRun() {
  let input;
  try {
    input = { report: JSON.parse($("report").value), trial_balance: JSON.parse($("tb").value) };
  } catch (error) {
    return toast(`The documents are not valid JSON: ${error.message}`, true);
  }
  const options = state.launch.provider === "demo" ? { demo_scenario: state.launch.scenario } : {};
  $("start").disabled = true;
  try {
    const { run } = await api("/api/runs", { method: "POST",
      body: JSON.stringify({ workflow: "derivatives_revaluation", provider: state.launch.provider, input, options }) });
    openRun(run.id);
  } catch (error) {
    $("start").disabled = false;
    if (error.info.existing_run_id) { toast(error.message, true); return openRun(error.info.existing_run_id); }
    toast([error.message, ...(error.info.details || [])].join(" · "), true);
  }
}

/* ------------------------------------------------------------------ runs list */

async function renderRuns() {
  const { runs } = await api("/api/runs");
  $("view-runs").innerHTML = `
    <div class="row spread"><div><h2>Runs</h2><p class="lede">Every execution is recorded, including failures and rejections.</p></div>
      <button class="btn" data-go="launch">Run a workflow</button></div>
    <div class="card table-wrap">${runs.length ? `<table>
      <thead><tr><th>Run</th><th>Period</th><th>Executed by</th><th>Status</th><th>Started by</th><th>Created</th></tr></thead>
      <tbody>${runs.map((r) => `
        <tr class="clickable" data-run="${esc(r.id)}">
          <td class="mono">${esc(r.id)}</td><td>${esc(r.entity)} ${esc(r.period)}</td>
          <td>${modeBadge(r.execution_mode, r.provider)}</td><td>${statusPill(r.status)}</td>
          <td>${esc(userName(r.requested_by))}</td><td>${esc(when(r.created_at))}</td>
        </tr>`).join("")}</tbody></table>` : '<p class="muted">No runs yet.</p>'}</div>`;
  if (runs.some((r) => BUSY.has(r.status))) state.timer = setTimeout(() => show("runs"), 2000);
}

/* ------------------------------------------------------------------ run detail */

function stepList(run) {
  const order = ["queued", "running", "validating", "awaiting_approval", "approved", "posting", "posted"];
  const labels = ["Queued", "AI execution", "Independent validation", "Review", "Approved", "Posting", "Posted"];
  const stops = { failed: 1, validation_failed: 2, rejected: 3, posting_failed: 5 };
  const stopAt = stops[run.status];
  const reached = stopAt ?? order.indexOf(run.status);
  return `<ol class="steps">${labels.map((label, i) => {
    let cls = i < reached ? "done" : "";
    if (i === reached) cls = stopAt !== undefined ? "stop" : (run.status === "posted" ? "done" : "now");
    return `<li class="${cls}">${esc(i === reached && stopAt !== undefined ? STATUS_TEXT[run.status] : label)}</li>`;
  }).join("")}</ol>`;
}

function journalsHtml(proposal) {
  return proposal.journals.map((j) => `
    <div class="journal">
      <div class="journal-head row spread">
        <div><strong class="mono">${esc(j.journal_ref)}</strong> · ${esc(j.description)}</div>
        ${j.source ? `<span class="small muted">${esc(j.source.hedge_designation === "cash_flow_hedge" ? "Cash flow hedge" : "Not designated")} · fair value ${money(j.source.prior_fair_value)} → ${money(j.source.current_fair_value)}</span>` : ""}
      </div>
      <div class="table-wrap"><table>
        <thead><tr><th>Account</th><th>Narrative</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead>
        <tbody>${j.lines.map((l) => `<tr>
          <td><span class="mono">${esc(l.account)}</span> ${esc(l.account_name)}</td><td>${esc(l.narrative)}</td>
          <td class="num">${Number(l.debit) ? money(l.debit) : ""}</td><td class="num">${Number(l.credit) ? money(l.credit) : ""}</td></tr>`).join("")}
          <tr class="total"><td colspan="2">Total</td><td class="num">${money(j.total_debit)}</td><td class="num">${money(j.total_credit)}</td></tr>
        </tbody></table></div>
      <div class="journal-foot"><strong>Rationale:</strong> ${esc(j.rationale)}</div>
    </div>`).join("");
}

function validationHtml(validation) {
  const rank = { fail: 0, warn: 1, pass: 2 };
  const checks = [...validation.checks].sort((a, b) => rank[a.status] - rank[b.status]);
  const s = validation.summary;
  return `
    <div class="banner ${validation.passed ? "good" : "bad"}">
      <strong>${validation.passed ? "Independent validation passed" : "Independent validation failed - this proposal cannot be approved"}</strong>
      ${s.passed} of ${s.total} checks passed · ${s.failed} failed · ${s.warnings} for reviewer attention. Every figure was recomputed from the source report without using the provider's output.
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Check</th><th>Scope</th><th>Result</th><th>Expected</th><th>Proposed / actual</th></tr></thead>
      <tbody>${checks.map((c) => `<tr>
        <td><span class="mono">${esc(c.check_id)}</span> ${esc(c.name)}<div class="small muted">${esc(c.detail)}</div></td>
        <td class="mono">${esc(c.scope || "batch")}</td>
        <td><span class="pill ${c.status === "pass" ? "good" : c.status === "fail" ? "bad" : "warn"}">${c.status === "warn" ? "Review" : esc(c.status)}</span></td>
        <td class="mono">${esc(c.expected ?? "")}</td><td class="mono">${esc(c.actual ?? "")}</td></tr>`).join("")}</tbody>
    </table></div>`;
}

function approvalHtml(run) {
  const me = state.meta.users.find((u) => u.id === state.user);
  if (run.status === "validation_failed") {
    return `<div class="banner bad"><strong>Approval is blocked.</strong>A proposal that fails independent validation cannot be approved or overridden. Fix the source data or start a new run.</div>`;
  }
  if (run.status !== "awaiting_approval") {
    if (run.approved_by) return `<p>Approved by <strong>${esc(userName(run.approved_by))}</strong> on ${esc(when(run.approved_at))}${run.approval_comment ? ` - “${esc(run.approval_comment)}”` : ""}.</p>`;
    if (run.rejected_by) return `<p>Rejected by <strong>${esc(userName(run.rejected_by))}</strong> on ${esc(when(run.rejected_at))} - “${esc(run.rejection_reason)}”.</p>`;
    return `<p class="muted">Nothing to approve yet.</p>`;
  }
  let blocker = "";
  if (!me.roles.includes("approver")) blocker = "Your role cannot approve. Switch to an approver.";
  else if (me.id === run.requested_by) blocker = "You started this run, so someone else has to approve it (segregation of duties).";
  const canReject = me.roles.includes("approver") || me.id === run.requested_by;
  return `
    ${blocker ? `<div class="banner"><strong>You cannot approve this run.</strong>${esc(blocker)}</div>` : ""}
    <label class="field">Comment or rejection reason<input type="text" id="decision-text" maxlength="300" placeholder="Required when rejecting"></label>
    <div class="row">
      <button class="btn" id="approve" ${blocker ? "disabled" : ""}>Approve and post (simulated)</button>
      <button class="btn danger" id="reject" ${canReject ? "" : "disabled"}>Reject</button>
    </div>
    <p class="small muted">Approval is bound to proposal <span class="mono">${esc(run.proposal_hash.slice(0, 16))}…</span>. If the proposal on record differs from the one on this page, approval is refused.</p>`;
}

function postingHtml(run) {
  if (run.status === "posting_failed") {
    return `<div class="banner bad"><strong>Posting failed.</strong>${esc(run.error_message)}</div>
            <button class="btn" id="retry-posting">Retry posting</button>`;
  }
  const p = run.posting;
  if (!p) return `<p class="muted">Not posted. Posting happens automatically after approval, and it is simulated.</p>`;
  return `
    <div class="banner sim"><strong>Simulated posting - nothing was sent to Microsoft.</strong>${esc(p.system)}. Batch and voucher numbers are made up and carry the SIM- prefix.</div>
    <dl class="kv"><dt>Batch</dt><dd class="mono">${esc(p.batch_number)}</dd><dt>Legal entity</dt><dd>${esc(p.legal_entity)}</dd>
      <dt>Posting date</dt><dd>${esc(p.posting_date)}</dd><dt>Recorded</dt><dd>${esc(when(p.posted_at))}</dd></dl>
    <div class="table-wrap"><table><thead><tr><th>Voucher</th><th>Journal</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead>
      <tbody>${p.vouchers.map((v) => `<tr><td class="mono">${esc(v.voucher)}</td><td>${esc(v.description)}</td>
        <td class="num">${money(v.total_debit)}</td><td class="num">${money(v.total_credit)}</td></tr>`).join("")}</tbody></table></div>`;
}

async function renderRun() {
  const { run, audit } = await api(`/api/runs/${encodeURIComponent(state.runId)}`);
  const meta = run.provider_meta || {};
  const usage = meta.usage && Object.keys(meta.usage).length
    ? Object.entries(meta.usage).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ") : "-";
  $("view-run").innerHTML = `
    <button class="btn link" data-go="runs">← All runs</button>
    <div class="row spread">
      <div><h2 class="mono">${esc(run.id)}</h2><p class="lede">Derivatives revaluation · ${esc(run.entity)} · ${esc(run.period)}</p></div>
      <div class="row">${modeBadge(run.execution_mode, meta.model || run.provider)} ${statusPill(run.status)}</div>
    </div>
    ${stepList(run)}
    ${run.error_message && run.status !== "posting_failed" ? `<div class="banner bad"><strong>${esc(run.error_code)}</strong>${esc(run.error_message)}</div>` : ""}
    ${run.execution_mode === "simulated" ? `<div class="banner sim"><strong>This proposal was not produced by AI.</strong>It is a scripted answer from the offline demo provider.</div>` : ""}
    <div class="grid-2">
      <div class="card"><h3>Execution record</h3><dl class="kv">
        <dt>Provider</dt><dd>${esc(run.provider)}</dd><dt>Model</dt><dd class="mono">${esc(meta.model || "-")}</dd>
        <dt>Provider request</dt><dd class="mono">${esc(meta.provider_request_id || "-")}</dd><dt>Token usage</dt><dd>${esc(usage)}</dd>
        <dt>Started by</dt><dd>${esc(userName(run.requested_by))}</dd><dt>Created</dt><dd>${esc(when(run.created_at))}</dd>
        <dt>Started</dt><dd>${esc(when(run.started_at))}</dd><dt>Finished</dt><dd>${esc(when(run.completed_at))}</dd>
        <dt>Input hash</dt><dd class="mono">${esc(run.input_hash.slice(0, 24))}…</dd></dl>
        ${(meta.notes || []).map((n) => `<p class="small muted">${esc(n)}</p>`).join("")}</div>
      <div class="card"><h3>Review and approval</h3>${approvalHtml(run)}</div>
    </div>
    ${run.proposal ? `
      <div class="card"><h3>Proposed journals</h3><p>${esc(run.proposal.summary)}</p>
        ${run.proposal.exceptions.length ? `<ul>${run.proposal.exceptions.map((e) =>
          `<li><span class="pill ${e.severity === "info" ? "idle" : e.severity === "error" ? "bad" : "warn"}">${esc(e.severity)}</span> <span class="mono">${esc(e.instrument_id)}</span> ${esc(e.message)}</li>`).join("")}</ul>` : ""}
        ${journalsHtml(run.proposal)}
        <p class="small muted">Batch total: debits ${money(run.proposal.totals.debit)} · credits ${money(run.proposal.totals.credit)} ${esc(run.proposal.currency)} · posting date ${esc(run.proposal.posting_date)} (taken from the source report, not from the provider)</p></div>
      <div class="card"><h3>Independent validation</h3>${validationHtml(run.validation)}</div>` : ""}
    <div class="card"><h3>D365 posting</h3>${postingHtml(run)}</div>
    <div class="card"><h3>Audit log for this run</h3><div class="table-wrap">${auditTable(audit)}</div></div>`;

  $("approve")?.addEventListener("click", () => decide("approve", { proposal_hash: run.proposal_hash, comment: $("decision-text").value }));
  $("reject")?.addEventListener("click", () => decide("reject", { reason: $("decision-text").value }));
  $("retry-posting")?.addEventListener("click", () => decide("retry-posting", null));
  if (BUSY.has(run.status)) state.timer = setTimeout(() => show("run"), 1200);
}

async function decide(action, body) {
  try {
    await api(`/api/runs/${encodeURIComponent(state.runId)}/${action}`, { method: "POST", body: body ? JSON.stringify(body) : undefined });
  } catch (error) { toast(error.message, true); }
  show("run");
}

/* ------------------------------------------------------------------ audit */

function auditTable(entries) {
  return `<table><thead><tr><th>#</th><th>Time</th><th>Actor</th><th>Event</th><th>Detail</th></tr></thead>
    <tbody>${entries.map((e) => `<tr><td class="mono">${e.seq}</td><td>${esc(when(e.ts))}</td><td>${esc(e.actor)}</td>
      <td class="mono">${esc(e.event)}</td><td class="mono small">${esc(Object.entries(e.detail).filter(([, v]) => v !== null)
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  "))}</td></tr>`).join("")}</tbody></table>`;
}

async function renderAudit() {
  const { entries, chain } = await api("/api/audit");
  $("view-audit").innerHTML = `
    <div><h2>Audit log</h2><p class="lede">Append-only. The database rejects edits and deletions, and each entry is hash-chained to the one before it.</p></div>
    <div class="banner ${chain.intact ? "good" : "bad"}"><strong>${chain.intact ? "Hash chain intact" : `Hash chain broken at entry ${chain.first_broken_seq}`}</strong>${chain.entries} entries verified.</div>
    <div class="card table-wrap">${entries.length ? auditTable(entries) : '<p class="muted">No entries yet.</p>'}</div>`;
}

/* ------------------------------------------------------------------ boot */

document.addEventListener("click", (event) => {
  const go = event.target.closest("[data-go]");
  if (go) return show(go.dataset.go);
  const run = event.target.closest("[data-run]");
  if (run) return openRun(run.dataset.run);
  const tab = event.target.closest(".tab");
  if (tab) show(tab.dataset.view);
});

(async function boot() {
  state.meta = await (await fetch("/api/meta")).json();
  state.user = localStorageGet("hub-user") || state.meta.users[0].id;
  if (!state.meta.users.some((u) => u.id === state.user)) state.user = state.meta.users[0].id;
  const select = $("user-select");
  select.innerHTML = state.meta.users.map((u) =>
    `<option value="${esc(u.id)}">${esc(u.name)} - ${esc(u.roles.join(" + "))}</option>`).join("");
  select.value = state.user;
  select.addEventListener("change", () => {
    state.user = select.value;
    try { localStorage.setItem("hub-user", state.user); } catch { /* storage unavailable */ }
    const visible = [...document.querySelectorAll(".view")].find((v) => !v.hidden).id.replace("view-", "");
    show(visible);
  });
  $("identity-note").textContent = "Demo identity · not authenticated";
  show("checklist");
})();

function localStorageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
