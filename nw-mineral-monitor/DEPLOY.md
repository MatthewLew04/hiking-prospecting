# Hosting the NW Mineral Monitor on AWS — step by step

You'll end up with: your map at a public `https://` URL, served from S3 through CloudFront (fast + cheap), with a Lambda job that re-pulls **active mining claims from BLM every night** and closed claims monthly into a private staging prefix. The browser reads checked PMTiles build artifacts, never those whole-state staging snapshots. It also queries BLM live for the current viewport when zoomed in, so the visible live-boundary overlay is current between PMTiles builds.

**Estimated base cost: roughly $1–3/month** (S3 + CloudFront transfer;
Lambda + EventBridge are comfortably inside the always-free tier at this
traffic). WS10 COGs and tile pyramids can be much larger than the base site,
so their storage and egress are additional and depend on the number and
resolution of published sheets. Teardown instructions are at the bottom.

The supported production path uses `infra/deploy.sh` and takes roughly 15
minutes. The AWS console remains useful for inspecting the stack and its
outputs, but a manual console upload is not equivalent: it cannot reproduce
the repository validators, exact binary allowlists, remote checksum checks,
or pointer-last publication order.

---

## Before you start

1. **An AWS account** — sign up at https://aws.amazon.com if you don't have one (needs a credit card; you'll stay in free-tier territory).
2. **Sign in and pick a region.** Top-right of the AWS console, set the region to **US West (Oregon) — us-west-2**. (Any region works; Oregon is fitting and cheap. Whatever you pick, stay consistent.)
3. You need the `nw-mineral-monitor/` folder from your repo on your machine (it contains `site/`, `infra/`, and this file). Install Git LFS and run `git lfs pull` after cloning; PMTiles and COGs are LFS objects, and the deploy preflight intentionally rejects pointer-only files.

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
The script creates the CloudFormation stack (bucket, CloudFront, Lambda,
schedules), packages the complete Lambda dependencies, validates and uploads
the public site in dependency order, triggers the first claims refresh, and
prints the URL.

First run takes 5–10 minutes (CloudFront distributions are slow to create). When it prints `Your map is live at: https://dXXXXXXXX.cloudfront.net`, give CloudFront ~5 more minutes, then open the URL.

### A4. Verify (2 minutes)
- Map loads, heatmap visible, header shows **898,684 SITES / 458,049 FED ACTIVE / 1,262,983 FED CLOSED / 39,320 AK ACTIVE+PENDING** for the committed baseline artifacts. Alaska counts are source polygons, not asserted unique claims; the Alaska total comprises 39,269 active and 51 pending polygons, and multipart serials may repeat.
- Open **49-STATE COVERAGE** and confirm that every incomplete state says
  **BUILDING**; a source registration must not appear as a released state.
- Zoom into the Silver Valley, Idaho (search "bunker hill") past zoom 10.5 — a green **LIVE** badge should appear top-right with claim polygons fetched straight from BLM.
- Console → **Lambda → nw-mineral-monitor-claims-updater → Monitor**: one successful invocation. Its output is private tile-build staging, not a browser JSON snapshot.

That's it. Later:
```bash
./deploy.sh update-site   # after editing site files
./deploy.sh upload-ws10-assets  # explicit add/update of ignored quad rasters/tiles
./deploy.sh upload-release-assets  # explicit add/update of immutable WS11 DONE artifacts
./deploy.sh upload-doc-store  # upload + remotely verify the private WS12 source-document corpus
./deploy.sh refresh       # force a private MLRS staging pull right now
./deploy.sh teardown      # delete everything
```

### Public-site upload guard and stale binaries

Before either `deploy` or `update-site` makes an AWS request,
`pipelines/validate_public_site.py` inspects the whole `site/` tree. It rejects
symlinks, hidden files, temporary/backup paths, sensitive credential/key
extensions, and every PMTiles/TIFF not referenced by the current public
manifest. Literal mutable tile paths in `index.html` are forbidden; the UI must
resolve them through the manifest. The validator maps every exact friendly
local build path to
`map-assets/baselines/<full-sha256><original-suffix>`, recursively rewrites
both plain paths and exact `pmtiles://` paths in a strict temporary deployment
manifest, and verifies that duplicate browser descriptors still agree. Query
string cache-busters and substring rewrites are rejected.

The deploy uploads only the deduplicated content-addressed plan, verifies each
remote object's byte count and full-object SHA-256, and gives these immutable
objects a one-year cache policy. It never uploads or overwrites a friendly
`data/tiles/...` binary key. After every dependency succeeds it uploads
`coverage.json`, the transformed `manifest.json` pointer (never the checked-in
friendly-path manifest), and finally `index.html`. A binary upload or
verification failure therefore cannot advance the public pointers or UI, and
an old cached manifest continues to name its old unmodified generation.

Public binaries are excluded from both recursive `sync --delete` phases.
Content-addressed generations are intentionally retained because cached old
manifests may still reference them; the default CloudFront behavior can safely
cache these unique `map-assets/baselines/` keys without range responses mixing
bytes from different generations. There is no authenticated historical
inventory that can prove when all clients have stopped using a generation, so
the deploy does not guess and does not automatically delete one. To inspect
the current local build allowlist:

```bash
python3 pipelines/validate_public_site.py --site site --format paths
```

Any future lifecycle cleanup must operate on an exact reviewed
`map-assets/baselines/<sha>.<suffix>` key only after its retention window and
all retained manifests have been audited. Never use a recursive/glob deletion,
and never include `map-assets/releases/` or `ws10-assets/`; those immutable
namespaces have separate lifecycle rules. Bucket versioning makes an exact
mistaken deletion recoverable, but it is not a substitute for reviewing the
key.

---

## WS12 stored source documents — private, presigned, opt-in

This section sits before the WS11 one to keep DEPLOY's newest-first order.

Citation chips open our own archived copy of a cited document, not the
portal that published it, so a citation still resolves after the portal
moves or dies. Each document is stored twice under a private `docs/` prefix
— the raw original and a searchable copy whose text layer sits on the same
pages — and the corpus is deliberately **not** in the CloudFront bucket-policy
allowlist. The only way to read one is a 300-second presigned GET that the
`nw-mineral-monitor-docs` Lambda mints after checking your Cognito session.
The full manifest is private at
`private/ws12/document-store-manifest.json`. Signed-in clients receive a
minimized catalog from the Docs API; CloudFront explicitly denies the former
`data/docs/manifest.json` path as well as every PDF under `docs/`.

Delivery is opt-in. `ENABLE_LEGACY_DOC_STORE` defaults to `false`, and an
ordinary deploy therefore ships neither `viewer.html`, the vendored PDF.js
payload, the manifest, nor the corpus — and actively removes them if they
were published before. Turn it on only when every row you intend to serve
carries an affirmative public-domain basis:

```bash
export ENABLE_LEGACY_DOC_STORE=true
bash deploy.sh upload-doc-store   # PDFs + private manifest first; remotely re-verified
bash deploy.sh deploy             # creates Docs API/Lambda, JWT binding, viewer, and auth.json
```

When enabling from `false`, `update-site` is not enough: it cannot create the
conditional Docs API/Lambda or write a new `docsUrl` output into `auth.json`.
The full deploy requires the existing Cognito password environment variables;
coordinate it with any active WS11 owner before publishing shared site files.

`upload-doc-store` refuses to run without that variable, re-runs the store
gate with `--store-dir`, sends the manifest digest explicitly with
`--checksum-sha256`, and reads the digest back with `head-object
--checksum-mode ENABLED` before counting it verified. Each PUT has three
bounded attempts plus an exact remote-identity check, so a stalled transport
cannot pin the resumable queue. Local success is never treated as remote
proof.

Raw originals are tagged `ws12-variant=raw` and move to Infrequent Access
after 30 days; searchable copies stay hot because every citation opens one.
Both `deploy` and `update-site` exclude `docs/*` from every
`aws s3 sync --delete`, so the corpus is never pruned by a site sync.

---

## WS11 state releases — immutable, gate-controlled upload

Per-state PMTiles and COGs that pass the WS11 DONE gate live below
`site/map-assets/releases/`. The normal `deploy` and `update-site` commands
upload that tree before publishing the mutable manifest and registry pointers.
The ordinary site sync continues to exclude `map-assets/`, so `--delete`
cannot prune an older content-addressed generation. To upload the immutable
tree alone after a reviewed build:

```bash
cd infra
bash deploy.sh upload-release-assets
```

This command requires an empty release tree when zero states are released and
rejects missing, unexpected, hidden/temporary, or symlinked objects otherwise.
It never deletes remote objects, sets a one-year immutable cache policy, and
invalidates only `/map-assets/releases/*`. A registry entry must not be enabled
until the local artifact, hash, bytes, feature schema, and all DONE evidence
pass the release validator.

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

For manual recovery or inspection, the equivalent object layout places the
current *contents* of `pipelines/cache/ws10/assets/` below the exact
`ws10-assets/` bucket prefix. This is not a supported replacement for the
scripted upload/verify/mark-ready cycle. Preserve the directory/key layout:
XYZ URLs depend on `{z}/{x}/{y}` matching those keys, and never advance the
final `site/data/geology-quads/` inventory until all 18 scripted cycles pass.

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

## AWS console — inspection only, not a production deploy path

The former click-by-click Path B is retired. Pasting one Lambda source file
omits its packaged runtime registry, clipping configuration, and helper
modules. Dragging the roughly **2.11 GB** `site/` tree into S3 also bypasses
the exact manifest-derived binary allowlist, immutable release authorization,
remote SHA-256/size verification, and the binary-before-pointer/index-last
failure boundary. It can expose partial or unvalidated state and is not a
supported production procedure.

Use Path A above for every deploy and `bash infra/deploy.sh update-site` for a
site-only publication. The console is still appropriate for watching
CloudFormation complete, reading stack
outputs, inspecting Lambda logs, and confirming S3/CloudFront state after the
script succeeds.

The deployed EventBridge rules run active-claim staging nightly at 09:10 UTC.
Closed-claim staging runs in **eight batches across days 1–5 of each month**:
three batches on day 1, two on day 2, then one batch on each of days 3, 4, and
5. These jobs update private build staging; they do not directly replace the
browser PMTiles.

---

## How claim refresh and publication work

```
             nightly / monthly                    checked build + deploy
EventBridge ───────────────► Lambda ──► private S3 staging/claims/*.json ─────┐
                              │                                               │
                              └── queries BLM MLRS GIS                        ▼
                                                                   claims.pmtiles ──► browser

At zoom ≥ 10.5 the browser independently queries BLM for the current viewport.
```
The bucket policy intentionally does not expose `staging/`, and production sets
`LEGACY_JSON_STATES` to empty. Therefore `deploy.sh refresh` does **not** silently
replace the public map: it refreshes inputs for a later validated PMTiles build.
The committed `claims.pmtiles` changes only when its builder succeeds and the
site is re-uploaded. The direct live-boundary overlay remains independent of
that build cycle.

## The login (AWS Cognito)

The stack creates a **Cognito user pool** with self-signup disabled and three named users. Path A's `./deploy.sh` sets their permanent passwords and uploads an `auth.json` (region + app-client id, both public-by-design values), which switches the site into login mode. There are deliberately no passwords in the repository or fallback defaults: set `COGNITO_PASS`, `COGNITO_PASS_SEAN`, and `COGNITO_PASS_RACHEL` to distinct values of at least 12 characters containing uppercase, lowercase, a number, and a symbol before running `./deploy.sh` (optionally override the first username with `COGNITO_USER`). Until `auth.json` exists in the bucket, the site is open — so local dev (`python3 tools/range_server.py 8000`) never asks you to sign in. That range-capable server is required for PMTiles; plain `python3 -m http.server` is not supported.

If you deployed **before** this feature existed, just run `./deploy.sh` again — CloudFormation adds the Cognito pieces to the existing stack in place, then the script re-uploads the site, writes `auth.json`, and sets the password. Sessions last 24 hours with a 30-day remember-me refresh token; SIGN OUT is in the header.

Do not use a manual `auth.json`/`index.html` upload as a Path B substitute: it
can advance the UI independently of its data dependencies. The supported
deploy script writes `auth.json` only after the validated data pointers and
uploads `index.html` last. The console remains suitable for inspecting the
user pool; the CLI commands below are supported for individual user
administration after a script deployment.

**Managing users** (Cognito console → User pools → nw-mineral-monitor-users, or CLI):

```bash
# add a user
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> --username jane --message-action SUPPRESS --region us-west-2
aws cognito-idp admin-set-user-password --user-pool-id <UserPoolId> --username jane --password 'her-password' --permanent --region us-west-2
# change a password / disable someone
aws cognito-idp admin-set-user-password --user-pool-id <UserPoolId> --username codyClinger --password 'new-pass' --permanent --region us-west-2
aws cognito-idp admin-disable-user --user-pool-id <UserPoolId> --username codyClinger --region us-west-2
```

**Honest scope:** sign-in is enforced by the app in the browser against real Cognito (wrong password = no session, and there's no way to reach the map UI without one). The underlying public-data artifacts are still individually fetchable by someone who reads the page source and constructs URLs — the gate controls access to the *app*, not secrets. Private `staging/`, `watch/`, and `ckpt/` prefixes are excluded from CloudFront's bucket-policy resources. If you need hard enforcement at the CDN layer (every request checked), that requires a Cognito-at-edge companion design. Cognito cost at this scale: $0 (free tier covers the first 10,000 monthly active users).

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
- **A citation chip opens a viewer that says "no document endpoint configured"** — `auth.json` has no `docsUrl`. It is written only by the full `deploy` path, not `update-site`, and only when the stack exposes the `DocsUrl` output. Re-run `./deploy.sh` (with `ENABLE_LEGACY_DOC_STORE=true` if you intend to serve documents), then hard-reload.
- **The viewer opens but the scanned page is blank under a working highlight** — the vendored PDF.js WebAssembly decoders are missing. Survey scans are JBIG2. Run `npm run vendor:pdfjs`, confirm `site/assets/pdfjs/wasm/jbig2.wasm` exists, then `update-site`.
- **`https://<distribution>/docs/...` returns 200** — stop. The document corpus must be uncrawlable; a 200 means `docs/*` reached the bucket-policy allowlist. Remove it from `SiteBucketPolicy` and redeploy the stack before doing anything else.
- **A GEOLOGY (QUAD) toggle returns 403/404** — the inventory pointer was deployed before its S3 object, or its key does not match the local asset layout. Run `upload-ws10-assets` first, verify the object under the bucket's `ws10-assets/` prefix, then `update-site`. A low-confidence plate should stay in review rather than being patched around with a misleading URL.

## Optional: custom domain
Buy/hold a domain in **Route 53** (or elsewhere) → **ACM** (in us-east-1!) → request a public certificate for `mines.yourdomain.com` → validate via DNS → CloudFront console → your distribution → Edit → add the Alternate domain name + attach the certificate → create a Route 53 **A record (alias)** pointing at the distribution. ~20 minutes, mostly waiting on certificate validation.

## Teardown
Preferred teardown: `./deploy.sh teardown`. For emergency console cleanup,
empty the exact stack-owned S3 bucket (including versions) and then delete the
CloudFormation stack. This removes everything the stack created, including
S3-only `ws10-assets/` and the private WS12 `docs/` corpus; total cost stops
immediately.
