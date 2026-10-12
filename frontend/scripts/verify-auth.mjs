import assert from 'node:assert/strict';
import { idleScoreSync } from './score-sync-fixture.mjs';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.resolve(
  process.env.AUTH_AUDIT_OUTPUT || path.join(project, 'output/auth-ui-audit'),
);
await fs.mkdir(output, { recursive: true });
const checks = [];
const errors = [];
const privateRequests = [];
const key = 'synthetic-ui-test-key';
let validSessions = new Set();
let nextSession = 0;
let unavailable = false;
let loginDelay = 0;
const data = {
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
  '/api/sync/status': idleScoreSync,
  '/api/settings/divingfish': { configured: false, source: null, sync: null },
  '/api/settings/preferences': { default_constant_min: 13, default_constant_max: 13.9 },
  '/api/chart-filters': { constants: [13, 13.9], versions: [], cn_versions: [] },
  '/api/catalog/update': {
    revision: 'auth-fixture',
    snapshot_at: null,
    main: { status: 'idle', stage: '', message: '', warnings: [] },
    remy: {
      status: 'complete',
      pending_count: 0,
      verified_count: 0,
      updated_count: 0,
      message: '',
    },
    can_rollback: false,
  },
};
const server = http.createServer(async (request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const json = (status, body, headers = {}) => {
    response.writeHead(status, {
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
      ...headers,
    });
    response.end(JSON.stringify(body));
  };
  try {
    const token = request.headers.cookie?.match(/fixture_session=([^;]+)/)?.[1];
    if (pathname === '/api/auth/session') {
      json(
        unavailable ? 503 : 200,
        unavailable
          ? { detail: 'Unavailable' }
          : { authenticated: validSessions.has(token), auth_required: true },
      );
      return;
    }
    if (pathname === '/api/auth/login') {
      let body = '';
      for await (const chunk of request) body += chunk;
      if (loginDelay) await new Promise((resolve) => setTimeout(resolve, loginDelay));
      const supplied = JSON.parse(body).master_key;
      if (supplied !== key) {
        json(supplied === 'limited' ? 429 : 401, { detail: 'Invalid key' });
        return;
      }
      const session = String(++nextSession);
      validSessions.add(session);
      json(
        200,
        { authenticated: true, auth_required: true },
        {
          'Set-Cookie': `fixture_session=${session}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000`,
        },
      );
      return;
    }
    if (pathname === '/api/auth/logout') {
      validSessions.delete(token);
      json(
        200,
        { authenticated: false, auth_required: true },
        { 'Set-Cookie': 'fixture_session=; HttpOnly; Path=/; Max-Age=0' },
      );
      return;
    }
    if (pathname.startsWith('/api/')) {
      privateRequests.push(pathname);
      json(
        validSessions.has(token) ? 200 : 401,
        validSessions.has(token) ? data[pathname] || {} : { detail: 'Expired' },
      );
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
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.route('https://tile.openstreetmap.org/**', (route) =>
    route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" />',
    }),
  );
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(origin + '/#venues');
  const input = page.getByLabel('Master Key', { exact: true });
  await input.waitFor();
  assert.deepEqual(privateRequests, []);
  assert.equal(await page.locator('.shell').count(), 0);
  assert.equal(await page.getByRole('button', { name: '进入手账' }).isDisabled(), true);
  checks.push(
    'Unauthenticated startup renders only the login page and makes no personal-data requests',
  );
  await page.screenshot({ path: path.join(output, 'login-desktop.png') });
  await input.fill('wrong-key');
  await page.getByRole('button', { name: '显示密钥' }).click();
  assert.equal(await input.getAttribute('type'), 'text');
  await page.getByRole('button', { name: '隐藏密钥' }).click();
  assert.equal(await input.getAttribute('type'), 'password');
  await input.press('Enter');
  await page.getByRole('alert').filter({ hasText: 'Master Key 不正确' }).waitFor();
  assert.deepEqual(privateRequests, []);
  checks.push('Show/hide and Enter submission work; incorrect keys stay on the login page');
  await input.fill('limited');
  await input.press('Enter');
  await page.getByRole('alert').filter({ hasText: '尝试次数较多' }).waitFor();
  checks.push('Rate limits have a readable retry message');
  await input.fill(key);
  loginDelay = 350;
  await input.press('Enter');
  await page.getByRole('button', { name: '正在进入…' }).waitFor();
  assert.equal(await input.isDisabled(), true);
  await page.locator('.shell').waitFor();
  loginDelay = 0;
  assert.equal(await page.locator('.auth-page').count(), 0);
  assert(privateRequests.length > 0);
  await page.getByRole('button', { name: '汇总表', exact: true }).click();
  assert.equal(await page.locator('.venue-summary-total').innerText(), '0 家机厅 · 0 AP');
  assert.equal(
    await page
      .evaluate(() =>
        JSON.stringify({
          local: { ...localStorage },
          session: { ...sessionStorage },
          cookie: document.cookie,
        }),
      )
      .then((value) => value.includes(key)),
    false,
  );
  checks.push(
    'Successful login opens the app; the Master Key is absent from browser storage and cookies',
  );
  await page.reload();
  await page.locator('.shell').waitFor();
  assert.equal(await page.locator('.auth-page').count(), 0);
  checks.push('Cookie-backed login survives a full page reload');
  await page.getByRole('button', { name: '退出', exact: true }).click();
  await input.waitFor();
  assert.equal(await input.inputValue(), '');
  assert.equal(await page.locator('.shell').count(), 0);
  await page.reload();
  await input.waitFor();
  checks.push('Logout clears the session and unmounts personal state; reload stays signed out');
  await input.fill(key);
  await input.press('Enter');
  await page.locator('.shell').waitFor();
  await page.waitForLoadState('networkidle');
  validSessions = new Set();
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await input.waitFor();
  await page.getByRole('alert').filter({ hasText: '登录已过期' }).waitFor();
  assert.equal(await page.locator('.shell').count(), 0);
  checks.push('A personal API 401 immediately returns to login and removes the app state');
  unavailable = true;
  const beforeUnavailable = privateRequests.length;
  await page.reload();
  await page.getByRole('button', { name: '重新连接' }).waitFor();
  assert.equal(privateRequests.length, beforeUnavailable);
  unavailable = false;
  await page.getByRole('button', { name: '重新连接' }).click();
  await input.waitFor();
  checks.push('Session service failures stay closed and recover through an explicit retry');
  await context.close();
  for (const width of [320, 390]) {
    const mobile = await browser.newContext({
      viewport: { width, height: 844 },
      isMobile: true,
      hasTouch: true,
      deviceScaleFactor: 2,
    });
    await mobile.route('https://tile.openstreetmap.org/**', (route) =>
      route.fulfill({
        contentType: 'image/svg+xml',
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" />',
      }),
    );
    const mobilePage = await mobile.newPage();
    mobilePage.on('pageerror', (error) => errors.push(error.message));
    await mobilePage.goto(origin + '/#venues');
    await mobilePage.getByLabel('Master Key', { exact: true }).waitFor();
    const metrics = await mobilePage.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
      fontSize: parseFloat(getComputedStyle(document.getElementById('master-key')).fontSize),
      buttonHeight: document.querySelector('.auth-visibility').getBoundingClientRect().height,
    }));
    assert(metrics.documentWidth <= metrics.viewportWidth);
    assert(metrics.fontSize >= 16);
    assert(metrics.buttonHeight >= 44);
    await mobilePage.screenshot({ path: path.join(output, `login-${width}.png`) });
    await mobilePage.getByLabel('Master Key', { exact: true }).fill(key);
    await mobilePage.getByRole('button', { name: '进入手账' }).click();
    const logout = mobilePage.getByRole('button', { name: '退出', exact: true });
    await logout.waitFor();
    const bounds = await logout.boundingBox();
    assert(bounds.x >= 0 && bounds.x + bounds.width <= width);
    await logout.click();
    await mobilePage.getByLabel('Master Key', { exact: true }).waitFor();
    checks.push(
      `${width}px mobile layout has no horizontal overflow, 16px input, 44px visibility control, and reachable logout`,
    );
    await mobile.close();
  }
  assert.deepEqual(errors, []);
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ checks, errors }, null, 2));
  console.log(`Authentication UI: ${checks.length} checks passed (isolated fixtures only).`);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
