# Hosting the NW Mineral Monitor on AWS — step by step

You'll end up with: your map at a public `https://` URL, served from S3 through CloudFront (fast + cheap), with a Lambda job that re-pulls **active mining claims from BLM every night** and closed claims monthly. The map also queries BLM live for whatever you're looking at when zoomed in — so claim boundaries are always current even between refreshes.

**Estimated base cost: roughly $1–3/month** (S3 + CloudFront transfer;
Lambda + EventBridge are comfortably inside the always-free tier at this
traffic). WS10 COGs and tile pyramids can be much larger than the base site,
so their storage and egress are additional and depend on the number and
resolution of published sheets. Teardown instructions are at the bottom.

There are two paths. **Path A (recommended)** uses one script and takes ~15 minutes. **Path B** is every console click, if you'd rather see and understand each piece. Both produce identical results.

---

## Before you start (both paths)

1. **An AWS account** — sign up at https://aws.amazon.com if you don't have one (needs a credit card; you'll stay in free-tier territory).
2. **Sign in and pick a region.** Top-right of the AWS console, set the region to **US West (Oregon) — us-west-2**. (Any region works; Oregon is fitting and cheap. Whatever you pick, stay consistent.)
3. You need the `nw-mineral-monitor/` folder from your repo on your machine (it contains `site/`, `infra/`, and this file).

---

## Path A — script deploy (recommended, ~15 min)

### A1. Install the AWS CLI
macOS: `brew install awscli` — or download the installer from https://aws.amazon.com/cli/. Verify with `aws --version` (want v2.x).

### A2. Create an access key and configure the CLI
1. AWS console → search **IAM** → **Users** → **Create user**. Name: `deployer`. Don't enable console access.
2. On the user's page → **Permissions** → **Add permissions → Attach policies directly** → check **AdministratorAccess** → add. (Fine for a personal account; you can tighten later.)
3. **Security credentials** tab → **Create access key** → use case "Command Line Interface" → create. Copy both values.
4. In your terminal: `aws configure` — paste the Access Key ID, Secret Access Key, default region `us-west-2`, output format `json`.

### A3. Run the deploy
```bash
cd nw-mineral-monitor/infra
chmod +x deploy.sh
./deploy.sh
```
The script: creates the CloudFormation stack (bucket, CloudFront, Lambda, schedules) → uploads the real Lambda code → syncs the `site/` folder to S3 → triggers the first claims refresh → prints your URL.

First run takes 5–10 minutes (CloudFront distributions are slow to create). When it prints `Your map is live at: https://dXXXXXXXX.cloudfront.net`, give CloudFront ~5 more minutes, then open the URL.

### A4. Verify (2 minutes)
- Map loads, heatmap visible, header shows **177,994 SITES / 113,330 ACTIVE CLAIMS**.
- Footer says `claims snapshot <today's date>` — that means the Lambda refresh worked.
- Zoom into the Silver Valley, Idaho (search "bunker hill") past zoom 10.5 — a green **LIVE** badge should appear top-right with claim polygons fetched straight from BLM.
- Console → **Lambda → nw-mineral-monitor-claims-updater → Monitor**: one successful invocation.

That's it. Later:
```bash
./deploy.sh update-site   # after editing site files
./deploy.sh upload-ws10-assets  # explicit add/update of ignored quad rasters/tiles
./deploy.sh refresh       # force a claims re-pull right now
./deploy.sh teardown      # delete everything
```

---

## WS10 quad rasters — separate, protected upload

Quad source scans, COGs, legends/previews, and XYZ tiles do not live in
`site/` or in git. The build stages publishable objects under
`pipelines/cache/ws10/assets/`; the deploy script copies that tree to the
fixed bucket prefix `ws10-assets/`:

```bash
# Build one layer at a time from nw-mineral-monitor/.
python3 pipelines/prepare_quad_geology.py --download --skip-vector --only mayflower-mbmg-ofr-505
cd infra
WS10_UPLOAD_DRY_RUN=1 bash deploy.sh upload-ws10-assets
bash deploy.sh upload-ws10-assets
cd ..
# After verifying this layer's remote COG/tile/legend-or-preview objects and alignment:
python3 pipelines/prepare_quad_geology.py --mark-ready mayflower-mbmg-ofr-505
python3 pipelines/geology_quads.py
python3 pipelines/prepare_quad_geology.py --evict-ready-local mayflower-mbmg-ofr-505
# Repeat build → upload → verify → mark-ready → evict for each layer.
```

This order matters. A local build deliberately emits `processing` /
`built-awaiting-upload`, so upload and verify the objects before promoting
their pointers. The asset command refuses a missing or empty staging
directory and any symlink, follows no symlinks, never uses `--delete`, applies
a one-day cache header to binary map products and a 15-minute header to
JSON/GeoJSON, then invalidates `/ws10-assets/*` in CloudFront. It cannot prune
older remote versions. `--mark-ready` validates the still-local build again
and records `uploaded-and-verified`; `geology_quads.py` merges that state into
the inventory. `--evict-ready-local` then removes only that remotely verified
layer's exact local COG, XYZ, and legend-or-preview files, retaining its
source cache, checksums, provenance, and S3 copy. This sequential cycle keeps
disk use bounded while building the 18 unique rasters. If the workstation is
still space-constrained, the exact ignored source/work cache may be removed
separately after remote verification; the committed official URL, SHA-256,
byte count, extraction contract, and published pointers preserve a
reproducible path without implying that source KMZ/PDF files were uploaded.

The deployed inventory has 19 target checkboxes backed by 18 unique layers:
four seed overlays plus 14 ranked-map selections, with Hailey shared by Idaho
Bonanza and Atlanta. Once all 18 have completed the cycle, publish the final
pointer/UI bundle:

```bash
python3 pipelines/geology_quads.py
/Users/matthewlew/miniconda3/bin/python pipelines/validate_quad_geology.py --skip-assets
bash infra/deploy.sh update-site
```

`--skip-assets` is valid here only because every evicted layer passed local
build/mark-ready checks and remote COG/tile/legend-or-preview verification
before eviction. The final validator still checks rank/quad/gap and
target-mapping invariants, 18 ready and zero blocked layers, remote stamps,
the DWM-193 native-vector rescan, Jackson's raster-only provenance, guarded
outbox, target-switcher/UI syntax, and the no-raster-in-git rule. Replace the
shown Miniconda path with another dependency-capable Python when necessary;
do not publish after a validator failure.

Both the full deploy and `update-site` exclude `ws10-assets/*` from every
`aws s3 sync --delete`. That prefix is S3-owned, so an ordinary site/data
deploy cannot erase it even though no matching local files exist. `teardown`
is the exception: after its typed confirmation it intentionally empties the
whole bucket, including WS10 assets.

For a console-only deployment, repeat the same one-layer cycle: upload the
current *contents* of `pipelines/cache/ws10/assets/` into a bucket folder
named exactly `ws10-assets/`, verify and mark that layer ready, then evict it
locally. Upload the final `site/data/geology-quads/` inventory only after all
18 cycles. Preserve the directory/key layout: XYZ URLs depend on
`{z}/{x}/{y}` matching those keys.

Rasters never belong in `site/`, even temporarily. Doing so bloats git and
also turns the root site sync into an accidental lifecycle manager. The 14
ranked selections publish a reduced whole-sheet **map preview** under
`ws10-assets/previews/`; that is orientation context, not a geologic-unit
legend. A **legend** URL is used only for a reviewed crop of an actual unit
key, as on the four seed overlays. Full acquisition, Python/Poppler
prerequisites, alignment review, and verification steps are in `RUNBOOK.md`.
The current builder uses Pillow, NumPy, tifffile, Fiona, pyproj, and Shapely;
it requires no GDAL command-line tools.

Each ranked source is an official NGMDB KMZ. Its configured KML
GroundOverlay bounds, zero rotation, raster member, and target containment
were reviewed before selection; Hailey was checked against both target
coordinates. Regional fallbacks remain explicitly labeled for Willow
Creek/Pearl, Azurite, New Trail, Excelsior, Mc Grath, Idaho Bonanza/Atlanta,
and Mammoth. Finer non-georeferenced scans or GIS products remain cataloged
upgrade candidates and are not silently substituted with unreviewed warps.

Jackson PGM-19-01 follows the same per-layer promotion cycle. Its source is
the official public NGMDB 4096×4096 georeferenced KMZ, and its legend comes
from the NGMDB sheet preview. The project owner waived a separate reuse review for
this academic deployment; preserve CGS/NGMDB attribution and do not claim an
open-content license. The original CGS PDF remains email-delivered through
the California ADA workflow, native attributed GIS is not publicly
available, and no Jackson vector rescan is published. The CGS GIS-request
draft remains unsent and is superseded for raster acquisition.

---

## Path B — console, click by click (~40 min)

### B1. Create the stack (bucket + CloudFront + Lambda + schedules in one shot)
1. Console → search **CloudFormation** → **Create stack → With new resources (standard)**.
2. "Specify template" → **Upload a template file** → choose `infra/template.yaml` → Next.
3. Stack name: `nw-mineral-monitor` → Next.
4. On "Configure stack options": scroll down, under **Capabilities** check *"I acknowledge that AWS CloudFormation might create IAM resources"* → Next → **Submit**.
5. Wait for status **CREATE_COMPLETE** (~5–10 min; refresh occasionally). Open the **Outputs** tab and note three values: `SiteURL`, `BucketName`, `UpdaterFunctionName`.

### B2. Paste in the real Lambda code
1. Console → **Lambda** → open **nw-mineral-monitor-claims-updater**.
2. In the **Code** tab you'll see a placeholder `index.py`. Open `infra/lambda_updater.py` from the repo on your machine, copy ALL of it, and paste it over the placeholder contents.
3. Click **Deploy** (the button above the editor).

### B3. Upload the site
1. Console → **S3** → open the bucket from `BucketName` (looks like `nw-mineral-monitor-123456789012`).
2. Click **Upload** → **Add files / Add folder** → select the *contents* of the `site/` folder: `index.html`, the `assets` folder, and the `data` folder. (Important: `index.html` must land at the top level of the bucket, not inside a `site/` prefix.) Upload WS10 raster assets separately as described above; do not mix them into `site/`.
3. Upload (~65 MB — a few minutes on ordinary broadband).

### B4. First refresh + verify
1. Lambda → **nw-mineral-monitor-claims-updater** → **Test** tab → create a test event named `active` with payload `{"mode":"active"}` → **Test**. It runs 2–4 minutes and returns per-state counts.
2. Open the `SiteURL` from B1 and run the same checks as A4.

The EventBridge schedules created by the stack (visible under **Amazon EventBridge → Rules**) now run the refresh automatically: active claims nightly at 09:10 UTC, closed claims on the 1st of each month in three batches.

---

## How the auto-update works (what you just built)

```
             nightly 09:10 UTC              on page view
EventBridge ───────────────► Lambda ──► S3 /data/claims/*.json ──► CloudFront ──► browser
                              │                                                    │
                              └── queries BLM MLRS GIS                             └── at zoom ≥ 10.5, browser ALSO
                                  (active: daily, ~113k claims;                        queries BLM directly for the
                                   closed: monthly, ~820k)                             current view — always live
```
The `data/*` path is cached at CloudFront for 15 minutes, so a fresh snapshot is visible at most 15 minutes after the Lambda finishes. Everything else (map code, USGS/state layers) is static and updates only when you re-upload.

## The login (AWS Cognito)

The stack creates a **Cognito user pool** with self-signup disabled and one user, **codyClinger**. Path A's `./deploy.sh` finishes the job automatically: it sets the permanent password (`testing123` by default — override with `COGNITO_USER=... COGNITO_PASS=... ./deploy.sh`) and uploads an `auth.json` (region + app-client id, both public-by-design values) to the bucket, which is what switches the site into login mode. Until `auth.json` exists in the bucket, the site is open — so local dev (`python3 -m http.server`) never asks you to sign in.

If you deployed **before** this feature existed, just run `./deploy.sh` again — CloudFormation adds the Cognito pieces to the existing stack in place, then the script re-uploads the site, writes `auth.json`, and sets the password. Sessions last 24 hours with a 30-day remember-me refresh token; SIGN OUT is in the header.

**Path B equivalents:** after updating the stack with the new `template.yaml`, (1) upload the new `site/index.html` to the bucket, (2) create a file `auth.json` containing `{"region":"us-west-2","clientId":"<UserPoolClientId from stack Outputs>"}` and upload it to the bucket root, (3) set the password with the one CLI command below (the console can only issue temporary passwords — though the login page handles those too, by making the first sign-in permanent).

**Managing users** (Cognito console → User pools → nw-mineral-monitor-users, or CLI):

```bash
# add a user
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> --username jane --message-action SUPPRESS --region us-west-2
aws cognito-idp admin-set-user-password --user-pool-id <UserPoolId> --username jane --password 'her-password' --permanent --region us-west-2
# change a password / disable someone
aws cognito-idp admin-set-user-password --user-pool-id <UserPoolId> --username codyClinger --password 'new-pass' --permanent --region us-west-2
aws cognito-idp admin-disable-user --user-pool-id <UserPoolId> --username codyClinger --region us-west-2
```

**Honest scope:** sign-in is enforced by the app in the browser against real Cognito (wrong password = no session, and there's no way to reach the map UI without one). The underlying `data/*.json` files are still individually fetchable by someone who reads the page source and constructs URLs — they're public government data, so the gate is about controlling access to the *app*, not secrets. If you ever need hard enforcement at the CDN layer (every request checked), that's the "cognito-at-edge" Lambda@Edge pattern — say the word and it can be added, at the cost of a us-east-1 companion stack and slower teardowns. Also: `testing123` is a weak password on a public URL — worth changing once real use starts. Cognito cost at this scale: $0 (free tier covers the first 10,000 monthly active users).

## The AI answerer (Bedrock)

The ASK terminal answers structured questions ("gold producers in montana") with its built-in engine — free, instant, offline. Questions it can't parse ("why did mining collapse in washington?") route to an **AI answerer**: a small Lambda (`nw-mineral-monitor-ask`) that relays the conversation to a Claude model on **Amazon Bedrock** with tool calling. The model's tools — query the site layers, pull district dossiers, read the intel bundle, fly the map — execute in your browser against the loaded data, so answers are grounded in the real records, and the model is instructed to never state a number it didn't get from a tool.

**Setup:** none, usually. AWS retired the Model access page (mid-2026) — serverless models now auto-enable on first invoke. Just `bash deploy.sh` (it uploads the ask-Lambda and adds the endpoint to `auth.json`) and ask a question. One caveat: **first-time Anthropic use in an account may require a one-time use-case form** — if the chat returns a 424 "could not serve model" message, open **Bedrock → Model catalog → Claude 3.5 Haiku → playground**, send one message, fill the form if it appears (a minute, typically instant), then retry the chat. Everything else keeps working regardless.

Facts worth knowing: requests require a signed-in Cognito session (the Lambda verifies the token against Cognito before spending a cent), so strangers can't burn your tokens. Cost is per-question: Haiku-class pricing puts a typical tool-using question at **well under a penny**; there's no idle cost. The green **AI ON** chip in the ASK panel toggles it; with it off (or locally, or if Bedrock errors) the terminal falls back to the structured engine. To use a different model: Lambda console → `nw-mineral-monitor-ask` → Configuration → Environment variables → `MODEL_ID` (any Bedrock model ID or inference profile your account has access to).

## Troubleshooting

- **"AccessDenied" XML when opening the URL** — you opened the S3 website URL instead of the CloudFront `SiteURL`, or `index.html` is nested inside a `site/` prefix in the bucket. Fix the file layout (B3 note).
- **Map loads but footer shows `claims snapshot 2026-07-30` forever** — the Lambda isn't running or can't write. Check Lambda → Monitor → recent invocations, and that its environment variable `BUCKET` matches your bucket (the stack sets this automatically).
- **LIVE badge says "BLM live query failed"** — BLM's GIS server is down or rate-limiting (it happens; federal servers nap). The snapshot layers still work; live polygons return when BLM does.
- **ASK chat says "Bedrock could not serve <model>"** — first-time Anthropic use-case form (see the AI section above: playground once, then retry), or an IAM/SCP restriction, or a bad MODEL_ID. **"session expired"** — sign out/in (the AI endpoint checks your Cognito token). **"model is throttled"** — Bedrock burst limit; retry in a few seconds.
- **Stack create failed with "BucketName already exists"** — you deployed before; either reuse that stack (CloudFormation → update) or delete the old one first.
- **Changed a file but the site didn't update** — CloudFront caching. Data refreshes within 15 min; for `index.html` changes either wait an hour, or CloudFront console → your distribution → Invalidations → create invalidation for `/*`.
- **A GEOLOGY (QUAD) toggle returns 403/404** — the inventory pointer was deployed before its S3 object, or its key does not match the local asset layout. Run `upload-ws10-assets` first, verify the object under the bucket's `ws10-assets/` prefix, then `update-site`. A low-confidence plate should stay in review rather than being patched around with a misleading URL.

## Optional: custom domain
Buy/hold a domain in **Route 53** (or elsewhere) → **ACM** (in us-east-1!) → request a public certificate for `mines.yourdomain.com` → validate via DNS → CloudFront console → your distribution → Edit → add the Alternate domain name + attach the certificate → create a Route 53 **A record (alias)** pointing at the distribution. ~20 minutes, mostly waiting on certificate validation.

## Teardown
Path A: `./deploy.sh teardown`. Path B: empty the S3 bucket (S3 → bucket → Empty), then CloudFormation → select the stack → **Delete**. This removes everything the stack created, including S3-only `ws10-assets/`; total cost stops immediately.
