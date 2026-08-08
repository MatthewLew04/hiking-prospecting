#!/usr/bin/env python3
"""ID cited grades (WS9) -> merged into site/data/grades/grades.json.

Round 2 of the grade hunt for Idaho: the WS9 source queue, curated the same
way as every earlier round — verbatim grade statements, page-cited into
cached page-indexed PDFs, era-correct conversions, bonanza caps — but now
multi-commodity (Ag oz/t, Pb/Zn/Cu %, Sb %, WO3 units, Hg flasks, placer
$/yd3) and merged into existing rows by mine+county key: a mine already in
the dataset is ENRICHED (extra quote, gap-fill, possible primary upgrade),
never duplicated.

Sources this pipeline owns (src_key -> citation below): IBMG B-11 Silver
City (Piper & Laney 1926), USGS B 528 Lemhi (Umpleby 1913), USGS PP 97
Mackay (Umpleby 1917), USGS B 877 Bayhorse (Ross 1937), USGS B 969-F
Stibnite (Cooper 1951), Idaho Mine Inspector annual reports 1915/1917/1918
(deeper cuts beyond the round-1 extraction), IBMG B-14 Eastern Cassia
(Anderson 1931 — Black Pine completion sweep), IGS Pamphlets 49 (Atlanta),
61 (Blackbird cobalt), 72 (Snake River fine gold — Cassia placer
concentrates), 26 (Rocky Bar, when cached), and Liberty Gold's Black Pine
2026 MRE news release (modern g/t leg; the SEDAR technical report itself is
not fetchable from this sandbox — noted in coverage_ws9).

Also backfills the county column for the round-0/1 ID library rows from
grades-research/raw_id_or.json (the counties were curated there but the
original build dropped them), so mine+county keys and the Cassia acceptance
count work.

Idempotent: rows owned by 'id-r2' (and enrichments tagged 'id-r2') are
dropped and rebuilt each run; rebuild needs only pipelines/cache/pagetext/
(committed) or the cached/refetchable PDFs.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gradeslib as G

OWN = 'id-r2'
ROWS = os.path.join(HERE, '..', 'grades-research', 'rows_id_r2.json')
RAW01 = os.path.join(HERE, '..', 'grades-research', 'raw_id_or.json')

SOURCES = {
 'igs_b11': ('Piper, A.M. & Laney, F.B., 1926, Geology and Metalliferous '
             'Resources of the Region about Silver City, Idaho: IBMG '
             'Bulletin 11', 'https://www.idahogeology.org/product/b-11'),
 'b528': ('Umpleby, J.B., 1913, Geology and Ore Deposits of Lemhi County, '
          'Idaho: USGS Bulletin 528',
          'https://pubs.usgs.gov/bul/0528/report.pdf'),
 'pp97': ('Umpleby, J.B., 1917, Geology and Ore Deposits of the Mackay '
          'Region, Idaho: USGS Professional Paper 97',
          'https://pubs.usgs.gov/pp/0097/report.pdf'),
 'b877': ('Ross, C.P., 1937, Geology and Ore Deposits of the Bayhorse '
          'Region, Custer County, Idaho: USGS Bulletin 877',
          'https://pubs.usgs.gov/bul/0877/report.pdf'),
 'b969f': ('Cooper, J.R., 1951, Geology of the Tungsten, Antimony, and Gold '
           'Deposits near Stibnite, Idaho: USGS Bulletin 969-F',
           'https://pubs.usgs.gov/bul/0969f/report.pdf'),
 'ismir1915': ('Idaho Mine Inspector Annual Report 1915 (R.N. Bell)',
               'https://www.idahogeology.org/Uploads/Data/ISMIR/1915_ISMIR.pdf'),
 'ismir1917': ('Idaho Mine Inspector Annual Report 1917 (R.N. Bell)',
               'https://www.idahogeology.org/Uploads/Data/ISMIR/1917_ISMIR.pdf'),
 'ismir1918': ('Idaho Mine Inspector Annual Report 1918 (R.N. Bell)',
               'https://www.idahogeology.org/Uploads/Data/ISMIR/1918_ISMIR.pdf'),
 'igs_b14': ('Anderson, A.L., 1931, Geology and Mineral Resources of Eastern '
             'Cassia County, Idaho: IBMG Bulletin 14',
             'https://www.idahogeology.org/product/b-14'),
 'igs_p49': ('Anderson, A.L., 1939, Geology and Ore Deposits of the Atlanta '
             'District, Elmore County, Idaho: IBMG Pamphlet 49',
             'https://www.idahogeology.org/pub/Pamphlets/P-49.pdf'),
 'igs_p61': ('Anderson, A.L., 1943, A Preliminary Report on the Cobalt '
             'Deposits in the Blackbird District, Lemhi County, Idaho: IBMG '
             'Pamphlet 61', 'https://www.idahogeology.org/pub/Pamphlets/P-61.pdf'),
 'igs_p72': ('Staley, W.W., 1945, Fine Gold of Snake River and Lower Salmon '
             'River, Idaho: IBMG Pamphlet 72',
             'https://www.idahogeology.org/pub/Pamphlets/P-72.pdf'),
 'igs_p26': ('Ballard, S.M., 1928, Geology and Ore Deposits of the Rocky Bar '
             'Quadrangle: IBMG Pamphlet 26',
             'https://www.idahogeology.org/pub/Pamphlets/P-26.pdf'),
 'pp610': ('Koschmann, A.H. & Bergendahl, M.H., 1968, Principal '
           'Gold-Producing Districts of the United States: USGS Professional '
           'Paper 610', 'https://pubs.usgs.gov/pp/0610/report.pdf'),
}
# PDF page-index caveat: cite pdf pagination for typescript pamphlets whose
# printed folios are unreliable in scan/OCR
PDF_PAGED = {'igs_p72'}


def backfill_counties():
    """cnty for round-0/1 ID library rows, from raw_id_or.json (idempotent)."""
    raw = json.load(open(RAW01))
    bync = {}
    for x in raw:
        if x.get('county') and x['state'] == 'ID':
            bync.setdefault((G.canon(G.base_name(x['mine_name'])), 'ID'),
                            x['county'])
    g, p = G.load_grades()
    n = 0
    for i in range(g['n']):
        if g['st'][i] == 'ID' and g['cnty'][i] is None:
            c = bync.get((G.canon(G.base_name(g['name'][i])), 'ID'))
            if c:
                g['cnty'][i] = c
                n += 1
    if n:
        json.dump(g, open(p, 'w'), separators=(',', ':'))
    print(f'  county backfill (raw_id_or.json): {n} ID rows filled')


def main():
    backfill_counties()
    rows = G.curated(ROWS)
    for r in rows:                                   # citation strings
        if r.get('src_key'):
            cite, url = SOURCES[r['src_key']]
            if r.get('chapter'):
                cite = f"{r['chapter']}: {cite}"
            pg = f"pdf p. {r['page']}" if r['src_key'] in PDF_PAGED \
                else f"p. {r['page']}"
            r['src_cite'] = f'{cite}, {pg}'
            r['src_url'] = url
    ok, bad = G.validate_rows(rows, min_fuzzy=0.85, slack=3)
    if bad:
        for r in bad:
            print(f"  DROP quote-fail ({r['_vscore']}): {r['name']} "
                  f"[{r['src_key']} p.{r['page']}]")
        raise SystemExit(f'{len(bad)} rows failed verbatim-quote validation '
                         '— fix rows_id_r2.json before splicing')
    rows = [r for r in ok if G.normalize_row(r)]
    dropped = len(ok) - len(rows)
    if dropped:
        print(f'  {dropped} rows dropped by sanity/bonanza caps')
    G.locate_by_county(rows, 'ID')
    G.open_metres(rows, 'ID')
    added, enr = G.splice(rows, 'ID', OWN,
        'ID round-2 rows added 2026-08-08 (WS9): IBMG B-11/B-14/P-49/P-61/'
        'P-72, USGS B 528/877/969-F, PP 97, PP 610 districts, ISMIR '
        '1915-18 deeper cuts, Liberty Gold 2026 MRE; multi-commodity '
        'fields (pb/zn/cu/sb/wo3/hgf/yd3), placer flagged, Ag $-conversions '
        'by annual-average price table.')
    # Cassia acceptance check
    g, _ = G.load_grades()
    cassia = [i for i in range(g['n'])
              if g['st'][i] == 'ID' and (g['cnty'][i] or '') == 'Cassia']
    wq = [i for i in cassia if g['quote'][i]]
    print(f'  Cassia County rows: {len(cassia)} ({len(wq)} with quotes)')


if __name__ == '__main__':
    main()
