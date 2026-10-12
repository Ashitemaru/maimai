import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.resolve(
  process.env.BUFFER_AUDIT_OUTPUT || path.join(project, 'output/ap-buffer-audit'),
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
  requires_venue: true,
  in_original_table: false,
  legacy_complete: false,
  legacy_venue: '',
  manual_ap: true,
  manual_revision: 0,
  record_revision: 1,
  pending_record: {
    id: `pending-${index}`,
    venue: 'Original venue',
    revision: 1,
    reason: '待同步核实',
    created_at: '2026-10-12T01:00:00+08:00',
    updated_at: '2026-10-12T01:00:00+08:00',
  },
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
const requests = new Map();
let failWriteOnce = true;
let syncStage = 0;
let delaySync = null;
let syncFailure = false;
charts.forEach((chart, index) =>
  Object.assign(chart, {
    record_revision: 0,
    pending_record: null,
    manual_ap: null,
    provider_fc: index ? 'app' : null,
    provider_achievement: index ? 101 : null,
    is_ap: Boolean(index),
    is_complete: Boolean(index),
    status: index ? 'app' : 'incomplete',
    venue_pending: Boolean(index),
    provider_dx_score: index ? 2400 : null,
    provider_fs: index ? 'fsdp' : null,
    provider_rate: index ? 'sssp' : null,
    provider_ra: index ? 290 : null,
  }),
);
const queue = () => ({
  items: charts.filter((chart) => chart.pending_record),
  count: charts.filter((chart) => chart.pending_record).length,
});
const counts = () => ({
  ...summary,
  total: charts.length,
  complete: charts.filter((chart) => chart.is_complete).length,
  ap: charts.filter((chart) => chart.is_ap).length,
  remaining: charts.filter((chart) => !chart.is_complete).length,
  constants: [
    { constant: 13.2, total: 2, complete: charts.filter((chart) => chart.is_complete).length },
  ],
});
try {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  await context.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = decodeURIComponent(url.pathname);
    const method = request.method();
    const record = pathname.match(/^\/api\/charts\/([^/]+)\/ap-record$/);
    if (record) {
      const chart = charts.find((item) => item.chart_key === record[1]);
      assert(chart);
      if (method === 'PUT') {
        const body = request.postDataJSON();
        assert.deepEqual(Object.keys(body).sort(), ['expected_revision', 'request_id', 'venue']);
        mutations.push({ ...body, chart_key: chart.chart_key });
        if (requests.has(body.request_id)) {
          assert.deepEqual(requests.get(body.request_id), body);
          await route.fulfill({ json: { chart } });
          return;
        }
        assert.equal(body.expected_revision, chart.record_revision);
        requests.set(body.request_id, body);
        chart.record_revision += 1;
        chart.pending_record = {
          id: body.request_id,
          venue: body.venue,
          revision: chart.record_revision,
          created_at: '2026-10-12T18:00:00+08:00',
          updated_at: '2026-10-12T18:00:00+08:00',
          reason: '待同步核实',
        };
        if (failWriteOnce) {
          failWriteOnce = false;
          await route.abort('failed');
          return;
        }
        await route.fulfill({ json: { chart } });
        return;
      }
      assert.equal(method, 'DELETE');
      assert.equal(Number(url.searchParams.get('expected_revision')), chart.record_revision);
      chart.record_revision += 1;
      chart.pending_record = null;
      await route.fulfill({ json: { chart } });
      return;
    }
    if (pathname === '/api/sync/divingfish') {
      assert.equal(method, 'POST');
      if (delaySync) {
        const delay = delaySync;
        delaySync = null;
        delay.started.resolve();
        await delay.release.promise;
      }
      if (syncFailure) {
        syncFailure = false;
        await route.fulfill({ status: 502, json: { detail: '水鱼暂时无法连接' } });
        return;
      }
      let confirmed = 0;
      for (const chart of charts) {
        if (!chart.pending_record) continue;
        if (syncStage === 0 && !chart.is_ap) {
          chart.pending_record.reason = '水鱼暂未显示 AP';
          continue;
        }
        chart.venue = chart.pending_record.venue;
        chart.pending_record = null;
        chart.record_revision += 1;
        Object.assign(chart, {
          provider_fc: 'ap',
          provider_achievement: 100.9876,
          provider_synced_at: '2026-10-12T22:00:00+08:00',
          provider_dx_score: 2450,
          provider_fs: 'fsdp',
          provider_rate: 'sssp',
          provider_ra: 290,
          is_ap: true,
          is_complete: true,
          status: 'ap',
          venue_pending: false,
        });
        confirmed += 1;
      }
      await route.fulfill({
        json: {
          ok: true,
          records: 2,
          matched: 2,
          unmatched: 0,
          conflicts: 0,
          confirmed,
          pending: queue().count,
          synced_at: '2026-10-12T22:00:00+08:00',
        },
      });
      return;
    }
    assert.equal(method, 'GET');
    const detail = pathname.match(/^\/api\/charts\/([^/]+)$/);
    const fixtures = {
      '/api/auth/session': { authenticated: true, auth_required: false },
      '/api/settings/preferences': { default_constant_min: 13, default_constant_max: 13.9 },
      '/api/settings/divingfish': { configured: true, source: 'local', sync: null },
      '/api/chart-filters': { constants: [13, 13.9], versions: [], cn_versions: [] },
      '/api/catalog/update': catalog,
      '/api/venues': {
        items: [{ name: '【上海】最近机厅', count: 2, last_used_at: '2026-10-12T18:00:00+08:00' }],
      },
      '/api/ap-records': queue(),
      '/api/summary': counts(),
      '/api/charts': { items: charts, total: charts.length, offset: 0, limit: 12 },
      '/api/completion-table': {
        items: charts,
        total: charts.length,
        summary: counts(),
        generated_at: '2026-10-12T18:00:00+08:00',
      },
    };
    const data = detail
      ? charts.find((chart) => chart.chart_key === detail[1])
      : fixtures[pathname];
    assert(data, `Unmocked ${pathname}`);
    await route.fulfill({ json: data });
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  const drawer = page.getByRole('dialog', { name: '谱面记录', exact: true });
  const alpha = page.locator('.completion-tile[data-chart-key="9100:3"]');
  const beta = page.locator('.completion-tile[data-chart-key="9101:3"]');
  const save = () => drawer.getByRole('button', { name: '保存待核记录', exact: true }).click();
  const close = () => drawer.getByRole('button', { name: '关闭', exact: true }).click();
  const settle = async () => {
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  };
  await page.goto(origin + '/?view=completion#ap');
  await alpha.click();
  await drawer.waitFor();
  assert.equal(await drawer.locator('input').count(), 1);
  assert.equal(await drawer.locator('textarea').count(), 0);
  assert.equal(
    await drawer.getByLabel('AP 机厅', { exact: true }).inputValue(),
    '【上海】最近机厅',
  );
  await drawer.getByLabel('AP 机厅', { exact: true }).fill('【广州】新机厅');
  await save();
  await drawer.getByRole('alert').waitFor();
  assert.equal(await drawer.getByLabel('AP 机厅', { exact: true }).inputValue(), '【广州】新机厅');
  await save();
  await drawer.waitFor({ state: 'detached' });
  assert.equal(mutations[0].request_id, mutations[1].request_id);
  assert.equal(charts[0].record_revision, 1);
  assert.equal(await alpha.getAttribute('data-complete'), 'false');
  assert.equal(await alpha.locator('.completion-result').count(), 0);
  await alpha.getByLabel('待核实', { exact: true }).waitFor();
  checks.push(
    'Mobile venue-only form uses recent venue; an accepted request with lost response retries idempotently without claiming AP',
  );
  await page.reload();
  await alpha.getByLabel('待核实', { exact: true }).waitFor();
  await page.getByRole('link', { name: /待核记录/ }).click();
  await page.getByText('【广州】新机厅', { exact: true }).waitFor();
  await page.getByRole('button', { name: '修改登记', exact: true }).click();
  await drawer.getByLabel('AP 机厅', { exact: true }).fill('');
  await drawer.getByRole('button', { name: '【上海】最近机厅', exact: true }).click();
  await save();
  await drawer.waitFor({ state: 'detached' });
  assert.equal(charts[0].pending_record.venue, '【上海】最近机厅');
  await page.getByRole('button', { name: '取消登记', exact: true }).click();
  await page.getByText('暂无待核记录', { exact: true }).waitFor();
  assert.equal(charts[0].is_ap, false);
  checks.push(
    'Server queue survives reload; existing-venue selection, edit and cancellation preserve AP facts',
  );
  await page.getByRole('link', { name: 'AP 图鉴', exact: true }).click();
  await alpha.click();
  await drawer.getByLabel('AP 机厅', { exact: true }).fill('【广州】新机厅');
  await save();
  await drawer.waitFor({ state: 'detached' });
  await page.getByRole('link', { name: /待核记录/ }).click();
  await page.getByRole('button', { name: '同步水鱼', exact: true }).click();
  await page.getByText('水鱼暂未显示 AP', { exact: true }).waitFor();
  assert.equal(charts[0].is_ap, false);
  syncFailure = true;
  await page.getByRole('button', { name: '同步水鱼', exact: true }).click();
  await page.getByText('水鱼暂时无法连接', { exact: true }).waitFor();
  assert.equal(queue().count, 1);
  checks.push('Non-AP results and failed sync retain pending rows and leave completion unchanged');
  syncStage = 1;
  await page.getByRole('button', { name: '同步水鱼', exact: true }).click();
  await page.getByText('暂无待核记录', { exact: true }).waitFor();
  assert.equal(charts[0].venue, '【广州】新机厅');
  await page.getByRole('link', { name: 'AP 图鉴', exact: true }).click();
  await alpha.locator('.completion-result').waitFor();
  assert.equal(await alpha.getAttribute('data-complete'), 'true');
  await alpha.click();
  await drawer.getByText('水鱼成绩', { exact: true }).click();
  await drawer.getByText('100.9876%', { exact: true }).waitFor();
  await drawer.getByText('2450', { exact: true }).waitFor();
  await drawer.getByText('FDX+', { exact: true }).waitFor();
  await close();
  checks.push(
    'Confirmed sync commits venue and AP mask; provider achievement and other score fields remain readonly',
  );
  await beta.click();
  await drawer.getByText('机厅待补', { exact: true }).waitFor();
  await drawer.getByLabel('AP 机厅', { exact: true }).fill('【潮州】第二机厅');
  await save();
  await drawer.waitFor({ state: 'detached' });
  await beta.locator('.completion-result').waitFor();
  await beta.getByLabel('待核实', { exact: true }).waitFor();
  await beta.click();
  await drawer.getByRole('button', { name: '取消登记', exact: true }).click();
  await drawer.waitFor({ state: 'detached' });
  assert.equal(charts[1].is_ap, true);
  checks.push(
    'Already-confirmed AP remains complete throughout pending venue edits and cancellation',
  );
  const delay = { started: deferred(), release: deferred() };
  delaySync = delay;
  await page.getByRole('button', { name: '同步水鱼', exact: true }).click();
  await delay.started.promise;
  await alpha.click();
  await drawer.getByLabel('AP 机厅', { exact: true }).fill('Do not lose this draft');
  delay.release.resolve();
  await settle();
  assert.equal(
    await drawer.getByLabel('AP 机厅', { exact: true }).inputValue(),
    'Do not lose this draft',
  );
  await page.screenshot({ path: path.join(output, 'mobile-record.png') });
  await close();
  checks.push('Delayed sync cannot close a later-opened form or discard its unsaved venue');
  charts[1].record_revision += 1;
  charts[1].pending_record = {
    id: 'external',
    venue: '【手机】另一个设备',
    revision: charts[1].record_revision,
    reason: '待同步核实',
    created_at: '2026-10-12T23:00:00+08:00',
    updated_at: '2026-10-12T23:00:00+08:00',
  };
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await beta.getByLabel('待核实', { exact: true }).waitFor();
  await page.getByRole('link', { name: /待核记录/ }).click();
  await page.getByText('【手机】另一个设备', { exact: true }).waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: path.join(output, 'mobile-queue.png') });
  await page.setViewportSize({ width: 320, height: 720 });
  assert(
    await page.evaluate(() => {
      const labels = [...document.querySelectorAll('.nav-mobile-label')].map((element) =>
        element.getBoundingClientRect(),
      );
      return (
        document.documentElement.scrollWidth <= innerWidth + 1 &&
        labels.every((rect, index) => !index || labels[index - 1].right < rect.left)
      );
    }),
  );
  await page.screenshot({ path: path.join(output, 'narrow-queue.png') });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({ path: path.join(output, 'desktop-queue.png') });
  checks.push(
    'Focus refresh brings another device’s queue into mobile UI without horizontal overflow',
  );
  assert.deepEqual(errors, []);
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify({ checks, errors, mutations }, null, 2),
  );
  console.log(`AP buffer UI: ${checks.length} checks passed (isolated fixtures only).`);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
