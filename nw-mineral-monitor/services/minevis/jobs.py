"""minevis.jobs — an on-disk job store with a thread pool in front of it.

A build fetches terrain tiles the first time it sees a district, so it is too
slow to answer inside an HTTP request.  Jobs are therefore files: one JSON
document per job, written atomically, holding everything needed to run it.
That is what makes a restart survivable — a job that was ``running`` when the
process died is re-queued on the next start, because its arguments are still
on disk.

States:  queued -> running -> done | questions | error
``questions`` is not a failure.  It is the normal outcome when the description
left something open, and it carries the questions the agent must answer.
"""
import json
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

STATES = ('queued', 'running', 'done', 'questions', 'error')

JOB_ID = re.compile(r'^j-[0-9a-f]{16}$')


def _utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _atomic_write(path, data):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class JobStore(object):
    """Job records on disk, plus the pool that runs them."""

    def __init__(self, root, workers=2, runner=None, log=print):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, 'jobs')
        os.makedirs(self.dir, exist_ok=True)
        self.runner = runner
        self.log = log
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(workers)),
                                        thread_name_prefix='minevis-job')
        self._closed = False

    # ------------------------------------------------------------- records
    def _path(self, job_id):
        if not JOB_ID.match(job_id or ''):
            raise KeyError(job_id)
        return os.path.join(self.dir, '%s.json' % job_id)

    def read(self, job_id):
        try:
            with open(self._path(job_id), encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, IOError, ValueError):
            return None

    def _write(self, rec):
        rec['updated_utc'] = _utc()
        _atomic_write(self._path(rec['job_id']),
                      json.dumps(rec, sort_keys=True, default=str).encode('utf-8'))
        return rec

    def update(self, job_id, **fields):
        with self._lock:
            rec = self.read(job_id)
            if rec is None:
                raise KeyError(job_id)
            rec.update(fields)
            return self._write(rec)

    # -------------------------------------------------------------- submit
    def submit(self, name, arguments):
        """Record the job before running it, so it survives a restart."""
        job_id = 'j-%s' % uuid.uuid4().hex[:16]
        rec = {'job_id': job_id, 'name': name, 'arguments': arguments, 'state': 'queued',
               'created_utc': _utc(), 'attempts': 0}
        with self._lock:
            self._write(rec)
        self._dispatch(job_id)
        return job_id

    def _dispatch(self, job_id):
        if self._closed:
            return
        self._pool.submit(self._run, job_id)

    def _run(self, job_id):
        rec = self.read(job_id)
        if rec is None:
            return
        self.update(job_id, state='running', attempts=rec.get('attempts', 0) + 1)
        try:
            state, result = self.runner(rec['name'], rec['arguments'])
        except Exception as exc:                       # a job must never kill the pool
            self.log('job %s failed: %s: %s' % (job_id, type(exc).__name__, exc))
            self.update(job_id, state='error',
                        result={'error': type(exc).__name__, 'detail': str(exc)})
            return
        self.update(job_id, state=state, result=result)

    # ------------------------------------------------------------- restart
    def resume(self):
        """Re-queue anything that was mid-flight when the process last stopped.
        Returns the ids it picked back up."""
        picked = []
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith('.json') or name.startswith('.'):
                continue
            job_id = name[:-5]
            rec = self.read(job_id)
            if rec is None or rec.get('state') not in ('queued', 'running'):
                continue
            if rec.get('attempts', 0) >= 3:
                self.update(job_id, state='error',
                            result={'error': 'abandoned',
                                    'detail': 'restarted %d times without finishing'
                                              % rec['attempts']})
                continue
            self.update(job_id, state='queued')
            picked.append(job_id)
        for job_id in picked:
            self._dispatch(job_id)
        return picked

    def close(self, wait=True):
        self._closed = True
        self._pool.shutdown(wait=wait)


class SpecStore(object):
    """Parsed specs, keyed by the spec id the parser hands out, alongside the
    mine they were resolved against.  This is what lets the second half of a
    question round trip say only ``spec_id`` and ``answers``."""

    def __init__(self, root):
        self.dir = os.path.join(os.path.abspath(root), 'specs')
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, spec_id):
        if not re.match(r'^s[0-9a-f]{8}$', spec_id or ''):
            raise KeyError(spec_id)
        return os.path.join(self.dir, '%s.json' % spec_id)

    def put(self, spec, site=None):
        with self._lock:
            _atomic_write(self._path(spec['spec_id']),
                          json.dumps({'spec': spec, 'site': site}, sort_keys=True,
                                     default=str).encode('utf-8'))
        return spec['spec_id']

    def get(self, spec_id):
        try:
            with open(self._path(spec_id), encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, IOError, ValueError):
            return None
