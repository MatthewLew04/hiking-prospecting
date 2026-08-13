#!/usr/bin/env python3
"""Reconcile public tiled metadata and private legacy build-input counts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

from build_inputs import (BUILD_INPUTS, MANIFEST as BUILD_INPUT_MANIFEST,
                          artifact_path, load_manifest as load_build_manifest,
                          write_manifest as write_build_manifest)
from state_registry import ALL_STATES, is_xyz_tile_template, load_states

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(SITE, 'data', 'manifest.json')

# These inventories bind the two already-published national point archives to
# exhaustive, decoded maximum-zoom scans of their stable MVT top-level IDs.
# The fingerprint guard matters: a future artifact must bring its own freshly
# computed inventory instead of inheriting these values by baseline name.
AUDITED_POINT_BASELINES = {
    'mrds': {
        'fingerprint': {
            'n': 265_702,
            'bytes': 31_678_500,
            'sha256': '3f33783f1553599d1580194eae76491858d858331b92881a132ec9f18d889e0e',
        },
        'source_id_inventory': {
            'status': 'complete_at_retrieval',
            'source_records': 265_702,
            'maxzoom_feature_instances': 286_999,
            'maxzoom_unique_tiled_ids': 265_702,
            'ids_sha256': '846e39f1c347cd3138d933fbe3d9bd2e4c26e9f2548a98c9ceac44cbb42367f7',
        },
    },
    'usmin': {
        'fingerprint': {
            'n': 570_484,
            'bytes': 61_775_696,
            'sha256': 'deb812811314ce825624801f3c118e350d3c8d4cadaf20d8d4bd4379d85d12f4',
        },
        'source_id_inventory': {
            'status': 'complete_at_retrieval',
            'source_records': 570_484,
            'maxzoom_feature_instances': 615_507,
            'maxzoom_unique_tiled_ids': 570_484,
            'ids_sha256': 'ad0ff4cf3383d6839e3bc2eaf5a07f2163f70038aede72536bad257549fe60fb',
        },
    },
}


def _materialize(path, code):
    if not isinstance(path, str):
        return ''
    return path.replace('{state}', code.lower()).replace('{STATE}', code)


def _slug(value):
    value = re.sub(r'[^a-z0-9]+', '-', str(value).lower()).strip('-')
    if not value:
        raise ValueError('release source-layer name cannot produce an empty id')
    return value


def _source_id(url):
    """One stable browser source id per archive, even when layers share it."""
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
    return f'ws11-pmtiles-{digest}'


def _source_layers(layer, label):
    source_layers = layer.get('source_layers')
    if (not isinstance(source_layers, list) or not source_layers or
            any(not isinstance(item, str) or not item for item in source_layers) or
            len(source_layers) != len(set(source_layers))):
        raise ValueError(f'{label} has no valid release source_layers contract')
    return source_layers


def _layer_metadata(layer, source_layer, source_layers):
    """Normalize optional registry count/availability data for one descriptor.

    ``layer_metadata`` is the canonical registry shape.  The count aliases are
    accepted as a migration convenience for existing builders, whose stamped
    summaries commonly call the same object ``counts`` or ``by_status``.
    A descriptor is intentionally silent when no reviewed count exists:
    missing is unknown and must never be turned into zero.
    """
    result = {}
    metadata = layer.get('layer_metadata')
    item = metadata.get(source_layer) if isinstance(metadata, dict) else None
    if isinstance(item, dict):
        for field in ('n', 'availability', 'complete'):
            if field in item:
                result[field] = item[field]

    if 'n' not in result:
        for field in ('layer_counts', 'counts', 'by_status', 'by_mode'):
            values = layer.get(field)
            value = values.get(source_layer) if isinstance(values, dict) else None
            if isinstance(value, dict):
                value = value.get('n')
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result['n'] = value
                break
    if ('n' not in result and len(source_layers) == 1 and
            isinstance(layer.get('n'), int) and not isinstance(layer.get('n'), bool) and
            layer['n'] >= 0):
        result['n'] = layer['n']

    if 'availability' not in result:
        for field in ('layer_availability', 'availability_by_layer'):
            values = layer.get(field)
            value = values.get(source_layer) if isinstance(values, dict) else None
            if value is not None:
                result['availability'] = value
                break
    return result


def _style(layer_id, map_type, state_filter):
    if map_type == 'line':
        return {'id': layer_id, 'type': 'line', 'filter': state_filter,
                'paint': {'line-color': '#d4a53f', 'line-width': 1}}
    if map_type == 'circle':
        return {'id': layer_id, 'type': 'circle', 'filter': state_filter,
                'paint': {'circle-color': '#2dd4bf', 'circle-radius': 3}}
    return {'id': layer_id, 'type': 'fill', 'filter': state_filter,
            'paint': {'fill-color': '#64748b', 'fill-opacity': 0.22}}


def _vector_bounds(code, state):
    """Return the registry's dateline-safe state envelopes for browser life-cycle use."""
    envelopes = state.get('query_envelopes')
    bounds = []
    if isinstance(envelopes, list):
        for envelope in envelopes:
            bbox = envelope.get('bbox') if isinstance(envelope, dict) else None
            valid = (isinstance(bbox, list) and len(bbox) == 4 and
                     all(isinstance(value, (int, float)) and
                         not isinstance(value, bool) for value in bbox) and
                     -180 <= bbox[0] < bbox[2] <= 180 and
                     -90 <= bbox[1] < bbox[3] <= 90)
            if valid:
                bounds.append(list(bbox))
    if not bounds:
        raise ValueError(f'{code} release has no valid query-envelope bounds')
    return bounds


def _vector_descriptors(code, state, kind, layer, map_type, *, system=None):
    if layer.get('browser_delivery') != 'pmtiles':
        return []
    url = _materialize(layer.get('artifact') or layer.get('browser_path'), code)
    if not url:
        raise ValueError(f'{code} {kind} release has no PMTiles URL')
    source_layers = _source_layers(layer, f'{code} {kind}')
    state_filter = ['==', ['get', 'st'], code]
    bounds_list = _vector_bounds(code, state)
    descriptors = []
    for source_layer in source_layers:
        layer_kind = (f'{kind}-{source_layer}' if system else kind)
        layer_id = '-'.join((_slug(code), _slug(kind), _slug(source_layer)))
        geometry = map_type(source_layer) if callable(map_type) else map_type
        descriptor = {
            'id': layer_id,
            'source_id': _source_id(url),
            'state': code,
            'regime': state['regime'],
            'kind': layer_kind,
            'delivery': 'pmtiles',
            'url': url,
            'source_layer': source_layer,
            # Query envelopes are deliberately a list: Alaska has two
            # antimeridian-safe footprints. The browser uses them to allocate
            # and tear down state archives without pretending one wrapped
            # rectangle is valid.
            'view_bounds': bounds_list,
            # Claim layers remain useful in the national overview. Heavier
            # state geology/context stacks replace the national overview only
            # after the user has zoomed toward a state.
            'activation_minzoom': 4,
            'filter': state_filter,
            'interactive': True,
            'style_layers': [_style(layer_id, geometry, state_filter)],
        }
        if system:
            descriptor['system'] = system
        descriptor.update(_layer_metadata(layer, source_layer, source_layers))
        descriptors.append(descriptor)
    return descriptors


def _claim_geometry(system_id, source_layer):
    if system_id == 'federal_mlrs':
        return 'fill' if source_layer == 'open_ground' else 'circle'
    # Alaska DNR source features are claim polygons, including closed and
    # pending records. Unknown future systems must declare polygons until a
    # reviewed registry geometry contract is added.
    return 'fill'


def _raster_descriptor(code, state):
    aeromag = state['aeromag']
    if aeromag.get('browser_delivery') != 'cog':
        raise ValueError(f'{code} released aeromag must use a COG artifact')
    tile_url = _materialize(aeromag.get('tile_url'), code)
    if not is_xyz_tile_template(tile_url):
        raise ValueError(f'{code} aeromag tile_url is not a valid XYZ template')
    cog_url = _materialize(aeromag.get('artifact') or
                           aeromag.get('browser_path'), code)
    sha256 = aeromag.get('sha256')
    size = aeromag.get('bytes')
    tile_size = aeromag.get('tile_size', 256)
    bounds = aeromag.get('bounds')
    minzoom, maxzoom = aeromag.get('minzoom'), aeromag.get('maxzoom')
    valid_bounds = (isinstance(bounds, list) and len(bounds) == 4 and
                    all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        for value in bounds) and
                    -180 <= bounds[0] < bounds[2] <= 180 and
                    -90 <= bounds[1] < bounds[3] <= 90)
    valid_zooms = (isinstance(minzoom, int) and not isinstance(minzoom, bool) and
                   isinstance(maxzoom, int) and not isinstance(maxzoom, bool) and
                   0 <= minzoom <= maxzoom <= 24)
    if (not re.search(r'\.tiff?$', cog_url or '', re.I) or
            not re.fullmatch(r'[0-9a-f]{64}', str(sha256 or '')) or
            not isinstance(size, int) or isinstance(size, bool) or size <= 127 or
            tile_size not in (256, 512) or not valid_bounds or not valid_zooms):
        raise ValueError(f'{code} aeromag lacks valid immutable COG metadata')
    layer_id = f'{code.lower()}-aeromag'
    descriptor = {
        'id': layer_id,
        'source_id': f'ws11-raster-{code.lower()}-aeromag',
        'state': code,
        'regime': state['regime'],
        'kind': 'aeromag',
        # The canonical data product is a COG; MapLibre consumes the explicit
        # XYZ template. Keeping both in one descriptor prevents a .tif URL
        # from being silently mistaken for a raster tile endpoint.
        'delivery': 'cog',
        'url': tile_url,
        'tile_url_template': tile_url,
        'tile_scheme': 'xyz',
        'tile_size': tile_size,
        'bounds': bounds,
        'minzoom': minzoom,
        'maxzoom': maxzoom,
        'cog': {'url': cog_url, 'sha256': sha256, 'bytes': size},
        'attribution': (aeromag.get('attribution') or
                        'state aeromagnetic COG'),
        'style_layers': [{
            'id': layer_id,
            'type': 'raster',
            'paint': {'raster-opacity': 0.65},
        }],
    }
    return descriptor


def _validate_compiled_descriptors(descriptors):
    """Fail closed on id collisions or ambiguous shared-source identities."""
    descriptor_ids = set()
    style_ids = set()
    source_urls = {}
    source_promote_ids = {}
    for descriptor in descriptors:
        descriptor_id = descriptor.get('id')
        if descriptor_id in descriptor_ids:
            raise ValueError(f'duplicate release descriptor id {descriptor_id!r}')
        descriptor_ids.add(descriptor_id)
        for style in descriptor.get('style_layers') or []:
            style_id = style.get('id') if isinstance(style, dict) else None
            if style_id in style_ids:
                raise ValueError(f'duplicate release style-layer id {style_id!r}')
            style_ids.add(style_id)
        if descriptor.get('delivery') in ('pmtiles', 'cog'):
            source_id = descriptor.get('source_id')
            url = descriptor.get('url')
            prior = source_urls.setdefault(source_id, url)
            if prior != url:
                raise ValueError(f'tiled source id {source_id!r} maps to multiple URLs')
            promote_id = descriptor.get('promote_id')
            if source_id in source_promote_ids and source_promote_ids[source_id] != promote_id:
                raise ValueError(
                    f'tiled source id {source_id!r} maps to multiple promote_id values')
            source_promote_ids[source_id] = promote_id
        if descriptor.get('delivery') == 'pmtiles':
            bounds_list = descriptor.get('view_bounds')
            valid_bounds = (
                isinstance(bounds_list, list) and bool(bounds_list) and
                all(isinstance(bounds, list) and len(bounds) == 4 and
                    all(isinstance(value, (int, float)) and
                        not isinstance(value, bool) for value in bounds) and
                    -180 <= bounds[0] < bounds[2] <= 180 and
                    -90 <= bounds[1] < bounds[3] <= 90
                    for bounds in bounds_list))
            minzoom = descriptor.get('activation_minzoom')
            if (not valid_bounds or not isinstance(minzoom, int) or
                    isinstance(minzoom, bool) or not 0 <= minzoom <= 24):
                raise ValueError(
                    f'{descriptor_id!r} lacks valid vector life-cycle bounds/zoom')
    return descriptors


def tiled_layers():
    """Compile browser descriptors from release-enabled state adapters only."""
    out = []
    for code, state in sorted(load_states().items()):
        if not (state['release']['enabled'] and state['release']['status'] == 'done'):
            continue
        out.extend(_vector_descriptors(
            code, state, 'geology', state['geology'], 'fill'))
        out.extend(_vector_descriptors(
            code, state, 'faults', state['faults'], 'line'))
        if state['regime'] == 'non_claim':
            out.extend(_vector_descriptors(
                code, state, 'land-context', state['land_context'],
                lambda source_layer: ('circle' if source_layer == 'target_context'
                                      else 'fill')))
            if state.get('aml', {}).get('release_inventory_status') == 'ingested_complete':
                out.extend(_vector_descriptors(
                    code, state, 'aml', state['aml'], 'circle'))
            if state.get('trust_land', {}).get('release_inventory_status') == 'ingested_complete':
                out.extend(_vector_descriptors(
                    code, state, 'trust-land', state['trust_land'], 'fill'))
        for system in state['claim_systems']:
            system_id = system['id']
            parts = system.get('publication_artifacts')
            publications = (parts.values() if isinstance(parts, dict) and parts
                            else [system])
            for publication in publications:
                out.extend(_vector_descriptors(
                    code, state, system_id, publication,
                    lambda source_layer, sid=system_id:
                    _claim_geometry(sid, source_layer), system=system_id))
        out.append(_raster_descriptor(code, state))
    return _validate_compiled_descriptors(out)


def reconcile_build_inputs(manifest):
    """Refresh private inventory counts from its canonical artifacts."""
    # CA was generated by the existing MLRS pipeline but was absent from the
    # original compatibility inventory.
    ca_path = artifact_path(
        'claims', 'ca_active', root=BUILD_INPUTS, require_file=True)
    with open(ca_path, encoding='utf-8') as ca_file:
        ca = json.load(ca_file)
    if ca.get('state') != 'CA' or ca.get('layer') != 'active':
        raise ValueError('ca_active.json identity fields are invalid')
    if ca.get('n') != len(ca.get('serial') or []):
        raise ValueError('ca_active.json n does not match its serial column')
    manifest.setdefault('claims', {})['ca_active'] = {
        'n': ca['n'], 'file': 'data/claims/ca_active.json',
        'retrieved': ca.get('retrieved'),
    }
    # Every count comes from the artifact itself. This catches migrations,
    # clipping repairs, and failed/partial updater outputs instead of trusting
    # stale manifest integers. Preserve explicit incompleteness in the public
    # manifest so a capped snapshot can never look silently complete.
    copied_flags = ('truncated', 'total_available', 'envelope_total_upper_bound',
                    'spatial_clip', 'partial_after_spatial_clip', 'partial_note')
    for section in ('sites', 'claims'):
        for key, entry in manifest.get(section, {}).items():
            path = artifact_path(section, key, entry, root=BUILD_INPUTS)
            with open(path, encoding='utf-8') as artifact_file:
                artifact = json.load(artifact_file)
            n = artifact.get('n')
            if not isinstance(n, int) or n < 0:
                raise ValueError(f'{entry["file"]}: invalid n={n!r}')
            entry['n'] = n
            if artifact.get('retrieved') is not None:
                entry['retrieved'] = artifact['retrieved']
            for flag in copied_flags:
                if flag in artifact:
                    entry[flag] = artifact[flag]
                else:
                    entry.pop(flag, None)
    manifest.setdefault('totals', {})['sites'] = sum(
        int(v.get('n') or 0) for v in manifest.get('sites', {}).values())
    for mode in ('active', 'closed'):
        manifest['totals'][f'claims_{mode}'] = sum(
            int(v.get('n') or 0) for key, v in manifest['claims'].items()
            if key.endswith('_' + mode))
    return manifest


def baseline_totals(manifest):
    """Exact public totals represented by browser-delivered PMTiles."""
    baselines = manifest.get('national_baselines')
    if not isinstance(baselines, dict):
        baselines = {}
    sites = sum(
        int((baselines.get(source) or {}).get('n') or 0)
        for source in ('mrds', 'usmin', 'stategeo', 'ardf'))
    claims = baselines.get('claims') or {}
    by_mode = claims.get('by_mode') if isinstance(claims, dict) else {}
    if not isinstance(by_mode, dict):
        by_mode = {}
    return {
        'sites': sites,
        'claims_active': int((by_mode.get('active') or {}).get('n') or 0),
        'claims_closed': int((by_mode.get('closed') or {}).get('n') or 0),
    }


def reconcile_geophys_baseline(manifest):
    """Bind the published survey-index archive to immutable manifest fields."""
    entry = manifest.setdefault('ws56', {}).get('geophys_surveys')
    if not isinstance(entry, dict):
        raise ValueError('manifest ws56.geophys_surveys entry is missing')
    relative = 'data/tiles/geophys/surveys.pmtiles'
    artifact = os.path.join(SITE, relative)
    if not os.path.isfile(artifact):
        raise ValueError('national survey-index PMTiles artifact is missing')
    digest = hashlib.sha256()
    with open(artifact, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    entry.update({
        'file': relative,
        'format': 'pmtiles',
        'source_layer': 'surveys',
        'required_properties': ['src', 'nm'],
        'bytes': os.path.getsize(artifact),
        'sha256': digest.hexdigest(),
    })
    return manifest


def reconcile_audited_point_baselines(manifest):
    """Stamp audited inventories only onto the exact archives that were scanned."""
    baselines = manifest.get('national_baselines')
    if not isinstance(baselines, dict):
        raise ValueError('manifest national_baselines entry is missing')
    for baseline_id, audited in AUDITED_POINT_BASELINES.items():
        entry = baselines.get(baseline_id)
        if not isinstance(entry, dict):
            raise ValueError(f'national {baseline_id} baseline entry is missing')
        fingerprint = audited['fingerprint']
        matches = all(entry.get(field) == value
                      for field, value in fingerprint.items())
        if matches:
            entry['source_id_inventory'] = dict(audited['source_id_inventory'])
        elif entry.get('source_id_inventory') == audited['source_id_inventory']:
            raise ValueError(
                f'national {baseline_id} has a stale audited source-ID inventory')
    return manifest


def reconcile(manifest):
    """Publish no legacy JSON addresses; retain exact tiled baseline totals."""
    manifest['region'] = sorted(ALL_STATES)
    manifest['sites'] = {}
    manifest['claims'] = {}
    manifest['totals'] = baseline_totals(manifest)
    manifest['tiled_layers'] = tiled_layers()
    reconcile_geophys_baseline(manifest)
    reconcile_audited_point_baselines(manifest)
    stategeo = (manifest.get('national_baselines') or {}).get('stategeo')
    if isinstance(stategeo, dict) and isinstance(stategeo.get('sources'), dict):
        for source in stategeo['sources'].values():
            if not isinstance(source, dict):
                continue
            legacy = source.pop('file', None)
            if 'build_input' not in source and isinstance(legacy, str):
                source['build_input'] = os.path.splitext(os.path.basename(legacy))[0]
    return manifest


def encoded(obj):
    return json.dumps(obj, separators=(',', ':')).encode()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args(argv)
    old = open(MANIFEST, 'rb').read()
    current = json.loads(old)
    old_inputs = open(BUILD_INPUT_MANIFEST, 'rb').read()
    inputs = load_build_manifest(BUILD_INPUT_MANIFEST, root=BUILD_INPUTS)
    expected_inputs_obj = reconcile_build_inputs(inputs)
    expected_inputs = encoded(expected_inputs_obj)
    expected = encoded(reconcile(current))
    if args.check:
        stale = []
        if old != expected:
            stale.append('site/data/manifest.json')
        if old_inputs != expected_inputs:
            stale.append('build-inputs/manifest.json')
        if stale:
            print('ERROR: stale manifest(s): ' + ', '.join(stale) +
                  '; run pipelines/reconcile_manifest.py', file=sys.stderr)
            return 1
        print('public tiled totals and private build-input counts are current')
        return 0
    write_build_manifest(
        expected_inputs_obj, BUILD_INPUT_MANIFEST, root=BUILD_INPUTS)
    with open(MANIFEST, 'wb') as fh:
        fh.write(expected)
    print('reconciled build-inputs/manifest.json and site/data/manifest.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
