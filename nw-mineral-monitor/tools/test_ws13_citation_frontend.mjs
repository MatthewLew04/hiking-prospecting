#!/usr/bin/env node
/* Contracts for the WS13 half of the citation path in site/index.html.

   Run: node tools/test_ws13_citation_frontend.mjs

   The defects these pin down, in the order they would bite:

     * s3_key and viewer_key are PRIVATE object keys. They may travel from a
       tool RESULT into browser state and from there into a viewer fragment,
       and they must never be parsed out of the model's answer text, where a
       hallucinated key would become a real signed-URL request for an object
       nobody chose. infra/ask_lambda.py's system prompt forbids the model
       from printing one, but a prompt is a request and not a boundary — the
       boundary is that docChip() looks a sha256 up in DOC_WS13_ROUTES, which
       only rememberDocViewerRoute() writes, and only from a tool result.
     * A link must not assert its own rights class. Everything in the fragment
       is a hint for a viewer that has no manifest for this corpus; the docs
       API re-anchors the key's shape and reads admission_class off its own
       ws12/{class}/ prefix. So the UI's key check is a pre-flight that
       authorizes nothing, and the test says so rather than implying the
       browser is a gate.
     * viewer.html forwards its fragment's s3_key straight to the docs API,
       which fullmatches it against the ws12/ ORIGINAL shape — so putting a
       ws13/searchable/ key in that field would 400 for every one of the
       28,988 OCR documents. viewer_key is captured to reject a pair of
       citation fields that disagree, and is deliberately NOT forwarded.
     * Attribution travels with the citation. 13,013 documents are CC BY-NC-SA
       copies and 32,312 are state-archive research copies, so a citation
       whose rights were never recorded must not become an openable chip, and
       one that cannot be opened must still render its licence.
     * Dark ship. With WS13_RETRIEVAL_ENABLED unset, tool results carry no
       admission_class and no s3_key, so every one of these paths has to
       collapse to the behaviour that shipped before the flag existed.

   No browser and no network. index.html is one 6,300-line inline script that
   boots a map on load, so the four regions this contract is about are sliced
   out by anchor and run in a node:vm realm over stub globals — the file's own
   bytes, not a copy of them. An anchor that stops matching fails the run
   rather than silently testing nothing. */
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
const eq = (name, got, want) => ok(name, got === want, `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);

/* ---------------------------------------------------------------- slicing */
function only(marker) {
  const first = SOURCE.indexOf(marker);
  if (first < 0) throw new Error(`anchor is gone from site/index.html: ${marker}`);
  if (SOURCE.indexOf(marker, first + 1) >= 0)
    throw new Error(`anchor is no longer unique in site/index.html: ${marker}`);
  return first;
}
function slice(startMarker, endMarker) {
  const start = only(startMarker), end = only(endMarker);
  if (end <= start) throw new Error(`slice runs backwards: ${startMarker}`);
  return SOURCE.slice(start, end);
}

const REGIONS = [
  // esc(), which every chip's markup goes through.
  slice('const esc = s =>', '\nconst $ = id =>'),
  // The three registries the whole path turns on.
  slice('let DOCS = null;', '\nlet map, tipEl'),
  // The markdown renderer, which is where an answer's doc: chips are built.
  slice('function mdCells(l){', '/* ---------- WS12 addendum: stored source documents ----------'),
  // The WS12 addendum: resolution, rights, routes, chips, References.
  slice('const docsAvailable = ()=>', '/* --- the query engine --- */'),
];

/* --------------------------------------------------------------- the realm */
const opened = [];
const sandbox = {
  console,
  URL, URLSearchParams, TextEncoder, TextDecoder, atob, btoa,
  // The ASK config and the grade-row table: present so the sliced code can
  // reference them, empty so nothing under test reads a fixture out of them.
  AUTH: { cfg: null },
  GR: null,
  accessToken: () => '',
  fetch: () => { throw new Error('the harness makes no requests'); },
  location: { href: 'https://nwmm.example.test/index.html' },
  window: { open: (url, target, features) => { opened.push({ url, target, features }); return null; } },
};
const context = vm.createContext(sandbox);
const EXPORTS = ['rememberDocRights', 'rememberDocViewerRoute', 'rememberDocMines',
  'rememberDocsForRow', 'rememberSearchDocumentHit', 'rememberDocQuote',
  'ws13Route', 'openWs13Doc', 'openDoc', 'docChip', 'docCiteStatic',
  'docRightsBadge', 'docRefsHtml', 'ws13RefsHtml', 'mdToHtml', 'docFind',
  'docLinkRights', 'WS13_ORIGINAL_KEY', 'ws13SearchableKey', 'DOC_RIGHTS',
  'DOC_RIGHTS_BY_URL', 'DOC_WS13_ROUTES', 'DOC_WS13_BY_MINE'];
vm.runInContext(
  REGIONS.join('\n') +
  `\nglobalThis.API = {${EXPORTS.join(',')}};` +
  '\nglobalThis.setDocs = value => { DOCS = value; };',
  context, { filename: 'site/index.html (sliced)' });
const API = sandbox.API;

/* A session's state is global to the page, so every case clears it first. */
function freshSession() {
  for (const registry of [API.DOC_RIGHTS, API.DOC_RIGHTS_BY_URL,
                         API.DOC_WS13_ROUTES, API.DOC_WS13_BY_MINE])
    for (const key of Object.keys(registry)) delete registry[key];
  opened.length = 0;
  sandbox.setDocs(null);
}

/* ------------------------------------------------------------- fixtures */
// One real document from each half of the corpus, keyed exactly as
// ws13_query_lambda.document_citation() emits them.
const OCR_SHA = '3c3fc7e970db5286640b83c35e886fc07a6db4c415e92782674aa94a3058a9a1';
const BORN_SHA = 'd29aab7b4e9fcde0e084dddc84ef9da37d0c15860af4674bf58bd0decd71e07f';
const OCR_KEY = `ws12/originals/igs_mines/3c/${OCR_SHA}.pdf`;
const OCR_SEARCHABLE = `ws13/searchable/3c/${OCR_SHA}/searchable.pdf`;
// The flat archive shape, which names no digest at all.
const BORN_KEY = 'ws12/research-copies/IF0131_001.pdf';

const citation = (changes = {}) => Object.assign({
  document_title: 'Mines and Prospects of Idaho, DD-1',
  page: 4,
  source_url: null,
  markdown: `[Mines and Prospects of Idaho, DD-1, p. 4](doc:${OCR_SHA}#4)`,
  sha256: OCR_SHA,
  s3_key: OCR_KEY,
  viewer_key: OCR_SEARCHABLE,
  viewer_key_kind: 'searchable',
  admission_class: 'research-copies',
  rights_basis: 'Idaho Geological Survey state archive',
  rights_terms: 'state-archive research copy, not redistributable',
  attribution_required: true, non_commercial: true, share_alike: false,
  resolvable_via: 'stored_copy',
}, changes);

// One row of a WS13 docs_for result: rights and citation on the row, no page.
const documentRow = (changes = {}) => Object.assign({
  document_id: OCR_SHA, sha256: OCR_SHA,
  title: 'Mines and Prospects of Idaho, DD-1',
  portal: 'igs-mines', state: 'ID', county: 'Cassia County',
  mine_ids: ['IF0126', 'ADMM-01234'], mine_names: ['St. Louis Mine'],
  page_count: 118, indexed_pages: 118, source_url: null,
  indexed: true, embedded: true,
  admission_class: 'research-copies',
  rights_basis: 'Idaho Geological Survey state archive',
  rights_terms: 'state-archive research copy, not redistributable',
  attribution_required: true, non_commercial: true, share_alike: false,
  citation: citation({ page: null, markdown: `[Mines and Prospects of Idaho, DD-1](doc:${OCR_SHA})` }),
}, changes);

// One row of the LEGACY WS12 docs_for result — what ws12DocsFor() returns and
// what the SQLite tool returns with WS13_RETRIEVAL_ENABLED unset. No rights,
// no keys, no admission class.
const legacyRow = (changes = {}) => Object.assign({
  document_id: OCR_SHA, title: 'Mines and Prospects of Idaho, DD-1',
  portal: 'igs-mines', portal_id: 'igs-mines', state: 'ID',
  county: 'Cassia County', trs: 'T03N R24E S15', doc_date: '1948',
  doc_type: 'mine file', page_count: 118, indexed_pages: 118,
  source_url: 'https://www.idahogeology.org/product/DWM-49', mine_names: [],
  citation: { document_title: 'Mines and Prospects of Idaho, DD-1',
    source_url: 'https://www.idahogeology.org/product/DWM-49' },
}, changes);

// A refused open returns null; read it as an empty fragment so a broken
// case reports a failure rather than killing the run on a null dereference.
const urlOf = result => (result && result.url) || '';
const fragment = result => new URLSearchParams(urlOf(result).split('#')[1] || '');

/* ============================ 1. a captured route opens the right document */
{
  freshSession();
  const recorded = API.rememberDocsForRow(documentRow(), 'stategeo-igs-dd-1-if0126');
  ok('capture: a full-corpus docs_for row records a route', !!API.ws13Route(OCR_SHA), String(recorded));
  const result = API.openDoc(OCR_SHA);
  ok('open: a routed document opens', !!result && !!result.url, JSON.stringify(result));
  const f = fragment(result);
  ok('open: the viewer is asked for the WS13 corpus', urlOf(result).startsWith('viewer.html#'), urlOf(result));
  eq('fragment: corpus', f.get('corpus'), 'ws13');
  eq('fragment: doc', f.get('doc'), OCR_SHA);
  eq('fragment: s3_key is the STORED ORIGINAL, the only shape the docs API accepts', f.get('s3_key'), OCR_KEY);
  eq('fragment: rights_basis rides with the link', f.get('rights_basis'), 'Idaho Geological Survey state archive');
  eq('fragment: viewer_key_kind', f.get('viewer_key_kind'), 'searchable');
  eq('fragment: title, because this corpus has no manifest to look one up in', f.get('title'), 'Mines and Prospects of Idaho, DD-1');
  // A listing row names no page; opening one must not invent one beyond the
  // document's own first page.
  eq('fragment: a page-less listing opens at page 1', f.get('page'), '1');
  ok('fragment: the searchable key is NOT forwarded — the docs API would 400 on it',
    !!urlOf(result) && !urlOf(result).includes('ws13/searchable') && !urlOf(result).includes('viewer_key='),
    urlOf(result));
  eq('open: exactly one window was opened', opened.length, 1);
  eq('open: the tab is opened noopener', opened.length && opened[0].features, 'noopener');
}

/* ============ 2. a cited page and a quote survive into the fragment

   The page reaches the viewer the way it reaches every other chip: through
   the chip's own data-page, which ws13RefsHtml() fills from the route's
   remembered page and the answer-text rule fills from the markdown's '#42'.
   NOTE which path this does NOT exercise: openWs13Doc's own `|| route.page`
   fallback is unreachable, because Math.max(1, ...) makes its left operand
   truthy for every input — openDoc(ref) with no page argument opens page 1
   rather than the cited page. Nothing in site/index.html calls it that way
   today (the one call site is the chip's onclick, which always passes a
   page), so this is a dead branch and not a live defect. */
{
  freshSession();
  const hit = { sha256: OCR_SHA, page: 42, excerpt: '…averaged 14.2 ounces of silver per ton…',
    citation: citation({ page: 42 }), metadata: { mine_ids: ['IF0126'] } };
  API.rememberSearchDocumentHit(hit);
  ok('search hit: the document is filed against the mine the RESULT named',
    (API.DOC_WS13_BY_MINE['IF0126'] || []).includes(OCR_SHA),
    JSON.stringify(API.DOC_WS13_BY_MINE));
  const refs = API.ws13RefsHtml('IF0126');
  ok('search hit: the References chip opens where something was actually read',
    refs.includes('data-page="42"') && refs.includes('open p. 42'), refs.slice(0, 400));
  ok('search hit: the excerpt travels as the highlight, ellipses trimmed',
    refs.includes('data-quote="averaged 14.2 ounces of silver per ton"'), refs.slice(0, 400));
  const f = fragment(API.openDoc(OCR_SHA, 42, 'averaged 14.2 ounces of silver per ton'));
  eq('search hit: the fragment carries that page', f.get('page'), '42');
  eq('search hit: and that quote', f.get('q'), 'averaged 14.2 ounces of silver per ton');
  eq('search hit: still the stored original, never the searchable copy', f.get('s3_key'), OCR_KEY);
}

/* ================= 3. an uncaptured sha256 renders inert, never guessed at */
{
  freshSession();
  // The answer text names a document. No tool returned it: this is the shape
  // of a hallucinated citation, and of a real one whose tool result the
  // browser never saw.
  const html = API.mdToHtml(`See [Mines and Prospects of Idaho, DD-1, p. 4](doc:${OCR_SHA}#4).`);
  ok('answer text: an uncaptured id renders as inert cited text', html.includes('docstatic'), html.slice(0, 220));
  ok('answer text: it is not painted as an openable chip', !html.includes('docchip'), html.slice(0, 220));
  ok('answer text: no object key appears in the markup',
    !html.includes('ws12/') && !html.includes('ws13/'), html.slice(0, 220));
  eq('answer text: chip markup is refused outright', API.docChip(OCR_SHA, 4, 'DD-1, p. 4'), '');
  eq('answer text: opening it resolves to nothing', API.openDoc(OCR_SHA, 4), null);
  eq('answer text: and opens no window', opened.length, 0);
  eq('answer text: no route was invented for it', API.ws13Route(OCR_SHA), null);
}

/* === 4. a routed document cited in answer text still keeps its key out of the DOM */
{
  freshSession();
  API.rememberDocsForRow(documentRow(), 'IF0126');
  const html = API.mdToHtml(`See [Mines and Prospects of Idaho, DD-1, p. 4](doc:${OCR_SHA}#4).`);
  ok('answer text: a captured id IS painted as a chip', html.includes('docchip'), html.slice(0, 200));
  ok('answer text: the chip carries no object key — the route stays in JS state',
    !html.includes('ws12/') && !html.includes('ws13/'), html.slice(0, 400));
  ok('answer text: the chip opens through openDoc, which re-reads the registry',
    html.includes('onclick="openDoc(this.dataset.doc'), html.slice(0, 400));
  ok('answer text: the rights badge rides with it',
    html.includes('RESEARCH COPY'), html.slice(0, 400));
}

/* ================================ 5. what rememberDocViewerRoute refuses */
{
  const refusals = [
    ['a searchable key in the s3_key field (the docs API 400s on it)', OCR_SHA, citation({ s3_key: OCR_SEARCHABLE })],
    ['a traversal segment', OCR_SHA, citation({ s3_key: 'ws12/originals/../../etc/passwd.pdf' })],
    ['a leading slash', OCR_SHA, citation({ s3_key: `/ws12/originals/igs_mines/3c/${OCR_SHA}.pdf` })],
    ['an embedded newline', OCR_SHA, citation({ s3_key: `ws12/originals/igs_mines/3c/${OCR_SHA}.pdf\nws12/x.pdf` })],
    ['a key outside the two admitted prefixes', OCR_SHA, citation({ s3_key: `ws12/private/3c/${OCR_SHA}.pdf` })],
    ['a filename digest naming another document', OCR_SHA, citation({ s3_key: `ws12/originals/igs_mines/d2/${BORN_SHA}.pdf` })],
    ['a viewer_key naming neither of this document\'s two objects', OCR_SHA,
      citation({ viewer_key: `ws13/searchable/d2/${BORN_SHA}/searchable.pdf` })],
    ['a sha256 PREFIX, which this corpus has no index to resolve', OCR_SHA.slice(0, 16), citation({ sha256: OCR_SHA.slice(0, 16) })],
    ['a citation whose rights were never stated', OCR_SHA, citation({ admission_class: '' })],
    ['nothing at all', OCR_SHA, null],
  ];
  for (const [label, docId, cite] of refusals) {
    freshSession();
    if (cite) API.rememberDocRights(docId, cite);
    const recorded = API.rememberDocViewerRoute(docId, cite);
    ok(`refuse: ${label}`, recorded === false && !API.ws13Route(docId), `recorded=${recorded}`);
    eq(`refuse: ${label} — and nothing opens`, API.openWs13Doc(docId, 1), null);
  }
}

/* ====== 5b. a document we cannot open is never listed as a reference */
{
  freshSession();
  // Rights stated, but the key names the searchable copy: openable is exactly
  // what this document is not. The References block would otherwise print a
  // title and a licence line with no button under it.
  API.rememberDocsForRow(documentRow({
    citation: citation({ page: null, s3_key: OCR_SEARCHABLE }) }), 'IF0126');
  eq('unopenable: the rights are still recorded',
    (API.DOC_RIGHTS[OCR_SHA] || {}).admission_class, 'research-copies');
  eq('unopenable: no route is recorded', API.ws13Route(OCR_SHA), null);
  eq('unopenable: it is filed against no mine', JSON.stringify(API.DOC_WS13_BY_MINE), '{}');
  eq('unopenable: and the References block lists nothing', API.ws13RefsHtml('IF0126'), '');
  // Same rule on the search-hit path, where the mine ids come off the hit's
  // own metadata rather than off the result envelope.
  freshSession();
  API.rememberSearchDocumentHit({ sha256: OCR_SHA, page: 4,
    citation: citation({ s3_key: OCR_SEARCHABLE }),
    metadata: { mine_ids: ['IF0126'] } });
  eq('unopenable: an unopenable search hit is filed against no mine either',
    JSON.stringify(API.DOC_WS13_BY_MINE), '{}');
}

/* ============= 6. the born-digital half of the corpus still opens */
{
  freshSession();
  const born = citation({ sha256: BORN_SHA, s3_key: BORN_KEY, viewer_key: BORN_KEY,
    viewer_key_kind: 'born_digital_original', document_title: 'IF0131 mine file' });
  ok('born digital: the flat archive shape is accepted', API.rememberDocRights(BORN_SHA, born) === undefined &&
    API.rememberDocViewerRoute(BORN_SHA, born) === true);
  const f = fragment(API.openDoc(BORN_SHA));
  eq('born digital: the original is its own servable object', f.get('s3_key'), BORN_KEY);
  eq('born digital: the kind hint survives, so the viewer keeps offering text search',
    f.get('viewer_key_kind'), 'born_digital_original');
}

/* ================================= 7. dark ship: the legacy docs_for row */
{
  freshSession();
  API.rememberDocsForRow(legacyRow(), 'stategeo-igs-dd-1-if0126');
  eq('dark ship: a legacy row records no route', API.ws13Route(OCR_SHA), null);
  eq('dark ship: and no rights, because it carries no admission class',
    JSON.stringify(API.DOC_RIGHTS), '{}');
  eq('dark ship: it is filed against no mine', JSON.stringify(API.DOC_WS13_BY_MINE), '{}');
  eq('dark ship: the References block renders nothing for that mine',
    API.ws13RefsHtml('stategeo-igs-dd-1-if0126'), '');
  eq('dark ship: openDoc is the null it always was', API.openDoc(OCR_SHA), null);
  eq('dark ship: docChip is the empty string it always was', API.docChip(OCR_SHA, 1, 'DD-1'), '');
  eq('dark ship: nothing opened', opened.length, 0);
  // A WS12 search hit with no admission_class is the flag-off shape too.
  API.rememberSearchDocumentHit({ document_id: OCR_SHA, page: 4,
    citation: { document_title: 'DD-1', source_url: 'https://example.test/a.pdf' } });
  eq('dark ship: a flag-off search hit records no route', API.ws13Route(OCR_SHA), null);
}

/* ================ 8. a WS12 manifest document is untouched by any of this */
{
  freshSession();
  const WS12_ID = 'a'.repeat(64);
  const doc = { doc_id: WS12_ID, title: 'IF0126 mine file', pages: 1,
    authority: 'Idaho Geological Survey', source_url: 'https://example.test/if0126.pdf',
    text_layer: { status: 'ocr' } };
  const cite = { doc_id: WS12_ID, page: 1, page_cite: 'p. 1', quote: 'LAVA CREEK DISTRICT',
    quote_located: true, state: 'ID', mine_id: 'stategeo-igs-dd-1-if0126' };
  sandbox.setDocs({ documents: [doc], citations: [cite], byId: { [WS12_ID]: doc },
    bySource: {}, byQuote: { 'lava creek district': cite }, byUrl: {}, byTitle: {},
    byDocPage: { [`${WS12_ID}/1`]: [cite] },
    bySubject: { 'ID/stategeo-igs-dd-1-if0126': [cite] } });
  const result = API.openDoc(WS12_ID, 1, 'LAVA CREEK DISTRICT');
  eq('ws12: the manifest fragment is unchanged',
    urlOf(result), 'viewer.html#doc=' + WS12_ID + '&page=1&q=LAVA+CREEK+DISTRICT');
  ok('ws12: it names no corpus and carries no key',
    !urlOf(result).includes('corpus=') && !urlOf(result).includes('s3_key='), urlOf(result));
  // The WS13 route registry must not be able to shadow a manifest document:
  // docFind() runs first, and a manifest hit never reaches openWs13Doc.
  API.rememberDocViewerRoute(WS12_ID, citation({ sha256: WS12_ID, s3_key: `ws12/originals/igs_mines/aa/${WS12_ID}.pdf` }));
  const again = API.openDoc(WS12_ID, 1, 'LAVA CREEK DISTRICT');
  eq('ws12: a captured WS13 route does not divert a manifest document',
    urlOf(again), 'viewer.html#doc=' + WS12_ID + '&page=1&q=LAVA+CREEK+DISTRICT');
}

/* ====================== 9. the References block states what its count is */
{
  freshSession();
  API.rememberDocsForRow(documentRow(), 'IF0126');
  API.rememberDocsForRow(documentRow({
    document_id: BORN_SHA, sha256: BORN_SHA, title: 'IF0131 mine file',
    admission_class: 'licensed-copies',
    rights_basis: 'Arizona Geological Survey document repository',
    rights_terms: 'CC BY-NC-SA 4.0 — Arizona Geological Survey document repository',
    share_alike: true,
    citation: citation({ sha256: BORN_SHA, s3_key: BORN_KEY, viewer_key: BORN_KEY,
      viewer_key_kind: 'born_digital_original', page: null,
      document_title: 'IF0131 mine file',
      admission_class: 'licensed-copies',
      rights_basis: 'Arizona Geological Survey document repository',
      rights_terms: 'CC BY-NC-SA 4.0 — Arizona Geological Survey document repository',
      share_alike: true }),
  }), 'IF0126');
  const html = API.ws13RefsHtml('IF0126');
  ok('references: both documents are listed', html.includes(OCR_SHA.slice(0, 12)) && html.includes(BORN_SHA.slice(0, 12)));
  ok('references: the count is labelled as what this session saw, not as the mine\'s file',
    /2 SEEN THIS SESSION, NOT THIS MINE'S WHOLE FILE/.test(html), html.slice(0, 300));
  ok('references: every entry carries its licence beside it',
    html.includes('state-archive research copy') && html.includes('CC BY-NC-SA 4.0'), html.slice(0, 900));
  ok('references: the share-alike copy is badged as such', html.includes('CC BY-NC-SA<'), html.slice(0, 900));
  ok('references: no object key is rendered', !html.includes('ws12/') && !html.includes('ws13/'), html.slice(0, 900));
}

/* ===== 10. rights without a route: still stated, still not openable */
{
  freshSession();
  const unroutable = citation({ s3_key: '' });          // rights known, key not
  API.rememberDocRights(OCR_SHA, unroutable);
  eq('rights only: no route is recorded', API.rememberDocViewerRoute(OCR_SHA, unroutable), false);
  const html = API.mdToHtml(`[DD-1, p. 4](doc:${OCR_SHA}#4)`);
  ok('rights only: the citation still renders its licence', html.includes('RESEARCH COPY'), html.slice(0, 300));
  ok('rights only: as inert text, not a chip', html.includes('docstatic') && !html.includes('docchip'), html.slice(0, 300));
}

/* ========== 11. the capture site the vm cannot reach, read as source */
{
  // aiAnswer() is bound to fetch, the DOM and the ASK endpoint, so its
  // docs_for capture cannot run here; these read its bytes instead.
  //
  // An earlier version of this block asserted that out.mine_id is "the
  // RESULT's echo of the subject, never the model's input". That was wrong,
  // and pinning it made the claim look verified: both backends echo the
  // model's own argument straight back (ws13_query_lambda.documents()
  // republishes filters.mine_id after strip()[:256]; document_tools._docs_for
  // does the same), so the two strings are equal and reading one instead of
  // the other checks nothing about the subject. What can be pinned is that
  // the capture is guarded on a real documents array, and that every row is
  // ALSO filed under row.mine_ids, which is corpus data rather than an echo —
  // that is the half that holds when the mine key does not.
  const call = SOURCE.match(/for \(const row of out\.documents\) rememberDocsForRow\(row, ([^)]+)\)/);
  ok('capture site: docs_for rows are captured in the tool loop', !!call, String(call));
  const guard = SOURCE.match(/if \(c\.toolUse\.name==='docs_for' && out && Array\.isArray\(out\.documents\)\)/);
  ok('capture site: guarded on a real documents array', !!guard);
  ok('capture site: the comment no longer claims the mine id is not model-authored',
    !/RESULT's echo of the\s+\/\/\s+subject, not from c\.toolUse\.input/.test(SOURCE) &&
    !/the RESULT's echo of the subject, not from/.test(SOURCE));

  freshSession();
  // The row's own mine_ids reach the index whatever the second argument is:
  // a mine the model invented gets the documents the corpus actually returned
  // for it, and those documents stay reachable under their corpus ids too.
  API.rememberDocsForRow(documentRow(), 'a-mine-id-the-model-made-up');
  ok('capture site: the row is filed under its corpus mine ids as well',
    (API.DOC_WS13_BY_MINE['IF0126'] || []).includes(OCR_SHA) &&
    (API.DOC_WS13_BY_MINE['ADMM-01234'] || []).includes(OCR_SHA),
    JSON.stringify(API.DOC_WS13_BY_MINE));
}

/* ========== 12. a page-less citation still carries its licence */
{
  // ws13_query_lambda.document_citation() emits [title](source_url) for a
  // document with a live publisher URL: no page, because nothing in a listing
  // was matched to one. docChipForCitation() requires a 'p. N' in the label,
  // so that markdown falls through to the plain http-link rule and used to
  // render as a bare <a> with no terms beside it — on a corpus that is 13,013
  // CC BY-NC-SA copies and 32,312 research copies.
  freshSession();
  const url = 'https://repository.azgs.az.gov/item/admmr-000123.pdf';
  API.rememberDocsForRow(documentRow({
    source_url: url,
    admission_class: 'licensed-copies',
    rights_basis: 'Arizona Geological Survey, collection admmr-mine-files',
    rights_terms: 'CC BY-NC-SA 4.0 - attribution required, non-commercial use only, share-alike',
    share_alike: true,
    citation: citation({ page: null, source_url: url,
      admission_class: 'licensed-copies',
      rights_basis: 'Arizona Geological Survey, collection admmr-mine-files',
      rights_terms: 'CC BY-NC-SA 4.0 - attribution required, non-commercial use only, share-alike',
      share_alike: true, resolvable_via: 'source_url',
      markdown: `[Mines and Prospects of Idaho, DD-1](${url})` }),
  }), 'IF0126');
  const html = API.mdToHtml(`[Mines and Prospects of Idaho, DD-1](${url})`);
  ok('page-less link: renders as an outbound link, not a chip',
    html.includes('<a href=') && !html.includes('docchip'), html.slice(0, 300));
  ok('page-less link: carries its licence badge', html.includes('CC BY-NC-SA<'), html.slice(0, 300));
  ok('page-less link: the full terms ride in the title attribute',
    html.includes('attribution required') && html.includes('Arizona Geological Survey'),
    html.slice(0, 400));
  ok('page-less link: no object key is rendered',
    !html.includes('ws12/') && !html.includes('ws13/'), html.slice(0, 400));

  // A public-domain original has no obligation to state, so the link is left
  // exactly as it rendered before this path existed.
  freshSession();
  const pd = 'https://pubs.usgs.gov/of/1999/0123/report.pdf';
  API.rememberDocsForRow(documentRow({
    source_url: pd, admission_class: 'originals',
    rights_basis: null, rights_terms: 'public domain (US federal / state survey public record)',
    attribution_required: false, non_commercial: false,
    citation: citation({ page: null, source_url: pd, admission_class: 'originals',
      rights_basis: null, attribution_required: false, non_commercial: false,
      resolvable_via: 'source_url', markdown: `[USGS OFR 99-123](${pd})` }),
  }), 'IF0126');
  eq('page-less link: a public-domain link is untouched',
    API.mdToHtml(`[USGS OFR 99-123](${pd})`),
    `<a href="${pd}" target="_blank">USGS OFR 99-123</a>`);

  // Dark ship: a legacy WS12 row carries no admission_class, so nothing is
  // recorded and the link renders exactly as it always did.
  freshSession();
  API.rememberDocsForRow(legacyRow(), 'IF0126');
  eq('page-less link: inert with WS13_RETRIEVAL_ENABLED off',
    API.mdToHtml('[Mines and Prospects of Idaho, DD-1](https://www.idahogeology.org/product/DWM-49)'),
    '<a href="https://www.idahogeology.org/product/DWM-49" target="_blank">' +
    'Mines and Prospects of Idaho, DD-1</a>');
}

console.log(`\nws13 citation frontend: ${pass} passed, ${fail} failed`);
if (fail) { console.log('\nFAILURES:'); for (const item of fails) console.log('  ✗ ' + item); process.exit(1); }
