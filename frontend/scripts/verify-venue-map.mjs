import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const root = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(root, 'frontend/dist'));
const output = path.join(root, 'output/venue-map-audit');
await fs.mkdir(output, { recursive: true });
const graphic =
  '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" fill="#e9edf0"/><path d="M0 80H256M85 0V256M0 190H256M180 0V256" stroke="#fff" stroke-width="12"/></svg>';
const chart = {
  chart_key: '1:3',
  id: 1,
  title: 'Fixture AP',
  difficulty: 3,
  difficulty_name: 'MASTER',
  constant: 13.7,
  level: '13+',
  type: 'standard',
  jacket_url: '/jackets/1.png',
  is_ap: true,
  status: 'ap',
};
const location = {
  place_name: '历史游乐城',
  address: '上海测试路1号',
  latitude: 31.22,
  longitude: 121.46,
  coordinate_system: 'WGS84',
  accuracy: 'building',
  closed_status: 'closed',
};
let venues = [
  {
    name: '【上海】测试机厅',
    city: '上海',
    total: 2,
    pending_count: 1,
    location,
    jackets: [chart],
  },
  {
    name: '【北京】测试机厅',
    city: '北京',
    total: 1,
    pending_count: 0,
    location: { ...location, latitude: 39.9, longitude: 116.4 },
    jackets: [chart],
  },
  { name: '新机厅', city: '未分类', total: 0, pending_count: 1, location: null, jackets: [] },
];
const writes = [],
  requests = [],
  errors = [],
  checks = [];
let failSave = false;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const send = (data, status = 200) => {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
  };
  requests.push({ path: url.pathname, method: req.method });
  try {
    if (url.pathname === '/api/venues/location' && req.method === 'PUT') {
      let body = '';
      for await (const chunk of req) body += chunk;
      const data = JSON.parse(body);
      writes.push(data);
      if (failSave) {
        failSave = false;
        return send({ detail: '保存暂时失败' }, 503);
      }
      const record = venues.find((item) => item.name === data.name);
      const saved = { ...data, coordinate_system: 'WGS84' };
      if (record) record.location = saved;
      else
        venues.push({
          name: data.name,
          city: '测试',
          total: 0,
          pending_count: 0,
          location: saved,
          jackets: [],
        });
      return send({ name: data.name, location: saved });
    }
    assert.equal(req.method, 'GET');
    if (url.pathname === '/api/auth/session')
      return send({ authenticated: true, auth_required: false });
    if (url.pathname === '/api/settings/preferences')
      return send({ default_constant_min: 13, default_constant_max: 13.9 });
    if (url.pathname === '/api/settings/divingfish')
      return send({ configured: true, source: 'local', sync: null });
    if (url.pathname === '/api/ap-records') return send({ items: [], count: 0 });
    if (url.pathname === '/api/venues')
      return send({ items: venues.map((item) => ({ name: item.name, count: item.total })) });
    if (url.pathname === '/api/venues/map') return send({ items: venues });
    if (url.pathname === '/api/venues/charts')
      return send({ items: [chart], total: 1, offset: 0, limit: 48 });
    if (url.pathname === '/api/venues/search') {
      assert.equal(url.searchParams.get('q'), '测试广场');
      return send({
        items: [
          {
            place_name: '搜索结果广场',
            address: '搜索结果路8号',
            latitude: 31.24,
            longitude: 121.49,
          },
        ],
      });
    }
    if (url.pathname === '/api/chart-filters')
      return send({ constants: [1, 15], versions: [], cn_versions: [] });
    if (url.pathname === '/api/catalog/update')
      return send({
        revision: 'test',
        main: { status: 'succeeded', warnings: [], diff: {} },
        remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0 },
        can_rollback: false,
      });
    if (url.pathname.startsWith('/api/'))
      throw new Error('Unexpected API request: ' + url.pathname);
    if (url.pathname.startsWith('/jackets/')) {
      res.writeHead(200, { 'Content-Type': 'image/svg+xml' });
      return res.end(graphic);
    }
    const file = path.resolve(bundle, url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
    assert(file.startsWith(bundle + path.sep));
    res.setHeader(
      'Content-Type',
      { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[path.extname(file)] ||
        'application/octet-stream',
    );
    res.end(await fs.readFile(file));
  } catch (error) {
    errors.push(error.message);
    send({ detail: error.message }, 500);
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launchBrowser();
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route('https://tile.openstreetmap.org/**', (route) =>
    route.fulfill({ contentType: 'image/svg+xml', body: graphic }),
  );
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(origin + '/#venues');
  await page.getByText('3 家机厅 · 3 AP', { exact: true }).waitFor();
  const marker = page.getByRole('button', { name: '【上海】测试机厅，2 AP', exact: true });
  await marker.click();
  await page.getByRole('region', { name: '机厅详情' }).waitFor();
  await page.getByText('Fixture AP', { exact: true }).waitFor();
  assert(await page.getByText('已闭店 · 历史位置').isVisible());
  assert(requests.some((item) => item.path === '/api/venues/charts'));
  checks.push('Mouse click on SVG jacket opens paginated AP album and historical location');
  await page.getByRole('button', { name: '编辑位置', exact: true }).click();
  const editor = page.getByRole('dialog');
  await editor.waitFor();
  assert.equal(await editor.getByLabel('机厅名称', { exact: true }).getAttribute('readonly'), '');
  await editor.getByLabel('地址', { exact: true }).fill('修改后取消');
  await editor.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(writes.length, 0);
  checks.push('Cancel does not write location');
  await page.getByRole('button', { name: '编辑位置', exact: true }).click();
  await editor.getByLabel('搜索地点', { exact: true }).fill('测试广场');
  assert.equal(requests.filter((item) => item.path === '/api/venues/search').length, 0);
  await editor.getByRole('button', { name: '搜索', exact: true }).click();
  await editor.getByRole('button', { name: /搜索结果广场/ }).click();
  failSave = true;
  await editor.getByRole('button', { name: '保存位置', exact: true }).click();
  await editor.getByRole('alert').filter({ hasText: '保存暂时失败' }).waitFor();
  assert.equal(await editor.getByLabel('地址', { exact: true }).inputValue(), '搜索结果路8号');
  await editor.getByRole('button', { name: '保存位置', exact: true }).click();
  await editor.waitFor({ state: 'hidden' });
  assert.equal(writes.at(-1).name, '【上海】测试机厅');
  assert.equal(writes.at(-1).latitude, 31.24);
  assert.equal(writes.at(-1).closed_status, 'closed');
  checks.push('Search is explicit; failed save preserves form; retry persists WGS84 point');
  await page.reload();
  await page
    .getByRole('button', { name: /【上海】测试机厅.*2 AP/ })
    .last()
    .click();
  await page.getByText('搜索结果路8号', { exact: true }).waitFor();
  checks.push('Saved location survives reload');
  await page.getByRole('button', { name: '新增机厅', exact: true }).click();
  await editor.getByLabel('机厅名称', { exact: true }).fill('【测试】新门店');
  await editor.getByLabel('地址', { exact: true }).fill('新址9号');
  await editor.getByRole('button', { name: '保存位置', exact: true }).click();
  await editor.waitFor({ state: 'hidden' });
  assert.equal(writes.at(-1).latitude, null);
  assert.equal(writes.at(-1).longitude, null);
  await page.getByText('4 家机厅 · 3 AP', { exact: true }).waitFor();
  checks.push('Address-only new venue is available without inventing AP records');
  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.getByRole('button', { name: '新增机厅', exact: true }).click();
  const svg = editor.locator('.venue-map-svg');
  const box = await svg.boundingBox();
  await page.mouse.click(box.x + box.width * 0.6, box.y + box.height * 0.5);
  assert(await editor.getByRole('button', { name: '清除点位', exact: true }).isVisible());
  assert(
    await page.evaluate(
      () =>
        document.querySelector('dialog').scrollWidth <=
        document.querySelector('dialog').clientWidth,
    ),
  );
  await page.screenshot({ path: path.join(output, 'mobile-editor.png') });
  await editor.getByRole('button', { name: '取消', exact: true }).click();
  await page.screenshot({ path: path.join(output, 'mobile.png') });
  checks.push('390px layout fits viewport; map click selects a coordinate');
  assert.deepEqual(errors, []);
  await fs.writeFile(path.join(output, 'report.json'), JSON.stringify({ checks, errors }, null, 2));
  console.log(JSON.stringify({ checks: checks.length, errors }));
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
