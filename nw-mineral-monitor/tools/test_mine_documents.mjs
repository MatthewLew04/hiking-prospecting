#!/usr/bin/env node
/* Contract for surfacing a mine's documents when the reader clicks it.

   Run: node tools/test_mine_documents.mjs

   The gap this closes was written down in site/index.html itself, above
   docRefsForDossier(): a dossier is keyed by grade-row index and identified by
   its own quote, so only the ~3,370 graded mines could ever show a source, and
   "a mine whose documents exist only in the WS13 corpus still shows nothing …
   nothing on this path knows which mine to ask it for". The clicked feature
   does know, and now says so.

   Four things have to hold, each its own way to regress:

     * A bare numeric id must never be sent. That exact shape is what made a
       click on an MRDS Nevada deposit answer with an unrelated NBMG
       mining-district file — both namespaces number records with bare integers
       and 52 collide. Ids leave here namespaced or not at all.
     * Only namespaces the bridge can address may produce a subject. No
       usmin_*.json carries an `id` column, so ws13_mine_id_map has no
       front-end id for a topo working; asking anyway would be inventing a
       lookup, and an invented lookup that hits attaches another mine's file.
     * "No documents" and "no index" must stay distinguishable to the last
       pixel. Rendering an unreachable index as an empty file is a false
       negative about a mine's entire documentary record.
     * A page of a document set must not read as the whole set. On the
       full-corpus backend `count` and `documents.length` are different
       numbers, and a References-shaped panel is exactly where a reader takes
       the smaller one for a total.

   No browser and no network: the regions under test are sliced out of
   index.html by anchor and run in a node:vm realm over stubs. An anchor that
   stops matching fails the run rather than quietly testing nothing. */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const INDEX = path.join(ROOT, 'site', 'index.html');
const SOURCE = fs.readFileSync(INDEX, 'utf8');

let pass = 0, fail = 0; const fails = [];
const ok = (name, cond, extra = '') => {
  if (cond) { pass++; } else { fail++; fails.push(name + (extra ? ' — ' + extra : '')); }
};
const eq = (name, got, want) => ok(name, got === want,
  `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

function only(marker) {
  const first = SOURCE.indexOf(marker);
  if (first < 0) throw new Error(`anchor is gone from site/index.html: ${marker}`);
  if (SOURCE.indexOf(marker, first + 1) >= 0)
    throw new Error(`anchor is no longer unique in site/index.html: ${marker}`);
  return first;
}
const slice = (a, b) => {
  const start = only(a), end = only(b);
  if (end <= start) throw new Error(`slice runs backwards: ${a}`);
  return SOURCE.slice(start, end);
};

/* ---------------------------------------------------------------- realm */
// Scripted docs_for backend: the test drives what each spelling answers.
const calls = [];
let backend = () => ({ status: 'not_loaded' });

const sandbox = {
  console, JSON, Math, Number, String, Array, Object, Boolean, RegExp, Date,
  execWs12Tool: async (name, a) => { calls.push({ name, mine_id: a.mine_id }); return backend(a.mine_id); },
  // Rendering stubs: the panel's own text is what is under test, not the
  // chip internals, which tools/test_ws13_citation_frontend.mjs already pins.
  docChip: (id, page) => (/^[0-9a-f]{64}$/.test(String(id)) ? `<chip:${id.slice(0, 8)}:${page}>` : ''),
  docRightsBadge: id => (String(id).startsWith('a') ? { html: '<span>CC BY-NC-SA</span>', tip: 'CC BY-NC-SA 4.0 — AZGS ADMMR' } : { html: '', tip: '' }),
  docQuoteFor: () => '',
  rememberDocsForRow: () => {},
  pointXY: f => (f && f.__xy) || null,
  document: { createElement: () => ({ innerHTML: '', isConnected: true }) },
  dInner: { appendChild() {} },
  $: () => null,
};
vm.createContext(sandbox);
vm.runInContext(slice('const fmt = n => n.toLocaleString', 'const $ = id => document.getElementById(id);'), sandbox);
// The real slug function: pipelines/ws13_mine_id_map.front_end_slug() documents
// itself as mirroring this exact expression, so the harness runs the original
// rather than a copy that could drift away from the Python it must match.
vm.runInContext(slice('function stateSurveySafeId(value){', 'function stateSurveyNormalize(key,entry){'), sandbox);
vm.runInContext(slice('let MINE_FOCUS = null;', '/* --- the query engine --- */'), sandbox);
const { mineSubject, mineDocsFetch, mineDocsHtml } = sandbox;

/* ------------------------------------------- 1. ids leave here namespaced */
const mrds = mineSubject({
  source: 'national-mrds', layer: { id: 'national-mrds-c' },
  properties: { id: '10071608', nm: 'Center Star Mine', st: 'ID' },
  __xy: [-115.55958, 45.80767],
});
ok('an MRDS feature resolves to a subject', !!mrds);
eq('namespace comes off the tile source', mrds.namespace, 'mrds');
eq('ids are offered namespaced and slugged',
  JSON.stringify(mrds.ids), JSON.stringify(['mrds:10071608', 'mrds-10071608']));
ok('a bare numeric id is never offered', !mrds.ids.includes('10071608'));
eq('the subject carries its coordinates', `${mrds.lat},${mrds.lon}`, '45.80767,-115.55958');

const survey = mineSubject({
  source: 'national-stategeo', layer: { id: 'stategeo-c' },
  properties: { id: 'IGS DD-1 IF0126', nm: 'St. Louis Mine', st: 'ID' },
});
eq('a state-survey record slugs the way the bridge spells it',
  JSON.stringify(survey.ids),
  JSON.stringify(['stategeo:IGS DD-1 IF0126', 'stategeo-igs-dd-1-if0126', 'IGS DD-1 IF0126']));
ok('a self-namespacing id may also go bare', survey.ids.includes('IGS DD-1 IF0126'));

/* ------------------------- 2. only namespaces the bridge can address ask */
ok('a USMIN topo working produces no subject', mineSubject({
  source: 'national-usmin', layer: { id: 'national-usmin-c' },
  properties: { gda: 'GDA-12345', typ: 'Adit', st: 'ID' } }) === null);
ok('an ARDF occurrence produces no subject', mineSubject({
  source: 'national-ardf', layer: { id: 'national-ardf-c' },
  properties: { id: 'AK-0001', nm: 'Some occurrence', st: 'AK' } }) === null);
ok('a claim produces no subject', mineSubject({
  source: 'national-claims', layer: { id: 'claimsA-dot' },
  properties: { serial: 'ID103454437', nm: 'CENTER STAR NO 1' } }) === null);
ok('a record with no id produces no subject', mineSubject({
  source: 'national-mrds', layer: { id: 'national-mrds-c' },
  properties: { nm: 'Unnamed', st: 'ID' } }) === null);

// The bridge's own source files are the authority for that coverage.
const withIds = fs.readdirSync(path.join(ROOT, 'build-inputs', 'data', 'sites'))
  .filter(f => f.endsWith('.json'))
  .map(f => JSON.parse(fs.readFileSync(path.join(ROOT, 'build-inputs/data/sites', f), 'utf8')))
  .filter(d => Array.isArray(d.id) && d.id.length)
  .map(d => d.src);
ok('every namespace the panel asks for really carries ids',
  ['mrds', 'stategeo'].every(ns => withIds.includes(ns)), JSON.stringify([...new Set(withIds)]));
ok('usmin still carries none, which is why it is excluded',
  !withIds.includes('usmin'));

/* --------------------------------- 3. spelling order and honest statuses */
calls.length = 0;
backend = id => (id === 'mrds-10071608'
  ? { status: 'loaded', count: 2, documents: [{ document_id: 'a'.repeat(64), title: 'Mine file' }] }
  : { status: 'loaded', count: 0, documents: [] });
let found = await mineDocsFetch(mrds);
eq('the first spelling that answers wins', found.mine_id, 'mrds-10071608');
eq('and it stops there', calls.length, 2);
eq('namespaced is tried before slugged', calls[0].mine_id, 'mrds:10071608');

// Re-clicking the same mine must not re-spend a Lambda slot; the account has
// ten across every function and one ASK question already holds two.
calls.length = 0;
found = await mineDocsFetch(mrds);
eq('a second click on the same mine is served from cache', calls.length, 0);
eq('and gives the same answer', found.mine_id, 'mrds-10071608');

calls.length = 0;
backend = () => ({ status: 'not_loaded' });
found = await mineDocsFetch(survey);
eq('an unreachable index is not an empty document set', found.status, 'not_loaded');
calls.length = 0;
await mineDocsFetch(survey);
ok('an unreachable index is never cached — it is transient, not a fact about the mine',
  calls.length > 0, `${calls.length} calls on the retry`);

calls.length = 0;
backend = () => ({ status: 'loaded', count: 0, documents: [] });
found = await mineDocsFetch(survey);
eq('a reachable index with nothing filed says so distinctly', found.status, 'none');

/* --------------------------------------- 4. the panel says what it knows */
const unreachable = mineDocsHtml({ status: 'not_loaded' }, mrds);
ok('unreachable renders as unknown, never as none', /unknown/i.test(unreachable));
ok('unreachable never claims the mine has no documents',
  !/no document in the index/i.test(unreachable));

const none = mineDocsHtml({ status: 'none' }, mrds);
ok('none names the ids it actually tried', none.includes('mrds:10071608'));

const page = mineDocsHtml({
  status: 'loaded', mine_id: 'mrds-10071608',
  result: { count: 47, truncated: true },
  documents: [{ document_id: 'a'.repeat(64), title: 'NBMG file', doc_type: 'report', page_count: 12 }],
}, mrds);
ok('a page of a set is labelled as a page', /1 OF 47/.test(page), page.slice(0, 240));
ok('and says so again in prose', /not all of them/i.test(page));
ok('rights ride beside the entry', /CC BY-NC-SA/.test(page));
ok('the reader is offered the question path', /askAboutMine\(\)/.test(page));

const whole = mineDocsHtml({
  status: 'loaded', mine_id: 'mrds-10071608', result: { count: 1 },
  documents: [{ document_id: 'b'.repeat(64), title: 'Only file' }],
}, mrds);
ok('a complete set is not labelled as a page', !/OF/.test(whole) && !/not all of them/i.test(whole));

// The Python half of the bridge derives the same slug and says so in its
// docstring; if the two ever disagree, every state-survey lookup misses.
const PY = fs.readFileSync(path.join(ROOT, 'pipelines', 'ws13_mine_id_map.py'), 'utf8');
ok('the bridge still claims to mirror this slug function',
  /Mirror stateSurveySafeId\(\) in site\/index\.html/.test(PY));
eq('and the documented example agrees',
  sandbox.stateSurveySafeId('IGS DD-1 IF0126'), 'igs-dd-1-if0126');

/* --------------------------------------------- 5. the panel and the model */
ok('opening a detail panel sets the focus',
  /function openDetail\(h, subject\)\{[\s\S]{0,240}?MINE_FOCUS = subject \|\| null;/.test(SOURCE));
ok('closing it clears the focus',
  /function closeDetail\(\)\{[\s\S]{0,120}?MINE_FOCUS=null;/.test(SOURCE));
ok('a stale fetch cannot paint into a newer panel',
  /if \(seq !== MINE_DOC_SEQ \|\| !slot\.isConnected\) return;/.test(SOURCE));
ok('the open mine reaches the model as labelled context',
  /map context, not the question/.test(SOURCE));
ok('its ids are handed to the document tools by name',
  /docs_for's mine_id and as search_documents's mine_id/.test(SOURCE));
ok('archive text is bounded before it goes near a prompt',
  /replace\(\/\\s\+\/g,' '\)\.trim\(\)\.slice\(0,120\)/.test(SOURCE));
ok('the two record layers pass their subject to the panel',
  (SOURCE.match(/`, subject\);/g) || []).length === 2);

const ASK = fs.readFileSync(path.join(ROOT, 'infra', 'ask_lambda.py'), 'utf8');
ok('the system prompt explains the map-context line',
  /map context, not the question/.test(ASK));
ok('and tells the model to quote the page rather than the record fields',
  /quote the page you get back rather than answering from the record fields alone/.test(ASK));

console.log(`\nmine documents: ${pass} passed, ${fail} failed`);
if (fail) { console.log(fails.map(f => '  ✗ ' + f).join('\n')); process.exit(1); }
