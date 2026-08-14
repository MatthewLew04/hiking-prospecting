"""NW Mineral Monitor — WS12 document store: short-TTL presigned GETs.

The document corpus is private.  It is deliberately NOT in the CloudFront
bucket-policy allowlist, so it cannot be crawled: the only way to read a
stored PDF is a signed URL minted here, for one object, for a few minutes,
after this function has verified the caller's Cognito session.

A request names a doc_id (the SHA-256 of the raw original) or one of the
document's stable source ids, plus an optional page and quote.  The response
carries the presigned URL of the SEARCHABLE copy — the one whose text layer
sits on the original pages, so page N of the citation is page N of the file —
together with the page to open, the quote to highlight, and the originating
portal URL for provenance.  The portal URL is displayed, never fetched; the
citation resolves entirely out of our own manifest and our own S3 objects.

Env: BUCKET (required), MANIFEST_KEY (default private/ws12/document-store-manifest.json),
     SITE_URL (viewer origin), PRESIGN_TTL (seconds, default 300),
     MANIFEST_TTL (manifest cache seconds, default 300),
     ALLOW_ANON ("true" to skip the Cognito check — local/dev only).
"""
import json, os, time, urllib.parse, boto3
from botocore.exceptions import ClientError

import doc_store

BUCKET = os.environ.get("BUCKET", "")
MANIFEST_KEY = os.environ.get(
    "MANIFEST_KEY", "private/ws12/document-store-manifest.json")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
PRESIGN_TTL = max(30, min(3600, int(os.environ.get("PRESIGN_TTL", "300") or 300)))
MANIFEST_TTL = max(0, int(os.environ.get("MANIFEST_TTL", "300") or 300))
ALLOW_ANON = os.environ.get("ALLOW_ANON", "false").lower() == "true"
MAX_QUOTE = 2000

s3 = boto3.client("s3")
cognito = boto3.client("cognito-idp")
_manifest = {"at": 0.0, "value": None}


def resp(code, obj):
    return {"statusCode": code,
            "headers": {"Content-Type": "application/json",
                        "Cache-Control": "no-store",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "authorization, content-type, x-auth-token",
                        "Access-Control-Allow-Methods": "GET, OPTIONS"},
            "body": json.dumps(obj)}


def manifest():
    """Load and validate the manifest, cached across warm invocations.

    The manifest is validated on every load rather than trusted: a manifest
    that does not reconcile would otherwise mint signatures for keys nothing
    verified.
    """
    now = time.time()
    if _manifest["value"] is not None and now - _manifest["at"] < MANIFEST_TTL:
        return _manifest["value"]
    body = s3.get_object(Bucket=BUCKET, Key=MANIFEST_KEY)["Body"].read()
    value = doc_store.validate_manifest(
        doc_store.strict_json_bytes(body, MANIFEST_KEY))
    _manifest["value"] = value
    _manifest["at"] = now
    return value


def viewer_url(resolved):
    """Deep link to our own viewer.

    Parameters ride in the fragment so the doc id, page, and quote never
    appear in a request line, a proxy log, or a Referer header.  The viewer
    calls this endpoint itself for a fresh signature, so the link stays valid
    long after the presigned URL beside it has expired.
    """
    if not SITE_URL:
        return None
    fragment = urllib.parse.urlencode({
        "doc": resolved["doc_id"],
        "page": resolved["page"],
        "q": resolved["quote"] or "",
    })
    return f"{SITE_URL}/viewer.html#{fragment}"


def browser_catalog(catalog):
    """Return only the authenticated browser fields, never S3 object keys.

    The canonical manifest remains private and drives signing. The map needs
    titles, stable ids, subject joins, and reviewed citation quotes to render
    chips, but it never needs storage paths, object hashes, or rights internals.
    """
    document_fields = (
        "doc_id", "title", "authority", "state", "mine_id", "pages",
        "text_layer", "retrieved", "source_ids", "source_url", "catalog_url",
        "subjects",
    )
    citation_fields = (
        "citation_id", "dataset", "doc_id", "mine_id", "mine_name", "page",
        "page_cite", "quote", "quote_located", "source_id", "state",
    )
    return {
        "schema_version": catalog["schema_version"],
        "dataset": catalog["dataset"],
        "generated": catalog["generated"],
        "metrics": {
            key: catalog["metrics"][key]
            for key in ("documents", "pages", "citations", "citations_quote_located")
        },
        "documents": [
            {key: row[key] for key in document_fields if key in row}
            for row in catalog["documents"]
        ],
        "citations": [
            {key: row[key] for key in citation_fields if key in row}
            for row in catalog["citations"]
        ],
    }


def presign(key, filename):
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key,
                "ResponseContentType": "application/pdf",
                "ResponseContentDisposition": f'inline; filename="{filename}"'},
        ExpiresIn=PRESIGN_TTL)


def authenticate(event):
    if ALLOW_ANON:
        return None
    # token arrives in x-auth-token — a public Function URL 403s on any
    # Authorization header (it's parsed as a SigV4 attempt), so never use it
    auth = ""
    for k, v in (event.get("headers") or {}).items():
        if k.lower() in ("x-auth-token", "authorization"):
            auth = v or ""
    token = auth.replace("Bearer ", "").strip()
    if not token:
        return resp(401, {"error": "sign in to open source documents"})
    try:
        cognito.get_user(AccessToken=token)
    except ClientError:
        return resp(401, {"error": "session expired — sign in again"})
    return None


def handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "GET")
    if method == "OPTIONS":
        return resp(200, {"ok": True})
    if not BUCKET:
        return resp(500, {"error": "document store bucket is not configured"})

    params = event.get("queryStringParameters") or {}
    denied = authenticate(event)
    if denied is not None:
        return denied

    try:
        catalog = manifest()
    except ClientError:
        return resp(503, {"error": "the document manifest is not published yet"})
    except doc_store.DocStoreError as exc:
        return resp(500, {"error": f"document manifest is invalid: {exc}"})

    if params.get("ping"):
        return resp(200, {"ok": True, "documents": catalog["metrics"]["documents"],
                          "citations": catalog["metrics"]["citations"],
                          "generated": catalog["generated"]})
    if params.get("catalog"):
        return resp(200, browser_catalog(catalog))

    doc_id = (params.get("doc_id") or params.get("doc") or "").strip()
    if not doc_id:
        return resp(400, {"error": "doc_id is required"})
    page = params.get("page")
    if page is not None:
        try:
            page = int(page)
        except (TypeError, ValueError):
            return resp(400, {"error": "page must be an integer"})
    quote = (params.get("quote") or "")[:MAX_QUOTE] or None
    variant = params.get("variant") or "searchable"
    if variant not in doc_store.VARIANTS:
        return resp(400, {"error": f"variant must be one of {list(doc_store.VARIANTS)}"})

    try:
        resolved = doc_store.resolve_open_doc(catalog, doc_id, page, quote)
    except doc_store.DocStoreError as exc:
        return resp(404, {"error": str(exc)})

    key = resolved["key"] if variant == "searchable" else resolved["raw_key"]
    digest = resolved["sha256"] if variant == "searchable" else resolved["raw_sha256"]
    byte_count = resolved["bytes"] if variant == "searchable" else resolved["raw_bytes"]
    filename = f'{resolved["doc_id"][:16]}-{variant}.pdf'
    try:
        url = presign(key, filename)
    except ClientError as exc:
        return resp(502, {"error": f"could not sign the document URL: "
                                   f'{exc.response.get("Error", {}).get("Code", "")}'})
    return resp(200, {
        "doc_id": resolved["doc_id"],
        "title": resolved["title"],
        "authority": resolved["authority"],
        "state": resolved["state"],
        "mine_id": resolved["mine_id"],
        "page": resolved["page"],
        "pages": resolved["pages"],
        "page_cite": resolved["page_cite"],
        "quote": resolved["quote"],
        "quote_located": resolved["quote_located"],
        "citation_id": resolved["citation_id"],
        "text_layer": resolved["text_layer"],
        "variant": variant,
        "file_url": url,
        "sha256": digest,
        "bytes": byte_count,
        "expires_in": PRESIGN_TTL,
        "viewer_url": viewer_url(resolved),
        # Shown beside the document so a reader can check where it came from.
        # We serve our own copy: portals move and die, citations do not.
        "source_url": resolved["source_url"],
        "catalog_url": resolved["catalog_url"],
        "retrieved": resolved["retrieved"],
    })
