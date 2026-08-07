"""NW Mineral Monitor — nightly claims updater.

Pulls current mining-claim records from BLM MLRS GIS for WA/OR/ID/MT/WY,
rewrites the columnar snapshot files in S3, and stamps the manifest.

Runs on AWS Lambda (python3.12, stdlib only — no layers needed).
Event: {"mode": "active" | "closed", "states": ["WA","OR","ID","MT","WY"]}
       (both keys optional; defaults: mode=active, all five states)

Pagination per BLM server quirks (verified 2026-07-30):
- use an OBJECTID cursor (where=OBJECTID>n), NOT resultOffset
- short pages with exceededTransferLimit=true are normal — stop only on an
  empty page
- query with a state BBOX ENVELOPE, not a detailed polygon (polygons exhaust
  the server's per-request budget and cause mid-stream empty pages)

Closed layers are pulled NEWEST-FIRST (OBJECTID DESC, where OBJECTID<cursor)
and capped at CLOSED_CAP (default 250,000) per state — NV alone has 1.23M
closed cases, which would blow the site file budget.

CHECKPOINT + SELF-CONTINUATION (added 2026-08-06 — the crash fix):
Lambda's hard ceiling is 15 minutes, but a 250k-record closed pull takes
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
import json, os, time, urllib.request, urllib.parse, boto3

EP = "https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer"
LAYER = {"active": 1, "closed": 2}
# generous envelopes; border-straddling claims are expected and kept
BBOX = {
    "WA": (-124.90, 45.45, -116.80, 49.10),
    "OR": (-124.75, 41.85, -116.35, 46.40),
    "ID": (-117.35, 41.85, -110.95, 49.10),
    "MT": (-116.15, 44.25, -103.95, 49.10),
    "WY": (-111.15, 40.90, -103.95, 45.10),
    "NV": (-120.01, 35.00, -114.03, 42.01),
    "UT": (-114.06, 36.99, -109.06, 42.01),
    "CA": (-124.48, 32.45, -114.05, 42.05),
}
TYPE_DECODE = {"384101": "L", "384103": "L", "384201": "P", "384203": "P",
               "384301": "T", "384303": "T", "384401": "M", "384403": "M"}
FIELDS = "OBJECTID,CSE_NR,CSE_NAME,CSE_TYPE_NR,CSE_DISP,RCRD_ACRS"
PAGE = 2000
MAX_PAGES = 400          # safety valve per state/layer (per chain segment)
CLOSED_CAP = int(os.environ.get("CLOSED_CAP", "250000"))
CHAIN_MAX = int(os.environ.get("CHAIN_MAX", "40"))
TIME_RESERVE_MS = 150_000          # checkpoint + re-invoke headroom
LOCK_TTL = 3 * 3600                # a lock older than this is a dead chain
s3 = boto3.client("s3")
lam = boto3.client("lambda")


def fetch(url, tries=6):
    """Retries transport failures AND JSON-carried server errors — the NV
    partition throws {'error': {'code': 503, 'Wait timeout…'}} mid-stream
    under load; those are transient and must not kill the refresh."""
    last = None
    req = urllib.request.Request(url, headers={
        "User-Agent": "nw-mineral-monitor-updater/1.0 (AWS Lambda)",
        "Accept": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.loads(r.read())
            if "error" in j:
                last = j["error"]
                time.sleep(min(30, 3 * (i + 1)))
                continue
            return j
        except Exception as e:           # noqa: BLE001 — retry anything transient
            last = e
            time.sleep(min(30, 3 * (i + 1)))
    raise RuntimeError(f"BLM fetch failed after {tries} tries: {last}")


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


def ckpt_key(state, mode):
    return f"ckpt/{state.lower()}_{mode}.json"


def ckpt_load(bucket, state, mode):
    try:
        return json.loads(s3.get_object(Bucket=bucket,
                                        Key=ckpt_key(state, mode))["Body"].read())
    except Exception:                    # noqa: BLE001 — no checkpoint = fresh start
        return None


def ckpt_save(bucket, state, mode, ck):
    s3.put_object(Bucket=bucket, Key=ckpt_key(state, mode),
                  Body=json.dumps(ck, separators=(",", ":")).encode(),
                  ContentType="application/json")


def ckpt_clear(bucket, state, mode):
    try:
        s3.delete_object(Bucket=bucket, Key=ckpt_key(state, mode))
    except Exception:                    # noqa: BLE001
        pass


# ---- per-state run locks -------------------------------------------------
# Multiple roots can fire the updater for overlapping states (nightly rules,
# deploy.sh's refresh, manual invokes, Lambda's async auto-retry) — without a
# lock their chains stomp one shared checkpoint AND double the BLM load,
# which is exactly what makes BLM start timing out. One owner per state×mode.
def lock_key(state, mode):
    return f"ckpt/lock_{state.lower()}_{mode}.json"


def lock_read(bucket, state, mode):
    try:
        return json.loads(s3.get_object(Bucket=bucket,
                                        Key=lock_key(state, mode))["Body"].read())
    except Exception:                    # noqa: BLE001
        return None


def lock_write(bucket, state, mode, run_id):
    s3.put_object(Bucket=bucket, Key=lock_key(state, mode),
                  Body=json.dumps({"run_id": run_id, "ts": time.time()}).encode(),
                  ContentType="application/json")


def lock_release(bucket, state, mode):
    try:
        s3.delete_object(Bucket=bucket, Key=lock_key(state, mode))
    except Exception:                    # noqa: BLE001
        pass


def claim_states(bucket, states, mode, run_id, chain):
    """Return the subset of states this run may work on, asserting locks."""
    mine = []
    for st in states:
        lk = lock_read(bucket, st, mode)
        if lk and lk.get("run_id") != run_id and time.time() - lk.get("ts", 0) < LOCK_TTL:
            if chain == 0:
                print(f"{st} {mode}: another run owns it (run {lk['run_id'][:8]}…) — skipping")
                continue
            # a continuation that lost its lock was superseded — stand down
            print(f"{st} {mode}: lock taken over by run {lk['run_id'][:8]}… — standing down")
            continue
        lock_write(bucket, st, mode, run_id)
        mine.append(st)
    return mine


def pull_state(state, mode, bucket, ms_left):
    """Resumable pull. Returns (data, done). done=False means we ran low on
    time: progress is checkpointed to S3 and the caller should re-invoke."""
    xmin, ymin, xmax, ymax = BBOX[state]
    geometry = json.dumps({"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                           "spatialReference": {"wkid": 4326}})
    desc = (mode == "closed")            # closed: newest-first with a cap
    order = "OBJECTID+DESC" if desc else "OBJECTID"
    base = (f"{EP}/{LAYER[mode]}/query?geometryType=esriGeometryEnvelope"
            f"&geometry={urllib.parse.quote(geometry)}"
            f"&inSR=4326&spatialRel=esriSpatialRelIntersects"
            f"&outFields={FIELDS}&returnGeometry=true&outSR=4326&geometryPrecision=5"
            f"&orderByFields={order}&resultRecordCount={PAGE}&f=json")

    ck = ckpt_load(bucket, state, mode)
    if ck:
        cursor, total, cols = ck["cursor"], ck.get("total"), ck["cols"]
        seen = set(cols["serial"])
        print(f"{state} {mode}: resuming checkpoint at cursor {cursor}, "
              f"{len(seen):,} rows so far")
    else:
        cursor, cols = None, {"serial": [], "name": [], "type": [], "x": [], "y": []}
        if mode == "active":
            cols["disp"] = []; cols["acres"] = []
        seen = set()
        total = None
        if desc:
            j = fetch(f"{EP}/{LAYER[mode]}/query?geometryType=esriGeometryEnvelope"
                      f"&geometry={urllib.parse.quote(geometry)}&inSR=4326"
                      f"&spatialRel=esriSpatialRelIntersects&where=1%3D1"
                      f"&returnCountOnly=true&f=json")
            total = j.get("count")

    for _ in range(MAX_PAGES):
        if ms_left() < TIME_RESERVE_MS:
            ckpt_save(bucket, state, mode,
                      {"cursor": cursor, "total": total, "cols": cols})
            print(f"{state} {mode}: out of time at {len(cols['serial']):,} rows — "
                  f"checkpointed, continuing in next invocation")
            return None, False
        if desc:
            where = "1=1" if cursor is None else f"OBJECTID<{cursor}"
        else:
            where = f"OBJECTID>{cursor or 0}"
        try:
            j = fetch(base + "&where=" + urllib.parse.quote(where))
        except RuntimeError as e:
            # BLM stalled past all retries — that's a checkpoint, not a crash.
            # Raising here used to trigger Lambda's async auto-retry, which
            # spawned DUPLICATE chains over the same states.
            ckpt_save(bucket, state, mode,
                      {"cursor": cursor, "total": total, "cols": cols})
            print(f"{state} {mode}: BLM stalled at {len(cols['serial']):,} rows ({e}) — "
                  f"checkpointed, continuing in next invocation")
            return None, False
        if "error" in j:
            raise RuntimeError(f"BLM error {state}/{mode}: {j['error']}")
        feats = j.get("features", [])
        if not feats:
            break
        for f in feats:
            at = f["attributes"]
            oid = at["OBJECTID"]
            cursor = oid if cursor is None else (min(cursor, oid) if desc else max(cursor, oid))
            ser = at.get("CSE_NR")
            if not ser or ser in seen:
                continue
            seen.add(ser)
            rings = (f.get("geometry") or {}).get("rings")
            if not rings:
                continue
            x, y = centroid(rings)
            cols["serial"].append(ser)
            cols["name"].append(at.get("CSE_NAME"))
            cols["type"].append(TYPE_DECODE.get(str(at.get("CSE_TYPE_NR")), "?"))
            cols["x"].append(round(x, 5)); cols["y"].append(round(y, 5))
            if mode == "active":
                cols["disp"].append(at.get("CSE_DISP"))
                cols["acres"].append(at.get("RCRD_ACRS"))
            if desc and len(cols["serial"]) >= CLOSED_CAP:
                break
        if desc and len(cols["serial"]) >= CLOSED_CAP:
            break
    today = time.strftime("%Y-%m-%d")
    out = {"state": state, "layer": mode, "retrieved": today,
           "n": len(cols["serial"]), **cols}
    if desc and total is not None:
        out["truncated"] = out["n"] < total
        out["total_available"] = total
    ckpt_clear(bucket, state, mode)
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
    chain = int(event.get("chain", 0))
    run_id = event.get("run_id") or (context.aws_request_id if context
                                     else f"local-{time.time():.0f}")
    bucket = os.environ["BUCKET"]
    ms_left = (context.get_remaining_time_in_millis if context
               else (lambda: 900_000))
    states = claim_states(bucket, list(event.get("states", list(BBOX))),
                          mode, run_id, chain)
    if not states:
        print(f"nothing to do — all requested states owned by other runs")
        return {"mode": mode, "counts": {}, "chain": chain, "skipped": True}
    results = {}
    while states:
        st = states[0]
        data, done = pull_state(st, mode, bucket, ms_left)
        if not done:
            if chain + 1 > CHAIN_MAX:
                for s2 in states:
                    lock_release(bucket, s2, mode)
                raise RuntimeError(f"chain limit {CHAIN_MAX} hit on {st} {mode} — "
                                   f"BLM slower than ever seen; raise CHAIN_MAX")
            for s2 in states:                       # keep our locks fresh
                lock_write(bucket, s2, mode, run_id)
            nxt = {"mode": mode, "states": states, "chain": chain + 1,
                   "run_id": run_id}
            lam.invoke(FunctionName=os.environ.get("AWS_LAMBDA_FUNCTION_NAME",
                                                   context.function_name),
                       InvocationType="Event",
                       Payload=json.dumps(nxt).encode())
            if results:
                stamp_manifest(bucket, mode, results)
            print(f"continuing as chain {chain + 1}: {states}")
            return {"mode": mode, "continued": True, "chain": chain + 1,
                    "completed": results, "remaining": states}
        key = f"data/claims/{st.lower()}_{mode}.json"
        s3.put_object(Bucket=bucket, Key=key,
                      Body=json.dumps(data, separators=(",", ":")).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=900")
        results[st] = data["n"]
        print(f"{st} {mode}: {data['n']} claims -> s3://{bucket}/{key}")
        states.pop(0)
        lock_release(bucket, st, mode)
    if results:
        stamp_manifest(bucket, mode, results)
    return {"mode": mode, "counts": results, "chain": chain, "run_id": run_id}
