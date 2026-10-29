// Auth
function getToken() { return localStorage.getItem('dia_token') || ''; }
const _origFetch = window.fetch;
window.fetch = function(url, opts = {}) {
  if (typeof url === 'string' && url.startsWith('/api/')) {
    opts.headers = { ...(opts.headers || {}), 'x-auth-token': getToken() };
  }
  return _origFetch(url, opts);
};

let currentRunId   = null;
let currentPage    = 1;
let totalPages     = 1;
let allRows        = [];
let selectedRows   = new Set();
let activeRowKey   = null;
let activeRow      = null;
let filterDebounce = null;
let dashboardData  = null;
let directRowIdx   = null;
let paymentImport  = null;

const PER_PAGE = 50;

function rvLogout() {
  fetch('/api/auth/logout', { method: 'POST' });
  localStorage.removeItem('dia_token');
  localStorage.removeItem('dia_user');
  localStorage.removeItem('dia_last_run');
  window.location.href = '/login';
}

document.addEventListener('DOMContentLoaded', async () => {
  const token = getToken();
  if (!token) { window.location.href = '/login'; return; }
  const check = await fetch('/api/auth/me');
  if (!check.ok) { window.location.href = '/login'; return; }
  const user = await check.json();
  const displayName = user.display_name || user.username || '?';
  const unEl = document.getElementById('rvUsername');
  const avEl = document.getElementById('rvAvatar');
  if (unEl) unEl.textContent = displayName;
  if (avEl) avEl.textContent = displayName.charAt(0).toUpperCase();
  const sideName = document.getElementById('rvSidebarUser');
  const sideAvatar = document.getElementById('rvSidebarAvatar');
  if (sideName) sideName.textContent = displayName;
  if (sideAvatar) sideAvatar.textContent = displayName.charAt(0).toUpperCase();
  document.addEventListener('keydown', handleKeyboard);
  await loadRuns();
});

// Browsers may restore this page from the back-forward cache without firing
// DOMContentLoaded again. Restore the launcher for the already-loaded run.
window.addEventListener('pageshow', event => {
  if (event.persisted && currentRunId) {
    closeChat();
    show('rvChat');
  }
});

async function loadRuns() {
  try {
    const runs = await fetch('/api/runs?limit=50').then(r => r.json());
    const sel = document.getElementById('runSelect');
    sel.innerHTML = '<option value="">Select a run…</option>';
    if (!runs.length) { hide('rvChat'); show('rvEmpty'); return; }

    runs.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.run_id;
      const ts = r.created_at ? new Date(r.created_at).toLocaleString() : '';
      opt.textContent = `${r.filename || r.run_id}  —  ${(r.total_rows||0).toLocaleString()} rows  ${ts}`;
      sel.appendChild(opt);
    });

    // auto-load from URL param or first run
    const urlRunId = new URLSearchParams(location.search).get('run_id');
    const firstId  = urlRunId && runs.some(r => r.run_id === urlRunId) ? urlRunId : runs[0].run_id;
    sel.value = firstId;
    await loadRun(firstId);
  } catch(e) { console.error(e); show('rvEmpty'); }
}

let currentView   = 'clusters';
let clustersData  = null;
let flatLoaded    = false;
let currentGroupBy = 'signature';
let findingReportText = '';

function allClusters() {
  if (!clustersData) return [];
  return [...(clustersData.rule_clusters || []), ...(clustersData.emergent_clusters || [])];
}

function topCluster() {
  const rules = clustersData?.rule_clusters || [];
  const emergent = clustersData?.emergent_clusters || [];
  return rules[0] || emergent[0] || null;
}

function clusterReason(c) {
  if (!c) return 'No cluster selected';
  if (c.source === 'emergent') {
    return c.description || 'Data-driven anomaly detected outside configured tests.';
  }
  const tests = c.test_count || (c.test_ids || []).length || 1;
  if (tests > 1) return `${tests} tests fired on the same invoice group, making this a high-confidence review priority.`;
  return c.subtitle || 'Single-test pattern grouped for bulk review.';
}

function renderWorkbench() {
  const el = document.getElementById('rvWorkbenchGrid');
  if (!el || !clustersData) return;
  const rules = clustersData.rule_clusters || [];
  const emergent = clustersData.emergent_clusters || [];
  const d = dashboardData || {};
  const top = topCluster();
  const reviewed = d.reviewed_pct || 0;
  const openAmt = fmtAmt(d.unreviewed_amount || 0);

  el.innerHTML = `
    <div class="rv-wb-card primary">
      <div class="rv-wb-label">Next best action</div>
      <div class="rv-wb-value">${top ? esc(top.title) : 'No priority cluster'}</div>
      <div class="rv-wb-note">${top ? esc(clusterReason(top)) : 'Run tests to create a review queue.'}</div>
    </div>
    <div class="rv-wb-card">
      <div class="rv-wb-label">Rule patterns</div>
      <div class="rv-wb-value">${fmt(rules.length)}</div>
      <div class="rv-wb-note">Grouped by anomaly pattern first, not vendor fragments.</div>
    </div>
    <div class="rv-wb-card">
      <div class="rv-wb-label">Also abnormal</div>
      <div class="rv-wb-value">${fmt(emergent.length)}</div>
      <div class="rv-wb-note">Data-driven anomalies beyond configured rules.</div>
    </div>
    <div class="rv-wb-card">
      <div class="rv-wb-label">Open exposure</div>
      <div class="rv-wb-value">${openAmt}</div>
      <div class="rv-wb-note">${fmt(d.total_hit_rows || 0)} hit rows · ${reviewed}% reviewed</div>
      <div class="rv-wb-meter"><span style="width:${reviewed}%"></span></div>
    </div>
  `;
}

async function loadRun(runId) {
  if (!runId) { closeChat(); hide('rvChat'); show('rvEmpty'); return; }
  currentRunId = runId;
  paymentImport = null;
  document.getElementById('rvPaymentStatus').textContent = '';
  refreshPaymentImport(runId);
  localStorage.setItem('dia_last_run', JSON.stringify({ runId }));
  currentPage  = 1;
  selectedRows.clear();
  activeRowKey = null;
  flatLoaded   = false;
  clustersData = null;
  clearDetail();

  closeChat();
  hide('rvEmpty'); hide('rvKpiStrip'); hide('rvFilters'); hide('rvListPanel');
  hide('rvBody'); hide('rvClusterView'); hide('rvLevel1'); hide('rvAgent'); hide('rvViewToggle'); hide('rvChat');
  show('rvLoading');

  let reviewReady = false;
  try {
    // KPI dashboard (shared by both views)
    const dRes = await fetch(`/api/runs/${currentRunId}/review?per_page=1`);
    if (!dRes.ok) throw new Error(await dRes.text());
    const d = await dRes.json();
    renderDashboard(d.dashboard);

    // Chat only needs a valid run + test session, not cluster rendering.
    // Show it now so a cluster/API presentation failure cannot hide the copilot.
    show('rvChat');
    resetChat();
    reviewReady = true;

    await loadClusters();
    hide('rvLoading');
    show('rvKpiStrip'); show('rvViewToggle');
    // The decision queue is the reviewer's landing page; clusters are preloaded
    // above so the secondary tab opens instantly.
    setView('level1');
  } catch(e) {
    console.error(e);
    hide('rvLoading');
    if (reviewReady) {
      // The review API and chat are valid even if cluster presentation fails.
      show('rvKpiStrip');
      show('rvViewToggle');
      setView('flat');
    } else {
      closeChat();
      hide('rvChat');
      show('rvEmpty');
    }
  }
}

async function refreshPaymentImport(runId) {
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/payments`);
    if (!res.ok) throw new Error('Payment status unavailable');
    const data = await res.json();
    if (currentRunId !== runId) return;
    paymentImport = data;
    document.getElementById('rvPaymentStatus').textContent = data.imported
      ? `${fmt(data.matched)} linked · ${fmt(data.ambiguous)} ambiguous · ${fmt(data.unmatched)} unmatched`
      : 'No payment file';
    if (activeRow) renderDetail(activeRow);
  } catch (e) {
    if (currentRunId === runId) document.getElementById('rvPaymentStatus').textContent = e.message;
  }
}

async function uploadPaymentCsv(input) {
  const file = input.files?.[0];
  input.value = '';
  if (!file || !currentRunId) return;
  if (paymentImport?.imported && !window.confirm(
    'Replace this run’s existing payment evidence? Review decisions and risk scores will not change.'
  )) return;
  const runId = currentRunId;
  const button = document.getElementById('rvPaymentButton');
  const status = document.getElementById('rvPaymentStatus');
  button.disabled = true;
  status.textContent = 'Matching payment rows…';
  try {
    const body = new FormData();
    body.append('file', file);
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/payments`, { method: 'POST', body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Payment import failed');
    if (runId === currentRunId) await refreshPaymentImport(runId);
  } catch (e) {
    if (runId === currentRunId) status.textContent = `Import failed: ${e.message}`;
  } finally {
    button.disabled = false;
  }
}

function setTabBadge(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = fmt(n);
  el.classList.toggle('is-zero', !n);
}

document.addEventListener('keydown', e => {
  if (!e.altKey || e.ctrlKey || e.metaKey) return;
  const view = { '1': 'level1', '2': 'clusters', '3': 'flat', '4': 'agent' }[e.key];
  const bar = document.getElementById('rvViewToggle');
  if (!view || !bar || bar.style.display === 'none') return;
  e.preventDefault();
  setView(view);
});

function setView(view) {
  currentView = view;
  document.getElementById('viewReviewBtn')?.classList.toggle('active', view === 'level1');
  document.getElementById('viewClustersBtn').classList.toggle('active', view === 'clusters');
  document.getElementById('viewFlatBtn').classList.toggle('active', view === 'flat');
  document.getElementById('viewAgentBtn').classList.toggle('active', view === 'agent');
  hide('rvLevel1'); hide('rvAgent');
  if (view === 'level1') {
    hide('rvClusterView'); hide('rvBody'); hide('rvFilters'); hide('rvListPanel');
    show('rvLevel1');
    loadLevel1();
  } else if (view === 'clusters') {
    show('rvClusterView'); hide('rvBody'); hide('rvFilters'); hide('rvListPanel');
  } else if (view === 'agent') {
    hide('rvClusterView'); hide('rvBody'); hide('rvFilters'); hide('rvListPanel');
    show('rvAgent');
    loadAgentReview();
  } else {
    hide('rvClusterView'); show('rvBody');
    if (!flatLoaded) { flatLoaded = true; fetchPage(); }
    else { show('rvFilters'); show('rvListPanel'); }
  }
}

function agentActivityHtml(step) {
  const label = `<strong>${esc(step.step)}</strong>: ${esc(step.detail || '')}`;
  const rationale = step.reason ? `<div>Why: ${esc(step.reason)}</div>` : '';
  if (step.step === 'facts' && step.result) {
    const facts = step.result;
    const sources = Object.entries(facts.source_fields || {})
      .map(([field, value]) => `<span>${esc(field)}: ${esc(value)}</span>`).join('');
    const tests = (facts.tests || []).map(test => `
      <li><strong>${esc(test.name || test.id)}</strong>
        ${(test.matched_evidence || []).map(item =>
          `<span>${esc(item.field)}: ${esc(item.value)} (${esc(item.note)})</span>`).join('')}
      </li>`).join('');
    const comparison = facts.vendor_comparison;
    const peerSummary = comparison
      ? comparison.baseline_available
        ? `${comparison.peer_count} same-vendor peers; median ${comparison.median_peer_amount}; invoice ${comparison.amount_vs_median}× median; ${comparison.same_amount_peers} exact-amount peers.`
        : `Only ${comparison.peer_count} comparable same-vendor peers; no reliable baseline.`
      : '';
    return `<li>${label}<div class="rv-agent-facts">${sources || 'No source fields supplied.'}
      ${comparison ? `<div>Vendor context: ${esc(peerSummary)}</div>` : ''}
      ${facts.missing_required_fields?.length
        ? `<div>Verified blank required fields: ${facts.missing_required_fields.map(esc).join(', ')}</div>` : ''}
      ${facts.unavailable_required_fields?.length
        ? `<div>Required fields unavailable in this dataset: ${facts.unavailable_required_fields.map(esc).join(', ')}</div>` : ''}
      <ul>${tests}</ul></div></li>`;
  }
  if (step.step === 'tool_result' && step.result) {
    const tool = step.result;
    const summary = Array.isArray(tool)
      ? tool.map(item => `${item.test || 'Test'}: ${item.field} = ${item.value}`).join(' · ')
      : tool.related_count != null
        ? `${tool.related_count} other vendor invoices; sample: ${(tool.sample || [])
            .map(item => `${item.invoice || '?'} (${item.amount || '?'})`).join(', ')}`
        : tool.message || '';
    return `<li>${label}<div>${esc(summary || 'No additional evidence found')}</div></li>`;
  }
  return `<li>${label}${rationale}</li>`;
}

let agentCaseFilter = 'all';
let agentJobTimer = null;
let agentTrackedJob = null;

function agentCaseHtml(c) {
  const recommendation = {
    confirm: 'Confirm finding',
    dismiss: 'Dismiss finding',
    request_evidence: 'Needs evidence',
    manual_review: 'Manual review',
  }[c.recommendation] || 'Review needed';
  const status = {
    pending: 'Awaiting reviewer',
    confirmed: 'Confirmed',
    false_positive: 'False positive',
    needs_info: 'Needs information',
  }[c.review_status] || 'Awaiting reviewer';
  const tone = ['confirm', 'dismiss', 'request_evidence', 'manual_review'].includes(c.recommendation)
    ? c.recommendation : 'manual_review';
  const legacy = c.schema_version !== 5;
  const approvable = c.review_status === 'pending' && !legacy &&
    ['confirm', 'request_evidence'].includes(c.recommendation);
  const vendorArg = esc(JSON.stringify(c.vendor || ''));
  const recommendationArg = esc(JSON.stringify(c.recommendation || ''));
  return `
    <article class="rv-agent-case rv-agent-case-${tone}">
      <div class="rv-agent-case-top">
        <div class="rv-agent-identity">
          <div class="rv-agent-case-eyebrow">INVOICE ${esc(c.invoice || String(c.row_idx))}</div>
          <h3>${esc(c.vendor || 'Unknown vendor')}</h3>
          <div class="rv-agent-meta">
            <span class="rv-agent-pill">${esc(c.risk_band || 'Risk')} · ${esc(String(c.risk_score ?? '—'))}</span>
            <span>${fmtAmt(c.amount)}${c.currency ? ` ${esc(c.currency)}` : ''}</span>
            <span class="rv-agent-meta-note">Priority score is not sent to the agent</span>
          </div>
        </div>
        <span class="rv-agent-state ${c.review_status === 'pending' ? 'pending' : 'resolved'}">${esc(status)}</span>
      </div>
      <div class="rv-agent-recommendation">
        <span>AGENT RECOMMENDATION</span>
        <strong>${esc(recommendation)}</strong>
        <p>${esc(c.reason || 'No reason recorded. Inspect source evidence before deciding.')}</p>
      </div>
      ${c.suggested_check ? `<div class="rv-agent-next">
        <span>NEXT CHECK</span><div>${esc(c.suggested_check)}</div>
      </div>` : ''}
      ${c.vendor_comparison ? `<div class="rv-agent-context">${c.vendor_comparison.baseline_available
        ? `${fmt(c.vendor_comparison.peer_count)} same-vendor peers · Median ${fmt(c.vendor_comparison.median_peer_amount)} ${esc(c.currency || '')} · ${esc(String(c.vendor_comparison.amount_vs_median))}× median`
        : `Only ${fmt(c.vendor_comparison.peer_count)} comparable vendor peers · No reliable baseline`}</div>` : ''}
      ${c.missing_fields?.length ? `<div class="rv-agent-gap"><strong>Verified blank fields:</strong> ${c.missing_fields.map(esc).join(', ')}</div>` : ''}
      ${legacy ? '<div class="rv-agent-warning">Earlier brief: recheck the source evidence. Pending cases need a new investigation; recorded decisions are not changed automatically.</div>' : ''}
      ${c.review_status === 'pending' && c.recommendation === 'manual_review'
        ? '<div class="rv-agent-warning">Investigation task only. No decision has been recorded.</div>' : ''}
      ${c.recommendation === 'request_evidence'
        ? '<div class="rv-agent-hint">Needs Info is a hold. No evidence request is sent automatically.</div>' : ''}
      <div class="rv-agent-case-actions">
        <button class="rv-btn-secondary" onclick="openLevel1Invoice(${c.row_idx}, ${vendorArg})">Open invoice evidence</button>
        ${approvable
          ? `<button class="rv-btn-primary" onclick="approveAgentCase(${c.row_idx}, ${recommendationArg})">Approve recommendation</button>`
          : c.review_status !== 'pending'
            ? '<span class="rv-agent-resolved">Decision saved separately from the recommendation</span>'
            : ''}
      </div>
      <details class="rv-agent-trail">
        <summary>View investigation trail <span>${esc(c.model || 'Saved brief')}</span></summary>
        <ol>${(c.trace || []).map(agentActivityHtml).join('')}</ol>
      </details>
    </article>`;
}

function filterAgentCases(filter) {
  agentCaseFilter = filter;
  document.querySelectorAll('[data-agent-filter]').forEach(button =>
    button.classList.toggle('active', button.dataset.agentFilter === filter));
  loadAgentReview();
}

async function loadAgentReview() {
  if (!currentRunId) return;
  const status = document.getElementById('rvAgentStatus');
  try {
    const res = await fetch(`/api/runs/${currentRunId}/agent-review?filter=${agentCaseFilter}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not load agent review');
    setTabBadge('tabBadgeAgent', data.remaining);
    document.getElementById('rvAgentOverview').innerHTML = `
      <div><strong>${fmt(data.remaining)}</strong><span>Awaiting investigation</span></div>
      <div><strong>${fmt(data.processed)}</strong><span>Briefs prepared</span></div>
      <div><strong>${fmt(data.auto_approved)}</strong><span>Policy auto-approved · <button class="rv-agent-link" onclick="openAutoApproved()">View cases</button></span></div>
      <div><strong>${fmt(data.legacy_decisions)}</strong><span>Earlier decisions to recheck</span></div>`;
    const cases = data.cases.filter(c =>
      agentCaseFilter === 'all' ||
      (agentCaseFilter === 'needs_info' &&
        (c.review_status === 'needs_info' || c.recommendation === 'request_evidence')) ||
      (agentCaseFilter === 'legacy' && c.schema_version !== 5 && c.review_status !== 'pending'));
    const list = document.getElementById('rvAgentCases');
    list.innerHTML = cases.length ? cases.map(agentCaseHtml).join('')
      : '<div class="rv-agent-empty">No matching briefs. Run a batch to investigate pending invoices.</div>';
    if (data.cases_truncated) {
      list.insertAdjacentHTML('beforeend',
        '<div class="rv-agent-empty">Showing the latest 100 matching briefs. Older cases remain in the audit database.</div>');
    }
    if (data.active_job && !agentJobTimer) {
      monitorAgentJob(data.active_job.id);
    }
  } catch (e) {
    status.textContent = `✕ ${e.message}`;
  }
}

function openAutoApproved() {
  level1Lane = 'auto_clear';
  setView('level1');
}

async function approveAgentCase(rowIdx, recommendation) {
  const message = recommendation === 'request_evidence'
    ? 'Mark this invoice Needs Info? No request will be sent automatically. After checking the missing evidence, decide in All Rows.'
    : `Approve the agent's "${recommendation}" recommendation for this invoice?`;
  if (!window.confirm(message)) return;
  const status = document.getElementById('rvAgentStatus');
  try {
    const res = await fetch(`/api/runs/${currentRunId}/agent-review/${rowIdx}/approve`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Decision could not be saved');
    await loadAgentReview();
    status.textContent = `Decision recorded: ${data.review_status}.`;
  } catch (e) {
    status.textContent = `✕ ${e.message}`;
  }
}

async function monitorAgentJob(jobId) {
  agentTrackedJob = jobId;
  const runId = currentRunId;
  const status = document.getElementById('rvAgentStatus');
  const buttons = ['rvAgentRun5', 'rvAgentRun10'].map(id => document.getElementById(id));
  buttons.forEach(button => { button.disabled = true; });
  try {
    const res = await fetch(`/api/runs/${runId}/agent-review/jobs/${jobId}`);
    const job = await res.json();
    if (!res.ok) throw new Error(job.error || 'Batch status unavailable');
    if (runId !== currentRunId) return;
    status.textContent = `${job.status === 'failed' ? 'Batch stopped' : 'Investigating'}: ${job.completed.length}/${job.batch_size} processed.`;
    if (job.status === 'queued' || job.status === 'running') {
      agentJobTimer = setTimeout(() => {
        agentJobTimer = null;
        monitorAgentJob(jobId);
      }, 1500);
    } else {
      agentTrackedJob = null;
      buttons.forEach(button => { button.disabled = false; });
      await loadAgentReview();
      status.textContent = job.status === 'failed'
        ? `${job.error} ${job.completed.length} case(s) saved. Press Review next 5 or 10 to retry the rest.`
        : `Processed ${job.completed.length} invoice(s), including ${job.completed.filter(c => c.result === 'auto_approved').length} policy auto-approval(s).`;
    }
  } catch (e) {
    buttons.forEach(button => { button.disabled = false; });
    status.textContent = `✕ ${e.message}. Refresh this view to check the batch.`;
  }
}

async function runAgentBatch(size) {
  if (!currentRunId) return;
  const status = document.getElementById('rvAgentStatus');
  const buttons = ['rvAgentRun5', 'rvAgentRun10'].map(id => document.getElementById(id));
  buttons.forEach(b => { b.disabled = true; });
  status.textContent = `Investigating up to ${size} highest-risk pending invoices…`;
  try {
    const res = await fetch(`/api/runs/${currentRunId}/agent-review/batch`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ size }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not start batch');
    if (data.job) {
      monitorAgentJob(data.job);
    } else {
      status.textContent = 'No pending invoices need investigation.';
      buttons.forEach(b => { b.disabled = false; });
    }
  } catch (e) {
    status.textContent = `✕ ${e.message}`;
    buttons.forEach(b => { b.disabled = false; });
  }
}

function showRiskPatterns() {
  setView('clusters');
  requestAnimationFrame(() =>
    document.getElementById('rvClusterBoard')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  );
}

function showInvoiceReview() {
  setView('flat');
  requestAnimationFrame(() =>
    document.getElementById('rvBody')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  );
}

const LANE_ORDER = ['escalate', 'needs_evidence', 'auto_clear'];
const LANE_BLURB = {
  escalate: 'Clear high risk. Confirm the finding, or override with a reason.',
  needs_evidence: 'Genuinely uncertain. Open the evidence before deciding.',
  auto_clear: 'Low risk and complete evidence. Automatically approved by policy; reopen any case to review it.',
};

// Flat render order of every visible card, so J/K can walk the whole queue
// across lanes without the reviewer thinking about lane boundaries.
let level1Cards  = [];
let level1Cursor = -1;

async function showLevel1Review() {
  if (!currentRunId) return;
  setView('level1');
}

async function loadLevel1() {
  if (!currentRunId) return;
  const status = document.getElementById('rvLevel1Status');
  status.textContent = 'Sorting the queue…';
  try {
    const res  = await fetch(`/api/runs/${currentRunId}/level1?limit=100`);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Could not load the review queue');
    renderLevel1(data);
    status.textContent = '';
  } catch (e) {
    status.textContent = `✕ ${e.message}`;
  }
}

const LEVEL1_PAGE = 25;
let level1Data   = null;
let level1Lane   = null;
let level1Shown  = LEVEL1_PAGE;

function renderLevel1(data) {
  level1Data = data;
  const s     = data.summary || {};
  const lanes = data.lanes || {};
  const approved = data.auto_approved || { count: 0 };
  document.getElementById('rvLevel1Count').textContent = fmt(s.pending || 0);
  setTabBadge('tabBadgeReview', s.pending || 0);
  document.getElementById('rvLevel1ProgressText').textContent =
    `${fmt(s.decided || 0)} of ${fmt(s.total_flagged || 0)} decided`;
  document.getElementById('rvLevel1ProgressFill').style.width = `${s.progress_pct || 0}%`;

  // Keep the reviewer's lane across reloads; otherwise open the most urgent lane that has work.
  if (!level1Lane || !(lanes[level1Lane] && lanes[level1Lane].count)
      && !(level1Lane === 'auto_clear' && approved.count)) {
    level1Lane = LANE_ORDER.find(r => lanes[r] && lanes[r].count)
      || (approved.count ? 'auto_clear' : level1Lane || LANE_ORDER[0]);
  }
  paintLevel1();
}

function paintLevel1() {
  const lanes = (level1Data && level1Data.lanes) || {};
  const approved = (level1Data && level1Data.auto_approved) || { count: 0, items: [] };
  const tiles = LANE_ORDER.map((route, i) => {
    const lane = lanes[route] || { label: route, count: 0, amount: 0 };
    const isApproved = route === 'auto_clear';
    return `
      <button class="rv-l1-tile ${esc(route)}${route === level1Lane ? ' active' : ''}" onclick="switchLevel1Lane('${route}')">
        <span class="rv-l1-tile-label">${isApproved ? 'Auto-approved' : esc(lane.label)} <kbd>${i + 1}</kbd></span>
        <span class="rv-l1-tile-count">${fmt(isApproved ? approved.count : lane.count)}</span>
        <span class="rv-l1-tile-sub">${fmtAmt(isApproved ? approved.amount || 0 : lane.amount)}</span>
      </button>`;
  }).join('');

  const isApproved = level1Lane === 'auto_clear';
  const lane = isApproved
    ? { label: 'Auto-approved · Review again anytime', count: approved.count,
        items: approved.items || [], truncated: approved.truncated }
    : lanes[level1Lane];
  level1Cards = [];
  let body = '<div class="rv-l1-empty">Nothing left to review.</div>';
  let head = '';
  if (lane) {
    const items   = lane.items || [];
    const visible = items.slice(0, level1Shown);
    head = `
      <div class="rv-l1-lane-head ${esc(level1Lane)}">
        <div><strong>${esc(lane.label)}</strong><small>${esc(LANE_BLURB[level1Lane] || '')}</small></div>
      </div>`;
    if (visible.length) {
      body = visible.map(item => isApproved ? buildAutoApprovedCard(item) : buildLaneCard(item, level1Lane)).join('');
      const more = items.length - visible.length;
      if (more > 0) {
        body += `<button class="rv-l1-more" onclick="showMoreLevel1()">Show ${fmt(Math.min(LEVEL1_PAGE, more))} more · ${fmt(lane.count - visible.length)} remaining</button>`;
      } else if (lane.truncated) {
        body += `<div class="rv-l1-trunc">Showing the first ${fmt(items.length)} by risk. Decide some to load the rest.</div>`;
      }
    } else {
      body = '<div class="rv-l1-empty">Nothing in this lane.</div>';
    }
  }
  document.getElementById('rvLevel1Lanes').innerHTML =
    `<div class="rv-l1-tiles">${tiles}</div>${head}<div class="rv-l1-list">${body}</div>`;
  level1Cursor = level1Cards.length ? 0 : -1;
  paintLevel1Cursor(false);
}

function buildAutoApprovedCard(item) {
  const tests = (item.flagged_by || []).map(t => t.test_name || t.test_id).join(' · ');
  const vendorArg = esc(JSON.stringify(item.vendor_name || item.vendor_number || ''));
  return `
    <article class="rv-l1-card">
      <div class="rv-l1-score ${esc(String(item.band_css || ''))}">${item.risk_score}</div>
      <div class="rv-l1-main">
        <strong>${esc(String(item.vendor_name || item.vendor_number || 'Unknown vendor'))}</strong>
        <span>#${esc(String(item.invoice_number || item.row_idx))} · ${esc(tests)}</span>
        <p class="rv-l1-reason">Auto-approved: ${esc(String(item.routing?.reason || 'Low risk with complete evidence'))}</p>
      </div>
      <div class="rv-l1-amount">${fmtAmt(item.amount)}<div class="rv-l1-cur">${esc(String(item.currency || ''))}</div></div>
      <div class="rv-l1-actions">
        <button onclick="openLevel1Invoice(${item.row_idx}, ${vendorArg})">Evidence</button>
        <button onclick="reopenAutoApproval(${item.row_idx})">Review again</button>
      </div>
    </article>`;
}

async function reopenAutoApproval(rowIdx) {
  const status = document.getElementById('rvLevel1Status');
  try {
    const res = await fetch(`/api/runs/${currentRunId}/level1/${rowIdx}/reopen`, { method: 'POST' });
    if (!res.ok) throw new Error('Could not reopen this invoice');
    level1Lane = 'needs_evidence';
    await loadLevel1();
    status.textContent = 'Reopened for manual review. This invoice will not be auto-approved again.';
  } catch (e) {
    status.textContent = `✕ ${e.message}`;
  }
}

function switchLevel1Lane(route) {
  if (!LANE_ORDER.includes(route) || route === level1Lane) return;
  level1Lane  = route;
  level1Shown = LEVEL1_PAGE;
  paintLevel1();
}

function showMoreLevel1() {
  level1Shown += LEVEL1_PAGE;
  const keep = level1Cursor;
  paintLevel1();
  level1Cursor = Math.max(0, keep);
  paintLevel1Cursor(false);
}

function buildLaneCard(item, route) {
  const cursor = level1Cards.length;
  level1Cards.push({ row_idx: item.row_idx, route });

  const r     = item.routing || {};
  const tests = (item.flagged_by || []).map(t => t.test_name || t.test_id).join(' · ');
  const chips = (item.evidence || []).map(e =>
    `<span class="rv-chip">${esc(String(e.note || e.field || ''))}: ${esc(String(e.value))}</span>`).join('');
  const vendorArg = esc(JSON.stringify(item.vendor_name || item.vendor_number || ''));
  const opposite  = route === 'escalate' ? 'auto_clear' : 'escalate';

  return `
    <article class="rv-l1-card" id="rvL1Card_${item.row_idx}" data-cursor="${cursor}">
      <div class="rv-l1-score ${esc(String(item.band_css || ''))}">${item.risk_score}</div>
      <div class="rv-l1-main">
        <strong>${esc(String(item.vendor_name || item.vendor_number || 'Unknown vendor'))}</strong>
        <span>#${esc(String(item.invoice_number || item.row_idx))} · ${esc(String(item.risk_band || ''))} · ${esc(tests || 'No test label')}</span>
        <p class="rv-l1-reason">${esc(String(r.reason || ''))}</p>
        <div class="rv-l1-chips">${chips}</div>
      </div>
      <div class="rv-l1-amount">${fmtAmt(item.amount)}<div class="rv-l1-cur">${esc(String(item.currency || ''))}</div></div>
      <div class="rv-l1-actions">
        <button class="primary" onclick="reviewLevel1(${item.row_idx}, '${esc(route)}', false)">Accept</button>
        <button onclick="reviewLevel1(${item.row_idx}, '${opposite}', true)">Override</button>
        <button onclick="openLevel1Invoice(${item.row_idx}, ${vendorArg})">Evidence</button>
      </div>
    </article>`;
}

function paintLevel1Cursor(scroll = true) {
  document.querySelectorAll('.rv-l1-card.is-cursor').forEach(el => el.classList.remove('is-cursor'));
  const card = level1Cards[level1Cursor];
  if (!card) return;
  const el = document.getElementById(`rvL1Card_${card.row_idx}`);
  if (!el) return;
  el.classList.add('is-cursor');
  // Keep the cursor inside a collapsed lane from scrolling the page nowhere.
  if (scroll && el.offsetParent !== null) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function moveLevel1Cursor(step) {
  if (!level1Cards.length) return;
  level1Cursor = Math.min(level1Cards.length - 1, Math.max(0, level1Cursor + step));
  paintLevel1Cursor();
}

document.addEventListener('keydown', e => {
  if (currentView !== 'level1') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target?.isContentEditable) return;

  const key = e.key.toLowerCase();
  if (key >= '1' && key <= '3') { e.preventDefault(); switchLevel1Lane(LANE_ORDER[Number(key) - 1]); }
  else if (key === 'j') { e.preventDefault(); moveLevel1Cursor(1); }
  else if (key === 'k') { e.preventDefault(); moveLevel1Cursor(-1); }
  else if (key === 'a' || key === 'o') {
    const card = level1Cards[level1Cursor];
    if (!card) return;
    e.preventDefault();
    const opposite = card.route === 'escalate' ? 'auto_clear' : 'escalate';
    reviewLevel1(card.row_idx, key === 'a' ? card.route : opposite, key === 'o');
  }
});

async function bulkAcceptLane(lane) {
  const label = level1Data?.lanes?.[lane]?.label || lane;
  if (!window.confirm(
    `Accept every pending invoice in the "${label}" lane?\n\n` +
    `Each one is recorded with an audit note naming the rule that cleared it. ` +
    `Risk scores and test results do not change.`
  )) return;

  const status = document.getElementById('rvLevel1Status');
  status.textContent = 'Recording decisions…';
  try {
    const res = await fetch(`/api/runs/${currentRunId}/level1/bulk-accept`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lane }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Bulk accept failed');
    // loadLevel1() resets the status line, so confirm only after it has finished.
    await loadLevel1();
    status.textContent = data.updated
      ? `✓ ${fmt(data.updated)} invoice${data.updated === 1 ? '' : 's'} decided and logged.`
      : data.message;
  } catch (e) {
    status.textContent = `✕ ${e.message}`;
  }
}

async function reviewLevel1(rowIdx, finalRoute, override) {
  let note = '';
  if (override) {
    note = window.prompt('Why are you overriding the suggested route?')?.trim() || '';
    if (!note) return;
  }
  const status = document.getElementById('rvLevel1Status');
  try {
    const res = await fetch(`/api/runs/${currentRunId}/level1/${rowIdx}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: override ? 'overridden' : 'accepted',
        final_route: finalRoute,
        note,
      }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Could not save the decision');
    const keep = level1Cursor;
    await loadLevel1();
    // Hold the cursor roughly where it was so the queue keeps flowing downward.
    level1Cursor = Math.min(keep, level1Cards.length - 1);
    paintLevel1Cursor();
    // Set the message after the reload, which otherwise clears the status line.
    status.textContent = override
      ? `✓ Override saved — invoice marked ${data.review_status || ''}.`
      : `✓ Decided — invoice marked ${data.review_status || ''}.`;
  } catch (e) {
    status.textContent = `✕ ${e.message}`;
  }
}

async function openLevel1Invoice(rowIdx, vendor) {
  // The evidence view replaces the queue; do not leave both panels visible.
  flatLoaded = true;
  setView('flat');
  document.getElementById('vendorSearch').value = vendor;
  currentPage = 1;
  directRowIdx = Number(rowIdx);
  try {
    await fetchPage();
  } finally {
    directRowIdx = null;
  }
  const row = allRows.find(item => Number(item.row_idx) === Number(rowIdx));
  if (row) selectRow(row);
}

function goToTestLibrary() {
  if (!currentRunId) {
    alert('Select a run before opening its test library.');
    return;
  }
  localStorage.setItem('dia_last_run', JSON.stringify({ runId: currentRunId }));
  window.location.href = '/?section=tests';
}

async function loadClusters() {
  const res = await fetch(`/api/runs/${currentRunId}/clusters?group_by=${currentGroupBy}`);
  if (!res.ok) throw new Error(await res.text());
  clustersData = await res.json();
  setTabBadge('tabBadgeClusters', (clustersData.rule_clusters || []).length);
  renderWorkbench();
  renderTestOverview(clustersData.test_overview || []);
  renderClusterBoard(clustersData.rule_clusters || []);
  renderEmergentBoard(clustersData.emergent_clusters || []);
  renderLinkedRisks(clustersData.linked_entities || []);
  document.getElementById('rvSuggest').style.display =
    (clustersData.emergent_clusters || []).length ? 'block' : 'none';
  // restore cached AI triage if present
  if (clustersData.ai && clustersData.ai.executive_summary) {
    renderTriage(clustersData.ai);
  } else {
    runTriage(true);
  }
}

async function setGroupBy(gb) {
  if (gb === currentGroupBy) return;
  currentGroupBy = gb;
  document.querySelectorAll('#rvGroupBy .rv-gb-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.gb === gb));
  document.getElementById('rvBoardSub').textContent =
    gb === 'signature' ? 'grouped by anomaly pattern' : `grouped by ${gb}`;
  await loadClusters();
}

function renderTestOverview(tests) {
  const el = document.getElementById('rvTestOverview');
  if (!tests.length) { el.innerHTML = ''; return; }
  el.innerHTML = tests.map(t => `
    <div class="rv-test-ov-card">
      <div class="rv-test-ov-name">${esc(t.test_name)}</div>
      <div class="rv-test-ov-stats">
        <span class="rv-test-ov-hits">${fmt(t.hit_count)} hits</span>
        <span class="rv-test-ov-amt">${fmtAmt(t.amount_at_risk)}</span>
      </div>
    </div>
  `).join('');
}

function renderClusterBoard(clusters) {
  const board = document.getElementById('rvClusterBoard');
  document.getElementById('rvBoardSub').textContent =
    currentGroupBy === 'signature'
      ? `${clusters.length} patterns · most tests fired first`
      : `${clusters.length} clusters · grouped by ${currentGroupBy}`;
  if (!clusters.length) {
    board.innerHTML = '<div class="rv-board-empty">No clusters found</div>';
    return;
  }
  board.innerHTML = clusters.map(c => buildClusterCard(c)).join('');
}

function renderEmergentBoard(clusters) {
  const board = document.getElementById('rvEmergentBoard');
  document.getElementById('rvEmergentSub').textContent =
    clusters.length
      ? `${clusters.length} data-driven anomalies no rule flagged`
      : 'nothing statistically abnormal beyond your rules';
  if (!clusters.length) {
    board.innerHTML = '<div class="rv-board-empty">No emergent anomalies detected</div>';
    return;
  }
  board.innerHTML = clusters.map(c => buildEmergentCard(c)).join('');
}

function renderLinkedRisks(entities) {
  const header = document.getElementById('rvLinkedHeader');
  const board = document.getElementById('rvLinkedBoard');
  header.style.display = entities.length ? 'flex' : 'none';
  if (!entities.length) { board.innerHTML = ''; return; }
  document.getElementById('rvLinkedSub').textContent =
    `${entities.length} entities recur across two or more anomaly patterns`;
  board.innerHTML = entities.map(e => `
    <div class="rv-linked-card">
      <div class="rv-linked-icon">${e.entity_type === 'vendor' ? 'V' : 'A'}</div>
      <div class="rv-linked-main">
        <div class="rv-linked-kicker">${esc(e.entity_type)}</div>
        <div class="rv-linked-name">${esc(e.label)}</div>
        <div class="rv-linked-patterns">${(e.cluster_titles || []).map(esc).join(' · ')}</div>
      </div>
      <div class="rv-linked-stat"><strong>${fmt(e.cluster_count)}</strong><span>patterns</span></div>
      <div class="rv-linked-stat"><strong>${fmt(e.invoice_count)}</strong><span>invoices</span></div>
      <div class="rv-linked-stat amount"><strong>${fmtAmt(e.amount_at_risk)}</strong><span>exposure</span></div>
    </div>
  `).join('');
}

function connectedBadges(c) {
  return (c.connected_entities || []).map(e =>
    `<span class="rv-cl-linked">${e.entity_type === 'vendor' ? 'Vendor' : 'Approver'} in ${e.cluster_count} patterns</span>`
  ).join('');
}

function buildClusterCard(c) {
  const chips = (c.evidence_chips || []).map(ch => `<span class="rv-cl-chip">${esc(ch)}</span>`).join('');
  const why = c.ai_summary || clusterReason(c);
  const aiLine = `<div class="rv-cl-ai">${esc(why)}</div>`;
  const reviewedBadge = c.reviewed_pct > 0
    ? `<span class="rv-cl-reviewed">${c.reviewed_pct}% reviewed</span>` : '';
  const tcBadge = c.test_count > 1
    ? `<span class="rv-cl-tc">${c.test_count} tests</span>` : '';
  const progress = Math.max(0, Math.min(100, c.reviewed_pct || 0));

  return `
    <div class="rv-cl-card band-${c.dominant_band}" id="clcard_${c.cluster_id}">
      <div class="rv-cl-head" onclick="toggleCluster('${c.cluster_id}')">
        <div class="rv-cl-rank">#${c.rank}<span>Priority</span></div>
        <div class="rv-cl-main">
          <div class="rv-cl-kicker">Rules caught</div>
          <div class="rv-cl-title">${esc(c.title)} ${tcBadge}</div>
          <div class="rv-cl-sub"><span class="rv-cl-test">${esc(c.subtitle || 'Pattern cluster')}</span></div>
          <div class="rv-cl-chips">${chips} ${reviewedBadge} ${connectedBadges(c)}</div>
          ${aiLine}
        </div>
        <div class="rv-cl-metrics">
          <div class="rv-cl-size">${fmt(c.size)}<span>invoices</span></div>
          <div class="rv-cl-amt">${c.amount_display}<span>at risk</span></div>
          <div class="rv-cl-band ${c.dominant_band}">${c.dominant_band}</div>
        </div>
        <div class="rv-cl-chevron" id="clchev_${c.cluster_id}">▾</div>
      </div>
      <div class="rv-cl-body" id="clbody_${c.cluster_id}" style="display:none">
        <div class="rv-cl-progress"><span style="width:${progress}%"></span></div>
        <div class="rv-cl-actions">
          <button class="rv-cl-btn review" onclick="setView('flat')">Review rows</button>
          <button class="rv-cl-btn investigate" id="invbtn_${c.cluster_id}" onclick="investigateCluster('${c.cluster_id}')">Investigate</button>
          <button class="rv-cl-btn confirm" onclick="clusterBulk('${c.cluster_id}','confirmed')">✓ Confirm all ${c.size}</button>
          <button class="rv-cl-btn fp" onclick="clusterBulk('${c.cluster_id}','false_positive')">✕ Dismiss all ${c.size}</button>
          <span class="rv-cl-samplenote">showing top ${Math.min(c.sample_rows.length, c.size)} of ${c.size}</span>
        </div>
        <div class="rv-investigation" id="clinvest_${c.cluster_id}" style="display:none"></div>
        <div class="rv-cl-samples">${(c.sample_rows || []).map(buildSampleRow).join('')}</div>
      </div>
    </div>
  `;
}

function buildEmergentCard(c) {
  const chips = (c.evidence_chips || []).map(ch => `<span class="rv-cl-chip">${esc(ch)}</span>`).join('');
  const aiLine = `<div class="rv-cl-ai">${esc(c.ai_summary || c.description || 'Data-driven anomaly detected outside configured rules.')}</div>`;
  const suggest = c.suggested_test
    ? `<button class="rv-cl-btn suggest" onclick='genTestFromAnomaly(${JSON.stringify(c.suggested_test)})'>✦ Generate this test</button>`
    : '';
  const progress = Math.max(0, Math.min(100, c.reviewed_pct || 0));
  return `
    <div class="rv-cl-card emergent band-${c.dominant_band}" id="clcard_${c.cluster_id}">
      <div class="rv-cl-head" onclick="toggleCluster('${c.cluster_id}')">
        <div class="rv-cl-rank emergent">${c.rank}<span>Signal</span></div>
        <div class="rv-cl-main">
          <div class="rv-cl-kicker">Also looks abnormal</div>
          <div class="rv-cl-title">${esc(c.title)} <span class="rv-cl-sev ${c.severity}">${c.severity}</span></div>
          <div class="rv-cl-chips">${chips} ${connectedBadges(c)}</div>
          ${aiLine}
        </div>
        <div class="rv-cl-metrics">
          <div class="rv-cl-size">${fmt(c.size)}<span>invoices</span></div>
          <div class="rv-cl-amt">${c.amount_display}<span>at risk</span></div>
          <div class="rv-cl-band ${c.dominant_band}">${c.dominant_band}</div>
        </div>
        <div class="rv-cl-chevron" id="clchev_${c.cluster_id}">▾</div>
      </div>
      <div class="rv-cl-body" id="clbody_${c.cluster_id}" style="display:none">
        <div class="rv-cl-progress"><span style="width:${progress}%"></span></div>
        <div class="rv-cl-actions">
          <button class="rv-cl-btn review" onclick="setView('flat')">Review rows</button>
          <button class="rv-cl-btn investigate" id="invbtn_${c.cluster_id}" onclick="investigateCluster('${c.cluster_id}')">Investigate</button>
          <button class="rv-cl-btn confirm" onclick="clusterBulk('${c.cluster_id}','confirmed')">✓ Confirm all ${c.size}</button>
          <button class="rv-cl-btn fp" onclick="clusterBulk('${c.cluster_id}','false_positive')">✕ Dismiss all ${c.size}</button>
          ${suggest}
          <span class="rv-cl-samplenote">showing top ${Math.min((c.sample_rows||[]).length, c.size)} of ${c.size}</span>
        </div>
        <div class="rv-investigation" id="clinvest_${c.cluster_id}" style="display:none"></div>
        <div class="rv-cl-samples">${(c.sample_rows || []).map(buildSampleRow).join('')}</div>
      </div>
    </div>
  `;
}

function genTestFromAnomaly(st) {
  if (!st || !st.prompt) return;
  localStorage.setItem('dia_prefill_test', st.prompt);
  window.location.href = '/#build';
}

async function loadSuggestions() {
  const btn = document.getElementById('suggestBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="rv-spinner-sm"></span> Thinking…';
  const body = document.getElementById('rvSuggestBody');
  try {
    const res = await fetch(`/api/runs/${currentRunId}/anomaly/suggest`, { method: 'POST' });
    const data = await res.json();
    const list = data.suggestions || [];
    if (!list.length) {
      body.innerHTML = '<div class="rv-suggest-empty">No test suggestions available.</div>';
    } else {
      body.innerHTML = list.map(s => `
        <div class="rv-suggest-card">
          <div class="rv-suggest-name">${esc(s.name || '')} <span class="rv-suggest-size">${fmt(s.size||0)} invoices</span></div>
          <div class="rv-suggest-desc">${esc(s.description || '')}</div>
          <div class="rv-suggest-prompt">${esc(s.prompt || '')}</div>
          <button class="rv-cl-btn suggest" onclick='genTestFromAnomaly(${JSON.stringify({prompt: s.prompt})})'>✦ Generate test</button>
        </div>
      `).join('');
    }
    if (data.disclaimer) body.innerHTML += `<div class="rv-disclaimer">${esc(data.disclaimer)}</div>`;
  } catch(e) {
    body.innerHTML = '<div class="rv-suggest-empty">Suggestion failed.</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Re-suggest';
  }
}

function buildSampleRow(s) {
  const evMap = {};
  (s.evidence || []).forEach(e => { evMap[e.field] = e; });

  function fld(label, field, val) {
    const ev = evMap[field];
    const v = val != null ? val : (s[field] || '—');
    if (ev) {
      return `<div class="rv-sr-fld highlighted"><span class="rv-sr-lbl">${label}</span>` +
             `<span class="rv-sr-val">${esc(String(v))}</span>` +
             `<span class="rv-sr-note">${esc(ev.note)}</span></div>`;
    }
    return `<div class="rv-sr-fld"><span class="rv-sr-lbl">${label}</span><span class="rv-sr-val">${esc(String(v))}</span></div>`;
  }

  const vendor = s.vendor_name || s.vendor_number || 'Unknown';
  const statusPill = s.review_status && s.review_status !== 'pending'
    ? `<span class="rv-sr-status ${s.review_status}">${statusLabel(s.review_status)}</span>` : '';

  return `
    <div class="rv-sr">
      <div class="rv-sr-score ${s.band_css}">${s.risk_score}</div>
      <div class="rv-sr-content">
        <div class="rv-sr-top">
          <span class="rv-sr-vendor">${esc(vendor)}</span>
          <span class="rv-sr-inv">${esc(s.physical_invoice_no || '—')}</span>
          ${statusPill}
        </div>
        <div class="rv-sr-flds">
          ${fld('Amount', 'amount_excl_lc', (s.amount_excl_lc || '—') + ' ' + (s.reference_currency || ''))}
          ${fld('Authorized', 'date_authorized')}
          ${fld('Captured', 'date_captured')}
          ${fld('Pay date', 'scheduled_pay_date')}
          ${fld('Description', 'invoice_description')}
        </div>
      </div>
    </div>
  `;
}

function toggleCluster(cid) {
  const body = document.getElementById(`clbody_${cid}`);
  const chev = document.getElementById(`clchev_${cid}`);
  const open = body.style.display === 'none';
  body.style.display = open ? 'block' : 'none';
  chev.textContent = open ? '▴' : '▾';
}

async function investigateCluster(cid) {
  const btn = document.getElementById(`invbtn_${cid}`);
  const panel = document.getElementById(`clinvest_${cid}`);
  if (!btn || !panel) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="rv-spinner-sm dark"></span> Investigating…';
  panel.style.display = 'block';
  panel.innerHTML = '<div class="rv-investigation-loading">Connecting vendors, approvers, history, and related patterns…</div>';
  try {
    const res = await fetch(`/api/runs/${currentRunId}/clusters/investigate?group_by=${currentGroupBy}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cluster_id: cid }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Investigation failed');
    const facts = data.facts || {};
    const actions = (data.next_actions || []).map(a => `<li>${esc(a)}</li>`).join('');
    const related = (facts.related_patterns || []).map(p =>
      `<span>${esc(p.title)} (${fmt(p.shared_invoices)} shared)</span>`
    ).join('');
    panel.innerHTML = `
      <div class="rv-investigation-head">
        <strong>Investigation brief</strong>
        <span>${data.cached ? 'cached' : data.source === 'ai' ? 'AI-assisted' : 'instant fallback'}</span>
      </div>
      <p>${esc(data.summary || '')}</p>
      <div class="rv-investigation-facts">
        <span><strong>${fmt(facts.prior_vendor_invoice_count || 0)}</strong> prior vendor invoices</span>
        <span><strong>${fmt(facts.confirmed || 0)}</strong> confirmed</span>
        <span><strong>${fmt(facts.pending || 0)}</strong> pending</span>
      </div>
      ${related ? `<div class="rv-investigation-related"><strong>Related patterns</strong>${related}</div>` : ''}
      ${actions ? `<div class="rv-investigation-actions"><strong>Next actions</strong><ol>${actions}</ol></div>` : ''}
    `;
  } catch(e) {
    panel.innerHTML = `<div class="rv-triage-error">${esc(e.message || 'Investigation failed')}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Investigate';
  }
}

async function clusterBulk(cid, status) {
  const c = allClusters().find(x => x.cluster_id === cid);
  if (!c) return;
  const verb = status === 'confirmed' ? 'Confirm' : 'Dismiss';
  if (!confirm(`${verb} all ${c.size} invoices in "${c.title}"?`)) return;

  const res = await fetch(`/api/runs/${currentRunId}/review/bulk`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ row_indices: c.row_indices, status }),
  });
  if (!res.ok) { alert('Bulk action failed'); return; }

  // refresh dashboard + clusters
  const dRes = await fetch(`/api/runs/${currentRunId}/review?per_page=1`);
  if (dRes.ok) { const d = await dRes.json(); renderDashboard(d.dashboard); }
  await loadClusters();
  flatLoaded = false;
}

async function runTriage(auto = false) {
  const btn = document.getElementById('triageBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="rv-spinner-sm"></span> Summarizing…';
  try {
    const res = await fetch(`/api/runs/${currentRunId}/ai/triage?group_by=${currentGroupBy}`, { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      document.getElementById('rvTriageBody').style.display = 'block';
      document.getElementById('rvTriageBody').innerHTML =
        `<div class="rv-triage-error">${esc(data.error)}</div>`;
    } else {
      renderTriage(data);
      // merge cluster summaries into both boards
      if (data.cluster_summaries && clustersData) {
        allClusters().forEach(c => { c.ai_summary = data.cluster_summaries[c.cluster_id] || c.ai_summary; });
        renderClusterBoard(clustersData.rule_clusters || []);
        renderEmergentBoard(clustersData.emergent_clusters || []);
      }
    }
  } catch(e) {
    console.error(e);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh Summary';
  }
}

function renderTriage(ai) {
  const body = document.getElementById('rvTriageBody');
  body.style.display = 'block';
  const priorities = (ai.priorities || []).map(p => {
    const c = allClusters().find(x => x.cluster_id === p.cluster_id);
    const name = c ? c.title : p.cluster_id;
    return `<li><span class="rv-pri-name">${esc(name)}</span> — ${esc(p.reason)}</li>`;
  }).join('');
  body.innerHTML = `
    <div class="rv-triage-summary">${esc(ai.executive_summary || '')}</div>
    ${priorities ? `<div class="rv-triage-pri-title">Review these first:</div><ol class="rv-triage-pri">${priorities}</ol>` : ''}
  `;
  if (ai.disclaimer) {
    const d = document.getElementById('rvTriageDisclaimer');
    d.style.display = 'block';
    d.textContent = ai.disclaimer;
  }
  document.getElementById('triageBtn').textContent = 'Refresh';
}

async function fetchPage() {
  const params = buildParams();
  params.set('page', currentPage);
  params.set('per_page', PER_PAGE);

  const res = await fetch(`/api/runs/${currentRunId}/review?${params}`);
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();

  allRows       = data.rows || [];
  totalPages    = data.total_pages || 1;
  dashboardData = data.dashboard;

  // Build test filter checkboxes from available test IDs
  buildTestCheckboxes(data.test_ids || []);

  hide('rvLoading');
  renderDashboard(data.dashboard);
  renderList(allRows, data.total_filtered);
  renderPagination(data.page, data.total_pages);
  updateBulkButtons();
  show('rvKpiStrip'); show('rvFilters'); show('rvListPanel');

  if (activeRowKey) {
    const same = allRows.find(r => r.row_key === activeRowKey);
    if (same) { activeRow = same; renderDetail(same); }
  }
}

function buildTestCheckboxes(testIds) {
  const container = document.getElementById('testCheckboxes');
  // only rebuild if test list changed
  if (container.dataset.built === testIds.join(',')) return;
  container.dataset.built = testIds.join(',');
  container.innerHTML = testIds.map(id => `
    <label class="rv-chip-label">
      <input type="checkbox" name="test" value="${esc(id)}" onchange="applyFilters()"> ${esc(id)}
    </label>
  `).join('');
}

function buildParams() {
  const p = new URLSearchParams();
  if (directRowIdx !== null) p.set('row_idx', directRowIdx);
  const bands    = [...document.querySelectorAll('input[name="band"]:checked')].map(i => i.value);
  const tests    = [...document.querySelectorAll('input[name="test"]:checked')].map(i => i.value);
  const statuses = [...document.querySelectorAll('input[name="status"]:checked')].map(i => i.value);
  if (bands.length === 1) p.set('band', bands[0]);
  if (tests.length === 1) p.set('test_id', tests[0]);
  if (statuses.length === 1) p.set('status', statuses[0]);
  if (document.getElementById('overlapOnly').checked) p.set('overlap_only', 'true');
  const vendor = document.getElementById('vendorSearch').value.trim();
  if (vendor) p.set('vendor', vendor);
  const minAmt = document.getElementById('minAmount').value;
  const maxAmt = document.getElementById('maxAmount').value;
  if (minAmt) p.set('min_amount', minAmt);
  if (maxAmt) p.set('max_amount', maxAmt);
  return p;
}

function applyFilters() { currentPage = 1; fetchPage(); }
function debounceFilters() { clearTimeout(filterDebounce); filterDebounce = setTimeout(applyFilters, 350); }
function clearFilters() {
  document.querySelectorAll('input[name="band"],input[name="test"],input[name="status"]').forEach(i => i.checked = false);
  document.getElementById('overlapOnly').checked = false;
  document.getElementById('vendorSearch').value  = '';
  document.getElementById('minAmount').value     = '';
  document.getElementById('maxAmount').value     = '';
  applyFilters();
}
function filterBand(band) {
  if (currentView === 'clusters') setView('flat');
  document.querySelectorAll('input[name="band"]').forEach(i => i.checked = (i.value === band));
  applyFilters();
}

function filterStatus(status) {
  if (currentView === 'clusters') setView('flat');
  document.querySelectorAll('input[name="status"]').forEach(i => i.checked = (i.value === status));
  applyFilters();
}

function renderDashboard(d) {
  if (!d) return;
  dashboardData = d;
  document.getElementById('kpiTotal').textContent         = fmt(d.total_hit_rows);
  setTabBadge('tabBadgeFlat', d.total_hit_rows || 0);
  document.getElementById('kpiCritical').textContent      = fmt(d.critical);
  document.getElementById('kpiHigh').textContent          = fmt(d.high);
  document.getElementById('kpiMedium').textContent        = fmt(d.medium);
  document.getElementById('kpiLow').textContent           = fmt(d.low);
  document.getElementById('kpiReviewedPct').textContent   = (d.reviewed_pct || 0) + '%';
  document.getElementById('kpiConfirmed').textContent     = fmt(d.confirmed || 0);
  document.getElementById('kpiFp').textContent            = fmt(d.false_positive || 0);
  document.getElementById('kpiAmountAtRisk').textContent  = fmtAmt(d.unreviewed_amount || 0);
  const queueCount = Math.max(
    0,
    (d.total_hit_rows || 0) - (d.confirmed || 0) - (d.false_positive || 0) - (d.needs_info || 0)
  );
  const queueBadge = document.getElementById('rvQueueCount');
  if (queueBadge) queueBadge.textContent = fmt(queueCount);

  const pct = d.reviewed_pct || 0;
  if (pct > 0 || d.confirmed || d.false_positive) {
    show('rvProgress');
    document.getElementById('rvProgressFill').style.width  = pct + '%';
    document.getElementById('rvProgressLabel').textContent = pct + '% reviewed';
  }
}

function renderList(rows, total) {
  const container = document.getElementById('rvHitList');
  document.getElementById('rvListCount').textContent = `${fmt(total)} invoice${total !== 1 ? 's' : ''}`;

  if (!rows.length) {
    container.innerHTML = '<div style="padding:40px;text-align:center;color:#475569;font-size:13px">No results match the current filters</div>';
    return;
  }

  container.innerHTML = '';
  rows.forEach(row => container.appendChild(buildHitRow(row)));
}

function buildHitRow(row) {
  const div = document.createElement('div');
  div.className = `rv-hit-row${row.row_key === activeRowKey ? ' active' : ''}${row.review_status !== 'pending' ? ' reviewed' : ''}`;
  div.dataset.rowKey = row.row_key;

  const vendor = row.vendor_name || row.vendor_number || 'Unknown Vendor';
  const invNo  = row.physical_invoice_no || row.system_invoice_no || '—';
  const date   = row.date_authorized || row.date_documented || '—';

  const tagHtml = row.flagged_by.map(f => {
    const aiClass = f.type === 'generated' ? ' ai' : '';
    const label   = f.test_name || f.test_id;
    const ai      = f.type === 'generated' ? ' ✦' : '';
    return `<span class="rv-tag${aiClass}" title="${esc(label)}">${esc(label.length > 18 ? label.substring(0,18)+'…' : label)}${ai}</span>`;
  }).join('');

  const overlapHtml = row.overlap_count >= 2
    ? `<span class="rv-overlap-badge">${row.overlap_count} tests</span>` : '';

  div.innerHTML = `
    <input type="checkbox" class="rv-hit-check" onclick="toggleRowSelect(event,'${row.row_key}')" ${selectedRows.has(row.row_key) ? 'checked' : ''}>
    <div class="rv-hit-score ${row.band_css}">${row.risk_score}</div>
    <div class="rv-hit-body">
      <div class="rv-hit-top">
        <div class="rv-hit-vendor">${esc(vendor)}</div>
        <div class="rv-hit-amount">${fmtAmt(row.amount)} <span style="font-size:10px;font-weight:400;color:#94a3b8">${esc(row.reference_currency||'')}</span></div>
      </div>
      <div class="rv-hit-meta-row">
        <div class="rv-hit-meta">${esc(invNo)} · ${esc(date)}</div>
        <div class="rv-hit-status ${row.review_status}">${statusLabel(row.review_status)}</div>
      </div>
      <div class="rv-hit-tags">${tagHtml} ${overlapHtml}</div>
    </div>
  `;

  div.addEventListener('click', e => { if (e.target.type === 'checkbox') return; selectRow(row); });
  return div;
}

function selectRow(row) {
  activeRowKey = row.row_key;
  activeRow    = row;
  document.querySelectorAll('.rv-hit-row').forEach(el => el.classList.toggle('active', el.dataset.rowKey === activeRowKey));
  renderDetail(row);
}

function evidenceSummary(row) {
  const evidence = row.evidence || [];
  const tests = row.flagged_by || [];
  const testNames = tests.map(t => t.test_name || t.test_id).filter(Boolean);
  if (!evidence.length && !testNames.length) return '';
  const chips = evidence.map(e =>
    `<span class="rv-why-chip"><strong>${esc(e.field || 'field')}</strong> ${esc(e.note || '')}</span>`
  ).join('');
  const testList = testNames.map(n => `<span>${esc(n)}</span>`).join('');
  return `
    <div class="rv-detail-section rv-why">
      <div class="rv-detail-section-title">Why this needs review</div>
      <div class="rv-why-title">${row.overlap_count >= 2 ? 'Multiple signals found on this invoice' : 'Evidence matched configured review logic'}</div>
      <div class="rv-why-tests">${testList}</div>
      ${chips ? `<div class="rv-why-chips">${chips}</div>` : ''}
    </div>
  `;
}

function timelineSection(row) {
  const sentinel = v => !v || ['none','null','nan','n/a',''].includes(String(v).toLowerCase().trim());
  const steps = [
    ['Documented',    row.date_documented,    false],
    ['Captured',      row.date_captured,      false],
    ['Authorized',    row.date_authorized,    false],
    ['Scheduled Pay', row.scheduled_pay_date, false],
    ['Payment',       row.payment_date,       true],   // always show when payment-timing test present
    ['Actual Pay',    row.actual_pay_date,    true],   // alternate payment date field
  ];
  const hasPaymentTimingTest = (row.flagged_by || []).some(f =>
    /payment|before|invoice|date/i.test(f.test_id + ' ' + f.test_name)
  );
  const filtered = steps.filter(([, val, alwaysShow]) => !sentinel(val) || (alwaysShow && hasPaymentTimingTest));
  if (!filtered.length) return '';

  const bothPayBlank = hasPaymentTimingTest && sentinel(row.payment_date) && sentinel(row.actual_pay_date);

  return `
    <div class="rv-detail-section">
      <div class="rv-detail-section-title">Invoice timeline</div>
      <div class="rv-timeline">
        ${filtered.map(([label, val]) => {
          const blank = sentinel(val);
          const cls = blank ? ' rv-tl-missing' : '';
          const display = blank ? 'not recorded' : esc(String(val));
          return `<div class="rv-tl-step${cls}"><span></span><div><strong>${label}</strong><em>${display}</em></div></div>`;
        }).join('')}
      </div>
      ${bothPayBlank ? `<div class="rv-tl-warn">⚠ <strong>Both payment_date and actual_pay_date are blank.</strong> The generated test code should skip this row — if it still flagged it, the code is using scheduled_pay_date or date_captured as a payment proxy. Review the generated code.</div>` : ''}
    </div>
  `;
}

async function loadPaymentEvidence(runId, rowIdx) {
  const target = document.getElementById('rvPaymentEvidence');
  if (!target) return;
  if (!paymentImport?.imported) {
    target.textContent = 'Attach a payment CSV to this run to view candidate records. This will not change test results or decisions.';
    return;
  }
  target.textContent = 'Loading payment evidence…';
  try {
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/payments/invoices/${rowIdx}`);
    if (!res.ok) throw new Error('Could not load payment evidence');
    const data = await res.json();
    if (currentRunId !== runId || activeRow?.row_idx !== rowIdx ||
        document.getElementById('rvPaymentEvidence') !== target) return;
    const records = (data.records || []).map(p => {
      const label = p.match_status === 'matched'
        ? 'Unique invoice-key match (payment status unverified)'
        : `Ambiguous: ${p.candidate_count} invoice rows share this key`;
      return `<div class="rv-kv" style="display:block;padding:8px 0">
        <strong>${esc(p.system_payment_no || p.physical_payment_no || `CSV row ${p.source_row}`)}</strong>
        <div style="font-size:11px;color:var(--text-muted)">${esc(label)}</div>
        <div style="font-size:11px;line-height:1.6">
          Document: ${esc(p.document_type || '—')} · Status: ${esc(p.payment_status || '—')}<br>
          Export DatePay: ${esc(p.date_pay || 'not recorded')} · Cleared date: ${esc(p.date_cleared || 'not recorded')}<br>
          Amount incl.: ${esc(p.amount_incl_lc || '—')} ${esc(p.currency || '')}
        </div>
      </div>`;
    }).join('');
    target.innerHTML = `
      <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">
        Matched by company, system invoice number, vendor and fiscal year.
        DatePay and status are source claims, not proof of executed payment.
        Unmatched payment rows cannot be assigned to an invoice.
      </div>
      ${records || '<div>No candidate payment rows for this invoice.</div>'}
      ${data.truncated ? `<small>Showing 20 of ${data.total} candidate records.</small>` : ''}
      <small>Source: ${esc(data.source?.filename || 'payment CSV')} · No risk or decision changes</small>
    `;
  } catch (e) {
    if (document.getElementById('rvPaymentEvidence') === target) target.textContent = e.message;
  }
}

function renderDetail(row) {
  hide('rvDetailEmpty');
  const dc = document.getElementById('rvDetailContent');
  dc.style.display = 'block';

  const vendor = row.vendor_name || row.vendor_number || 'Unknown Vendor';

  const testHitsHtml = row.flagged_by.map(f => `
    <div class="rv-test-hit">
      <span class="rv-test-hit-id" title="${esc(f.test_id)}">${esc(f.test_id)}</span>
      <span class="rv-test-hit-name" title="${esc(f.test_name)}">${esc(f.test_name)}</span>
      <span class="rv-test-hit-wt">wt ${f.weightage}</span>
      ${f.type === 'generated' ? '<span class="rv-test-hit-ai">✦ AI</span>' : ''}
    </div>
  `).join('');

  // build evidence map: udm_field → {note}
  const evMap = {};
  (row.evidence || []).forEach(e => { evMap[e.field] = e; });

  const fieldDefs = [
    ['Vendor Number',   'vendor_number',        row.vendor_number],
    ['Invoice No',      'physical_invoice_no',   row.physical_invoice_no || row.system_invoice_no],
    ['Date Authorized', 'date_authorized',       row.date_authorized],
    ['Date Documented', 'date_documented',       row.date_documented],
    ['Date Captured',   'date_captured',         row.date_captured],
    ['Scheduled Pay',   'scheduled_pay_date',    row.scheduled_pay_date],
    ['Payment Date',    'payment_date',          row.payment_date],
    ['Actual Pay Date', 'actual_pay_date',       row.actual_pay_date],
    ['Captured By',     'captured_by',           row.captured_by],
    ['Authorized By',   'authorized_by',         row.authorized_by],
    ['Amount (Excl.)',  'amount_excl_lc',        row.amount_excl_lc],
    ['Amount (Incl.)',  'amount_incl_lc',        row.amount_incl_lc],
    ['Currency',        'reference_currency',    row.reference_currency],
    ['Company Code',    'company_code',          row.company_code],
    ['Payment Type',    'payment_type',          row.payment_type],
    ['Fiscal Year',     'fiscal_year',           row.fiscal_year],
  ].filter(([, , v]) => v && String(v).toLowerCase() !== 'none' && String(v).toLowerCase() !== 'null');

  const kvHtml = fieldDefs.map(([label, field, rawVal]) => {
    const ev = evMap[field];
    const display = (field === 'amount_excl_lc' || field === 'amount_incl_lc')
      ? fmtAmt(parseFloat(String(rawVal).replace(',', '')))
      : esc(String(rawVal));
    if (ev) {
      return `<div class="rv-kv rv-kv-highlighted">
        <span class="rv-kv-label">${label}</span>
        <div class="rv-kv-right">
          <span class="rv-kv-val">${display}</span>
          <span class="rv-kv-note">${esc(ev.note)}</span>
        </div>
      </div>`;
    }
    return `<div class="rv-kv"><span class="rv-kv-label">${label}</span><div class="rv-kv-right"><span class="rv-kv-val">${display}</span></div></div>`;
  }).join('');

  const descEv = evMap['invoice_description'];
  const descHtml = row.invoice_description
    ? `<div class="rv-detail-section">
        <div class="rv-detail-section-title">Description${descEv ? ` <span class="rv-kv-note" style="margin-left:6px">${esc(descEv.note)}</span>` : ''}</div>
        <div style="font-size:12px;line-height:1.6;${descEv ? 'background:#fffbeb;border:1px solid #fde68a;border-radius:7px;padding:8px 10px;' : 'color:var(--text-secondary)'}">${esc(row.invoice_description)}</div>
       </div>` : '';
  const whyHtml = evidenceSummary(row);
  const timelineHtml = timelineSection(row);

  const status = row.review_status || 'pending';
  const note   = row.review_note || '';

  dc.innerHTML = `
    <div class="rv-detail-head">
      <div class="rv-detail-score-row">
        <div class="rv-detail-score ${row.band_css}">${row.risk_score}</div>
        <div>
          <div class="rv-detail-vendor" style="cursor:pointer;color:var(--blue)" onclick="openVendorDrill('${esc(row.vendor_number||vendor)}')" title="View all invoices for this vendor">${esc(vendor)} ↗</div>
          <div class="rv-detail-sub">${row.band_label} Risk${row.overlap_count >= 2 ? ` · ${row.overlap_count} tests` : ''}</div>
        </div>
      </div>
    </div>

    ${whyHtml}

    <div class="rv-detail-section">
      <div class="rv-detail-section-title">Flagged By (${row.flagged_by.length} test${row.flagged_by.length !== 1 ? 's' : ''})</div>
      ${testHitsHtml}
    </div>

    ${timelineHtml}

    <div class="rv-detail-section">
      <div class="rv-detail-section-title">Payment export evidence</div>
      <div id="rvPaymentEvidence"></div>
    </div>

    <div class="rv-detail-section">
      <div class="rv-detail-section-title">Invoice Details</div>
      ${kvHtml}
    </div>

    ${descHtml}

    <div class="rv-action-strip">
      <div class="rv-action-title">Review Decision</div>
      <div class="rv-action-btns">
        <button class="rv-action-btn confirm${status==='confirmed'?' active':''}" onclick="setDecision('confirmed')">✓ Confirm</button>
        <button class="rv-action-btn fp${status==='false_positive'?' active':''}" onclick="setDecision('false_positive')">✕ False +</button>
        <button class="rv-action-btn needs-info${status==='needs_info'?' active':''}" onclick="setDecision('needs_info')">? Info</button>
        <button class="rv-action-btn reset${status==='pending'?' active':''}" onclick="setDecision('pending')">↺ Reset</button>
      </div>
      <textarea id="reviewNote" class="rv-note-input" placeholder="Add note (optional)…">${esc(note)}</textarea>
      <button class="rv-save-btn" onclick="saveReview()">Save Decision</button>
      <div class="rv-shortcut-hint">C = Confirm · F = False Positive · N = Needs Info · ↓ = Next</div>
    </div>
  `;
  dc._pendingStatus = status;
  loadPaymentEvidence(currentRunId, row.row_idx);
}

function clearDetail() {
  activeRow = null; show('rvDetailEmpty');
  const dc = document.getElementById('rvDetailContent');
  dc.style.display = 'none';
}

function setDecision(status) {
  const dc = document.getElementById('rvDetailContent');
  dc._pendingStatus = status;
  dc.querySelectorAll('.rv-action-btn').forEach(b => b.classList.remove('active'));
  const map = { confirmed:'confirm', false_positive:'fp', needs_info:'needs-info', pending:'reset' };
  dc.querySelector(`.rv-action-btn.${map[status]}`)?.classList.add('active');
}

async function saveReview() {
  if (!activeRow || !currentRunId) return;
  const dc     = document.getElementById('rvDetailContent');
  const status = dc._pendingStatus || 'pending';
  const note   = document.getElementById('reviewNote').value.trim();

  await fetch(`/api/runs/${currentRunId}/review/${activeRow.row_idx}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status, note }),
  });

  activeRow.review_status = status;
  activeRow.review_note   = note;

  const listEl = document.querySelector(`.rv-hit-row[data-row-key="${CSS.escape(activeRow.row_key)}"]`);
  if (listEl) {
    const statusEl = listEl.querySelector('.rv-hit-status');
    if (statusEl) { statusEl.className = `rv-hit-status ${status}`; statusEl.textContent = statusLabel(status); }
    listEl.classList.toggle('reviewed', status !== 'pending');
  }

  // refresh KPI strip
  const res = await fetch(`/api/runs/${currentRunId}/review?per_page=1`);
  if (res.ok) { const d = await res.json(); renderDashboard(d.dashboard); }

  advanceToNext();
}

function toggleRowSelect(event, rowKey) {
  event.stopPropagation();
  if (selectedRows.has(rowKey)) selectedRows.delete(rowKey); else selectedRows.add(rowKey);
  updateBulkButtons();
}

function toggleSelectAll(checked) {
  allRows.forEach(r => { if (checked) selectedRows.add(r.row_key); else selectedRows.delete(r.row_key); });
  document.querySelectorAll('.rv-hit-check').forEach(cb => cb.checked = checked);
  updateBulkButtons();
}

function updateBulkButtons() {
  const has = selectedRows.size > 0;
  document.getElementById('bulkFpBtn').style.display      = has ? '' : 'none';
  document.getElementById('bulkConfirmBtn').style.display = has ? '' : 'none';
  if (has) {
    document.getElementById('bulkConfirmBtn').textContent = `Confirm (${selectedRows.size})`;
    document.getElementById('bulkFpBtn').textContent      = `Mark FP (${selectedRows.size})`;
  }
}

async function bulkAction(status) {
  if (!selectedRows.size || !currentRunId) return;
  await Promise.all([...selectedRows].map(rk => {
    const row = allRows.find(r => r.row_key === rk);
    return fetch(`/api/runs/${currentRunId}/review/${row?.row_idx ?? rk}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, note: '' }),
    });
  }));
  selectedRows.clear();
  document.getElementById('selectAllVisible').checked = false;
  updateBulkButtons();
  fetchPage();
}

function renderPagination(page, total) {
  const wrap = document.getElementById('rvPagination');
  if (total <= 1) { wrap.style.display = 'none'; return; }
  wrap.style.display = 'flex';
  document.getElementById('pageInfo').textContent = `Page ${page} of ${total}`;
  document.getElementById('prevBtn').disabled = page <= 1;
  document.getElementById('nextBtn').disabled = page >= total;
}

function changePage(delta) {
  const next = currentPage + delta;
  if (next < 1 || next > totalPages) return;
  currentPage = next;
  clearDetail();
  fetchPage();
}

function handleKeyboard(e) {
  if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
  if (!activeRow) return;
  const map = { c:'confirmed', f:'false_positive', n:'needs_info' };
  if (map[e.key?.toLowerCase()]) { setDecision(map[e.key.toLowerCase()]); saveReview(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); advanceToNext(); }
  else if (e.key === 'ArrowUp')   { e.preventDefault(); advanceToPrev(); }
}

function advanceToNext() {
  if (!activeRowKey || !allRows.length) return;
  const idx = allRows.findIndex(r => r.row_key === activeRowKey);
  if (idx < allRows.length - 1) selectRow(allRows[idx + 1]);
  else if (currentPage < totalPages) { currentPage++; fetchPage(); }
}
function advanceToPrev() {
  if (!activeRowKey || !allRows.length) return;
  const idx = allRows.findIndex(r => r.row_key === activeRowKey);
  if (idx > 0) selectRow(allRows[idx - 1]);
}

// ── Feature 4: Vendor drill-down ──────────────────────────────────────────
async function openVendorDrill(vendorNumber) {
  if (!currentRunId || !vendorNumber) return;
  const res = await fetch(`/api/runs/${currentRunId}/vendor/${encodeURIComponent(vendorNumber)}`);
  if (!res.ok) return;
  const d = await res.json();

  const existing = document.getElementById('vendorModal');
  if (existing) existing.remove();

  const rows = (d.invoices || []).map(r => `
    <tr style="${r.flagged ? 'background:#fff5f5' : ''}">
      <td>${esc(r.physical_invoice_no || r.system_invoice_no || '—')}</td>
      <td>${esc(r.date_authorized || r.date_documented || '—')}</td>
      <td style="text-align:right;font-weight:600">${esc(r.amount_excl_lc || '—')}</td>
      <td>${esc(r.invoice_description?.substring(0,60) || '—')}</td>
      <td>${r.flagged ? '<span style="color:var(--red);font-weight:700">Flagged</span>' : '<span style="color:var(--green)">✓ Clean</span>'}</td>
    </tr>
  `).join('');

  const modal = document.createElement('div');
  modal.id = 'vendorModal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;display:flex;align-items:center;justify-content:center;padding:20px';
  modal.innerHTML = `
    <div style="background:#fff;border-radius:14px;width:100%;max-width:820px;max-height:85vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.2)">
      <div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px">
        <div style="flex:1">
          <div style="font-size:15px;font-weight:800;color:var(--text-primary)">${esc(d.vendor_name || d.vendor_number)}</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:2px">${d.invoice_count} invoices · ${fmtAmt(d.total_amount)} total · <span style="color:var(--red)">${d.flagged_count} flagged</span></div>
        </div>
        <button onclick="document.getElementById('vendorModal').remove()" style="background:#f1f5f9;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px;font-weight:600">✕ Close</button>
      </div>
      <div style="overflow-y:auto;padding:0">
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr style="background:#f8fafc;position:sticky;top:0">
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Invoice No</th>
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Date</th>
            <th style="padding:8px 12px;text-align:right;border-bottom:1px solid var(--border)">Amount</th>
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Description</th>
            <th style="padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)">Status</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

// ── Feature 5: Export ──────────────────────────────────────────────────────
async function exportFindings(status = 'confirmed') {
  if (!currentRunId) return;
  try {
    const res = await fetch(`/api/runs/${currentRunId}/export?status=${encodeURIComponent(status)}`);
    if (!res.ok) {
      let message = 'Export failed';
      try { message = (await res.json()).error || message; } catch (_) {}
      throw new Error(message);
    }
    const blob = await res.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `dia_findings_${currentRunId}_${status}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
  } catch (e) {
    alert(e.message || 'Export failed');
  }
}

async function generateFinding() {
  if (!currentRunId) return;
  const btn = document.getElementById('findingBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="rv-spinner-sm"></span> Generating…';
  try {
    const res = await fetch(`/api/runs/${currentRunId}/finding-report`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Finding generation failed');
    findingReportText = data.plain_text || '';
    openFindingModal(data);
  } catch(e) {
    alert(e.message || 'Finding generation failed');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate Finding';
  }
}

function openFindingModal(report) {
  document.getElementById('findingModal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'findingModal';
  modal.className = 'rv-modal';
  modal.innerHTML = `
    <div class="rv-finding-modal">
      <div class="rv-finding-head">
        <div>
          <div class="rv-finding-kicker">Confirmed findings report</div>
          <div class="rv-finding-title">${esc(report.title || 'Audit Finding')}</div>
        </div>
        <button class="rv-modal-close" onclick="document.getElementById('findingModal').remove()">✕ Close</button>
      </div>
      <div class="rv-finding-meta">
        <span>${fmt(report.facts?.confirmed_count || 0)} confirmed invoices</span>
        <span>${fmtAmt(report.facts?.amount_at_risk || 0)} exposure</span>
        <span>${report.cached ? 'cached' : report.source === 'ai' ? 'AI-assisted' : 'instant fallback'}</span>
      </div>
      <textarea id="findingText" class="rv-finding-text">${esc(findingReportText)}</textarea>
      <div class="rv-finding-actions">
        <button class="rv-btn-secondary" onclick="copyFinding()">Copy</button>
        <button class="rv-btn-primary" onclick="downloadFinding()">Download .txt</button>
      </div>
      ${report.disclaimer ? `<div class="rv-disclaimer">${esc(report.disclaimer)}</div>` : ''}
    </div>
  `;
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

async function copyFinding() {
  const text = document.getElementById('findingText')?.value || '';
  await navigator.clipboard.writeText(text);
}

function downloadFinding() {
  const text = document.getElementById('findingText')?.value || '';
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `dia_finding_${currentRunId}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function fmt(n) { return n?.toLocaleString() ?? '—'; }
function fmtAmt(n) {
  if (!n) return '$0';
  const a = Math.abs(n);
  if (a >= 1e9) return `$${(n/1e9).toFixed(1)}B`;
  if (a >= 1e6) return `$${(n/1e6).toFixed(1)}M`;
  if (a >= 1e3) return `$${(n/1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}
function statusLabel(s) {
  return { pending:'Pending', confirmed:'Confirmed', false_positive:'False +', needs_info:'Needs Info' }[s] || s;
}
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function show(id) { const el = document.getElementById(id); if (el) el.style.display = ''; }

// ── Grounded chat copilot ────────────────────────────────────────────────────
// The backend (chat_agent.py) only lets the model parse intent; every number,
// filter, and evidence item below comes back from the same deterministic
// scoring/clustering the rest of this page uses. Nothing here is invented.
let chatBusy = false;

function resetChat() {
  const log = document.getElementById('rvChatLog');
  if (!log) return;
  log.innerHTML = `<div class="rv-chat-msg rv-chat-msg-bot">
    <strong>How can I help with this run?</strong>
    Ask about vendors, risk, evidence, patterns, or comparisons.
    <div class="rv-chat-prompts">
      <button onclick="askChat('Summarize this run')">Summarize this run</button>
      <button onclick="askChat('Top 5 vendors by highest risk')">Highest-risk vendors</button>
      <button onclick="askChat('What should I review first?')">What should I review first?</button>
      <button onclick="askChat('How many invoices have no actual payment date?')">Missing payment dates</button>
      <button onclick="askChat('How many invoices are pending review?')">Review status</button>
    </div>
  </div>`;
}

function toggleChat() {
  const panel = document.getElementById('rvChat');
  const open = panel.classList.toggle('open');
  document.getElementById('rvChatLauncher')?.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) document.getElementById('rvChatInput')?.focus();
}

function closeChat() {
  const panel = document.getElementById('rvChat');
  panel?.classList.remove('open', 'expanded');
  document.getElementById('rvChatLauncher')?.setAttribute('aria-expanded', 'false');
}

function toggleChatExpanded() {
  document.getElementById('rvChat')?.classList.toggle('expanded');
}

function appendChatMsg(html, cls) {
  const log = document.getElementById('rvChatLog');
  const div = document.createElement('div');
  div.className = `rv-chat-msg ${cls}`;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function submitChat(ev) {
  ev.preventDefault();
  if (chatBusy || !currentRunId) return;
  const input = document.getElementById('rvChatInput');
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  await askChat(message);
}

async function askChat(message) {
  if (chatBusy || !currentRunId) return;
  chatBusy = true;
  appendChatMsg(esc(message), 'rv-chat-msg-user');
  const btn = document.getElementById('rvChatSendBtn');
  btn.disabled = true;
  try {
    const res = await fetch(`/api/runs/${currentRunId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    const data = await res.json();
    renderChatResponse(data);
  } catch (e) {
    appendChatMsg(`✕ ${esc(e.message || 'Request failed')}`, 'rv-chat-msg-error');
  } finally {
    chatBusy = false;
    btn.disabled = false;
  }
}

function renderChatResponse(data) {
  if (!data || data.error) {
    appendChatMsg(`✕ ${esc(data?.error || data?.message || 'Something went wrong')}`, 'rv-chat-msg-error');
    return;
  }

  if (data.type === 'error') {
    appendChatMsg(`✕ ${esc(data.message)}`, 'rv-chat-msg-error');
    return;
  }

  if (data.type === 'clarify' || data.type === 'not_found') {
    appendChatMsg(esc(data.message), 'rv-chat-msg-clarify');
    return;
  }

  if (data.type === 'unsupported') {
    appendChatMsg(`
      ${esc(data.message)}
      <button class="rv-chat-draft-btn" onclick="draftFromChat('${esc(data.draft_prompt).replace(/'/g, "&#39;")}')">✦ Draft this as a new test</button>
    `, 'rv-chat-msg-clarify');
    return;
  }

  if (['run_stats_result', 'review_overview_result', 'review_case_result',
       'field_help_result', 'risk_help_result', 'invoice_field_result'].includes(data.type)) {
    const sources = (data.sources || []).map(s => esc(s)).join(' · ');
    const samples = data.type === 'run_stats_result' && data.sample_invoices?.length
      ? `<div style="margin-top:6px;font-size:12px;color:var(--text-muted)">Example invoices: ${data.sample_invoices.map(r => esc(r.invoice)).join(', ')}</div>` : '';
    appendChatMsg(`
      <strong>${esc(data.message)}</strong>
      ${data.missing_fields?.length ? `<div style="margin-top:6px">Missing required fields: ${data.missing_fields.map(esc).join(', ')}</div>` : ''}
      ${samples}
      ${sources ? `<div style="margin-top:7px;font-size:11px;color:var(--text-muted)">Based on: ${sources}</div>` : ''}
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'row_detail') {
    const ev = (data.evidence || []).map(e =>
      `<div class="rv-chat-group-row"><span>${esc(e.field)}</span><span>${esc(e.value)} — ${esc(e.note)}</span></div>`
    ).join('');
    appendChatMsg(`
      <strong>${esc(data.message)}</strong>
      ${ev ? `<div class="rv-chat-groups">${ev}</div>` : ''}
      ${data.sources?.length ? `<div style="margin-top:7px;font-size:11px;color:var(--text-muted)">Based on: ${data.sources.map(esc).join(' · ')}</div>` : ''}
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'list_tests_result') {
    const proj = data.tests || [];
    const rowsHtml = proj.map(t => `
      <div class="rv-chat-row-item" onclick="askChat('explain the ${esc(t.test_name)} test')">
        <span>${esc(t.test_name)}</span>
        <span>${t.hit_count > 0 ? `<strong>${t.hit_count}</strong> hits` : '<em>no hits</em>'}</span>
      </div>`).join('');
    appendChatMsg(`
      <strong>${esc(data.message)}</strong>
      <div class="rv-chat-rows" style="margin-top:6px">${rowsHtml || '<em>No tests found.</em>'}</div>
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'count_clusters_result') {
    appendChatMsg(`
      <strong>${esc(data.message)}</strong>
      <div class="rv-chat-groups" style="margin-top:6px">
        <div class="rv-chat-group-row"><span>Rule-based clusters</span><span>${data.rule_clusters}</span></div>
        <div class="rv-chat-group-row"><span>Multi-test clusters</span><span>${data.multi_test}</span></div>
        <div class="rv-chat-group-row"><span>Data-driven anomalies</span><span>${data.emergent}</span></div>
      </div>
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'explain_test_result') {
    const t = data.test;
    const rowsHtml = (data.rows || []).slice(0, 6).map(r => `
      <div class="rv-chat-row-item" onclick="askChat('why is invoice ${esc(r.physical_invoice_no || r.row_idx)} flagged?')">
        <span>${esc(r.vendor_name || r.vendor_number || '—')} · #${esc(r.physical_invoice_no || '')}</span>
        <span>${fmtAmt(r.amount_excl_lc)}</span>
      </div>`).join('');
    appendChatMsg(`
      <strong>${t ? esc(t.test_name) : 'Test'}</strong>
      <div style="margin:6px 0 4px;font-size:13px;color:var(--text-secondary)">${esc(data.message)}</div>
      ${rowsHtml ? `<div class="rv-chat-rows">${rowsHtml}</div>` : ''}
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'explain_vendor_result') {
    const evHtml = (data.evidence || []).map(e =>
      `<div class="rv-chat-group-row"><span>${esc(e.field)}</span><span>${esc(e.value)} — ${esc(e.note)}</span></div>`
    ).join('');
    const testsHtml = (data.test_names || []).map(n =>
      `<span class="rv-chip">${esc(n)}</span>`).join('');
    appendChatMsg(`
      <div style="font-weight:600;font-size:14px;margin-bottom:4px">${esc(data.vendor_name)}</div>
      <div style="font-size:13px;margin-bottom:8px;color:var(--text-secondary)">${esc(data.message)}</div>
      <div class="rv-chat-groups" style="margin-bottom:6px">
        <div class="rv-chat-group-row"><span>Flagged invoices</span><span>${data.row_count}</span></div>
        <div class="rv-chat-group-row"><span>Amount at risk</span><span>${fmtAmt(data.total_amount)}</span></div>
        <div class="rv-chat-group-row"><span>Highest risk score</span><span>${data.max_risk} (${esc(data.risk_band)})</span></div>
      </div>
      ${testsHtml ? `<div style="margin-bottom:6px;font-size:12px">${testsHtml}</div>` : ''}
      ${evHtml ? `<div class="rv-chat-groups">${evHtml}</div>` : ''}
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'filter_result') {
    const groupsHtml = (data.groups || []).slice(0, 10).map(g =>
      `<div class="rv-chat-group-row"><span>${esc(g.key)} (${g.row_count})</span><span>${fmtAmt(g.amount)}</span></div>`
    ).join('');
    // Projection-aware row rendering
    const proj = data.projection || null;
    const rowsHtml = (data.rows || []).slice(0, 8).map(r => {
      const label = esc(r.vendor_name || r.vendor_number || '—');
      if (proj && proj.includes('vendor_number')) {
        return `<div class="rv-chat-row-item">
          <span>${label}</span>
          <strong style="font-family:monospace">${esc(r.vendor_number || '—')}</strong>
        </div>`;
      }
      if (proj && proj.includes('risk_score')) {
        return `<div class="rv-chat-row-item">
          <span>${label}</span>
          <span>risk: <strong>${r.risk_score}</strong> (${esc(r.risk_band)})</span>
        </div>`;
      }
      if (proj && proj.includes('amount_excl_lc')) {
        return `<div class="rv-chat-row-item">
          <span>${label} · #${esc(r.physical_invoice_no || '')}</span>
          <strong>${fmtAmt(r.amount_excl_lc)}</strong>
        </div>`;
      }
      if (proj && proj.includes('flagged_by_names')) {
        return `<div class="rv-chat-row-item">
          <span>${label}</span>
          <span>${esc((r.flagged_by_names || []).join(', '))}</span>
        </div>`;
      }
      // Default: full row (risk-sort mode shows score prominently)
      const rowSort = data.row_sort || '';
      const rightCol = (rowSort === 'risk_desc' || rowSort === 'risk_asc')
        ? `<span>score: <strong>${r.risk_score}</strong> · ${esc(r.risk_band)}</span>`
        : `<span>${fmtAmt(r.amount_excl_lc)} · ${esc(r.risk_band)}</span>`;
      return `<div class="rv-chat-row-item" onclick="askChat('why is invoice ${esc(r.physical_invoice_no || r.row_idx)} flagged?')">
        <span>${label} · #${esc(r.physical_invoice_no || r.row_idx)}</span>
        ${rightCol}
      </div>`;
    }).join('');
    appendChatMsg(`
      <strong>${esc(data.message)}</strong>
      ${groupsHtml ? `<div class="rv-chat-groups">${groupsHtml}</div>` : ''}
      ${rowsHtml ? `<div class="rv-chat-rows">${rowsHtml}</div>` : ''}
      ${data.truncated ? `<div style="margin-top:6px;font-size:11px;color:var(--text-muted)">Showing first ${data.rows.length} of ${data.row_count} matching rows.</div>` : ''}
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'aggregate_result') {
    const metricLabel = { count: 'rows', amount: 'amount', avg_risk: 'avg risk', max_risk: 'max risk', min_risk: 'min risk' }[data.metric] || data.metric;
    if (data.group_by === 'vendor') {
      const cardsHtml = (data.entries || []).map((e, i) => {
        const vendorArg = esc(JSON.stringify(e.key));
        const primaryRisk = data.metric === 'avg_risk' ? e.avg_risk : e.max_risk;
        return `<div class="rv-chat-vendor-card">
          <div class="rv-chat-vendor-head">
            <div class="rv-chat-vendor-rank">#${i + 1} vendor</div>
            <div class="rv-chat-vendor-name">${esc(e.key)}</div>
          </div>
          <div class="rv-chat-vendor-stats">
            <div class="rv-chat-vendor-stat"><span>Risk score</span><strong>${esc(primaryRisk)}</strong></div>
            <div class="rv-chat-vendor-stat"><span>Amount</span><strong>${fmtAmt(e.amount)}</strong></div>
            <div class="rv-chat-vendor-stat"><span>Invoices</span><strong>${fmt(e.row_count)}</strong></div>
          </div>
          <div class="rv-chat-vendor-actions">
            <button onclick="openChatVendor(${vendorArg}, false)">View invoices</button>
            <button onclick="openChatVendor(${vendorArg}, true)">Show evidence</button>
          </div>
        </div>`;
      }).join('');
      appendChatMsg(`
        <strong>${esc(data.message)}</strong>
        ${cardsHtml || '<div style="margin-top:4px;font-size:12px;color:var(--text-muted)">No matching vendors.</div>'}
      `, 'rv-chat-msg-bot');
      return;
    }
    const rowsHtml = (data.entries || []).map((e, i) => {
      const val = data.metric === 'amount' ? fmtAmt(e.amount)
        : data.metric === 'count' ? `${e.row_count}`
        : data.metric === 'avg_risk' ? `${e.avg_risk}`
        : data.metric === 'max_risk' ? `${e.max_risk}`
        : `${e.min_risk}`;
      return `<div class="rv-chat-group-row"><span>${i + 1}. ${esc(e.key)}</span><span>${esc(val)} ${esc(metricLabel)}</span></div>`;
    }).join('');
    appendChatMsg(`
      <strong>${esc(data.message)}</strong>
      ${rowsHtml ? `<div class="rv-chat-groups">${rowsHtml}</div>` : '<div style="margin-top:4px;font-size:12px;color:var(--text-muted)">No matching groups.</div>'}
    `, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'summary_result') {
    appendChatMsg(`<strong>${esc(data.message)}</strong>`, 'rv-chat-msg-bot');
    return;
  }

  if (data.type === 'compare_result') {
    appendChatMsg(`<strong>${esc(data.message)}</strong>`, 'rv-chat-msg-bot');
    return;
  }

  appendChatMsg(esc(data.message || JSON.stringify(data)), 'rv-chat-msg-bot');
}

function draftFromChat(prompt) {
  if (!currentRunId) return;
  localStorage.setItem('dia_prefill_test', prompt);
  localStorage.setItem('dia_last_run', JSON.stringify({ runId: currentRunId }));
  window.location.href = '/?section=tests';
}

async function openChatVendor(vendor, showEvidence = false) {
  closeChat();
  flatLoaded = true;
  setView('flat');
  document.getElementById('vendorSearch').value = vendor;
  currentPage = 1;
  await fetchPage();
  if (showEvidence && allRows.length) {
    selectRow(allRows[0]);
  }
}
function hide(id) { const el = document.getElementById(id); if (el) el.style.display = 'none'; }
