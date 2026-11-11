// Browser check of the lab column's picture panes (Jialei 2026-11-11): one
// Camera pane and one Microscope pane, each showing the source of the
// instrument that moves and keeping it while nothing moves; one Expand
// button that moves the live pane into the lightbox and back.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const STATIC = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'qumus2', 'ui', 'static');
const TYPES = {'.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml'};

test('camera and microscope panes follow the moving instrument', async t => {
  let chromium;
  try { ({chromium} = createRequire(import.meta.url)('playwright')); } catch { t.skip('playwright is not installed'); return; }
  const server = http.createServer((req, res) => {
    const file = path.join(STATIC, decodeURIComponent(req.url.split('?')[0]).replace(/^\/static\//, ''));
    if (!file.startsWith(STATIC) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, {'content-type': TYPES[path.extname(file)] || 'application/octet-stream'});
    fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({viewport: {width: 480, height: 760}});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/static/index.html`, {waitUntil: 'commit'});
    const steps = await page.evaluate(async () => {
      document.documentElement.innerHTML = '<head><link rel="stylesheet" href="/static/style.css"></head><body>'
        + '<div class="lab" style="width:460px;height:700px"><div class="lab-body"><div class="cams" id="cam"></div></div></div>'
        + '<div class="lightbox" id="lightbox" hidden></div></body>';
      const {CameraCard} = await import('/static/camera.js');
      const card = new CameraCard({replay: true, lightbox: () => {}, openFlake: () => {}});
      const state = () => ({
        shown: [...document.querySelectorAll('#cam .cam')].filter(e => !e.hidden).map(e => e.dataset.cam),
        panes: Object.fromEntries([...document.querySelectorAll('#cam .cam-pane')].map(p =>
          [p.dataset.slot, {source: p.querySelector('.pane-src').textContent, moving: p.classList.contains('active'),
            expand: p.querySelectorAll('.pane-x').length}])),
      });
      const out = {start: state()};
      card.context(true, true, false); out.search = state();
      card.context(false, true, true); out.reactorWithout = state();
      card.setReactor(true); card.context(false, false, false); card.context(false, true, true); out.reactor = state();
      card.context(false, false, false); out.idle = state();
      card.setReactor(false); out.removed = state();
      card.expand('microscope');
      out.expanded = {inLightbox: !!document.querySelector('#lightbox .cam-pane[data-slot="microscope"]'),
        closeButtons: document.querySelectorAll('#lightbox .pane-close').length};
      document.getElementById('lightbox').hidden = true;
      await new Promise(r => setTimeout(r, 50));
      out.closed = {backInColumn: !!document.querySelector('#cam .cam-pane[data-slot="microscope"]'),
        closeButtons: document.querySelectorAll('.pane-close').length, order: [...document.querySelectorAll('#cam .cam-pane')].map(p => p.dataset.slot)};
      return out;
    });
    assert.deepEqual(errors, []);
    assert.deepEqual(steps.start.shown, ['overview_camera', 'microscope']);
    assert.equal(steps.start.panes.camera.source, 'Overview');
    assert.equal(steps.start.panes.microscope.source, 'Transfer stage');
    assert.equal(steps.start.panes.microscope.expand, 1);
    assert.equal(steps.start.panes.microscope.moving, false);
    assert.equal(steps.search.panes.microscope.moving, true);
    assert.equal(steps.search.panes.camera.moving, true);
    // no reactor in this lab: a reactor job never switches to an absent microscope
    assert.deepEqual(steps.reactorWithout.shown, ['overview_camera', 'microscope']);
    assert.deepEqual(steps.reactor.shown, ['overview_camera', 'reactor_microscope']);
    assert.equal(steps.reactor.panes.microscope.source, 'Reactor');
    assert.equal(steps.reactor.panes.microscope.moving, true);
    // nothing moves: the last source stays, unmarked
    assert.deepEqual(steps.idle.shown, ['overview_camera', 'reactor_microscope']);
    assert.equal(steps.idle.panes.microscope.moving, false);
    assert.deepEqual(steps.removed.shown, ['overview_camera', 'microscope']);
    assert.deepEqual(steps.expanded, {inLightbox: true, closeButtons: 1});
    assert.deepEqual(steps.closed, {backInColumn: true, closeButtons: 0, order: ['camera', 'microscope']});
  } finally {
    await browser.close();
    server.close();
  }
});
