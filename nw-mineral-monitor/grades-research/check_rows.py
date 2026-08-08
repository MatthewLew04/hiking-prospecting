#!/usr/bin/env python3
"""Validate a WS9 curated-rows JSON file: quote-on-page, schema, caps.
usage: python3 check_rows.py rows_x.json [--fix-pages]"""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'pipelines'))
import gradeslib as G

path = sys.argv[1]
rows = json.load(open(path))
ok, bad = G.validate_rows(rows, min_fuzzy=0.85, slack=3)
drop_caps = []
for r in ok[:]:
    if not G.normalize_row(r):
        ok.remove(r)
        drop_caps.append(r)
print(f'{os.path.basename(path)}: {len(rows)} rows -> {len(ok)} valid, '
      f'{len(bad)} quote-fail, {len(drop_caps)} cap-dropped')
for r in bad:
    print(f"  QUOTE-FAIL ({r.get('_vscore')}) {r['name']} "
          f"[{r['src_key']} p.{r['page']}] :: {r['quote'][:80]}...")
for r in drop_caps:
    print(f"  CAP-DROP {r['name']} au={r.get('au_opt')}")
if '--fix-pages' in sys.argv:
    json.dump(rows, open(path, 'w'), indent=1)
    print('pdf_page/_vscore annotations saved')
