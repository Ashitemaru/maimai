import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.join(project, 'output/venue-summary-audit');
await fs.mkdir(output, { recursive: true });
const mime = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
};
const levels = ['12+', '13', '13+', '14', '14+/15'];
const row = (name, city, counts, pending_count = 0) => ({
  name,
  city,
  counts: Object.fromEntries(levels.map((level, index) => [level, counts[index]])),
  total: counts.reduce((total, value) => total + value, 0),
  pending_count,
});
let items = [
  row('【北京】上地', '北京', [2, 5, 3, 1, 0], 9),
  row('【北京】海淀测试很长的机厅名称城市购物中心店', '北京', [0, 0, 0, 0, 0], 3),
  row('星光游乐城', '上海', [1, 2, 0, 0, 1]),
  row('【上海】HOP', '上海', [0, 3, 0, 1, 0]),
];
function summary() {
  return {
    levels: levels.map((value) => ({ value, label: value })),
    items,
    totals: Object.fromEntries(
      levels.map((level) => [level, items.reduce((n, item) => n + item.counts[level], 0)]),
    ),
    total: items.reduce((n, item) => n + item.total, 0),
    pending_count: items.reduce((n, item) => n + item.pending_count, 0),
  };
}
const requests = [];
const checks = [];
const errors = [];
const failures = [];
let failNextSummary = false;
const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost');
  const pathname = decodeURIComponent(url.pathname);
  requests.push({ path: pathname, method: request.method });
  try {
    assert.equal(request.method, 'GET', 'This read-only fixture must never write');
    if (pathname.startsWith('/api/')) {
      let data;
      if (pathname === '/api/auth/session') data = { authenticated: true, auth_required: false };
      else if (pathname === '/api/settings/preferences')
        data = { default_constant_min: 13, default_constant_max: 13.9 };
      else if (pathname === '/api/settings/divingfish')
        data = { configured: true, source: 'local', sync: null };
      else if (pathname === '/api/venues') data = { items: [] };
      else if (pathname === '/api/venues/map')
        data = { items: items.map((item) => ({ ...item, location: null, jackets: [] })) };
      else if (pathname === '/api/ap-records') data = { items: [], count: 0 };
      else if (pathname === '/api/chart-filters')
        data = { constants: [1, 15], versions: [], cn_versions: [] };
      else if (pathname === '/api/catalog/update')
        data = {
          revision: 'venue-fixture',
          snapshot_at: null,
          main: { status: 'succeeded', warnings: [], diff: {} },
          remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0 },
          can_rollback: false,
        };
      else if (pathname === '/api/venues/summary') {
        if (failNextSummary) {
          failNextSummary = false;
          failures.push(pathname);
          response.writeHead(503, { 'Content-Type': 'application/json' });
          response.end(JSON.stringify({ detail: '机厅暂时无法加载' }));
          return;
        }
        data = summary();
      } else throw new Error(`Unexpected API request: ${pathname}`);
      response.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      response.end(JSON.stringify(data));
      return;
    }
    const file = path.resolve(bundle, pathname === '/' ? 'index.html' : pathname.slice(1));
    assert(file.startsWith(bundle + path.sep));
    response.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
    response.end(await fs.readFile(file));
  } catch (error) {
    if (pathname.startsWith('/api/')) errors.push(error.message);
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launchBrowser();
const countSummary = () => requests.filter((item) => item.path === '/api/venues/summary').length;
async function cellTexts(locator) {
  return locator.locator('th,td').allTextContents();
}
async function waitCount(page, text) {
  await page.waitForFunction(
    (expected) => document.querySelector('.venue-summary-total')?.textContent === expected,
    text,
  );
}
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route('https://tile.openstreetmap.org/**', (route) =>
    route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" />',
    }),
  );
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(origin + '/#venues', { waitUntil: 'networkidle' });
  assert.equal(countSummary(), 0, 'The default map must not prefetch the summary');
  assert.equal(await page.locator('.venue-atlas-card').count(), 4);
  await page.getByRole('button', { name: '汇总表', exact: true }).click();
  await waitCount(page, '4 家机厅 · 19 AP');
  assert.equal(countSummary(), 1, 'Initial metadata loading must not repeat the venue summary');
  assert.deepEqual(await page.locator('.venue-summary-table thead th').allTextContents(), [
    '机厅',
    '12+',
    '13',
    '13+',
    '14',
    '14+/15',
    '合计',
  ]);
  assert.equal(await page.locator('.venue-summary-entry').count(), 4);
  assert.deepEqual(await cellTexts(page.locator('.venue-summary-city').nth(0)), [
    '北京小计',
    '2',
    '5',
    '3',
    '1',
    '0',
    '11',
  ]);
  assert.deepEqual(await cellTexts(page.locator('.venue-summary-city').nth(1)), [
    '上海小计',
    '1',
    '5',
    '0',
    '1',
    '1',
    '8',
  ]);
  assert.deepEqual(await cellTexts(page.locator('.venue-summary-table tfoot tr')), [
    '合计',
    '3',
    '10',
    '3',
    '2',
    '1',
    '19',
  ]);
  checks.push(
    'One initial request renders venue rows, city subtotals and every level total; pending-only venues remain zero AP',
  );
  const search = page.getByRole('searchbox', { name: '搜索机厅或城市' });
  await search.fill(' 上海 ');
  await waitCount(page, '2 家机厅 · 8 AP');
  assert.equal(await page.locator('.venue-summary-city').count(), 1);
  assert.deepEqual(await cellTexts(page.locator('.venue-summary-table tfoot tr')), [
    '合计',
    '1',
    '5',
    '0',
    '1',
    '1',
    '8',
  ]);
  assert.equal(countSummary(), 1, 'Searching the loaded table must not refetch it');
  checks.push(
    'City search uses the city field, trims whitespace and recalculates displayed subtotals and totals without a request',
  );
  await search.fill('hop');
  await waitCount(page, '1 家机厅 · 4 AP');
  assert.equal(await page.locator('.venue-summary-entry th').textContent(), 'HOP');
  await search.fill('不存在的机厅');
  await waitCount(page, '0 家机厅 · 0 AP');
  assert.equal(await page.locator('.venue-summary-empty').textContent(), '没有匹配的机厅');
  assert.deepEqual(await cellTexts(page.locator('.venue-summary-table tfoot tr')), [
    '合计',
    '0',
    '0',
    '0',
    '0',
    '0',
    '0',
  ]);
  checks.push('Case-insensitive venue search and empty results preserve a correct zero total');
  await search.fill('');
  await waitCount(page, '4 家机厅 · 19 AP');
  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
  items = [...items, row('【上海】同步新增机厅', '上海', [1, 0, 0, 0, 0])];
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await waitCount(page, '5 家机厅 · 20 AP');
  assert.equal(countSummary(), 2, 'A personal revision must refresh the summary once');
  checks.push('App resume refreshes cross-device changes through the existing personal revision');
  await context.close();

  failNextSummary = true;
  const mobile = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 3,
  });
  await mobile.route('https://tile.openstreetmap.org/**', (route) =>
    route.fulfill({
      contentType: 'image/svg+xml',
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" />',
    }),
  );
  const phone = await mobile.newPage();
  phone.on('pageerror', (error) => errors.push(error.message));
  await phone.goto(origin + '/#venues', { waitUntil: 'networkidle' });
  await phone.getByRole('button', { name: '汇总表', exact: true }).click();
  await phone.getByRole('alert').waitFor();
  assert.equal(await phone.getByRole('alert').textContent(), '机厅暂时无法加载重试');
  assert.equal(
    await phone.locator('.venue-summary-table').count(),
    0,
    'A failed initial load must not display invented zero totals',
  );
  await phone.getByRole('button', { name: '重试', exact: true }).click();
  await waitCount(phone, '5 家机厅 · 20 AP');
  assert.equal(await phone.getByRole('alert').count(), 0);
  assert.equal(countSummary(), 4);
  checks.push('A failed request shows an actionable retry, which recovers without a full reload');
  for (const width of [390, 320]) {
    await phone.setViewportSize({ width, height: 844 });
    const before = await phone.locator('.venue-summary-scroll').evaluate((element) => {
      element.scrollLeft = 0;
      const first = element.querySelector('.venue-summary-entry th');
      return {
        left: first.getBoundingClientRect().left,
        viewport: document.documentElement.clientWidth,
        document: document.documentElement.scrollWidth,
        scroll: element.scrollWidth,
        client: element.clientWidth,
        font: getComputedStyle(first).fontSize,
        position: getComputedStyle(first).position,
      };
    });
    assert(
      before.document <= before.viewport + 1,
      `${width}px: page must not overflow horizontally`,
    );
    assert(before.scroll > before.client, `${width}px: overflow is confined to table scroller`);
    assert.equal(before.font, '14px');
    assert.equal(before.position, 'sticky');
    await phone.locator('.venue-summary-scroll').evaluate((element) => {
      element.scrollLeft = element.scrollWidth;
    });
    const after = await phone.locator('.venue-summary-entry th').first().boundingBox();
    assert(
      Math.abs(after.x - before.left) < 1,
      `${width}px: first column stays fixed while numbers scroll`,
    );
  }
  checks.push(
    '390px and 320px mobile layouts scroll only the table, with a fixed first column and readable 14px body',
  );
  await phone.setViewportSize({ width: 390, height: 844 });
  await phone.locator('.venue-summary-scroll').evaluate((element) => {
    element.scrollLeft = 0;
  });
  await phone.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
  await mobile.close();
  assert.deepEqual(errors, []);
  assert.equal(failures.length, 1);
  assert(requests.every((request) => request.method === 'GET'));
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify({ checks, requests, failures, errors }, null, 2),
  );
  console.log(`Venue summary: ${checks.length} checks passed.`);
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
