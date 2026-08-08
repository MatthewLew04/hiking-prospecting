// Heap/storage measurement harness — runs the same toggle script against
// the before-fix and after-fix builds and prints a comparison table.
const { chromium } = require('playwright');
const http = require('http');
const path = require('path');
const fs = require('fs');

const DARK_PX = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XLTwAAAABJRU5ErkJggg==',
  'base64'); // near-black 1x1 (NOT the classic red pixel)

function serve(dir, port){
  const mime = {'.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.json':'application/json', '.png':'image/png'};
  return new Promise(res => {
    const s = http.createServer((rq, rs) => {
      let p = decodeURIComponent(rq.url.split('?')[0]);
      if (p === '/') p = '/index.html';
      const f = path.join(dir, p);
      fs.readFile(f, (e, b) => {
        if (e){ rs.writeHead(404); return rs.end(); }
        rs.writeHead(200, {'content-type': mime[path.extname(f)] || 'application/octet-stream'});
        rs.end(b);
      });
    }).listen(port, () => res(s));
  });
}

async function sample(page, label){
  try{
    const s = await page.evaluate(async () => {
      if (window.gc) { window.gc(); await new Promise(r=>setTimeout(r,300)); window.gc(); }
      const est = await navigator.storage.estimate().catch(()=>({}));
      const src = {};
      for (const k of ['claimsA','claimsC','mrds','stategeo','usmin']){
        try{
          if (window.__pushed && k in window.__pushed){ src[k] = window.__pushed[k]; continue; }
          const s = map.getSource(k);
          src[k] = s && s._data && s._data.features ? s._data.features.length :
                   Object.entries(stores).filter(([sk,d])=>sk.startsWith(k+'_')&&d._feats)
                     .reduce((a,[,d])=>a+d._feats.length, 0);
        }catch(e){ src[k] = -1; }
      }
      return { heapMB: performance.memory ? +(performance.memory.usedJSHeapSize/1048576).toFixed(0) : null,
               storMB: est.usage ? +(est.usage/1048576).toFixed(1) : 0, src };
    });
    const f = Object.entries(s.src).map(([k,v])=>`${k}:${v>=0?v.toLocaleString():'?'}`).join(' ');
    console.log(`  ${label.padEnd(34)} heap ${String(s.heapMB).padStart(5)} MB   origin-storage ${s.storMB} MB   [${f}]`);
    return s;
  }catch(e){
    console.log(`  ${label.padEnd(34)} *** RENDERER DEAD: ${e.message.split('\n')[0].slice(0,80)}`);
    return null;
  }
}

async function run(name, port){
  console.log(`\n===== ${name} =====`);
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/opt/pw-browsers/chromium',
    args: ['--js-flags=--expose-gc', '--enable-precise-memory-info',
           '--use-gl=angle', '--use-angle=swiftshader-webgl', '--disable-dev-shm-usage'],
  });
  const page = await browser.newPage({viewport:{width:1440, height:900}});
  page.on('crash', () => console.log('  !!! page CRASHED (renderer killed)'));
  // stub every external tile/service; allow only localhost
  await page.route(/^https?:\/\/(?!localhost)/, r => r.fulfill({status:204, body:''}));
  const t0 = Date.now();
  await page.goto(`http://localhost:${port}/?debug=1`, {waitUntil:'domcontentloaded', timeout:60000});
  // boot loads sites + active claims for all 8 states by default
  let last = -1, stable = 0;
  for (let i=0; i<150 && stable<3; i++){
    await page.waitForTimeout(5000);
    const n = await page.evaluate(() => { try{ return Object.keys(stores).length; }catch(e){ return -2; } }).catch(()=>-2);
    if (n === last && n > 0) stable++; else stable = 0;
    last = n;
  }
  console.log(`  boot ${(Date.now()-t0)/1000|0}s — all 8 states, active claims + sites default-on`);
  await sample(page, 'after boot (z4.9, statewide)');

  const step = async (label, fn) => {
    try{ await page.evaluate(fn); }catch(e){ console.log(`  ${label} — EVAL FAILED: ${e.message.split('\n')[0].slice(0,70)}`); return null; }
    await page.waitForTimeout(2600);
    return sample(page, label);
  };
  await step('closed claims ON (1.3M rows)', async () => { S.layers.claimsC = true; await loadClaimsClosed(); applyFilters(); });
  await step('jump Cassia z9', () => { map.jumpTo({center:[-113.75,42.25], zoom:9}); });
  await step('jump Elko NV z9 (dense)', () => { map.jumpTo({center:[-115.75,40.85], zoom:9}); });
  await step('jump Grass Valley CA z10', () => { map.jumpTo({center:[-121.06,39.21], zoom:10}); });
  await step('zoom out z5 (coarse band)', () => { map.jumpTo({center:[-114.6,42.6], zoom:5}); });
  await step('toggle all layers OFF', async () => {
    for (const k of ['claimsA','claimsC','mrds','stategeo','usmin']) S.layers[k] = false;
    if (typeof repushAll === 'function'){ repushAll(true); } else { for (const k of ['claimsA','claimsC']) pushKind(k); }
    applyFilters();
  });
  await step('toggle back ON + z9 Cassia', async () => {
    for (const k of ['claimsA','claimsC','mrds','stategeo','usmin']) S.layers[k] = true;
    map.jumpTo({center:[-113.75,42.25], zoom:9});
    await loadSites(); await loadClaimsActive(); await loadClaimsClosed();
    if (typeof repushAll === 'function') repushAll(true);
    applyFilters();
  });
  await browser.close();
}

(async () => {
  const which = process.argv[2] || 'after';
  if (which === 'before'){
    const s1 = await serve('/tmp/before_site', 8121);
    await run('BEFORE (build 2026-08-07g + CA data)', 8121);
    s1.close();
  } else {
    const s2 = await serve('/home/claude/nwmm/site', 8122);
    await run('AFTER  (build 2026-08-08b)', 8122);
    s2.close();
  }
  process.exit(0);
})();
