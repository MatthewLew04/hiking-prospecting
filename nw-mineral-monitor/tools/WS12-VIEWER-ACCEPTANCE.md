# WS12 citation viewer browser acceptance

Run the real IF0126 citation path locally with:

```sh
npm run test:doc-viewer
```

The test requires the ignored document store produced by
`pipelines/build_doc_store.py`. It SHA-256 verifies the actual IF0126
searchable PDF against `var/ws12/document-store-manifest.json`, serves the repository
through `tools/range_server.py`, and mocks only the authenticated Docs API
resolution response.

The browser runs at a 390 × 844 touch viewport. Acceptance requires a rendered
page-1 canvas with non-blank page art, the reviewed citation quote highlighted
in the PDF.js text layer, and no horizontal mobile overflow. Both publisher
host variants are blocked before the viewer opens; an explicit publisher fetch
must fail while the stored citation remains rendered and highlighted.

Set `NWMM_DOC_VIEWER_PORT` only when a fixed local port is required. By default
the harness asks the operating system for an unused port.
