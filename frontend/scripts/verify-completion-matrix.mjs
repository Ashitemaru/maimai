import assert from 'node:assert/strict';
import { idleScoreSync } from './score-sync-fixture.mjs';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.resolve(
  process.env.MATRIX_AUDIT_OUTPUT || path.join(project, 'output/completion-matrix-audit'),
);
await fs.mkdir(output, { recursive: true });
const checks = [],
  measurements = [],
  requests = [],
  mutations = [],
  errors = [],
  injectedFailures = [];
const navOnly = process.env.MATRIX_AUDIT_NAV_ONLY === '1';
const mime = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
};
const server = http.createServer(async (req, res) => {
  try {
    if (req.method !== 'GET') {
      res.writeHead(405);
      res.end();
      return;
    }
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    if (pathname === '/fixture-jacket.svg') {
      res.setHeader('Content-Type', 'image/svg+xml');
      res.end(
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#6677bc"/><path d="M0 80 80 0v80Z" fill="#edb2d2"/></svg>',
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
const versionRows = [
  ['10000', '初', 'maimai'],
  ['11000', '真', 'maimai PLUS'],
  ['12000', '超', 'GreeN'],
  ['13000', '檄', 'GreeN PLUS'],
  ['14000', '橙', 'ORANGE'],
  ['15000', '晓', 'ORANGE PLUS'],
  ['16000', '桃', 'PiNK'],
  ['17000', '樱', 'PiNK PLUS'],
  ['18000', '紫', 'MURASAKi'],
  ['18500', '堇', 'MURASAKi PLUS'],
  ['19000', '白', 'MiLK'],
  ['19500', '雪', 'MiLK PLUS'],
  ['19900', '辉', 'FiNALE'],
  ['20000', '熊华', '舞萌DX'],
  ['21000', '爽煌', '舞萌DX2021'],
  ['22000', '宙星', '舞萌DX2022'],
  ['23000', '祭祝', '舞萌DX2023'],
  ['24000', '双宴', '舞萌DX2024'],
  ['25000', '镜', '舞萌DX2025'],
  ['25500', '彩', '舞萌DX2026'],
  ['__unknown__', '待核实', '国服版本待核实'],
];
const versions = versionRows.map(([value, label, title]) => ({ value, label, title }));
const names = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'];
const charts = [];
function chart(version, constant, difficulty, complete, title) {
  const id = 9000 + charts.length;
  const row = {
    id,
    song_id: id,
    chart_key: `${id}:${difficulty}`,
    title: title || `Fixture Song ${id}`,
    display_title: '',
    artist: 'Isolated UI fixture',
    type: 'standard',
    difficulty,
    difficulty_index: difficulty,
    difficulty_name: names[difficulty],
    level: String(Math.floor(constant)),
    constant,
    jacket_url: '/fixture-jacket.svg',
    cn_version: version,
    jp_release_version: 'UNiVERSE',
    jp_release_version_candidate: null,
    jp_release_provider: 'arcade_songs',
    jp_release_date: null,
    release_status: 'verified',
    requires_venue: difficulty >= 3,
    in_original_table: true,
    legacy_complete: complete && id % 2 === 0,
    legacy_venue: '',
    manual_revision: 0,
    manual_ap: null,
    manual_venue: '',
    venue: '',
    note: '',
    manual_date: null,
    achievement: complete ? 100.9876 : null,
    provider_achievement: complete ? 100.9876 : null,
    provider_fc: complete ? 'ap' : null,
    provider_synced_at: null,
    is_ap: complete && id % 2 !== 0,
    is_complete: complete,
    status: complete ? (id % 2 === 0 ? 'legacy' : 'ap') : 'incomplete',
    conflict: false,
    warnings: [],
  };
  charts.push(row);
  return row;
}
for (let i = 0; i < 32; i++)
  chart(
    '10000',
    14.9,
    3,
    i < 7,
    i === 0
      ? '旧版长曲名：僕たちの旅とエピローグ。そして、幾星霜の果てに見る景色。'
      : `Old CN Fixture ${i + 1}`,
  );
chart('11000', 14.8, 3, true);
chart('11000', 14.8, 3, true);
// A mixed-difficulty group becomes wholly complete only after selecting Master.
chart('11000', 14.8, 2, false, 'Incomplete Expert in otherwise complete row and column');
for (let i = 0; i < 40; i++)
  chart(
    versions[2 + (i % 18)].value,
    Number((14.7 - i * 0.1).toFixed(1)),
    i % 5 === 0 ? 0 : i % 3 === 0 ? 2 : 3,
    i % 4 === 0,
  );
chart('__unknown__', 13.1, 3, false, 'Unknown CN Fixture');
function counts(items) {
  const complete = items.filter((row) => row.is_complete).length;
  return { complete, total: items.length, remaining: items.length - complete };
}
function filtered(params) {
  return charts.filter(
    (row) =>
      (!params.has('difficulty') || row.difficulty === Number(params.get('difficulty'))) &&
      (!params.has('constant_min') || row.constant >= Number(params.get('constant_min'))) &&
      (!params.has('constant_max') || row.constant <= Number(params.get('constant_max'))) &&
      (!params.has('cn_version') || row.cn_version === params.get('cn_version')) &&
      (!params.has('cn_version_min') ||
        (row.cn_version !== '__unknown__' &&
          Number(row.cn_version) >= Number(params.get('cn_version_min')))) &&
      (!params.has('cn_version_max') ||
        (row.cn_version !== '__unknown__' &&
          Number(row.cn_version) <= Number(params.get('cn_version_max')))) &&
      (params.get('status') !== 'incomplete' || !row.is_complete),
  );
}
function matrix(params) {
  assert(!params.has('status'), 'Matrix denominator cannot be constrained by a completion status');
  const items = filtered(params);
  const selected = versions.filter((version) =>
    version.value === '__unknown__'
      ? items.some((row) => row.cn_version === version.value)
      : (!params.has('cn_version_min') ||
          Number(version.value) >= Number(params.get('cn_version_min'))) &&
        (!params.has('cn_version_max') ||
          Number(version.value) <= Number(params.get('cn_version_max'))),
  );
  return {
    versions: selected,
    rows: [...new Set(items.map((row) => row.constant))]
      .sort((a, b) => b - a)
      .map((constant) => {
        const matches = items.filter((row) => row.constant === constant);
        return {
          constant,
          cells: Object.fromEntries(
            selected.map((version) => [
              version.value,
              counts(matches.filter((row) => row.cn_version === version.value)),
            ]),
          ),
          total: counts(matches),
        };
      }),
    totals: Object.fromEntries(
      selected.map((version) => [
        version.value,
        counts(items.filter((row) => row.cn_version === version.value)),
      ]),
    ),
    summary: counts(items),
  };
}
const catalog = {
  revision: 'matrix-ui-fixture',
  decision_revision: 0,
  applied_decision_revision: 0,
  decisions_pending: false,
  snapshot_at: '2026-10-11T01:00:00Z',
  main: { status: 'succeeded', stage: '', message: 'Fixture only', diff: {}, warnings: [] },
  remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0, message: '' },
  can_rollback: false,
};
async function mock(context, name) {
  const control = { hold: null, fail: null };
  await context.route('**/api/**', async (route) => {
    if (new URL(route.request().url()).pathname === '/api/auth/session') {
      await route.fulfill({ json: { authenticated: true, auth_required: false } });
      return;
    }
    const request = route.request(),
      url = new URL(request.url()),
      params = url.searchParams,
      pathname = url.pathname;
    requests.push({
      context: name,
      path: pathname,
      params: Object.fromEntries(params),
      method: request.method(),
    });
    if (request.method() !== 'GET') {
      mutations.push({ path: pathname, method: request.method() });
      await route.fulfill({
        status: 405,
        json: { detail: 'Personal-data mutations are forbidden in this fixture' },
      });
      return;
    }
    if (control.fail?.(pathname, params)) {
      control.fail = null;
      injectedFailures.push({ context: name, path: pathname });
      await route.fulfill({ status: 503, json: { detail: '隔离测试：请求暂时失败' } });
      return;
    }
    if (control.hold?.matches(pathname, params)) await control.hold.promise;
    let value;
    if (pathname === '/api/completion-matrix') value = matrix(params);
    else if (pathname === '/api/charts') {
      const items = filtered(params),
        offset = Number(params.get('offset') || 0),
        limit = Number(params.get('limit') || 12);
      value = { items: items.slice(offset, offset + limit), total: items.length, offset, limit };
    } else if (pathname.startsWith('/api/charts/'))
      value = charts.find(
        (row) => row.chart_key === decodeURIComponent(pathname.split('/').at(-1)),
      );
    else if (pathname === '/api/chart-filters')
      value = {
        constants: [...new Set(charts.map((row) => row.constant))].sort((a, b) => a - b),
        versions: [],
        cn_versions: versions
          .filter((version) => version.value !== '__unknown__')
          .map((version) => ({
            value: version.value,
            label:
              Number(version.value) < 20000
                ? version.title
                : version.value === '20000'
                  ? 'DX'
                  : 'DX' + version.title.slice(-4),
          })),
        cn_matrix_versions: versions.filter((version) => version.value !== '__unknown__'),
      };
    else if (pathname === '/api/ap-records') value = { items: [], count: 0 };
    else if (pathname === '/api/venues') value = { items: [] };
    else if (pathname === '/api/sync/status') value = idleScoreSync;
    else if (pathname === '/api/settings/divingfish')
      value = { configured: false, source: null, sync: null };
    else if (pathname === '/api/settings/preferences')
      value = {
        default_constant_min: Math.min(...charts.map((row) => row.constant)),
        default_constant_max: Math.max(...charts.map((row) => row.constant)),
      };
    else if (pathname === '/api/catalog/update') value = catalog;
    else errors.push(`Unmocked API: ${pathname}`);
    await route
      .fulfill(
        value === undefined ? { status: 404, json: { detail: 'Unmocked API' } } : { json: value },
      )
      .catch((error) => {
        if (!/Target.*closed|Route.*already handled/.test(error.message)) throw error;
      });
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
function cell(page, constant, versionTitle) {
  return page.getByRole('button', {
    name: new RegExp(`^定数 ${constant.toFixed(1).replace('.', '\\.')}，${versionTitle}，`),
  });
}
function latest(name, api) {
  return requests.findLast((request) => request.context === name && request.path === `/api/${api}`);
}
async function settled(page, params = new URLSearchParams()) {
  const expected = matrix(params);
  await waitFor(
    async () =>
      expected.summary.total
        ? (await page
            .locator('.matrix-table tfoot td')
            .last()
            .textContent()
            .catch(() => '')) === `${expected.summary.complete} / ${expected.summary.total}`
        : (await page
            .locator('.matrix-workspace .empty-state')
            .textContent()
            .catch(() => '')) === '没有符合条件的谱面',
    'matrix response renders its exact denominator',
  );
}
async function versionAliasControls(page, name) {
  const expected = versions
    .filter((version) => version.value !== '__unknown__')
    .map((version) => ({ value: version.value, label: version.label }));
  for (const label of ['起始版本', '截止版本']) {
    const actual = await page
      .getByRole('combobox', { name: label, exact: true })
      .locator('option')
      .evaluateAll((options) =>
        options
          .filter((option) => option.value)
          .map((option) => ({ value: option.value, label: option.textContent })),
      );
    assert.deepEqual(
      actual,
      expected,
      `${name}: ${label} uses matrix labels while preserving numeric IDs`,
    );
  }
  for (const version of versions.filter((version) => Number(version.value) >= 20000)) {
    const header = page
      .locator('.matrix-table thead th')
      .filter({ has: page.getByText(version.label, { exact: true }) });
    assert.equal(await header.textContent(), version.label, `${name}: alias table heading`);
    assert.equal(
      await header.getAttribute('title'),
      version.title,
      `${name}: original version title remains in tooltip`,
    );
    const chart = charts.find((row) => row.cn_version === version.value);
    await cell(page, chart.constant, version.title).click();
    const dialog = page.getByRole('dialog', {
      name: `${chart.constant.toFixed(1)} · ${version.label}`,
      exact: true,
    });
    await dialog.locator('.matrix-chart').first().waitFor();
    assert.equal(
      latest(name, 'charts').params.cn_version,
      version.value,
      `${name}: alias drilldown retains numeric version ID`,
    );
    await dialog.getByRole('button', { name: '关闭谱面明细', exact: true }).click();
  }
  await page.locator('.matrix-scroll').evaluate((element) => {
    element.scrollLeft = 0;
    element.scrollTop = 0;
  });
  checks.push(
    `${name}: seven DX aliases agree across filter options, column headers and detail headings; original tooltip titles and numeric drilldown IDs are preserved`,
  );
}
async function highlightGroup(page, name, { complete, color, onlyGroup = false }) {
  const appearance = await page.locator('.matrix-table').evaluate((table, onlyGroup) => {
    const headers = [...table.querySelectorAll('thead th')];
    const column = headers.findIndex((header) => header.title === 'maimai PLUS');
    const row = [...table.querySelectorAll('tbody tr')].find(
      (row) => row.querySelector('th').textContent === '14.8',
    );
    const footer = table.querySelector('tfoot tr');
    const elements = {
      rowHeader: row.querySelector('th'),
      rowTotal: row.lastElementChild,
      rowTotalCount: row.lastElementChild.firstElementChild,
      columnHeader: headers[column],
      columnTotal: footer.children[column],
      columnTotalCount: footer.children[column].firstElementChild,
      ...(onlyGroup
        ? {
            grandTotalHeading: headers.at(-1),
            grandTotalFooterHeading: footer.firstElementChild,
            grandTotal: footer.lastElementChild,
            grandTotalCount: footer.lastElementChild.firstElementChild,
          }
        : {}),
    };
    return Object.fromEntries(
      Object.entries(elements).map(([name, element]) => [
        name,
        {
          text: element.textContent.trim(),
          color: getComputedStyle(element).backgroundColor,
          position: getComputedStyle(element).position,
        },
      ]),
    );
  }, onlyGroup);
  measurements.push({ name, expectedComplete: complete, highlightColor: color, appearance });
  for (const [element, value] of Object.entries(appearance)) {
    if (complete)
      assert.equal(
        value.color,
        color,
        `${name}: completed ${element} retains the pale red background despite sticky styles`,
      );
    else
      assert.notEqual(
        value.color,
        color,
        `${name}: incomplete ${element} must not retain completion highlighting`,
      );
  }
  assert.equal(appearance.rowTotal.text, complete ? '2 / 2' : '2 / 3');
  assert.equal(appearance.columnTotal.text, complete ? '2 / 2' : '2 / 3');
}
async function emptyHighlights(page, name, color) {
  const appearance = await page
    .locator('.matrix-table [data-empty="true"]')
    .evaluateAll((elements) => ({
      count: elements.length,
      colors: [...new Set(elements.map((element) => getComputedStyle(element).backgroundColor))],
      wrongText: elements
        .filter((element) => element.textContent.trim() !== '—')
        .map((element) => element.textContent),
    }));
  assert(appearance.count > 0, `${name}: fixture includes empty cells`);
  assert.deepEqual(
    appearance.colors,
    [color],
    `${name}: every zero-total cell has the same pale red background`,
  );
  assert.deepEqual(appearance.wrongText, [], `${name}: empty cells retain the dash`);
  measurements.push({ name, emptyAppearance: appearance });
}
async function audit(page, name, { dialog = false } = {}) {
  const geometry = await page.evaluate(() => {
    const visible = (element) => {
      const rect = element.getBoundingClientRect(),
        css = getComputedStyle(element);
      return (
        rect.width > 0 && rect.height > 0 && css.display !== 'none' && css.visibility !== 'hidden'
      );
    };
    const all = [...document.querySelectorAll('body *')].filter(visible);
    return {
      width: innerWidth,
      height: innerHeight,
      bodyWidth: document.body.scrollWidth,
      documentWidth: document.documentElement.scrollWidth,
      inline: all
        .filter((element) => element.hasAttribute('style'))
        .map((element) => element.outerHTML.slice(0, 100)),
      tinyText: all
        .filter(
          (element) =>
            [...element.childNodes].some(
              (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim(),
            ) && parseFloat(getComputedStyle(element).fontSize) < 12,
        )
        .map((element) => element.textContent.trim().slice(0, 60)),
      scroll: (() => {
        const element = document.querySelector('.matrix-scroll');
        return element
          ? {
              clientWidth: element.clientWidth,
              scrollWidth: element.scrollWidth,
              clientHeight: element.clientHeight,
              scrollHeight: element.scrollHeight,
            }
          : null;
      })(),
    };
  });
  measurements.push({ name, ...geometry });
  assert(
    geometry.bodyWidth <= geometry.width + 1 && geometry.documentWidth <= geometry.width + 1,
    `${name}: body must not overflow horizontally`,
  );
  assert.deepEqual(geometry.inline, [], `${name}: no inline styles`);
  assert.deepEqual(geometry.tinyText, [], `${name}: readable 12px minimum text`);
  if (dialog) {
    const box = await page.getByRole('dialog').boundingBox();
    assert(
      box.x >= 0 &&
        box.y >= 0 &&
        box.x + box.width <= geometry.width + 1 &&
        box.y + box.height <= geometry.height + 1,
      `${name}: dialog remains within viewport`,
    );
  }
  await page.screenshot({ path: path.join(output, `${name}.png`), fullPage: !dialog });
  checks.push(
    `${name}: no body overflow, no inline styles, readable labels${dialog ? ', dialog fits viewport' : ''}`,
  );
}
async function sticky(page, name) {
  const before = await page.locator('.matrix-scroll').evaluate((element) => {
    element.scrollLeft = 0;
    element.scrollTop = 0;
    const rect = element.getBoundingClientRect();
    return {
      left: element.querySelector('tbody th').getBoundingClientRect().left - rect.left,
      top: element.querySelector('thead th').getBoundingClientRect().top - rect.top,
      width: element.clientWidth,
      full: element.scrollWidth,
      height: element.clientHeight,
      fullHeight: element.scrollHeight,
    };
  });
  assert(before.full > before.width, `${name}: fixture must exercise local horizontal scrolling`);
  assert(
    before.fullHeight > before.height,
    `${name}: fixture must exercise local vertical scrolling`,
  );
  await page.locator('.matrix-scroll').evaluate((element) => {
    element.scrollLeft = 430;
    element.scrollTop = 310;
  });
  const after = await page.locator('.matrix-scroll').evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      left: element.querySelector('tbody th').getBoundingClientRect().left - rect.left,
      top: element.querySelector('thead th').getBoundingClientRect().top - rect.top,
      scrollLeft: element.scrollLeft,
      scrollTop: element.scrollTop,
    };
  });
  assert(after.scrollLeft > 0 && after.scrollTop > 0);
  assert(Math.abs(after.left - before.left) < 1, `${name}: first column must stay fixed`);
  assert(Math.abs(after.top - before.top) < 1, `${name}: header must stay fixed`);
  await audit(page, `${name}-scrolled`);
  await page.locator('.matrix-scroll').evaluate((element) => {
    element.scrollLeft = 0;
    element.scrollTop = 0;
  });
  checks.push(`${name}: matrix scrolls locally on both axes; column and header stay fixed`);
}
let activePage;
try {
  const viewports = navOnly
    ? [
        ['mobile-390', { width: 390, height: 844 }],
        ['mobile-320', { width: 320, height: 844 }],
      ]
    : [
        ['desktop', { width: 1440, height: 1000 }],
        ['mobile', { width: 390, height: 844 }],
      ];
  for (const [name, viewport] of viewports) {
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
    await page.goto(origin + '/#statistics', { waitUntil: 'networkidle' });
    await settled(page);
    if (navOnly) {
      const navigation = await page.locator('.nav-item').evaluateAll((items) =>
        items.map((element) => {
          const text = [...element.querySelectorAll('span')].find(
              (label) => label.getBoundingClientRect().width > 0,
            ),
            bounds = element.getBoundingClientRect(),
            label = text.getBoundingClientRect(),
            css = getComputedStyle(text);
          return {
            text: text.textContent,
            left: bounds.left,
            right: bounds.right,
            labelLeft: label.left,
            labelRight: label.right,
            labelHeight: label.height,
            lineHeight: parseFloat(css.lineHeight),
            whiteSpace: css.whiteSpace,
          };
        }),
      );
      measurements.push({ name: `${name}-navigation`, navigation });
      assert.equal(navigation.length, 6);
      for (const [index, item] of navigation.entries()) {
        assert.equal(item.whiteSpace, 'nowrap', `${name}: ${item.text} uses a single line`);
        assert(item.labelHeight <= item.lineHeight + 1, `${name}: ${item.text} wraps`);
        assert(
          item.labelLeft >= item.left - 1 && item.labelRight <= item.right + 1,
          `${name}: ${item.text} exceeds its navigation slot`,
        );
        assert(
          item.labelLeft >= 0 && item.labelRight <= viewport.width,
          `${name}: ${item.text} exceeds viewport`,
        );
        if (index)
          assert(
            navigation[index - 1].labelRight <= item.labelLeft,
            `${name}: adjacent navigation labels overlap`,
          );
      }
      await audit(page, `${name}-matrix`);
      await page.screenshot({ path: path.join(output, `${name}-viewport.png`) });
      checks.push(
        `${name}: all six navigation labels remain single-line within their slots without overlap`,
      );
      await context.close();
      activePage = null;
      continue;
    }
    assert.equal(await page.getByRole('heading', { name: '完成统计', exact: true }).count(), 1);
    assert.equal(await page.locator('a.nav-item.active').getAttribute('aria-label'), '完成统计');
    assert.equal(
      await page.locator('a.nav-item.active').innerText(),
      name === 'mobile' ? '统计' : '完成统计',
    );
    assert.equal(await page.getByRole('combobox', { name: '难度', exact: true }).inputValue(), '');
    assert.equal(await page.locator('.matrix-table th[title="maimai"]').textContent(), '初');
    assert.equal(await page.locator('.matrix-table th[title="maimai PLUS"]').textContent(), '真');
    assert.equal(await cell(page, 14.9, 'maimai').textContent(), '7 / 32');
    assert.equal(await cell(page, 14.7, 'GreeN').getAttribute('data-complete'), 'true');
    assert((await page.locator('.matrix-table td [data-empty="true"]').count()) > 0);
    const completeColor = await cell(page, 14.7, 'GreeN').evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    );
    const incompleteColor = await cell(page, 14.9, 'maimai').evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    );
    assert.notEqual(completeColor, incompleteColor, 'All-complete cells have visible highlight');
    await versionAliasControls(page, name);
    await emptyHighlights(page, `${name}-empty-cell-highlights`, completeColor);
    await highlightGroup(page, `${name}-group-before-filter`, {
      complete: false,
      color: completeColor,
    });
    await audit(page, `${name}-matrix`);
    await sticky(page, name);
    await page.getByRole('combobox', { name: '难度', exact: true }).selectOption('3');
    await settled(page, new URLSearchParams({ difficulty: '3' }));
    await highlightGroup(page, `${name}-group-filtered`, { complete: true, color: completeColor });
    await emptyHighlights(page, `${name}-filtered-empty-cell-highlights`, completeColor);
    await page.locator('.matrix-scroll').evaluate((element) => {
      element.scrollLeft = 430;
      element.scrollTop = 310;
    });
    await highlightGroup(page, `${name}-group-filtered-scrolled`, {
      complete: true,
      color: completeColor,
    });
    await audit(page, `${name}-filtered-highlights`);
    await page.locator('.matrix-scroll').evaluate((element) => {
      element.scrollLeft = 0;
      element.scrollTop = 0;
    });
    await page.getByRole('combobox', { name: '起始版本', exact: true }).selectOption('11000');
    await page.getByRole('combobox', { name: '截止版本', exact: true }).selectOption('11000');
    await settled(
      page,
      new URLSearchParams({ difficulty: '3', cn_version_min: '11000', cn_version_max: '11000' }),
    );
    await highlightGroup(page, `${name}-whole-filter-complete`, {
      complete: true,
      color: completeColor,
      onlyGroup: true,
    });
    await page.getByRole('button', { name: '清除', exact: true }).click();
    await settled(page);
    await highlightGroup(page, `${name}-group-cleared`, { complete: false, color: completeColor });
    await emptyHighlights(page, `${name}-cleared-empty-cell-highlights`, completeColor);
    checks.push(
      `${name}: zero-total cells keep a dash with pale red fill; row/column headers and totals change from incomplete to complete after filtering and restore on clear, including sticky intersections`,
    );
    const matrixRequests = requests.filter(
      (request) => request.context === name && request.path === '/api/completion-matrix',
    ).length;
    await page.getByRole('combobox', { name: '显示', exact: true }).selectOption('remaining');
    assert.equal(await cell(page, 14.9, 'maimai').textContent(), '25');
    assert.equal(await cell(page, 14.7, 'GreeN').textContent(), '0');
    assert.equal(
      requests.filter(
        (request) => request.context === name && request.path === '/api/completion-matrix',
      ).length,
      matrixRequests,
      'Display toggle does not refetch a changed denominator',
    );
    await cell(page, 14.9, 'maimai').click();
    const dialog = page.getByRole('dialog', { name: '14.9 · 初', exact: true });
    await dialog.locator('.matrix-chart').first().waitFor();
    assert.equal(latest(name, 'charts').params.cn_version, '10000');
    assert.equal(latest(name, 'charts').params.constant_min, '14.9');
    assert.equal(latest(name, 'charts').params.constant_max, '14.9');
    assert.equal(latest(name, 'charts').params.status, 'incomplete');
    assert.equal(await dialog.getByRole('button', { name: '全部 32', exact: true }).count(), 1);
    assert.equal(
      await dialog
        .getByRole('button', { name: '未完成 25', exact: true })
        .getAttribute('aria-pressed'),
      'true',
    );
    assert.equal(await dialog.locator('.matrix-chart').count(), 20);
    await audit(page, `${name}-old-cn-dialog`, { dialog: true });
    await dialog.getByRole('button', { name: '下一页谱面' }).click();
    await waitFor(
      async () => (await dialog.locator('.matrix-chart').count()) === 5,
      'incomplete pagination',
    );
    assert.equal(latest(name, 'charts').params.offset, '20');
    await dialog.getByRole('button', { name: '全部 32', exact: true }).click();
    await waitFor(
      async () =>
        latest(name, 'charts').params.status === 'all' &&
        (await dialog.locator('.matrix-chart').count()) === 20,
      'all tab resets page',
    );
    assert.equal(latest(name, 'charts').params.offset, '0');
    await dialog.getByRole('button', { name: '下一页谱面' }).click();
    await waitFor(
      async () => (await dialog.locator('.matrix-chart').count()) === 12,
      'all pagination',
    );
    await dialog.locator('.matrix-chart').first().click();
    await page.getByRole('dialog', { name: '谱面记录', exact: true }).waitFor();
    assert.equal(await page.locator('.matrix-dialog').count(), 0);
    await audit(page, `${name}-chart-detail`, { dialog: true });
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    assert.equal(
      await cell(page, 14.9, 'maimai').textContent(),
      '25',
      'Drilldown filters preserve matrix state',
    );
    await cell(page, 14.7, 'GreeN').click();
    await page.getByText('这个分组已全部完成', { exact: true }).waitFor();
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0);
    await cell(page, 13.1, '国服版本待核实').click();
    await page.locator('.matrix-chart').first().waitFor();
    assert.equal(latest(name, 'charts').params.cn_version, '__unknown__');
    await page.keyboard.press('Escape');
    checks.push(
      `${name}: old CN and unknown cells drill down, both tabs paginate, all-complete empty state works, detail opens, denominators stay fixed`,
    );
    await page.getByRole('combobox', { name: '显示', exact: true }).selectOption('complete');
    await page.getByRole('combobox', { name: '难度', exact: true }).selectOption('3');
    await page.getByRole('combobox', { name: '起始版本', exact: true }).selectOption('10000');
    await page.getByRole('combobox', { name: '截止版本', exact: true }).selectOption('11000');
    await settled(
      page,
      new URLSearchParams({ difficulty: '3', cn_version_min: '10000', cn_version_max: '11000' }),
    );
    const lower = page.getByRole('slider', { name: '最低定数' });
    await lower.focus();
    await page.keyboard.press('End');
    await settled(
      page,
      new URLSearchParams({
        difficulty: '3',
        cn_version_min: '10000',
        cn_version_max: '11000',
        constant_min: '14.9',
        constant_max: '14.9',
      }),
    );
    const params = latest(name, 'completion-matrix').params;
    assert.deepEqual(params, {
      difficulty: '3',
      constant_min: '14.9',
      constant_max: '14.9',
      cn_version_min: '10000',
      cn_version_max: '11000',
    });
    await cell(page, 14.9, 'maimai').click();
    await page.locator('.matrix-chart').first().waitFor();
    assert.equal(latest(name, 'charts').params.difficulty, '3');
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: '清除', exact: true }).click();
    await settled(page);
    assert.deepEqual(latest(name, 'completion-matrix').params, {});
    assert.equal(
      await page.getByRole('combobox', { name: '显示', exact: true }).inputValue(),
      'complete',
    );
    await page.getByRole('combobox', { name: '截止版本', exact: true }).selectOption('11000');
    await page.getByRole('combobox', { name: '起始版本', exact: true }).selectOption('20000');
    await settled(page, new URLSearchParams({ cn_version_min: '20000', cn_version_max: '20000' }));
    assert.equal(
      await page.getByRole('combobox', { name: '截止版本', exact: true }).inputValue(),
      '20000',
    );
    await page.getByRole('button', { name: '清除', exact: true }).click();
    await settled(page);
    await page.getByRole('combobox', { name: '难度', exact: true }).selectOption('4');
    await settled(page, new URLSearchParams({ difficulty: '4' }));
    assert.equal(await page.locator('.matrix-table').count(), 0);
    await audit(page, `${name}-empty`);
    await page.getByRole('button', { name: '清除', exact: true }).click();
    await settled(page);
    checks.push(
      `${name}: difficulty, inclusive version bounds, keyboard constant range and clear reach API; reversed bounds normalize; empty state hides stale table`,
    );
    if (name === 'desktop') {
      let release;
      const promise = new Promise((resolve) => {
        release = resolve;
      });
      control.hold = {
        matches: (pathname, params) =>
          pathname === '/api/completion-matrix' && params.get('difficulty') === '0',
        promise,
      };
      await page.getByRole('combobox', { name: '难度', exact: true }).selectOption('0');
      await waitFor(
        () => latest(name, 'completion-matrix').params.difficulty === '0',
        'held request starts',
      );
      await page.getByText('加载完成统计…', { exact: true }).waitFor();
      assert.equal(await page.locator('.matrix-table').count(), 0);
      await page.getByRole('combobox', { name: '难度', exact: true }).selectOption('3');
      await settled(page, new URLSearchParams({ difficulty: '3' }));
      control.hold = null;
      release();
      await page.waitForLoadState('networkidle');
      await settled(page, new URLSearchParams({ difficulty: '3' }));
      control.fail = (pathname, params) =>
        pathname === '/api/completion-matrix' && params.get('difficulty') === '2';
      await page.getByRole('combobox', { name: '难度', exact: true }).selectOption('2');
      await page.getByRole('alert').waitFor();
      assert.equal(await page.locator('.matrix-table').count(), 0);
      await page.getByRole('alert').getByRole('button', { name: '重试', exact: true }).click();
      await settled(page, new URLSearchParams({ difficulty: '2' }));
      await page.getByRole('button', { name: '清除', exact: true }).click();
      await settled(page);
      checks.push(
        'Delayed superseded response cannot overwrite the newer filter; loading hides stale counts; failed request exposes retry and recovers',
      );
    }
    await context.close();
    activePage = null;
  }
  assert.deepEqual(mutations, [], 'No personal-data writes');
  assert.deepEqual(errors, [], 'No browser/API errors except injected 503');
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify(
      {
        passed: true,
        isolated: true,
        bundle,
        createdAt: new Date().toISOString(),
        checks,
        measurements,
        requests,
        personalDataWrites: mutations.length,
        mutations,
        errors,
        injectedFailures,
      },
      null,
      2,
    ),
  );
  await fs.rm(path.join(output, 'failure.png'), { force: true });
  console.log(
    `Completion matrix UI passed (${checks.length} checks, ${navOnly ? '390px + 320px navigation' : 'desktop + 390px mobile'}, ${mutations.length} personal-data writes)`,
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
        bundle,
        createdAt: new Date().toISOString(),
        checks,
        measurements,
        requests,
        personalDataWrites: mutations.length,
        mutations,
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
