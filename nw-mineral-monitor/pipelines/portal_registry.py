#!/usr/bin/env python3
"""Strict loader for the WS12 mine-file portal registry.

Portal files use JSON-compatible YAML for the same reason as the WS11 state
registry: validation and crawl planning must work in the stdlib-only runtime.
An entry that was probed but cannot be harvested is data, not an omission.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from urllib.parse import urlsplit


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PORTALS_DIR = os.path.join(ROOT, 'portals')

STATUS_VALUES = frozenset((
    'harvest_ready',
    'index_only_no_public_documents',
    'manual_request_only',
    'probed_no_attachment_pattern',
    'probed_empty',
    'blocked_by_access_controls',
    'registered_publication_catalog',
))
ACCESS_MODES = frozenset((
    'public_download', 'index_only', 'manual_request', 'review_required',
))
ADAPTERS = frozenset((
    'igs_legacy_and_current', 'azgs_metadata', 'nbmg_embedded_js',
    'html_catalog', 'arcgis_attachments',
))

# These are the named WS12 portals, the remaining WS11 phase-2 survey probes,
# and the explicitly requested P3/PHUMMIS probes.  A validator failure is
# preferable to silently dropping a difficult portal.
REQUIRED_PORTALS = {
    'igs_mines': 'ID',
    'azgs_admmr': 'AZ',
    'nbmg_mining_district_files': 'NV',
    'mbmg_data_center': 'MT',
    'nmbgmr_mines': 'NM',
    'ugs_geodata_archive': 'UT',
    'usgs_ardf': 'AK',
    'ak_dggs_publications': 'AK',
    'cgs_publications': 'CO',
    'co_drms_permits': 'CO',
    'ca_mines_online': 'CA',
    'cgs_information_warehouse': 'CA',
    'wa_geology_mines': 'WA',
    'dogami_milo': 'OR',
    'wsgs_mines': 'WY',
    'sdgs_publications': 'SD',
    'ar_survey_publications': 'AR',
    'fl_survey_publications': 'FL',
    'la_survey_publications': 'LA',
    'ms_survey_publications': 'MS',
    'nd_survey_publications': 'ND',
    'ne_survey_publications': 'NE',
    'mi_survey_publications': 'MI',
    'mo_mine_map_repository': 'MO',
    'pa_phummis': 'PA',
    'va_geology_collections': 'VA',
    'nc_survey_publications': 'NC',
    'sc_survey_publications': 'SC',
    'ga_survey_publications': 'GA',
    'osmre_nmmr': 'federal',
    'usgs_pubs_warehouse': 'federal',
    'ngmdb': 'federal',
    'msha_mdrs': 'federal',
}

PORTAL_ID_RE = re.compile(r'^[a-z][a-z0-9_]{2,63}$')
STATE_RE = re.compile(r'^[A-Z]{2}$')


class PortalRegistryError(ValueError):
    """A portal registry file violates the WS12 contract."""


def _reject_json_constant(value):
    raise ValueError(f'non-standard JSON number {value}')


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON object key {key!r}')
        result[key] = value
    return result


def _read(path):
    try:
        with open(path, encoding='utf-8') as source:
            value = json.load(
                source, parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PortalRegistryError(
            f'{path}: portal YAML must stay JSON-compatible: {exc}') from exc
    if not isinstance(value, dict):
        raise PortalRegistryError(f'{path}: top level must be an object')
    return value


def _text(value, minimum=1):
    return isinstance(value, str) and len(value.strip()) >= minimum


def _https(value):
    if not _text(value):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == 'https' and bool(parsed.netloc) and not parsed.username


def validate_portal(row, path='<portal>'):
    errors = []
    portal_id = row.get('id')
    if not isinstance(portal_id, str) or not PORTAL_ID_RE.fullmatch(portal_id):
        errors.append(f'{path}: invalid id')
    jurisdiction = row.get('jurisdiction')
    if jurisdiction != 'federal' and not (
            isinstance(jurisdiction, str) and STATE_RE.fullmatch(jurisdiction)):
        errors.append(f'{path}: jurisdiction must be a state code or federal')
    if not _text(row.get('name'), 4):
        errors.append(f'{path}: missing name')
    if row.get('tier') not in (1, 2, 3, 'federal'):
        errors.append(f'{path}: invalid tier')
    if not _text(row.get('portal_type')):
        errors.append(f'{path}: missing portal_type')
    if not _https(row.get('entry_url')):
        errors.append(f'{path}: entry_url must be HTTPS')
    for explicit in ('detail_page_pattern', 'pdf_link_pattern', 'id_scheme'):
        if explicit not in row:
            errors.append(f'{path}: missing explicit {explicit}')
        elif explicit == 'id_scheme' and not _text(row.get(explicit), 5):
            errors.append(f'{path}: id_scheme must explain the identifier')
        elif explicit != 'id_scheme' and row.get(explicit) is not None and not _text(
                row.get(explicit)):
            errors.append(f'{path}: {explicit} must be text or null')
    status = row.get('status')
    if status not in STATUS_VALUES:
        errors.append(f'{path}: invalid status {status!r}')

    probe = row.get('probe')
    if not isinstance(probe, dict):
        errors.append(f'{path}: missing probe result')
    else:
        try:
            checked = dt.date.fromisoformat(probe.get('checked', ''))
            if checked > dt.date.today():
                errors.append(f'{path}: probe.checked is in the future')
        except (TypeError, ValueError):
            errors.append(f'{path}: probe.checked must be YYYY-MM-DD')
        if not _https(probe.get('verified_url')):
            errors.append(f'{path}: probe.verified_url must be HTTPS')
        http_status = probe.get('http_status')
        if http_status is not None and (
                not isinstance(http_status, int) or isinstance(http_status, bool) or
                not 100 <= http_status <= 599):
            errors.append(f'{path}: probe.http_status must be an HTTP status or null')
        if not _text(probe.get('result'), 20):
            errors.append(f'{path}: probe.result must record what was found')

    access = row.get('access')
    if not isinstance(access, dict):
        errors.append(f'{path}: missing access policy')
    else:
        if access.get('mode') not in ACCESS_MODES:
            errors.append(f'{path}: invalid access.mode')
        if access.get('robots') != 'respect':
            errors.append(f'{path}: access.robots must be respect')
        if access.get('paywalled') not in (True, False):
            errors.append(f'{path}: access.paywalled must be boolean')
        if access.get('official_public_documents_only') is not True:
            errors.append(
                f'{path}: official_public_documents_only must be true')
        if not _text(access.get('rights_note'), 15):
            errors.append(f'{path}: access.rights_note is required')
        if status == 'harvest_ready':
            if access.get('terms_status') != 'reviewed_no_automation_prohibition':
                errors.append(
                    f'{path}: executable portal terms must be reviewed fail-closed')
            try:
                terms_checked = dt.date.fromisoformat(access.get('terms_checked', ''))
                if terms_checked > dt.date.today():
                    errors.append(f'{path}: access.terms_checked is in the future')
            except (TypeError, ValueError):
                errors.append(f'{path}: access.terms_checked must be YYYY-MM-DD')
            if not _https(access.get('terms_url')):
                errors.append(f'{path}: executable portal needs HTTPS terms_url')
            if not _text(access.get('terms_note'), 25):
                errors.append(f'{path}: executable portal needs terms_note')

    crawler = row.get('crawler')
    if status == 'harvest_ready':
        if not isinstance(crawler, dict):
            errors.append(f'{path}: harvest_ready entry needs crawler')
        else:
            if crawler.get('adapter') not in ADAPTERS:
                errors.append(f'{path}: unsupported crawler adapter')
            interval = crawler.get('min_interval_seconds')
            if (not isinstance(interval, (int, float)) or
                    isinstance(interval, bool) or interval < 0.25):
                errors.append(
                    f'{path}: crawler min_interval_seconds must be >= 0.25')
            if crawler.get('automation_permitted') is not True:
                errors.append(
                    f'{path}: harvest_ready requires reviewed automation permission')
    elif crawler is not None:
        errors.append(
            f'{path}: non-ready portal must not carry executable crawler config')

    harvest_state = row.get('harvest_state')
    if not isinstance(harvest_state, dict):
        errors.append(f'{path}: missing explicit harvest_state')
    else:
        complete = harvest_state.get('full_crawl_complete')
        if complete not in (True, False):
            errors.append(f'{path}: full_crawl_complete must be boolean')
        if not _text(harvest_state.get('crawl_scope'), 8):
            errors.append(f'{path}: harvest_state.crawl_scope is required')
        if complete:
            if not _text(harvest_state.get('last_complete_cursor'), 8):
                errors.append(
                    f'{path}: completed crawl needs last_complete_cursor')
            digest = harvest_state.get('manifest_sha256')
            if not isinstance(digest, str) or not re.fullmatch(
                    r'[0-9a-f]{64}', digest):
                errors.append(
                    f'{path}: completed crawl needs manifest_sha256')
        elif (harvest_state.get('last_complete_cursor') is not None or
              harvest_state.get('manifest_sha256') is not None or
              harvest_state.get('completed_at') is not None):
            errors.append(
                f'{path}: incomplete crawl cannot claim cursor/manifest/completion')

    if errors:
        raise PortalRegistryError('\n'.join(errors))
    return row


def load_registry(portals_dir=PORTALS_DIR, validate=True):
    if not os.path.isdir(portals_dir):
        raise PortalRegistryError(f'missing portal registry directory {portals_dir}')
    portals = {}
    files = sorted(
        name for name in os.listdir(portals_dir)
        if name.endswith('.yaml') and not name.startswith('_'))
    for name in files:
        path = os.path.join(portals_dir, name)
        envelope = _read(path)
        if envelope.get('schema_version') != 1:
            raise PortalRegistryError(f'{path}: schema_version must equal 1')
        jurisdiction = envelope.get('jurisdiction')
        expected_filename = (
            'federal.yaml' if jurisdiction == 'federal'
            else f'{str(jurisdiction).lower()}.yaml')
        if name != expected_filename:
            raise PortalRegistryError(
                f'{path}: filename must be {expected_filename}')
        rows = envelope.get('portals')
        if not isinstance(rows, list) or not rows:
            raise PortalRegistryError(f'{path}: portals must be a nonempty array')
        for index, row in enumerate(rows):
            label = f'{path}:portals[{index}]'
            if not isinstance(row, dict):
                raise PortalRegistryError(f'{label}: must be an object')
            if row.get('jurisdiction') != jurisdiction:
                raise PortalRegistryError(
                    f'{label}: jurisdiction differs from file envelope')
            if validate:
                validate_portal(row, label)
            portal_id = row.get('id')
            if portal_id in portals:
                raise PortalRegistryError(f'duplicate portal id {portal_id!r}')
            portals[portal_id] = row
    return portals


def validate_registry(portals_dir=PORTALS_DIR):
    errors = []
    try:
        portals = load_registry(portals_dir, validate=True)
    except PortalRegistryError as exc:
        return {'ok': False, 'errors': str(exc).splitlines(), 'portals': 0}
    missing = sorted(set(REQUIRED_PORTALS) - set(portals))
    extra_mismatch = sorted(
        portal_id for portal_id, jurisdiction in REQUIRED_PORTALS.items()
        if portal_id in portals and portals[portal_id]['jurisdiction'] != jurisdiction)
    if missing:
        errors.append('missing required portal entries: ' + ', '.join(missing))
    if extra_mismatch:
        errors.append(
            'required portal jurisdiction mismatch: ' + ', '.join(extra_mismatch))
    return {'ok': not errors, 'errors': errors, 'portals': len(portals)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portals-dir', default=PORTALS_DIR)
    args = parser.parse_args(argv)
    result = validate_registry(args.portals_dir)
    if not result['ok']:
        for error in result['errors']:
            print(f'ERROR: {error}', file=sys.stderr)
        return 1
    print(f"portal registry valid: {result['portals']} entries")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
