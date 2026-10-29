// ── Auth ───────────────────────────────────────────────────────────────────
function getToken() { return localStorage.getItem('dia_token') || ''; }

function authHeaders() {
  return { 'x-auth-token': getToken(), 'Content-Type': 'application/json' };
}

async function checkAuth() {
  const token = getToken();
  if (!token) { window.location.href = '/login'; return false; }
  const res = await fetch('/api/auth/me', { headers: { 'x-auth-token': token } });
  if (!res.ok) { localStorage.removeItem('dia_token'); window.location.href = '/login'; return false; }
  const user = await res.json();
  const displayName = user.display_name || user.username || '?';
  const el = document.getElementById('sbUsername');
  const av = document.getElementById('sbAvatar');
  if (el) el.textContent = displayName;
  if (av) av.textContent = displayName.charAt(0).toUpperCase();
  return true;
}

function logout() {
  fetch('/api/auth/logout', { method: 'POST', headers: { 'x-auth-token': getToken() } });
  localStorage.removeItem('dia_token');
  localStorage.removeItem('dia_user');
  localStorage.removeItem('dia_last_run');
  window.location.href = '/login';
}

// Wrap fetch to always include auth header
const _origFetch = window.fetch;
window.fetch = function(url, opts = {}) {
  if (typeof url === 'string' && url.startsWith('/api/')) {
    opts.headers = { ...(opts.headers || {}), 'x-auth-token': getToken() };
  }
  return _origFetch(url, opts);
};

// ── State ──────────────────────────────────────────────────────────────────
let llmOn = false;
let currentRunId = null;
let currentRunData = null;
let generatedTest = null;       // pending AI-generated test waiting to be added
let selectedPresets = new Set();
let addedTests = [];            // all tests selected for run (presets + generated)
let lastPrompt = '';
let mappingAcknowledged = false;

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  const ok = await checkAuth();
  if (!ok) return;
  loadHistory();
  if (new URLSearchParams(window.location.search).get('new_run') === '1') {
    localStorage.removeItem('dia_last_run');
    window.history.replaceState({}, '', '/');
  } else {
    restoreSession();
  }
  applyPrefillTest();
});

function applyPrefillTest() {
  const pre = localStorage.getItem('dia_prefill_test');
  if (!pre) return;
  localStorage.removeItem('dia_prefill_test');
  const ta = document.getElementById('aiPrompt');
  if (ta) {
    ta.value = pre;
    ta.scrollIntoView({ behavior: 'smooth', block: 'center' });
    ta.focus();
  }
}

async function restoreSession() {
  try {
    const saved = localStorage.getItem('dia_last_run');
    if (!saved) return;
    const { runId } = JSON.parse(saved);
    if (!runId) return;
    const res = await fetch(`/api/runs/${runId}`);
    if (!res.ok) { localStorage.removeItem('dia_last_run'); return; }
    const data = await res.json();
    if (data && data.run_id) {
      currentRunId = runId;
      currentRunData = data;
      renderAfterUpload(data);
      focusRequestedSection();
      const last = await fetch(`/api/runs/${runId}/tests/last`).catch(() => null);
      if (last && last.ok) {
        const sess = await last.json();
        if (sess && sess.results && sess.results.length) {
          renderResults(sess.results, sess.risk_summary);
          enableReviewLink(runId);
        }
      }
    }
  } catch {}
}

function focusRequestedSection() {
  if (new URLSearchParams(window.location.search).get('section') !== 'tests') return;
  const target = document.getElementById('secTests');
  if (!target) return;
  requestAnimationFrame(() => target.scrollIntoView({ behavior: 'smooth', block: 'start' }));
}

// ── Sidebar ────────────────────────────────────────────────────────────────
function sbNav(el, targetId) {
  document.querySelectorAll('.sb-link').forEach(l => l.classList.remove('active'));
  el.classList.add('active');
  const titles = {
    uploadCard: 'New analysis',
    secMapping: 'Schema mapping',
    secTests: 'Analytics tests',
    secResults: 'Results',
  };
  const title = document.getElementById('topbarTitle');
  if (title && titles[targetId]) title.textContent = titles[targetId];
  const target = document.getElementById(targetId);
  if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── History ────────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const runs = await fetch('/api/runs').then(r => r.json());
    const el = document.getElementById('sbHistory');
    if (!runs.length) { el.innerHTML = '<div style="padding:8px 8px;font-size:11px;color:#3d444d">No runs yet</div>'; return; }
    el.innerHTML = runs.slice(0, 8).map(r => `
      <div class="sb-history-item" onclick="loadRun('${r.run_id}')">
        <div class="hi-id">${r.run_id}</div>
        <div class="hi-file">${r.filename || '—'}</div>
        <div class="hi-rows">${(r.total_rows || 0).toLocaleString()} rows</div>
      </div>
    `).join('');
  } catch {}
}

async function loadRun(runId) {
  const data = await fetch(`/api/runs/${runId}`).then(r => r.json());
  if (!data || data.error) return;
  currentRunId = runId;
  currentRunData = data;
  disableReviewLink();
  renderAfterUpload(data);
  const last = await fetch(`/api/runs/${runId}/tests/last`).catch(() => null);
  if (last && last.ok) {
    const sess = await last.json();
    if (sess && sess.results && sess.results.length) {
      renderResults(sess.results, sess.risk_summary);
      enableReviewLink(runId);
    }
  }
}

// ── Drag & drop ────────────────────────────────────────────────────────────
const zone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');

zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
zone.addEventListener('drop', e => {
  e.preventDefault(); zone.classList.remove('dragover');
  const f = e.dataTransfer.files[0];
  if (f) setFile(f);
});
fileInput.addEventListener('change', () => { if (fileInput.files[0]) setFile(fileInput.files[0]); });

function setFile(f) {
  document.getElementById('dropHint').textContent = 'File selected:';
  document.getElementById('dropName').textContent = f.name;
  zone.style.borderColor = '#2563eb';
}

function toggleLLM() {
  llmOn = !llmOn;
  document.getElementById('llmToggle').classList.toggle('on', llmOn);
}

// ── Upload & Validate ──────────────────────────────────────────────────────
async function runValidation() {
  const file = fileInput.files[0];
  if (!file) { setStatus('Please choose a CSV file first.', 'err'); return; }

  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  setStatus('<span class="spinner"></span> Mapping columns...', '');

  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('use_llm', llmOn ? 'true' : 'false');
    fd.append('erp_type', document.getElementById('erpType').value);

    const res = await fetch('/api/validate', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Validation failed');

    currentRunId = data.run_id;
    currentRunData = data;

    localStorage.setItem('dia_last_run', JSON.stringify({ runId: data.run_id }));

    renderAfterUpload(data);
    loadHistory();
    setStatus(`<span class="ok">✓ Mapped ${data.total_rows.toLocaleString()} rows</span>`, '');
  } catch (e) {
    setStatus(`<span class="err">✕ ${e.message}</span>`, '');
  } finally {
    btn.disabled = false;
  }
}

function renderAfterUpload(data) {
  document.getElementById('sbRunInfo').style.display = '';
  document.getElementById('sbRunId').textContent = data.run_id;
  document.getElementById('sbRunFile').textContent = data.source_file || '—';
  document.getElementById('sbRunRows').textContent = `${(data.total_rows || 0).toLocaleString()} rows`;
  document.getElementById('newRunBtn').style.display = '';

  renderMapping(data);
  renderDataQuality(data.data_quality);
  renderTests(data);
  configureTestGate(data);
  loadCompareRuns();

  document.getElementById('secCompare').style.display = '';
  document.getElementById('secDQ').style.display = '';
  document.getElementById('secMapping').style.display = '';
  document.getElementById('secTests').style.display = '';
  document.getElementById('secResults').style.display = 'none';
}

function inferredMappingCount(data) {
  const statuses = Object.values(data?.schema_mapping?.status || {});
  return statuses.filter(s => s === 'inferred').length;
}

function configureTestGate(data) {
  mappingAcknowledged = false;
  const inferred = inferredMappingCount(data);
  const warning = document.getElementById('mappingGateWarning');
  const actions = document.getElementById('testGateActions');
  if (!warning || !actions) return;

  warning.style.display = inferred ? '' : 'none';
  actions.querySelectorAll('button').forEach(btn => { btn.disabled = inferred > 0; });
  document.getElementById('testGateTitle').textContent =
    inferred ? `${inferred} mapping${inferred === 1 ? '' : 's'} need review` : 'Ready to test';
  document.getElementById('testGateText').textContent = inferred
    ? 'Check the inferred fields above, then confirm once before running controls.'
    : 'Choose one path. DIA will run the tests, calculate risk, build the review queue, and take you there automatically.';
}

function confirmMappings() {
  mappingAcknowledged = true;
  document.getElementById('mappingGateWarning').style.display = 'none';
  document.querySelectorAll('#testGateActions button').forEach(btn => { btn.disabled = false; });
  document.getElementById('testGateTitle').textContent = 'Ready to test';
  document.getElementById('testGateText').textContent =
    'Mappings confirmed. Choose one path and DIA will complete the analysis.';
}

async function runCurrentTests() {
  if (inferredMappingCount(currentRunData) && !mappingAcknowledged) {
    document.getElementById('secMapping')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }
  await runAllTests({ openReview: true });
}

function suggestedTestsForRun() {
  const fields = new Set(Object.values(currentRunData?.schema_mapping?.mapped || {}));
  const suggestions = [];
  if (fields.has('captured_by') && fields.has('authorized_by')) {
    suggestions.push({
      name: 'Segregation of duties',
      detail: 'Same person captured and authorized an invoice',
      prompt: 'Find invoices where the same person captured and authorized the invoice, indicating a segregation of duties violation.',
    });
  }
  if (fields.has('date_documented') && (fields.has('payment_date') || fields.has('actual_pay_date'))) {
    suggestions.push({
      name: 'Payment before invoice',
      detail: 'Actual payment predates the documented invoice date',
      prompt: 'Find invoices where the actual payment date is before the documented invoice date. Use payment_date when present and actual_pay_date as the alternate actual-payment field.',
    });
  }
  if (fields.has('payment_date') || fields.has('actual_pay_date')) {
    suggestions.push({
      name: 'Weekend payments',
      detail: 'Actual payments executed on Saturday or Sunday',
      prompt: 'Find invoices whose actual payment date falls on a Saturday or Sunday. Use payment_date when present and actual_pay_date as the alternate actual-payment field.',
    });
  }
  if (fields.has('po_non_po') && fields.has('amount_excl_lc')) {
    suggestions.push({
      name: 'High-value non-PO invoices',
      detail: 'Non-PO invoices at or above 50,000',
      prompt: 'Find non-PO invoices where amount_excl_lc is at least 50000.',
    });
  }
  return suggestions.slice(0, 3);
}

function showSuggestedTests() {
  const wrap = document.getElementById('suggestedTests');
  const suggestions = suggestedTestsForRun();
  wrap.style.display = '';
  if (!suggestions.length) {
    wrap.innerHTML = '<div class="hint-text">No additional safe suggestions match the mapped fields. You can add your own test instead.</div>';
    return;
  }
  wrap.innerHTML = `
    <div class="ai-label">SUGGESTED FOR THIS DATASET</div>
    <div class="suggestion-list">${suggestions.map((s, i) => `
      <div class="suggestion-item">
        <div><strong>${esc(s.name)}</strong><span>${esc(s.detail)}</span></div>
        <button onclick="generateSuggestedTest(${i})">Create draft</button>
      </div>`).join('')}
    </div>`;
  wrap._suggestions = suggestions;
}

async function generateSuggestedTest(index) {
  const wrap = document.getElementById('suggestedTests');
  const suggestion = wrap?._suggestions?.[index];
  if (!suggestion) return;
  openTestBuilder();
  document.getElementById('aiPrompt').value = suggestion.prompt;
  await generateTest();
}

function openTestBuilder() {
  const panel = document.getElementById('aiBuilderPanel');
  const chev = document.getElementById('aiToggleChevron');
  panel.style.display = '';
  chev.textContent = '▴';
  panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
  document.getElementById('aiPrompt')?.focus();
}

// ── Schema Mapping ─────────────────────────────────────────────────────────
function renderMapping(data) {
  const sm = data.schema_mapping || {};
  const mapped = sm.mapped || {};
  const unmapped = sm.unmapped || [];
  const method = sm.method || {};
  const status = sm.status || {};

  const mc = Object.keys(mapped).length;
  const uc = unmapped.length;
  document.getElementById('mappingMeta').textContent = `${mc} mapped · ${uc} unmapped`;

  if (sm.erp_detected) {
    const b = document.getElementById('erpBanner');
    b.style.display = '';
    b.textContent = `ERP detected: ${sm.erp_detected} (${Math.round((sm.erp_confidence||0)*100)}% confidence)`;
  }

  const tbody = document.getElementById('mappingBody');
  let html = '';

  for (const [col, udm] of Object.entries(mapped)) {
    const m = method[col] || '—';
    const s = status[col] || 'confirmed';
    const sCls = s === 'confirmed' ? 'status-confirmed' : s === 'inferred' ? 'status-inferred' : 'status-unmapped';
    html += `<tr>
      <td><code style="font-size:12px">${esc(col)}</code></td>
      <td><strong>${esc(udm)}</strong></td>
      <td><span class="method-tag">${esc(m)}</span></td>
      <td><span class="${sCls}">${s}</span></td>
    </tr>`;
  }
  for (const col of unmapped) {
    html += `<tr>
      <td><code style="font-size:12px">${esc(col)}</code></td>
      <td style="color:var(--text-muted)">—</td>
      <td><span class="method-tag">unmapped</span></td>
      <td><span class="status-unmapped">unmapped</span></td>
    </tr>`;
  }

  tbody.innerHTML = html || '<tr><td colspan="4" class="empty">No mapping data</td></tr>';
}

// ── Tests Section ──────────────────────────────────────────────────────────
function renderTests(data) {
  const presets = data.preset_tests || [];
  selectedPresets = new Set();
  addedTests = [];
  generatedTest = null;

  const grid = document.getElementById('presetGrid');
  grid.innerHTML = presets.map((t, i) => {
    const na = !t.applicable;
    const checked = !na;
    if (checked) selectedPresets.add(t.id);
    return `
      <div class="preset-card ${na ? 'na' : 'selected'}" id="pcard_${t.id}" onclick="${na ? '' : `togglePreset('${t.id}')`}">
        <div class="pc-head">
          <input type="checkbox" class="pc-checkbox" id="pchk_${t.id}" ${checked ? 'checked' : ''} ${na ? 'disabled' : ''}
            onclick="event.stopPropagation();togglePreset('${t.id}')" />
          <span class="pc-name">
            ${esc(t.test_name)}
          </span>
        </div>
        <div class="pc-badges">
          <span class="badge badge-builtin">BUILT-IN</span>
          ${na ? '<span class="badge badge-gray">N/A</span>' : ''}
        </div>
        ${descToggle(t.explanation, 'pc-desc', 110)}
        ${na ? `<div class="pc-missing">Missing: ${(t.missing_fields||[]).join(', ')}</div>` : ''}
      </div>
    `;
  }).join('');

  syncMyTests();
  document.getElementById('testsMeta').textContent = `${presets.filter(t=>t.applicable).length} available`;
}

function togglePreset(id) {
  if (selectedPresets.has(id)) {
    selectedPresets.delete(id);
    document.getElementById(`pcard_${id}`)?.classList.remove('selected');
    document.getElementById(`pchk_${id}`).checked = false;
  } else {
    selectedPresets.add(id);
    document.getElementById(`pcard_${id}`)?.classList.add('selected');
    document.getElementById(`pchk_${id}`).checked = true;
  }
  syncMyTests();
}

function syncMyTests() {
  const presets = currentRunData?.preset_tests || [];
  const presetTests = presets.filter(t => selectedPresets.has(t.id));
  const allTests = [...presetTests, ...addedTests];
  const count = allTests.length;

  document.getElementById('testsCount').textContent = count;

  const wrap = document.getElementById('myTestsWrap');
  const chips = document.getElementById('chipsContainer');
  const runBtn = document.getElementById('runAllBtn');

  if (count === 0) { wrap.style.display = 'none'; runBtn.style.display = 'none'; return; }
  wrap.style.display = '';
  runBtn.style.display = '';
  runBtn.textContent = `▶ Run All Tests (${count} selected)`;

  chips.innerHTML = allTests.map((t, i) => {
    const isAI = t.type === 'generated';
    return `<span class="chip ${isAI ? 'chip-ai' : 'chip-builtin'}">
      ${isAI ? '✦' : '⊞'} ${esc(t.test_name)}
      ${isAI ? `<span class="chip-remove" onclick="removeGenTest(${i - presetTests.length})">✕</span>` : ''}
    </span>`;
  }).join('');
}

function removeGenTest(idx) {
  addedTests.splice(idx, 1);
  syncMyTests();
}

// ── AI Builder ─────────────────────────────────────────────────────────────
function toggleAIBuilder() {
  const panel = document.getElementById('aiBuilderPanel');
  const chev = document.getElementById('aiToggleChevron');
  const open = panel.style.display !== 'none';
  panel.style.display = open ? 'none' : '';
  chev.textContent = open ? '▾' : '▴';
}

async function generateTest() {
  const prompt = document.getElementById('aiPrompt').value.trim();
  if (!prompt) { document.getElementById('genStatus').textContent = 'Please enter a description.'; return; }
  if (!currentRunId) { document.getElementById('genStatus').textContent = 'Upload a file first.'; return; }

  lastPrompt = prompt;
  const btn = document.getElementById('generateBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Generating…';
  document.getElementById('genStatus').textContent = '';
  document.getElementById('genPreview').style.display = 'none';

  try {
    const res = await fetch(`/api/runs/${currentRunId}/tests/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Generation failed');

    generatedTest = data;
    showPreview(data);
  } catch (e) {
    document.getElementById('genStatus').textContent = `✕ ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<span>✦</span> Generate Test';
  }
}

function showPreview(t) {
  document.getElementById('previewName').textContent = t.test_name;
  document.getElementById('previewDesc').textContent = t.description;
  document.getElementById('previewExplanation').textContent = t.explanation;
  document.getElementById('previewCode').textContent = t.code;
  document.getElementById('previewCode').style.display = 'none';
  document.querySelector('.code-toggle')?.setAttribute('data-open', 'false');

  // Validation warnings from the semantic smoke-test
  const warnEl = document.getElementById('previewWarnings');
  const warns = t.validation_warnings || [];
  if (warns.length && warnEl) {
    warnEl.style.display = '';
    warnEl.innerHTML =
      `<div class="gen-validation-warn">` +
      `<strong>⚠ Accuracy check found ${warns.length} issue${warns.length > 1 ? 's' : ''}:</strong>` +
      `<ul style="margin:6px 0 0 16px;padding:0">` +
      warns.map(w => `<li>${esc(w)}</li>`).join('') +
      `</ul><div style="margin-top:6px;font-size:11px;color:#7c5a00">` +
      `You can still add this test, but review the code and consider regenerating.` +
      `</div></div>`;
  } else if (warnEl) {
    warnEl.style.display = 'none';
    warnEl.innerHTML = '';
  }

  document.getElementById('genPreview').style.display = '';
}

function togglePreviewCode(el) {
  const code = document.getElementById('previewCode');
  const open = el.getAttribute('data-open') === 'true';
  code.style.display = open ? 'none' : '';
  el.textContent = open ? 'Show Code ▾' : 'Hide Code ▴';
  el.setAttribute('data-open', !open);
}

function addGeneratedTest() {
  if (!generatedTest) return;
  const id = 'gen_' + Date.now();
  addedTests.push({ ...generatedTest, id });
  generatedTest = null;
  document.getElementById('genPreview').style.display = 'none';
  document.getElementById('aiPrompt').value = '';
  syncMyTests();
}

function discardGenerated() {
  generatedTest = null;
  document.getElementById('genPreview').style.display = 'none';
}

async function regenerateTest() {
  if (!lastPrompt) return;
  document.getElementById('aiPrompt').value = lastPrompt;
  await generateTest();
}

// ── Run Tests ──────────────────────────────────────────────────────────────
async function runAllTests({ openReview = false } = {}) {
  if (!currentRunId) return;

  const presets = currentRunData?.preset_tests || [];
  const testsToRun = [
    ...presets.filter(t => selectedPresets.has(t.id)),
    ...addedTests,
  ];

  if (!testsToRun.length) { setRunStatus('No tests selected.'); return; }

  const btn = document.getElementById('runAllBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Running tests…';
  setRunStatus('');

  try {
    const res = await fetch(`/api/runs/${currentRunId}/tests/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tests: testsToRun }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Run failed');

    renderResults(data.results, data.risk_summary);
    document.getElementById('secResults').style.display = '';
    document.getElementById('secResults').scrollIntoView({ behavior: 'smooth', block: 'start' });
    setRunStatus(`<span style="color:var(--green)">✓ Done — ${data.results.length} tests ran</span>`);
    enableReviewLink(currentRunId);
    if (openReview) {
      window.location.href = `/results?run_id=${currentRunId}`;
      return;
    }
  } catch (e) {
    setRunStatus(`<span style="color:var(--red)">✕ ${e.message}</span>`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `▶ Run All Tests (${testsToRun.length} selected)`;
  }
}

function enableReviewLink(runId) {
  const el = document.getElementById('sbReviewLink');
  if (!el) return;
  el.classList.add('enabled');
  el._runId = runId;
}

function disableReviewLink() {
  const el = document.getElementById('sbReviewLink');
  if (!el) return;
  el.classList.remove('enabled');
  el._runId = null;
}

function goToReview(event) {
  event?.preventDefault();
  const el = document.getElementById('sbReviewLink');
  let runId = el?._runId || currentRunId;
  if (!runId) {
    try {
      runId = JSON.parse(localStorage.getItem('dia_last_run') || '{}').runId;
    } catch {}
  }
  window.location.href = runId ? `/results?run_id=${encodeURIComponent(runId)}` : '/results';
  return false;
}

function setRunStatus(html) {
  document.getElementById('runStatus').innerHTML = html;
}

// ── Results store for drill-down ───────────────────────────────────────────
let _lastResults = [];
const DRILL_PAGE = 50;
let _drillPages = {};   // testId -> current page index

// ── Results Rendering ──────────────────────────────────────────────────────
function renderResults(results, risk) {
  _lastResults = results;
  _drillPages = {};

  document.getElementById('kpiHitRows').textContent = (risk.total_hit_rows || 0).toLocaleString();
  document.getElementById('kpiCritical').textContent = risk.critical || 0;
  document.getElementById('kpiHigh').textContent = risk.high || 0;
  document.getElementById('kpiMedium').textContent = risk.medium || 0;
  document.getElementById('kpiLow').textContent = risk.low || 0;

  let totalAmt = 0;
  for (const r of results) {
    if (r.applicable && r.hit_amount) totalAmt += r.hit_amount;
  }
  document.getElementById('kpiAmount').textContent = formatAmt(totalAmt);

  const sorted = [...results].sort((a, b) => {
    return (b.hit_count || 0) - (a.hit_count || 0);
  });

  const hitTests   = sorted.filter(r => r.applicable && r.hit_count > 0);
  const noHitTests = sorted.filter(r => r.applicable && r.hit_count === 0);
  const naTests    = sorted.filter(r => !r.applicable);

  document.getElementById('resultsMeta').textContent =
    `${hitTests.length} tests with hits · ${noHitTests.length} clean · ${naTests.length} N/A`;

  const container = document.getElementById('resultCardsContainer');
  container.innerHTML = [...hitTests, ...noHitTests, ...naTests].map(r => buildResultCard(r)).join('');

  document.getElementById('secResults').style.display = '';
}

function buildResultCard(r) {
  const isAI    = r.type === 'generated';
  const hasHits = r.applicable && r.hit_count > 0;
  const badgeClass  = isAI ? 'badge-ai' : 'badge-builtin';
  const badgeLabel  = isAI ? '✦ AI GENERATED' : '⊞ BUILT-IN';

  const explanationHtml = r.explanation
    ? `<div class="rc-explanation">${esc(r.explanation)}</div>`
    : '';

  const codeHtml = r.code
    ? `<span class="rc-code-toggle" onclick="toggleRCCode(this)">Show Code ▾</span>
       <div class="code-block" style="display:none">${esc(r.code)}</div>`
    : '';

  let bodyContent = '';
  if (!r.applicable) {
    bodyContent = `<div style="color:var(--text-muted);font-size:13px;padding:6px 0">${esc(r.not_applicable_reason || 'N/A')}</div>`;
  } else if (r.hit_count === 0) {
    bodyContent = `<div style="color:var(--green);font-size:13px;padding:6px 0">✓ No hits — this test found nothing suspicious</div>`;
  } else {
    // Pre-build the drill table so rows are visible immediately
    const drillRows = (r.sample_hits || []).slice(0, DRILL_PAGE).map(h => `
      <tr>
        <td style="white-space:nowrap"><strong>${esc(h.inv_id || '—')}</strong></td>
        <td style="white-space:nowrap">${esc(h.vendor_name || '—')}</td>
        <td><code style="font-size:11px">${esc(h.physical_invoice_no || '—')}</code></td>
        <td style="white-space:nowrap">${esc(h.date_authorized || '—')}</td>
        <td style="text-align:right;font-weight:700;white-space:nowrap">${esc(h.amount_excl_lc || '—')} <span style="font-size:10px;color:var(--text-muted)">${esc(h.reference_currency||'')}</span></td>
        <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-secondary)">${esc(h.invoice_description || '—')}</td>
      </tr>
    `).join('');

    const totalSamples = (r.sample_hits || []).length;
    const paginationHtml = totalSamples > DRILL_PAGE
      ? `<div style="display:flex;align-items:center;gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
           <span class="hint-text">Showing ${DRILL_PAGE} of ${r.hit_count.toLocaleString()} flagged rows</span>
           <button class="btn-page" onclick="drillNextPage('${r.test_id}')">Load more ▾</button>
         </div>`
      : `<div class="hint-text" style="margin-top:8px">Showing all ${totalSamples} flagged rows${r.hit_count > totalSamples ? ` (${r.hit_count.toLocaleString()} total hits)` : ''}</div>`;

    bodyContent = `
      ${explanationHtml}
      ${codeHtml}
      <div class="drill-section" id="drill_${r.test_id}">
        <div style="background:#fff;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-top:12px;margin-bottom:4px">
          <div style="padding:10px 14px;background:#f8fafc;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px">
            <span style="font-size:12px;font-weight:700;color:var(--text-primary)">Flagged Rows</span>
            <span class="badge badge-gray">${r.hit_count.toLocaleString()} hits</span>
            <span style="flex:1"></span>
            <span class="hint-text">${r.hit_pct}% of dataset · ${formatAmt(r.hit_amount)} at risk</span>
          </div>
          <div style="overflow-x:auto">
            <table>
              <thead><tr>
                <th>Invoice ID</th><th>Vendor Name</th><th>Invoice No</th>
                <th>Date</th><th style="text-align:right">Amount</th><th>Description</th>
              </tr></thead>
              <tbody id="drill_tbody_${r.test_id}">${drillRows}</tbody>
            </table>
          </div>
        </div>
        <div id="drill_pagination_${r.test_id}">${paginationHtml}</div>
      </div>
    `;
  }

  if (!hasHits) {
    bodyContent = `
      ${explanationHtml}
      ${codeHtml}
      ${bodyContent}
    `;
  }

  // Cards with hits start expanded; no-hit/NA cards start collapsed
  const startOpen = hasHits;

  return `
    <div class="result-card" id="rc_${r.test_id}">
      <div class="rc-head ${startOpen ? 'open' : ''}" onclick="toggleRC(this)">
        <div style="display:flex;flex-direction:column;gap:4px;flex:1;min-width:0">
          <div class="rc-name">
            ${esc(r.test_name)}
          </div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <span class="badge ${badgeClass}">${badgeLabel}</span>
          </div>
        </div>
        ${hasHits ? `
        <div class="rc-stats">
          <div class="rc-stat">
            <div class="rc-stat-val">${r.hit_count.toLocaleString()}</div>
            <div class="rc-stat-label">hits</div>
          </div>
          <div class="rc-stat">
            <div class="rc-stat-val">${r.hit_pct}%</div>
            <div class="rc-stat-label">of rows</div>
          </div>
          <div class="rc-stat">
            <div class="rc-stat-val">${formatAmt(r.hit_amount)}</div>
            <div class="rc-stat-label">amount</div>
          </div>
        </div>` : `<div style="color:var(--text-muted);font-size:12px;margin-right:8px">${r.applicable ? 'No hits' : 'N/A'}</div>`}
        <span class="rc-chevron" style="transform:${startOpen ? 'rotate(180deg)' : ''}">▾</span>
      </div>
      <div class="rc-body" style="display:${startOpen ? '' : 'none'}">
        ${bodyContent}
      </div>
    </div>
  `;
}

function toggleRC(head) {
  const body = head.nextElementSibling;
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : '';
  head.classList.toggle('open', !open);
  head.querySelector('.rc-chevron').style.transform = open ? '' : 'rotate(180deg)';
}

function toggleRCCode(el) {
  const code = el.nextElementSibling;
  const open = code.style.display !== 'none';
  code.style.display = open ? 'none' : '';
  el.textContent = open ? 'Show Code ▾' : 'Hide Code ▴';
}

// ── Drill pagination (load more) ───────────────────────────────────────────
function toggleDrill(testId, evt) {
  evt.stopPropagation();
  const section = document.getElementById(`drill_${testId}`);
  const btn = evt.target;
  const r = _lastResults.find(x => x.test_id === testId);
  if (!r) return;

  if (section.style.display !== 'none') {
    section.style.display = 'none';
    btn.textContent = `Inspect ${r.hit_count.toLocaleString()} flagged rows ▾`;
    return;
  }

  _drillPages[testId] = 0;
  renderDrillPage(testId, r);
  section.style.display = '';
  btn.textContent = `Hide rows ▴`;
}

function renderDrillPage(testId, r) {
  const section = document.getElementById(`drill_${testId}`);
  if (!section) return;

  const hits = r.sample_hits || [];
  const page = _drillPages[testId] || 0;
  const start = page * DRILL_PAGE;
  const end   = Math.min(start + DRILL_PAGE, hits.length);
  const pageHits = hits.slice(start, end);
  const totalShown = end;
  const totalHits  = r.hit_count;

  const rows = pageHits.map(h => `
    <tr>
      <td style="white-space:nowrap"><strong>${esc(h.inv_id || '—')}</strong></td>
      <td style="white-space:nowrap">${esc(h.vendor_name || '—')}</td>
      <td><code style="font-size:11px">${esc(h.physical_invoice_no || '—')}</code></td>
      <td style="white-space:nowrap">${esc(h.date_authorized || '—')}</td>
      <td style="text-align:right;font-weight:700;white-space:nowrap">${esc(h.amount_excl_lc || '—')} <span style="font-size:10px;color:var(--text-muted)">${esc(h.reference_currency||'')}</span></td>
      <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-secondary)">${esc(h.invoice_description || '—')}</td>
    </tr>
  `).join('');

  const paginationHtml = totalShown < hits.length ? `
    <div style="display:flex;align-items:center;gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
      <span class="hint-text">Showing ${totalShown} of ${totalHits.toLocaleString()} flagged rows${totalHits > 50 ? ' (top 50 returned by server)' : ''}</span>
      ${end < hits.length ? `<button class="btn-page" onclick="drillNextPage('${testId}')">Load more ▾</button>` : ''}
    </div>
  ` : `<div class="hint-text" style="margin-top:8px">Showing all ${totalShown} flagged rows</div>`;

  section.innerHTML = `
    <div style="background:#fff;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-top:12px;margin-bottom:4px">
      <div style="padding:10px 14px;background:#f8fafc;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px">
        <span style="font-size:12px;font-weight:700;color:var(--text-primary)">Flagged Rows</span>
        <span class="badge badge-gray">${r.hit_count.toLocaleString()} hits</span>
        <span style="flex:1"></span>
        <span class="hint-text">${r.hit_pct}% of dataset · ${formatAmt(r.hit_amount)} at risk</span>
      </div>
      <div style="overflow-x:auto">
        <table>
          <thead>
            <tr>
              <th>Invoice ID</th>
              <th>Vendor Name</th>
              <th>Invoice No</th>
              <th>Date</th>
              <th style="text-align:right">Amount</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody id="drill_tbody_${testId}">${rows}</tbody>
        </table>
      </div>
    </div>
    <div id="drill_pagination_${testId}">${paginationHtml}</div>
  `;
}

function drillNextPage(testId) {
  const r = _lastResults.find(x => x.test_id === testId);
  if (!r) return;
  _drillPages[testId] = (_drillPages[testId] || 0) + 1;

  const hits = r.sample_hits || [];
  const page  = _drillPages[testId];
  const start = page * DRILL_PAGE;
  const end   = Math.min(start + DRILL_PAGE, hits.length);
  const pageHits = hits.slice(start, end);

  const tbody = document.getElementById(`drill_tbody_${testId}`);
  if (tbody) {
    pageHits.forEach(h => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td style="white-space:nowrap"><strong>${esc(h.inv_id || '—')}</strong></td>
        <td style="white-space:nowrap">${esc(h.vendor_name || '—')}</td>
        <td><code style="font-size:11px">${esc(h.physical_invoice_no || '—')}</code></td>
        <td style="white-space:nowrap">${esc(h.date_authorized || '—')}</td>
        <td style="text-align:right;font-weight:700;white-space:nowrap">${esc(h.amount_excl_lc || '—')} <span style="font-size:10px;color:var(--text-muted)">${esc(h.reference_currency||'')}</span></td>
        <td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-secondary)">${esc(h.invoice_description || '—')}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  const pagination = document.getElementById(`drill_pagination_${testId}`);
  if (pagination) {
    const totalShown = end;
    pagination.innerHTML = totalShown < hits.length
      ? `<div style="display:flex;align-items:center;gap:10px;margin-top:10px;padding-top:10px;border-top:1px solid var(--border)">
           <span class="hint-text">Showing ${totalShown} of ${r.hit_count.toLocaleString()} flagged rows</span>
           <button class="btn-page" onclick="drillNextPage('${testId}')">Load more ▾</button>
         </div>`
      : `<div class="hint-text" style="margin-top:8px">All ${totalShown} rows shown</div>`;
  }
}

// ── Section toggle ─────────────────────────────────────────────────────────
function toggleSection(head) {
  const body = head.nextElementSibling;
  const open = head.classList.toggle('open');
  body.style.display = open ? '' : 'none';
  head.querySelector('.chevron').style.transform = open ? 'rotate(0deg)' : 'rotate(-90deg)';
}

// ── New run ────────────────────────────────────────────────────────────────
function startNewRun() {
  currentRunId = null;
  currentRunData = null;
  generatedTest = null;
  selectedPresets = new Set();
  addedTests = [];
  localStorage.removeItem('dia_last_run');
  disableReviewLink();
  setRunStatus('');
  document.getElementById('secCompare').style.display = 'none';
  document.getElementById('secDQ').style.display = 'none';
  document.getElementById('secMapping').style.display = 'none';
  document.getElementById('secTests').style.display = 'none';
  document.getElementById('secResults').style.display = 'none';
  document.getElementById('sbRunInfo').style.display = 'none';
  document.getElementById('newRunBtn').style.display = 'none';
  document.getElementById('dropName').textContent = '';
  document.getElementById('dropHint').textContent = 'Drop invoice data here, or click to browse';
  document.getElementById('statusMsg').textContent = '';
  fileInput.value = '';
  document.getElementById('dropZone').style.borderColor = '';
  uploadCard.scrollIntoView({ behavior: 'smooth' });
}

// ── Utilities ──────────────────────────────────────────────────────────────
function setStatus(html) {
  document.getElementById('statusMsg').innerHTML = html;
}

function esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function formatAmt(val) {
  const n = parseFloat(val) || 0;
  if (n >= 1_000_000) return '$' + (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return '$' + (n / 1_000).toFixed(1) + 'K';
  return '$' + n.toFixed(0);
}

// ── Feature 1: Data Quality ────────────────────────────────────────────────
function renderDataQuality(dq) {
  if (!dq || !dq.field_stats) return;

  // Meta
  const q = dq.overall_completeness;
  document.getElementById('dqMeta').textContent =
    `${q}% complete · ${dq.bad_fields.length} critical fields · ${Object.keys(dq.currency_mix).length} currencies`;

  // KPI strip
  const strip = document.getElementById('dqSummaryStrip');
  strip.innerHTML = `
    <div class="kpi-card"><div class="kpi-label">Overall Complete</div><div class="kpi-val" style="color:${q>=90?'var(--green)':q>=70?'var(--amber)':'var(--red)'}">${q}%</div><div class="kpi-sub">${q>=90?'Healthy':q>=70?'Fair':'Needs attention'}</div></div>
    <div class="kpi-card"><div class="kpi-label">Total Rows</div><div class="kpi-val">${(dq.total_rows||0).toLocaleString()}</div></div>
    <div class="kpi-card"><div class="kpi-label">Fields Mapped</div><div class="kpi-val">${dq.total_fields}</div></div>
    <div class="kpi-card" style="${dq.bad_fields.length?'border-color:var(--red);border-width:2px':''}"><div class="kpi-label">Critical Gaps</div><div class="kpi-val" style="color:${dq.bad_fields.length?'var(--red)':'var(--green)'}">${dq.bad_fields.length}</div><div class="kpi-sub" style="color:${dq.bad_fields.length?'var(--red)':'var(--green)'}">${dq.bad_fields.length?'Fields missing':'All present'}</div></div>
    <div class="kpi-card" style="${dq.warn_fields.length?'border-color:var(--amber)':''}"><div class="kpi-label">Warnings</div><div class="kpi-val" style="color:${dq.warn_fields.length?'var(--amber)':'var(--text-muted)'}">${dq.warn_fields.length}</div><div class="kpi-sub">${dq.warn_fields.length?'Review fields':'No issues'}</div></div>
  `;

  // Currency mix
  const curr = dq.currency_mix || {};
  if (Object.keys(curr).length) {
    document.getElementById('dqCurrency').innerHTML = `
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--text-muted);margin-bottom:6px">Currency Mix</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        ${Object.entries(curr).map(([c,n]) => `<span class="badge badge-blue">${esc(c)}: ${n.toLocaleString()}</span>`).join('')}
      </div>`;
  }

  // Field table — show only UDM fields with data, sorted worst first
  const importantFields = ['amount_excl_lc','date_authorized','date_captured','scheduled_pay_date',
    'vendor_number','vendor_name','physical_invoice_no','invoice_description','authorized_by','captured_by'];
  const stats = dq.field_stats || {};
  const sorted = Object.entries(stats)
    .filter(([f]) => importantFields.includes(f) || (stats[f].null_pct > 0))
    .sort((a,b) => b[1].null_pct - a[1].null_pct);

  const tbody = document.getElementById('dqBody');
  tbody.innerHTML = sorted.map(([field, s]) => {
    const barColor = s.quality === 'good' ? 'var(--green)' : s.quality === 'warn' ? 'var(--amber)' : 'var(--red)';
    const pillCls = s.quality === 'good' ? 'good' : s.quality === 'warn' ? 'warn' : 'poor';
    const qLabel = s.quality === 'good' ? '✓ Good' : s.quality === 'warn' ? '⚠ Warn' : '✕ Poor';
    const barW = Math.max(2, s.filled_pct);

    let notes = '';
    if (s.date_formats && !s.format_consistent)
      notes += `<span class="badge badge-amber">Mixed formats: ${Object.keys(s.date_formats).join(', ')}</span> `;
    if (s.sentinel_count > 0)
      notes += `<span class="badge badge-gray">${s.sentinel_count} sentinel dates</span> `;
    if (s.zero_count > 0)
      notes += `<span class="badge badge-gray">${s.zero_count} zero amounts</span> `;
    if (s.negative_count > 0)
      notes += `<span class="badge badge-amber">${s.negative_count} negatives</span> `;
    if (s.dominant_format)
      notes += `<span class="badge badge-blue">${esc(s.dominant_format)}</span>`;

    return `<tr>
      <td><strong>${esc(field)}</strong></td>
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <div style="width:80px;height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden">
            <div style="width:${barW}%;height:100%;background:${barColor};border-radius:3px"></div>
          </div>
          <span style="font-size:12px;font-weight:600">${s.filled_pct}%</span>
        </div>
      </td>
      <td style="color:var(--text-muted);font-size:12px">${s.null_count.toLocaleString()}</td>
      <td><span class="dq-pill ${pillCls}">${qLabel}</span></td>
      <td style="font-size:12px">${notes || '—'}</td>
    </tr>`;
  }).join('');
}

// ── Feature 2: Multi-file comparison ──────────────────────────────────────
async function loadCompareRuns() {
  try {
    const runs = await fetch('/api/runs').then(r => r.json());
    const sel = document.getElementById('compareRunSelect');
    sel.innerHTML = '<option value="">Compare against a previous run…</option>';
    runs.filter(r => r.run_id !== currentRunId).forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.run_id;
      opt.textContent = `${r.filename || r.run_id} — ${(r.total_rows||0).toLocaleString()} rows`;
      sel.appendChild(opt);
    });
  } catch {}
}

async function runCompare() {
  const runIdOld = document.getElementById('compareRunSelect').value;
  const fileInput = document.getElementById('compareFileInput');
  const container = document.getElementById('compareResults');

  if (!runIdOld && !fileInput.files[0]) {
    container.innerHTML = '<div class="hint-text">Select a previous run or upload a file to compare.</div>';
    return;
  }
  if (!currentRunId) {
    container.innerHTML = '<div class="hint-text">Upload and map a file first.</div>';
    return;
  }

  container.innerHTML = '<span class="spinner"></span> Comparing…';

  try {
    let data;
    if (runIdOld) {
      // Compare current run rows against old run via backend
      // We send current file again with old run_id
      const fd = new FormData();
      // Re-use current file if available, else send dummy
      const curFile = document.getElementById('fileInput').files[0];
      if (!curFile) { container.innerHTML = '<div class="hint-text">Please re-upload the current file to compare.</div>'; return; }
      fd.append('file_new', curFile);
      fd.append('run_id_old', runIdOld);
      const res = await fetch('/api/compare', { method: 'POST', body: fd });
      data = await res.json();
    } else {
      const fd = new FormData();
      fd.append('file_new', fileInput.files[0]);
      fd.append('run_id_old', currentRunId);
      // Swap: compare uploaded file against current run
      const res = await fetch('/api/compare', { method: 'POST', body: fd });
      data = await res.json();
    }
    renderCompare(data, container);
  } catch(e) {
    container.innerHTML = `<div style="color:var(--red);font-size:13px">Error: ${esc(e.message)}</div>`;
  }
}

function renderCompare(d, container) {
  const vendorRows = (d.vendor_changes || []).slice(0, 10).map(v => {
    const dir = v.direction === 'up' ? '↑' : v.direction === 'down' ? '↓' : v.direction === 'new' ? '🆕' : '🗑';
    const col = v.direction === 'up' || v.direction === 'new' ? 'var(--red)' : v.direction === 'down' ? 'var(--green)' : 'var(--text-muted)';
    return `<tr>
      <td>${esc(v.vendor)}</td>
      <td style="text-align:right">${formatAmt(v.old_total)}</td>
      <td style="text-align:right">${formatAmt(v.new_total)}</td>
      <td style="text-align:right;font-weight:700;color:${col}">${dir} ${Math.abs(v.change_pct)}%</td>
    </tr>`;
  }).join('');

  container.innerHTML = `
    <div class="kpi-strip" style="margin-bottom:14px">
      <div class="kpi-card"><div class="kpi-label">New Invoices</div><div class="kpi-val" style="color:var(--red)">${(d.added_count||0).toLocaleString()}</div></div>
      <div class="kpi-card"><div class="kpi-label">Changed</div><div class="kpi-val" style="color:var(--amber)">${(d.changed_count||0).toLocaleString()}</div></div>
      <div class="kpi-card"><div class="kpi-label">Removed</div><div class="kpi-val" style="color:var(--text-muted)">${(d.removed_count||0).toLocaleString()}</div></div>
    </div>
    ${vendorRows ? `
    <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:var(--text-muted);margin-bottom:8px">Vendor Amount Changes (&gt;20%)</div>
    <div style="overflow-x:auto;border:1px solid var(--border);border-radius:8px">
      <table>
        <thead><tr><th>Vendor</th><th style="text-align:right">Previous</th><th style="text-align:right">Current</th><th style="text-align:right">Change</th></tr></thead>
        <tbody>${vendorRows}</tbody>
      </table>
    </div>` : '<div class="hint-text">No significant vendor changes detected.</div>'}
  `;
}

// ── Feature 3: Saved test library ─────────────────────────────────────────
let _library = [];

async function toggleLibrary() {
  const panel = document.getElementById('libraryPanel');
  const chev  = document.getElementById('libraryChevron');
  const open  = panel.style.display !== 'none';
  panel.style.display = open ? 'none' : '';
  chev.textContent = open ? '▾' : '▴';
  if (!open) await loadLibrary();
}

async function loadLibrary() {
  _library = await fetch('/api/tests/library').then(r => r.json());
  const container = document.getElementById('libraryList');
  if (!_library.length) {
    container.innerHTML = '<div class="hint-text">No saved tests yet. Generate an AI test and click "Save to Library".</div>';
    return;
  }
  container.innerHTML = _library.map(t => `
    <div style="display:flex;align-items:center;gap:8px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;background:#fff">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:700;color:var(--text-primary)">${esc(t.name)}</div>
        ${descToggle(t.description, 'lib-desc', 90)}
      </div>
      <button class="btn-add" onclick="addLibraryTest(${t.id})" style="font-size:11px;padding:5px 10px">+ Add</button>
      <button class="btn-discard" onclick="deleteLibraryTest(${t.id})" style="font-size:11px;padding:5px 10px">✕</button>
    </div>
  `).join('');
}

function addLibraryTest(id) {
  const t = _library.find(x => x.id === id);
  if (!t) return;
  const test = {
    id: 'lib_' + id + '_' + Date.now(),
    test_name: t.name,
    type: 'generated',
    code: t.code,
    description: t.description,
    explanation: t.explanation,
    severity: t.severity,
    weightage: t.weightage,
    prompt: t.prompt,
  };
  addedTests.push(test);
  syncMyTests();
}

async function deleteLibraryTest(id) {
  await fetch(`/api/tests/library/${id}`, { method: 'DELETE' });
  await loadLibrary();
}

async function saveCurrentTestToLibrary() {
  if (!generatedTest) return;
  await fetch('/api/tests/library', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(generatedTest),
  });
  alert(`"${generatedTest.test_name}" saved to library.`);
}

// ── Feature 5: Export ──────────────────────────────────────────────────────
function exportFindings(status = 'confirmed') {
  if (!currentRunId) return;
  window.location.href = `/api/runs/${currentRunId}/export?status=${status}`;
}

// ── Description with Show more/less toggle ─────────────────────────────────
function descToggle(text, cls = 'pc-desc', limit = 120) {
  const t = String(text || '').trim();
  if (!t) return `<div class="${cls}">—</div>`;
  if (t.length <= limit) return `<div class="${cls}">${esc(t)}</div>`;
  return `<div class="${cls} desc-clamp">` +
    `<span class="desc-short">${esc(t.substring(0, limit))}… </span>` +
    `<span class="desc-full" style="display:none">${esc(t)} </span>` +
    `<span class="desc-toggle" onclick="event.stopPropagation();toggleDesc(this)">Show more</span>` +
    `</div>`;
}

function toggleDesc(el) {
  const wrap = el.closest('.desc-clamp');
  if (!wrap) return;
  const short = wrap.querySelector('.desc-short');
  const full  = wrap.querySelector('.desc-full');
  const collapsed = short.style.display !== 'none';
  short.style.display = collapsed ? 'none' : '';
  full.style.display  = collapsed ? '' : 'none';
  el.textContent = collapsed ? 'Show less' : 'Show more';
}
