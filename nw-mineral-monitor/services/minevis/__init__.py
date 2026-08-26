"""minevis — the HTTP service an agent on this box calls to turn a written mine
description into a 3-D model.

  server   stdlib ThreadingHTTPServer: /tools, /call, /jobs/<id>, /healthz
  tools    the OpenAI function-schema array and the dispatch behind it
  jobs     an on-disk job store, so a restart resumes a build rather than
           losing it

Everything below the service layer (parsing, resolving, building, rendering,
publishing) lives in ``pipelines/geomodel`` and is pure standard library.
"""
__version__ = '1.0.0'
