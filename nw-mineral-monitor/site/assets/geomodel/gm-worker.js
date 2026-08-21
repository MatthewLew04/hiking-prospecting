/* gm-worker.js — runs gm-engine.js off the main thread.

   Browser:  const w = new Worker(new URL('./gm-worker.js', import.meta.url), {type: 'module'});
   Node:     const w = new (await import('node:worker_threads')).Worker(new URL('./gm-worker.js', import.meta.url));
   (or just use EngineClient from gm-engine.js, which does the bookkeeping.)

   Protocol (one message per call):
     -> {id, op, args}              args: plain JSON + raw typed arrays + packed gm-core
                                    objects (GM.packObject output, recognised by .kind,
                                    unpacked recursively inside arrays / objects)
     <- {id, progress: 0..1, note}  zero or more times while the op runs
     <- {id, ok: true, result}      gm-core objects in the result are packed; large
                                    typed arrays are transferred, not copied
     <- {id, ok: false, error, stack}
   Ops: see OPS in gm-engine.js (gridFromPoints, buildStratigraphy, stratigraphyVolumes,
   stratigraphySection, empiricalVariogram, fitVariogram, estimate, gradeTonnage,
   tagBlockModel, isosurface, implicitSurface, meshPlaneIntersection,
   blockmodelPlaneSample, linesetNearPlane, desurvey, composite, ...). */

import { runOp, packValue, unpackValue, collectTransferables } from './gm-engine.js';

// a real browser worker scope (not a page that happened to import this file on its main thread)
const inBrowser = typeof self !== 'undefined' && typeof self.postMessage === 'function'
  && typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope;

function serve(port) {
  port.listen(async msg => {
    if (!msg || msg.id == null) return;
    const { id, op, args } = msg;
    let lastProgress = -1;
    const progress = (fraction, note) => {
      const f = Math.max(0, Math.min(1, +fraction || 0));
      if (f === lastProgress && note == null) return;
      lastProgress = f;
      port.post({ id, progress: f, note: note == null ? undefined : String(note) });
    };
    try {
      const result = await runOp(op, unpackValue(args), progress);
      const packed = packValue(result);
      port.post({ id, ok: true, result: packed }, collectTransferables(packed));
    } catch (e) {
      port.post({ id, ok: false, error: String((e && e.message) || e), stack: e && e.stack ? String(e.stack) : undefined });
    }
  });
}

if (inBrowser) {
  serve({
    post: (m, transfer) => (transfer && transfer.length ? self.postMessage(m, transfer) : self.postMessage(m)),
    listen: fn => { self.onmessage = e => fn(e.data); },
  });
} else {
  import('node:worker_threads').then(({ parentPort }) => {
    if (!parentPort) return;                      // imported on the main thread: nothing to serve
    serve({
      post: (m, transfer) => parentPort.postMessage(m, transfer && transfer.length ? transfer : undefined),
      listen: fn => parentPort.on('message', fn),
    });
  }).catch(() => { /* neither a browser worker nor a node worker thread */ });
}
