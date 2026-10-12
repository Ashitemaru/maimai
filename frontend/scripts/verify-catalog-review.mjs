import assert from 'node:assert/strict';
import { idleScoreSync } from './score-sync-fixture.mjs';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.CATALOG_REVIEW_DIST || path.join(project, 'frontend/dist'));
const output = path.join(project, 'output/catalog-review-batch');
await fs.mkdir(output, { recursive: true });
const server = http.createServer(async (req, res) => {
  try {
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    const file = path.resolve(bundle, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!file.startsWith(bundle + path.sep)) {
      res.writeHead(403);
      res.end();
      return;
    }
    const bytes = await fs.readFile(file);
    res.setHeader(
      'Content-Type',
      {
        '.html': 'text/html',
        '.js': 'text/javascript',
        '.css': 'text/css',
        '.svg': 'image/svg+xml',
        '.png': 'image/png',
      }[path.extname(file)] || 'application/octet-stream',
    );
    res.end(bytes);
  } catch {
    res.writeHead(404);
    res.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await launchBrowser();
const page = await browser.newPage({ viewport: { width: 1450, height: 1100 } });
const errors = [],
  reportRequests = [],
  posts = [],
  checks = [];
page.on('pageerror', (error) => errors.push(error.message));
let status = {
  revision: 'r1',
  decision_revision: 0,
  applied_decision_revision: 0,
  decisions_pending: false,
  snapshot_at: '2026-10-11T01:00:00Z',
  main: { status: 'idle', stage: '', message: '', warnings: [] },
  remy: {
    status: 'complete',
    pending_count: 0,
    verified_count: 134,
    updated_count: 0,
    message: '',
  },
  can_rollback: false,
};
const fields = [
  {
    field_key: 'title',
    label: '曲名',
    required: true,
    options: [
      { option_id: 'title-lxns', source: 'lxns', value: 'Link' },
      { option_id: 'title-divingfish', source: 'divingfish', value: 'Link(CoF)' },
    ],
  },
  {
    field_key: 'constant',
    label: '定数',
    required: true,
    options: [
      { option_id: 'constant-lxns', source: 'lxns', value: 4 },
      { option_id: 'constant-divingfish', source: 'divingfish', value: 4.1 },
    ],
  },
];
let records = [
  ...Array.from({ length: 5 }, (_, difficulty) => ({
    id: 383,
    difficulty,
    title: 'Link',
    difficulty_name: ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'][difficulty],
  })),
  { id: 617, difficulty: 3, title: 'Cherry', difficulty_name: 'Master' },
  { id: 700, difficulty: 3, title: 'Untouched', difficulty_name: 'Master' },
].map((record) => ({
  ...record,
  chart_key: `${record.id}:${record.difficulty}`,
  type: 'standard',
  status: 'pending',
  fields: structuredClone(fields),
}));
let saved = [],
  postGate = null,
  pendingKeys = [],
  failNext = false;
function gate() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function complete() {
  records = records.filter((record) => !pendingKeys.includes(record.chart_key));
  pendingKeys = [];
  status = {
    ...status,
    revision: 'r' + (Number(status.revision.slice(1)) + 1),
    applied_decision_revision: status.decision_revision,
    decisions_pending: false,
    main: { status: 'succeeded', stage: '', message: '裁决已应用', warnings: [] },
  };
}
await page.route('**/api/**', async (route) => {
  if (new URL(route.request().url()).pathname === '/api/auth/session') {
    await route.fulfill({ json: { authenticated: true, auth_required: false } });
    return;
  }
  const request = route.request(),
    pathname = new URL(request.url()).pathname;
  if (pathname === '/api/catalog/update/report') {
    reportRequests.push(pathname);
    await route.fulfill({ json: { diff: {}, issues: [], sources: {} } });
    return;
  }
  if (pathname === '/api/catalog/update') {
    await route.fulfill({ json: status });
    return;
  }
  if (pathname === '/api/catalog/records/review') {
    await route.fulfill({
      json: {
        schema_version: 2,
        revision: status.revision,
        decision_revision: status.decision_revision,
        decisions_pending: status.decisions_pending,
        counts: { total: 5564, admitted: 5564 - records.length, pending: records.length },
        records,
      },
    });
    return;
  }
  if (pathname === '/api/catalog/records/decisions' && request.method() === 'GET') {
    await route.fulfill({
      json: {
        revision: status.revision,
        decision_revision: status.decision_revision,
        decisions_pending: status.decisions_pending,
        records: saved,
      },
    });
    return;
  }
  if (pathname === '/api/catalog/records/decisions/batch' && request.method() === 'POST') {
    const body = request.postDataJSON();
    posts.push(body);
    assert.deepEqual(Object.keys(body).sort(), [
      'base_catalog_revision',
      'base_decision_revision',
      'records',
    ]);
    assert(body.records.length > 0);
    assert.equal(
      new Set(body.records.map((record) => `${record.id}:${record.difficulty}`)).size,
      body.records.length,
    );
    for (const record of body.records) assert.equal(record.choices.length, 2);
    if (failNext) {
      failNext = false;
      await route.fulfill({ status: 409, json: { detail: '曲库已变化，请刷新后重新选择。' } });
      return;
    }
    if (postGate) await postGate.promise;
    pendingKeys = body.records.map((item) => {
      const record = records.find(
        (row) => row.id === item.id && row.difficulty === item.difficulty,
      );
      assert(record, 'Submitted chart must still be pending');
      record.decision_pending = true;
      saved.push({
        ...record,
        fields: item.choices.map((choice) => {
          const field = record.fields.find((row) => row.field_key === choice.field_key);
          const option = field.options.find((row) => row.option_id === choice.option_id);
          assert(option);
          return {
            field_key: choice.field_key,
            label: field.label,
            value: option.value,
            source: option.source,
          };
        }),
      });
      return record.chart_key;
    });
    status = {
      ...status,
      decision_revision: status.decision_revision + 1,
      decisions_pending: true,
      main: { status: 'running', stage: 'decisions', message: '正在入库', warnings: [] },
    };
    await route.fulfill({ status: 202, json: status });
    return;
  }
  const defaults = {
    '/api/settings/preferences': { default_constant_min: 1, default_constant_max: 15 },
    '/api/sync/status': idleScoreSync,
    '/api/settings/divingfish': { configured: false, sync: null },
    '/api/venues': { items: [] },
    '/api/ap-records': { items: [], count: 0 },
    '/api/chart-filters': { constants: [1, 13.9, 15], versions: [] },
    '/api/summary': {
      total: 5560,
      complete: 0,
      ap: 0,
      remaining: 5560,
      conflicts: 0,
      legacy_complete: 0,
      levels: [],
      constants: [],
      sync: null,
    },
    '/api/charts': { items: [], total: 0, offset: 0, limit: 12 },
  };
  if (pathname in defaults) {
    await route.fulfill({ json: defaults[pathname] });
    return;
  }
  errors.push(`Unexpected API: ${request.method()} ${pathname}`);
  await route.fulfill({ status: 500, json: { detail: 'Unmocked API' } });
});
await page.route('**/*', async (route) => {
  if (new URL(route.request().url()).origin !== origin) {
    errors.push('External request blocked');
    await route.abort();
  } else await route.fallback();
});
const review = () => page.locator('.catalog-review');
const entries = () => review().locator('.record-review-list > .record-review-entry');
const entry = (id, difficulty) =>
  entries()
    .filter({ has: page.getByText(`ID ${id}`, { exact: true }) })
    .filter({ has: page.locator(`.chart-meta .difficulty[data-difficulty="${difficulty}"]`) });
const globalConfirm = () => review().getByRole('button', { name: '确认选择', exact: true });
const dialog = () => page.getByRole('dialog');
const modalConfirm = () => dialog().getByRole('button', { name: '确认提交', exact: true });
const cancel = () => dialog().getByRole('button', { name: '返回修改', exact: true });
const search = () => page.getByRole('searchbox', { name: '搜索曲库差异' });
const choose = async (id, difficulty) => {
  for (const row of await entry(id, difficulty).locator('tbody tr').all())
    await row.locator('input').first().check();
};
const waitFor = async (predicate, label) => {
  const until = Date.now() + 10000;
  while (Date.now() < until) {
    if (await predicate()) return;
    await page.waitForTimeout(40);
  }
  throw new Error('Timed out: ' + label);
};
const checkSummary = async () => {
  assert.equal(await dialog().locator('.record-confirm-entry').count(), 4);
  assert.equal(await dialog().locator('.record-confirm-table tbody tr').count(), 8);
  for (const [id, difficulty] of [
    [383, 0],
    [383, 1],
    [383, 4],
    [617, 3],
  ]) {
    const summary = dialog()
      .locator('.record-confirm-entry')
      .filter({ has: page.getByText(`ID ${id}`, { exact: true }) })
      .filter({ has: page.locator(`.chart-meta .difficulty[data-difficulty="${difficulty}"]`) });
    assert.equal(await summary.count(), 1);
    const title = summary
      .locator('tbody tr')
      .filter({ has: page.getByRole('rowheader', { name: '曲名', exact: true }) });
    assert.match(await title.innerText(), id === 383 && difficulty === 1 ? /Link\(CoF\)/ : /Link/);
    assert.match(await title.innerText(), id === 383 && difficulty === 1 ? /水鱼/ : /落雪/);
    const constant = summary
      .locator('tbody tr')
      .filter({ has: page.getByRole('rowheader', { name: '定数', exact: true }) });
    assert.match(await constant.innerText(), /4/);
    assert.match(await constant.innerText(), /落雪/);
  }
};
try {
  await page.goto(origin + '/#settings', { waitUntil: 'networkidle' });
  assert.equal(reportRequests.length, 0, 'No catalog changes must not fetch the detailed report');
  assert.equal(await page.locator('.catalog-changes').count(), 0);
  checks.push('An unchanged catalog does not mount or fetch an empty changes section');
  await page.getByRole('heading', { name: '曲库差异', exact: true }).waitFor();
  assert.equal(await globalConfirm().count(), 1);
  assert.equal(await globalConfirm().isDisabled(), true);
  assert.equal(await review().getByRole('button', { name: '确认合并', exact: true }).count(), 0);
  assert.equal(await entries().locator('.record-review-footer .btn.primary').count(), 0);
  await choose(383, 0);
  await choose(383, 1);
  await entry(383, 1).locator('input[value="title-divingfish"]').check();
  await entry(383, 2).locator('input[value="title-lxns"]').check();
  assert.equal(await globalConfirm().isDisabled(), true);
  assert.match(
    await review().locator('.record-batch-actions').innerText(),
    /还有 1 张谱面的字段未选完/,
  );
  await entry(383, 2).getByRole('button', { name: '清空选择', exact: true }).click();
  assert.equal(await globalConfirm().isEnabled(), true);
  checks.push(
    'Exactly one global confirmation; no per-chart submit; touched incomplete chart blocks the whole batch',
  );
  await page.getByRole('button', { name: '下一页曲库差异' }).click();
  await choose(383, 4);
  await search().fill('Cherry');
  await choose(617, 3);
  assert.equal(await entries().count(), 1);
  await globalConfirm().click();
  await dialog().waitFor();
  await checkSummary();
  assert.equal(posts.length, 0);
  assert.equal(await dialog().evaluate((element) => element.matches(':modal')), true);
  assert.equal(await cancel().evaluate((element) => element === document.activeElement), true);
  await page.evaluate(() => document.querySelector('input[aria-label="搜索曲库差异"]').focus());
  assert.equal(
    await dialog().evaluate((element) => element.contains(document.activeElement)),
    true,
  );
  await page.keyboard.press('Shift+Tab');
  // Native dialogs may focus browser chrome at the boundary, never background controls.
  assert.equal(
    await dialog().evaluate(
      (element) =>
        document.activeElement === document.body || element.contains(document.activeElement),
    ),
    true,
  );
  await page.keyboard.press('Tab');
  assert.equal(
    await dialog().evaluate((element) => element.contains(document.activeElement)),
    true,
  );
  await page.screenshot({ path: path.join(output, 'summary.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const modalBounds = await dialog().boundingBox();
  assert(
    modalBounds.x >= 0 &&
      modalBounds.x + modalBounds.width <= 390 &&
      modalBounds.y >= 0 &&
      modalBounds.y + modalBounds.height <= 844,
  );
  await page.screenshot({ path: path.join(output, 'summary-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1450, height: 1100 });
  checks.push(
    'Modal includes all selected charts across pages/search with exact ID, difficulty, values and sources',
  );
  checks.push(
    'Native modal focuses cancel and blocks outside focus; mobile summary fits viewport without horizontal overflow',
  );
  await cancel().click();
  assert.equal(await dialog().count(), 0);
  assert.equal(await entry(617, 3).locator('input:checked').count(), 2);
  await globalConfirm().click();
  await checkSummary();
  await page.keyboard.press('Escape');
  assert.equal(await dialog().count(), 0);
  assert.equal(posts.length, 0);
  assert.equal(await entry(617, 3).locator('input:checked').count(), 2);
  checks.push('Cancel and Escape send zero requests and preserve all selected values');

  await globalConfirm().click();
  await checkSummary();
  status = { ...status, revision: 'r2' };
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await waitFor(
    () =>
      dialog().evaluateAll(
        (elements) =>
          elements.length === 0 ||
          [...elements[0].querySelectorAll('button')].some(
            (button) => button.textContent.trim() === '确认提交' && button.disabled,
          ),
      ),
    'catalog revision invalidates open confirmation',
  );
  assert.equal(posts.length, 0);
  if (await dialog().count()) await cancel().click();
  await waitFor(() => globalConfirm().isEnabled(), 'fresh evidence allows a new summary');
  await globalConfirm().click();
  await checkSummary();
  checks.push(
    'Catalog revision change invalidates open summary without POST; compatible draft survives for fresh review',
  );

  postGate = gate();
  await modalConfirm().click();
  await waitFor(() => posts.length === 1, 'batch POST starts');
  assert.equal(posts[0].base_catalog_revision, 'r2');
  assert.equal(posts[0].base_decision_revision, 0);
  assert.deepEqual(
    new Set(posts[0].records.map((record) => `${record.id}:${record.difficulty}`)),
    new Set(['383:0', '383:1', '383:4', '617:3']),
  );
  await waitFor(
    async () =>
      /提交中/.test(await review().innerText()) || /提交中/.test(await dialog().innerText()),
    'immediate submitting feedback',
  );
  assert.equal((await page.locator('.spinner').count()) > 0, true);
  if (await dialog().count()) {
    assert.equal(await dialog().getByRole('button', { name: /提交/ }).isDisabled(), true);
    await dialog()
      .getByRole('button', { name: /提交/ })
      .evaluate((button) => {
        button.click();
        button.click();
      });
  }
  await page.waitForTimeout(150);
  assert.equal(posts.length, 1);
  await page.screenshot({ path: path.join(output, 'submitting.png'), fullPage: true });
  postGate.resolve();
  postGate = null;
  await waitFor(
    async () => status.main.status === 'running' && /入库|应用/.test(await review().innerText()),
    'batch applying feedback',
  );
  assert.equal(posts.length, 1);
  complete();
  await search().fill('');
  await waitFor(
    async () =>
      (await entries().count()) === 3 &&
      !(await review()
        .locator('.record-review-list')
        .evaluate((element) => element.getAttribute('aria-busy') === 'true')),
    'batch publication completes',
  );
  for (const [id, difficulty] of [
    [383, 0],
    [383, 1],
    [383, 4],
    [617, 3],
  ])
    assert.equal(await entry(id, difficulty).count(), 0);
  for (const [id, difficulty] of [
    [383, 2],
    [383, 3],
    [700, 3],
  ])
    assert.equal(await entry(id, difficulty).count(), 1);
  assert.equal(await entries().locator('input:checked').count(), 0);
  assert.equal(await review().locator('[role=alert]').count(), 0);
  assert.equal(posts.length, 1);
  checks.push(
    'One modal confirmation sends one atomic batch; spinner/disabled controls prevent duplicates; only selected charts disappear',
  );
  await page.screenshot({ path: path.join(output, 'completed.png'), fullPage: true });

  await choose(383, 2);
  failNext = true;
  await globalConfirm().click();
  await modalConfirm().click();
  await waitFor(async () => (await page.getByRole('alert').count()) > 0, 'stale batch reports 409');
  assert.equal(posts.length, 2);
  assert.equal(posts[1].base_catalog_revision, 'r3');
  assert.equal(posts[1].base_decision_revision, 1);
  if (await dialog().count()) {
    assert.equal(await modalConfirm().isDisabled(), true);
    await cancel().click();
  }
  assert.equal(await entry(383, 2).locator('input:checked').count(), 2);
  assert.equal(await globalConfirm().isDisabled(), true);
  await page.waitForTimeout(400);
  assert.equal(posts.length, 2);
  checks.push(
    'Real 409 keeps every draft, blocks stale summary/global confirmation and never retries automatically',
  );
  status = { ...status, revision: 'r4', decision_revision: 2, applied_decision_revision: 2 };
  records.find((record) => record.chart_key === '383:2').fields[0].options[0] = {
    option_id: 'title-lxns-changed',
    source: 'lxns',
    value: 'Link (new spelling)',
  };
  await page.getByRole('button', { name: '刷新差异', exact: true }).click();
  await waitFor(
    async () =>
      (await review().locator('[role=alert]').count()) === 0 &&
      (await entry(383, 2).locator('input[value="title-lxns-changed"]').count()) === 1,
    'explicit refresh adopts new evidence',
  );
  assert.equal(await entry(383, 2).locator('input:checked').count(), 1);
  assert.equal(await entry(383, 2).locator('input[value="constant-lxns"]').isChecked(), true);
  assert.equal(await entry(383, 2).locator('input[value="title-lxns-changed"]').isChecked(), false);
  assert.equal(await globalConfirm().isDisabled(), true);
  assert.equal(posts.length, 2);
  checks.push(
    'Explicit refresh retains unchanged choices, clears only invalid options and requires completing affected chart',
  );
  await entry(383, 2).locator('input[value="title-lxns-changed"]').check();
  await globalConfirm().click();
  assert.equal(await dialog().locator('.record-confirm-entry').count(), 1);
  await modalConfirm().click();
  await waitFor(
    () => posts.length === 3 && status.main.status === 'running',
    'reselected batch accepted',
  );
  assert.equal(posts[2].base_catalog_revision, 'r4');
  assert.equal(posts[2].base_decision_revision, 2);
  complete();
  await waitFor(async () => (await entries().count()) === 2, 'last publication completes');
  assert.equal(await entry(383, 3).count(), 1);
  assert.equal(await entry(700, 3).count(), 1);
  assert.deepEqual(errors, []);
  checks.push(
    'Fresh explicit confirmation publishes only revised selection; untouched charts remain and browser has no errors',
  );
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify(
      {
        passed: true,
        mocked: true,
        bundle,
        createdAt: new Date().toISOString(),
        checks,
        posts,
        errors,
      },
      null,
      2,
    ),
  );
  console.log(`Catalog review batch regression passed (${checks.length} checks)`);
} catch (error) {
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {});
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify(
      {
        passed: false,
        mocked: true,
        bundle,
        createdAt: new Date().toISOString(),
        checks,
        posts,
        errors,
        failure: String(error),
        html: await review()
          .innerText()
          .catch(() => 'unavailable'),
      },
      null,
      2,
    ),
  );
  throw error;
} finally {
  postGate?.resolve();
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
