import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.resolve(
  process.env.PREFERENCES_AUDIT_OUTPUT || path.join(project, 'output/preferences-audit'),
);
await fs.mkdir(output, { recursive: true });
const mime = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
};
const requests = [],
  checks = [],
  measurements = [],
  errors = [],
  blockedWrites = [],
  simulatedPreferenceWrites = [],
  injectedFailures = [];
const dataPaths = new Set([
  '/api/charts',
  '/api/summary',
  '/api/completion-table',
  '/api/completion-matrix',
]);
const prematureReads = [];
const server = http.createServer(async (req, res) => {
  try {
    // No API or write reaches a real backend: the browser route below handles the preference fixture in memory.
    if (req.method !== 'GET' || req.url.startsWith('/api/')) {
      res.writeHead(405);
      res.end();
      return;
    }
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    if (pathname === '/fixture-jacket.svg') {
      res.setHeader('Content-Type', 'image/svg+xml');
      res.end(
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#6677bc"/></svg>',
      );
      return;
    }
    const file = path.resolve(bundle, pathname === '/' ? 'index.html' : pathname.slice(1));
    if (!file.startsWith(bundle + path.sep)) {
      res.writeHead(403);
      res.end();
      return;
    }
    res.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
    res.end(await fs.readFile(file));
  } catch {
    res.writeHead(404);
    res.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
const browser = await launchBrowser();
const charts = [1, 12.5, 13, 13.5, 13.9, 14.5, 15].map((constant, index) => ({
  id: 8000 + index,
  song_id: 8000 + index,
  chart_key: `${8000 + index}:3`,
  title: `Preference Fixture ${constant.toFixed(1)}`,
  display_title: '',
  artist: 'Isolated preference fixture',
  type: 'standard',
  difficulty: 3,
  difficulty_index: 3,
  difficulty_name: 'Master',
  level: String(Math.floor(constant)),
  constant,
  jacket_url: '/fixture-jacket.svg',
  cn_version: '25500',
  jp_release_version: 'PRiSM',
  jp_release_version_candidate: null,
  jp_release_provider: 'arcade_songs',
  jp_release_date: null,
  release_status: 'verified',
  requires_venue: true,
  in_original_table: true,
  legacy_complete: false,
  legacy_venue: '',
  manual_revision: 0,
  manual_ap: null,
  manual_venue: '',
  venue: '',
  note: '',
  manual_date: null,
  achievement: index % 2 ? null : 100.9876,
  provider_achievement: index % 2 ? null : 100.9876,
  provider_fc: index % 2 ? null : 'ap',
  provider_synced_at: null,
  is_ap: index % 2 === 0,
  is_complete: index % 2 === 0,
  status: index % 2 ? 'incomplete' : 'ap',
  conflict: false,
  warnings: [],
}));
const matrixVersions = [{ value: '25500', label: '彩', title: '舞萌DX 2026' }];
function filtered(params) {
  return charts.filter(
    (chart) =>
      (!params.has('constant_min') || chart.constant >= Number(params.get('constant_min'))) &&
      (!params.has('constant_max') || chart.constant <= Number(params.get('constant_max'))),
  );
}
function counts(items) {
  const complete = items.filter((row) => row.is_complete).length;
  return {
    total: items.length,
    complete,
    remaining: items.length - complete,
    ap: complete,
    conflicts: 0,
    legacy_complete: 0,
  };
}
function summary(items) {
  return { ...counts(items), levels: [], constants: [], sync: null };
}
function matrix(items) {
  return {
    versions: matrixVersions,
    rows: items.map((chart) => ({
      constant: chart.constant,
      cells: { 25500: counts([chart]) },
      total: counts([chart]),
    })),
    totals: { 25500: counts(items) },
    summary: counts(items),
  };
}
function gate() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
const catalog = {
  revision: 'preferences-fixture',
  decision_revision: 0,
  applied_decision_revision: 0,
  decisions_pending: false,
  snapshot_at: '2026-10-11T01:00:00Z',
  main: { status: 'succeeded', stage: '', message: 'Fixture only', diff: {}, warnings: [] },
  remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0, message: '' },
  can_rollback: false,
};
async function mock(context, name) {
  const control = {
    stored: { default_constant_min: 13, default_constant_max: 13.9 },
    ready: false,
    loadGate: null,
    saveGate: null,
    failNextGet: false,
    failNextSave: false,
  };
  await context.route('**/api/**', async (route) => {
    if (new URL(route.request().url()).pathname === '/api/auth/session') {
      await route.fulfill({ json: { authenticated: true, auth_required: false } });
      return;
    }
    const request = route.request(),
      url = new URL(request.url()),
      pathname = url.pathname,
      params = url.searchParams;
    requests.push({
      context: name,
      method: request.method(),
      path: pathname,
      params: Object.fromEntries(params),
    });
    if (pathname === '/api/settings/preferences' && request.method() === 'GET') {
      control.ready = false;
      if (control.loadGate) await control.loadGate.promise;
      if (control.failNextGet) {
        control.failNextGet = false;
        injectedFailures.push({ context: name, method: 'GET' });
        await route.fulfill({ status: 503, json: { detail: '隔离测试：偏好读取失败' } });
        return;
      }
      control.ready = true;
      await route.fulfill({ json: structuredClone(control.stored) });
      return;
    }
    if (pathname === '/api/settings/preferences' && request.method() === 'PUT') {
      const body = request.postDataJSON();
      assert.deepEqual(Object.keys(body).sort(), ['default_constant_max', 'default_constant_min']);
      assert(
        Number.isFinite(body.default_constant_min) && Number.isFinite(body.default_constant_max),
      );
      assert(body.default_constant_min <= body.default_constant_max);
      simulatedPreferenceWrites.push({ context: name, value: body });
      if (control.saveGate) await control.saveGate.promise;
      if (control.failNextSave) {
        control.failNextSave = false;
        injectedFailures.push({ context: name, method: 'PUT' });
        await route.fulfill({ status: 503, json: { detail: '隔离测试：偏好保存失败' } });
        return;
      }
      control.stored = structuredClone(body);
      await route.fulfill({ json: control.stored });
      return;
    }
    if (request.method() !== 'GET') {
      blockedWrites.push({ path: pathname, method: request.method() });
      await route.fulfill({ status: 405, json: { detail: 'Unexpected mutation blocked' } });
      return;
    }
    if (dataPaths.has(pathname) && !control.ready)
      prematureReads.push({ context: name, path: pathname, params: Object.fromEntries(params) });
    let value;
    if (pathname === '/api/charts') {
      const items = filtered(params),
        offset = Number(params.get('offset') || 0),
        limit = Number(params.get('limit') || 12);
      value = { items: items.slice(offset, offset + limit), total: items.length, offset, limit };
    } else if (pathname === '/api/summary') value = summary(filtered(params));
    else if (pathname === '/api/completion-matrix') value = matrix(filtered(params));
    else if (pathname === '/api/completion-table') {
      const items = filtered(params);
      value = {
        items,
        total: items.length,
        summary: summary(items),
        generated_at: '2026-10-11T04:30:00+08:00',
      };
    } else if (pathname === '/api/chart-filters')
      value = {
        constants: charts.map((chart) => chart.constant),
        versions: [{ value: 'PRiSM', label: 'PRiSM' }],
        cn_versions: [{ value: '25500', label: 'DX2026' }],
        cn_matrix_versions: matrixVersions,
      };
    else if (pathname === '/api/settings/divingfish')
      value = { configured: false, source: null, sync: null };
    else if (pathname === '/api/venues') value = { items: [] };
    else if (pathname === '/api/catalog/update') value = catalog;
    else if (pathname === '/api/catalog/records/review')
      value = {
        schema_version: 2,
        revision: catalog.revision,
        decision_revision: 0,
        decisions_pending: false,
        counts: { total: charts.length, admitted: charts.length, pending: 0 },
        records: [],
      };
    else if (pathname === '/api/catalog/records/decisions')
      value = {
        revision: catalog.revision,
        decision_revision: 0,
        decisions_pending: false,
        records: [],
      };
    else if (pathname === '/api/catalog/update/report')
      value = { diff: {}, issues: [], sources: {} };
    else errors.push(`Unexpected API: ${pathname}`);
    await route.fulfill(
      value === undefined ? { status: 404, json: { detail: 'Unmocked API' } } : { json: value },
    );
  });
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      errors.push(`External request blocked: ${url.origin}${url.pathname}`);
      await route.abort();
    } else await route.fallback();
  });
  return control;
}
async function waitFor(predicate, label) {
  const until = Date.now() + 10000;
  while (Date.now() < until) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 30));
  }
  throw new Error(`Timed out: ${label}`);
}
function latest(name, route) {
  return requests.findLast(
    (request) => request.context === name && request.path === '/api/' + route,
  );
}
async function range(page, name, route, minimum, maximum) {
  await waitFor(
    async () => {
      const params = latest(name, route)?.params;
      const matches =
        minimum === null
          ? params && !('constant_min' in params) && !('constant_max' in params)
          : params &&
            Number(params.constant_min) === minimum &&
            Number(params.constant_max) === maximum;
      return (
        matches &&
        (await page
          .getByRole('slider', { name: '最低定数' })
          .getAttribute('aria-valuenow')
          .catch(() => '')) === String(minimum ?? 1) &&
        (await page
          .getByRole('slider', { name: '最高定数' })
          .getAttribute('aria-valuenow')
          .catch(() => '')) === String(maximum ?? 15)
      );
    },
    `${route} and sliders settle at ${minimum ?? 'all'}–${maximum ?? 'all'}`,
  );
  const count = charts.filter(
    (chart) => minimum === null || (chart.constant >= minimum && chart.constant <= maximum),
  ).length;
  if (route === 'charts')
    await waitFor(
      async () =>
        (await page.locator('.chart-table tbody tr').count()) === count &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
      'AP filtered rows settle',
    );
  else
    await waitFor(
      async () => (await page.locator('.matrix-table tbody tr').count()) === count,
      'matrix filtered rows settle',
    );
}
async function settingsAudit(page, name) {
  const geometry = await page.locator('.preferences-panel').evaluate((panel) => {
    const rect = (element) => {
      const value = element.getBoundingClientRect();
      return {
        x: value.x,
        y: value.y,
        right: value.right,
        bottom: value.bottom,
        width: value.width,
        height: value.height,
      };
    };
    const label = (element) => element.labels?.[0]?.textContent || element.textContent.trim();
    const controls = [...panel.querySelectorAll('input,button')].map((element) => ({
      label: label(element),
      rect: rect(element),
      font: parseFloat(getComputedStyle(element).fontSize),
    }));
    const nextPanel = document.querySelector('.catalog-update');
    return {
      viewport: innerWidth,
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      panel: rect(panel),
      controls,
      inline: [...panel.querySelectorAll('[style]')].length,
      nextPanelTop: nextPanel ? rect(nextPanel).y : null,
    };
  });
  measurements.push({ name, ...geometry });
  assert(
    geometry.body <= geometry.viewport && geometry.document <= geometry.viewport,
    `${name}: no horizontal page overflow`,
  );
  assert.equal(geometry.inline, 0, `${name}: no inline styles`);
  for (const control of geometry.controls) {
    assert(
      control.rect.x >= 0 && control.rect.right <= geometry.viewport,
      `${name}: ${control.label} stays visible`,
    );
    assert(
      control.rect.height >= 36 && control.font >= 14,
      `${name}: ${control.label} stays readable and operable`,
    );
  }
  if (geometry.nextPanelTop !== null)
    assert(
      geometry.panel.bottom <= geometry.nextPanelTop,
      `${name}: preferences appears before catalog settings`,
    );
  await page.screenshot({ path: path.join(output, `${name}-preferences.png`), fullPage: true });
  checks.push(`${name}: preference card is readable, controls fit, no horizontal overflow`);
}
let activePage;
try {
  for (const [name, viewport] of [
    ['desktop', { width: 1440, height: 1000 }],
    ['mobile', { width: 390, height: 844 }],
  ]) {
    const context = await browser.newContext({ viewport });
    const control = await mock(context, name);
    const page = await context.newPage();
    activePage = page;
    page.on('pageerror', (error) => errors.push(`${name}: ${error.message}`));
    page.on('console', (message) => {
      if (
        message.type() === 'error' &&
        !message.text().includes('the server responded with a status of 503')
      )
        errors.push(`${name}: ${message.text()}`);
    });
    for (const route of ['ap', 'statistics']) {
      control.ready = false;
      control.loadGate = gate();
      const marker = requests.length;
      await page.goto(origin + '/?preferences-start=' + route + '#' + route, {
        waitUntil: 'domcontentloaded',
      });
      await waitFor(
        () =>
          requests
            .slice(marker)
            .some(
              (request) => request.context === name && request.path === '/api/settings/preferences',
            ),
        `${route} starts loading preferences`,
      );
      await page.waitForTimeout(250);
      assert.equal(
        requests
          .slice(marker)
          .filter((request) => request.context === name && dataPaths.has(request.path)).length,
        0,
        `${route}: no unbounded read while preference GET is pending`,
      );
      control.loadGate.resolve();
      control.loadGate = null;
      await range(page, name, route === 'ap' ? 'charts' : 'completion-matrix', 13, 13.9);
      const initialReads = requests
        .slice(marker)
        .filter((request) => request.context === name && dataPaths.has(request.path));
      assert(initialReads.length > 0);
      for (const request of initialReads) {
        assert.equal(Number(request.params.constant_min), 13);
        assert.equal(Number(request.params.constant_max), 13.9);
      }
    }
    checks.push(
      `${name}: direct AP and statistics loads wait for saved preferences; first data requests use 13.0–13.9 without a full-range flash`,
    );
    await page.getByRole('slider', { name: '最高定数' }).focus();
    await page.keyboard.press('ArrowLeft');
    await range(page, name, 'completion-matrix', 13, 13.8);
    await page.getByRole('button', { name: '清除', exact: true }).click();
    await range(page, name, 'completion-matrix', null, null);
    await page.locator('a.nav-item[href="#ap"]').click();
    await range(page, name, 'charts', 13, 13.9);
    await page.getByRole('slider', { name: '最低定数' }).focus();
    await page.keyboard.press('ArrowRight');
    await range(page, name, 'charts', 13.1, 13.9);
    await page.getByRole('button', { name: '清除筛选', exact: true }).click();
    await range(page, name, 'charts', null, null);
    assert.equal(simulatedPreferenceWrites.filter((write) => write.context === name).length, 0);
    assert.deepEqual(control.stored, { default_constant_min: 13, default_constant_max: 13.9 });
    checks.push(
      `${name}: manual sliders are temporary; clear restores full range without saving preferences`,
    );
    await page.locator('a.nav-item[href="#settings"]').click();
    const panel = page.locator('.preferences-panel');
    await panel.waitFor();
    const minimum = panel.getByRole('spinbutton', { name: '最低定数', exact: true });
    const maximum = panel.getByRole('spinbutton', { name: '最高定数', exact: true });
    const save = panel.locator('button[type="submit"]');
    assert.equal(Number(await minimum.inputValue()), 13);
    assert.equal(Number(await maximum.inputValue()), 13.9);
    await settingsAudit(page, name);
    await minimum.fill('12.5');
    await maximum.fill('14.5');
    control.saveGate = gate();
    await save.click();
    await waitFor(
      () => simulatedPreferenceWrites.some((write) => write.context === name),
      'isolated preference save starts',
    );
    assert.equal(
      await save.isDisabled(),
      true,
      'Duplicate preference save is disabled while pending',
    );
    control.saveGate.resolve();
    control.saveGate = null;
    await waitFor(
      () =>
        control.stored.default_constant_min === 12.5 &&
        control.stored.default_constant_max === 14.5,
      'fixture persists preference',
    );
    await panel.getByRole('status').filter({ hasText: '已保存' }).waitFor();
    for (const route of ['ap', 'statistics']) {
      await page.locator(`a.nav-item[href="#${route}"]`).click();
      await range(page, name, route === 'ap' ? 'charts' : 'completion-matrix', 12.5, 14.5);
    }
    const marker = requests.length;
    control.ready = false;
    await page.reload({ waitUntil: 'networkidle' });
    await range(page, name, 'completion-matrix', 12.5, 14.5);
    assert(
      requests
        .slice(marker)
        .some(
          (request) => request.path === '/api/settings/preferences' && request.method === 'GET',
        ),
    );
    for (const request of requests
      .slice(marker)
      .filter((request) => request.context === name && dataPaths.has(request.path))) {
      assert.equal(Number(request.params.constant_min), 12.5);
      assert.equal(Number(request.params.constant_max), 14.5);
    }
    await page.getByRole('button', { name: '清除', exact: true }).click();
    await range(page, name, 'completion-matrix', null, null);
    control.ready = false;
    await page.reload({ waitUntil: 'networkidle' });
    await range(page, name, 'completion-matrix', 12.5, 14.5);
    await page.locator('a.nav-item[href="#ap"]').click();
    await range(page, name, 'charts', 12.5, 14.5);
    control.ready = false;
    await page.reload({ waitUntil: 'networkidle' });
    await range(page, name, 'charts', 12.5, 14.5);
    await page.locator('a.nav-item[href="#settings"]').click();
    await panel.waitFor();
    assert.equal(Number(await minimum.inputValue()), 12.5);
    assert.equal(Number(await maximum.inputValue()), 14.5);
    await page.screenshot({
      path: path.join(output, `${name}-preferences-saved.png`),
      fullPage: true,
    });
    checks.push(
      `${name}: saving updates both page defaults; reload reads the saved values, including after clearing the current range`,
    );
    await minimum.fill('15');
    await maximum.fill('13');
    const writeCount = simulatedPreferenceWrites.length;
    await save.click();
    await waitFor(
      async () => (await panel.locator('.form-message').count()) > 0,
      'invalid preference range is rejected',
    );
    assert.equal(simulatedPreferenceWrites.length, writeCount, 'Invalid range never reaches PUT');
    assert.deepEqual(control.stored, { default_constant_min: 12.5, default_constant_max: 14.5 });
    checks.push(`${name}: reversed default range is rejected without a preference mutation`);
    if (name === 'desktop') {
      await minimum.fill('12.6');
      await maximum.fill('14.6');
      control.failNextSave = true;
      await save.click();
      await panel.getByRole('alert').filter({ hasText: '隔离测试：偏好保存失败' }).waitFor();
      assert.deepEqual(control.stored, { default_constant_min: 12.5, default_constant_max: 14.5 });
      control.ready = false;
      await page.reload({ waitUntil: 'networkidle' });
      await panel.waitFor();
      assert.equal(Number(await minimum.inputValue()), 12.5);
      assert.equal(Number(await maximum.inputValue()), 14.5);
      checks.push(
        'Failed preference save reports the error and keeps the previously saved values after reload',
      );
      control.failNextGet = true;
      control.ready = false;
      const marker = requests.length;
      await page.goto(origin + '/?preferences-failure#statistics', { waitUntil: 'networkidle' });
      const error = page.locator('.preferences-load-error');
      await error.waitFor();
      assert.equal(
        requests
          .slice(marker)
          .filter((request) => request.context === name && dataPaths.has(request.path)).length,
        0,
        'A failed preference GET cannot fall back to an unbounded chart request',
      );
      await error.getByRole('button', { name: '重试', exact: true }).click();
      await range(page, name, 'completion-matrix', 12.5, 14.5);
      checks.push(
        'Preference GET failure blocks data reads; explicit retry recovers with the saved default range',
      );
    }
    await context.close();
    activePage = null;
  }
  assert.deepEqual(prematureReads, [], 'No chart/stat reads before preference GET succeeds');
  assert.deepEqual(blockedWrites, [], 'Only in-memory preference fixture may be written');
  assert.deepEqual(errors, [], 'No browser or unexpected API errors');
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify(
      {
        passed: true,
        isolated: true,
        mockPersistence: true,
        realDatabaseWrites: 0,
        createdAt: new Date().toISOString(),
        bundle,
        checks,
        measurements,
        requests,
        simulatedPreferenceWrites,
        blockedWrites,
        prematureReads,
        errors,
        injectedFailures,
      },
      null,
      2,
    ),
  );
  await fs.rm(path.join(output, 'failure.png'), { force: true });
  console.log(
    `Preferences UI passed (${checks.length} checks, desktop + 390px mobile, isolated mock persistence, 0 real database writes)`,
  );
} catch (error) {
  await activePage
    ?.screenshot({ path: path.join(output, 'failure.png'), fullPage: true })
    .catch(() => {});
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify(
      {
        passed: false,
        isolated: true,
        mockPersistence: true,
        realDatabaseWrites: 0,
        createdAt: new Date().toISOString(),
        bundle,
        checks,
        measurements,
        requests,
        simulatedPreferenceWrites,
        blockedWrites,
        prematureReads,
        errors,
        injectedFailures,
        failure: String(error),
      },
      null,
      2,
    ),
  );
  throw error;
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
