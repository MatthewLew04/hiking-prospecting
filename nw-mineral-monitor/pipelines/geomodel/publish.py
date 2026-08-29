"""geomodel.publish — put a built model somewhere the viewer can open it.

The browser side of this feature is already deployed: ``site/model3d.html``
accepts ``?project=<url>`` and loads whatever it finds there, so publishing a
model is only "write some files and return a link".

What this module needs from its environment is narrow and stated once, because
the AWS surface (the IAM role, the bucket policy, the deploy excludes) belongs
to a different piece of work:

    the process can PUT objects under ``<bucket>/models/*`` and knows the
    bucket name and the public base URL

Anything satisfying :class:`Target` does.  :class:`S3Target` is the real one;
:class:`LocalTarget` writes to a directory and is what the tests use, so
nothing here is blocked on an AWS change landing.

Key layout::

    models/<mine-slug>-<hash8>/model.geomodel.json   <- the viewer opens this
                              /model.omf
                              /workings.dxf
                              /workings.geojson
                              /plan.svg /section.svg /iso.svg
                              /manifest.json

``hash8`` is content addressed over *(normalised spec + resolved mine +
builder version)*, so one description always lands on one URL and republishing
the same description is a no-op rather than a second model.

``manifest.json`` is the audit trail: the hash of the input text, every quote
with its character span, the confidence of every field, and — separately —
each answer an agent supplied, with the justification it gave.  A reader can
tell which numbers came from the document and which came from the model.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

from . import agentbuild
from . import mapplate
from . import narrative
from . import render2d

PUBLISHER_VERSION = 'nwmm-publish/1'

PREFIX = 'models'

#: Phase 5.  ``private/`` is absent from the CloudFront read allowlist in
#: infra/template.yaml, so a model written here is unreachable through the
#: distribution by construction — the only way in is a presigned GET, exactly
#: as the WS12 document store works.  The bucket's CORS rule is already
#: bucket-wide and GET/HEAD only, with the signature doing the authorising, so
#: model3d.html can fetch a presigned project cross-origin unchanged.
PRIVATE_PREFIX = 'private/models'

#: seconds a presigned link stays good; the same default and clamp the document
#: store uses, because the same reasoning applies — long enough to open, short
#: enough that a copied URL stops working
PRESIGN_TTL = 300
PRESIGN_MIN, PRESIGN_MAX = 30, 3600

CONTENT_TYPES = {
    '.json': 'application/json',
    '.geojson': 'application/geo+json',
    '.svg': 'image/svg+xml',
    '.omf': 'application/octet-stream',
    '.dxf': 'application/dxf',
}

#: the order files are written in, so a manifest reads the same way every time
FILE_ORDER = ('model.geomodel.json', 'model.omf', 'workings.dxf', 'workings.geojson',
              'plan.svg', 'section.svg', 'iso.svg', 'manifest.json')


class PublishError(RuntimeError):
    pass


# ------------------------------------------------------------------ targets
class Target(object):
    """Somewhere object bytes can be put and looked for."""

    def put(self, key, data, content_type):
        raise NotImplementedError

    def get(self, key):
        """Bytes, or ``None`` when the key is absent."""
        raise NotImplementedError

    def presign(self, key, expires_in):
        """A time-limited read URL, or ``None`` when this target cannot mint
        one.  ``None`` is an answer, not a failure: it means the caller must
        not claim the object is privately reachable."""
        return None


class LocalTarget(Target):
    """A directory that stands in for the bucket.  Used by the tests, and by
    ``--out`` on the command line."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.puts = []

    def _path(self, key):
        return os.path.join(self.root, *key.split('/'))

    def put(self, key, data, content_type):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(data)
        self.puts.append(key)
        return key

    def get(self, key):
        try:
            with open(self._path(key), 'rb') as fh:
                return fh.read()
        except (OSError, IOError):
            return None


class S3Target(Target):
    """The bucket.  Uses boto3 when it is importable and the ``aws`` CLI when
    it is not; both take their credentials from the instance role, so nothing
    here handles a secret."""

    def __init__(self, bucket, prefix=PREFIX, client=None, prefixes=None):
        if not bucket:
            raise PublishError('no bucket: pass bucket= or set NWMM_MODELS_BUCKET')
        self.bucket = bucket
        self.prefix = prefix
        self.prefixes = tuple(prefixes or (PREFIX, PRIVATE_PREFIX))
        self.puts = []
        self._client = client
        if client is None:
            try:
                import boto3                                    # noqa: F401
                self._client = boto3.client('s3')
            except Exception:
                self._client = None

    def put(self, key, data, content_type):
        if not any(key.startswith(p + '/') for p in self.prefixes):
            raise PublishError('refusing to write outside %s: %r'
                               % ('/, '.join(self.prefixes) + '/', key))
        if self._client is not None:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                    ContentType=content_type)
        else:
            self._cli(['s3api', 'put-object', '--bucket', self.bucket, '--key', key,
                       '--content-type', content_type, '--body', '-'], data)
        self.puts.append(key)
        return key

    def get(self, key):
        if self._client is not None:
            try:
                return self._client.get_object(Bucket=self.bucket, Key=key)['Body'].read()
            except Exception:
                return None
        try:
            return self._cli(['s3', 'cp', 's3://%s/%s' % (self.bucket, key), '-'], None)
        except PublishError:
            return None

    def presign(self, key, expires_in):
        seconds = clamp_ttl(expires_in)
        if self._client is not None:
            try:
                return self._client.generate_presigned_url(
                    'get_object', Params={'Bucket': self.bucket, 'Key': key},
                    ExpiresIn=seconds)
            except Exception:
                return None
        try:
            out = self._cli(['s3', 'presign', 's3://%s/%s' % (self.bucket, key),
                             '--expires-in', str(seconds)], None)
        except PublishError:
            return None
        return out.decode('utf-8', 'replace').strip() or None

    def _cli(self, args, data):
        try:
            proc = subprocess.run(['aws'] + args, input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise PublishError('neither boto3 nor the aws CLI is available on this box')
        if proc.returncode != 0:
            raise PublishError('aws %s failed: %s' % (args[0], proc.stderr.decode('utf-8', 'replace').strip()))
        return proc.stdout


def clamp_ttl(expires_in):
    try:
        seconds = int(expires_in if expires_in is not None else PRESIGN_TTL)
    except (TypeError, ValueError):
        seconds = PRESIGN_TTL
    return max(PRESIGN_MIN, min(PRESIGN_MAX, seconds))


def target_from_env(bucket=None, prefix=PREFIX):
    """The bucket named by the caller, or by ``NWMM_MODELS_BUCKET``."""
    return S3Target(bucket or os.environ.get('NWMM_MODELS_BUCKET'), prefix=prefix)


def base_url_from_env(base_url=None):
    url = base_url or os.environ.get('NWMM_SITE_URL') or ''
    return url.rstrip('/')


# ------------------------------------------------------------- content address
def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or 'mine').lower()).strip('-')
    return (s or 'mine')[:48].strip('-')


def _normal_spec(spec):
    """What the model id is computed over: the elements and the answers, with
    the parse-time bookkeeping (coverage counts, remaining questions) left out,
    because they do not change the geometry."""
    return {
        'parser': spec.get('parser_version'),
        'text_sha256': spec.get('text_sha256'),
        'elements': spec.get('elements') or [],
        'answers': spec.get('answers') or [],
        'assays': spec.get('assays') or [],
        'vein': spec.get('vein'),
        'plates': [{'plate_id': p.get('plate_id'), 'usable': p.get('usable'),
                    'traces': p.get('traces')} for p in (spec.get('plates') or [])],
    }


def model_id(spec, site, builder_version=agentbuild.BUILDER_VERSION):
    """``silver-king-9f2c1e0a`` — stable for one description of one mine."""
    payload = {
        'spec': _normal_spec(spec),
        'mine': {'mine_id': site.get('mine_id'), 'lon': site.get('lon'), 'lat': site.get('lat'),
                 'elevation_m': site.get('elevation_m')},
        'builder': builder_version,
        'publisher': PUBLISHER_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return '%s-%s' % (slugify(site.get('name') or site.get('mine_id')),
                      hashlib.sha256(blob.encode('utf-8')).hexdigest()[:8])


# ------------------------------------------------------------------ manifest
def manifest(built, spec, site, files, mid, content_hash, now=None, access='app-gate'):
    """The audit trail.  Everything a reader needs in order to check a number
    in the model against the sentence it came from."""
    placed = dict((p['element'], p) for p in built['placed'])
    elements = []
    for el in spec.get('elements') or []:
        rec = {'id': el['id'], 'kind': el['kind'], 'name': el.get('name', ''),
               'confidence': el.get('confidence', 'described'),
               'fields': dict(el.get('fields') or {}),
               'definitional_defaults': list(el.get('defaults') or []),
               'quote': el.get('quote', ''), 'span': el.get('span'),
               'built': el['id'] in placed}
        if el['id'] in placed:
            rec['placement'] = placed[el['id']]['placement']
            if placed[el['id']].get('note'):
                rec['placement_note'] = placed[el['id']]['note']
        elements.append(rec)

    return {
        'schema': 'nwmm-model-manifest/1',
        'model_id': mid,
        'access': access,
        'published_utc': now or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'versions': {'parser': narrative.PARSER_VERSION, 'builder': agentbuild.BUILDER_VERSION,
                     'renderer': render2d.RENDERER_VERSION, 'publisher': PUBLISHER_VERSION},
        'mine': {k: site.get(k) for k in
                 ('mine_id', 'name', 'state', 'district', 'county', 'commodity',
                  'lon', 'lat', 'elevation_m', 'elevation_source', 'terrain_zoom',
                  'basis', 'years', 'quote', 'source', 'source_url')},
        'input': {'spec_id': spec.get('spec_id'), 'text_sha256': spec.get('text_sha256'),
                  'mine_id': spec.get('mine_id')},
        'coverage': spec.get('coverage') or {},
        'confidence': built.get('confidence') or {},
        'levels': built.get('levels') or {},
        'collar': built.get('collar') or {},
        'crs': built.get('crs') or {},
        'summary': built.get('summary') or {},
        'elements': elements,
        # grades quoted in the text, with the basis kept: a selected sample is
        # not an average, and flattening the two would misdescribe the deposit
        'assays': list(spec.get('assays') or []),
        'vein': built.get('vein'),
        # the scanned plates any surveyed geometry was traced off, with the
        # georeference that was used and how well its control points agreed
        'plates': [{'plate_id': p.get('plate_id'), 'plane': p.get('plane'),
                    'image': p.get('image'), 'source': p.get('source'),
                    'usable': p.get('usable'), 'scale': p.get('scale'),
                    'traces': [{'id': t['id'], 'kind': t['kind'], 'name': t['name'],
                                'points': len(t['points'])} for t in (p.get('traces') or [])]}
                   for p in (spec.get('plates') or [])],
        # workings the text named but never described: kept so a reader can see
        # that they were noticed and deliberately not built
        'mentions': [{'id': m['id'], 'kind': m['kind'], 'count': m.get('count'),
                      'quote': m.get('quote', ''), 'span': m.get('span')}
                     for m in (spec.get('mentions') or [])],
        # answers are listed apart from the elements on purpose: this is the
        # list of numbers that did NOT come out of the source document
        'answers': list(spec.get('answers') or []),
        'unresolved': [dict(g) for g in narrative.unresolved(spec)] + list(built.get('gaps') or []),
        'warnings': list(built.get('warnings') or []),
        'content_sha256': content_hash,
        'files': files,
        'notes': [
            'Workings were digitised from a written description, not from a survey.',
            'confidence: surveyed = traced off a georeferenced plan; described = read off '
            'the source text; assumed = supplied in answer to a question the text left open.',
            'Levels are placed at their named depth below the collar.',
            'Every element carries the sentence it came from and that sentence\'s character '
            'span in the input text.',
            'mentions are workings the text names without describing; they were not built.',
            'surveyed elements were traced off a georeferenced plate listed under "plates"; '
            'their geometry is the survey\'s, not this builder\'s.',
            'assay "basis" separates a selected sample from an average; no grade surface is '
            'interpolated from quoted figures.',
            'access "app-gate" means the model sits under the public models/ prefix and is '
            'reachable by anyone who can construct its URL; "presigned" means it sits under '
            'private/ and is reachable only through a short-lived signed link.',
        ],
    }


# ------------------------------------------------------------------- publish
def publish(built, spec, site, views=None, target=None, base_url=None, prefix=None,
            force=False, private=False, expires_in=None, log=print):
    """Write the model and return the link the agent hands back.

    Republishing the same description is a no-op: the model id is derived from
    the content, so an unchanged rebuild finds its own manifest already in
    place and returns the same URL with ``republished: False``.
    """
    target = target or target_from_env()
    base_url = base_url_from_env(base_url)
    views = views if views is not None else render2d.render(built)
    if prefix is None:
        prefix = PRIVATE_PREFIX if private else PREFIX
    expires_in = clamp_ttl(expires_in) if private else None

    mid = model_id(spec, site)
    key_root = '%s/%s' % (prefix.rstrip('/'), mid)
    content_hash = agentbuild.content_sha256(built['project'])

    existing = target.get('%s/manifest.json' % key_root)
    if existing and not force:
        try:
            prior = json.loads(existing.decode('utf-8'))
        except ValueError:
            prior = {}
        if prior.get('content_sha256') == content_hash:
            log('%s already published, unchanged' % mid)
            return _result(mid, key_root, base_url, prior, republished=False,
                           target=target, private=private, expires_in=expires_in)

    payloads = _payloads(built, views)
    files = []
    for name in FILE_ORDER:
        if name not in payloads:
            continue
        data = payloads[name]
        files.append({'name': name, 'key': '%s/%s' % (key_root, name), 'bytes': len(data),
                      'sha256': hashlib.sha256(data).hexdigest(),
                      'content_type': _content_type(name)})

    # `files` deliberately does not list manifest.json: a manifest cannot carry
    # its own checksum, and leaving it out is what keeps the freshly published
    # result and a later republished one describing the same set of files.
    man = manifest(built, spec, site, files, mid, content_hash,
                   access='presigned' if private else 'app-gate')
    payloads['manifest.json'] = json.dumps(man, indent=1, sort_keys=True,
                                           default=str).encode('utf-8')

    for name in FILE_ORDER:
        if name not in payloads:
            continue
        target.put('%s/%s' % (key_root, name), payloads[name], _content_type(name))
        log('  put %s/%s (%d bytes)' % (key_root, name, len(payloads[name])))

    return _result(mid, key_root, base_url, man, republished=True,
                   target=target, private=private, expires_in=expires_in)


def _payloads(built, views):
    """Every file as bytes, without touching the filesystem."""
    import tempfile

    out = {}
    with tempfile.TemporaryDirectory() as d:
        for name, _ in agentbuild.write_exports(built, d):
            with open(os.path.join(d, name), 'rb') as fh:
                out[name] = fh.read()
    for name, svg in (views or {}).items():
        out['%s.svg' % name] = svg.encode('utf-8')
    return out


def _content_type(name):
    return CONTENT_TYPES.get(os.path.splitext(name)[1], 'application/octet-stream')


def _result(mid, key_root, base_url, man, republished, target=None, private=False,
            expires_in=None):
    names = [f['name'] for f in man.get('files', [])]
    link = _linker(key_root, base_url, target, private, expires_in)

    out = {
        'model_id': mid,
        'key_prefix': key_root,
        'republished': republished,
        'access': 'presigned' if private else 'app-gate',
        # a public model keeps the documented root-relative ?project= form; a
        # private one has to carry its whole signed URL in that parameter
        'model_url': _viewer_url(base_url,
                                 link('model.geomodel.json') if private
                                 else '/%s/model.geomodel.json' % key_root,
                                 private),
        'project_url': link('model.geomodel.json'),
        'views': dict((v, link('%s.svg' % v)) for v in ('plan', 'section', 'iso')
                      if '%s.svg' % v in names),
        'exports': dict((n, link(n)) for n in names
                        if n not in ('plan.svg', 'section.svg', 'iso.svg')),
        'manifest_url': link('manifest.json'),
        'confidence': man.get('confidence', {}),
        'unresolved': man.get('unresolved', []),
        'warnings': man.get('warnings', []),
        'content_sha256': man.get('content_sha256'),
    }
    if private:
        out['expires_in'] = expires_in
        out['expires_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                           time.gmtime(time.time() + (expires_in or 0)))
        if out['project_url'] and out['project_url'].startswith('http'):
            out['note'] = ('this model is private: the links above are signed and stop '
                           'working in %d seconds. Call sign_model_url to mint fresh ones.'
                           % expires_in)
        else:
            out['note'] = ('this model was written to the private prefix, but this storage '
                           'target cannot mint signed links, so the paths above are not '
                           'usable by a browser')
    return out


def _linker(key_root, base_url, target, private, expires_in):
    """How one file inside the model is addressed from outside."""
    def link(name):
        key = '%s/%s' % (key_root, name)
        if private:
            signed = target.presign(key, expires_in) if target is not None else None
            return signed if signed else '/' + key
        return ('%s/%s' % (base_url, key)) if base_url else '/' + key
    return link


def _viewer_url(base_url, project_url, private):
    """model3d.html already accepts ?project=<url>; a signed URL carries its own
    query string, so it has to be encoded into that parameter."""
    param = urllib.parse.quote(project_url, safe='') if private else project_url
    return ('%s/model3d.html?project=%s' % (base_url, param)) if base_url \
        else 'model3d.html?project=%s' % param


def sign(model_id, target=None, base_url=None, prefix=PRIVATE_PREFIX, expires_in=None):
    """Mint a fresh set of signed links for a model already published to the
    private prefix, without rebuilding it."""
    target = target or target_from_env()
    base_url = base_url_from_env(base_url)
    expires_in = clamp_ttl(expires_in)
    key_root = '%s/%s' % (prefix.rstrip('/'), model_id)
    raw = target.get('%s/manifest.json' % key_root)
    if raw is None:
        raise PublishError('no model %r under %s/' % (model_id, prefix))
    try:
        man = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PublishError('the manifest for %r is unreadable: %s' % (model_id, exc))
    out = _result(model_id, key_root, base_url, man, republished=False, target=target,
                  private=True, expires_in=expires_in)
    out['published_utc'] = man.get('published_utc')
    return out
