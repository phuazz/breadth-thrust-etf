// Run with node --test tests/test_execution_timing_display.cjs.
// JavaScript months are 0-indexed; all dates below use ISO strings.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'template.html'), 'utf8');
const data = JSON.parse(fs.readFileSync(path.join(root, 'data/execution_timing.json'), 'utf8'));
const code = html.slice(html.indexOf('function _execSession('), html.indexOf('function renderExecutionTiming()'));
const context = vm.createContext({Intl, Date});
vm.runInContext(code, context);
const session = (v,s,d,e='close',payload=data) => context._execSession(payload,v,s,d,e);

test('both venues carry their own regimes and Singapore weekday rollover', () => {
  assert.equal(session('XETR','summer','2026-09-14').label,'Mon 23:30 SGT');
  assert.equal(session('XETR','winter','2026-09-14').label,'Tue 00:30 SGT');
  assert.equal(session('US','summer','2026-09-14').label,'Tue 04:00 SGT');
  assert.equal(session('US','winter','2026-09-14').label,'Tue 05:00 SGT');
});
test('month boundary rollover follows Date, not a weekday string append', () => {
  assert.equal(session('XETR','winter','2026-10-31').day,'2026-11-01');
  assert.equal(session('XETR','winter','2026-10-31').label,'Sun 00:30 SGT');
});
test('year boundary rollover follows Date', () => {
  assert.equal(session('US','winter','2026-12-31').day,'2027-01-01');
  assert.equal(session('US','winter','2026-12-31').label,'Fri 05:00 SGT');
});
test('clock-change mismatch is representable independently per venue', () => {
  assert.equal(session('XETR','winter','2026-10-30').label,'Sat 00:30 SGT');
  assert.equal(session('US','summer','2026-10-30').label,'Sat 04:00 SGT');
});
test('bar coordinates are derived from the same fields as labels', () => {
  const svg = context._execClockSVG(data);
  assert.equal((svg.match(/data-venue=/g) || []).length,4);
  for (const [regime,open,close] of [['CEST',15,23.5],['CET',16,24.5],['EDT',21.5,28],['EST',22.5,29]]) {
    assert.ok(svg.includes(`data-regime="${regime}" data-open-hour="${open}" data-close-hour="${close}"`));
  }
  assert.ok(!svg.includes('orders submitted here'));
});
test('changed input updates both geometry and label without a literal clock', () => {
  const altered = structuredClone(data);
  altered.sessions_sgt.XETR.sgt_close_winter = '01:15';
  const svg = context._execClockSVG(altered);
  assert.ok(svg.includes('data-close-hour="25.25"'));
  assert.ok(svg.includes('Tue 01:15 SGT'));
});
test('missing time or rollover is unavailable, never assumed to be summer', () => {
  assert.equal(session('US','winter','2026-09-14','close',{}),null);
  const altered = structuredClone(data);
  delete altered.sessions_sgt.XETR.sgt_close_rolls_winter;
  assert.equal(session('XETR','winter','2026-09-14','close',altered),null);
  assert.ok(context._execClockSVG(altered).includes('Session time unavailable'));
});
test('weekly cycle has four reflowing steps and no fixed order cutoff', () => {
  const cycle = context._execCycleHTML(data);
  assert.equal((cycle.match(/<li>/g) || []).length,4);
  for (const regime of ['CEST','CET','EDT','EST']) assert.ok(cycle.includes(regime));
  assert.ok(cycle.includes('Sat 00:30 SGT'));
  assert.ok(cycle.includes('Tue 00:30 SGT'));
  assert.ok(cycle.includes('Confirm with the broker'));
  assert.ok(!/21:50|03:50/.test(cycle));
});
test('live consumers share helper; only reference table reads seasonal fields directly', () => {
  assert.ok(!html.includes('Mon 21:50 / Tue 03:50 SGT'));
  assert.ok(!html.includes('summer offsets'));
  assert.ok(!html.includes("setX('us_close_s'"));
  assert.ok(html.includes('SGT (US daylight-time reference)'));
  assert.ok(html.includes('exec-clock-source-date'));
});
