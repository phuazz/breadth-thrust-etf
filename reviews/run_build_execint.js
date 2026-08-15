const { buildReport } = require('C:/Users/phuaz/.claude/skills/research-review/assets/report_builder.js');
const spec = require('./build_2026-08-15_execution-integrity.js');
buildReport(spec, 'C:/dev/breadth-thrust-etf/reviews/2026-08-15_execution-integrity.docx')
  .then(r => console.log('wrote', r.outPath, r.bytes, 'bytes'))
  .catch(e => { console.error('FAILED:', e.message); process.exit(1); });
