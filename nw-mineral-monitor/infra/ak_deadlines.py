"""Pure Alaska state-claim deadline rules shared by watch jobs and tests.

Sources are the AK DNR annual-rent and annual-labor fact sheets referenced in
states/AK.yaml. Returned dates are alerting leads; DNR source records control.
"""
from __future__ import annotations

import calendar
import datetime as dt

RENT_SOURCE = 'https://dnr.alaska.gov/mlw/cdn/pdf/factsheets/annual-rent.pdf'
LABOR_SOURCE = 'https://dnr.alaska.gov/mlw/cdn/pdf/factsheets/annual-labor.pdf'


def _add_days(value, days):
    return value + dt.timedelta(days=days)


def state_claim_deadlines(posting_date, assessment_year, conveyed_date=None):
    """Return independent AK rent/labor clocks for one state claim.

    Dates use ISO strings. The September 1 legal moment is noon Alaska time;
    callers must retain the `at_noon_alaska` flag rather than treating it as
    midnight UTC.
    """
    if isinstance(posting_date, str):
        posting_date = dt.date.fromisoformat(posting_date)
    if isinstance(conveyed_date, str):
        conveyed_date = dt.date.fromisoformat(conveyed_date)
    sep1 = dt.date(int(assessment_year), 9, 1)
    nov30 = dt.date(int(assessment_year), 11, 30)
    first_rent_due = _add_days(posting_date, 45)
    if conveyed_date:
        first_rent_due = _add_days(conveyed_date, 90)
    return {
        'system_id': 'alaska_state_claims',
        'rent': {
            'initial_due': first_rent_due.isoformat(),
            'subsequent_due': sep1.isoformat(),
            'received_grace_ends': nov30.isoformat(),
            'abandonment_if_unpaid': dt.date(int(assessment_year), 12, 1).isoformat(),
            'rate_schedule': 'effective_dated_external',
            'source': RENT_SOURCE,
        },
        'labor': {
            'work_complete_due': sep1.isoformat(),
            'cash_in_lieu_due': sep1.isoformat(),
            'statement_recording_due': nov30.isoformat(),
            'at_noon_alaska': True,
            'source': LABOR_SOURCE,
        },
        'disclaimer': ('Computed alert dates are not authoritative claim status; '
                       'verify the DNR land-administration record and recording district.'),
    }


def rent_amount(claim_age_years, acres, schedule):
    """Resolve an effective-dated registry schedule; never hard-code stale rent."""
    tier = 'years_1_5' if claim_age_years <= 5 else \
           'years_6_10' if claim_age_years <= 10 else 'years_11_plus'
    size = 'traditional_40_acre' if acres <= 40 else 'quarter_section_160_acre'
    try:
        return schedule[tier][size]
    except (KeyError, TypeError) as exc:
        raise ValueError('missing effective AK rent schedule for claim age/size') from exc
