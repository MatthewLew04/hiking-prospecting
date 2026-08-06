#!/usr/bin/env python3
"""WS5 — county-direct claim extraction for the AOI.

Claims become real at the county recorder before BLM ever sees them: the
location notice is recorded under state law first (Idaho Code tit. 47
ch. 15) and the FLPMA filing with BLM is due within 90 days (43 U.S.C.
§ 1744) — plus adjudication/indexing lag before a case shows in MLRS. The
gap between those two moments is this workstream's signal.

Reality check (verified 2026-08-06): Cassia County has NO online
recorded-document index — records vault by appointment, or a records
request to recorder@cassia.gov. So the Cassia adapter is OPERATOR-ASSISTED
by design, per the working agreement (respect ToS, no scraping walled or
robots-disallowed portals, degrade gracefully):

  1. it emits a prefilled records-request (doc types × date range ×
     townships of interest) you can paste into an email to the recorder;
  2. you drop whatever comes back — or an export from any county portal
     you search in a browser — into  data-inbox/county/<county>/  as CSV /
     TSV / JSON (headers are sniffed, many aliases accepted);
  3. it normalizes + classifies instruments, extracts TRS from legals,
     fuzzy-matches each record to its MLRS serial (claim name + TRS — the
     public GIS carries NO claimant names, so parties corroborate but
     never drive the match), and attaches matches to the WS3 dossiers;
  4. records that look like NEW LOCATIONS with no MLRS match become the
     new WS2 signal class  COUNTY-RECORDED — NOT IN MLRS ; county
     assessment-work affidavits matched to a serial become
     ASSESSMENT FILED (COUNTY)  maintenance evidence.

Coverage matrix lives in config/county_portals.json (per county:
scrape / bulk-export / operator-export / manual-request / unavailable /
unverified) and is written out to COUNTY-COVERAGE.md + the county JSON so
the UI can show its work.

Usage:  python3 county_records.py            # real run (inbox may be empty)
        python3 county_records.py --demo     # also ingest demo/county_sample.csv
Output: site/data/county/{aoi}.json, COUNTY-COVERAGE.md
"""
import csv, io, json, os, re, sys
from datetime import date, datetime
from difflib import SequenceMatcher

from common import load_aoi, HERE, SITE, TODAY, TRS_RE, frstdivid, write_json, update_manifest

ROOT = os.path.normpath(os.path.join(HERE, '..'))
INBOX = os.path.join(ROOT, 'data-inbox', 'county')
PORTALS = json.load(open(os.path.join(HERE, 'config', 'county_portals.json')))

# ---------------------------------------------------------------- normalize
HEADER_ALIASES = {
    'instrument': ['instrument', 'instrument no', 'instrument_no', 'instrument number',
                   'inst', 'inst no', 'doc', 'doc no', 'docnum', 'doc_no', 'document',
                   'document no', 'document number', 'recording no', 'rec no', 'file no',
                   'entry', 'entry no'],
    'doc_type':   ['doc type', 'doc_type', 'type', 'document type', 'instrument type',
                   'kind', 'title'],
    'recorded':   ['recorded', 'record date', 'recording date', 'date recorded',
                   'rec date', 'recorded date', 'date', 'filed', 'file date'],
    'grantor':    ['grantor', 'grantors', 'from', 'party 1', 'party1', 'direct', 'first party'],
    'grantee':    ['grantee', 'grantees', 'to', 'party 2', 'party2', 'indirect',
                   'reverse', 'second party'],
    'legal':      ['legal', 'legal description', 'legal_description', 'description',
                   'remarks', 'comments', 'notes', 'trs', 'location'],
    'book':       ['book', 'bk'],
    'page':       ['page', 'pg'],
    'claim_name': ['claim', 'claim name', 'claim_name', 'mine', 'mine name'],
}


def _canon_header(h):
    return re.sub(r'[^a-z0-9 ]+', ' ', (h or '').lower()).strip()


def map_headers(headers):
    out = {}
    canon = [_canon_header(h) for h in headers]
    for field, aliases in HEADER_ALIASES.items():
        for a in aliases:
            if a in canon:
                out[field] = headers[canon.index(a)]
                break
    return out


DOC_RULES = [
    ('AMENDED LOCATION', re.compile(r'amend', re.I)),
    ('NOTICE OF LOCATION', re.compile(r'notice\s+of\s+loc|location\s+notice|\bNOL\b|'
                                      r'certificate\s+of\s+location', re.I)),
    ('MINING CLAIM', re.compile(r'mining\s+claim|lode\s+(claim|location)|'
                                r'placer\s+(claim|location)|mill\s?site|tunnel\s+site', re.I)),
    ('AFFIDAVIT OF ASSESSMENT', re.compile(r'affidavit|assessment\s+work|proof\s+of\s+labor|'
                                           r'annual\s+labor|intent(ion)?\s+to\s+hold', re.I)),
    ('QUITCLAIM', re.compile(r'quit\s?claim', re.I)),
    ('DEED', re.compile(r'\bdeed\b|conveyance', re.I)),
]
NEW_LOCATION_TYPES = {'NOTICE OF LOCATION', 'MINING CLAIM', 'AMENDED LOCATION'}
MINING_CTX = re.compile(r'claim|lode|placer|mill\s?site|mineral|mining|prospect|vein', re.I)


def classify_doc(doc_type_text, legal_text):
    blob = f'{doc_type_text or ""} {legal_text or ""}'
    # pass 1: the recorder's own doc-type column is authoritative
    for label, rx in DOC_RULES:
        if rx.search(doc_type_text or ''):
            if label in ('QUITCLAIM', 'DEED') and not MINING_CTX.search(blob):
                return label, False          # recorded, but no mining context found
            return label, True
    # pass 2: fall back to the legal/remarks text
    for label, rx in DOC_RULES:
        if label != 'DEED' and rx.search(blob):
            return label, True
    return 'OTHER', bool(MINING_CTX.search(blob))


_NAME_STOP = {'amended', 'amendment', 'location', 'relocation', 'notice', 'certificate',
              'of', 'the', 'for', 'in', 'to', 'on', 'a', 'an', 'interest', 'labor',
              'annual', 'proof', 'work', 'assessment', 'affidavit', 'quitclaim', 'deed',
              'mining', 'lode', 'placer', 'and', 'known', 'as', 'called'}
_NAME_TERM = re.compile(r"((?:[A-Z0-9][\w#\-\.']*\s+){1,6}?)(?:lode|placer|quartz|"
                        r"mill\s?site|mining\s+claim|claims?)\b")
_DOCWORDS = re.compile(r'\b(notice|location|affidavit|assessment|labor|proof|quitclaim|'
                       r'deed|amend\w*|annual|recorded?|county|section|township|range)\b', re.I)


def extract_claim_name(legal):
    """Best-effort claim name from a legal/remarks blob."""
    if not legal:
        return None
    m = re.search(r'["“]([^"”]{2,60})["”]', legal)          # quoted name wins
    if m:
        return m.group(1).strip()
    m = _NAME_TERM.search(legal)
    if m:
        toks = m.group(1).strip().split()
        while toks and toks[0].lower().strip('.,') in _NAME_STOP:
            toks.pop(0)
        if toks:
            return ' '.join(toks).strip(' ,.')
    # comma-segment fallback: first short segment that isn't doc boilerplate or a TRS
    for seg in re.split(r'[,;]', legal):
        seg = seg.strip()
        if not seg or len(seg.split()) > 5 or TRS_RE.search(seg) or _DOCWORDS.search(seg):
            continue
        toks = seg.split()
        while toks and toks[0].lower().strip('.,') in _NAME_STOP:
            toks.pop(0)
        if toks and re.search(r'[A-Za-z]{2}', ' '.join(toks)):
            return ' '.join(toks).strip(' ,.')
    return None


DATE_FORMATS = ['%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%m-%d-%Y', '%d-%b-%Y', '%b %d, %Y',
                '%B %d, %Y', '%Y%m%d']


def parse_date(s):
    s = (s or '').strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
    return f'{m.group(1)}-{m.group(2)}-{m.group(3)}' if m else None


def trs_sections(text, aoi):
    """All FRSTDIVIDs mentioned in a legal-description blob."""
    out = set()
    for m in TRS_RE.finditer(text or ''):
        t, td, r, rd, s = int(m.group(1)), m.group(2).upper(), int(m.group(3)), \
                          m.group(4).upper(), int(m.group(5))
        out.add(frstdivid(aoi['plss_state_meridian'], t, td, r, rd, s))
        # 'Secs 14 and 15' / 'Sec 14, 15' — grab trailing extra section numbers
        tail = (text or '')[m.end():m.end() + 40]
        for em in re.finditer(r'(?:^|,|\band\b|&)\s*(\d{1,2})\b', tail):
            try:
                out.add(frstdivid(aoi['plss_state_meridian'], t, td, r, rd, int(em.group(1))))
            except ValueError:
                pass
    return sorted(out)


# ---------------------------------------------------------------- matching
CANON_RE = re.compile(r'\b(the|mine|mines|mining|claims?|group|lode|placer|prospect|'
                      r'property|tunnel|shaft|no\.?\s*\d+|#\s*\d+|\d+)\b')


def canon(name):
    n = re.sub(r'\s*\([^)]*\)', '', (name or '').lower())
    n = CANON_RE.sub(' ', n)
    n = re.sub(r'[^a-z ]+', ' ', n)
    return re.sub(r'\s+', ' ', n).strip()


def name_number(name):
    m = re.search(r'(?:no\.?\s*|#\s*)?(\d+)\s*$', (name or '').strip(), re.I)
    return m.group(1).lstrip('0') if m else None


def similarity(a, b):
    """Blend of char-level ratio and token overlap on canonical names."""
    ca, cb = canon(a), canon(b)
    if not ca or not cb:
        return 0.0
    r = SequenceMatcher(None, ca, cb).ratio()
    ta, tb = set(ca.split()), set(cb.split())
    j = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    s = 0.6 * r + 0.4 * j
    # numbered series ('PMG 370' vs 'PMG 371'): same stem, different number
    na, nb = name_number(a), name_number(b)
    if na and nb and na != nb:
        s -= 0.25
    elif na and nb and na == nb:
        s = min(1.0, s + 0.15)
    return s


def build_token_index(claims):
    idx = {}
    for c in claims:
        for t in set(canon(c['name']).split()):
            idx.setdefault(t, []).append(c)
    return idx


def match_record(rec, claims, by_sec, tok_idx):
    """Best MLRS candidates for a normalized county record.
    Returns (match|None, candidates[]) — match carries a confidence tier."""
    cands = {}
    pool = []
    seen = set()
    if rec['secs']:
        for s in rec['secs']:
            for c in by_sec.get(s, []):
                if id(c) not in seen:
                    seen.add(id(c)); pool.append(c)
    name = rec.get('claim_name')
    if name:
        # name search across the whole AOI too (legal may be missing/wrong);
        # token index keeps this linear instead of records × claims
        for t in set(canon(name).split()):
            for c in tok_idx.get(t, []):
                if id(c) not in seen:
                    seen.add(id(c)); pool.append(c)
    for c in pool:
        sim = similarity(name, c['name']) if name else 0.0
        sec_hit = bool(set(rec['secs']) & set(c.get('secs') or []))
        if not name and not sec_hit:
            continue
        if sim >= 0.9 and sec_hit:
            conf = 'HIGH'
        elif sim >= 0.9 or (sim >= 0.72 and sec_hit):
            conf = 'MEDIUM'
        elif (sim >= 0.55 and sec_hit) or (not name and sec_hit and
                                           rec['type'] == 'AFFIDAVIT OF ASSESSMENT'):
            conf = 'LOW'
        else:
            continue
        score = sim + (0.5 if sec_hit else 0) + (0.1 if c.get('_active') else 0)
        prev = cands.get(c['ser'])
        if not prev or score > prev[0]:
            cands[c['ser']] = (score, conf, sim, sec_hit, c)
    ranked = sorted(cands.values(), key=lambda t: -t[0])[:3]
    out = [{'ser': c['ser'], 'name': c['name'], 'conf': conf, 'sim': round(sim, 2),
            'sec': sec_hit, 'active': bool(c.get('_active'))}
           for score, conf, sim, sec_hit, c in ranked]
    return (out[0] if out else None), out


# ---------------------------------------------------------------- ingest
def read_rows(path):
    if path.lower().endswith('.json'):
        j = json.load(open(path))
        rows = j if isinstance(j, list) else j.get('records') or j.get('rows') or []
        return [{str(k): v for k, v in r.items()} for r in rows if isinstance(r, dict)]
    delim = '\t' if path.lower().endswith('.tsv') else ','
    with open(path, newline='', encoding='utf-8-sig', errors='replace') as f:
        return list(csv.DictReader(f, delimiter=delim))


def normalize(rows, src_file, aoi):
    if not rows:
        return []
    hmap = map_headers(list(rows[0].keys()))
    out = []
    for r in rows:
        g = lambda f: (r.get(hmap[f]) or '').strip() if f in hmap else ''
        legal = g('legal')
        dt, mining_ctx = classify_doc(g('doc_type'), f"{legal} {g('claim_name')}")
        claim_name = g('claim_name') or extract_claim_name(legal)
        out.append({
            'instrument': g('instrument') or None,
            'type': dt, 'mining': mining_ctx,
            'type_raw': g('doc_type') or None,
            'recorded': parse_date(g('recorded')),
            'grantor': g('grantor') or None, 'grantee': g('grantee') or None,
            'claim_name': claim_name,
            'legal': legal or None,
            'book': g('book') or None, 'page': g('page') or None,
            'secs': trs_sections(f'{legal} {g("claim_name")}', aoi),
            'src_file': os.path.basename(src_file),
        })
    return out


def request_template(aoi, cfg, twn_span):
    rec = cfg['recorder']
    since = f'{date.today().year - 2}-01-01'
    return (
f"""To: {rec.get('email', rec.get('url', ''))}
Subject: Records request — recorded mining instruments, {cfg['county']} County

Hello,

I'd like to request an index listing (and copies where inexpensive) of recorded
mining instruments in {cfg['county']} County from {since} to present:

  - Notices of Location / certificates of location (lode, placer, millsite)
  - Amended location notices
  - Affidavits of assessment work / proof of labor / intent to hold
  - Quitclaims and deeds referencing mining claims

Area of interest: {twn_span} ({aoi.get('meridian_name', 'Boise Meridian')}).
If you keep a separate mining index book, that index is exactly what I'm after.
A CSV/spreadsheet export is ideal if your system allows it; otherwise a copy of
the index pages is fine. Happy to pay copy fees — please let me know the amount.

Thank you,
""")


# ---------------------------------------------------------------- main
def run(aoi_key=None, demo=False):
    aoi = load_aoi(aoi_key)
    k = aoi['key']
    ckey = (aoi.get('county') or '').lower().replace(' ', '_') or k
    cfg = PORTALS['counties'].get(ckey) or PORTALS['counties'].get(k) or {
        'county': aoi.get('county'), 'state': aoi['state'],
        'access': 'unverified', 'recorder': aoi.get('recorder', {})}

    claims_j = json.load(open(os.path.join(SITE, f'data/openground/{k}_claims.json')))
    claims = []
    for c in claims_j['active']:
        c = dict(c); c['_active'] = True; claims.append(c)
    for c in claims_j['closed']:
        c = dict(c); c['_active'] = False; claims.append(c)
    by_sec = {}
    for c in claims:
        for s in c.get('secs') or []:
            by_sec.setdefault(s, []).append(c)
    tok_idx = build_token_index(claims)
    print(f'MLRS pool: {len(claims_j["active"])} active + {len(claims_j["closed"])} closed')

    # township span for the request template (from the PLSS grid we already have)
    try:
        plss = json.load(open(os.path.join(SITE, f'data/plss/{k}.json')))
        ts = sorted({(f['properties']['t'], f['properties']['td']) for f in plss['features']})
        rs = sorted({(f['properties']['r'], f['properties']['rd']) for f in plss['features']})
        twn_span = (f'T{ts[0][0]}{ts[0][1]}–T{ts[-1][0]}{ts[-1][1]}, '
                    f'R{rs[0][0]}{rs[0][1]}–R{rs[-1][0]}{rs[-1][1]}')
    except Exception:
        twn_span = 'the full county'

    # ---- ingest operator exports ----
    files = []
    d = os.path.join(INBOX, ckey)
    if os.path.isdir(d):
        files = [os.path.join(d, f) for f in sorted(os.listdir(d))
                 if f.lower().endswith(('.csv', '.tsv', '.json')) and not f.startswith('.')]
    if demo:
        demo_f = os.path.join(ROOT, 'demo', 'county_sample.csv')
        if os.path.exists(demo_f):
            files.append(demo_f)
    records = []
    for f in files:
        try:
            rows = normalize(read_rows(f), f, aoi)
            print(f'ingested {os.path.basename(f)}: {len(rows)} rows')
            records.extend(rows)
        except Exception as e:                       # noqa: BLE001 — keep going per file
            print(f'SKIP {f}: {e}')

    # de-dupe on (instrument, recorded) where instrument exists
    seen, uniq = set(), []
    for r in records:
        key = (r['instrument'], r['recorded']) if r['instrument'] else id(r)
        if key in seen:
            continue
        seen.add(key); uniq.append(r)
    records = uniq

    # ---- match to MLRS ----
    matched = {'HIGH': 0, 'MEDIUM': 0, 'LOW': 0}
    matches_by_serial = {}
    alerts = []
    lookback_days = int(os.environ.get('COUNTY_LOOKBACK_DAYS', '400'))
    today = date.today()
    for i, r in enumerate(records):
        m, cands = match_record(r, claims, by_sec, tok_idx)
        r['match'], r['cands'] = m, cands
        if m:
            matched[m['conf']] += 1
            matches_by_serial.setdefault(m['ser'], []).append(i)
        recent = False
        if r['recorded']:
            try:
                y, mo, dd = map(int, r['recorded'].split('-'))
                recent = (today - date(y, mo, dd)).days <= lookback_days
            except ValueError:
                pass
        if r['type'] in NEW_LOCATION_TYPES and not m and recent:
            alerts.append({
                'kind': 'COUNTY-RECORDED — NOT IN MLRS',
                'name': r['claim_name'] or '(unnamed location)',
                'ser': None, 'instrument': r['instrument'], 'recorded': r['recorded'],
                'trs': r['secs'][:3],
                'evidence': (f"{r['type']} recorded at the county "
                             f"({r['recorded'] or 'undated'}, inst. {r['instrument'] or '?'}) "
                             f"with no matching MLRS case in the AOI snapshot. FLPMA gives the "
                             f"locator 90 days to file with BLM, then adjudication lag — this is "
                             f"the earliest public signal a claim exists. Verify at the recorder "
                             f"and re-check MLRS."),
            })
        if r['type'] == 'AFFIDAVIT OF ASSESSMENT' and m and recent:
            alerts.append({
                'kind': 'ASSESSMENT FILED (COUNTY)',
                'name': r['claim_name'] or m['name'], 'ser': m['ser'],
                'instrument': r['instrument'], 'recorded': r['recorded'],
                'trs': r['secs'][:3],
                'evidence': (f"Assessment-work/labor affidavit recorded "
                             f"{r['recorded'] or '(undated)'} matched to {m['ser']} "
                             f"({m['conf']} confidence) — the claimant is actively maintaining "
                             f"this claim."),
            })

    by_type = {}
    for r in records:
        by_type[r['type']] = by_type.get(r['type'], 0) + 1

    out = {
        'aoi': k, 'county': cfg.get('county'), 'state': cfg.get('state'),
        'generated': TODAY, 'demo': bool(demo),
        'law_note': PORTALS.get('law_note'),
        'coverage': {kk: cfg.get(kk) for kk in
                     ('access', 'verified', 'verified_date', 'verify_method', 'vendor',
                      'notes', 'recorder', 'adapter')},
        'request_template': request_template(aoi, cfg, twn_span),
        'inbox': f'data-inbox/county/{ckey}/  (CSV/TSV/JSON; headers sniffed — see README there)',
        'stats': {'files': len(files), 'records': len(records), 'by_type': by_type,
                  'matched': matched,
                  'unmatched_new_locations': sum(1 for a in alerts
                                                 if a['kind'].startswith('COUNTY-RECORDED'))},
        'records': records,
        'matches_by_serial': matches_by_serial,
        'alerts': alerts,
        'disclaimer': ('County index data is operator-supplied (no scraping of walled or '
                       'robots-disallowed portals). Name+TRS matching is fuzzy — treat every '
                       'match and every alert as a lead to verify against the recorder\'s '
                       'official record and the MLRS serial register, not a conclusion.'),
    }
    write_json(f'data/county/{k}.json', out)
    update_manifest('county', {'file': f'data/county/{k}.json', 'records': len(records),
                               'demo': bool(demo), 'retrieved': TODAY})
    write_coverage_md()
    print(json.dumps(out['stats'], indent=1))
    return out


def write_coverage_md():
    rows = []
    for key, c in PORTALS['counties'].items():
        rec = c.get('recorder') or {}
        rows.append(f"| {c['county']}, {c['state']} | {'AOI' if c.get('in_aoi') else ''} "
                    f"| **{c['access']}** | {c.get('vendor') or '—'} "
                    f"| {'✔ ' + str(c.get('verified_date', '')) if c.get('verified') else 'unverified'} "
                    f"| {rec.get('url') or '—'} |")
    md = (
        '# WS5 — county recorder coverage matrix\n\n'
        f'_Generated {TODAY} by `pipelines/county_records.py`. '
        'Access levels: scrape / bulk-export / operator-export / manual-request / '
        'unavailable / unverified (= browser check needed; several portals block '
        'automated verification via robots.txt, which we respect)._\n\n'
        f"{PORTALS.get('law_note')}\n\n"
        '| County | | Access | Vendor | Verified | Recorder |\n|---|---|---|---|---|---|\n'
        + '\n'.join(rows) + '\n\n'
        '## Cassia workflow (operator-assisted)\n\n'
        '1. `python3 pipelines/county_records.py` writes the prefilled records request into '
        '`site/data/county/cassia.json` (also shown in the map UI under COUNTY RECORDS).\n'
        '2. Email it to recorder@cassia.gov (or visit the vault — call ahead, (208) 878-5240).\n'
        '3. Drop whatever you get back — CSV/TSV/JSON export, or a spreadsheet you type up '
        'from the index books — into `data-inbox/county/cassia/`.\n'
        '4. Re-run the script: instruments are classified, TRS-parsed, fuzzy-matched to MLRS '
        'serials, attached to dossiers, and unmatched new locations surface in WATCH as '
        '**COUNTY-RECORDED — NOT IN MLRS**.\n\n'
        '`--demo` ingests `demo/county_sample.csv` (synthetic) to see the full flow end-to-end.\n')
    p = os.path.join(ROOT, 'COUNTY-COVERAGE.md')
    open(p, 'w').write(md)
    print(f'wrote COUNTY-COVERAGE.md')


if __name__ == '__main__':
    run(demo='--demo' in sys.argv)
