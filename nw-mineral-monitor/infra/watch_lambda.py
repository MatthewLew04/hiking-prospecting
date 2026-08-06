"""NW Mineral Monitor — WS2d expiration watch.

Diffs MLRS mining-claim dispositions for the AOI against the previous
snapshot stored in S3, alerts on ACTIVE→CLOSED transitions (and new FILED
locations, which prospectors also want to see), and — during the Sept-1
maintenance-fee window — flags likely lapses.

Honesty constraints, by design:
- The public GIS layers carry NO fee-payment actions. True fee status lives
  in the MLRS serial register / LR2000-successor reports behind an
  interactive app. So "LIKELY LAPSED" is only emitted when a fee-status CSV
  (exported manually from reports.blm.gov and uploaded to
  s3://bucket/watch/fee_status.csv) is present and lists no
  current-assessment-year fee action for an active claim. Without that file
  the seasonal run says plainly that fee data was unavailable.
- Every alert says: BLM adjudication lags; treat as lead, not conclusion.

Event: {"mode": "daily" | "seasonal"}   (seasonal adds the lapse scan)
Env:   BUCKET (site bucket), AOI_STATES ("ID"), AOI_BBOX ("x0,y0,x1,y1"),
       SES_FROM, SES_TO (comma list; empty = skip email),
       WEBHOOK_URL (empty = skip), SITE_URL (for deep links)
Writes: s3://BUCKET/watch/state_{st}.json      (snapshot for next diff)
        s3://BUCKET/data/alerts/latest.json    (map consumes this)
"""
import csv, io, json, os, time, urllib.request, urllib.parse
import boto3

EP = "https://gis.blm.gov/nlsdb/rest/services/Mining_Claims/MiningClaims/MapServer"
TYPE_DECODE = {"384101": "L", "384103": "L", "384201": "P", "384203": "P",
               "384301": "T", "384303": "T", "384401": "M", "384403": "M"}
s3 = boto3.client("s3")


def fetch(url, tries=4):
    req = urllib.request.Request(url, headers={
        "User-Agent": "nw-mineral-monitor-watch/1.0 (AWS Lambda)",
        "Accept": "application/json"})
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception as e:                    # noqa: BLE001
            last = e; time.sleep(2 * (i + 1))
    raise RuntimeError(f"BLM fetch failed: {last}")


def pull_cases(layer, bbox):
    geometry = json.dumps({"xmin": bbox[0], "ymin": bbox[1], "xmax": bbox[2],
                           "ymax": bbox[3], "spatialReference": {"wkid": 4326}})
    base = (f"{EP}/{layer}/query?geometryType=esriGeometryEnvelope"
            f"&geometry={urllib.parse.quote(geometry)}&inSR=4326"
            f"&spatialRel=esriSpatialRelIntersects"
            f"&outFields=OBJECTID,CSE_NR,CSE_NAME,CSE_TYPE_NR,CSE_DISP,CSE_META,RCRD_ACRS"
            f"&returnGeometry=true&outSR=4326&geometryPrecision=5"
            f"&orderByFields=OBJECTID&resultRecordCount=2000&f=json")
    cursor, out = 0, {}
    for _ in range(200):
        j = fetch(base + "&where=" + urllib.parse.quote(f"OBJECTID>{cursor}"))
        feats = j.get("features", [])
        if not feats: break
        for f in feats:
            at = f["attributes"]
            cursor = max(cursor, at["OBJECTID"])
            ser = at.get("CSE_NR")
            if not ser: continue
            rings = (f.get("geometry") or {}).get("rings")
            xy = None
            if rings:
                ring = rings[0]
                xy = (round(sum(p[0] for p in ring) / len(ring), 5),
                      round(sum(p[1] for p in ring) / len(ring), 5))
            # TRS straight from the legal description
            trs = []
            meta = at.get("CSE_META") or ""
            for part in meta.split("|")[:4]:
                seg = part.strip().split(" ")
                if len(seg) >= 6:
                    trs.append(f"T{seg[2].lstrip('0')} R{seg[3].lstrip('0')} Sec {seg[4].lstrip('0') or '0'}")
            out[ser] = {"name": at.get("CSE_NAME"), "disp": at.get("CSE_DISP"),
                        "type": TYPE_DECODE.get(str(at.get("CSE_TYPE_NR")), "?"),
                        "acres": at.get("RCRD_ACRS"), "trs": sorted(set(trs))[:3],
                        "xy": xy}
    return out


def s3_json(bucket, key, default=None):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:                             # noqa: BLE001
        return default


def put_json(bucket, key, obj, cache="public, max-age=300"):
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(obj, separators=(",", ":")).encode(),
                  ContentType="application/json", CacheControl=cache)


def fee_status_index(bucket):
    """Optional operator-supplied MLRS fee report (see module docstring)."""
    try:
        raw = s3.get_object(Bucket=bucket, Key="watch/fee_status.csv")["Body"].read()
    except Exception:                             # noqa: BLE001
        return None
    idx = {}
    rdr = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", "replace")))
    cols = {c.lower().strip(): c for c in rdr.fieldnames or []}
    ser_c = next((cols[c] for c in cols if "serial" in c or c in ("cse_nr", "case number")), None)
    fee_c = next((cols[c] for c in cols if "fee" in c or "assessment" in c or "maintain" in c), None)
    if not ser_c: return None
    for row in rdr:
        ser = (row.get(ser_c) or "").replace(" ", "")
        if ser: idx[ser] = (row.get(fee_c) or "").strip() if fee_c else ""
    return idx


def handler(event, context):
    event = event or {}
    mode = event.get("mode", "daily")
    bucket = os.environ["BUCKET"]
    site = os.environ.get("SITE_URL", "").rstrip("/")
    bbox = [float(v) for v in os.environ.get(
        "AOI_BBOX", "-114.2867,41.9882,-113.0,42.6878").split(",")]
    today = time.strftime("%Y-%m-%d %H:%M UTC")

    cur_active = pull_cases(1, bbox)
    prev = s3_json(bucket, "watch/state_active.json", {})
    alerts = []

    for ser, was in prev.items():
        now = cur_active.get(ser)
        if now is None or (now["disp"] or "").lower() == "closed":
            alerts.append({
                "kind": "ACTIVE→CLOSED",
                "ser": ser, "name": was.get("name"), "type": was.get("type"),
                "trs": was.get("trs"), "xy": was.get("xy"),
                "evidence": f"present as '{was.get('disp')}' in prior snapshot; "
                            f"absent from MLRS active layer {today}",
                "note": "BLM adjudication lags reality — verify the serial register "
                        "before treating this ground as open.",
                "link": (f"{site}/#claim={ser}" if site else None),
                "srp": "https://reports.blm.gov/report/MLRS/95/Serial-Register-Page"})
    for ser, now in cur_active.items():
        if ser not in prev and (now["disp"] or "").upper() == "FILED":
            alerts.append({"kind": "NEW FILING", "ser": ser, "name": now.get("name"),
                           "type": now.get("type"), "trs": now.get("trs"), "xy": now.get("xy"),
                           "evidence": f"new FILED case in MLRS active layer {today}",
                           "link": (f"{site}/#claim={ser}" if site else None)})

    lapse_note = None
    if mode == "seasonal":
        fees = fee_status_index(bucket)
        if fees is None:
            lapse_note = ("Maintenance-fee window scan ran WITHOUT fee data: the public "
                          "GIS layers carry no fee actions and no fee_status.csv was "
                          "uploaded to watch/. Export the fee report from "
                          "reports.blm.gov and upload it to enable LIKELY-LAPSED flags.")
        else:
            yr = time.strftime("%Y")
            for ser, now in cur_active.items():
                fee = fees.get(ser)
                if fee is not None and yr not in fee:
                    alerts.append({
                        "kind": "LIKELY LAPSED — verify",
                        "ser": ser, "name": now.get("name"), "trs": now.get("trs"),
                        "xy": now.get("xy"),
                        "evidence": f"no {yr} fee action in operator-supplied MLRS fee "
                                    f"report (field value: '{fee or 'blank'}')",
                        "note": "Sept 1 maintenance-fee deadline; nonpayment forfeits by "
                                "operation of law, but BLM adjudication lags — this is a "
                                "LEAD, not a conclusion. Verify the serial register.",
                        "link": (f"{site}/#claim={ser}" if site else None)})

    put_json(bucket, "watch/state_active.json",
             cur_active, cache="no-store")
    digest = {"generated": today, "mode": mode, "aoi_bbox": bbox,
              "active_now": len(cur_active), "alerts": alerts,
              "lapse_note": lapse_note,
              "disclaimer": "Research leads only. Verify at BLM and the county "
                            "recorder before staking."}
    put_json(bucket, "data/alerts/latest.json", digest)

    # ---- notify ----
    sent = []
    if alerts or lapse_note:
        frm, to = os.environ.get("SES_FROM", ""), os.environ.get("SES_TO", "")
        if frm and to:
            lines = [f"NW Mineral Monitor — {len(alerts)} alert(s), {today}", ""]
            for a in alerts[:60]:
                lines += [f"[{a['kind']}] {a.get('name') or '(unnamed)'} — {a['ser']}",
                          f"  TRS: {'; '.join(a.get('trs') or []) or 'see map'}",
                          f"  {a.get('evidence', '')}",
                          f"  {a.get('link') or ''}", ""]
            if lapse_note: lines += ["", lapse_note]
            lines += ["", digest["disclaimer"]]
            try:
                boto3.client("ses").send_email(
                    Source=frm,
                    Destination={"ToAddresses": [t.strip() for t in to.split(",") if t.strip()]},
                    Message={"Subject": {"Data": f"[NW-MM] {len(alerts)} claim alert(s) — {mode}"},
                             "Body": {"Text": {"Data": "\n".join(lines)}}})
                sent.append("ses")
            except Exception as e:                # noqa: BLE001
                print("SES send failed:", e)
        hook = os.environ.get("WEBHOOK_URL", "")
        if hook:
            try:
                req = urllib.request.Request(hook, data=json.dumps(digest).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=20).read()
                sent.append("webhook")
            except Exception as e:                # noqa: BLE001
                print("webhook failed:", e)
    print(f"{mode}: {len(cur_active)} active, {len(alerts)} alerts, notified via {sent or 'nobody'}")
    return {"mode": mode, "active": len(cur_active), "alerts": len(alerts), "notified": sent}
