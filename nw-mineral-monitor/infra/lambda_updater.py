"""NW Mineral Monitor — nightly claims updater.

Pulls current mining-claim records from BLM MLRS GIS for all 19 registry
claim states. Raw snapshots land in private S3 staging for the PMTiles
publication job; the browser never receives a statewide JSON snapshot.

Runs on AWS Lambda (python3.12, stdlib only — no layers needed).
Event: {"mode": "active" | "closed", "states": ["NV"]}
       (both keys optional; defaults: mode=active, all registry claim states)

Pagination per BLM server quirks (verified 2026-07-30):
- use an OBJECTID cursor (where=OBJECTID>n), NOT resultOffset
- short pages with exceededTransferLimit=true are normal — stop only on an
  empty page
- query with a state BBOX ENVELOPE, not a detailed polygon (polygons exhaust
  the server's per-request budget and cause mid-stream empty pages)

Closed layers are pulled NEWEST-FIRST (OBJECTID DESC, where OBJECTID<cursor)
and page to exhaustion by default. These snapshots are private PMTiles build
inputs, so the former 250,000-row browser-JSON cap would silently prevent a
complete state release. CLOSED_CAP remains an explicit progress/debug
override; capped output is labeled truncated and cannot pass publication.

CHECKPOINT + SELF-CONTINUATION (added 2026-08-06 — the crash fix):
Lambda's hard ceiling is 15 minutes, and a large closed pull takes
40–160 minutes at BLM's throttle, so the monthly closed refreshes used to
time out on every run (and NV active flirted with OOM at 512 MB — memory is
now 2048 in the template). The updater is now resumable: when fewer than
TIME_RESERVE ms remain, it writes progress (cursor + columns) to
s3://bucket/ckpt/{st}_{mode}.json, asynchronously re-invokes ITSELF with the
remaining work, and exits cleanly. The chain continues until every state is
done; each completed state deletes its checkpoint and stamps the manifest.
CHAIN_MAX (env, default 30) is the runaway stop. Needs
lambda:InvokeFunction on itself + s3 rw on ckpt/* (both in template.yaml).
"""
import hashlib, json, os, time, urllib.request, urllib.parse, boto3

from spatial_clip import StateClipIndex

EP = "https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer"
LAYER = {"active": 1, "closed": 2}
RUNTIME_PATH = os.path.join(os.path.dirname(__file__), 'state_runtime.json')
STATE_CLIPS_PATH = os.path.join(os.path.dirname(__file__), 'state_clips.json')
with open(RUNTIME_PATH) as _runtime_file:
    _runtime = json.load(_runtime_file)['states']
CLAIM_STATES = {code: row for code, row in _runtime.items()
                if row['regime'] == 'claim' and 'federal_mlrs' in row['claim_systems']}
with open(STATE_CLIPS_PATH, 'rb') as _clips_file:
    _clip_bytes = _clips_file.read()
_clip_document = json.loads(_clip_bytes)
STATE_CLIPS = _clip_document['states']
STATE_CLIP_INDEX = {}
TYPE_DECODE = {"384101": "L", "384103": "L", "384201": "P", "384203": "P",
               "384301": "T", "384303": "T", "384401": "M", "384403": "M"}
FIELDS = "OBJECTID,ADMIN_STATE,GEO_STATE,CSE_NR,CSE_NAME,CSE_TYPE_NR,CSE_DISP,RCRD_ACRS"
PAGE = 2000
MAX_PAGES = 400          # safety valve per state/layer (per chain segment)
CLOSED_CAP = int(os.environ.get("CLOSED_CAP", "0"))
if CLOSED_CAP < 0:
    raise ValueError("CLOSED_CAP must be zero (unlimited) or a positive integer")
CHAIN_MAX = int(os.environ.get("CHAIN_MAX", "40"))
TIME_RESERVE_MS = 150_000          # checkpoint + re-invoke headroom
LOCK_TTL = 3 * 3600                # a lock older than this is a dead chain
CHECKPOINT_SCHEMA = 3
CLIP_SHA256 = hashlib.sha256(_clip_bytes).hexdigest()
CLIP_METHOD = "claim-polygon centroid within authoritative state polygon"
PAGINATION_METHOD = "OBJECTID cursor to empty page for every query envelope"
CLIP_VERSION = f"state-centroid-{CLIP_SHA256[:16]}"
LEGACY_JSON_STATES = frozenset(s.strip().upper() for s in os.environ.get(
    "LEGACY_JSON_STATES", "").split(",") if s.strip())
s3 = boto3.client("s3")
lam = boto3.client("lambda")


def fetch(url, tries=6, ms_left=None):
    """Retries transport failures AND JSON-carried server errors — the NV
    partition throws {'error': {'code': 503, 'Wait timeout…'}} mid-stream
    under load; those are transient and must not kill the refresh."""
    last = None
    req = urllib.request.Request(url, headers={
        "User-Agent": "nw-mineral-monitor-updater/1.0 (AWS Lambda)",
        "Accept": "application/json"})
    attempts = 0
    for i in range(tries):
        attempts = i + 1
        if ms_left:
            usable_ms = ms_left() - TIME_RESERVE_MS
            if usable_ms <= 5_000:
                raise RuntimeError(f"BLM fetch stopped for Lambda time budget: {last}")
            timeout = max(5, min(45, int(usable_ms / 1000) - 2))
        else:
            timeout = 60
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                j = json.loads(r.read())
            if "error" in j:
                last = j["error"]
            else:
                return j
        except Exception as e:           # noqa: BLE001 — retry anything transient
            last = e
        if i + 1 >= tries:
            break
        delay = min(30, 3 * (i + 1))
        if ms_left and ms_left() - TIME_RESERVE_MS <= delay * 1000 + 5_000:
            break
        time.sleep(delay)
    raise RuntimeError(f"BLM fetch failed after {attempts} tries: {last}")


def closed_cap_reached(mode, count):
    """True only for an explicitly requested, non-release progress cap."""
    return mode == "closed" and CLOSED_CAP > 0 and count >= CLOSED_CAP


def centroid(rings):
    """Area-weighted centroid of the outer ring (shoelace); mean-vertex fallback."""
    ring = rings[0]
    a = cx = cy = 0.0
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i][:2], ring[i + 1][:2]
        cross = x1 * y2 - x2 * y1
        a += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a) < 1e-12:
        xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    a *= 0.5
    return cx / (6 * a), cy / (6 * a)


def point_in_state(state, x, y):
    """Indexed point-in-polygon against the official Census state clip."""
    if state not in STATE_CLIP_INDEX:
        STATE_CLIP_INDEX[state] = StateClipIndex(STATE_CLIPS[state])
    return STATE_CLIP_INDEX[state].contains(x, y)


def ckpt_key(state, mode):
    return f"ckpt/{state.lower()}_{mode}.json"


def _s3_error_code(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _is_missing_s3_error(exc):
    return _s3_error_code(exc) in {"NoSuchKey", "404", "NotFound"}


def ckpt_load(bucket, state, mode):
    try:
        ck = json.loads(s3.get_object(Bucket=bucket,
                                      Key=ckpt_key(state, mode))["Body"].read())
    except Exception as exc:             # noqa: BLE001 — botocore is not vendored
        if _is_missing_s3_error(exc):
            return None
        raise
    if (ck.get("_schema") != CHECKPOINT_SCHEMA or
            ck.get("clip_version") != CLIP_VERSION):
        print(f"{state} {mode}: discarding an incompatible pre-clip checkpoint")
        ckpt_clear(bucket, state, mode)
        return None
    required_columns = {"serial", "name", "type", "x", "y",
                        "admin_state", "geo_state"}
    if mode == "active":
        required_columns |= {"disp", "acres"}
    cols = ck.get("cols")
    serials = cols.get("serial") if isinstance(cols, dict) else None
    valid = (
        isinstance(ck.get("partition"), int) and
        not isinstance(ck.get("partition"), bool) and
        0 <= ck["partition"] < len(CLAIM_STATES[state]["query_envelopes"]) and
        (ck.get("cursor") is None or
         (isinstance(ck["cursor"], int) and not isinstance(ck["cursor"], bool) and
          ck["cursor"] >= 0)) and
        (ck.get("total") is None or
         (isinstance(ck["total"], int) and not isinstance(ck["total"], bool) and
          ck["total"] >= 0)) and
        isinstance(ck.get("pages_total"), int) and
        not isinstance(ck.get("pages_total"), bool) and ck["pages_total"] >= 0 and
        isinstance(cols, dict) and set(cols) == required_columns and
        isinstance(serials, list) and
        all(isinstance(serial, str) and serial for serial in serials) and
        len(serials) == len(set(serials)) and
        all(isinstance(values, list) and len(values) == len(serials)
            for values in cols.values()))
    if not valid:
        raise RuntimeError(f"malformed current checkpoint for {state} {mode}")
    return ck


def ckpt_save(bucket, state, mode, ck):
    ck = {"_schema": CHECKPOINT_SCHEMA, "clip_version": CLIP_VERSION, **ck}
    s3.put_object(Bucket=bucket, Key=ckpt_key(state, mode),
                  Body=json.dumps(ck, separators=(",", ":")).encode(),
                  ContentType="application/json")


def ckpt_clear(bucket, state, mode):
    s3.delete_object(Bucket=bucket, Key=ckpt_key(state, mode))


# ---- per-state run locks -------------------------------------------------
# Multiple roots can fire the updater for overlapping states (nightly rules,
# deploy.sh's refresh, manual invokes, Lambda's async auto-retry) — without a
# lock their chains stomp one shared checkpoint AND double the BLM load,
# which is exactly what makes BLM start timing out. One owner per state×mode.
def lock_key(state, mode):
    return f"ckpt/lock_{state.lower()}_{mode}.json"


def lock_read(bucket, state, mode):
    try:
        response = s3.get_object(Bucket=bucket, Key=lock_key(state, mode))
        lock = json.loads(response["Body"].read())
        lock["_etag"] = response.get("ETag")
        if not lock["_etag"]:
            raise RuntimeError(f"S3 omitted ETag for {state} {mode} lock")
        return lock
    except Exception as exc:             # noqa: BLE001 — botocore is not vendored
        if _is_missing_s3_error(exc):
            return None
        raise


def lock_acquire(bucket, state, mode, run_id):
    """Atomically create a lock. S3 If-None-Match admits exactly one root."""
    try:
        s3.put_object(Bucket=bucket, Key=lock_key(state, mode),
                      Body=json.dumps({"run_id": run_id, "ts": time.time()}).encode(),
                      ContentType="application/json", IfNoneMatch="*")
        return True
    except Exception as exc:             # noqa: BLE001 — botocore is not vendored
        if _s3_error_code(exc) in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
            return False
        raise


def lock_refresh(bucket, state, mode, run_id):
    current = lock_read(bucket, state, mode)
    if not current or current.get("run_id") != run_id:
        return False
    try:
        s3.put_object(Bucket=bucket, Key=lock_key(state, mode),
                      Body=json.dumps({"run_id": run_id, "ts": time.time()}).encode(),
                      ContentType="application/json", IfMatch=current["_etag"])
        return True
    except Exception as exc:             # noqa: BLE001 — botocore is not vendored
        if _s3_error_code(exc) in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
            return False
        raise


def lock_release(bucket, state, mode, run_id):
    current = lock_read(bucket, state, mode)
    if not current or current.get("run_id") != run_id:
        return False
    try:
        s3.delete_object(Bucket=bucket, Key=lock_key(state, mode),
                         IfMatch=current["_etag"])
        return True
    except Exception as exc:             # noqa: BLE001 — botocore is not vendored
        if _s3_error_code(exc) in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
            return False
        raise


def claim_states(bucket, states, mode, run_id, chain):
    """Return the subset of states this run may work on, asserting locks."""
    mine = []
    for st in states:
        if st not in CLAIM_STATES:
            raise ValueError(f"{st} is not a registry claim state")
        lk = lock_read(bucket, st, mode)
        if lk and lk.get("run_id") == run_id:
            if lock_refresh(bucket, st, mode, run_id):
                mine.append(st)
            continue
        if lk and time.time() - float(lk.get("ts", 0)) < LOCK_TTL:
            owner = str(lk.get("run_id", "unknown"))
            action = "skipping" if chain == 0 else "standing down"
            print(f"{st} {mode}: another run owns it (run {owner[:8]}…) — {action}")
            continue
        if lk:
            # Reclaim a dead chain, then contend through a conditional create.
            lock_release(bucket, st, mode, lk.get("run_id"))
        if lock_acquire(bucket, st, mode, run_id):
            mine.append(st)
        else:
            print(f"{st} {mode}: lost the atomic lock race — skipping")
    return mine


def pull_state(state, mode, bucket, ms_left):
    """Resumable pull. Returns (data, done). done=False means we ran low on
    time: progress is checkpointed to S3 and the caller should re-invoke."""
    envelopes = CLAIM_STATES[state]["query_envelopes"]
    # Spatial clipping is the authoritative state attribution. Case serial
    # prefixes/ADMIN_STATE identify the administering office and eastern
    # records may all use ES; neither is a reliable geographic-state filter.
    desc = (mode == "closed")            # closed: newest-first; cap is opt-in only
    order = "OBJECTID+DESC" if desc else "OBJECTID"
    ck = ckpt_load(bucket, state, mode)
    if ck:
        cursor, total, cols = ck["cursor"], ck.get("total"), ck["cols"]
        partition = ck.get("partition", 0)
        pages_total = ck.get("pages_total", 0)
        for field in ("admin_state", "geo_state"):
            cols.setdefault(field, [None] * len(cols["serial"]))
        seen = set(cols["serial"])
        print(f"{state} {mode}: resuming checkpoint at cursor {cursor}, "
              f"{len(seen):,} rows so far")
    else:
        cursor, partition = None, 0
        pages_total = 0
        cols = {"serial": [], "name": [], "type": [], "x": [], "y": [],
                "admin_state": [], "geo_state": []}
        if mode == "active":
            cols["disp"] = []; cols["acres"] = []
        seen = set()
        # Envelope counts are neither needed for paging nor exact after the
        # authoritative state clip. Avoid spending retry/time budget on them.
        total = None

    pages = 0
    while partition < len(envelopes) and pages < MAX_PAGES:
        xmin, ymin, xmax, ymax = envelopes[partition]
        geometry = json.dumps({"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                               "spatialReference": {"wkid": 4326}})
        base = (f"{EP}/{LAYER[mode]}/query?geometryType=esriGeometryEnvelope"
                f"&geometry={urllib.parse.quote(geometry)}"
                f"&inSR=4326&spatialRel=esriSpatialRelIntersects"
                f"&outFields={FIELDS}&returnGeometry=true&outSR=4326&geometryPrecision=5"
                f"&orderByFields={order}&resultRecordCount={PAGE}&f=json")
        if ms_left() < TIME_RESERVE_MS:
            ckpt_save(bucket, state, mode,
                      {"partition": partition, "cursor": cursor,
                       "pages_total": pages_total,
                       "total": total, "cols": cols})
            print(f"{state} {mode}: out of time at {len(cols['serial']):,} rows — "
                  f"checkpointed, continuing in next invocation")
            return None, False
        if desc:
            where = "1=1" if cursor is None else f"OBJECTID<{cursor}"
        else:
            where = f"OBJECTID>{cursor or 0}"
        try:
            j = fetch(base + "&where=" + urllib.parse.quote(where), ms_left=ms_left)
        except RuntimeError as e:
            # BLM stalled past all retries — that's a checkpoint, not a crash.
            # Raising here used to trigger Lambda's async auto-retry, which
            # spawned DUPLICATE chains over the same states.
            ckpt_save(bucket, state, mode,
                      {"partition": partition, "cursor": cursor,
                       "pages_total": pages_total,
                       "total": total, "cols": cols})
            print(f"{state} {mode}: BLM stalled at {len(cols['serial']):,} rows ({e}) — "
                  f"checkpointed, continuing in next invocation")
            return None, False
        if "error" in j:
            raise RuntimeError(f"BLM error {state}/{mode}: {j['error']}")
        feats = j.get("features", [])
        if not feats:
            partition += 1
            cursor = None
            continue
        pages += 1
        pages_total += 1
        for f in feats:
            at = f["attributes"]
            oid = at["OBJECTID"]
            cursor = oid if cursor is None else (min(cursor, oid) if desc else max(cursor, oid))
            ser = at.get("CSE_NR")
            if not ser or ser in seen:
                continue
            rings = (f.get("geometry") or {}).get("rings")
            if not rings:
                continue
            x, y = centroid(rings)
            if not point_in_state(state, x, y):
                continue
            seen.add(ser)
            cols["serial"].append(ser)
            cols["name"].append(at.get("CSE_NAME"))
            cols["type"].append(TYPE_DECODE.get(str(at.get("CSE_TYPE_NR")), "?"))
            cols["x"].append(round(x, 5)); cols["y"].append(round(y, 5))
            cols["admin_state"].append(at.get("ADMIN_STATE"))
            cols["geo_state"].append(at.get("GEO_STATE"))
            if mode == "active":
                cols["disp"].append(at.get("CSE_DISP"))
                cols["acres"].append(at.get("RCRD_ACRS"))
            if closed_cap_reached(mode, len(cols["serial"])):
                break
        if closed_cap_reached(mode, len(cols["serial"])):
            break
    if partition < len(envelopes) and not closed_cap_reached(mode, len(cols["serial"])):
        ckpt_save(bucket, state, mode,
                  {"partition": partition, "cursor": cursor,
                   "pages_total": pages_total,
                   "total": total, "cols": cols})
        print(f"{state} {mode}: page budget reached at {len(cols['serial']):,} rows — "
              "checkpointed for continuation")
        return None, False
    today = time.strftime("%Y-%m-%d")
    out = {"state": state, "layer": mode, "retrieved": today,
           "n": len(cols["serial"]), "source": EP,
           "spatial_clip": {
               "method": CLIP_METHOD,
               "artifact_sha256": CLIP_SHA256,
               "version": CLIP_VERSION,
           },
           "pagination": {
               "schema_version": 1,
               "method": PAGINATION_METHOD,
               "order": "OBJECTID DESC" if desc else "OBJECTID ASC",
               "page_size": PAGE,
               "pages": pages_total,
               "envelopes": len(envelopes),
               "completed_envelopes": partition,
               "terminal_empty_pages": partition,
               "complete": (partition == len(envelopes) and
                            not closed_cap_reached(mode, len(cols["serial"]))),
           }, **cols}
    if desc:
        out["truncated"] = closed_cap_reached(mode, out["n"])
    if desc and total is not None:
        # Retain this only for compatible in-flight checkpoints created by an
        # earlier build. It is an envelope upper bound, never a state count.
        out["envelope_total_upper_bound"] = total
    return out, True


def stamp_manifest(bucket, mode, results):
    try:
        man = json.loads(s3.get_object(Bucket=bucket, Key="data/manifest.json")["Body"].read())
        today = time.strftime("%Y-%m-%d")
        for st, n in results.items():
            k = f"{st.lower()}_{mode}"
            man.setdefault("claims", {}).setdefault(k, {})
            man["claims"][k].update({"n": n, "retrieved": today,
                                     "file": f"data/claims/{st.lower()}_{mode}.json"})
        man["claims_updated"] = today
        man["totals"]["claims_" + mode] = sum(
            v["n"] for k, v in man["claims"].items() if k.endswith("_" + mode))
        s3.put_object(Bucket=bucket, Key="data/manifest.json",
                      Body=json.dumps(man, separators=(",", ":")).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=300")
    except Exception as e:              # noqa: BLE001 — manifest stamp is best-effort
        print("manifest update skipped:", e)


def handler(event, context):
    event = event or {}
    mode = event.get("mode", "active")
    if mode not in LAYER:
        raise ValueError(f"unsupported mode {mode!r}; expected one of {sorted(LAYER)}")
    chain = int(event.get("chain", 0))
    run_id = event.get("run_id") or (context.aws_request_id if context
                                     else f"local-{time.time():.0f}")
    bucket = os.environ["BUCKET"]
    ms_left = (context.get_remaining_time_in_millis if context
               else (lambda: 900_000))
    requested = event.get("states", sorted(CLAIM_STATES))
    if not isinstance(requested, list):
        raise ValueError("states must be a list of two-letter registry codes")
    requested = list(dict.fromkeys(str(st).upper() for st in requested))
    states = claim_states(bucket, requested, mode, run_id, chain)
    if not states:
        print(f"nothing to do — all requested states owned by other runs")
        return {"mode": mode, "counts": {}, "chain": chain, "skipped": True}
    results = {}
    try:
        while states:
            st = states[0]
            data, done = pull_state(st, mode, bucket, ms_left)
            if not done:
                if chain + 1 > CHAIN_MAX:
                    raise RuntimeError(f"chain limit {CHAIN_MAX} hit on {st} {mode} — "
                                       f"BLM slower than ever seen; raise CHAIN_MAX")
                for s2 in states:                       # keep our locks fresh
                    if not lock_refresh(bucket, s2, mode, run_id):
                        raise RuntimeError(f"lost {s2} {mode} lock before continuation")
                nxt = {"mode": mode, "states": states, "chain": chain + 1,
                       "run_id": run_id}
                function_name = (os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or
                                 (context.function_name if context else None))
                if not function_name:
                    raise RuntimeError("AWS_LAMBDA_FUNCTION_NAME is required for continuation")
                lam.invoke(FunctionName=function_name, InvocationType="Event",
                           Payload=json.dumps(nxt).encode())
                if results:
                    stamp_manifest(bucket, mode, results)
                print(f"continuing as chain {chain + 1}: {states}")
                return {"mode": mode, "continued": True, "chain": chain + 1,
                        "completed": results, "remaining": states}
            # Raw national snapshots are private staging inputs for the PMTiles
            # builder. LEGACY_JSON_STATES has an empty production default and
            # exists only as an explicit emergency rollback switch.
            key = (f"data/claims/{st.lower()}_{mode}.json" if st in LEGACY_JSON_STATES
                   else f"staging/claims/{st.lower()}_{mode}.json")
            s3.put_object(Bucket=bucket, Key=key,
                          Body=json.dumps(data, separators=(",", ":")).encode(),
                          ContentType="application/json",
                          CacheControl=("public, max-age=900" if st in LEGACY_JSON_STATES
                                        else "private, no-store"))
            # Checkpoint deletion follows the durable snapshot write. A failed
            # write therefore remains resumable; a failed delete retries safely.
            ckpt_clear(bucket, st, mode)
            if st in LEGACY_JSON_STATES:
                results[st] = data["n"]
            print(f"{st} {mode}: {data['n']} claims -> s3://{bucket}/{key}")
            if not lock_release(bucket, st, mode, run_id):
                raise RuntimeError(f"lost ownership while releasing {st} {mode}")
            states.pop(0)
    except Exception:
        # Leave checkpoints intact, but release every lock this invocation
        # still owns so an async retry with a new request ID can resume.
        for owned in states:
            try:
                lock_release(bucket, owned, mode, run_id)
            except Exception as cleanup_error:       # noqa: BLE001
                print(f"failed to release {owned} {mode} after error: {cleanup_error}")
        raise
    if results:
        stamp_manifest(bucket, mode, results)
    return {"mode": mode, "counts": results, "chain": chain, "run_id": run_id}
