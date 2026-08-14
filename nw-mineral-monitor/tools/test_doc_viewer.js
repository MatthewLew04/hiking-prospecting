#!/usr/bin/env node
/* Browser acceptance for the WS12 citation-to-PDF path.

   The test deliberately uses the checked-in viewer and the real, ignored
   IF0126 searchable artifact produced by pipelines/build_doc_store.py. It
   mocks only the authenticated Docs API resolver; PDF.js reads the artifact
   itself through tools/range_server.py, including byte-range delivery.

   Publisher hosts are blocked before navigation. After the stored copy has
   rendered, the test also attempts a publisher fetch and requires that it be
   aborted, proving that a dead portal does not affect an already-resolvable
   citation. */
'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require('playwright');

const ROOT = path.resolve(__dirname, '..');
const MANIFEST_FILE = path.join(ROOT, 'var', 'ws12', 'document-store-manifest.json');
const STORE = path.join(ROOT, 'pipelines', 'cache', 'ws12', 'store');
const MINE_ID = 'stategeo-igs-dd-1-if0126';
const ACCESS_TOKEN = 'ws12-browser-acceptance-token';
const VIEWPORT = {width: 390, height: 844};

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function browserExecutable() {
  const candidates = [
    process.env.CHROME_PATH,
    chromium.executablePath(),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ];
  return candidates.find(candidate => candidate && fs.existsSync(candidate));
}

function loadFixture() {
  assert.ok(fs.existsSync(MANIFEST_FILE),
    `document manifest is missing: ${MANIFEST_FILE}`);
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_FILE, 'utf8'));
  const citation = manifest.citations.find(item =>
    item.mine_id === MINE_ID && item.page === 1 && item.quote_located === true &&
    item.quote.includes('LAVA CREEK DISTRICT'));
  assert.ok(citation, 'the reviewed IF0126 page-1 Lava Creek citation is missing');

  const document = manifest.documents.find(item => item.doc_id === citation.doc_id);
  assert.ok(document, `manifest document ${citation.doc_id} is missing`);
  assert.equal(document.mine_id, MINE_ID);
  assert.equal(document.pages, 1);
  assert.equal(document.pagination_preserved, true);

  const file = path.resolve(STORE, document.searchable.key);
  const storePrefix = `${path.resolve(STORE)}${path.sep}`;
  assert.ok(file.startsWith(storePrefix), 'searchable key escaped the WS12 store');
  assert.ok(fs.existsSync(file),
    `real IF0126 searchable artifact is missing: ${file}\n` +
    'Run: python3 pipelines/build_doc_store.py');
  assert.equal(sha256(file), document.searchable.sha256,
    'IF0126 searchable artifact does not match the manifest SHA-256');
  assert.equal(fs.statSync(file).size, document.searchable.bytes,
    'IF0126 searchable artifact does not match the manifest byte count');

  return {citation, document, file};
}

function startRangeServer() {
  return new Promise((resolve, reject) => {
    const requestedPort = process.env.NWMM_DOC_VIEWER_PORT || '0';
    const child = spawn('python3', [
      '-u', 'tools/range_server.py', requestedPort, '--directory', ROOT,
    ], {cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe']});
    let stdout = '';
    let stderr = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill('SIGTERM');
      reject(new Error(`Range server did not start within 10 seconds.\n${stderr}`));
    }, 10_000);

    child.stdout.on('data', chunk => {
      stdout += chunk.toString();
      const match = stdout.match(/http:\/\/[^:]+:(\d+) \(Range enabled\)/);
      if (!match || settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({child, port: Number(match[1]), stderr: () => stderr});
    });
    child.stderr.on('data', chunk => { stderr += chunk.toString(); });
    child.once('error', error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.once('exit', code => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new Error(`Range server exited before startup (${code}).\n${stderr}`));
    });
  });
}

async function stopRangeServer(server) {
  if (!server || server.child.exitCode != null || server.child.signalCode != null) return;
  await new Promise(resolve => {
    const timer = setTimeout(() => {
      if (server.child.exitCode == null) server.child.kill('SIGKILL');
    }, 2_000);
    server.child.once('exit', () => {
      clearTimeout(timer);
      resolve();
    });
    server.child.kill('SIGTERM');
  });
}

function browserPath(file) {
  const relative = path.relative(ROOT, file);
  assert.ok(relative && !relative.startsWith('..'), 'artifact is outside the served tree');
  return '/' + relative.split(path.sep).map(encodeURIComponent).join('/');
}

function publisherHosts(document) {
  const hosts = new Set();
  for (const value of [document.source_url, document.catalog_url]) {
    const hostname = new URL(value).hostname.toLowerCase();
    hosts.add(hostname);
    hosts.add(hostname.startsWith('www.') ? hostname.slice(4) : `www.${hostname}`);
  }
  return hosts;
}

async function main() {
  const fixture = loadFixture();
  let server = null;
  let browser = null;
  try {
    server = await startRangeServer();
    const base = `http://127.0.0.1:${server.port}`;
    const pdfUrl = `${base}${browserPath(fixture.file)}`;

    // Exercise the actual local server's Range implementation independently
    // of PDF.js so a full-file 200 cannot accidentally satisfy this contract.
    const range = await fetch(pdfUrl, {headers: {Range: 'bytes=0-63'}});
    assert.equal(range.status, 206, 'stored PDF server did not honor a byte range');
    assert.match(range.headers.get('content-range') || '', /^bytes 0-63\/\d+$/);
    assert.equal((await range.arrayBuffer()).byteLength, 64);

    browser = await chromium.launch({
      headless: true,
      executablePath: browserExecutable(),
    });
    const context = await browser.newContext({
      viewport: VIEWPORT,
      deviceScaleFactor: 2,
      isMobile: true,
      hasTouch: true,
    });
    await context.addInitScript(token => {
      localStorage.setItem('nwmm_auth', JSON.stringify({access: token}));
    }, ACCESS_TOKEN);

    const hosts = publisherHosts(fixture.document);
    const publisherAttempts = [];
    const publisherResponses = [];
    const resolverRequests = [];
    const unexpectedFailures = [];
    const pageErrors = [];
    const pdfResponses = [];

    await context.route(url => hosts.has(url.hostname.toLowerCase()), route => {
      publisherAttempts.push(route.request().url());
      return route.abort('internetdisconnected');
    });
    await context.route('**/auth.json', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({docsUrl: `${base}/__docs/resolve`}),
    }));
    await context.route('**/__docs/resolve**', route => {
      const request = route.request();
      const url = new URL(request.url());
      resolverRequests.push({
        docId: url.searchParams.get('doc_id'),
        page: url.searchParams.get('page'),
        quote: url.searchParams.get('quote') || url.searchParams.get('q'),
        token: request.headers()['x-auth-token'],
      });
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          doc_id: fixture.document.doc_id,
          title: fixture.document.title,
          authority: fixture.document.authority,
          state: fixture.document.state,
          pages: fixture.document.pages,
          page: fixture.citation.page,
          page_cite: fixture.citation.page_cite,
          quote: null,
          sha256: fixture.document.raw.sha256,
          text_layer: fixture.document.text_layer.status,
          retrieved: fixture.document.retrieved,
          source_url: fixture.document.source_url,
          catalog_url: fixture.document.catalog_url,
          file_url: pdfUrl,
          expires_in: 300,
        }),
      });
    });

    const page = await context.newPage();
    page.on('response', response => {
      if (response.url() === pdfUrl) pdfResponses.push(response.status());
      if (hosts.has(new URL(response.url()).hostname.toLowerCase())) {
        publisherResponses.push({url: response.url(), status: response.status()});
      }
    });
    page.on('pageerror', error => { pageErrors.push(String(error)); });
    page.on('requestfailed', request => {
      const hostname = new URL(request.url()).hostname.toLowerCase();
      if (!hosts.has(hostname)) {
        unexpectedFailures.push({url: request.url(), error: request.failure()});
      }
    });

    const fragment = new URLSearchParams({
      doc: fixture.document.doc_id,
      page: String(fixture.citation.page),
      q: fixture.citation.quote,
    });
    await page.goto(`${base}/site/viewer.html#${fragment}`, {
      waitUntil: 'domcontentloaded', timeout: 30_000,
    });
    await page.waitForFunction(() =>
      document.querySelector('#quoteState')?.textContent ===
        'highlighted in the text layer' &&
      document.querySelectorAll('.textLayer .hl').length > 0 &&
      document.querySelector('.page canvas'), null, {timeout: 30_000});

    assert.deepEqual(resolverRequests, [{
      docId: fixture.document.doc_id,
      page: '1',
      quote: null,
      token: ACCESS_TOKEN,
    }], 'viewer did not make one authenticated, quote-free resolver request');

    const rendered = await page.evaluate(() => {
      const canvas = document.querySelector('.page canvas');
      const context2d = canvas.getContext('2d');
      const pixels = context2d.getImageData(0, 0, canvas.width, canvas.height).data;
      const stride = Math.max(1, Math.floor((canvas.width * canvas.height) / 120_000));
      let sampled = 0;
      let ink = 0;
      for (let pixel = 0; pixel < canvas.width * canvas.height; pixel += stride) {
        const offset = pixel * 4;
        sampled++;
        if (pixels[offset + 3] > 0 &&
            (pixels[offset] < 240 || pixels[offset + 1] < 240 || pixels[offset + 2] < 240)) {
          ink++;
        }
      }
      const rect = canvas.getBoundingClientRect();
      return {
        canvasWidth: canvas.width,
        canvasHeight: canvas.height,
        canvasCssWidth: rect.width,
        ink,
        sampled,
        page: document.querySelector('#pageNo').value,
        pageOf: document.querySelector('#pageOf').textContent.trim(),
        quote: document.querySelector('#quote').textContent,
        quoteState: document.querySelector('#quoteState').textContent,
        highlights: document.querySelectorAll('.textLayer .hl').length,
        viewportWidth: window.innerWidth,
        touchPoints: navigator.maxTouchPoints,
        mobileMedia: matchMedia('(max-width:900px)').matches,
        scrollWidth: document.documentElement.scrollWidth,
        provenance: [...document.querySelectorAll('#prov a')].map(link => link.href),
        wideElements: [...document.querySelectorAll('body *')].map(element => {
          const box = element.getBoundingClientRect();
          return {
            tag: element.tagName.toLowerCase(),
            id: element.id,
            className: String(element.className || ''),
            left: Math.round(box.left),
            right: Math.round(box.right),
            width: Math.round(box.width),
            scrollWidth: element.scrollWidth,
            clientWidth: element.clientWidth,
          };
        }).filter(item => item.right > window.screen.width + 1 ||
          item.scrollWidth > item.clientWidth + 1),
      };
    });

    assert.equal(rendered.page, '1');
    assert.equal(rendered.pageOf, '/ 1');
    assert.equal(rendered.quote, fixture.citation.quote);
    assert.equal(rendered.quoteState, 'highlighted in the text layer');
    assert.ok(rendered.highlights > 0, 'the cited quote has no highlighted text spans');
    assert.ok(rendered.canvasWidth > 300 && rendered.canvasHeight > 300,
      `canvas has implausible dimensions ${rendered.canvasWidth}x${rendered.canvasHeight}`);
    assert.ok(rendered.ink > 100,
      `canvas has too little rendered page art (${rendered.ink}/${rendered.sampled} ink samples)`);
    assert.equal(rendered.viewportWidth, VIEWPORT.width,
      `mobile viewport expanded; wide elements: ${JSON.stringify(rendered.wideElements)}`);
    assert.ok(rendered.touchPoints > 0, 'browser context is not touch/mobile capable');
    assert.equal(rendered.mobileMedia, true);
    assert.ok(rendered.canvasCssWidth <= VIEWPORT.width,
      'rendered PDF overflows the mobile viewport');
    assert.ok(rendered.scrollWidth <= VIEWPORT.width + 1,
      'viewer introduces horizontal page overflow on mobile');
    assert.deepEqual(new Set(rendered.provenance), new Set([
      fixture.document.catalog_url, fixture.document.source_url,
    ]));
    assert.ok(pdfResponses.some(status => status === 200 || status === 206),
      'PDF.js did not receive the real local searchable PDF');
    assert.deepEqual(pageErrors, [], 'citation viewer raised a browser exception');

    // Prove that the publisher route is truly dead, not merely unused. The
    // citation remains on the stored canvas while this network attempt fails.
    const publisherProbe = await page.evaluate(async sourceUrl => {
      try {
        await fetch(sourceUrl, {mode: 'no-cors', cache: 'no-store'});
        return 'unexpectedly-resolved';
      } catch (error) {
        return 'blocked';
      }
    }, fixture.document.source_url);
    assert.equal(publisherProbe, 'blocked');
    assert.ok(publisherAttempts.length > 0, 'publisher block route was not exercised');
    assert.deepEqual(publisherResponses, [], 'a publisher request received a response');
    assert.deepEqual(unexpectedFailures, [], 'a non-publisher viewer request failed');
    assert.equal(await page.locator('#quoteState').textContent(),
      'highlighted in the text layer');
    assert.ok(await page.locator('.textLayer .hl').count() > 0);

    console.log('WS12 citation viewer browser acceptance passed');
    console.log(`  document: ${fixture.document.doc_id}`);
    console.log(`  artifact: ${path.relative(ROOT, fixture.file)}`);
    console.log(`  canvas: ${rendered.canvasWidth}x${rendered.canvasHeight}; ` +
      `${rendered.highlights} highlighted span(s)`);
    console.log(`  publisher hosts blocked: ${[...hosts].sort().join(', ')}`);
    await context.close();
  } catch (error) {
    if (server && server.stderr()) {
      error.message += `\nRange server log:\n${server.stderr()}`;
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    await stopRangeServer(server);
  }
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
