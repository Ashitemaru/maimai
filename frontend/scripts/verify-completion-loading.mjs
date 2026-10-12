import assert from 'node:assert/strict';
import { idleScoreSync } from './score-sync-fixture.mjs';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { fixtureAsset, jacketSVG } from './fixtures.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.join(project, 'output/completion-loading-audit');
await fs.mkdir(output, { recursive: true });
const requests = [];
const checks = [];
const errors = [];
let catalogRevision = 'fixture';
let catalogVersions = [];
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const charts = Array.from({ length: 360 }, (_, index) => ({
  id: 9000 + index,
  song_id: 9000 + index,
  chart_key: `${9000 + index}:3`,
  title: `Fixture ${index}`,
  display_title: '',
  artist: 'Synthetic fixture',
  type: 'standard',
  difficulty: 3,
  difficulty_index: 3,
  difficulty_name: 'Master',
  constant: 13.2,
  level: '13',
  jacket_url: `/jackets/${9000 + index}.png`,
  thumbnail_url: `/thumbnails/${9000 + index}.svg`,
  jp_release_version: 'UNiVERSE',
  jp_release_version_candidate: null,
  release_status: 'verified',
  requires_venue: true,
  in_original_table: false,
  legacy_complete: false,
  legacy_venue: '',
  manual_ap: null,
  manual_revision: 0,
  manual_venue: '',
  manual_date: null,
  achievement: null,
  note: '',
  venue: '',
  provider_achievement: null,
  provider_fc: null,
  provider_synced_at: null,
  is_ap: false,
  is_complete: false,
  status: 'incomplete',
  conflict: false,
  warnings: [],
  record_revision: 0,
  pending_record: null,
}));
const counts = () => ({
  total: charts.length,
  complete: charts.filter((chart) => chart.is_complete).length,
  ap: charts.filter((chart) => chart.is_ap).length,
  remaining: charts.filter((chart) => !chart.is_complete).length,
  conflicts: 0,
  legacy_complete: 0,
  levels: [],
  constants: [],
  sync: null,
});
const queue = () => ({
  items: charts.filter((chart) => chart.pending_record),
  count: charts.filter((chart) => chart.pending_record).length,
});
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost');
  const pathname = decodeURIComponent(url.pathname);
  const entry = { path: pathname, method: request.method, aborted: false };
  requests.push(entry);
  response.on('close', () => {
    entry.aborted = !response.writableFinished;
  });
  try {
    if (pathname.startsWith('/api/')) {
      let data;
      let delay = 0;
      const detail = pathname.match(/^\/api\/charts\/([^/]+)$/);
      const record = pathname.match(/^\/api\/charts\/([^/]+)\/ap-record$/);
      if (record && request.method === 'PUT') {
        let body = '';
        for await (const chunk of request) body += chunk;
        const input = JSON.parse(body);
        const chart = charts.find((item) => item.chart_key === record[1]);
        assert.equal(input.expected_revision, chart.record_revision);
        chart.record_revision += 1;
        chart.pending_record = {
          id: input.request_id,
          venue: input.venue,
          revision: chart.record_revision,
          reason: '待同步核实',
          created_at: '2026-10-12T22:00:00+08:00',
          updated_at: '2026-10-12T22:00:00+08:00',
        };
        data = { chart };
      } else {
        assert.equal(request.method, 'GET');
        if (detail) data = charts.find((chart) => chart.chart_key === detail[1]);
        else if (pathname === '/api/auth/session')
          data = { authenticated: true, auth_required: false };
        else if (pathname === '/api/settings/preferences') {
          data = { default_constant_min: 13, default_constant_max: 13.9 };
          delay = 25;
        } else if (pathname === '/api/sync/status') {
          data = idleScoreSync;
        } else if (pathname === '/api/settings/divingfish') {
          data = { configured: true, source: 'local', sync: null };
          delay = 500;
        } else if (pathname === '/api/venues') {
          data = {
            items: [
              { name: '【上海】修改后机厅', count: 0 },
              { name: '【广州】另一台手机', count: 0 },
            ],
          };
          delay = 500;
        } else if (pathname === '/api/ap-records') {
          data = queue();
          delay = 500;
        } else if (pathname === '/api/chart-filters') {
          data = { constants: [13, 13.9], versions: catalogVersions, cn_versions: [] };
          delay = 250;
        } else if (pathname === '/api/catalog/update')
          data = {
            revision: catalogRevision,
            snapshot_at: null,
            main: { status: 'succeeded', warnings: [], diff: {} },
            remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0 },
            can_rollback: false,
          };
        else if (pathname === '/api/completion-table') {
          data = {
            items: charts,
            total: charts.length,
            summary: { ...counts(), constants: [{ ...counts(), constant: 13.2 }] },
            generated_at: '2026-10-12T22:00:00+08:00',
          };
          delay = 80;
        } else throw new Error(`Unmocked API ${pathname}`);
      }
      const json = JSON.stringify(data);
      await pause(delay);
      response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      response.end(json);
      return;
    }
    if (pathname.startsWith('/thumbnails/')) {
      response.writeHead(200, {
        'Content-Type': 'image/svg+xml',
        'Cache-Control': 'private,max-age=31536000,immutable',
      });
      response.end(jacketSVG);
      return;
    }
    const fixture = fixtureAsset(pathname);
    if (fixture) {
      response.setHeader('Content-Type', fixture.contentType);
      response.end(fixture.body);
      return;
    }
    const file = path.resolve(bundle, pathname === '/' ? 'index.html' : pathname.slice(1));
    assert(file.startsWith(bundle + path.sep));
    response.setHeader(
      'Content-Type',
      {
        '.html': 'text/html',
        '.js': 'text/javascript',
        '.css': 'text/css',
        '.svg': 'image/svg+xml',
      }[path.extname(file)] || 'application/octet-stream',
    );
    response.end(await fs.readFile(file));
  } catch (error) {
    errors.push(error.message);
    response.writeHead(500);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launchBrowser();
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  page.on('pageerror', (error) => errors.push(error.message));
  const settle = async () => {
    await page.waitForLoadState('networkidle');
    await pause(200);
  };
  const count = (list, route) => list.filter((item) => item.path === route).length;
  const initialPaths = [
    '/api/catalog/update',
    '/api/chart-filters',
    '/api/completion-table',
    '/api/settings/preferences',
    '/api/venues',
    '/api/settings/divingfish',
    '/api/ap-records',
  ];
  async function verifyCold(start) {
    await page.locator('.completion-tile').first().waitFor();
    await settle();
    const slice = requests.slice(start);
    for (const pathname of initialPaths)
      assert.equal(count(slice, pathname), 1, `Cold load duplicates ${pathname}`);
    assert.deepEqual(
      slice.filter((item) => item.path.startsWith('/api/') && item.aborted),
      [],
    );
    assert.equal(
      slice.filter((item) => item.path.startsWith('/jackets/')).length,
      0,
      'Preview must not fetch original jackets',
    );
    const thumbs = slice.filter((item) => item.path.startsWith('/thumbnails/'));
    const geometry = await page.evaluate(() => {
      const tiles = [...document.querySelectorAll('.completion-tile')];
      const near = tiles.filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.top <= innerHeight + 80 && rect.bottom >= -80;
      });
      return {
        mounted: tiles.length,
        near: near.length,
        cols: new Set(tiles.map((element) => element.getBoundingClientRect().left)).size,
        loaded: [...document.querySelectorAll('.completion-art > img[src]')].length,
      };
    });
    assert(
      thumbs.length > 0 && thumbs.length <= geometry.near + geometry.cols,
      JSON.stringify({ thumbs: thumbs.length, ...geometry }),
    );
    assert(
      thumbs.length < 120 && geometry.mounted < 240,
      `Initial work must remain near viewport: ${JSON.stringify({ thumbs: thumbs.length, ...geometry })}`,
    );
    assert.equal(thumbs.length, geometry.loaded);
    checks.push({
      coldLoad: { thumbs: thumbs.length, ...geometry },
      apiCalls: Object.fromEntries(initialPaths.map((route) => [route, count(slice, route)])),
    });
  }
  await page.goto(origin + '/?view=completion#ap');
  await verifyCold(0);
  const reloadStart = requests.length;
  await cdp.send('Network.clearBrowserCache');
  await page.reload();
  await verifyCold(reloadStart);
  const resumeStart = requests.length;
  charts[0].pending_record = {
    id: 'other-device',
    venue: '【广州】另一台手机',
    revision: 1,
    reason: '待同步核实',
    created_at: '2026-10-12T22:00:00+08:00',
    updated_at: '2026-10-12T22:00:00+08:00',
  };
  charts[0].record_revision = 1;
  await page.evaluate(() => {
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  });
  const first = page.locator('.completion-tile').first();
  await first.getByLabel('待核实', { exact: true }).waitFor();
  await settle();
  const resumed = requests.slice(resumeStart);
  for (const pathname of [
    '/api/venues',
    '/api/settings/divingfish',
    '/api/ap-records',
    '/api/completion-table',
    '/api/catalog/update',
  ])
    assert.equal(count(resumed, pathname), 1, `Resume duplicates ${pathname}`);
  assert.equal(
    count(resumed, '/api/chart-filters'),
    0,
    'Personal refresh must not refetch catalog filters',
  );
  checks.push('Focus and visibility bursts coalesce once and show another device’s pending change');
  await first.click();
  const drawer = page.getByRole('dialog', { name: '谱面记录', exact: true });
  await drawer.getByLabel('AP 机厅', { exact: true }).selectOption('【上海】修改后机厅');
  const writeStart = requests.length;
  await drawer.getByRole('button', { name: '保存待核记录', exact: true }).click();
  await drawer.waitFor({ state: 'detached' });
  await first.getByLabel('待核实', { exact: true }).waitFor();
  await settle();
  assert.equal(count(requests.slice(writeStart), '/api/completion-table'), 1);
  assert.equal(count(requests.slice(writeStart), '/api/chart-filters'), 0);
  assert.equal(charts[0].pending_record.venue, '【上海】修改后机厅');
  checks.push(
    'Successful AP venue mutations refresh the board exactly once without fetching filters',
  );
  const staleRead = page.waitForRequest(
    (request) => new URL(request.url()).pathname === '/api/ap-records',
  );
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await staleRead;
  charts[1].record_revision = 1;
  charts[1].pending_record = {
    id: 'late-device',
    venue: '【上海】较晚的手机记录',
    revision: 1,
    reason: '待同步核实',
    created_at: '2026-10-12T23:00:00+08:00',
    updated_at: '2026-10-12T23:00:00+08:00',
  };
  await page.evaluate(() => {
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
  });
  await page.waitForFunction(() => document.querySelector('.nav-count')?.textContent === '2');
  await settle();
  checks.push(
    'A later focus waits for an old inflight snapshot, then refreshes to retain cross-device queue updates',
  );
  const beforeScroll = requests.filter((item) => item.path.startsWith('/thumbnails/')).length;
  await page.locator('.completion-more').scrollIntoViewIfNeeded();
  await settle();
  assert(requests.filter((item) => item.path.startsWith('/thumbnails/')).length > beforeScroll);
  const allTotals = Number(
    (await page.locator('.stat-value').nth(2).textContent()).replace(/[^0-9]/g, ''),
  );
  assert.equal(allTotals, 360, 'Lazy mounting must not change totals');
  checks.push('Scrolling requests the next visible jackets while all-chart totals remain complete');
  const catalogResumeStart = requests.length;
  catalogRevision = 'from-another-device';
  catalogVersions = [{ value: 'PRiSM', label: 'PRiSM' }];
  charts[0].title = 'Remote catalog update';
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await pause(220);
  assert.equal(
    count(requests.slice(catalogResumeStart), '/api/catalog/update'),
    0,
    'Hidden state must not fetch catalog',
  );
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForFunction(() =>
    [...document.querySelectorAll('select[aria-label="登场版本"] option')].some(
      (option) => option.value === 'PRiSM',
    ),
  );
  await page.waitForFunction(() =>
    document
      .querySelector('.completion-tile[data-chart-key="9000:3"]')
      ?.getAttribute('aria-label')
      ?.includes('Remote catalog update'),
  );
  await settle();
  assert.equal(count(requests.slice(catalogResumeStart), '/api/catalog/update'), 1);
  assert.equal(count(requests.slice(catalogResumeStart), '/api/chart-filters'), 1);
  checks.push(
    'Visibility-only iPhone/PWA resume refreshes remote catalog revision, version options and chart content without a focus event',
  );
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify({ checks, requests, errors }, null, 2),
  );
  assert.deepEqual(errors, []);
  console.log(`Completion loading: ${checks.length} checks passed with real HTTP cold reloads.`);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
