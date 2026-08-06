# RUNBOOK — deploy & operate NW Mineral Monitor (WS1–WS4 edition)

Everything below assumes the repo checked out on your Mac and the AWS CLI
configured (`aws sts get-caller-identity` works). Region: us-west-2.

## 1. One-shot deploy / update

```bash
cd nw-mineral-monitor/infra
bash deploy.sh
```

This creates/updates the CloudFormation stack (now 35 resources: site,
CloudFront, Cognito, claims updater, AI relay, **expiration watch**),
uploads all three Lambda code bundles, syncs `site/` (including the new
`data/plss`, `data/openground`, `data/dossiers`, `data/history`,
`data/alerts`, `data/userlayers`), sets the login password, runs a claims
refresh, and invalidates CloudFront. Footer must read **build 2026-08-05a**.

An unchanged template prints "No changes to deploy" and keeps going — that's
normal (fixed 2026-08).

## 2. Enable the alert email (SES) — one-time

SES accounts start in sandbox: both sender and recipient must be verified.

```bash
aws sesv2 create-email-identity --email-address you@example.com --region us-west-2
# click the verification link AWS emails you, then (sandbox) verify the
# recipient the same way if it's a different address
```

Then pass the addresses into the stack:

```bash
aws cloudformation deploy --template-file template.yaml \
  --stack-name nw-mineral-monitor --region us-west-2 \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides AlertEmailFrom=you@example.com AlertEmailTo=you@example.com
bash deploy.sh   # re-uploads lambda code after the parameter change
```

Optional webhook (Slack/Discord/ntfy relay, anything that takes a JSON POST):
add `AlertWebhookUrl=https://...` to the same `--parameter-overrides`.

To watch a different area: `WatchBbox="x0,y0,x1,y1"` in the overrides
(default is Cassia County).

## 3. What runs when (all UTC)

| job | schedule | what |
|---|---|---|
| claims updater (5 NW states) | daily 09:10 | active-claim snapshots |
| claims updater (NV+UT) | daily 09:40 | active-claim snapshots |
| closed-claims refresh | monthly, 1st | three batches 10:00–11:00 |
| **expiration watch — daily** | daily 13:10 | AOI disposition diff → alerts |
| **expiration watch — fee window** | **every 6 h, Aug 25 – Sep 10** | diff + LIKELY-LAPSED scan |

Manual runs:

```bash
bash deploy.sh watch            # daily-mode diff right now
bash deploy.sh watch seasonal   # fee-window scan right now
bash deploy.sh refresh          # claims snapshot refresh
```

First watch run seeds the baseline; transitions alert from the second run.
Alerts also land on the map: the **WATCH** button in the header (badge shows
the count), each alert deep-links `#claim=SERIAL`.

## 4. The LIKELY-LAPSED fee scan (manual 2-minute step each August)

The public GIS layers carry no fee-payment actions, so once a year, when the
fee window opens (~Aug 25):

1. Open https://reports.blm.gov → Mining Claims reports → run the fee/case
   report for ID (or your AOI state), export CSV.
2. `aws s3 cp <export>.csv s3://<bucket>/watch/fee_status.csv`
3. Done — the 6-hourly seasonal runs now flag active claims with no
   current-year fee action as "LIKELY LAPSED — verify", with the
   lead-not-conclusion caveat in every alert. Without the file the digest
   says fee data was unavailable (it never fakes it).

Bucket name: `aws cloudformation describe-stacks --stack-name nw-mineral-monitor \
--query 'Stacks[0].Outputs' --output table`

## 5. Refreshing the AOI research bundles (pipelines/)

Idempotent, cached (pipelines/cache/), safe to re-run any time:

```bash
cd nw-mineral-monitor/pipelines
python3 fetch_plss.py          # PLSS sections (cache 90 d)
python3 fetch_claims_aoi.py    # AOI claims w/ legal descriptions (active cache 1 d)
python3 fetch_landstatus.py    # SMA + withdrawals + segregations (cache 30-90 d)
python3 open_ground.py         # recompute section statuses
python3 webscrub.py            # ChronAm/GBooks/MSHA sweep (cache 30-60 d)
python3 dossier.py             # rebuild mine dossiers (fold in new history)
python3 inbox_ingest.py        # convert anything dropped in data-inbox/
bash ../infra/deploy.sh update-site
```

Google Books often rate-limits shared IPs; re-running from home fills the
gaps (cached, so only misses re-fetch). New AOI: add a block to
`config/aoi.json`, then `AOI=<key> python3 <each script>`.

## 6. Demo

`DEMO.md` walks each workstream end-to-end on Cassia County, including the
messy-CSV acceptance test (`demo/messy_cassia.csv`).

## 7. Troubleshooting

- **Stack says UPDATE_ROLLBACK_COMPLETE** — rerun `bash deploy.sh`; if it
  persists, check the CloudFormation events tab for the failing resource.
- **No alert email** — SES identity unverified, or params empty. The digest
  still writes to `data/alerts/latest.json` (WATCH button) regardless.
- **Watch found 0 active cases** — check `WatchBbox` parameter; the Lambda
  logs (`/aws/lambda/nw-mineral-monitor-expiration-watch`) print counts.
- **Ingest says "no usable location"** — the row needs lat/lon, UTM
  easting+northing, or a TRS legal ("T12S R22E Sec 14"). Outside Idaho's
  cached sections the browser queries CadNSDI live; offline it reports the
  row unmatched.
- **git push HTTP 400** — `git config http.postBuffer 524288000` then retry.
