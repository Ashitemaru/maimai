import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { idleScoreSync } from './score-sync-fixture.mjs';

const root = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(root, 'frontend/dist'));
const output = path.join(root, 'output/score-sync-audit');
await fs.mkdir(output, { recursive: true });
const qr = 'MAIMAI-SYNC-FIXTURE-NOT-A-REAL-CREDENTIAL';
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
  failJob = false,
  pollReady = false,
  jobSequence = 0;
let pendingCount = 1;
const receipt = {
  ok: true,
  source: 'wahlap',
  records: 120,
  matched: 119,
  unmatched: 1,
  confirmed: 1,
  pending: 0,
  conflicts: 0,
  synced_at: '2026-10-13T04:20:00+08:00',
  sync_id: 'fixture-sync',
  logout_confirmed: false,
  logout_status: 'unverified',
  notice: '成绩已同步，机台退出状态尚未确认。',
};
const catalog = {
  revision: 'score-sync',
  main: { status: 'succeeded', warnings: [], diff: {} },
  remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0 },
  can_rollback: false,
};
const browser = await launchBrowser();
try {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  });
  await context.addInitScript(() => {
    window.BarcodeDetector = undefined;
  });
  await context.route('**/api/**', async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    requests.push({ path: pathname, method: request.method(), body: request.postData() });
    const send = (json, status = 200) => route.fulfill({ status, json });
    if (pathname === '/api/sync/wahlap') {
      assert.equal(request.method(), 'POST');
      const data = request.postDataJSON();
      assert.deepEqual(Object.keys(data), ['qr_text']);
      assert(data.qr_text.length <= 4096);
      if (postError) {
        const message = postError;
        postError = '';
        return send({ detail: message }, 422);
      }
      syncStatus.job = {
        id: `sync-${++jobSequence}`,
        kind: 'wahlap',
        state: 'running',
        stage: 'fetching',
        message: '正在读取华立成绩…',
        error: null,
        result: null,
      };
      return send(syncStatus.job, 202);
    }
    if (pathname === '/api/sync/divingfish/upload') {
      assert.equal(request.method(), 'POST');
      assert.equal(request.postData(), null);
      assert(syncStatus.divingfish.configured && syncStatus.wahlap.has_snapshot);
      syncStatus.job = {
        id: `upload-${++jobSequence}`,
        kind: 'divingfish_upload',
        state: 'running',
        stage: 'uploading',
        message: '正在上传水鱼…',
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
      if (syncStatus.job?.state === 'running' && pollReady) {
        if (failJob) {
          syncStatus.job = {
            ...syncStatus.job,
            state: 'failed',
            stage: 'failed',
            error: { code: 'test_failed', message: '华立暂时无法连接' },
          };
          failJob = false;
        } else if (syncStatus.job.kind === 'wahlap') {
          syncStatus.wahlap.last_sync = receipt;
          syncStatus.wahlap.has_snapshot = true;
          syncStatus.job = {
            ...syncStatus.job,
            state: 'succeeded',
            stage: 'completed',
            result: receipt,
          };
          pendingCount = 0;
        } else {
          const uploaded = {
            ok: true,
            source: 'divingfish_upload',
            uploaded_at: '2026-10-13T04:25:00+08:00',
            records: 118,
            uploaded_count: 118,
            skipped_count: 2,
            snapshot_records: 120,
            sync_id: receipt.sync_id,
          };
          syncStatus.divingfish.last_upload = uploaded;
          syncStatus.job = {
            ...syncStatus.job,
            state: 'succeeded',
            stage: 'completed',
            result: uploaded,
          };
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
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(origin + '/#settings');
  const panel = page.getByRole('region', { name: '同步游戏成绩', exact: true });
  const uploadPanel = page.getByRole('region', { name: '上传水鱼', exact: true });
  const submit = () => panel.getByRole('button', { name: '同步华立成绩', exact: true });
  const upload = () => uploadPanel.getByRole('button', { name: '上传到水鱼', exact: true });
  await panel.getByText('测试环境未启用华立同步', { exact: true }).waitFor();
  assert(await submit().isDisabled());
  assert(await panel.getByRole('button', { name: '选择二维码截图', exact: true }).isDisabled());
  assert(await upload().isDisabled());
  syncStatus.wahlap.available = true;
  syncStatus.wahlap.reason = null;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await panel.getByText('首次同步尚未验证。', { exact: true }).waitFor();
  await page.screenshot({ path: path.join(output, 'mobile-ready.png'), fullPage: true });
  checks.push('Unavailable server disables QR submit honestly; first unverified sync is labelled');
  await panel
    .getByLabel('二维码截图', { exact: true })
    .setInputFiles(path.join(import.meta.dirname, 'fixtures/maimai-sync-qr.png'));
  await panel.getByText('二维码已识别', { exact: true }).waitFor();
  assert.equal(requests.filter((item) => item.method === 'POST').length, 0);
  assert(
    await page.evaluate(
      (value) => !Object.values(localStorage).some((item) => item.includes(value)),
      qr,
    ),
  );
  assert.equal(await panel.getByLabel('或粘贴二维码内容').inputValue(), qr);
  await submit().click();
  await panel.getByText('正在读取华立成绩…', { exact: true }).waitFor();
  assert.equal(
    JSON.parse(requests.find((item) => item.path === '/api/sync/wahlap').body).qr_text,
    qr,
  );
  assert.equal(await panel.getByLabel('或粘贴二维码内容').inputValue(), '');
  assert.equal(await panel.getByLabel('二维码截图').inputValue(), '');
  assert(await panel.getByRole('button', { name: '正在同步…', exact: true }).isDisabled());
  const refreshBefore = requests.filter((item) => item.path === '/api/ap-records').length;
  await page.getByRole('link', { name: 'AP 图鉴', exact: true }).click();
  pollReady = true;
  await page.getByText('华立成绩已同步，待核记录已核对', { exact: true }).waitFor();
  assert(requests.filter((item) => item.path === '/api/ap-records').length > refreshBefore);
  await page.getByRole('link', { name: /待核记录/ }).click();
  await page.getByText('0 条待核记录', { exact: true }).waitFor();
  checks.push(
    'Screenshot decodes without BarcodeDetector, sends only QR text, clears input, and sync completes after leaving settings',
  );
  await page.getByRole('link', { name: '数据与同步', exact: true }).click();
  await panel.getByText(receipt.notice, { exact: true }).waitFor();
  await panel.getByText('2026-10-13 04:20', { exact: true }).waitFor();
  assert(await upload().isDisabled());
  await uploadPanel.getByLabel('Import-Token', { exact: true }).fill('synthetic-upload-token');
  await uploadPanel.getByRole('button', { name: '保存 Token', exact: true }).click();
  await page.waitForFunction(() =>
    [...document.querySelectorAll('button')].some(
      (item) => item.textContent === '上传到水鱼' && !item.disabled,
    ),
  );
  assert.equal(requests.filter((item) => item.path === '/api/sync/divingfish/upload').length, 0);
  await upload().click();
  await uploadPanel.getByText('2026-10-13 04:25', { exact: true }).waitFor();
  await uploadPanel.getByText('已上传 118 条 · 跳过 2 条', { exact: true }).waitFor();
  assert.equal(requests.filter((item) => item.path === '/api/sync/divingfish/upload').length, 1);
  await page.screenshot({ path: path.join(output, 'mobile-complete.png'), fullPage: true });
  checks.push(
    'Upload is explicit, requires Token and snapshot, reports skipped rows, and no exit confirmation is invented',
  );
  postError = '二维码已过期，请重新获取';
  await panel.getByLabel('或粘贴二维码内容').fill('fixture-expired');
  await submit().click();
  await panel.getByRole('alert').filter({ hasText: '二维码已过期，请重新获取' }).waitFor();
  assert.equal(await panel.getByLabel('或粘贴二维码内容').inputValue(), '');
  assert.equal(pendingCount, 0);
  pollReady = false;
  failJob = true;
  await panel.getByLabel('或粘贴二维码内容').fill('fixture-job-failure');
  await submit().click();
  await panel.getByText('正在读取华立成绩…', { exact: true }).waitFor();
  pollReady = true;
  await panel.getByRole('alert').filter({ hasText: '华立暂时无法连接' }).waitFor();
  assert.equal(syncStatus.wahlap.last_sync.synced_at, receipt.synced_at);
  assert.equal(pendingCount, 0);
  checks.push(
    'Expired QR and asynchronous failure remain visible without replacing successful data',
  );
  await panel.getByLabel('二维码截图').setInputFiles({
    name: 'blank.svg',
    mimeType: 'image/svg+xml',
    buffer: Buffer.from(
      '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300"><rect width="300" height="300" fill="white"/></svg>',
    ),
  });
  await panel.getByText('没有识别到二维码，请选择完整清晰的截图', { exact: true }).waitFor();
  assert(await submit().isDisabled());
  syncStatus.wahlap.has_snapshot = false;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await uploadPanel.getByText('请先完成一次华立同步。', { exact: true }).waitFor();
  assert(await upload().isDisabled());
  assert.equal(requests.filter((item) => item.path === '/api/sync/divingfish').length, 0);
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
  checks.push(
    'Unreadable image stays local; no snapshot disables upload; mobile layout fits; old pull endpoint is unused',
  );
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
  await new Promise((resolve) => server.close(resolve));
}
