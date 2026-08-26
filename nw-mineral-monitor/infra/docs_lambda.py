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

The 56,282-document WS13 corpus is served the same way, through a TWO-SHAPE
resolver with no data movement.  ws13_documents.searchable_key is populated for
all 28,988 ocr_queue documents and NULL for all 27,294 born_digital ones,
because a born-digital original already carries its publisher's text layer: the
servable object is the OCR output under ws13/searchable/ for the first group and
the ORIGINAL under ws12/ for the second.  infra/ws13_query_lambda.py already
resolves exactly that on every citation (viewer_key, viewer_key_kind), so this
function signs what that contract names instead of defining a second one.

Why the originals are not simply copied into ws13/searchable/ so that one prefix
could serve everything: ws13_documents.admission_class is GENERATED ALWAYS AS
(split_part(s3_key, '/', 2)) STORED, so the ws12/{class}/ prefix IS the rights
signal.  A servable duplicate under ws13/ would sit at a key that no longer
encodes its own rights class, and this corpus is 32,312 state-archive research
copies and 13,013 CC BY-NC-SA licensed copies against 10,957 public-domain
originals — attribution is the thing that makes serving any of it defensible.
So nothing moves.  The resolver goes to the object where it already is, and the
admission class is re-derived here from the same split the generated column
uses, never taken from the request.

Env: BUCKET (required), MANIFEST_KEY (default private/ws12/document-store-manifest.json),
     SITE_URL (viewer origin), PRESIGN_TTL (seconds, default 300),
     MANIFEST_TTL (manifest cache seconds, default 300),
     ALLOW_ANON ("true" to skip the Cognito check — local/dev only).
The WS13 path adds no environment of its own: it signs objects in the same
BUCKET, under the same short TTL, for the same authenticated caller.
"""
import json, logging, os, re, time, urllib.parse, boto3
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

LOG = logging.getLogger("ws12.docs")
LOG.setLevel(logging.INFO)


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


# --- WS13 corpus: two servable shapes, resolved and never guessed ----------

WS13_CORPUS = "ws13"
LEGACY_CORPUS = "ws12-store"
CORPORA = (LEGACY_CORPUS, WS13_CORPUS)

# S3 caps an object key at 1024 bytes; anything longer cannot name a stored
# object, so it is refused before the patterns run rather than backtracked over.
MAX_KEY_BYTES = 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# pipelines/ws13_worker.py writes ws13/searchable/{sha[:2]}/{sha}/searchable.pdf
# with a sidecar.txt beside it. Only the PDF is servable, and the digest in the
# key is the document id, so this shape is DERIVED from the request and never
# taken from it.
WS13_SEARCHABLE_KEY_RE = re.compile(
    r"ws13/searchable/[0-9a-f]{2}/[0-9a-f]{64}/searchable\.pdf")
# The WS12 original: ws12/{class}/... . Observed keys run from the flat
# ws12/research-copies/IF0131_001.pdf to
# ws12/originals/igs_mines/d2/d29a...e07f.pdf, so the depth is a range and not
# a fixed template. No segment may start with a dot, which is what makes '.'
# and '..' unrepresentable rather than merely filtered; the pattern is applied
# with fullmatch, so a leading '/', an embedded newline, or a valid prefix
# buried inside a longer string is refused too. The three classes are spelled
# out because ws12/ also holds ws12/deploy/document-index.sqlite3, which is a
# build artefact and not a document anyone may sign.
WS12_ORIGINAL_KEY_RE = re.compile(
    r"ws12/(originals|licensed-copies|research-copies)"
    r"(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*){0,5}"
    r"/[A-Za-z0-9_-][A-Za-z0-9._-]*\.pdf")

# The vocabulary infra/ws13_query_lambda.py emits on every citation. Matched
# here rather than re-invented: the viewer receives one of these from search
# and hands the same word back when it asks for a signature.
VIEWER_KEY_KINDS = ("searchable", "born_digital_original",
                    "scanned_original_no_text_layer")
# doc_store.TEXT_LAYER_STATUSES, keyed by the shape actually resolved. The
# viewer decides whether to offer text search and quote highlighting from this,
# so promising 'native' for a raster scan shows the reader an empty result and
# no error.
TEXT_LAYER_BY_KIND = {
    "searchable": "ocr_added",
    "born_digital_original": "native",
    "scanned_original_no_text_layer": "absent",
}

# Byte-identical mirror of ws13_query_lambda.RIGHTS_BY_CLASS. The two Lambdas
# ship as separate zips — infra/deploy.sh packs this one with only
# pipelines/doc_store.py — so the table cannot be imported from there;
# tests/test_ws13_doc_resolver.py asserts the two are equal so they cannot
# drift into telling a reader two different licences for one document.
RIGHTS_BY_CLASS = {
    "originals": {
        "rights_terms": "public domain (US federal / state survey public record)",
        "attribution_required": False,
        "non_commercial": False,
        "share_alike": False,
    },
    "licensed-copies": {
        "rights_terms": (
            "CC BY-NC-SA 4.0 - attribution required, non-commercial use only, "
            "share-alike; source: {basis}"
        ),
        "attribution_required": True,
        "non_commercial": True,
        "share_alike": True,
    },
    "research-copies": {
        "rights_terms": (
            "state-archive research copy - internal, attributed, authenticated "
            "access only; not redistributable; source: {basis}"
        ),
        "attribution_required": True,
        "non_commercial": True,
        "share_alike": False,
    },
}

# DocsRole holds s3:GetObject and NOT s3:ListBucket, so S3 answers a HEAD of a
# key that is not there with 403 AccessDenied instead of 404: it will not
# confirm non-existence to a caller that cannot list the bucket. Both codes
# therefore mean "no object of this shape", and the difference is logged rather
# than guessed at in the response.
MISSING_OBJECT_CODES = frozenset({
    "404", "NotFound", "NoSuchKey", "403", "AccessDenied"})

# site/index.html parses a citation chip as \(doc:<sha>#(\d{1,5})\), so a page
# beyond five digits could not have come from one.
MAX_PAGE = 99999


class ResolveError(Exception):
    """A WS13 resolve that fails closed, carrying the status it answers with.

    The status travels with the refusal because the three refusals mean
    different things to a caller: a malformed key is the caller's bug (400), a
    licensed copy with no attribution is a document we hold but may not serve
    (403), and neither shape being present is a document that is not stored
    (404). Collapsing them into one code turns "we cannot state this licence"
    into "no such document", which reads as a retryable miss.
    """

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def clip(value, limit=120):
    """Bound caller input echoed into an error message."""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def searchable_key_for(doc_id):
    """The one key an OCR'd copy of this document can live at.

    Derived from the digest, never read out of the request: pipelines/
    ws13_worker.py writes exactly this key, so a supplied searchable key can
    only agree with this string or be wrong.
    """
    return f"ws13/searchable/{doc_id[:2]}/{doc_id}/searchable.pdf"


def rights_for(admission_class, rights_basis):
    """Rights terms and obligation flags for one admission class.

    Mirrors ws13_query_lambda.rights_for(). admission_class is re-derived from
    the stored key rather than believed, but rights_basis cannot be: the key
    encodes the class, not the licensor. It is therefore required for the two
    classes whose licence names its source, and its absence is a refusal — the
    corpus is 13,013 CC BY-NC-SA copies and 32,312 archive research copies, and
    serving one without the attribution that makes it servable is the failure
    this gate exists for.
    """
    template = RIGHTS_BY_CLASS.get(admission_class)
    if template is None:
        raise ResolveError(
            400, f"unknown admission_class {clip(admission_class)!r}: refusing "
                 f"to sign a document with unknown rights")
    basis = str(rights_basis or "").strip() or None
    if "{basis}" in template["rights_terms"] and basis is None:
        raise ResolveError(
            403, f"admission_class {admission_class!r} has no rights_basis: its "
                 f"licence names the source, so this copy cannot be attributed "
                 f"and must not be served")
    return {
        "rights_basis": basis,
        "rights_terms": template["rights_terms"].format(basis=basis),
        "attribution_required": template["attribution_required"],
        "non_commercial": template["non_commercial"],
        "share_alike": template["share_alike"],
    }


def validate_ws13_request(params):
    """Validate a WS13 presign request into the fields the resolver may use.

    Nothing here is trusted for being well-formed elsewhere. The document id
    must be a full digest — this corpus has no manifest to disambiguate a
    prefix against, so an 8-character prefix that matched one of 56,282
    documents would be a guess. s3_key must fullmatch the WS12 original shape,
    which is both the object we may sign and the rights signal:
    ws13_documents.admission_class is GENERATED ALWAYS AS
    (split_part(s3_key, '/', 2)) STORED, so the class is read off the same
    split the database uses and a caller-supplied admission_class is only ever
    cross-checked against it.
    """
    doc_id = str(params.get("doc_id") or params.get("doc") or "").strip().lower()
    if not SHA256_RE.fullmatch(doc_id):
        raise ResolveError(
            400, f"doc_id {clip(doc_id)!r} must be the full 64-hex sha256 of "
                 f"the document; this corpus has no index to resolve a prefix "
                 f"against")
    s3_key = str(params.get("s3_key") or "").strip()
    if not s3_key:
        raise ResolveError(
            400, "s3_key is required: it names the stored original and, in its "
                 "ws12/{class}/ prefix, the rights that document is served "
                 "under")
    if len(s3_key.encode("utf-8")) > MAX_KEY_BYTES:
        raise ResolveError(400, "s3_key is longer than an S3 object key can be")
    match = WS12_ORIGINAL_KEY_RE.fullmatch(s3_key)
    if match is None:
        raise ResolveError(
            400, f"s3_key {clip(s3_key)!r} is not a stored WS12 original; it "
                 f"must be ws12/<originals|licensed-copies|research-copies>/"
                 f"<path>.pdf with no traversal")
    admission_class = match.group(1)
    # Where the key names its document by digest — ws12/{class}/{portal}/
    # {sha[:2]}/{sha}.pdf, the shape the bulk of the corpus is stored in —
    # that digest must be this document's. The searchable copy is keyed by
    # doc_id while the rights class is read off THIS key, so an unrelated pair
    # would serve one document's OCR text labelled with another document's
    # licence. The flat archive shape (ws12/research-copies/IF0131_001.pdf)
    # names no digest and cannot be checked this way, which is why the check is
    # conditional rather than a required key template.
    stem = s3_key.rsplit("/", 1)[-1][:-len(".pdf")].lower()
    if SHA256_RE.fullmatch(stem) and stem != doc_id:
        raise ResolveError(
            400, f"s3_key names document {stem[:12]} but doc_id is "
                 f"{doc_id[:12]}; a signature is only ever minted for one "
                 f"document's own stored objects")
    claimed_class = str(params.get("admission_class") or "").strip()
    if claimed_class and claimed_class != admission_class:
        raise ResolveError(
            400, f"admission_class {clip(claimed_class)!r} disagrees with the "
                 f"key, which is a {admission_class} document; the prefix is "
                 f"the rights signal and wins")
    viewer_key = str(params.get("viewer_key") or "").strip()
    if viewer_key and viewer_key not in (s3_key, searchable_key_for(doc_id)):
        # A viewer_key that names neither of this document's two objects is
        # either a mismatched pair of citation fields or an attempt to sign an
        # unrelated key; both must fail rather than be quietly ignored.
        raise ResolveError(
            400, f"viewer_key {clip(viewer_key)!r} names neither this "
                 f"document's searchable copy nor its stored original")
    kind_hint = str(params.get("viewer_key_kind") or "").strip()
    if kind_hint and kind_hint not in VIEWER_KEY_KINDS:
        raise ResolveError(
            400, f"viewer_key_kind must be one of {list(VIEWER_KEY_KINDS)}")
    variant = str(params.get("variant") or "searchable").strip()
    if variant not in doc_store.VARIANTS:
        raise ResolveError(
            400, f"variant must be one of {list(doc_store.VARIANTS)}")
    page = params.get("page")
    if page is None or str(page).strip() == "":
        page = 1
    try:
        page = int(page)
    except (TypeError, ValueError):
        raise ResolveError(400, "page must be an integer") from None
    if page < 1 or page > MAX_PAGE:
        # No page count is reachable from this function — the WS13 page counts
        # live in the database the retrieval Lambda reads — so this bounds the
        # value to what a citation chip can carry and leaves the end-of-document
        # check to the viewer, rather than silently clamping to page 1.
        raise ResolveError(400, f"page must be between 1 and {MAX_PAGE}")
    quote = str(params.get("quote") or "")[:MAX_QUOTE] or None
    return {
        "doc_id": doc_id,
        "s3_key": s3_key,
        "admission_class": admission_class,
        "kind_hint": kind_hint,
        "variant": variant,
        "page": page,
        "quote": quote,
        "rights_basis": str(params.get("rights_basis") or "").strip() or None,
    }


def assert_servable_key(key):
    """Last gate before a key is probed or signed: one of the two shapes only.

    validate_ws13_request() has already checked the key it was handed, so this
    can only fire on a bug in this module — which is why it is here. A
    signature is not revocable, and a HEAD on an arbitrary key is an existence
    oracle over the whole bucket, so every WS13 key passes this one chokepoint
    before either happens rather than relying on the caller-facing check having
    been the only way in.
    """
    if (WS13_SEARCHABLE_KEY_RE.fullmatch(key) is None and
            WS12_ORIGINAL_KEY_RE.fullmatch(key) is None):
        raise ResolveError(
            400, f"refusing to touch {clip(key)!r}: it is neither a "
                 f"ws13/searchable/ copy nor a ws12/ stored original")
    return key


def stored_object_length(key):
    """ContentLength of one stored object, or None when it is not there.

    HEAD is how the two shapes are told apart without a database: this function
    has no reach into ws13_documents, so "does the OCR output exist" is asked of
    the object store itself. It costs one extra HEAD (~15 ms) on every
    born-digital open, which is nothing beside the multi-megabyte PDF the same
    request is about to fetch, and it buys the guarantee that 'searchable' is
    only ever claimed for an object this function has seen.
    """
    try:
        head = s3.head_object(Bucket=BUCKET, Key=assert_servable_key(key))
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in MISSING_OBJECT_CODES:
            raise
        LOG.info("no stored object at %s (s3 answered %s)", key, code)
        return None
    length = head.get("ContentLength")
    return 0 if length is None else int(length)


def resolve_ws13_doc(request):
    """Resolve one WS13 document to the object it is actually served from.

    Two shapes, no data movement. ws13_documents.searchable_key is populated for
    all 28,988 ocr_queue documents and NULL for all 27,294 born_digital ones, so
    the servable object is the OCR output under ws13/searchable/ for the first
    group and the untouched original under ws12/ for the second — a born-digital
    original already carries its publisher's text layer, which is why the
    fallback is the right viewer and not a degraded one.

    Which shape a document is does not come from the request. The searchable key
    is derived from the digest and probed; only an object that answered a HEAD is
    served as 'searchable'. viewer_key_kind from the caller can therefore only
    ever WEAKEN the claim: it decides between 'born_digital_original' and
    'scanned_original_no_text_layer' once the searchable copy is known to be
    absent, and an absent or unrecognised hint takes the weaker of the two. The
    consequence of getting that backwards is not an error — the viewer offers
    text search over a raster scan and silently finds nothing.

    A document that answers neither HEAD is refused. Signing the key we would
    have guessed would hand the reader a signature that resolves to an S3
    AccessDenied body rendered as a broken PDF.
    """
    searchable = searchable_key_for(request["doc_id"])
    # variant 'raw' means here what it means for the legacy corpus: the stored
    # original as harvested. It skips the OCR copy rather than preferring it,
    # so a provenance view opens the file whose digest is the document id.
    if request["variant"] == "searchable":
        length = stored_object_length(searchable)
        if length is not None:
            return {"key": searchable, "kind": "searchable", "bytes": length}
    length = stored_object_length(request["s3_key"])
    if length is None:
        raise ResolveError(
            404, f"{request['doc_id'][:12]} is stored under neither shape: no "
                 f"searchable copy at {searchable} and no original at "
                 f"{request['s3_key']}")
    kind = ("born_digital_original"
            if request["kind_hint"] == "born_digital_original"
            else "scanned_original_no_text_layer")
    return {"key": request["s3_key"], "kind": kind, "bytes": length}


def ws13_viewer_url(request):
    """Deep link to our own viewer for a WS13 document.

    Same fragment discipline as viewer_url(): the parameters never reach a
    request line, a proxy log or a Referer header. The stored key and the rights
    basis ride along because this corpus has no manifest for the viewer to look
    a document up in — the viewer hands them straight back for a fresh
    signature, and they are re-validated then exactly as they were here. They
    grant nothing on their own: every request through this path is still gated
    on a live Cognito session.
    """
    if not SITE_URL:
        return None
    fragment = urllib.parse.urlencode({
        "corpus": WS13_CORPUS,
        "doc": request["doc_id"],
        "page": request["page"],
        "q": request["quote"] or "",
        "s3_key": request["s3_key"],
        "rights_basis": request["rights_basis"] or "",
    })
    return f"{SITE_URL}/viewer.html#{fragment}"


def ws13_response(params):
    """Presign one WS13 document, or fail closed with the reason.

    The manifest is never loaded on this path: the WS13 corpus is indexed in
    Postgres, and requiring the WS12 manifest here would make 56,282 documents
    unopenable whenever that one JSON object is unpublished. The gating it
    performs is not dropped, only replaced by the gate that fits this corpus —
    an anchored key shape, a rights class read off that key, and an attribution
    that must be statable before anything is signed.
    """
    try:
        request = validate_ws13_request(params)
        rights = rights_for(request["admission_class"], request["rights_basis"])
        resolved = resolve_ws13_doc(request)
        assert_servable_key(resolved["key"])
    except ResolveError as exc:
        return resp(exc.status, {"error": exc.message})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        return resp(502, {"error": f"could not read the document store: {code}"})
    filename = f'{request["doc_id"][:16]}-{resolved["kind"]}.pdf'
    try:
        url = presign(resolved["key"], filename)
    except ClientError as exc:
        return resp(502, {"error": f"could not sign the document URL: "
                                   f'{exc.response.get("Error", {}).get("Code", "")}'})
    return resp(200, {
        "corpus": WS13_CORPUS,
        "doc_id": request["doc_id"],
        # In WS13 the document id IS the sha256 of the raw original
        # (ws13_documents.sha256). The searchable copy has its own digest, which
        # only the OCR worker ever computed, so no digest is asserted for the
        # signed object itself.
        "sha256": request["doc_id"],
        "page": request["page"],
        "quote": request["quote"],
        "variant": request["variant"],
        "viewer_key_kind": resolved["kind"],
        "text_layer": TEXT_LAYER_BY_KIND[resolved["kind"]],
        "file_url": url,
        "bytes": resolved["bytes"],
        "expires_in": PRESIGN_TTL,
        # The same three fields infra/ws13_query_lambda.py puts on a citation,
        # carried through so the viewer can print the licence beside the page
        # instead of implying public domain by silence.
        "admission_class": request["admission_class"],
        "rights_basis": rights["rights_basis"],
        "rights_terms": rights["rights_terms"],
        "attribution_required": rights["attribution_required"],
        "non_commercial": rights["non_commercial"],
        "share_alike": rights["share_alike"],
        "viewer_url": ws13_viewer_url(request),
    })


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

    # Both corpora sit in the same bucket behind the same Cognito check; they
    # differ only in what a document is looked up in, so the branch is taken
    # before the WS12 manifest is loaded rather than inside it.
    corpus = str(params.get("corpus") or LEGACY_CORPUS).strip()
    if corpus not in CORPORA:
        return resp(400, {"error": f"corpus must be one of {list(CORPORA)}"})
    if corpus == WS13_CORPUS:
        return ws13_response(params)

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
