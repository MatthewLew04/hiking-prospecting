"""NW Mineral Monitor — national claim expiration watch.

The default job diffs federal MLRS active-claim snapshots for the exact 19
registry claim states.  Statewide snapshots and resumable checkpoints stay in
the private ``watch/`` prefix; only small, state-labelled alert digests are
published below ``data/alerts/``.  A missing or incomplete pull is UNKNOWN,
never zero and never evidence that claims closed.

Alaska is intentionally dual-system.  Federal claims run through the same
national MLRS path as the other 18 states, while the independent AK DNR state
claim watcher keeps its own identifiers, snapshot, and rent/labor calendar.
Both systems are folded into the public national digest without joining IDs.

Events::

    {"mode": "daily" | "seasonal"}
        Run all registry claim states.  Seasonal also evaluates the optional
        operator-supplied ``watch/fee_status.csv``.
    {"mode": "daily", "states": ["NV"]}
        Operator/debug subset.  It writes the state digest but cannot replace
        the complete national digest.
    {"system": "alaska_state_claims", "mode": "daily" | "seasonal"}
        Run the independent AK DNR state-law watcher.
    {"scope": "legacy_aoi", "mode": "daily" | "seasonal"}
        Explicit legacy-only bbox compatibility.  It writes
        ``data/alerts/legacy_aoi_latest.json`` and never the national digest.

Production environment: BUCKET, SES_FROM, SES_TO, WEBHOOK_URL, SITE_URL.
``LEGACY_AOI_BBOX`` is consulted only for the explicit legacy event.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request

import boto3

from ak_deadlines import state_claim_deadlines
from spatial_clip import StateClipIndex


EP = "https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer"
AK_DNR_EP = ("https://arcgis.dnr.alaska.gov/arcgis/rest/services/OpenData/"
             "NaturalResource_StateMiningClaim/MapServer")
TYPE_DECODE = {
    "384101": "L", "384103": "L", "384201": "P", "384203": "P",
    "384301": "T", "384303": "T", "384401": "M", "384403": "M",
}
FEDERAL_FIELDS = (
    "OBJECTID,ADMIN_STATE,GEO_STATE,CSE_NR,CSE_NAME,CSE_TYPE_NR,"
    "CSE_DISP,CSE_META,RCRD_ACRS"
)
FOUNDATIONAL_CLAIM_STATES = frozenset(
    "AK AZ AR CA CO FL ID LA MS MT NE NV NM ND OR SD UT WA WY".split()
)
WATCH_ORDER = (
    "NV", "AZ", "CO", "UT", "NM", "AK", "SD", "ND", "NE", "AR",
    "FL", "LA", "MS", "CA", "ID", "MT", "OR", "WA", "WY",
)

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_PATH = os.path.join(HERE, "state_runtime.json")
STATE_CLIPS_PATH = os.path.join(HERE, "state_clips.json")
with open(RUNTIME_PATH, encoding="utf-8") as _runtime_file:
    _runtime = json.load(_runtime_file)["states"]
CLAIM_STATES = {
    code: row for code, row in _runtime.items()
    if row.get("regime") == "claim" and "federal_mlrs" in row.get("claim_systems", [])
}
if set(CLAIM_STATES) != set(FOUNDATIONAL_CLAIM_STATES):
    raise RuntimeError(
        "state_runtime.json claim-state set differs from the WS11 foundational set"
    )
with open(STATE_CLIPS_PATH, "rb") as _clip_file:
    _clip_bytes = _clip_file.read()
STATE_CLIPS = json.loads(_clip_bytes)["states"]
CLIP_VERSION = f"state-centroid-{hashlib.sha256(_clip_bytes).hexdigest()[:16]}"
STATE_CLIP_INDEX = {}

PAGE = 2000
MAX_PAGES = 400
TIME_RESERVE_MS = int(os.environ.get("WATCH_TIME_RESERVE_MS", "150000"))
CHAIN_MAX = int(os.environ.get("WATCH_CHAIN_MAX", "40"))
LOCK_TTL = int(os.environ.get("WATCH_LOCK_TTL_SECONDS", str(12 * 3600)))
CHECKPOINT_SCHEMA = 1
SNAPSHOT_SCHEMA = 2
DIGEST_SCHEMA = 2

s3 = boto3.client("s3")
lam = boto3.client("lambda")


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def fetch(url, tries=6, ms_left=None):
    """Fetch JSON, treating JSON-carried ArcGIS errors as retryable failures."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "nw-mineral-monitor-watch/2.0 (AWS Lambda)",
        "Accept": "application/json",
    })
    last = None
    attempts = 0
    for i in range(tries):
        attempts = i + 1
        if ms_left:
            usable_ms = ms_left() - TIME_RESERVE_MS
            if usable_ms <= 5_000:
                raise RuntimeError(
                    f"source fetch stopped for Lambda time budget: {last}"
                )
            timeout = max(5, min(45, int(usable_ms / 1000) - 2))
        else:
            timeout = 60
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                value = json.loads(response.read())
            if "error" in value:
                last = value["error"]
            else:
                return value
        except Exception as exc:  # noqa: BLE001 - remote transports vary
            last = exc
        if i + 1 == tries:
            break
        delay = min(30, 3 * (i + 1))
        if ms_left and ms_left() - TIME_RESERVE_MS <= delay * 1000 + 5_000:
            break
        time.sleep(delay)
    raise RuntimeError(f"source fetch failed after {attempts} tries: {last}")


def _s3_error_code(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _is_missing_s3_error(exc):
    return _s3_error_code(exc) in {"NoSuchKey", "404", "NotFound"}


def s3_json(bucket, key, default=None):
    """Read strict JSON.  Only a real missing key becomes *default*.

    AccessDenied, malformed JSON, and transport errors propagate.  Treating
    any of those as an empty snapshot would manufacture mass closures.
    """
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001 - botocore is not vendored
        if _is_missing_s3_error(exc):
            return default
        raise
    return json.loads(body)


def put_json(bucket, key, obj, cache="public, max-age=300"):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(obj, separators=(",", ":"), sort_keys=True).encode(),
        ContentType="application/json",
        CacheControl=cache,
    )


def canonical_json_bytes(obj):
    return json.dumps(
        obj, separators=(",", ":"), sort_keys=True).encode()


def object_sha256(obj):
    return hashlib.sha256(canonical_json_bytes(obj)).hexdigest()


def centroid(rings):
    """Area-weighted centroid of the first polygon ring."""
    ring = rings[0]
    area = cx = cy = 0.0
    for index in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[index][:2], ring[index + 1][:2]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area *= 0.5
    if abs(area) < 1e-12:
        return (
            sum(point[0] for point in ring) / len(ring),
            sum(point[1] for point in ring) / len(ring),
        )
    return cx / (6 * area), cy / (6 * area)


def point_in_state(state, x, y):
    if state not in STATE_CLIP_INDEX:
        STATE_CLIP_INDEX[state] = StateClipIndex(STATE_CLIPS[state])
    return STATE_CLIP_INDEX[state].contains(x, y)


def _trs(meta):
    out = []
    for part in (meta or "").split("|")[:4]:
        segment = part.strip().split()
        if len(segment) >= 5:
            township = segment[2].lstrip("0") or "0"
            range_number = segment[3].lstrip("0") or "0"
            section = segment[4].lstrip("0") or "0"
            out.append(f"T{township} R{range_number} Sec {section}")
    return sorted(set(out))[:3]


def _case_row(attributes, rings):
    x, y = centroid(rings)
    return x, y, {
        "name": attributes.get("CSE_NAME"),
        "disp": attributes.get("CSE_DISP"),
        "type": TYPE_DECODE.get(str(attributes.get("CSE_TYPE_NR")), "?"),
        "acres": attributes.get("RCRD_ACRS"),
        "trs": _trs(attributes.get("CSE_META")),
        "xy": [round(x, 5), round(y, 5)],
    }


def checkpoint_key(state):
    return f"watch/checkpoints/federal_{state.lower()}_active.json"


def snapshot_key(state):
    return f"watch/federal/{state.lower()}_active.json"


def state_digest_key(state):
    return f"data/alerts/states/{state.lower()}.json"


def run_state_digest_key(run_id, state, public=False):
    prefix = "data/alerts/runs" if public else "watch/federal/runs"
    return f"{prefix}/{run_id}/{state.lower()}.json"


def release_evidence_key(run_id, state, evidence):
    digest = object_sha256(evidence)
    return f"data/evidence/watch/{run_id}/{state.lower()}-{digest}.json"


def lock_key(state, system="federal_mlrs"):
    if system == "alaska_state_claims":
        return "watch/locks/alaska_state_active.json"
    if system != "federal_mlrs":
        raise ValueError(f"unsupported lock system {system!r}")
    return f"watch/locks/federal_{state.lower()}_active.json"


def checkpoint_load(bucket, state):
    checkpoint = s3_json(bucket, checkpoint_key(state))
    if checkpoint is None:
        return None
    if (checkpoint.get("schema_version") != CHECKPOINT_SCHEMA or
            checkpoint.get("state") != state or
            checkpoint.get("clip_version") != CLIP_VERSION):
        print(f"{state}: discarding incompatible watch checkpoint")
        checkpoint_clear(bucket, state)
        return None
    return checkpoint


def checkpoint_save(bucket, state, partition, cursor, records):
    put_json(bucket, checkpoint_key(state), {
        "schema_version": CHECKPOINT_SCHEMA,
        "state": state,
        "clip_version": CLIP_VERSION,
        "partition": partition,
        "cursor": cursor,
        "records": records,
    }, cache="private, no-store")


def checkpoint_clear(bucket, state):
    s3.delete_object(Bucket=bucket, Key=checkpoint_key(state))


def lock_read(bucket, state, system="federal_mlrs"):
    key = lock_key(state, system=system)
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001
        if _is_missing_s3_error(exc):
            return None
        raise
    value = json.loads(response["Body"].read())
    value["_etag"] = response.get("ETag")
    if not value["_etag"]:
        raise RuntimeError(f"S3 omitted ETag for {state} watch lock")
    return value


def lock_acquire(bucket, state, run_id, system="federal_mlrs"):
    try:
        s3.put_object(
            Bucket=bucket,
            Key=lock_key(state, system=system),
            Body=json.dumps({"run_id": run_id, "ts": time.time()}).encode(),
            ContentType="application/json",
            CacheControl="private, no-store",
            IfNoneMatch="*",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if _s3_error_code(exc) in {
                "PreconditionFailed", "412", "ConditionalRequestConflict"}:
            return False
        raise


def lock_refresh(bucket, state, run_id, system="federal_mlrs"):
    current = lock_read(bucket, state, system=system)
    if not current or current.get("run_id") != run_id:
        return False
    try:
        s3.put_object(
            Bucket=bucket,
            Key=lock_key(state, system=system),
            Body=json.dumps({"run_id": run_id, "ts": time.time()}).encode(),
            ContentType="application/json",
            CacheControl="private, no-store",
            IfMatch=current["_etag"],
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if _s3_error_code(exc) in {
                "PreconditionFailed", "412", "ConditionalRequestConflict"}:
            return False
        raise


def lock_release(bucket, state, run_id, system="federal_mlrs"):
    current = lock_read(bucket, state, system=system)
    if not current or current.get("run_id") != run_id:
        return False
    try:
        s3.delete_object(
            Bucket=bucket, Key=lock_key(state, system=system),
            IfMatch=current["_etag"])
        return True
    except Exception as exc:  # noqa: BLE001
        if _s3_error_code(exc) in {
                "PreconditionFailed", "412", "ConditionalRequestConflict"}:
            return False
        raise


def _owned_states(bucket, states, run_id, chain):
    owned = []
    for state in states:
        current = lock_read(bucket, state)
        if current and current.get("run_id") == run_id:
            if lock_refresh(bucket, state, run_id):
                owned.append(state)
            continue
        if current and time.time() - float(current.get("ts", 0)) < LOCK_TTL:
            action = "skipping root" if chain == 0 else "lost continuation"
            print(f"{state}: another watch run owns the snapshot — {action}")
            continue
        if current:
            lock_release(bucket, state, current.get("run_id"))
        if lock_acquire(bucket, state, run_id):
            owned.append(state)
    return owned


def pull_state_cases(state, bucket, ms_left):
    """Pull one complete state.  Return ``(records, done)``.

    The checkpoint is private and does not cause a diff until all query
    envelopes have reached an empty page.  Consequently an interrupted state
    stays unknown rather than becoming a zero-row state.
    """
    checkpoint = checkpoint_load(bucket, state)
    if checkpoint:
        partition = checkpoint["partition"]
        cursor = checkpoint["cursor"]
        records = checkpoint["records"]
        print(f"{state}: resume watch at envelope {partition}, cursor {cursor}, "
              f"{len(records):,} serials")
    else:
        partition, cursor, records = 0, 0, {}
    envelopes = CLAIM_STATES[state]["query_envelopes"]
    pages = 0
    while partition < len(envelopes) and pages < MAX_PAGES:
        if ms_left() < TIME_RESERVE_MS:
            checkpoint_save(bucket, state, partition, cursor, records)
            return None, False
        xmin, ymin, xmax, ymax = envelopes[partition]
        geometry = json.dumps({
            "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
            "spatialReference": {"wkid": 4326},
        })
        base = (
            f"{EP}/1/query?geometryType=esriGeometryEnvelope"
            f"&geometry={urllib.parse.quote(geometry)}&inSR=4326"
            f"&spatialRel=esriSpatialRelIntersects&outFields={FEDERAL_FIELDS}"
            "&returnGeometry=true&outSR=4326&geometryPrecision=5"
            "&orderByFields=OBJECTID&resultRecordCount=2000&f=json"
        )
        where = urllib.parse.quote(f"OBJECTID>{cursor}")
        try:
            value = fetch(base + "&where=" + where, ms_left=ms_left)
        except RuntimeError as exc:
            checkpoint_save(bucket, state, partition, cursor, records)
            print(f"{state}: source stalled ({exc}); checkpointed for continuation")
            return None, False
        features = value.get("features", [])
        if not features:
            partition += 1
            cursor = 0
            continue
        pages += 1
        for feature in features:
            attributes = feature.get("attributes") or {}
            cursor = max(cursor, int(attributes.get("OBJECTID") or 0))
            serial = attributes.get("CSE_NR")
            rings = (feature.get("geometry") or {}).get("rings")
            if not serial or not rings:
                continue
            x, y, row = _case_row(attributes, rings)
            if not point_in_state(state, x, y):
                continue
            if serial in records:
                old_trs = records[serial].get("trs") or []
                records[serial]["trs"] = sorted(set(old_trs + row["trs"]))[:3]
            else:
                records[serial] = row
    if partition < len(envelopes):
        checkpoint_save(bucket, state, partition, cursor, records)
        print(f"{state}: watch page budget reached; checkpointed at {len(records):,}")
        return None, False
    return records, True


def pull_cases(layer, bbox):
    """Legacy AOI-only pull.  National scheduled jobs never call this."""
    geometry = json.dumps({
        "xmin": bbox[0], "ymin": bbox[1], "xmax": bbox[2], "ymax": bbox[3],
        "spatialReference": {"wkid": 4326},
    })
    base = (
        f"{EP}/{layer}/query?geometryType=esriGeometryEnvelope"
        f"&geometry={urllib.parse.quote(geometry)}&inSR=4326"
        f"&spatialRel=esriSpatialRelIntersects&outFields={FEDERAL_FIELDS}"
        "&returnGeometry=true&outSR=4326&geometryPrecision=5"
        "&orderByFields=OBJECTID&resultRecordCount=2000&f=json"
    )
    cursor, out = 0, {}
    for _ in range(200):
        value = fetch(base + "&where=" + urllib.parse.quote(f"OBJECTID>{cursor}"))
        features = value.get("features", [])
        if not features:
            break
        for feature in features:
            attributes = feature.get("attributes") or {}
            cursor = max(cursor, int(attributes.get("OBJECTID") or 0))
            serial = attributes.get("CSE_NR")
            rings = (feature.get("geometry") or {}).get("rings")
            if serial and rings:
                _x, _y, out[serial] = _case_row(attributes, rings)
    return out


def pull_ak_state_cases(layer=0):
    """AK DNR state-law claims, a namespace independent of federal MLRS."""
    fields = (
        "OBJECTID,CASE_ID,CLAIM_NAME,CSSTTSDSCR,NTPSTDT,DATE_ALF,"
        "RFRNCMTRSC,TOT_ACRES,FILENUMBER,INFO_LINK,RFRSHDT"
    )
    base = (
        f"{AK_DNR_EP}/{layer}/query?outFields={fields}"
        "&returnGeometry=false&orderByFields=OBJECTID"
        "&resultRecordCount=2000&f=json"
    )
    cursor, out = 0, {}
    for _ in range(500):
        value = fetch(base + "&where=" + urllib.parse.quote(f"OBJECTID>{cursor}"))
        features = value.get("features", [])
        if not features:
            break
        for feature in features:
            attributes = feature.get("attributes") or {}
            cursor = max(cursor, attributes.get("OBJECTID") or cursor)
            serial = attributes.get("CASE_ID")
            if not serial:
                continue
            out[serial] = {
                "claim_key": f"alaska_state_claims:{serial}",
                "system_id": "alaska_state_claims",
                "jurisdiction": "state",
                "serial": serial,
                "name": attributes.get("CLAIM_NAME"),
                "status": attributes.get("CSSTTSDSCR"),
                "posting_date": attributes.get("NTPSTDT"),
                "annual_labor_filed": attributes.get("DATE_ALF"),
                "mtrsc": attributes.get("RFRNCMTRSC"),
                "acres": attributes.get("TOT_ACRES"),
                "file_number": attributes.get("FILENUMBER"),
                "info_link": attributes.get("INFO_LINK"),
                "refresh_date": attributes.get("RFRSHDT"),
            }
    return out


def _epoch_date(value):
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(
            float(value) / 1000, dt.timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def _normalize_serial(value):
    return "".join(str(value or "").upper().split())


def fee_status_index(bucket):
    """Return optional MLRS fee rows, or None when no report was supplied."""
    try:
        raw = s3.get_object(Bucket=bucket, Key="watch/fee_status.csv")["Body"].read()
    except Exception as exc:  # noqa: BLE001
        if _is_missing_s3_error(exc):
            return None
        raise
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace")))
    columns = {column.lower().strip(): column for column in reader.fieldnames or []}
    serial_column = next((
        columns[column] for column in columns
        if "serial" in column or column in ("cse_nr", "case number")
    ), None)
    fee_column = next((
        columns[column] for column in columns
        if "fee" in column or "assessment" in column or "maintain" in column
    ), None)
    if not serial_column:
        return None
    index = {}
    for row in reader:
        serial = _normalize_serial(row.get(serial_column))
        if serial:
            index[serial] = ((row.get(fee_column) or "").strip()
                             if fee_column else "")
    return index


def _federal_snapshot(state, records, generated, run_id=None):
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "system_id": "federal_mlrs",
        "state": state,
        "generated": generated,
        "run_id": run_id,
        "complete": True,
        "active_count": len(records),
        "records": records,
    }


def _snapshot_records(snapshot, state):
    if snapshot is None:
        return None
    if (not isinstance(snapshot, dict) or
            snapshot.get("schema_version") != SNAPSHOT_SCHEMA or
            snapshot.get("system_id") != "federal_mlrs" or
            snapshot.get("state") != state or snapshot.get("complete") is not True or
            not isinstance(snapshot.get("records"), dict) or
            snapshot.get("active_count") != len(snapshot["records"])):
        raise RuntimeError(
            f"{state}: prior federal watch snapshot is malformed; refusing a diff"
        )
    return snapshot["records"]


def build_state_digest(state, mode, run_id, current, prior_snapshot, fees, site=""):
    prior = _snapshot_records(prior_snapshot, state)
    seeded = prior is None
    alerts = []
    generated = _utc_now().isoformat()
    if not seeded:
        for serial, previous in prior.items():
            if serial not in current:
                alerts.append({
                    "kind": "ACTIVE→CLOSED",
                    "state": state,
                    "system_id": "federal_mlrs",
                    "ser": serial,
                    "name": previous.get("name"),
                    "type": previous.get("type"),
                    "trs": previous.get("trs"),
                    "xy": previous.get("xy"),
                    "evidence": (
                        f"present as '{previous.get('disp')}' in the prior complete "
                        f"{state} snapshot; absent from the complete MLRS active "
                        f"snapshot {generated}"
                    ),
                    "note": (
                        "BLM adjudication lags reality — verify the serial register "
                        "before treating this ground as open."
                    ),
                    "link": f"{site}/#claim={serial}" if site else None,
                    "srp": "https://reports.blm.gov/report/MLRS/95/Serial-Register-Page",
                })
        for serial, row in current.items():
            if serial not in prior and (row.get("disp") or "").upper() == "FILED":
                alerts.append({
                    "kind": "NEW FILING",
                    "state": state,
                    "system_id": "federal_mlrs",
                    "ser": serial,
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "trs": row.get("trs"),
                    "xy": row.get("xy"),
                    "evidence": f"new FILED case in the complete {state} MLRS snapshot",
                    "link": f"{site}/#claim={serial}" if site else None,
                })

    lapse_note = None
    lapse_scan = {"status": "not_run", "likely_lapsed_count": None}
    if mode == "seasonal":
        if fees is None:
            lapse_note = (
                f"{state}: maintenance-fee scan UNKNOWN because the public GIS has "
                "no fee actions and watch/fee_status.csv was not supplied."
            )
            lapse_scan = {
                "status": "unknown",
                "likely_lapsed_count": None,
                "reason": "fee_status_unavailable",
            }
        else:
            assessment_year = str(_utc_now().year)
            likely = 0
            for serial, row in current.items():
                fee_value = fees.get(_normalize_serial(serial))
                if fee_value is not None and assessment_year not in fee_value:
                    likely += 1
                    alerts.append({
                        "kind": "LIKELY LAPSED — verify",
                        "state": state,
                        "system_id": "federal_mlrs",
                        "ser": serial,
                        "name": row.get("name"),
                        "trs": row.get("trs"),
                        "xy": row.get("xy"),
                        "evidence": (
                            f"no {assessment_year} fee action in the operator-supplied "
                            f"MLRS report (field value: '{fee_value or 'blank'}')"
                        ),
                        "note": (
                            "Sept 1 is the federal maintenance-fee deadline, but BLM "
                            "adjudication lags. This is a lead, not a conclusion; verify "
                            "the serial register."
                        ),
                        "link": f"{site}/#claim={serial}" if site else None,
                    })
            lapse_scan = {"status": "complete", "likely_lapsed_count": likely}

    return {
        "schema_version": DIGEST_SCHEMA,
        "generated": generated,
        "run_id": run_id,
        "mode": mode,
        "state": state,
        "system_id": "federal_mlrs",
        "status": "complete",
        "seeded": seeded,
        "previous_active": None if seeded else len(prior),
        "active_now": len(current),
        "alerts": alerts,
        "lapse_scan": lapse_scan,
        "lapse_note": lapse_note,
        "source": f"{EP}/1",
        "snapshot_scope": "private_state_snapshot",
        "disclaimer": (
            "Research leads only. Verify at BLM and the applicable county or "
            "recording-district recorder before staking."
        ),
    }


def _valid_state_digest(value, state, run_id):
    return (
        isinstance(value, dict) and value.get("schema_version") == DIGEST_SCHEMA and
        value.get("system_id") == "federal_mlrs" and value.get("state") == state and
        value.get("run_id") == run_id and value.get("status") == "complete" and
        isinstance(value.get("active_now"), int) and
        not isinstance(value.get("active_now"), bool) and value["active_now"] >= 0 and
        isinstance(value.get("alerts"), list)
    )


def _watch_system_evidence(system_id, snapshot):
    if (not isinstance(snapshot, dict) or
            snapshot.get("schema_version") != SNAPSHOT_SCHEMA or
            snapshot.get("system_id") != system_id or
            snapshot.get("complete") is not True or
            not isinstance(snapshot.get("active_count"), int) or
            isinstance(snapshot.get("active_count"), bool) or
            snapshot["active_count"] < 0 or
            not isinstance(snapshot.get("records"), dict) or
            len(snapshot["records"]) != snapshot["active_count"]):
        raise RuntimeError(
            f"{system_id}: cannot emit release evidence from incomplete snapshot"
        )
    return {
        "status": "complete",
        "active_now": snapshot["active_count"],
        "source_snapshot_sha256": object_sha256(snapshot),
    }


def build_release_evidence(bucket, state, marker):
    """Build one checksummed, content-addressed DONE-gate watch artifact."""
    run_id = marker["run_id"]
    federal = s3_json(bucket, snapshot_key(state))
    if (not isinstance(federal, dict) or federal.get("run_id") != run_id or
            federal.get("state") != state):
        raise RuntimeError(
            f"{state}: federal snapshot is not bound to national run {run_id}"
        )
    systems = {
        "federal_mlrs": _watch_system_evidence("federal_mlrs", federal),
    }
    if state == "AK":
        alaska = s3_json(bucket, "watch/alaska_state/active.json")
        if not isinstance(alaska, dict) or alaska.get("state") != "AK":
            raise RuntimeError(
                "AK: complete Alaska state-claim snapshot is required for release evidence"
            )
        systems["alaska_state_claims"] = _watch_system_evidence(
            "alaska_state_claims", alaska)
    return {
        "schema_version": 1,
        "state": state,
        "run_id": run_id,
        "generated": marker["generated"],
        "complete": True,
        "systems": systems,
    }


def publish_release_evidence(bucket, marker, evidence=None):
    """Publish all-or-none evidence for the federal run.

    An Alaska state snapshot may not exist yet on a new deployment. In that
    case no state's new release evidence is published; releases remain
    correctly blocked, while the operational alert digest still advances.
    """
    if evidence is None:
        evidence = {
            state: build_release_evidence(bucket, state, marker)
            for state in WATCH_ORDER
        }
    for state, value in evidence.items():
        put_json(bucket, release_evidence_key(marker["run_id"], state, value), value)
    return {
        state: release_evidence_key(marker["run_id"], state, evidence[state])
        for state in WATCH_ORDER
    }


def _committed_state_digest(bucket, state, run_id):
    """Return a prior same-run commit after an asynchronous retry.

    Lambda can fail after advancing a state snapshot but before it queues the
    next continuation.  Binding the private digest to the snapshot run ID and
    timestamp makes replay idempotent instead of erasing the transition on the
    retry's second diff.
    """
    digest = s3_json(bucket, run_state_digest_key(run_id, state))
    snapshot = s3_json(bucket, snapshot_key(state))
    if (not _valid_state_digest(digest, state, run_id) or
            not isinstance(snapshot, dict) or
            snapshot.get("schema_version") != SNAPSHOT_SCHEMA or
            snapshot.get("system_id") != "federal_mlrs" or
            snapshot.get("state") != state or snapshot.get("complete") is not True or
            snapshot.get("run_id") != run_id or
            snapshot.get("generated") != digest.get("generated") or
            snapshot.get("active_count") != digest.get("active_now") or
            not isinstance(snapshot.get("records"), dict) or
            len(snapshot["records"]) != snapshot["active_count"]):
        return None
    return digest


def build_merged_digest(bucket, marker=None, ak_digest=None):
    """Build the national public digest with explicit unknown state rows."""
    marker = marker if marker is not None else s3_json(
        bucket, "watch/federal/latest_run.json")
    marker_valid = (
        isinstance(marker, dict) and marker.get("schema_version") == 2 and
        marker.get("complete") is True and
        set(marker.get("states") or []) == set(CLAIM_STATES) and
        isinstance(marker.get("run_id"), str) and bool(marker["run_id"]) and
        marker.get("state_digest_prefix") ==
        f"data/alerts/runs/{marker.get('run_id')}/"
    )
    run_id = marker.get("run_id") if marker_valid else None
    state_rows = {}
    federal_alerts = []
    lapse_notes = []
    active_total = 0
    for state in WATCH_ORDER:
        value = s3_json(
            bucket, run_state_digest_key(run_id, state, public=True)
        ) if run_id else None
        if run_id and _valid_state_digest(value, state, run_id):
            state_rows[state] = {
                "status": "complete",
                "active_now": value["active_now"],
                "alerts": len(value["alerts"]),
                "generated": value.get("generated"),
            }
            active_total += value["active_now"]
            federal_alerts.extend(value["alerts"])
            if value.get("lapse_note"):
                lapse_notes.append(value["lapse_note"])
        else:
            state_rows[state] = {
                "status": "unknown",
                "active_now": None,
                "alerts": None,
                "generated": value.get("generated") if isinstance(value, dict) else None,
            }
    federal_complete = marker_valid and all(
        row["status"] == "complete" for row in state_rows.values())

    if ak_digest is None:
        ak_digest = s3_json(bucket, "data/alerts/ak_state_latest.json")
    ak_valid = (
        isinstance(ak_digest, dict) and
        ak_digest.get("schema_version") == DIGEST_SCHEMA and
        ak_digest.get("system_id") == "alaska_state_claims" and
        ak_digest.get("state") == "AK" and ak_digest.get("status") == "complete" and
        isinstance(ak_digest.get("active_now"), int) and
        not isinstance(ak_digest.get("active_now"), bool) and
        ak_digest["active_now"] >= 0 and isinstance(ak_digest.get("alerts"), list)
    )
    ak_summary = {
        "status": "complete" if ak_valid else "unknown",
        "active_now": ak_digest["active_now"] if ak_valid else None,
        "alerts": len(ak_digest["alerts"]) if ak_valid else None,
        "generated": ak_digest.get("generated") if isinstance(ak_digest, dict) else None,
    }
    ak_alerts = ak_digest["alerts"] if ak_valid else []
    generated = _utc_now().isoformat()
    all_alerts = federal_alerts + ak_alerts
    return {
        "schema_version": DIGEST_SCHEMA,
        "generated": generated,
        "mode": marker.get("mode", "daily") if marker_valid else "unknown",
        "system_id": "national_claim_expiration_watch",
        "status": "complete" if federal_complete and ak_valid else "incomplete",
        "complete": bool(federal_complete and ak_valid),
        # Compatibility field: federal total only.  Unknown is JSON null, not 0.
        "active_now": active_total if federal_complete else None,
        "alerts": all_alerts,
        "lapse_note": " ".join(dict.fromkeys(lapse_notes)) or None,
        "systems": {
            "federal_mlrs": {
                "status": "complete" if federal_complete else "unknown",
                "active_now": active_total if federal_complete else None,
                "alerts": len(federal_alerts) if federal_complete else None,
                "run_id": run_id,
                "states": state_rows,
            },
            "alaska_state_claims": ak_summary,
        },
        "state_order": list(WATCH_ORDER),
        "disclaimer": (
            "Research leads only. An absent claim from a complete snapshot is not a "
            "title conclusion; verify BLM, AK DNR when applicable, and recorder records."
        ),
    }


def publish_merged_digest(bucket, marker=None, ak_digest=None):
    digest = build_merged_digest(bucket, marker=marker, ak_digest=ak_digest)
    put_json(bucket, "data/alerts/latest.json", digest)
    return digest


def send_notifications(digest, site=""):
    """Send only when configured.  Tests and builds never configure recipients."""
    alerts = digest.get("alerts") or []
    lapse_note = digest.get("lapse_note")
    if not alerts and not lapse_note:
        return []
    sent = []
    sender, recipients = os.environ.get("SES_FROM", ""), os.environ.get("SES_TO", "")
    system = digest.get("system_id") or "federal_mlrs"
    generated = digest.get("generated") or ""
    if sender and recipients:
        lines = [f"NW Mineral Monitor — {len(alerts)} {system} alert(s), {generated}", ""]
        for alert in alerts[:60]:
            serial = alert.get("ser") or alert.get("serial") or alert.get("instrument")
            lines += [
                f"[{alert.get('kind', 'ALERT')}] {alert.get('state', '')} "
                f"{alert.get('name') or '(system calendar)'} — {serial or 'no serial'}",
                f"  TRS: {'; '.join(alert.get('trs') or []) or alert.get('mtrsc') or 'see source'}",
                f"  {alert.get('evidence', '')}",
                f"  {alert.get('deadline') or alert.get('info_link') or alert.get('link') or ''}",
                "",
            ]
        if lapse_note:
            lines += ["", lapse_note]
        lines += ["", digest.get("disclaimer") or "Research leads only; verify source records."]
        try:
            boto3.client("ses").send_email(
                Source=sender,
                Destination={"ToAddresses": [
                    item.strip() for item in recipients.split(",") if item.strip()
                ]},
                Message={
                    "Subject": {"Data": f"[NW-MM] {len(alerts)} {system} alert(s)"},
                    "Body": {"Text": {"Data": "\n".join(lines)}},
                },
            )
            sent.append("ses")
        except Exception as exc:  # noqa: BLE001
            print("SES send failed:", exc)
    webhook = os.environ.get("WEBHOOK_URL", "")
    if webhook:
        try:
            request = urllib.request.Request(
                webhook, data=json.dumps(digest).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=20).read()
            sent.append("webhook")
        except Exception as exc:  # noqa: BLE001
            print("webhook failed:", exc)
    return sent


def _ak_prior_records(snapshot):
    if snapshot is None:
        return None
    if (isinstance(snapshot, dict) and
            snapshot.get("schema_version") == SNAPSHOT_SCHEMA and
            snapshot.get("system_id") == "alaska_state_claims" and
            snapshot.get("state") == "AK" and snapshot.get("complete") is True and
            isinstance(snapshot.get("records"), dict) and
            snapshot.get("active_count") == len(snapshot["records"])):
        return snapshot["records"]
    # One-time migration from the pre-national private raw mapping.
    if isinstance(snapshot, dict) and all(isinstance(row, dict) for row in snapshot.values()):
        return snapshot
    raise RuntimeError("AK: prior state-claim snapshot is malformed; refusing a diff")


def _handler_ak_state_owned(event, context):
    """Run the AK DNR diff after the caller owns its snapshot lock."""
    bucket = os.environ["BUCKET"]
    mode = event.get("mode", "daily")
    if mode not in {"daily", "seasonal"}:
        raise ValueError("AK state watch mode must be daily or seasonal")
    current = pull_ak_state_cases(0)
    snapshot_path = "watch/alaska_state/active.json"
    prior_snapshot = s3_json(bucket, snapshot_path)
    if prior_snapshot is None:
        prior_snapshot = s3_json(bucket, "watch/state_ak_dnr_active.json")
    prior = _ak_prior_records(prior_snapshot)
    seeded = prior is None
    today = _utc_now().date()
    alerts = []
    if not seeded:
        for serial, row in current.items():
            if serial not in prior:
                alerts.append({
                    "kind": "AK STATE CLAIM — NEW ACTIVE", "state": "AK", **row,
                    "evidence": "new in the complete AK DNR active state-claim layer",
                    "note": "State-law claim; do not look for this ADL identifier in MLRS.",
                })
                posted = _epoch_date(row.get("posting_date"))
                if posted:
                    rules = state_claim_deadlines(posted, today.year)
                    due = dt.date.fromisoformat(rules["rent"]["initial_due"])
                    if -5 <= (due - today).days <= 45:
                        alerts.append({
                            "kind": "AK STATE INITIAL RENT DEADLINE",
                            "state": "AK", **row,
                            "deadline": due.isoformat(),
                            "deadline_type": "initial_rent",
                            "evidence": "computed as 45 days after DNR posting date",
                            "source": rules["rent"]["source"],
                            "note": rules["disclaimer"],
                        })
        for serial, row in prior.items():
            if serial not in current:
                alerts.append({
                    "kind": "AK STATE CLAIM — LEFT ACTIVE LAYER",
                    "state": "AK", **row,
                    "evidence": "present in prior complete AK DNR snapshot; absent now",
                    "note": "Verify LAS/source documents; absence alone does not prove open ground.",
                })
    calendar_alert = None
    if mode == "seasonal":
        rules = state_claim_deadlines(today, today.year)
        calendar_alert = {
            "kind": "AK STATE RENT + LABOR CALENDAR",
            "state": "AK",
            "system_id": "alaska_state_claims",
            "rent_due": rules["rent"]["subsequent_due"],
            "rent_received_grace_ends": rules["rent"]["received_grace_ends"],
            "abandonment_if_unpaid": rules["rent"]["abandonment_if_unpaid"],
            "labor_or_cash_due": rules["labor"]["work_complete_due"],
            "labor_statement_due": rules["labor"]["statement_recording_due"],
            "sources": [rules["rent"]["source"], rules["labor"]["source"]],
            "note": (
                "DNR publishes no reviewed rent-payment action feed here; this is a "
                "deadline calendar, not a nonpayment determination. " + rules["disclaimer"]
            ),
        }
        alerts.insert(0, calendar_alert)
    generated = _utc_now().isoformat()
    digest = {
        "schema_version": DIGEST_SCHEMA,
        "generated": generated,
        "mode": mode,
        "state": "AK",
        "system_id": "alaska_state_claims",
        "status": "complete",
        "seeded": seeded,
        "previous_active": None if seeded else len(prior),
        "active_now": len(current),
        "alerts": alerts,
        "source": f"{AK_DNR_EP}/0",
        "calendar": calendar_alert,
        "snapshot_scope": "private_state_snapshot",
        "disclaimer": "AK DNR/LAS and recorded source documents control.",
    }
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "system_id": "alaska_state_claims",
        "state": "AK",
        "generated": generated,
        "complete": True,
        "active_count": len(current),
        "records": current,
    }
    # Publish the alert evidence before advancing the private diff baseline.
    put_json(bucket, "data/alerts/ak_state_latest.json", digest)
    put_json(bucket, snapshot_path, snapshot, cache="private, no-store")
    publish_merged_digest(bucket, ak_digest=digest)
    sent = send_notifications(digest, os.environ.get("SITE_URL", "").rstrip("/"))
    print(f"AK DNR {mode}: {len(current)} active, {len(alerts)} alerts")
    return {
        "mode": mode,
        "system_id": "alaska_state_claims",
        "active": len(current),
        "alerts": len(alerts),
        "notified": sent,
    }


def handler_ak_state(event, context):
    """Independent AK DNR watch with one conditional owner per snapshot."""
    bucket = os.environ["BUCKET"]
    run_id = event.get("run_id") or (
        context.aws_request_id if context else f"local-ak-{time.time_ns()}"
    )
    if (not isinstance(run_id, str) or
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id) is None):
        raise ValueError("run_id must be 1-128 safe identifier characters")
    current = lock_read(bucket, "AK", system="alaska_state_claims")
    if current and time.time() - float(current.get("ts", 0)) >= 30 * 60:
        lock_release(
            bucket, "AK", current.get("run_id"), system="alaska_state_claims")
        current = None
    if current or not lock_acquire(
            bucket, "AK", run_id, system="alaska_state_claims"):
        return {
            "mode": event.get("mode", "daily"),
            "system_id": "alaska_state_claims",
            "status": "busy",
            "complete": False,
            "active": None,
            "alerts": None,
            "notified": [],
        }
    try:
        return _handler_ak_state_owned(event, context)
    finally:
        if not lock_release(
                bucket, "AK", run_id, system="alaska_state_claims"):
            print("AK DNR watch lock was no longer owned at release")


def _normalize_states(value):
    if value is None:
        states = list(CLAIM_STATES)
    elif not isinstance(value, list) or not value:
        raise ValueError("states must be a nonempty list of registry claim-state codes")
    else:
        states = list(dict.fromkeys(str(item).upper() for item in value))
    invalid = sorted(set(states) - set(CLAIM_STATES))
    if invalid:
        raise ValueError(f"not registry claim states: {', '.join(invalid)}")
    order = {state: index for index, state in enumerate(WATCH_ORDER)}
    return sorted(states, key=lambda state: order[state])


def _invoke_continuation(context, payload):
    function_name = (os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or
                     (context.function_name if context else None))
    if not function_name:
        raise RuntimeError("AWS_LAMBDA_FUNCTION_NAME is required for continuation")
    lam.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode(),
    )


def handler_federal(event, context):
    mode = event.get("mode", "daily")
    if mode not in {"daily", "seasonal"}:
        raise ValueError("federal watch mode must be daily or seasonal")
    bucket = os.environ["BUCKET"]
    site = os.environ.get("SITE_URL", "").rstrip("/")
    chain = int(event.get("chain", 0))
    if chain < 0 or chain > CHAIN_MAX:
        raise ValueError("invalid federal watch chain number")
    run_id = event.get("run_id") or (
        context.aws_request_id if context else f"local-{time.time_ns()}"
    )
    if (not isinstance(run_id, str) or
            re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id) is None):
        raise ValueError("run_id must be 1-128 safe identifier characters")
    requested = _normalize_states(
        event.get("requested_states", event.get("states")))
    national = set(requested) == set(CLAIM_STATES)
    remaining = _normalize_states(event.get("remaining_states", requested))
    if not set(remaining) <= set(requested):
        raise ValueError("remaining_states must be a subset of requested_states")
    completed = _normalize_states(event.get("completed_states")) \
        if event.get("completed_states") else []
    if set(completed) & set(remaining):
        raise ValueError("completed_states and remaining_states overlap")
    ms_left = (context.get_remaining_time_in_millis if context
               else (lambda: 900_000))

    lock_scope = requested if national else remaining
    owned = _owned_states(bucket, lock_scope, run_id, chain)
    if set(owned) != set(lock_scope):
        busy = sorted(set(lock_scope) - set(owned))
        for state in owned:
            lock_release(bucket, state, run_id)
        if chain:
            raise RuntimeError(
                f"continuation lost state snapshot ownership: {', '.join(busy)}"
            )
        # No partial run starts when a scheduled national root cannot own all
        # requested snapshots.  Counts remain unknown, never synthesized zero.
        return {
            "mode": mode,
            "status": "busy",
            "complete": False,
            "active": None,
            "unknown_states": busy,
            "notified": [],
        }

    fees = fee_status_index(bucket) if mode == "seasonal" else None
    try:
        while remaining:
            state = remaining[0]
            committed = _committed_state_digest(bucket, state, run_id)
            if committed is not None:
                try:
                    checkpoint_clear(bucket, state)
                except Exception as exc:  # noqa: BLE001
                    print(f"{state}: replay checkpoint cleanup failed: {exc}")
                if not national and not lock_release(bucket, state, run_id):
                    raise RuntimeError(
                        f"lost ownership while replaying {state} watch commit")
                if state not in completed:
                    completed.append(state)
                remaining.pop(0)
                print(f"{state}: reused durable same-run watch commit")
                continue
            records, done = pull_state_cases(state, bucket, ms_left)
            if not done:
                if chain + 1 > CHAIN_MAX:
                    raise RuntimeError(
                        f"federal watch chain limit {CHAIN_MAX} hit on {state}"
                    )
                # A national run deliberately retains all 19 state locks until
                # private snapshots/evidence inputs are captured. Refresh the
                # completed-state locks too, not just the paging remainder.
                continuation_locks = requested if national else remaining
                for owned_state in continuation_locks:
                    if not lock_refresh(bucket, owned_state, run_id):
                        raise RuntimeError(
                            f"lost {owned_state} watch lock before continuation"
                        )
                _invoke_continuation(context, {
                    "mode": mode,
                    "requested_states": requested,
                    "remaining_states": remaining,
                    "completed_states": completed,
                    "chain": chain + 1,
                    "run_id": run_id,
                })
                return {
                    "mode": mode,
                    "status": "continued",
                    "complete": False,
                    "active": None,
                    "chain": chain + 1,
                    "completed_states": completed,
                    "unknown_states": list(remaining),
                    "notified": [],
                }

            previous = s3_json(bucket, snapshot_key(state))
            digest = build_state_digest(
                state, mode, run_id, records, previous, fees, site=site)
            generated = digest["generated"]
            snapshot = _federal_snapshot(state, records, generated, run_id=run_id)
            # Alert evidence is durable before advancing the private baseline.
            put_json(bucket, run_state_digest_key(run_id, state), digest,
                     cache="private, no-store")
            put_json(bucket, snapshot_key(state), snapshot, cache="private, no-store")
            try:
                checkpoint_clear(bucket, state)
            except Exception as exc:  # noqa: BLE001
                print(f"{state}: stale checkpoint cleanup failed after commit: {exc}")
            if not national and not lock_release(bucket, state, run_id):
                raise RuntimeError(f"lost ownership while releasing {state} watch lock")
            completed.append(state)
            remaining.pop(0)
            print(f"{state}: {len(records):,} active, {len(digest['alerts'])} alerts")
    except Exception:
        for state in (requested if national else remaining):
            try:
                lock_release(bucket, state, run_id)
            except Exception as cleanup_error:  # noqa: BLE001
                print(f"failed releasing {state} after watch error: {cleanup_error}")
        raise

    if not national:
        subset_digest = {
            "schema_version": DIGEST_SCHEMA,
            "generated": _utc_now().isoformat(),
            "run_id": run_id,
            "mode": mode,
            "system_id": "federal_mlrs",
            "status": "complete_subset",
            "states": list(completed),
            "alerts": [],
            "disclaimer": (
                "Operator subset run; the scheduled national digest was not replaced."
            ),
        }
        for state in completed:
            digest = s3_json(bucket, run_state_digest_key(run_id, state))
            if _valid_state_digest(digest, state, run_id):
                subset_digest["alerts"].extend(digest["alerts"])
        sent = send_notifications(subset_digest, site)
        return {
            "mode": mode,
            "status": "complete_subset",
            "complete": True,
            "states": completed,
            "active": None,
            "notified": sent,
            "run_id": run_id,
        }
    if set(completed) != set(CLAIM_STATES):
        raise RuntimeError("national watch completed without exact 19-state evidence")
    marker = {
        "schema_version": 2,
        "generated": _utc_now().isoformat(),
        "run_id": run_id,
        "mode": mode,
        "states": list(WATCH_ORDER),
        "state_digest_prefix": f"data/alerts/runs/{run_id}/",
        "complete": True,
    }
    # Capture every private input while this run still owns all state locks.
    # Public writes happen after release and therefore cannot be mixed with a
    # manual subset run that starts at the same instant.
    prepared_digests = {}
    prepared_evidence = None
    preparation_error = None
    try:
        for state in WATCH_ORDER:
            digest = s3_json(bucket, run_state_digest_key(run_id, state))
            if not _valid_state_digest(digest, state, run_id):
                raise RuntimeError(
                    f"{state}: missing durable state digest at promotion")
            prepared_digests[state] = digest
        try:
            prepared_evidence = {
                state: build_release_evidence(bucket, state, marker)
                for state in WATCH_ORDER
            }
        except RuntimeError as exc:
            # Alert delivery has an independent operational cadence. A stale
            # AK-state snapshot blocks DONE evidence, not federal alerts.
            print(f"release evidence withheld: {exc}")
    except Exception as exc:  # noqa: BLE001 - release locks before propagating
        preparation_error = exc

    release_errors = []
    for state in requested:
        try:
            if not lock_release(bucket, state, run_id):
                release_errors.append(state)
        except Exception as exc:  # noqa: BLE001
            print(f"failed releasing final {state} watch lock: {exc}")
            release_errors.append(state)
    if release_errors:
        raise RuntimeError(
            "lost final watch lock ownership: " + ", ".join(release_errors))
    if preparation_error is not None:
        raise preparation_error

    # Promote only alert-sized state evidence into a content-addressed public
    # run. The marker switches readers after every one of the 19 objects is
    # present, so a crash during promotion leaves the prior national run valid.
    for state in WATCH_ORDER:
        digest = prepared_digests[state]
        put_json(bucket, run_state_digest_key(run_id, state, public=True), digest)
        put_json(bucket, state_digest_key(state), digest)
    put_json(bucket, "watch/federal/latest_run.json", marker,
             cache="private, no-store")
    merged = publish_merged_digest(bucket, marker=marker)
    evidence_paths = None
    if prepared_evidence is not None:
        evidence_paths = publish_release_evidence(
            bucket, marker, evidence=prepared_evidence)
    federal = merged["systems"]["federal_mlrs"]
    notification = {
        "generated": merged["generated"],
        "mode": mode,
        "system_id": "federal_mlrs",
        "alerts": [
            alert for alert in merged["alerts"]
            if alert.get("system_id") == "federal_mlrs"
        ],
        "lapse_note": merged.get("lapse_note"),
        "disclaimer": merged["disclaimer"],
    }
    sent = send_notifications(notification, site)
    return {
        "mode": mode,
        "status": "complete",
        "complete": True,
        "states": list(WATCH_ORDER),
        "active": federal["active_now"],
        "alerts": federal["alerts"],
        "national_digest_status": merged["status"],
        "release_evidence": evidence_paths,
        "notified": sent,
        "run_id": run_id,
    }


def handler_legacy_aoi(event, context):
    """Explicitly isolated compatibility path for the former Cassia bbox."""
    mode = event.get("mode", "daily")
    if mode not in {"daily", "seasonal"}:
        raise ValueError("legacy AOI mode must be daily or seasonal")
    bucket = os.environ["BUCKET"]
    raw_bbox = os.environ.get(
        "LEGACY_AOI_BBOX",
        os.environ.get("AOI_BBOX", "-114.2867,41.9882,-113.0,42.6878"),
    )
    bbox = [float(value) for value in raw_bbox.split(",")]
    if len(bbox) != 4:
        raise ValueError("LEGACY_AOI_BBOX needs four comma-separated numbers")
    current = pull_cases(1, bbox)
    previous = s3_json(bucket, "watch/legacy_aoi/active.json")
    prior = previous.get("records") if isinstance(previous, dict) else None
    seeded = prior is None
    alerts = []
    if not seeded:
        for serial, row in prior.items():
            if serial not in current:
                alerts.append({
                    "kind": "ACTIVE→CLOSED", "ser": serial,
                    "name": row.get("name"), "trs": row.get("trs"),
                    "xy": row.get("xy"), "scope": "legacy_aoi",
                    "evidence": "absent from the next complete legacy AOI snapshot",
                })
    generated = _utc_now().isoformat()
    digest = {
        "schema_version": DIGEST_SCHEMA,
        "generated": generated,
        "mode": mode,
        "scope": "legacy_aoi",
        "status": "complete",
        "seeded": seeded,
        "active_now": len(current),
        "aoi_bbox": bbox,
        "alerts": alerts,
        "disclaimer": "Legacy AOI compatibility only; the national digest is separate.",
    }
    put_json(bucket, "data/alerts/legacy_aoi_latest.json", digest)
    put_json(bucket, "watch/legacy_aoi/active.json", {
        "schema_version": SNAPSHOT_SCHEMA,
        "generated": generated,
        "complete": True,
        "records": current,
    }, cache="private, no-store")
    sent = send_notifications(digest, os.environ.get("SITE_URL", "").rstrip("/"))
    return {
        "mode": mode, "scope": "legacy_aoi", "active": len(current),
        "alerts": len(alerts), "notified": sent,
    }


def handler(event, context):
    event = event or {}
    if event.get("system") == "alaska_state_claims":
        return handler_ak_state(event, context)
    if event.get("scope") == "legacy_aoi":
        return handler_legacy_aoi(event, context)
    if event.get("system") not in (None, "federal_mlrs"):
        raise ValueError(f"unsupported claim system {event.get('system')!r}")
    return handler_federal(event, context)
