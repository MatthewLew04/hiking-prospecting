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
}
TYPE_DECODE = {"384101": "L", "384103": "L", "384201": "P", "384203": "P",
               "384301": "T", "384303": "T", "384401": "M", "384403": "M"}
FIELDS = "OBJECTID,CSE_NR,CSE_NAME,CSE_TYPE_NR,CSE_DISP,RCRD_ACRS"
PAGE = 2000
MAX_PAGES = 400          # safety valve per state/layer
s3 = boto3.client("s3")


def fetch(url, tries=4):
    last = None
    req = urllib.request.Request(url, headers={
        "User-Agent": "nw-mineral-monitor-updater/1.0 (AWS Lambda)",
        "Accept": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:           # noqa: BLE001 — retry anything transient
            last = e
            time.sleep(2 * (i + 1))
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


def pull_state(state, mode):
    xmin, ymin, xmax, ymax = BBOX[state]
    geometry = json.dumps({"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                           "spatialReference": {"wkid": 4326}})
    base = (f"{EP}/{LAYER[mode]}/query?geometryType=esriGeometryEnvelope"
            f"&geometry={urllib.parse.quote(geometry)}"
            f"&inSR=4326&spatialRel=esriSpatialRelIntersects"
            f"&outFields={FIELDS}&returnGeometry=true&outSR=4326&geometryPrecision=5"
            f"&orderByFields=OBJECTID&resultRecordCount={PAGE}&f=json")
    cursor, seen, cols = 0, set(), {"serial": [], "name": [], "type": [], "x": [], "y": []}
    if mode == "active":
        cols["disp"] = []; cols["acres"] = []
    for _ in range(MAX_PAGES):
        j = fetch(base + "&where=" + urllib.parse.quote(f"OBJECTID>{cursor}"))
        if "error" in j:
            raise RuntimeError(f"BLM error {state}/{mode}: {j['error']}")
        feats = j.get("features", [])
        if not feats:
            break
        for f in feats:
            at = f["attributes"]
            cursor = max(cursor, at["OBJECTID"])
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
    today = time.strftime("%Y-%m-%d")
    return {"state": state, "layer": mode, "retrieved": today,
            "n": len(cols["serial"]), **cols}


def handler(event, context):
    event = event or {}
    mode = event.get("mode", "active")
    states = event.get("states", list(BBOX))
    bucket = os.environ["BUCKET"]
    results = {}
    for st in states:
        data = pull_state(st, mode)
        key = f"data/claims/{st.lower()}_{mode}.json"
        s3.put_object(Bucket=bucket, Key=key,
                      Body=json.dumps(data, separators=(",", ":")).encode(),
                      ContentType="application/json",
                      CacheControl="public, max-age=900")
        results[st] = data["n"]
        print(f"{st} {mode}: {data['n']} claims -> s3://{bucket}/{key}")
    # stamp the manifest
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
    return {"mode": mode, "counts": results}
