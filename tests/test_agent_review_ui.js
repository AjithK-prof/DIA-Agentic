const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'dia_agentic', 'static', 'results.js'), 'utf8');
const context = {
  window: { fetch() {}, addEventListener() {} },
  document: { addEventListener() {} },
  localStorage: {},
  console,
};
vm.createContext(context);
vm.runInContext(source, context);

const base = {
  row_idx: 4, invoice: 'INV-4', vendor: '<img src=x onerror=alert(1)>',
  risk_score: 58, risk_band: 'High', amount: 1200, currency: 'USD',
  recommendation: 'confirm', review_status: 'pending', schema_version: 5,
  reason: 'Inspect invoice evidence', suggested_check: 'Compare the source document',
  trace: [], model: 'test',
};
const eligible = context.agentCaseHtml(base);
assert.match(eligible, /AGENT RECOMMENDATION/);
assert.match(eligible, /NEXT CHECK/);
assert.match(eligible, /Approve recommendation/);
assert.doesNotMatch(eligible, /<img/);
assert.match(eligible, /&lt;img/);
assert.doesNotMatch(context.agentCaseHtml({ ...base, recommendation: 'manual_review' }),
  /Approve recommendation/);
assert.doesNotMatch(context.agentCaseHtml({ ...base, schema_version: 4 }),
  /Approve recommendation/);
const decided = context.agentCaseHtml({ ...base, review_status: 'confirmed' });
assert.doesNotMatch(decided, /Approve recommendation/);
assert.match(decided, /Decision saved separately/);
console.log('Agent Review card rendering and approval visibility passed.');
