#!/usr/bin/env node
/* Contract for how the ASK panel routes a question that NAMES something.

   Run: node tools/test_ask_routing.mjs

   The defect this pins down, in the shape it actually shipped in: asked
   "center star mine in idaho", the panel matched "idaho", found no other
   grounded token, and fell into the branch that answers a whole-state
   archive count — replying "19,741 MRDS + state-survey + ARDF occurrence
   records in the immutable baseline archives for ID". Every number in that
   sentence was right and the sentence answered a question nobody asked. The
   AI answerer, which has the tools to do this properly, was never reached,
   because the router treats "I recognised one token" as "I understood the
   question".

   Three things have to hold, and each is a separate way for this to regress:

     * parseQuery must SURFACE the part it could not ground (f.nameq) instead
       of discarding it. A parser that silently drops the subject is how a
       state filter came to stand in for a mine.
     * The whole-archive count branch must refuse to fire while a residual
       name is present, and the named-site branch must sit AHEAD of the
       hasSignal gate — a state alone is not grounding when the question also
       named a subject.
     * Name resolution must not depend on the viewport. The gazetteer is the
       only site lookup in the app that is not scoped to loaded tiles, so it
       is checked against the real shipped shard, not a fixture.

   No browser and no network: index.html is one long inline script that boots
   a map on load, so the regions under test are sliced out by anchor and run
   in a node:vm realm over stubs. An anchor that stops matching fails the run
   rather than quietly testing nothing. */
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
function slice(startMarker, endMarker) {
  const start = only(startMarker), end = only(endMarker);
  if (end <= start) throw new Error(`slice runs backwards: ${startMarker}`);
  return SOURCE.slice(start, end);
}

/* ---------------------------------------------------------------- realm */
const REGIONS = [
  slice("const STATES = ['AL','AK'", 'const GROUPS = ['),
  // siteGazSearch now stamps each row with the document-id spellings, so the
  // shared helper and the slug function it mirrors come along.
  slice('function stateSurveySafeId(value){', 'function stateSurveyNormalize(key,entry){'),
  slice("const MINE_DOC_NAMESPACES = {'national-mrds'", 'function mineSubject(f){'),
  slice('const COMTERMS = [', 'function runQuery(f){'),
  slice('function havKm(la1,lo1,la2,lo2){', 'function initIntel(){'),
  slice('const SITEGAZ = {index:null', '/* ---------- WS10:'),
];
const sandbox = {
  console,
  // parseQuery consults the curated district/town index; a name question is
  // exactly the case where that index has nothing, which is what the router
  // used to have no answer for.
  findPlace: () => null,
  allDistricts: () => [],
  GAZ: {},
  // The gazetteer's only I/O. Reading the shipped shards off disk is the
  // point: a fixture would let the build and the frontend drift apart.
  jget: async (u) => {
    const p = path.join(ROOT, 'site', u);
    if (!fs.existsSync(p)) throw new Error(u + ' 404');
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  },
};
vm.createContext(sandbox);
for (const region of REGIONS) vm.runInContext(region, sandbox);
const { parseQuery, residualName, siteGazSearch, siteGazScore, siteGazCluster } = sandbox;

/* ------------------------------------------------- 1. the parser surfaces
   the subject it could not ground */
const nameq = q => parseQuery(q).nameq;

eq('names the subject of the reported failure', nameq('center star mine in idaho'), 'center star mine');
eq('and still grounds its state', JSON.stringify(parseQuery('center star mine in idaho').states), '["ID"]');
eq('bare name, no state', nameq('center star mine'), 'center star mine');
eq('"tell me about" is not part of a name', nameq('tell me about the bunker hill mine'), 'bunker hill mine');
eq('a head noun with no name in front is not a name', nameq('mines in idaho'), null);
eq('head noun is kept as part of the name', nameq('what is the sunshine mine in idaho'), 'sunshine mine');

// The other half of the contract: an aggregate question must NOT look named,
// or every count query in the app would divert into a name lookup.
eq('commodity + state is not a name', nameq('gold sites in idaho'), null);
eq('count question is not a name', nameq('how many claims in nevada'), null);
eq('status + commodity is not a name', nameq('producing silver mines in montana'), null);
eq('scope word alone is not a name', nameq('workings in washington'), null);
eq('grade superlative is not a name', nameq('richest unclaimed gold'), null);
eq('near-clause place is not a residual name', nameq('gold near philipsburg'), null);
eq('radius clause is consumed', nameq('gold within 40 km of butte'), null);
eq('one short leftover is not a name', nameq('sites in id ok'), null);

/* The whole point of f.nameq is that it fires for a subject and stays quiet
   for an aggregate. A word this parser forgets to list is not a harmless gap:
   it silently diverts a count question into a name lookup. Every phrasing the
   panel's own help text and chips suggest is checked here for that reason. */
const AGGREGATE = [
  'gold sites in idaho', 'how many claims in nevada', 'producing silver mines in montana',
  'workings in washington', 'working mines in idaho', 'old workings in idaho',
  'richest unclaimed gold', 'gold near philipsburg', 'gold within 40 km of butte',
  'antimony occurrences in idaho', 'uranium deposits in wyoming', 'coal in utah',
  'historic gold mines', 'abandoned mines in oregon', 'number of records in nevada',
  'show me all the claims', 'list gold prospects in montana', 'placer gold in idaho',
  'top 10', 'where should i prospect', 'open sections', 'was claimed now open',
  'split estate sections', 'watch alerts', 'help', 'active claims in idaho',
  'how many gold sites are available in washington', 'silver grades in idaho',
  'best gold ground in nevada', 'county gold ranking', 'epithermal gold targets',
  'rare earth sites in idaho', 'what can you do', 'mines in idaho',
  'prospects in montana', 'adits in nevada', 'shafts in utah',
  'lodes in california', 'summary of nevada', 'stone quarries in oregon',
];
for (const q of AGGREGATE) eq(`aggregate stays aggregate: "${q}"`, nameq(q), null);

const NAMED = [
  ['center star mine in idaho', 'center star mine'],
  ['no give me the ai for center star mine in idaho', 'center star mine'],
  ['bunker hill mine', 'bunker hill mine'],
  ['what is the sunshine mine in idaho', 'sunshine mine'],
  ['thunder mountain', 'thunder mountain'],
  ['gold at the golden chest mine', 'golden chest mine'],
  // A district the curated index missed falls through to the archives rather
  // than to a statewide count — the fallback working, not a miss.
  ['tell me about the tenmile district', 'tenmile district'],
];
for (const [q, want] of NAMED) eq(`names its subject: "${q}"`, nameq(q), want);

/* --------------------------------------------- 2. the router honours it */
// These are structural: answer() cannot be run without a map, but the two
// orderings that caused the bug are checkable in the file's own bytes.
const answerBody = slice('function answer(q){', '/* ================= auth');
const nameBranch = answerBody.indexOf('if (f.nameq && !f.wantCount)');
const signalGate = answerBody.indexOf('const hasSignal =');
const countBranch = answerBody.indexOf("if((f.scope==='sites'||f.scope==='usmin')");
ok('named-site branch exists', nameBranch >= 0);
ok('named-site branch outranks the hasSignal gate', nameBranch >= 0 && nameBranch < signalGate,
  `nameq at ${nameBranch}, hasSignal at ${signalGate}`);
ok('named-site branch outranks the whole-archive count branch',
  nameBranch >= 0 && nameBranch < countBranch, `nameq at ${nameBranch}, count at ${countBranch}`);
ok('whole-archive count branch is guarded by !f.nameq',
  /if\(\(f\.scope==='sites'\|\|f\.scope==='usmin'\)&&!f\.near&&!f\.term&&!f\.nameq&&/.test(answerBody));
ok('a named question reaches the AI answerer when it is on',
  /if \(f\.nameq && !f\.wantCount\)\{[\s\S]{0,600}?aiAvailable\(\) && AI\.on\) return routeAI\(q, null, local\)/.test(answerBody));
// The AI is the destination, not a single point of failure: a round that
// throws must land on the archives rather than on "try help for patterns".
ok('a failed AI round falls back to the archives, not to the help text',
  /return routeAI\(q, null, local\)/.test(answerBody) &&
  /nameAnswer\(q, f, false\)/.test(answerBody));
ok('routeAI honours a caller-supplied failure hook',
  /function routeAI\(q, localNote, onFail\)\{[\s\S]{0,400}?if \(onFail\) return void onFail\(e\);/.test(SOURCE));
ok('the fallback path may not re-enter the AI it just saw fail',
  /const giveUp = note => allowAI \? routeAI\(q, note\)/.test(SOURCE));

/* ------------------------------------------- 3. resolution ignores the map */
ok('gazetteer index shipped', fs.existsSync(path.join(ROOT, 'site/data/gazetteer/index.json')));

// Scoring: the tight name must beat the long one that merely contains it.
ok('exact normalised name outranks a longer container',
  siteGazScore('Center Star Mine', 'center star mine') >
  siteGazScore('Center Star Extension Group No. 4', 'center star mine'));
ok('whole-word run outranks a mid-word substring',
  siteGazScore('Center Star Mine', 'center star') > siteGazScore('Epicenter Starr', 'center star'));

const found = await siteGazSearch('center star mine', ['ID'], 8);
// A resolved mine has to reach its own documents without the model inventing
// an id format — and never through a bare numeric one, which collides between
// namespaces (an MRDS Nevada dep_id and an NBMG file number are both integers).
ok('a resolved site carries the id spellings docs_for accepts',
  Array.isArray(found.hits[0].document_ids) && found.hits[0].document_ids.length >= 2,
  JSON.stringify(found.hits[0].document_ids));
ok('and never a bare numeric one',
  found.hits.every(h=>(h.document_ids||[]).every(id=>!/^\d+$/.test(id))));
ok('the click path and the name path spell ids identically',
  JSON.stringify(sandbox.mineDocIds('mrds','10071608')) ===
  JSON.stringify(['mrds:10071608','mrds-10071608']));
ok('gazetteer resolves without any tile being loaded', found.available && found.hits.length >= 2,
  JSON.stringify(found).slice(0, 200));
const lead = found.hits[0];
eq('the producer wins the name', lead.name.startsWith('Center Star Mine'), true);
ok('carries usable coordinates', Math.abs(lead.lat - 45.808) < 0.01 && Math.abs(lead.lon + 115.559) < 0.01,
  `${lead.lat}, ${lead.lon}`);
ok('carries commodities', /silver/i.test(lead.commodities || ''), lead.commodities);
ok('names its archive', ['mrds', 'stategeo'].includes(lead.record_source), lead.record_source);

// The two Tenmile records are one mine in two archives; the Yreka one is a
// different place with the same name and must stay separate.
const clusters = siteGazCluster(found.hits);
ok('co-located records collapse to one mine', clusters[0].records.length === 2,
  JSON.stringify(clusters.map(c => c.records.length)));
ok('a same-named site 150 km away stays separate', clusters.length >= 2,
  `${clusters.length} clusters`);

const missing = await siteGazSearch('zzzz nonexistent lode', ['ID'], 8);
ok('a name the archives do not hold returns nothing, not a fallback count',
  missing.available && missing.hits.length === 0);

const unindexed = await siteGazSearch('center star', ['NY'], 8);
ok('an unindexed state is reported, not silently searched',
  unindexed.available && unindexed.hits.length === 0 &&
  unindexed.unindexed_states.includes('NY'), JSON.stringify(unindexed));

/* ------------------------------------------------ 4. the model gets a tool */
const ASK = fs.readFileSync(path.join(ROOT, 'infra', 'ask_lambda.py'), 'utf8');
ok('resolve_site is declared to the model', /"name": "resolve_site"/.test(ASK));
ok('system prompt sends named questions to resolve_site',
  /NAMES a specific mine, prospect or working, start with resolve_site/.test(ASK));
ok('query_sites now admits it is tile-scoped',
  /tile-scoped and move with the user's viewport/.test(ASK));
ok('frontend implements the tool the backend advertises',
  /if \(name==='resolve_site'\) return execResolveSite\(a\);/.test(SOURCE));
ok('name search falls back off the viewport',
  /if \(a\.name_contains && !tiles\.count\) return execSitesByName\(a, tiles\);/.test(SOURCE));

/* --------------------------------------------------------------- report */
console.log(`\nask routing: ${pass} passed, ${fail} failed`);
if (fail) { console.log(fails.map(f => '  ✗ ' + f).join('\n')); process.exit(1); }
