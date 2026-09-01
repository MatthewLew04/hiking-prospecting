#!/usr/bin/env node
/* Contract for the autopopulated underground section on a mine card.

   Run: node tools/test_autopop_frontend.mjs

   pipelines/geomodel_autopopulate.py publishes a 3-D model per (mine,
   document) and an index at data/models/index.json; the card renders what
   the documents say is underground and opens the pregenerated model. Four
   properties, each its own regression:

     * An alias row must land on its canonical entry. One physical mine is
       several grade rows ("Victoria mine (1,050-foot level)" and "(shipping
       ore)"), and a click on any of them must find the one model.
     * A pregenerated model must open by ?project= alone — no coordinates,
       no IndexedDB handoff, no site build. The moment open3D falls through
       to the lat/lon path it rebuilds an empty scaffold and the reader
       never sees the workings.
     * An entry with documents but no model must say so, not render a dead
       button: "nothing is drawn rather than guessed" is a statement the
       card is required to make.
     * Document titles come from harvested portals and are untrusted; they
       render escaped or not at all.

   No browser and no network: the regions under test are sliced out of
   index.html by anchor and run in a node:vm realm over stubs. An anchor
   that stops matching fails the run rather than quietly testing nothing.
   If site/data/models/index.json exists on this checkout, its shape and
   its published files are checked too. */
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

/* ---- the code under test ---- */
const UG_BLOCK = slice('/* ---------- autopopulated underground models',
                       '/* ---------- county gold-signal ranking');
const OPEN3D = slice('async function open3D(', 'function aoiAtPoint(');
const ESC = slice('const esc =', 'const $ = id =>');
const KV = slice('function kv(rows)', 'function showFeature(');

/* ---- a realm with just enough page ---- */
function realm(indexJson) {
  const opened = [];
  const slotEl = { innerHTML: '', isConnected: true,
                   remove() { this.removed = true; } };
  const ctx = {
    console,
    URLSearchParams,
    window: { open: (u) => opened.push(u) },
    document: { createElement: () => slotEl },
    dInner: { appendChild: () => {} },
    jget: async (u) => {
      if (u === 'data/models/index.json' && indexJson) return indexJson;
      throw new Error('404 ' + u);
    },
    $: () => null,
    map: null, S: { layers: {} }, MAN: null,
    aoiAtPoint: () => null,
    indexedDB: undefined,
  };
  vm.createContext(ctx);
  vm.runInContext(ESC + ';' + KV + ' ;', ctx);
  // open3D needs the surrounding helpers stubbed harmlessly
  vm.runInContext('const g3dHandoff = () => ({layers:{}, total:0});'
    + 'const g3dDb = () => Promise.reject(new Error("no db in test"));'
    + 'const g3dSlug = s => String(s||"site").toLowerCase();', ctx);
  vm.runInContext(OPEN3D.replace('function aoiAtPoint(', '').trim(), ctx);
  vm.runInContext(UG_BLOCK, ctx);
  return { ctx, opened, slotEl };
}

const ENTRY = {
  key: 'grades:12', label: 'Tonopah Divide mine', site_kind: 'grades',
  methods: ['citation_quote'], store_mine_ids: ['ws9-nv-tonopah-divide-mine'],
  grade_rows: [12, 868],
  documents: [{ doc_id: 'a'.repeat(64),
    title: 'The Divide Silver District, Nevada',
    source_url: 'https://pubs.usgs.gov/bul/0715k/report.pdf',
    publication_year: 1921, cited_pages: [2], pages: 30, sections: 8 }],
  models: [{ model_id: 'tonopah-divide-mine-c0125b08',
    project_url: '/models/tonopah-divide-mine-c0125b08/model.geomodel.json',
    doc_id: 'a'.repeat(64), primary: true, elements: 7, omitted: 14,
    confidence: { surveyed: 0, described: 7, assumed: 0 } }],
  primary: 'tonopah-divide-mine-c0125b08',
  lexicon: { kinds: { shaft: { count: 16, surfaces: { shaft: 16 } },
                      level: { count: 9, surfaces: { level: 9 } } },
             verbs: {}, level_labels: ['100'], sentences: 40,
             mining_sentences: 22 },
  minerals: ['Gold', 'Silver'],
  extent: { total_m: 1039.7, by_type: { shaft: 902.5, crosscut: 137.2 },
            levels: ['45', '100'], deepest_level_m: 213.4 },
};
const IDX = { schema_version: 1, by_mine: {
  'grades:12': ENTRY, 'grades:868': { alias: 'grades:12' } } };

/* 1 — alias rows resolve to the canonical entry */
{
  const { ctx } = realm(IDX);
  await vm.runInContext('ugIndex()', ctx);
  const via = vm.runInContext('ugEntry("grades:868")', ctx);
  ok('alias row resolves to its canonical entry',
     via && via.key === 'grades:12');
  ok('a mine outside the index resolves to nothing',
     vm.runInContext('ugEntry("grades:999")', ctx) === null);
}

/* 2 — a pregenerated model opens by project url alone */
{
  const { ctx, opened } = realm(IDX);
  await vm.runInContext(
    'open3D(0, 0, "Tonopah Divide mine", {project: "/models/tonopah-divide-mine-c0125b08/model.geomodel.json"})',
    ctx);
  ok('open3D with a project opens exactly one tab', opened.length === 1);
  const url = opened[0] || '';
  ok('the tab is the viewer with the published project',
     url.startsWith('model3d.html?') &&
     decodeURIComponent(url).includes('/models/tonopah-divide-mine-c0125b08/model.geomodel.json'),
     url);
  ok('the project path carries no lat/lon rebuild', !/[?&]lat=/.test(url));
}

/* 3 — the card section renders the underground facts */
{
  const { ctx } = realm(IDX);
  await vm.runInContext('ugIndex()', ctx);
  const html = vm.runInContext('ugHtml(ugEntry("grades:12"))', ctx);
  ok('minerals are printed', html.includes('Gold, Silver'));
  ok('the workings breakdown is printed', /shaft 90[23] m/.test(html));
  ok('the deepest level is printed', html.includes('213 m'));
  ok('the model button carries the project url',
     html.includes('project:&quot;/models/tonopah-divide-mine-c0125b08/model.geomodel.json&quot;'));
  ok('the described-not-surveyed statement is made',
     html.includes('never surveyed'));
  ok('the omissions are counted for the reader',
     html.includes('14 left out'));
  ok('the source document is linked',
     html.includes('https://pubs.usgs.gov/bul/0715k/report.pdf'));
}

/* 4 — documents with no model say so instead of rendering a dead button */
{
  const { ctx } = realm(IDX);
  await vm.runInContext('ugIndex()', ctx);
  const bare = { ...ENTRY, models: [], primary: null, extent: null };
  ctx.__bare = bare;
  const html = vm.runInContext('ugHtml(__bare)', ctx);
  ok('no model, no button', !html.includes('OPEN 3D MODEL'));
  ok('the no-model statement is made',
     html.includes('nothing is drawn rather than guessed'));
  ok('the documents still list', html.includes('The Divide Silver District'));
}

/* 4b — a hostile label or project url cannot break out of the onclick */
{
  const { ctx } = realm(IDX);
  await vm.runInContext('ugIndex()', ctx);
  const evilLabel = `x"');</button><img src=x onerror=alert(1)>`;
  const evil = { ...ENTRY,
    label: evilLabel,
    models: [{ ...ENTRY.models[0], project_url: `/m/x'onmouseover=alert(1)` }] };
  ctx.__evil = evil;
  const html = vm.runInContext('ugHtml(__evil)', ctx);
  // what the browser does: the attribute ends at the first RAW double quote,
  // entities decode, and the first argument must round-trip as pure data
  const attr = /onclick="([^"]*)"/.exec(html);
  ok('the onclick attribute survives hostile data intact', !!attr);
  const js = attr[1].replace(/&quot;/g, '"').replace(/&gt;/g, '>')
    .replace(/&lt;/g, '<').replace(/&amp;/g, '&');
  const arg = /open3D\(0,0,("(?:[^"\\]|\\.)*")/.exec(js);
  ok('the label decodes back to the exact data, no code',
     !!arg && JSON.parse(arg[1]) === evilLabel);
  ok('no raw markup escapes into the page', !html.includes('<img src=x'));
}

/* 4c — an empty project url is inert, not a 0,0 site build */
{
  const { ctx, opened } = realm(IDX);
  await vm.runInContext('open3D(0, 0, "x", {project: ""})', ctx);
  ok('an empty project opens nothing at all', opened.length === 0);
}

/* 4d — a map-feature subject reaches the same section by its stategeo id */
{
  const idx = { schema_version: 1, by_mine: {
    'stategeo:IGS DD-1 IF0126': { ...ENTRY, key: 'stategeo:IGS DD-1 IF0126',
      site_kind: 'latlon', grade_rows: [], models: [], primary: null, extent: null } } };
  const { ctx, slotEl } = realm(idx);
  ctx.__subject = { ids: ['mrds:123', 'stategeo:IGS DD-1 IF0126'] };
  await vm.runInContext('fillUnderground3DSubject(__subject)', ctx);
  ok('the subject fill finds the latlon entry by a namespaced id',
     slotEl.innerHTML.includes('SOURCE DOCUMENTS'));
}

/* 5 — untrusted titles render escaped */
{
  const { ctx } = realm(IDX);
  await vm.runInContext('ugIndex()', ctx);
  const hostile = { ...ENTRY, models: [], primary: null, extent: null,
    documents: [{ doc_id: 'b'.repeat(64),
      title: '<script>alert(1)</script>', source_url: null,
      publication_year: null, cited_pages: [], pages: 1, sections: 0 }] };
  ctx.__hostile = hostile;
  const html = vm.runInContext('ugHtml(__hostile)', ctx);
  ok('a hostile title cannot become markup', !html.includes('<script>'));
  ok('the escaped title still shows', html.includes('&lt;script&gt;'));
}

/* 6 — showGrade wires the section in */
{
  const card = slice('function showGrade(i){', 'function gradeAnswer(');
  ok('the grade card requests its underground section',
     card.includes("fillUnderground3D('grades:'+i)"));
}

/* 7 — the published index on this checkout, when present, is coherent */
{
  const idxPath = path.join(ROOT, 'site', 'data', 'models', 'index.json');
  if (fs.existsSync(idxPath)) {
    const idx = JSON.parse(fs.readFileSync(idxPath, 'utf8'));
    ok('index schema_version 1', idx.schema_version === 1);
    const entries = Object.values(idx.by_mine).filter(e => !e.alias);
    ok('index has entries', entries.length > 0);
    for (const e of entries) {
      for (const m of e.models || []) {
        const file = path.join(ROOT, 'site', m.project_url.replace(/^\//, ''));
        ok(`published project exists for ${m.model_id}`, fs.existsSync(file));
      }
      if (e.primary)
        ok(`primary of ${e.key} is one of its models`,
           (e.models || []).some(m => m.model_id === e.primary));
    }
    for (const [k, e] of Object.entries(idx.by_mine))
      if (e.alias)
        ok(`alias ${k} points at a real entry`,
           idx.by_mine[e.alias] && !idx.by_mine[e.alias].alias);
  } else {
    ok('index not present on this checkout (skipped file checks)', true);
  }
}

console.log(`test_autopop_frontend: ${pass} passed, ${fail} failed`);
if (fail) { for (const f of fails) console.error('  FAIL ' + f); process.exit(1); }
