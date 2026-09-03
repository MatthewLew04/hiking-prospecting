#!/usr/bin/env node
/* Contract for the autopopulated underground section on a mine card.

   Run: node tools/test_autopop_frontend.mjs

   pipelines/geomodel_autopopulate.py (write_index — the WS13 driver writes
   through the same function) publishes a 3-D model per (mine, document), a
   COMPACT index at data/models/index.json (schema 2: a few short keys per
   mine, sized for tens of thousands of mines, keyed grades:<row>,
   mrds:<dep_id>, usmin:<fid>, stategeo:<id>, ardf:<id>) and the full record
   per model at models/<id>/card.json. The card renders what the compact row
   states at once, fetches card.json for the documents, levels and assays,
   and opens the pregenerated model. The properties, each its own regression:

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
     * The compact row is enough for the section and the button; card.json
       fills in behind a visible placeholder, and a card that does not
       arrive names the missing file. A silent blank is a failure.
     * A dot with no row appends nothing — an MRDS card without a model
       carries no empty section.
     * Schema-1 rows (the full record inline) still render: a stale deploy
       of either half keeps working.

   No browser and no network: the regions under test are sliced out of
   index.html by anchor and run in a node:vm realm over stubs. An anchor
   that stops matching fails the run rather than quietly testing nothing.
   If site/data/models/index.json exists on this checkout, its shape and
   its published files are checked too, whichever schema it carries. */
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
// let the awaited chain inside the realm advance without resolving a deferred
const tick = () => new Promise(r => setImmediate(r));

/* ---- the code under test ---- */
const UG_BLOCK = slice('/* ---------- autopopulated underground models',
                       '/* ---------- county gold-signal ranking');
const OPEN3D = slice('async function open3D(', 'function aoiAtPoint(');
const ESC = slice('const esc =', 'const $ = id =>');
const KV = slice('function kv(rows)', 'function showFeature(');
const DOC_IDS = slice('function mineDocIds(', 'function mineSubject(');

/* ---- a realm with just enough page ----
   `cards` maps a model id to its card.json, or to a function returning the
   promise to hand back (for a deferred or failing fetch). */
function realm(indexJson, cards = {}) {
  const opened = [], fetched = [];
  const slotEl = { innerHTML: '', isConnected: true,
                   remove() { this.removed = true; } };
  const ctx = {
    console,
    URLSearchParams,
    window: { open: (u) => opened.push(u) },
    document: { createElement: () => slotEl },
    dInner: { appendChild: () => {} },
    jget: async (u) => {
      fetched.push(u);
      if (u === 'data/models/index.json' && indexJson) return indexJson;
      const m = /^models\/([^/]+)\/card\.json$/.exec(u);
      if (m && Object.prototype.hasOwnProperty.call(cards, decodeURIComponent(m[1]))) {
        const c = cards[decodeURIComponent(m[1])];
        return typeof c === 'function' ? c() : c;
      }
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
  return { ctx, opened, fetched, slotEl };
}

/* ---- the schema-1 fixture: the full record inline ---- */
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
    confidence: { surveyed: 0, described: 7, assumed: 0 },
    levels: ['45', '100'], level_depths_m: { '45': 13.7, '100': 213.4 },
    assay_commodities: ['ag'], assays: 3, vein: true }],
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

/* ---- the schema-2 fixture: the compact row + the card behind it ---- */
const P = 'tonopah-divide-mine-c0125b08';
const ROW = { l: 'Tonopah Divide mine', p: P, n: 1, m: ['Gold', 'Silver'],
              x: 1040, w: 7, c: [7, 0, 0] };
const CARD = { schema_version: 2, model_id: P, ...ENTRY };
const IDX2 = { schema_version: 2, generated: 't', stats: {}, by_mine: {
  'grades:12': ROW, 'grades:868': { a: 'grades:12' },
  'mrds:10012345': { l: 'Divide Extension', p: P, n: 1, m: ['Silver'], x: 1040, w: 7, c: [7, 0, 0] },
  'stategeo:IGS DD-1 IF0126': { l: 'Lava Creek', p: null, n: 0, m: [], x: 0, w: 0, c: [0, 0, 0] },
  'ardf:MD012': { l: 'Ghost', p: 'ghost-mine-00000000', n: 1, m: [], x: 12, w: 1, c: [1, 0, 0] },
  'usmin:7': { l: 'Traversal', p: '../../etc/passwd', n: 1, m: [], x: 1, w: 1, c: [1, 0, 0] },
} };

/* 1 — alias rows resolve to the canonical entry, in either schema */
{
  const { ctx } = realm(IDX);
  await vm.runInContext('ugIndex()', ctx);
  const via = vm.runInContext('ugEntry("grades:868")', ctx);
  ok('alias row resolves to its canonical entry',
     via && via.key === 'grades:12');
  ok('a mine outside the index resolves to nothing',
     vm.runInContext('ugEntry("grades:999")', ctx) === null);
}
{
  const { ctx } = realm(IDX2);
  await vm.runInContext('ugIndex()', ctx);
  const via = vm.runInContext('ugEntry("grades:868")', ctx);
  ok('a compact {a:} alias resolves to its canonical key',
     via && via.key === 'grades:12' && via.primary === P);
  ok('the compact row normalises to the card shape',
     via && via.n === 1 && via.workings === 7 && via.counts.described === 7
     && via.total_m === 1040 && via.minerals.join() === 'Gold,Silver' && via.full === null
     && via.project === `/models/${P}/model.geomodel.json`);
  ok('an alias that points at nothing is nothing',
     vm.runInContext('UG3D.idx.by_mine["grades:1"]={a:"grades:none"}; ugEntry("grades:1")', ctx) === null);
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

/* 3 — a schema-1 row renders the whole section from the inline record */
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
  ok('the assay commodities are named', html.includes('3 quoted grades — Silver'));
}

/* 4 — documents with no model say so instead of rendering a dead button */
{
  const { ctx } = realm(IDX);
  await vm.runInContext('ugIndex()', ctx);
  const bare = { ...ENTRY, models: [], primary: null, extent: null };
  ctx.__bare = bare;
  const html = vm.runInContext('ugHtml(ugNormalise("grades:12", __bare))', ctx);
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
  const html = vm.runInContext('ugHtml(ugNormalise("grades:12", __evil))', ctx);
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
{
  // the same, from a compact row: the label and the model id are index data
  const evilLabel = `x"');</button><img src=x onerror=alert(1)>`;
  const idx = { schema_version: 2, by_mine: { 'grades:12': { ...ROW, l: evilLabel, p: `x"><img src=x>` } } };
  const { ctx } = realm(idx);
  await vm.runInContext('ugIndex()', ctx);
  const html = vm.runInContext('ugHtml(ugEntry("grades:12"))', ctx);
  const attr = /onclick="([^"]*)"/.exec(html);
  ok('a hostile compact row keeps the onclick attribute intact', !!attr);
  const js = attr[1].replace(/&quot;/g, '"').replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/&amp;/g, '&');
  const arg = /open3D\(0,0,("(?:[^"\\]|\\.)*")/.exec(js);
  ok('the compact label decodes back to the exact data', !!arg && JSON.parse(arg[1]) === evilLabel);
  ok('no raw markup escapes from a compact row', !html.includes('<img src=x'));
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
  const html = vm.runInContext('ugHtml(ugNormalise("grades:12", __hostile))', ctx);
  ok('a hostile title cannot become markup', !html.includes('<script>'));
  ok('the escaped title still shows', html.includes('&lt;script&gt;'));
}

/* 6 — every dot type's card asks for its row */
{
  const card = slice('function showGrade(i){', 'function gradeAnswer(');
  ok('the grade card requests its underground section',
     card.includes("fillUnderground3D('grades:'+i)"));
  const feature = slice('function showFeature(f){', 'function showDistrict(');
  const mrds = feature.slice(feature.indexOf("if(lid.startsWith('national-mrds')){"),
                             feature.indexOf("if(lid==='national-usmin-c'){"));
  ok('the MRDS card opens with its subject (mrds:<dep_id> spellings)',
     mrds.includes(', subject);'));
  const usmin = feature.slice(feature.indexOf("if(lid==='national-usmin-c'){"),
                              feature.indexOf("if(lid==='national-ardf-c'){"));
  ok('the USMIN card requests usmin:<fid>',
     usmin.includes("fillUnderground3D('usmin:'+") && usmin.includes('p.fid'));
  const ardf = feature.slice(feature.indexOf("if(lid==='national-ardf-c'){"),
                             feature.indexOf("if(lid==='national-geology-fill'"));
  ok('the ARDF card requests ardf:<id>', ardf.includes("fillUnderground3D('ardf:'+p.id)"));
  const stategeo = feature.slice(feature.indexOf("if(f.source==='national-stategeo'){"),
                                 feature.indexOf("if(f.source==='national-claims'){"));
  ok('the state-survey card opens with its subject (stategeo:<id> spellings)',
     stategeo.includes(', subject);'));
  const openDetail = slice('function openDetail(h, subject){', 'function closeDetail(');
  ok('openDetail hands the subject to the underground lookup',
     openDetail.includes('fillUnderground3DSubject(subject)'));
  // the subject spellings put the index key first
  const ctx = vm.createContext({ stateSurveySafeId: v => String(v).toLowerCase().replace(/[^a-z0-9-]+/g, '-') });
  vm.runInContext(DOC_IDS, ctx);
  ok('the MRDS subject leads with mrds:<dep_id>',
     vm.runInContext('mineDocIds("mrds", 10012345)[0]', ctx) === 'mrds:10012345');
  ok('the state-survey subject leads with stategeo:<id>',
     vm.runInContext('mineDocIds("stategeo", "IGS DD-1 IF0126")[0]', ctx) === 'stategeo:IGS DD-1 IF0126');
}

/* 7 — the published index on this checkout, when present, is coherent */
{
  const idxPath = path.join(ROOT, 'site', 'data', 'models', 'index.json');
  if (fs.existsSync(idxPath)) {
    const idx = JSON.parse(fs.readFileSync(idxPath, 'utf8'));
    ok('index schema_version is 1 or 2', idx.schema_version === 1 || idx.schema_version === 2);
    const rows = Object.entries(idx.by_mine);
    const entries = rows.filter(([, e]) => !e.alias && !e.a);
    ok('index has entries', entries.length > 0);
    for (const [k, e] of entries) {
      if (idx.schema_version === 2) {
        ok(`row ${k} has the compact keys`, ['l', 'p', 'n', 'm', 'x', 'w', 'c'].every(f => f in e));
        if (e.p) {
          const dir = path.join(ROOT, 'site', 'models', e.p);
          ok(`published project exists for ${e.p}`, fs.existsSync(path.join(dir, 'model.geomodel.json')));
          ok(`card.json exists for ${e.p}`, fs.existsSync(path.join(dir, 'card.json')));
        }
        continue;
      }
      for (const m of e.models || []) {
        const file = path.join(ROOT, 'site', m.project_url.replace(/^\//, ''));
        ok(`published project exists for ${m.model_id}`, fs.existsSync(file));
      }
      if (e.primary)
        ok(`primary of ${e.key} is one of its models`,
           (e.models || []).some(m => m.model_id === e.primary));
    }
    for (const [k, e] of rows) {
      const to = e.alias || e.a;
      if (to)
        ok(`alias ${k} points at a real entry`,
           idx.by_mine[to] && !idx.by_mine[to].alias && !idx.by_mine[to].a);
    }
    if (idx.schema_version === 2) {
      const bytes = Buffer.byteLength(JSON.stringify(idx.by_mine));
      ok('the compact index stays under 256 bytes a row', bytes / rows.length < 256, `${bytes} / ${rows.length}`);
    }
  } else {
    ok('index not present on this checkout (skipped file checks)', true);
  }
}

/* 8 — a grades card shows the section from the compact row, then fills from card.json */
{
  let release; const gate = new Promise(r => { release = r; });
  const { ctx, slotEl, fetched } = realm(IDX2, { [P]: () => gate.then(() => CARD) });
  const done = vm.runInContext('fillUnderground3D("grades:12")', ctx);
  await tick(); await tick();
  const first = slotEl.innerHTML;
  ok('the section is up before card.json answers', first.includes('UNDERGROUND — FROM THE DOCUMENTS'));
  ok('the compact row prints the minerals', first.includes('Gold, Silver'));
  ok('the compact row prints the extent', first.includes('1.0 km of described workings'));
  ok('the compact row prints the counts', first.includes('7 elements') && first.includes('7 described, 0 assumed, 0 surveyed'));
  ok('the button carries /models/<p>/model.geomodel.json',
     first.includes(`project:&quot;/models/${P}/model.geomodel.json&quot;`));
  ok('the documents placeholder is visible', first.includes('loading documents'));
  ok('card.json was requested from models/<p>/card.json', fetched.includes(`models/${P}/card.json`));
  release(); await done;
  const html = slotEl.innerHTML;
  ok('the placeholder is gone', !html.includes('loading documents'));
  ok('the documents list arrives', html.includes('SOURCE DOCUMENTS — 1')
     && html.includes('https://pubs.usgs.gov/bul/0715k/report.pdf'));
  ok('the levels arrive', html.includes('45, 100') && html.includes('213 m'));
  ok('the assay commodities arrive', html.includes('3 quoted grades — Silver'));
  ok('the omissions arrive', html.includes('14 left out'));
  ok('the head is still there', html.includes(`project:&quot;/models/${P}/model.geomodel.json&quot;`));
  // a second card for the same model does not refetch
  const before = fetched.length;
  await vm.runInContext('fillUnderground3D("grades:868")', ctx);
  ok('card.json is fetched once per model per session', fetched.length === before);
}

/* 9 — an MRDS card with an mrds:<dep_id> row shows the button */
{
  const { ctx, slotEl } = realm(IDX2, { [P]: CARD });
  ctx.__subject = { ids: ['mrds:10012345', 'mrds-10012345'] };
  await vm.runInContext('fillUnderground3DSubject(__subject)', ctx);
  ok('the MRDS card gets the section', slotEl.innerHTML.includes('UNDERGROUND — FROM THE DOCUMENTS'));
  ok('the MRDS card gets the model button',
     slotEl.innerHTML.includes('OPEN 3D MODEL — DESCRIBED WORKINGS')
     && slotEl.innerHTML.includes(`project:&quot;/models/${P}/model.geomodel.json&quot;`));
  ok('the MRDS card fills its documents', slotEl.innerHTML.includes('SOURCE DOCUMENTS'));
}

/* 10 — a dot without a row shows no section at all */
{
  const { ctx, slotEl, fetched } = realm(IDX2, { [P]: CARD });
  await vm.runInContext('fillUnderground3D("usmin:999999")', ctx);
  ok('no row: the slot is removed', slotEl.removed === true);
  ok('no row: nothing was rendered', slotEl.innerHTML === '');
  ok('no row: no card was fetched', !fetched.some(u => u.includes('card.json')));
  const s2 = realm(IDX2, { [P]: CARD });
  s2.ctx.__subject = { ids: ['mrds:1', 'mrds-1'] };
  await vm.runInContext('fillUnderground3DSubject(__subject)', s2.ctx);
  ok('no row for the subject: the slot is removed', s2.slotEl.removed === true && s2.slotEl.innerHTML === '');
}

/* 11 — a missing card.json names the file, never a silent blank */
{
  const { ctx, slotEl } = realm(IDX2, { [P]: CARD });
  await vm.runInContext('fillUnderground3D("ardf:MD012")', ctx);
  const html = slotEl.innerHTML;
  ok('the head still renders from the row', html.includes('OPEN 3D MODEL — DESCRIBED WORKINGS'));
  ok('the failure names the missing file', html.includes('models/ghost-mine-00000000/card.json'));
  ok('the failure says the documents did not load', html.includes('did not load'));
  ok('the placeholder is gone after the failure', !html.includes('loading documents'));
  // a failure is not cached: the next card retries
  ctx.__retry = 0;
  const s2 = realm(IDX2, { 'ghost-mine-00000000': () => Promise.reject(new Error('500 flaky')) });
  await vm.runInContext('fillUnderground3D("ardf:MD012")', s2.ctx);
  await vm.runInContext('fillUnderground3D("ardf:MD012")', s2.ctx);
  ok('a failed card is retried by the next card',
     s2.fetched.filter(u => u.includes('ghost-mine')).length === 2);
}

/* 12 — a compact row with documents but no model says so, and fetches nothing */
{
  const { ctx, slotEl, fetched } = realm(IDX2, { [P]: CARD });
  await vm.runInContext('fillUnderground3D("stategeo:IGS DD-1 IF0126")', ctx);
  const html = slotEl.innerHTML;
  ok('no model: the section renders', html.includes('UNDERGROUND — FROM THE DOCUMENTS'));
  ok('no model: no button', !html.includes('OPEN 3D MODEL'));
  ok('no model: the statement is made', html.includes('nothing is drawn rather than guessed'));
  ok('no model: nothing to fetch', !fetched.some(u => u.includes('card.json')));
}

/* 13 — a model id that is not a model id never becomes a URL */
{
  const { ctx, slotEl, fetched } = realm(IDX2, {});
  await vm.runInContext('fillUnderground3D("usmin:7")', ctx);
  ok('a traversal id is never fetched', !fetched.some(u => u.includes('card.json')));
  ok('a traversal id is reported, not blank', slotEl.innerHTML.includes('did not load'));
  ok('the traversal id is escaped in the message', !slotEl.innerHTML.includes('<../'));
}

console.log(`test_autopop_frontend: ${pass} passed, ${fail} failed`);
if (fail) { for (const f of fails) console.error('  FAIL ' + f); process.exit(1); }
