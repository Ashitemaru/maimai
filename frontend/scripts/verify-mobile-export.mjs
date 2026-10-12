import assert from 'node:assert/strict';
import { idleScoreSync } from './score-sync-fixture.mjs';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { badgePNG, fixtureAsset, useSystemCoverFont } from './fixtures.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.resolve(
  process.env.MOBILE_EXPORT_AUDIT_OUTPUT || path.join(project, 'output/mobile-export-audit'),
);
const checks = [];
const requests = [];
const sharedFiles = [];
const errors = [];
const blockedWrites = [];
await fs.mkdir(output, { recursive: true });
const chart = {
  id: 9200,
  song_id: 9200,
  chart_key: '9200:3',
  title: 'Mobile Export Fixture',
  display_title: '',
  artist: 'Original synthetic fixture',
  type: 'standard',
  difficulty: 3,
  difficulty_index: 3,
  difficulty_name: 'Master',
  level: '13',
  constant: 13.2,
  jacket_url: '/jackets/9200.png',
  jp_release_version: 'UNiVERSE',
  jp_release_version_candidate: null,
  jp_release_provider: 'arcade_songs',
  release_status: 'verified',
  requires_venue: false,
  in_original_table: false,
  legacy_complete: false,
  legacy_venue: '',
  manual_ap: true,
  manual_revision: 1,
  manual_venue: '',
  venue: '',
  note: '',
  manual_date: null,
  achievement: 100.5,
  provider_achievement: null,
  provider_fc: null,
  provider_synced_at: null,
  is_ap: true,
  is_complete: true,
  status: 'ap',
  conflict: false,
  warnings: [],
};
const counts = { total: 1, complete: 1, ap: 1, remaining: 0, conflicts: 0, legacy_complete: 0 };
const summary = {
  ...counts,
  levels: [],
  constants: [{ ...counts, constant: 13.2 }],
  sync: null,
};
const catalog = {
  revision: 'mobile-fixture',
  decision_revision: 0,
  applied_decision_revision: 0,
  decisions_pending: false,
  snapshot_at: null,
  main: { status: 'succeeded', stage: '', message: '', warnings: [], diff: {} },
  remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0, message: '' },
  can_rollback: false,
};
function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
async function bounded(promise, message) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(message)), 10000);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
function pngSize(bytes) {
  assert.equal(bytes.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}
const server = http.createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    if (pathname.startsWith('/api/') || request.method !== 'GET') {
      response.writeHead(405);
      response.end();
      return;
    }
    const asset = fixtureAsset(pathname);
    if (asset) {
      response.setHeader('Content-Type', asset.contentType);
      response.end(asset.body);
      return;
    }
    const file = path.resolve(bundle, pathname === '/' ? 'index.html' : pathname.slice(1));
    if (!file.startsWith(bundle + path.sep)) throw new Error('Outside fixture bundle');
    response.setHeader(
      'Content-Type',
      {
        '.html': 'text/html',
        '.js': 'text/javascript',
        '.css': 'text/css',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
        '.webmanifest': 'application/manifest+json',
      }[path.extname(file)] || 'application/octet-stream',
    );
    response.end(await fs.readFile(file));
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await launchBrowser();
try {
  for (const width of [390, 320]) {
    const context = await browser.newContext({
      viewport: { width, height: 844 },
      isMobile: true,
      hasTouch: true,
      userAgent:
        'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
    });
    await useSystemCoverFont(context);
    await context.route('https://tile.openstreetmap.org/**', (route) =>
      route.fulfill({
        contentType: 'image/svg+xml',
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" />',
      }),
    );
    await context.addInitScript(() => {
      window.mobileShareMode = 'success';
      window.mobileShareCalls = [];
      Object.defineProperty(navigator, 'canShare', {
        configurable: true,
        value: ({ files }) => files?.length === 1 && files[0] instanceof File,
      });
      Object.defineProperty(navigator, 'share', {
        configurable: true,
        value: async ({ files }) => {
          const entry = {
            active: navigator.userActivation.isActive,
            count: files.length,
            name: files[0].name,
            bytes: files[0].size,
            type: files[0].type,
            mode: window.mobileShareMode,
          };
          window.mobileShareCalls.push(entry);
          const bytes = await files[0].arrayBuffer();
          const view = new DataView(bytes);
          entry.width = view.getUint32(16);
          entry.height = view.getUint32(20);
          entry.sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)))
            .map((value) => value.toString(16).padStart(2, '0'))
            .join('');
          entry.complete = true;
          if (entry.mode === 'cancel') throw new DOMException('Fixture cancellation', 'AbortError');
          if (entry.mode === 'failure')
            throw new DOMException('Fixture share failure', 'NotAllowedError');
        },
      });
    });
    const control = { gate: null, gameManifestReads: 0, successfulGameAssets: 0 };
    const jobs = new Map();
    await context.route('**/cover-assets/**', async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/cover-assets/game-ui/manifest.json') {
        control.gameManifestReads += 1;
        if (control.gameManifestReads === 1) {
          await route.fulfill({
            status: 503,
            json: { detail: 'Injected transient asset failure' },
          });
          return;
        }
      }
      if (pathname === '/cover-assets/game-ui/badge.svg') control.successfulGameAssets += 1;
      if (/\/ComboStatus\/[34]\.png$/.test(pathname)) {
        await route.fulfill({ contentType: 'image/png', body: badgePNG });
        return;
      }
      const asset = fixtureAsset(pathname);
      assert(asset);
      await route.fulfill(asset);
    });
    await context.route('**/api/**', async (route) => {
      if (new URL(route.request().url()).pathname === '/api/auth/session') {
        await route.fulfill({ json: { authenticated: true, auth_required: false } });
        return;
      }
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      const method = request.method();
      requests.push({ viewport: width, path: pathname, method });
      if (pathname === '/api/completion-exports' && method === 'POST') {
        const input = request.postDataJSON();
        assert.equal(input.width, 3200);
        assert.equal(input.parts, 1);
        const job = {
          ...input,
          id: `mobile-${width}-${jobs.size}`,
          deleted: deferred(),
          image: null,
        };
        jobs.set(job.id, job);
        if (control.gate) {
          const gate = control.gate;
          control.gate = null;
          gate.started.resolve(job);
          await gate.release.promise;
        }
        await route.fulfill({ json: { id: job.id } });
        return;
      }
      const jobPath = pathname.match(/^\/api\/completion-exports\/([^/]+)(.*)$/);
      if (jobPath) {
        const job = jobs.get(jobPath[1]);
        assert(job);
        const suffix = jobPath[2];
        if (method === 'DELETE' && !suffix) {
          await route.fulfill({ json: { ok: true } });
          job.deleted.resolve();
          return;
        }
        if (method === 'PUT' && suffix === '/parts/0') {
          job.image = request.postDataBuffer();
          assert.deepEqual(pngSize(job.image), { width: job.width, height: job.height });
          await route.fulfill({ json: { ok: true } });
          return;
        }
        if (method === 'POST' && suffix === '/finish') {
          assert(job.image);
          await route.fulfill({
            json: { url: `/api/completion-exports/${job.id}/image`, name: job.name },
          });
          return;
        }
        if (method === 'GET' && suffix === '/image') {
          assert(job.image);
          await route.fulfill({ contentType: 'image/png', body: job.image });
          return;
        }
        throw new Error(`Unexpected export API ${method} ${suffix}`);
      }
      if (method !== 'GET') {
        blockedWrites.push({ viewport: width, path: pathname, method });
        await route.fulfill({ status: 405, json: { detail: 'Private mutations forbidden' } });
        return;
      }
      const fixtures = {
        '/api/settings/preferences': { default_constant_min: 13, default_constant_max: 13.9 },
        '/api/sync/status': idleScoreSync,
        '/api/settings/divingfish': { configured: false, source: null, sync: null },
        '/api/chart-filters': { constants: [13, 13.9], versions: [], cn_versions: [] },
        '/api/catalog/update': catalog,
        '/api/venues': { items: [] },
        '/api/venues/map': { items: [] },
        '/api/venues/summary': {
          levels: ['12+', '13', '13+', '14', '14+/15'].map((value) => ({ value, label: value })),
          items: [],
          totals: { '12+': 0, 13: 0, '13+': 0, 14: 0, '14+/15': 0 },
          total: 0,
          pending_count: 0,
        },
        '/api/ap-records': { items: [], count: 0 },
        '/api/summary': summary,
        '/api/charts': { items: [chart], total: 1, offset: 0, limit: 12 },
        '/api/charts/9200%3A3': chart,
        '/api/charts/9200:3': chart,
        '/api/completion-table': {
          items: [chart],
          total: 1,
          summary,
          generated_at: '2026-10-11T16:00:00Z',
        },
      };
      assert(pathname in fixtures, `Unmocked API ${pathname}`);
      await route.fulfill({ json: fixtures[pathname] });
    });
    const page = await context.newPage();
    const downloads = [];
    page.on('download', (download) => downloads.push(download));
    page.on('pageerror', (error) => errors.push({ viewport: width, message: error.message }));
    await page.goto(origin + '/?view=completion#ap');
    const panel = page.getByRole('region', { name: '图片已准备好', exact: true });
    const exportButton = page.getByRole('button', { name: '导出图片', exact: true });
    await exportButton.tap();
    await panel.waitFor();
    const finished = [...jobs.values()][0];
    await bounded(finished.deleted.promise, 'Successful export must DELETE its temporary job');
    assert.equal(downloads.length, 0, 'iPhone generation must not automatically download');
    assert.equal(await panel.getByRole('link', { name: '保存 PNG', exact: true }).count(), 1);
    assert.equal(
      await panel.getByRole('link', { name: '打开整张图片', exact: true }).getAttribute('target'),
      '_blank',
    );
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    checks.push(
      `${width}px: complete 3200px PNG is prepared without automatic download and its job is deleted`,
    );

    async function share(mode, expectedWidth, expectedHash) {
      const before = await page.evaluate((value) => {
        window.mobileShareMode = value;
        return window.mobileShareCalls.length;
      }, mode);
      await panel.getByRole('button', { name: '分享图片', exact: true }).tap();
      await page.waitForFunction((index) => window.mobileShareCalls[index]?.complete, before);
      const call = await page.evaluate((index) => window.mobileShareCalls[index], before);
      assert.equal(call.active, true, 'navigator.share must receive the tap activation');
      assert.equal(call.count, 1);
      assert.equal(call.type, 'image/png');
      assert.equal(call.width, expectedWidth);
      assert(call.bytes > 100);
      if (expectedHash)
        assert.equal(call.sha256, expectedHash, 'Shared file must contain the whole PNG');
      sharedFiles.push({ viewport: width, ...call });
      await panel.getByRole('button', { name: '分享图片', exact: true }).waitFor();
    }
    const completedHash = createHash('sha256').update(finished.image).digest('hex');
    await share('cancel', 3200, completedHash);
    assert.equal(await panel.getByRole('alert').count(), 0);
    await share('success', 3200, completedHash);
    await share('failure', 3200, completedHash);
    await panel.getByRole('alert').waitFor();
    await page.screenshot({
      path: path.join(output, `completion-ready-${width}.png`),
      fullPage: true,
    });
    const downloaded = page.waitForEvent('download');
    await panel.getByRole('link', { name: '保存 PNG', exact: true }).tap();
    const saved = await downloaded;
    const file = await saved.path();
    assert.equal(
      createHash('sha256')
        .update(await fs.readFile(file))
        .digest('hex'),
      completedHash,
    );
    assert.equal(downloads.length, 1);
    checks.push(
      `${width}px: real taps share one whole PNG with activation; cancel retries and failure retains working download fallback`,
    );

    for (const action of ['cancel', 'navigate']) {
      const gate = { started: deferred(), release: deferred() };
      control.gate = gate;
      await exportButton.tap();
      const job = await bounded(gate.started.promise, 'Export creation was not reached');
      if (action === 'cancel')
        await page.getByRole('button', { name: '取消导出', exact: true }).tap();
      else {
        await page.getByRole('link', { name: '我的机厅', exact: true }).tap();
        await page.getByRole('button', { name: '汇总表', exact: true }).tap();
        assert.equal(await page.locator('.venue-summary-total').innerText(), '0 家机厅 · 0 AP');
      }
      gate.release.resolve();
      await bounded(job.deleted.promise, `The ${action} export must DELETE its late-created job`);
      assert.equal(job.image, null, 'Cancelled job must not upload a partial image');
      assert.equal(await panel.count(), 0);
      if (action === 'navigate')
        await page.getByRole('link', { name: 'AP 图鉴', exact: true }).tap();
      checks.push(
        `${width}px: ${action} cleans a job even when creation returns after cancellation`,
      );
    }

    await page.getByRole('link', { name: '封面工坊', exact: true }).tap();
    await page.getByRole('button', { name: '选择谱面', exact: true }).tap();
    await page
      .getByRole('dialog', { name: '选择谱面', exact: true })
      .getByRole('button', { name: /Mobile Export Fixture/ })
      .tap();
    await page.getByLabel('达成率', { exact: true }).fill('100.5000');
    await page.getByText('达成率暂用系统字体', { exact: true }).waitFor();
    assert.equal(
      control.gameManifestReads,
      1,
      'First render must encounter the injected manifest failure',
    );
    assert.equal(control.successfulGameAssets, 0);
    const retriedManifest = page.waitForResponse(
      (response) => response.url().endsWith('/game-ui/manifest.json') && response.status() === 200,
    );
    await page.getByLabel('导出比例', { exact: true }).selectOption('4:3');
    await retriedManifest;
    await page.waitForFunction(
      () => document.querySelector('.canvas-frame canvas')?.width === 1440,
    );
    await page.locator('.canvas-frame[aria-busy="false"]').waitFor();
    assert.equal(
      control.gameManifestReads,
      2,
      'Changing aspect must retry the failed cached manifest',
    );
    assert(control.successfulGameAssets > 0, 'Recovered manifest must load its actual badge asset');
    const downloadCount = downloads.length;
    await page.getByRole('button', { name: '导出封面', exact: true }).tap();
    await panel.waitFor();
    assert.equal(downloads.length, downloadCount, 'iPhone cover generation must not auto-download');
    await share('cancel', 1440);
    assert.equal(await panel.getByRole('alert').count(), 0);
    await share('success', 1440);
    await share('failure', 1440);
    await panel.getByRole('alert').waitFor();
    assert.equal(await panel.getByRole('link', { name: '保存 PNG', exact: true }).count(), 1);
    assert.equal(await panel.getByRole('link', { name: '打开整张图片', exact: true }).count(), 1);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({ path: path.join(output, `cover-ready-${width}.png`), fullPage: true });
    checks.push(
      `${width}px: failed game manifest recovers on rerender and cover sharing preserves activation/cancel/fallback behavior`,
    );

    const pwa = await page.evaluate(async () => {
      const manifestLink = document.querySelector('link[rel="manifest"]');
      const manifest = await (await fetch(manifestLink.href)).json();
      const icons = [
        ...manifest.icons,
        { src: document.querySelector('link[rel="apple-touch-icon"]').href, sizes: '180x180' },
      ];
      const dimensions = [];
      for (const icon of icons) {
        const data = await (await fetch(icon.src)).arrayBuffer();
        const view = new DataView(data);
        dimensions.push({
          declared: icon.sizes,
          width: view.getUint32(16),
          height: view.getUint32(20),
        });
      }
      return {
        display: manifest.display,
        dimensions,
        registrations: (await navigator.serviceWorker.getRegistrations()).length,
      };
    });
    assert.equal(pwa.display, 'standalone');
    assert.deepEqual(
      pwa.dimensions.map(({ width: size, height }) => [size, height]).sort((a, b) => a[0] - b[0]),
      [
        [180, 180],
        [192, 192],
        [512, 512],
      ],
    );
    assert(pwa.dimensions.every((icon) => icon.declared === `${icon.width}x${icon.height}`));
    assert.equal(pwa.registrations, 0);
    checks.push(
      `${width}px: standalone manifest and real 180/192/512 PNG icons are present, with no service worker`,
    );
    await context.close();
  }
  assert.deepEqual(blockedWrites, []);
  assert.deepEqual(errors, []);
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify({ checks, requests, sharedFiles, blockedWrites, errors }, null, 2),
  );
  console.log(
    `Mobile export: ${checks.length} checks passed (Chromium iPhone emulation; no real Safari or native share sheet).`,
  );
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
