#!/usr/bin/env python3
"""Refresh private closed-claim inventory metadata after a legacy pull."""
from __future__ import annotations

from build_inputs import (BUILD_INPUTS, MANIFEST, load_artifact, load_manifest,
                          write_manifest)


manifest = load_manifest()
for state in ('nv', 'ut'):
    key = f'{state}_closed'
    if key not in manifest['claims']:
        print(f'{key}.json missing — skipped')
        continue
    data = load_artifact('claims', key, manifest=manifest)
    entry = manifest['claims'][key]
    entry['n'] = data['n']
    entry['retrieved'] = data.get('retrieved')
    for field in ('truncated', 'total_available', 'partial_after_spatial_clip',
                  'partial_note', 'spatial_clip'):
        if field in data:
            entry[field] = data[field]
        else:
            entry.pop(field, None)
    print(f'{key}: n={data["n"]:,} of {data.get("total_available", data["n"]):,}')
manifest['totals']['claims_closed'] = sum(
    entry['n'] for key, entry in manifest['claims'].items()
    if key.endswith('_closed'))
write_manifest(manifest, MANIFEST, root=BUILD_INPUTS)
print('private claims_closed total:', f'{manifest["totals"]["claims_closed"]:,}')
