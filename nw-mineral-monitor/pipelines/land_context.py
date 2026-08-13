#!/usr/bin/env python3
"""Normalize target-level surface/mineral ownership context for WS11.

Surface-management polygons are evidence about the surface manager only.
They never prove mineral title. The generic normalizer therefore defaults
mineral ownership to `unknown` until a registry adapter supplies a cited
mineral-title/lease record.
"""
from __future__ import annotations

import json


def approach_for(surface_class, mineral_class, trust_land):
    surface = (surface_class or 'unknown').lower()
    mineral = (mineral_class or 'unknown').lower()
    if mineral in ('state', 'state_trust'):
        if trust_land.get('mineral_leasing_offered') is True:
            return {'kind': 'state_lease', 'agency': trust_land.get('agency'),
                    'url': trust_land.get('portal_url'),
                    'note': 'Confirm tract availability and lease terms with the state.'}
        return {'kind': 'research_only', 'agency': trust_land.get('agency'),
                'url': trust_land.get('portal_url'),
                'note': 'State mineral ownership indicated, but no reviewed leasing path is configured.'}
    if mineral == 'private' or surface == 'private':
        return {'kind': 'private_negotiation', 'agency': None, 'url': None,
                'note': 'Identify the mineral owner; surface ownership alone is insufficient.'}
    if mineral == 'federal_locatable':
        return {'kind': 'federal_claim_research', 'agency': 'BLM', 'url': None,
                'note': 'Claim-state workflow only; verify withdrawals, title, and existing claims.'}
    return {'kind': 'research_only', 'agency': None, 'url': None,
            'note': 'Mineral ownership is unresolved; do not infer it from the surface manager.'}


def normalize_land_context(surface, registry, mineral=None):
    """Return the per-target LAND CONTEXT card payload."""
    surface = surface or {}
    mineral = mineral or {}
    surface_class = surface.get('class') or 'unknown'
    mineral_class = mineral.get('class') or 'unknown'
    trust = registry.get('trust_land') or {}
    return {
        'surface_ownership': {
            'class': surface_class,
            'manager': surface.get('manager'),
            'source': surface.get('source'),
            'scale': surface.get('scale'),
            'as_of': surface.get('as_of'),
        },
        'mineral_ownership': {
            'class': mineral_class,
            'confidence': mineral.get('confidence') or 'unknown',
            'source': mineral.get('source'),
            'note': mineral.get('note') or
                    'Surface-management mapping does not establish mineral ownership.',
        },
        'approach': approach_for(surface_class, mineral_class, trust),
        'regime': registry['regime'],
        'open_ground': ('not_applicable' if registry['regime'] == 'non_claim'
                        else 'requires_claim_and_land_status_analysis'),
    }


if __name__ == '__main__':
    import argparse
    from state_registry import load_state
    ap = argparse.ArgumentParser()
    ap.add_argument('state')
    ap.add_argument('--surface-class', default='unknown')
    ap.add_argument('--mineral-class', default='unknown')
    args = ap.parse_args()
    print(json.dumps(normalize_land_context(
        {'class': args.surface_class}, load_state(args.state),
        {'class': args.mineral_class}), indent=2))
