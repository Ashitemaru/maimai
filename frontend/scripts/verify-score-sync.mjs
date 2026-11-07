import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { idleScoreSync } from './score-sync-fixture.mjs';

const root = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(root, 'frontend/dist'));
const output = process.env.SCORE_SYNC_AUDIT_OUTPUT || path.join(root, 'output/score-sync-audit');
await fs.mkdir(output, { recursive: true });
const token = 'DIVINGFISH-FIXTURE-NOT-A-REAL-TOKEN';
const checks = [],
  errors = [],
  requests = [];
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    const file = path.resolve(bundle, url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
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
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let syncStatus = structuredClone(idleScoreSync);
let configured = false,
  postError = '',
  statusError = '',
  failJob = false,
  pollReady = false,
  jobSequence = 0;
let pendingCount = 1;
const receipt = {
  ok: true,
  source: 'divingfish',
  records: 120,
  matched: 119,
  unmatched: 1,
  confirmed: 1,
  pending: 0,
  conflicts: 0,
  synced_at: '2026-10-13T04:20:00+08:00',
  sync_id: 'fixture-sync',
};
const catalog = {
  revision: 'score-sync',
  main: { status: 'succeeded', warnings: [], diff: {} },
  remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0 },
};
const browser = await launchBrowser();
try {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  await context.route('**/api/**', async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    requests.push({ path: pathname, method: request.method(), body: request.postData() });
    const send = (json, status = 200) => route.fulfill({ status, json });
    if (pathname === '/api/sync/divingfish') {
      assert.equal(request.method(), 'POST');
      assert.equal(request.postData(), null);
      if (postError) {
        const detail = postError;
        postError = '';
        return send({ detail }, 409);
      }
      syncStatus.job = {
        id: `sync-${++jobSequence}`,
        kind: 'divingfish',
        state: 'running',
        stage: 'fetching',
        message: '正在读取水鱼成绩',
        error: null,
        result: null,
      };
      return send(syncStatus.job, 202);
    }
    if (pathname === '/api/settings/divingfish' && request.method() === 'PUT') {
      configured = !!request.postDataJSON().token;
      syncStatus.divingfish.configured = configured;
      return send({ configured, source: configured ? 'local' : null, sync: null });
    }
    assert.equal(request.method(), 'GET');
    if (pathname === '/api/sync/status') {
      if (statusError) {
        const message = statusError;
        statusError = '';
        return send({ detail: message }, 503);
      }
      if (syncStatus.job?.state === 'running' && pollReady) {
        if (failJob) {
          syncStatus.job = {
            ...syncStatus.job,
            state: 'failed',
            stage: 'failed',
            error: { code: 'network_error', message: '暂时无法访问水鱼，本地记录已保留。' },
          };
          failJob = false;
        } else {
          syncStatus.divingfish.last_sync = receipt;
          syncStatus.job = {
            ...syncStatus.job,
            state: 'succeeded',
            stage: 'completed',
            result: receipt,
          };
          pendingCount = 0;
        }
      }
      return send(syncStatus);
    }
    const fixtures = {
      '/api/auth/session': { authenticated: true, auth_required: false },
      '/api/settings/preferences': { default_constant_min: 13, default_constant_max: 13.9 },
      '/api/settings/divingfish': { configured, source: configured ? 'local' : null, sync: null },
      '/api/catalog/update': catalog,
      '/api/catalog/records/review': {
        revision: catalog.revision,
        decision_revision: 0,
        records: [],
        counts: { admitted: 0, pending: 0 },
      },
      '/api/catalog/records/decisions': {
        revision: catalog.revision,
        decision_revision: 0,
        records: [],
      },
      '/api/chart-filters': { constants: [13, 13.9], versions: [], cn_versions: [] },
      '/api/venues': { items: [] },
      '/api/ap-records': { items: [], count: pendingCount },
      '/api/charts': { items: [], total: 0, offset: 0, limit: 12 },
      '/api/summary': {
        total: 0,
        complete: 0,
        ap: 0,
        remaining: 0,
        conflicts: 0,
        legacy_complete: 0,
        constants: [],
        levels: [],
        sync: null,
      },
    };
    assert(fixtures[pathname], `Unexpected API: ${pathname}`);
    return send(fixtures[pathname]);
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => {
    // WebKit reports fetches cancelled by a reload as uncaught access-control errors.
    if (!/\/api\/.* due to access control checks\.$/.test(error.message))
      errors.push(error.message);
  });
  const toast = (text) => page.locator('#toast').filter({ hasText: text }).waitFor();
  const syncPosts = () => requests.filter((item) => item.path === '/api/sync/divingfish').length;
  await page.goto(origin + '/#settings');
  const panel = page.getByRole('region', { name: '同步水鱼成绩', exact: true });
  const syncButton = () => panel.getByRole('button', { name: /从水鱼同步|正在同步…/ });
  await panel.waitFor();
  assert.equal(await page.getByText(/华立|二维码/).count(), 0, 'No Wahlap or QR controls remain');
  assert(await syncButton().isDisabled(), 'Sync needs a saved token');
  checks.push('Settings show only the Diving-Fish sync panel; sync is disabled without a token');

  await panel.getByLabel(/Import-Token/).fill(token);
  await panel.getByRole('button', { name: '保存 Token', exact: true }).click();
  await toast('已保存水鱼 Token');
  await panel.getByText('Import-Token · 已保存', { exact: true }).waitFor();
  assert.equal(await panel.getByLabel(/Import-Token/).inputValue(), '');
  assert(!(await syncButton().isDisabled()));
  checks.push('Saving a token clears the field and enables manual sync');

  await syncButton().click();
  await panel.getByRole('status').filter({ hasText: '正在读取水鱼成绩' }).waitFor();
  assert(await syncButton().isDisabled(), 'A running sync blocks a second start');
  pollReady = true;
  await toast('水鱼成绩已同步，待核记录已核对');
  await panel.locator('.score-sync-receipt').waitFor();
  const counts = await panel.locator('.score-sync-counts').innerText();
  assert.match(counts, /已同步\s*119/);
  assert.match(counts, /核准\s*1/);
  assert.equal(syncPosts(), 1);
  assert(
    !requests.some(
      (item) => item.body?.includes(token) && item.path !== '/api/settings/divingfish',
    ),
  );
  checks.push('Manual sync polls to completion, refreshes data and shows the receipt');

  pollReady = false;
  failJob = true;
  await syncButton().click();
  pollReady = true;
  await toast('暂时无法访问水鱼，本地记录已保留。');
  await panel.locator('.score-sync-receipt').waitFor();
  checks.push('A failed sync reports the error and keeps the previous receipt');

  postError = { code: 'sync_busy', message: '水鱼同步正在进行，请等待完成。' };
  await syncButton().click();
  await toast('水鱼同步正在进行，请等待完成。');
  checks.push('A refused start reports the server message');

  statusError = '同步状态暂时不可用';
  await page.reload();
  await toast('同步状态暂时不可用');
  await panel.getByRole('button', { name: '重试', exact: true }).click();
  await panel.locator('.score-sync-receipt').waitFor();
  checks.push('Status load failures can be retried without losing the receipt');

  await page.screenshot({ path: path.join(output, 'score-sync-mobile.png'), fullPage: true });
  assert.deepEqual(errors, []);
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify(
      { checks, errors, requests: requests.map(({ path, method }) => ({ path, method })) },
      null,
      2,
    ),
  );
  console.log(`Score sync UI: ${checks.length} checks passed.`);
} finally {
  await browser.close();
  server.close();
}
