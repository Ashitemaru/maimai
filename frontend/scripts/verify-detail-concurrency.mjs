import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.resolve(
  process.env.DETAIL_AUDIT_OUTPUT || path.join(project, 'output/detail-concurrency-audit'),
);
await fs.mkdir(output, { recursive: true });
const checks = [];
const mutations = [];
const errors = [];
const charts = ['Alpha', 'Beta'].map((title, index) => ({
  id: 9100 + index,
  song_id: 9100 + index,
  chart_key: `${9100 + index}:3`,
  title,
  display_title: '',
  artist: 'Isolated concurrency fixture',
  type: 'standard',
  difficulty: 3,
  difficulty_index: 3,
  difficulty_name: 'Master',
  level: '13',
  constant: 13.2,
  jacket_url: '/fixture-jacket.svg',
  jp_release_version: 'PRiSM',
  jp_release_version_candidate: null,
  release_status: 'verified',
  requires_venue: false,
  in_original_table: false,
  legacy_complete: false,
  legacy_venue: '',
  manual_ap: true,
  manual_revision: 1,
  manual_venue: '',
  venue: '',
  note: 'Original record',
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
}));
const summary = {
  total: 2,
  complete: 2,
  ap: 2,
  remaining: 0,
  conflicts: 0,
  legacy_complete: 0,
  levels: [],
  constants: [],
  sync: null,
};
const catalog = {
  revision: 'detail-fixture',
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
let delayedRead = null;
let delayedWrite = null;
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    if (url.pathname.startsWith('/api/') || request.method !== 'GET') {
      response.writeHead(405);
      response.end();
      return;
    }
    if (url.pathname === '/fixture-jacket.svg') {
      response.setHeader('Content-Type', 'image/svg+xml');
      response.end('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"/>');
      return;
    }
    const file = path.resolve(bundle, url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
    if (!file.startsWith(bundle + path.sep)) throw new Error('Outside fixture bundle');
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
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await launchBrowser();
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  // Deliberately ignore AbortSignal for detail reads so sequence invalidation is
  // exercised even if a transport or cached response arrives after cancellation.
  await context.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.fetch = (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      const detail = /^\/api\/charts\/[^/]+$/.test(url.pathname);
      return original(input, detail ? { ...init, signal: undefined } : init);
    };
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
    const manual = pathname.match(/^\/api\/charts\/([^/]+)\/manual$/);
    if (manual) {
      const chart = charts.find((item) => item.chart_key === decodeURIComponent(manual[1]));
      assert(chart);
      const body = method === 'PUT' ? request.postDataJSON() : null;
      const expected = body?.expected_revision ?? Number(url.searchParams.get('expected_revision'));
      assert(method === 'PUT' || method === 'DELETE');
      assert(method !== 'DELETE' || url.searchParams.has('expected_revision'));
      assert(Number.isInteger(expected));
      mutations.push({ method, expected_revision: expected, chart_key: chart.chart_key });
      if (delayedWrite) {
        const delay = delayedWrite;
        delayedWrite = null;
        delay.started.resolve();
        await delay.release.promise;
      }
      if (expected !== chart.manual_revision) {
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: 'manual_conflict',
              message: '记录已在其他页面更新，请重新载入后再保存。',
              chart,
            },
          },
        });
        return;
      }
      chart.manual_revision += 1;
      if (method === 'PUT') {
        Object.assign(chart, {
          note: body.note,
          manual_ap: body.ap,
          venue: body.venue,
          achievement: body.achievement,
          manual_date: body.date,
        });
      } else Object.assign(chart, { manual_ap: null, note: '', manual_date: null });
      await route.fulfill({ json: method === 'PUT' ? { chart, warnings: [] } : { ok: true } });
      return;
    }
    assert.equal(method, 'GET', 'Unexpected mutation must not reach a real service');
    const detail = pathname.match(/^\/api\/charts\/([^/]+)$/);
    if (detail) {
      const chart = charts.find((item) => item.chart_key === decodeURIComponent(detail[1]));
      assert(chart);
      const snapshot = structuredClone(chart);
      if (delayedRead?.chart_key === chart.chart_key) {
        const delay = delayedRead;
        delayedRead = null;
        delay.started.resolve();
        await delay.release.promise;
      }
      await route.fulfill({ json: snapshot });
      return;
    }
    const fixtures = {
      '/api/settings/preferences': { default_constant_min: 13, default_constant_max: 13.9 },
      '/api/settings/divingfish': { configured: false, source: null, sync: null },
      '/api/chart-filters': { constants: [13, 13.9], versions: [], cn_versions: [] },
      '/api/catalog/update': catalog,
      '/api/venues': { items: [] },
      '/api/summary': summary,
      '/api/charts': { items: charts, total: charts.length, offset: 0, limit: 12 },
    };
    assert(pathname in fixtures, `Unmocked API ${pathname}`);
    await route.fulfill({ json: fixtures[pathname] });
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(origin + '/#ap');
  const alpha = page.getByRole('button', { name: '查看 Alpha Master', exact: true });
  const beta = page.getByRole('button', { name: '查看 Beta Master', exact: true });
  const record = page.getByRole('dialog', { name: '谱面记录', exact: true });
  await alpha.waitFor();
  const holdRead = () => {
    const delay = { chart_key: charts[0].chart_key, started: deferred(), release: deferred() };
    delayedRead = delay;
    return delay;
  };
  const settle = async () => {
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  };

  const first = holdRead();
  await alpha.click();
  await first.started.promise;
  await beta.click();
  await record.getByRole('heading', { name: 'Beta', exact: true }).waitFor();
  first.release.resolve();
  await settle();
  assert.equal(await record.getByRole('heading').textContent(), 'Beta');
  checks.push('Delayed Alpha cannot overwrite the newer Beta detail, even without transport abort');
  await record.getByRole('button', { name: '关闭', exact: true }).click();

  const replaced = holdRead();
  await alpha.click();
  await replaced.started.promise;
  await page.getByRole('button', { name: '记录 AP', exact: true }).click();
  const picker = page.getByRole('dialog', { name: '记录 AP', exact: true });
  replaced.release.resolve();
  await settle();
  assert.equal(await picker.count(), 1);
  assert.equal(await record.count(), 0);
  checks.push('Starting a new picker invalidates the previously pending detail');
  const closed = holdRead();
  await picker.getByRole('button', { name: /Alpha/ }).click();
  await closed.started.promise;
  await picker.getByRole('button', { name: '关闭', exact: true }).click();
  closed.release.resolve();
  await settle();
  assert.equal(await page.getByRole('dialog').count(), 0);
  checks.push('Closing the picker invalidates its pending detail request');

  const navigated = holdRead();
  await alpha.click();
  await navigated.started.promise;
  await page.getByRole('link', { name: '我的机厅', exact: true }).click();
  navigated.release.resolve();
  await settle();
  assert.equal(await page.getByRole('dialog').count(), 0);
  checks.push('Leaving the page invalidates pending detail responses');
  await page.getByRole('link', { name: 'AP 图鉴', exact: true }).click();
  await beta.click();
  await record.waitFor();
  await record.getByLabel('备注', { exact: true }).fill('Keep this unsaved draft');
  Object.assign(charts[1], { manual_revision: 2, note: 'Other device update' });
  await record.getByRole('button', { name: '保存 AP', exact: true }).click();
  await record.getByRole('alert').waitFor();
  assert.equal(
    await record.getByLabel('备注', { exact: true }).inputValue(),
    'Keep this unsaved draft',
  );
  assert(await record.getByRole('button', { name: '保存 AP', exact: true }).isDisabled());
  assert(await record.getByRole('button', { name: '撤销手工记录', exact: true }).isDisabled());
  assert.equal(charts[1].note, 'Other device update');
  assert.equal(mutations.at(-1).expected_revision, 1);
  checks.push('PUT conflict keeps the draft, preserves server changes and blocks stale writes');
  await page.screenshot({ path: path.join(output, 'desktop-conflict.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await record.getByRole('button', { name: '重新载入记录', exact: true }).scrollIntoViewIfNeeded();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: path.join(output, 'mobile-conflict.png') });
  await page.setViewportSize({ width: 1280, height: 900 });
  checks.push('Conflict form remains usable at 390px with no document overflow');
  await record.getByRole('button', { name: '重新载入记录', exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector('#manual-note')?.value === 'Other device update',
  );
  await record.getByLabel('备注', { exact: true }).fill('Reviewed update');
  await record.getByRole('button', { name: '保存 AP', exact: true }).click();
  await record.waitFor({ state: 'detached' });
  assert.equal(charts[1].note, 'Reviewed update');
  assert.equal(mutations.at(-1).expected_revision, 2);
  checks.push('Explicit reload restores the latest form and uses its revision for the next save');

  await beta.click();
  await record.waitFor();
  Object.assign(charts[1], { manual_revision: 4, note: 'Newer before deletion' });
  await record.getByRole('button', { name: '撤销手工记录', exact: true }).click();
  await record.getByRole('alert').waitFor();
  assert.equal(charts[1].note, 'Newer before deletion');
  assert.equal(mutations.at(-1).expected_revision, 3);
  await record.getByRole('button', { name: '重新载入记录', exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector('#manual-note')?.value === 'Newer before deletion',
  );
  await record.getByRole('button', { name: '撤销手工记录', exact: true }).click();
  await record.waitFor({ state: 'detached' });
  assert.equal(charts[1].manual_ap, null);
  assert.equal(mutations.at(-1).expected_revision, 4);
  checks.push(
    'DELETE rejects a stale revision and succeeds only after an explicit latest-record reload',
  );

  await beta.click();
  await record.waitFor();
  const write = { started: deferred(), release: deferred() };
  delayedWrite = write;
  await record.getByRole('button', { name: '保存 AP', exact: true }).click();
  await write.started.promise;
  await record.getByRole('button', { name: '关闭', exact: true }).click();
  await alpha.click();
  await record.getByRole('heading', { name: 'Alpha', exact: true }).waitFor();
  write.release.resolve();
  await settle();
  assert.equal(await record.getByRole('heading').textContent(), 'Alpha');
  checks.push('A completed write from a closed detail cannot close a subsequently opened detail');
  assert.deepEqual(errors, []);
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify({ checks, mutations, errors }, null, 2),
  );
  console.log(`Detail concurrency: ${checks.length} checks passed (isolated fixtures only).`);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
