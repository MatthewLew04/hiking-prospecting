#!/usr/bin/env python3
"""Build one state's MRDS site inventory from the USGS bulk dump.

WHY THIS EXISTS. build-inputs/data/sites/ is the list of mines the rest of
this repository can address by id, and Arizona was not in it. Every other
consumer treats that directory as the enumeration of addressable sites:
pipelines/ws13_mine_id_map.py builds the front-end -> corpus identifier bridge
by walking it, and an id it never enumerates cannot be bridged. So the 13,013
Arizona documents in the WS13 corpus -- every one of them licensed CC BY-NC-SA
from the Arizona Geological Survey, the whole of that admission class -- were
reachable from no mine at all. Not badly matched: not looked at.

The browser map was never the problem. site/data/tiles/national/mrds.pmtiles
is the 49-state baseline and it has drawn Arizona's points since it was built;
they were addressable on screen and unaddressable in the identifier namespace,
which is exactly the failure mode that reads to a user as "this mine has no
documents".

WHAT IT WRITES. The same columnar shape the sibling files carry, so nothing
downstream needs a special case: id/nm/st/g/c/x/y row-aligned, src 'mrds',
plus one addition and one omission, both deliberate.

  county   ADDED. The bulk dump carries it for every Arizona record, and the
           AZGS collection names the WS13 corpus stores are document titles
           rather than mine names -- 'Cuprite Mine Area Total Magnetic
           Intensity Record', not 'Cuprite Mine'. Matching those by name needs
           a second key or it matches a Yavapai collection to a Pima mine, and
           county is the one both sides carry. A row may name two counties
           ('Pima, Santa Cruz'); the string is stored as MRDS wrote it and the
           reader splits it.
  d        OMITTED. mrds_nv.json has a district column and this does not,
           because mrds-csv.zip has no district field. Nevada's came from the
           WFS service, which exposes one. Writing an all-empty column here
           would read as "Arizona has no districts" rather than "this source
           does not record them".

The dump is the same 2022 file the sibling snapshots were cut from -- the
server still reports Last-Modified 2022-08-23 -- and its sha256 is pinned
below so a silent republish is a hard failure rather than a quiet change of
the inventory under a mapping that was checked against the old one.

    pipelines/fetch_mrds_state.py AZ
    pipelines/fetch_mrds_state.py AZ --dry-run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import sys
import zipfile

from build_national_mrds_pmtiles import (STATE_NAMES, commodity_group,
                                         status_code)
from common import cached_get, write_build_input
from state_registry import ALL_STATES

SOURCE = 'https://mrdata.usgs.gov/mrds/mrds-csv.zip'
# The 2022 dump the sibling snapshots were cut from. Pinned, not advisory:
# ws13_mine_id_map.py derives a name-and-county crosswalk from this file, and
# a republished dump that renamed or renumbered records would move mappings
# under a bridge nobody re-checked.
SOURCE_SHA256 = ('31be4baaa86b082787bc74989146183db21badf7157db39b3f0a6fe0b38a'
                 '5477')
MEMBER = 'mrds.csv'
RETRIEVED = 'USGS dump 2022 (legacy)'
# The sibling files join the three commodity fields this way; the national
# PMTiles builder joins them with ', ' instead. Matching the neighbours here
# matters more than matching the tile builder: these columns are read
# side-by-side with mrds_nv.json and mrds_id.json, never with the tiles.
COMMODITY_JOIN = ' · '


def rows_for(state: str, payload: bytes):
    """Every MRDS record in one state, in file order.

    File order, not sorted: the sibling snapshots preserve the dump's order and
    a rebuild that reordered them would rewrite the whole artifact for no
    change in content.
    """
    long_names = {long for long, short in STATE_NAMES.items()
                  if short == state}
    if not long_names:
        raise SystemExit(f'{state} is not a state MRDS names')
    archive = zipfile.ZipFile(io.BytesIO(payload))
    with archive.open(MEMBER) as handle:
        reader = csv.DictReader(io.TextIOWrapper(
            handle, encoding='utf-8-sig', errors='replace', newline=''))
        for row in reader:
            if (row.get('country') or '').strip() != 'United States':
                continue
            if (row.get('state') or '').strip() not in long_names:
                continue
            try:
                latitude = float(row['latitude'])
                longitude = float(row['longitude'])
            except (KeyError, TypeError, ValueError):
                continue
            # The manifest validator rejects a site file with a row outside
            # these bounds, so a bad coordinate is dropped here rather than
            # failing the whole artifact at validation time.
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            identifier = (row.get('dep_id') or '').strip()
            if not identifier:
                continue
            commodities = COMMODITY_JOIN.join(
                value.strip() for value in (row.get('commod1') or '',
                                            row.get('commod2') or '',
                                            row.get('commod3') or '')
                if value.strip())
            yield {
                'id': identifier,
                # nm must be a nonempty string in every row or the manifest
                # validator fails the artifact; MRDS leaves it blank on
                # occurrence records and the sibling files spell that gap
                # '(unnamed)'.
                'nm': (row.get('site_name') or '').strip() or '(unnamed)',
                'st': status_code(row.get('dev_stat')),
                'g': commodity_group(commodities),
                'c': commodities,
                'county': (row.get('county') or '').strip(),
                'x': longitude,
                'y': latitude,
            }


def columnar(state: str, rows) -> dict:
    payload = {'src': 'mrds', 'state': state, 'retrieved': RETRIEVED, 'n': 0,
               'id': [], 'nm': [], 'st': [], 'g': [], 'c': [], 'county': [],
               'x': [], 'y': []}
    seen: set[str] = set()
    for row in rows:
        # dep_id is the front-end's addressing key -- ws12MinesNear() in
        # site/index.html reads it straight off the PMTiles feature -- so a
        # duplicate would give two mines one address.
        if row['id'] in seen:
            continue
        seen.add(row['id'])
        for column in ('id', 'nm', 'st', 'g', 'c', 'county', 'x', 'y'):
            payload[column].append(row[column])
    payload['n'] = len(payload['id'])
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('state', help='two-letter state code, e.g. AZ')
    parser.add_argument('--dry-run', action='store_true',
                        help='report the counts and write nothing')
    parser.add_argument('--allow-source-drift', action='store_true',
                        help='accept a dump whose sha256 is not the pinned '
                             'one; say so in the commit that does it')
    args = parser.parse_args(argv)
    state = args.state.strip().upper()
    if state not in ALL_STATES:
        raise SystemExit(f'{state} is not in the state registry')

    payload = cached_get(SOURCE, ttl_days=3650, binary=True)
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SOURCE_SHA256:
        message = (f'{SOURCE} is sha256 {digest}, not the pinned '
                   f'{SOURCE_SHA256}')
        if not args.allow_source_drift:
            raise SystemExit(message + ' -- refusing')
        print('WARNING: ' + message, file=sys.stderr)

    columns = columnar(state, rows_for(state, payload))
    named = sum(1 for value in columns['nm'] if value != '(unnamed)')
    with_county = sum(1 for value in columns['county'] if value)
    print(f'{state}: {columns["n"]:,} records, {named:,} named, '
          f'{with_county:,} with a county')
    if args.dry_run:
        return 0
    write_build_input('sites', f'mrds_{state.lower()}', columns)
    return 0


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
