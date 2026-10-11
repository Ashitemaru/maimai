import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { fixtureAsset, useSystemCoverFont } from './fixtures.mjs';

const project = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(project, 'frontend/dist'));
const output = path.resolve(process.env.UI_AUDIT_OUTPUT || path.join(project, 'output/ui-audit'));
await fs.mkdir(output, { recursive: true });
const exportImages = new Map();
const mime = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.css': 'text/css',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.webp': 'image/webp',
  '.json': 'application/json',
  '.otf': 'font/otf',
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
    // Serve the finished image over HTTP so the browser fetch receives the actual uploaded PNG bytes.
    const exported = exportImages.get(pathname);
    if (exported) {
      exportRequests.push({ path: pathname, method: req.method });
      res.setHeader('Content-Type', 'image/png');
      res.setHeader(
        'Content-Disposition',
        `attachment; filename="completion.png"; filename*=UTF-8''${encodeURIComponent(exported.name)}`,
      );
      res.end(exported.image);
      return;
    }
    const fixture = fixtureAsset(pathname);
    if (fixture) {
      res.setHeader('Content-Type', fixture.contentType);
      res.end(fixture.body);
      return;
    }
    const root = bundle;
    const relative = pathname === '/' ? 'index.html' : pathname.slice(1);
    const file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep)) {
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
const errors = [],
  mutations = [],
  checks = [],
  measurements = [],
  requests = [],
  exportRequests = [];
const badgeAppearance = new Map();
const difficultyNames = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'];
const cnVersions = [
  { value: '20000', label: 'DX' },
  { value: '21000', label: 'DX2021' },
  { value: '22000', label: 'DX2022' },
  { value: '23000', label: 'DX2023' },
  { value: '24000', label: 'DX2024' },
  { value: '25000', label: 'DX2025' },
  { value: '25500', label: 'DX2026' },
];
const jpVersions = [
  { value: 'UNiVERSE', label: 'UNiVERSE' },
  { value: 'FESTiVAL', label: 'FESTiVAL' },
];
const charts = Array.from({ length: 18 }, (_, index) => ({
  id: 100 + index,
  song_id: 100 + index,
  difficulty: index % 5,
  difficulty_index: index % 5,
  chart_key: `${100 + index}:${index % 5}`,
  title:
    index === 0
      ? 'Imitation:Loud Lounge'
      : index === 1
        ? '僕たちの旅とエピローグ。そして、幾星霜の果てに見る景色。'
        : `Sample Song ${index + 1}`,
  display_title: '',
  artist:
    index === 1
      ? 'Very long artist name / ワンダーランズ×初音ミク feat. ピノキオピー'
      : 'maimai sound team',
  type: index % 2 ? 'standard' : 'dx',
  difficulty_name: difficultyNames[index % 5],
  level: index % 3 ? '13+' : '14',
  constant: Number((14.9 - index * 0.7).toFixed(1)),
  jacket_url: '/jackets/100.png',
  cn_version: index < 7 ? '20000' : index < 12 ? '23000' : '25500',
  jp_release_version: index === 17 ? 'FESTiVAL' : 'UNiVERSE',
  jp_release_version_candidate: null,
  jp_release_provider: 'arcade_songs',
  jp_release_date: null,
  release_status: 'verified',
  requires_venue: index % 2 === 0,
  in_original_table: true,
  legacy_complete: false,
  legacy_venue: '',
  manual_revision: 0,
  manual_ap: index % 3 === 0 ? true : null,
  manual_venue: index % 3 === 0 ? '上海 · 星光游乐城' : '',
  venue: index % 3 === 0 ? '上海 · 星光游乐城' : '',
  note: '',
  manual_date: index % 3 === 0 ? '2026-10-01' : null,
  achievement: index % 3 === 0 ? 100.9876 : null,
  provider_achievement: index % 3 === 0 ? 100.9876 : null,
  provider_fc: index % 3 === 0 ? 'ap' : null,
  provider_synced_at: null,
  is_ap: index % 3 === 0,
  is_complete: index % 3 === 0,
  status: index % 3 === 0 ? 'ap' : 'incomplete',
  conflict: false,
  warnings: [],
}));
Object.assign(charts[1], {
  constant: 14.9,
  legacy_complete: true,
  is_complete: true,
  status: 'legacy',
});
Object.assign(charts[2], {
  achievement: 101,
  provider_achievement: 101,
  provider_fc: 'app',
  is_ap: true,
  is_complete: true,
  status: 'app',
});
Object.assign(charts[4], { achievement: 99.7, provider_achievement: 99.7, provider_fc: 'fc' });
// Distinct canned API orders test transport and rendering; backend comparator semantics have their own tests.
const sortOrders = {
  constant_desc: [
    100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117,
  ],
  constant_asc: [
    117, 116, 115, 114, 113, 112, 111, 110, 109, 108, 107, 106, 105, 104, 103, 102, 100, 101,
  ],
  achievement_desc: [
    102, 100, 103, 106, 109, 112, 115, 104, 101, 105, 107, 108, 110, 111, 113, 114, 116, 117,
  ],
  achievement_asc: [
    104, 100, 103, 106, 109, 112, 115, 102, 101, 105, 107, 108, 110, 111, 113, 114, 116, 117,
  ],
  ap_desc: [
    100, 102, 103, 106, 109, 112, 115, 101, 104, 105, 107, 108, 110, 111, 113, 114, 116, 117,
  ],
  ap_asc: [
    101, 104, 105, 107, 108, 110, 111, 113, 114, 116, 117, 100, 102, 103, 106, 109, 112, 115,
  ],
};
const status = {
  revision: 'ui-audit-r1',
  decision_revision: 0,
  applied_decision_revision: 0,
  decisions_pending: false,
  snapshot_at: '2026-10-11T01:00:00Z',
  main: {
    status: 'succeeded',
    stage: '',
    message: '曲库已更新',
    diff: { songs_added: 2, charts_added: 8, constants_changed: 1 },
    warnings: [],
  },
  remy: {
    status: 'complete',
    pending_count: 0,
    verified_count: 134,
    updated_count: 0,
    message: '',
  },
  can_rollback: true,
};
const reviewRecords = [1, 2, 3].map((difficulty) => ({
  id: 383,
  difficulty,
  chart_key: `383:${difficulty}`,
  title: 'Link',
  type: 'standard',
  difficulty_name: difficultyNames[difficulty],
  status: 'pending',
  fields: [
    {
      field_key: 'title',
      label: '曲名',
      required: true,
      options: [
        { option_id: 'title-lxns', source: 'lxns', value: 'Link' },
        { option_id: 'title-divingfish', source: 'divingfish', value: 'Link(CoF)' },
      ],
    },
  ],
}));
function filtered(params) {
  return charts.filter(
    (chart) =>
      (!params.get('q') ||
        `${chart.id} ${chart.title}`.toLowerCase().includes(params.get('q').toLowerCase())) &&
      (!params.get('difficulty') || chart.difficulty === Number(params.get('difficulty'))) &&
      (!params.get('version') || chart.jp_release_version === params.get('version')) &&
      (!params.get('cn_version') || chart.cn_version === params.get('cn_version')) &&
      (!params.has('constant_min') || chart.constant >= Number(params.get('constant_min'))) &&
      (!params.has('constant_max') || chart.constant <= Number(params.get('constant_max'))) &&
      (!params.get('status') ||
        params.get('status') === 'all' ||
        (params.get('status') === 'complete' && chart.is_complete) ||
        (params.get('status') === 'incomplete' && !chart.is_complete)),
  );
}
function counts(items) {
  const complete = items.filter((row) => row.is_complete).length;
  return {
    total: items.length,
    complete,
    ap: items.filter((row) => row.is_ap).length,
    remaining: items.length - complete,
    conflicts: 0,
    legacy_complete: items.filter((row) => row.legacy_complete).length,
  };
}
function summary(items) {
  return {
    ...counts(items),
    levels: [],
    constants: [...new Set(items.map((row) => row.constant))].map((constant) => ({
      constant,
      ...counts(items.filter((row) => row.constant === constant)),
    })),
    sync: null,
  };
}
function ordered(params) {
  const matches = new Map(filtered(params).map((chart) => [chart.id, chart]));
  return (sortOrders[params.get('sort') || 'constant_desc'] || []).flatMap((id) =>
    matches.has(id) ? [matches.get(id)] : [],
  );
}
async function mock(context, { completionItems } = {}) {
  await useSystemCoverFont(context);
  // Temporary image exports are isolated in memory; all catalog and personal-data writes remain forbidden.
  let exportJob;
  await context.route('**/api/**', async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      pathname = url.pathname;
    requests.push({
      path: pathname,
      params: Object.fromEntries(url.searchParams),
      method: request.method(),
    });
    if (pathname === '/api/completion-exports' && request.method() === 'POST') {
      assert.equal(
        exportJob,
        undefined,
        'The single-chart export fixture must create only one temporary job',
      );
      const input = request.postDataJSON();
      assert.match(input.name, /^AP完成表-\d{12}\.png$/);
      assert.equal(input.width, 3200);
      assert(Number.isInteger(input.height) && input.height >= 150);
      assert.equal(input.parts, 1, 'The single-chart fixture should fit into one real PNG strip');
      exportJob = { ...input, id: 'ui-audit-export', image: null, finished: false };
      exportRequests.push({ path: pathname, method: request.method(), ...input });
      await route.fulfill({ json: { id: exportJob.id } });
      return;
    }
    if (
      exportJob &&
      pathname === `/api/completion-exports/${exportJob.id}/parts/0` &&
      request.method() === 'PUT'
    ) {
      assert.equal(exportJob.image, null, 'The fixture strip should upload once');
      assert.equal(request.headers()['content-type'], 'image/png');
      const image = request.postDataBuffer();
      assert(image && image.length > 24);
      assert.equal(image.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
      assert.equal(image.readUInt32BE(16), 3200);
      assert.equal(image.readUInt32BE(20), exportJob.height);
      exportJob.image = image;
      exportRequests.push({
        path: pathname,
        method: request.method(),
        bytes: image.length,
        width: 3200,
        height: exportJob.height,
      });
      await route.fulfill({ json: { ok: true } });
      return;
    }
    if (
      exportJob &&
      pathname === `/api/completion-exports/${exportJob.id}/finish` &&
      request.method() === 'POST'
    ) {
      assert(exportJob.image, 'Finish requires its uploaded strip');
      assert.equal(exportJob.finished, false);
      exportJob.finished = true;
      exportRequests.push({ path: pathname, method: request.method() });
      exportImages.set(`/api/completion-exports/${exportJob.id}/image`, {
        name: exportJob.name,
        image: exportJob.image,
      });
      await route.fulfill({
        json: { url: `/api/completion-exports/${exportJob.id}/image`, name: exportJob.name },
      });
      return;
    }
    if (
      exportJob &&
      pathname === `/api/completion-exports/${exportJob.id}/image` &&
      request.method() === 'GET'
    ) {
      assert(exportJob.finished, 'Only a finished temporary export may download');
      await route.continue();
      return;
    }
    if (
      exportJob &&
      pathname === `/api/completion-exports/${exportJob.id}` &&
      request.method() === 'DELETE'
    ) {
      exportRequests.push({ path: pathname, method: request.method() });
      await route.fulfill({ json: { ok: true } });
      return;
    }
    if (request.method() !== 'GET') {
      mutations.push({ path: pathname, method: request.method() });
      await route.fulfill({ status: 405, json: { detail: 'UI audit forbids mutations' } });
      return;
    }
    let value;
    if (pathname === '/api/charts') {
      const items = ordered(url.searchParams);
      const offset = Number(url.searchParams.get('offset') || 0),
        limit = Number(url.searchParams.get('limit') || 12);
      value = { items: items.slice(offset, offset + limit), total: items.length, offset, limit };
    } else if (pathname === '/api/completion-table') {
      const items = completionItems || ordered(url.searchParams);
      value = {
        items,
        total: items.length,
        summary: summary(items),
        generated_at: '2026-10-11T04:30:00+08:00',
      };
    } else if (pathname.startsWith('/api/charts/')) {
      value = charts.find(
        (chart) => chart.chart_key === decodeURIComponent(pathname.split('/').at(-1)),
      );
    } else if (pathname === '/api/summary') value = summary(filtered(url.searchParams));
    else if (pathname === '/api/chart-filters')
      value = { constants: [1, 5, 10, 13.9, 15], cn_versions: cnVersions, versions: jpVersions };
    else if (pathname === '/api/settings/preferences')
      value = { default_constant_min: 1, default_constant_max: 15 };
    else if (pathname === '/api/venues')
      value = {
        items: [
          { name: '上海 · 星光游乐城', count: 23 },
          { name: '广州 · 动漫星城', count: 108 },
          { name: '测试很长的机厅名字 — 城市购物中心店', count: 7 },
        ],
      };
    else if (pathname === '/api/settings/divingfish')
      value = { configured: false, source: null, sync: null };
    else if (pathname === '/api/catalog/update') value = status;
    else if (pathname === '/api/catalog/records/review')
      value = {
        schema_version: 2,
        revision: status.revision,
        decision_revision: 0,
        decisions_pending: false,
        counts: { total: 5564, admitted: 5561, pending: 3 },
        records: reviewRecords,
      };
    else if (pathname === '/api/catalog/records/decisions')
      value = {
        revision: status.revision,
        decision_revision: 0,
        decisions_pending: false,
        records: [
          {
            ...reviewRecords[0],
            difficulty: 0,
            chart_key: '383:0',
            difficulty_name: 'Basic',
            status: 'manual',
            fields: [
              { field_key: 'title', label: '曲名', value: 'Link', source: 'lxns' },
              { field_key: 'constant', label: '定数', value: 4, source: 'lxns' },
            ],
          },
        ],
      };
    else if (pathname === '/api/catalog/update/report')
      value = {
        diff: {
          added_songs: [{ song_id: 100, title: 'Imitation:Loud Lounge' }],
          added_charts: [
            {
              song_id: 100,
              title: 'Imitation:Loud Lounge',
              chart_key: '100:3',
              type: 'dx',
              difficulty: 3,
              constant: 13.9,
            },
          ],
          changed_charts: [
            {
              song_id: 101,
              title: 'Sample Song',
              chart_key: '101:3',
              type: 'standard',
              difficulty: 3,
              changes: { level_value: { before: 13.8, after: 13.9 } },
            },
          ],
        },
        issues: [],
        sources: {},
      };
    else errors.push(`Unexpected API: GET ${pathname}`);
    await route.fulfill(
      value === undefined ? { status: 404, json: { detail: 'Unmocked API' } } : { json: value },
    );
  });
  // The audit is self-contained: no request may escape the isolated static server.
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      errors.push(`External request blocked: ${url.origin}${url.pathname}`);
      await route.abort();
    } else await route.fallback();
  });
}
async function waitFor(predicate, label) {
  const until = Date.now() + 10000;
  while (Date.now() < until) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 35));
  }
  throw new Error(`Timed out: ${label}`);
}
async function badges(scope, label) {
  const values = await scope
    .locator('.difficulty[data-difficulty],.type-label[data-chart-type]')
    .evaluateAll((elements) =>
      elements
        .filter(
          (element) =>
            element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0,
        )
        .map((element) => {
          const css = getComputedStyle(element),
            kind = element.matches('.difficulty') ? 'difficulty' : 'type';
          return {
            kind,
            key: `${kind}:${element.getAttribute(kind === 'difficulty' ? 'data-difficulty' : 'data-chart-type')}`,
            text: element.textContent.trim(),
            style: Object.fromEntries(
              [
                'backgroundColor',
                'color',
                'borderRadius',
                'fontSize',
                'fontWeight',
                'lineHeight',
                'paddingTop',
                'paddingRight',
                'paddingBottom',
                'paddingLeft',
              ].map((property) => [property, css[property]]),
            ),
            height: element.getBoundingClientRect().height,
          };
        }),
    );
  assert(
    values.some((value) => value.kind === 'difficulty'),
    `${label}: missing visible difficulty badge`,
  );
  assert(
    values.some((value) => value.kind === 'type'),
    `${label}: missing visible chart format badge`,
  );
  for (const value of values) {
    assert(value.text, `${label}: empty ${value.kind} badge`);
    const appearance = { style: value.style, height: value.height };
    if (badgeAppearance.has(value.key))
      assert.deepEqual(
        appearance,
        badgeAppearance.get(value.key),
        `${label}: ${value.key} badge appearance changed under its parent`,
      );
    else badgeAppearance.set(value.key, appearance);
  }
}
async function retryControls(page) {
  const initial = structuredClone(status.remy);
  const setState = async (patch) => {
    status.remy = { ...initial, ...patch };
    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('.catalog-verification').waitFor();
  };
  const retry = () =>
    page.locator('.catalog-verification').getByRole('button', { name: '重试', exact: true });
  assert.equal(await retry().count(), 0, 'Completed evidence should have no retry action');
  for (const remyStatus of ['pending', 'failed']) {
    await setState({ status: remyStatus, pending_count: 2 });
    assert.equal(await retry().count(), 1);
    assert.equal(await retry().isEnabled(), true);
    assert.equal(
      await page.locator('.catalog-verification button').count(),
      1,
      'Unverified evidence should have exactly one retry action',
    );
  }
  await setState({ status: 'running', pending_count: 2 });
  assert.equal(await retry().count(), 0, 'Running verification should not allow duplicate retry');
  await setState({ status: 'failed', pending_count: 0 });
  assert.equal(
    await retry().count(),
    0,
    'No missing evidence should never trigger re-verification',
  );
  await setState(initial);
  assert.equal(await page.getByRole('button', { name: '重核已有证据', exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '重试核准', exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '回退上版', exact: true }).count(), 0);
  assert.equal(await page.locator('.catalog-footer').count(), 0);
  checks.push(
    'Verification exposes one retry only for unfinished evidence, none when busy or all verified; obsolete recheck and source/rollback footer removed',
  );
}
async function audit(page, label) {
  const measured = await page.evaluate(() => {
    const visible = (element) => {
      const css = getComputedStyle(element),
        rect = element.getBoundingClientRect();
      return (
        css.visibility !== 'hidden' && css.display !== 'none' && rect.width > 0 && rect.height > 0
      );
    };
    const identify = (element) =>
      `${element.tagName.toLowerCase()}.${[...element.classList].join('.')} ${(element.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 70)}`;
    const text = [...document.querySelectorAll('body *')].filter(
      (element) =>
        visible(element) &&
        [...element.childNodes].some(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim(),
        ),
    );
    const primary = [
      ...document.querySelectorAll(
        '.song-title,.nav-item,.btn,.field>label,.record-option>span,input:not([type=radio]),select,.venue-card>strong',
      ),
    ].filter(visible);
    const tiny = text
      .filter(
        (element) =>
          parseFloat(getComputedStyle(element).fontSize) <
          (element.matches('.completion-tile > .completion-id') ? 10 : 12),
      )
      .map(identify);
    const small = primary
      .filter((element) => parseFloat(getComputedStyle(element).fontSize) < 14)
      .map(identify);
    const inline = [...document.querySelectorAll('[style]')]
      .filter((element) => element.getAttribute('style').trim())
      .map((element) => ({ element: identify(element), html: element.outerHTML.slice(0, 400) }));
    const controls = [
      ...document.querySelectorAll('.btn,input:not([type=radio]):not([type=checkbox]),select'),
    ]
      .filter(visible)
      .map((element) => ({
        label: identify(element),
        height: (element.closest('.search') || element).getBoundingClientRect().height,
        minHeight: parseFloat(getComputedStyle(element).minHeight),
      }));
    const selects = [...document.querySelectorAll('select')].filter(visible).map((element) => {
      const css = getComputedStyle(element);
      return {
        label: element.getAttribute('aria-label') || element.id,
        appearance: css.appearance,
        paddingRight: parseFloat(css.paddingRight),
        backgroundImage: css.backgroundImage,
        backgroundPositionX: css.backgroundPositionX,
      };
    });
    const alignments = [];
    for (const selector of ['.catalog-panel-title', '.preview-label', '.record-confirm-actions'])
      for (const row of document.querySelectorAll(selector)) {
        const items = [...row.children]
          .filter(visible)
          .map((element) => ({ element, rect: element.getBoundingClientRect() }));
        if (
          items.length > 1 &&
          items.every(
            ({ rect }) =>
              Math.abs(rect.top - items[0].rect.top) < Math.max(rect.height, items[0].rect.height),
          )
        ) {
          const centers = items.map(({ rect }) => rect.top + rect.height / 2);
          alignments.push({ selector, delta: Math.max(...centers) - Math.min(...centers) });
        }
      }
    for (const row of document.querySelectorAll('.record-option')) {
      if (!visible(row)) continue;
      const radio = row.querySelector('input'),
        text = row.querySelector('span');
      if (!radio || !text) continue;
      const a = radio.getBoundingClientRect(),
        b = text.getBoundingClientRect();
      alignments.push({
        selector: '.record-option',
        delta: Math.abs(a.top + a.height / 2 - b.top - b.height / 2),
      });
    }
    return {
      bodyWidth: document.body.scrollWidth,
      documentWidth: document.documentElement.scrollWidth,
      viewport: innerWidth,
      tiny,
      small,
      inline,
      controls,
      alignments,
      selects,
    };
  });
  measurements.push({ label, ...measured });
  assert(
    measured.documentWidth <= measured.viewport + 1 && measured.bodyWidth <= measured.viewport + 1,
    `${label}: horizontal page overflow ${measured.documentWidth}/${measured.viewport}`,
  );
  assert.deepEqual(measured.inline, [], `${label}: UI contains inline style attributes`);
  assert.deepEqual(
    measured.tiny,
    [],
    `${label}: visible text below its minimum (10px chart IDs, 12px elsewhere)`,
  );
  assert.deepEqual(measured.small, [], `${label}: primary UI below 14px`);
  for (const select of measured.selects) {
    assert.equal(
      select.appearance,
      'none',
      `${label}: ${select.label} still uses an edge-aligned native arrow`,
    );
    assert.notEqual(
      select.backgroundImage,
      'none',
      `${label}: ${select.label} is missing its arrow`,
    );
    assert(
      select.paddingRight >= 36,
      `${label}: ${select.label} text has too little arrow clearance`,
    );
    assert.match(
      select.backgroundPositionX,
      /12px/,
      `${label}: ${select.label} arrow must be inset from the right edge`,
    );
  }
  for (const alignment of measured.alignments)
    assert(
      alignment.delta <= 2,
      `${label}: ${alignment.selector} vertical-center mismatch ${alignment.delta.toFixed(2)}px`,
    );
  for (const control of measured.controls)
    assert(
      control.height >= 36,
      `${label}: ${control.label} height ${control.height}px is too cramped`,
    );
  const singleLine = measured.controls.filter((control) => control.height <= 52);
  if (singleLine.length > 1)
    assert(
      Math.max(...singleLine.map((row) => row.height)) -
        Math.min(...singleLine.map((row) => row.height)) <=
        2,
      `${label}: inconsistent common control heights ${singleLine.map((row) => `${row.height}:${row.label}`).join(', ')}`,
    );
  await page.screenshot({
    path: path.join(output, `${label}.png`),
    fullPage: !label.includes('drawer') && !label.includes('modal'),
  });
  checks.push(
    `${label}: readable typography, consistent controls, centered rows, no inline styles or page overflow`,
  );
}
async function rangeRegression(page) {
  const lower = page.getByRole('slider', { name: '最低定数' }),
    upper = page.getByRole('slider', { name: '最高定数' });
  assert.equal(await lower.getAttribute('aria-valuenow'), '1');
  assert.equal(await upper.getAttribute('aria-valuenow'), '15');
  await lower.focus();
  await page.keyboard.press('ArrowRight');
  await waitFor(
    async () => (await lower.getAttribute('aria-valuenow')) === '1.1',
    'lower arrow increment',
  );
  assert.match(await page.locator('.range-tooltip').textContent(), /1\.1/);
  await page.keyboard.press('End');
  assert.equal(await lower.getAttribute('aria-valuenow'), '15');
  await page.keyboard.press('ArrowRight');
  assert.equal(await lower.getAttribute('aria-valuenow'), '15');
  await upper.focus();
  await page.keyboard.press('ArrowLeft');
  assert.equal(await upper.getAttribute('aria-valuenow'), '15');
  await lower.focus();
  await page.keyboard.press('Home');
  assert.equal(await lower.getAttribute('aria-valuenow'), '1');
  await upper.focus();
  await page.keyboard.press('Home');
  assert.equal(await upper.getAttribute('aria-valuenow'), '1');
  await page.keyboard.press('End');
  assert.equal(await upper.getAttribute('aria-valuenow'), '15');
  const rail = await page.locator('.range-rail').boundingBox();
  await page.mouse.move(rail.x + 1, rail.y + 24);
  await page.mouse.down();
  await page.mouse.move(rail.x + rail.width * 0.5, rail.y + 24, { steps: 8 });
  await page.locator('.range-tooltip').waitFor();
  const during = Number(await lower.getAttribute('aria-valuenow'));
  assert(during >= 7.8 && during <= 8.2, `drag should track near midpoint; got ${during}`);
  assert.match(
    await page.locator('.range-tooltip').textContent(),
    new RegExp(during.toFixed(1).replace('.', '\\.')),
  );
  await page.screenshot({ path: path.join(output, 'desktop-range-tooltip.png') });
  await page.mouse.up();
  await waitFor(
    () =>
      requests.some(
        (request) =>
          request.path === '/api/summary' && request.params.constant_min === during.toFixed(1),
      ),
    'filter updates summary',
  );
  const chartRequest = requests.findLast((request) => request.path === '/api/charts'),
    summaryRequest = requests.findLast((request) => request.path === '/api/summary');
  for (const name of [
    'constant_min',
    'constant_max',
    'difficulty',
    'version',
    'cn_version',
    'status',
    'q',
  ])
    assert.equal(
      chartRequest.params[name],
      summaryRequest.params[name],
      `chart/stat filters differ: ${name}`,
    );
  await waitFor(
    async () => !((await page.locator('.stats').getAttribute('aria-busy')) === 'true'),
    'summary settles',
  );
  const stats = await page.locator('.stat-value').allTextContents();
  const total = Number(stats[2].replace(/[^0-9]/g, '')),
    complete = Number(stats[0].replace(/[^0-9]/g, '')),
    remaining = Number(stats[1].replace(/[^0-9]/g, ''));
  assert.equal(complete + remaining, total);
  assert.equal(total, charts.filter((chart) => chart.constant >= during).length);
  await page.getByRole('button', { name: '清除筛选' }).click();
  await waitFor(
    async () =>
      (await lower.getAttribute('aria-valuenow')) === '1' &&
      (await upper.getAttribute('aria-valuenow')) === '15',
    'clear restores range',
  );
  checks.push(
    'SVG interval preserves accessible min/max/now, 0.1 steps, Home/End, no crossing, pointer tooltip, and identical chart/stat filters',
  );
}
async function sortControls(page, label) {
  const row = page.locator('.catalog-filters'),
    sort = page.getByRole('combobox', { name: '谱面排序' }),
    clear = page.getByRole('button', { name: '清除筛选', exact: true });
  assert.deepEqual(
    await sort.locator('option').evaluateAll((options) => options.map((option) => option.value)),
    Object.keys(sortOrders),
  );
  assert.equal(await sort.inputValue(), 'constant_desc');
  assert.equal(await clear.isDisabled(), true);
  assert.equal(
    await clear.evaluate(
      (button) =>
        button.classList.contains('btn') &&
        button === button.parentElement.lastElementChild &&
        button.parentElement.classList.contains('catalog-filters'),
    ),
    true,
    'Clear must be a button at the end of the shared filter toolbar',
  );
  assert.equal(await page.getByText('定数 ↓ · ID ↑', { exact: true }).count(), 0);
  assert.equal(
    await page.locator('.sort-row').count(),
    0,
    'Sorting must share the filter toolbar rather than occupy a separate row',
  );
  assert.deepEqual(
    await row
      .locator('select')
      .evaluateAll((elements) => elements.map((element) => element.getAttribute('aria-label'))),
    ['谱面难度', '登场版本', '谱面排序'],
  );
  const gap = await page.evaluate(
    () =>
      document.querySelector('.constant-range').getBoundingClientRect().top -
      document.querySelector('.catalog-filters').getBoundingClientRect().bottom,
  );
  assert(gap >= 12, `${label}: filter toolbar needs spacing below it: ${gap}`);
  const legacy = page.locator('.chart-table tbody tr').filter({ hasText: charts[1].title });
  assert.match(await legacy.locator('.badge').textContent(), /已完成/);
  assert.doesNotMatch(await legacy.locator('.badge').textContent(), /原表/);
  for (const chart of charts.slice(0, 12)) {
    const tr = page
      .locator('.chart-table tbody tr')
      .filter({ has: page.locator('.song-title', { hasText: chart.title }) });
    const venue = await tr.locator('td').nth(4).textContent();
    assert.equal(
      venue.trim(),
      chart.requires_venue ? chart.venue || '—' : chart.is_complete ? '不记录' : '—',
      `${label}: wrong venue display for ${chart.chart_key}`,
    );
  }
  if (label === 'desktop') {
    const initialStats = await page.locator('.stat-value').allTextContents();
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await waitFor(
      async () =>
        (await page.locator('.pager-buttons span').textContent()) === '2 / 2' &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
      'page two loaded',
    );
    for (const choice of [
      'achievement_desc',
      'achievement_asc',
      'ap_desc',
      'ap_asc',
      'constant_asc',
      'constant_desc',
    ]) {
      const marker = requests.length;
      await sort.selectOption(choice);
      await waitFor(
        async () =>
          requests
            .slice(marker)
            .some(
              (request) =>
                request.path === '/api/charts' &&
                request.params.sort === choice &&
                request.params.offset === '0',
            ) && (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
        'sort request and chart list settle',
      );
      assert.equal(
        await page.locator('.pager-buttons span').textContent(),
        '1 / 2',
        'Changing sort must reset pagination',
      );
      const expected = charts.find((chart) => chart.id === sortOrders[choice][0]);
      assert.equal(
        await page.locator('.chart-table .song-title').first().textContent(),
        expected.title,
        'Table must render the server ordering',
      );
      assert.deepEqual(
        await page.locator('.stat-value').allTextContents(),
        initialStats,
        'Sorting must not change statistics',
      );
      assert.equal(
        requests.findLast((request) => request.path === '/api/summary').params.sort,
        undefined,
        'Sort must not constrain summary',
      );
      assert.equal(await clear.isDisabled(), true, 'A sort selection is not an active filter');
    }
    await sort.selectOption('ap_asc');
    await page.getByRole('textbox', { name: '搜索谱面' }).fill('Sample');
    await page.getByRole('combobox', { name: '谱面难度' }).selectOption('3');
    await page.getByRole('combobox', { name: '登场版本' }).selectOption('UNiVERSE');
    await page.locator('.status-row').getByRole('button', { name: '已完成', exact: true }).click();
    const lower = page.getByRole('slider', { name: '最低定数' });
    await lower.focus();
    await page.keyboard.press('ArrowRight');
    await waitFor(async () => {
      const last = requests.findLast((request) => request.path === '/api/charts');
      return (
        last?.params.q === 'Sample' &&
        last.params.difficulty === '3' &&
        last.params.version === 'UNiVERSE' &&
        last.params.status === 'complete' &&
        last.params.constant_min === '1.1' &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false'
      );
    }, 'combined filters settle');
    const before = requests.findLast((request) => request.path === '/api/charts').params;
    const filteredStats = await page.locator('.stat-value').allTextContents();
    await sort.selectOption('achievement_asc');
    await waitFor(
      async () =>
        requests.findLast((request) => request.path === '/api/charts').params.sort ===
          'achievement_asc' && (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
      'sort with active filters',
    );
    const after = requests.findLast((request) => request.path === '/api/charts').params;
    for (const key of [
      'q',
      'difficulty',
      'version',
      'cn_version',
      'status',
      'constant_min',
      'constant_max',
    ])
      assert.equal(after[key], before[key], `Sorting should preserve ${key}`);
    assert.deepEqual(
      await page.locator('.stat-value').allTextContents(),
      filteredStats,
      'Identical filters must retain identical statistics across sort',
    );
    await clear.click();
    await waitFor(async () => {
      const last = requests.findLast((request) => request.path === '/api/charts');
      return (
        last?.params.sort === 'achievement_asc' &&
        last.params.q === '' &&
        last.params.status === 'all' &&
        !last.params.difficulty &&
        !last.params.version &&
        !last.params.cn_version &&
        !last.params.constant_min &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false'
      );
    }, 'clear all filters without resetting sort');
    assert.equal(await sort.inputValue(), 'achievement_asc');
    assert.equal(await clear.isDisabled(), true);
    assert.equal(await page.getByRole('textbox', { name: '搜索谱面' }).inputValue(), '');
    for (const name of ['谱面难度', '登场版本'])
      assert.equal(await page.getByRole('combobox', { name }).inputValue(), '');
    assert.equal(await lower.getAttribute('aria-valuenow'), '1');
    assert.equal(
      await page.getByRole('slider', { name: '最高定数' }).getAttribute('aria-valuenow'),
      '15',
    );
    assert.deepEqual(await page.locator('.stat-value').allTextContents(), initialStats);
    await sort.selectOption('constant_desc');
    await waitFor(
      async () =>
        requests.findLast((request) => request.path === '/api/charts').params.sort ===
          'constant_desc' && (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
      'restore initial sort',
    );
    checks.push(
      'Six sort options reach the API; server order is rendered; sorting resets to page one, preserves filters and statistics; clearing resets all filters while retaining sort',
    );
  }
  assert(await row.isVisible());
  checks.push(
    `${label}: sorting and clear button share the filter toolbar, legacy completion reads 已完成, only completed low-difficulty venues read 不记录`,
  );
}
async function regionalVersionControls(page, label) {
  const version = page.getByRole('combobox', { name: '登场版本' }),
    sort = page.getByRole('combobox', { name: '谱面排序' }),
    clear = page.getByRole('button', { name: '清除筛选', exact: true });
  assert.deepEqual(
    await version.locator('optgroup').evaluateAll((groups) =>
      groups.map((group) => ({
        label: group.label,
        values: [...group.children].map((option) => option.value),
      })),
    ),
    [
      { label: '日服', values: jpVersions.map((option) => option.value) },
      { label: '国服', values: cnVersions.map((option) => 'cn:' + option.value) },
    ],
    `${label}: version selector must preserve JP values and group CN options separately`,
  );
  const assertRequests = (cn, jp) => {
    const chartRequest = requests.findLast((request) => request.path === '/api/charts'),
      summaryRequest = requests.findLast((request) => request.path === '/api/summary');
    for (const request of [chartRequest, summaryRequest]) {
      assert.equal(
        request.params.cn_version,
        cn,
        `${label}: incorrect CN parameter on ${request.path}`,
      );
      assert.equal(
        request.params.version,
        jp,
        `${label}: incorrect JP parameter on ${request.path}`,
      );
      assert(
        !(request.params.cn_version && request.params.version),
        `${label}: regions cannot constrain each other`,
      );
    }
    for (const key of [
      'q',
      'difficulty',
      'version',
      'cn_version',
      'status',
      'constant_min',
      'constant_max',
    ])
      assert.equal(
        chartRequest.params[key],
        summaryRequest.params[key],
        `${label}: chart/summary filters differ: ${key}`,
      );
    assert.equal(chartRequest.params.offset, '0', `${label}: version changes must reset the page`);
    return chartRequest.params;
  };
  const choose = async (value) => {
    const start = requests.length,
      cn = value.startsWith('cn:') ? value.slice(3) : undefined,
      jp = value && !cn ? value : undefined;
    await version.selectOption(value);
    await waitFor(
      async () =>
        requests
          .slice(start)
          .some(
            (request) =>
              request.path === '/api/charts' &&
              request.params.cn_version === cn &&
              request.params.version === jp,
          ) && (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
      `${label}: regional version query settles`,
    );
    return assertRequests(cn, jp);
  };
  const assertStats = async (items) => {
    const counts = summary(items);
    await waitFor(async () => {
      const total = await page.locator('.stat-value').nth(2).textContent();
      return (
        Number(total.replace(/[^0-9]/g, '')) === counts.total &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false'
      );
    }, `${label}: regional totals render`);
    const stats = (await page.locator('.stat-value').allTextContents()).map((value) =>
      Number(value.replace(/[^0-9]/g, '')),
    );
    assert.deepEqual(
      stats,
      [counts.complete, counts.remaining, counts.total, counts.conflicts],
      `${label}: region statistics must match its fixture records`,
    );
    assert.equal(await page.locator('.chart-table tbody tr').count(), Math.min(12, items.length));
  };
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await waitFor(
    async () =>
      (await page.locator('.pager-buttons span').textContent()) === '2 / 2' &&
      (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
    `${label}: region pagination fixture`,
  );
  for (const option of cnVersions) {
    await choose('cn:' + option.value);
    await assertStats(charts.filter((chart) => chart.cn_version === option.value));
  }
  await page
    .locator('.catalog-filters')
    .evaluate((element) => element.scrollIntoView({ block: 'center' }));
  await audit(page, `${label}-cn-version`);
  await choose('FESTiVAL');
  await assertStats(charts.filter((chart) => chart.jp_release_version === 'FESTiVAL'));
  await choose('UNiVERSE');
  await assertStats(charts.filter((chart) => chart.jp_release_version === 'UNiVERSE'));
  await choose('cn:20000');
  await sort.selectOption('achievement_desc');
  await page.getByRole('textbox', { name: '搜索谱面' }).fill('Sample');
  await page.getByRole('combobox', { name: '谱面难度' }).selectOption('3');
  await page.locator('.status-row').getByRole('button', { name: '已完成', exact: true }).click();
  const lower = page.getByRole('slider', { name: '最低定数' });
  await lower.focus();
  await page.keyboard.press('ArrowRight');
  await waitFor(async () => {
    const request = requests.findLast((request) => request.path === '/api/charts');
    return (
      request?.params.q === 'Sample' &&
      request.params.difficulty === '3' &&
      request.params.status === 'complete' &&
      request.params.constant_min === '1.1' &&
      (await page.locator('.stats').getAttribute('aria-busy')) === 'false'
    );
  }, `${label}: combined CN filters settle`);
  const combined = assertRequests('20000', undefined);
  assert.equal(combined.sort, 'achievement_desc');
  await assertStats([charts[3]]);
  const switched = await choose('UNiVERSE');
  for (const key of ['q', 'difficulty', 'status', 'constant_min', 'constant_max', 'sort'])
    assert.equal(switched[key], combined[key], `${label}: changing region must preserve ${key}`);
  await assertStats([charts[3]]);
  const clearAndCheck = async () => {
    await clear.click();
    await waitFor(async () => {
      const request = requests.findLast((request) => request.path === '/api/charts');
      return (
        request?.params.q === '' &&
        request.params.status === 'all' &&
        !request.params.difficulty &&
        !request.params.version &&
        !request.params.cn_version &&
        !request.params.constant_min &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false'
      );
    }, `${label}: clear region and filters`);
    assertRequests(undefined, undefined);
    assert.equal(await version.inputValue(), '');
    assert.equal(await sort.inputValue(), 'achievement_desc');
    assert.equal(await clear.isDisabled(), true);
    await assertStats(charts);
  };
  await clearAndCheck();
  await choose('cn:25500');
  await clearAndCheck();
  await sort.selectOption('constant_desc');
  await waitFor(
    async () =>
      requests.findLast((request) => request.path === '/api/charts').params.sort ===
        'constant_desc' && (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
    `${label}: restore initial sort after region checks`,
  );
  checks.push(
    `${label}: all seven CN and both JP options work; region parameters are exclusive and shared by list/statistics; switching resets page while preserving other filters; clear removes either region and retains sorting`,
  );
}
async function completionControls(page, label) {
  const list = page.getByRole('button', { name: '列表', exact: true }),
    boardToggle = page.getByRole('button', { name: '完成表', exact: true });
  const board = page.locator('.completion-board'),
    tiles = page.locator('button.completion-tile'),
    sort = page.getByRole('combobox', { name: '谱面排序' });
  const version = page.getByRole('combobox', { name: '登场版本' }),
    clear = page.getByRole('button', { name: '清除筛选', exact: true });
  assert.equal(
    await list.getAttribute('aria-pressed'),
    'true',
    `${label}: default view should remain list`,
  );
  assert.equal(await boardToggle.getAttribute('aria-pressed'), 'false');
  assert.equal(await board.count(), 0, 'The all-record board should not be mounted in list mode');
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await waitFor(
    async () =>
      (await page.locator('.pager-buttons span').textContent()) === '2 / 2' &&
      (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
    `${label}: completion starts from a paginated list`,
  );
  await boardToggle.click();
  const settle = async (expected, labelSuffix = 'board settles') => {
    await waitFor(async () => {
      const request = requests.findLast((request) => request.path === '/api/completion-table'),
        selectedVersion = await version.inputValue();
      if (
        !request ||
        request.params.sort !== (await sort.inputValue()) ||
        request.params.cn_version !==
          (selectedVersion.startsWith('cn:') ? selectedVersion.slice(3) : undefined) ||
        request.params.version !==
          (selectedVersion && !selectedVersion.startsWith('cn:') ? selectedVersion : undefined) ||
        request.params.q !== (await page.getByRole('textbox', { name: '搜索谱面' }).inputValue())
      )
        return false;
      const items = ordered(new URLSearchParams(request.params));
      return (
        items.length === expected &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false' &&
        JSON.stringify(
          await tiles.evaluateAll((elements) =>
            elements.map((element) => element.dataset.chartKey),
          ),
        ) === JSON.stringify(items.map((chart) => chart.chart_key))
      );
    }, `${label}: ${labelSuffix}`);
    const request = requests.findLast((request) => request.path === '/api/completion-table');
    assert(request, `${label}: board must request its all-record endpoint`);
    assert(
      !request.params.offset && !request.params.limit,
      `${label}: board must not inherit list pagination`,
    );
    const items = ordered(new URLSearchParams(request.params)),
      counts = summary(items);
    assert.equal(items.length, expected);
    assert.deepEqual(
      (await page.locator('.stat-value').allTextContents()).map((value) =>
        Number(value.replace(/[^0-9]/g, '')),
      ),
      [counts.complete, counts.remaining, counts.total, counts.conflicts],
      `${label}: board and top statistics must describe the same records`,
    );
    assert.deepEqual(
      await tiles.evaluateAll((elements) => elements.map((element) => element.dataset.chartKey)),
      items.map((chart) => chart.chart_key),
      `${label}: board must preserve server order and one tile per chart`,
    );
    return request.params;
  };
  await settle(charts.length);
  assert.equal(await boardToggle.getAttribute('aria-pressed'), 'true');
  assert.equal(await list.getAttribute('aria-pressed'), 'false');
  assert.equal(
    await page.locator('.chart-table,.pager').count(),
    0,
    'Completion view should not retain list or pagination',
  );
  const constants = [...new Set(charts.map((chart) => chart.constant.toFixed(1)))];
  assert.deepEqual(
    await board
      .locator('.completion-group')
      .evaluateAll((elements) => elements.map((element) => element.dataset.constant)),
    constants,
    `${label}: descending constants should create ordered groups`,
  );
  const leading = board.locator('.completion-group').first();
  assert.equal(
    await leading.locator('.completion-tile').count(),
    2,
    'Equal constants should share one group',
  );
  assert.match(
    await leading.textContent(),
    /2\s*\/\s*2/,
    'Group heading should count imported completion together with confirmed AP',
  );
  assert.equal(
    await tiles.locator('.completion-caption,.type-label').count(),
    0,
    `${label}: compact tiles should omit difficulty/constant captions and format text badges`,
  );
  for (const chart of charts) {
    const actual = board.locator(`button.completion-tile[data-chart-key="${chart.chart_key}"]`);
    assert.equal(
      await actual.getAttribute('data-complete'),
      String(chart.is_complete),
      `${label}: wrong completion state for ${chart.chart_key}`,
    );
    assert.equal(
      await actual.getAttribute('data-status'),
      chart.status,
      `${label}: wrong AP distinction for ${chart.chart_key}`,
    );
    assert.equal(
      await actual.locator('img').first().getAttribute('loading'),
      'lazy',
      `${label}: jackets must load lazily`,
    );
    assert(
      (
        (await actual.getAttribute('aria-label')) ||
        (await actual.getAttribute('title')) ||
        ''
      ).includes(chart.title),
      `${label}: full song title should remain available on a tile`,
    );
    assert.equal(
      await actual.locator('.completion-id').count(),
      1,
      `${label}: each tile needs one ID label`,
    );
    assert.equal(
      (await actual.locator('.completion-id').textContent()).trim(),
      String(chart.id),
      `${label}: ID label must display the canonical chart ID without converting its format`,
    );
    const overlay = actual.locator('.completion-result'),
      shade = actual.locator('.completion-shade');
    assert.equal(
      await overlay.count(),
      chart.is_complete ? 1 : 0,
      `${label}: AP overlay must follow completion, including imported records`,
    );
    assert.equal(
      await shade.count(),
      chart.is_complete ? 1 : 0,
      `${label}: only completed jackets should have a dark shade`,
    );
    if (chart.is_complete) {
      const badge = chart.is_ap && chart.status === 'app' ? '4' : '3';
      assert.equal(
        await overlay.locator('image').getAttribute('href'),
        `/cover-assets/game-ui/game-badges-v2/nick/ComboStatus/${badge}.png`,
        `${label}: completion must reuse its AP or AP+ image resource`,
      );
      assert.equal(
        await overlay.locator('path').count(),
        0,
        `${label}: completed charts should use an AP image rather than a checkmark`,
      );
      assert.equal(
        await overlay.locator('svg').getAttribute('viewBox'),
        '4 4 71 71',
        `${label}: AP artwork should use the corrected optical crop`,
      );
      const layering = await actual.evaluate((tile) => {
        const shade = tile.querySelector('.completion-shade'),
          badge = tile.querySelector('.completion-result'),
          css = getComputedStyle(shade);
        return {
          before: !!(shade.compareDocumentPosition(badge) & Node.DOCUMENT_POSITION_FOLLOWING),
          shadeZ: Number(css.zIndex) || 0,
          badgeZ: Number(getComputedStyle(badge).zIndex) || 0,
          opacity: Number(css.opacity),
          color: css.backgroundColor,
        };
      });
      assert(
        layering.before && layering.shadeZ <= layering.badgeZ,
        `${label}: dark layer must remain behind the AP badge`,
      );
      assert.equal(
        layering.opacity,
        0.64,
        `${label}: completion shade should retain the stronger reference opacity`,
      );
      assert.equal(
        layering.color,
        'rgb(16, 20, 25)',
        `${label}: completion shade should be grey-black`,
      );
    }
  }
  const legacy = board.locator('[data-chart-key="101:1"]'),
    app = board.locator('[data-chart-key="102:2"]');
  assert.match(
    await legacy.getAttribute('aria-label'),
    /已完成/,
    `${label}: imported completion should retain its actual record status`,
  );
  assert.match(
    await app.getAttribute('aria-label'),
    /AP\+/,
    `${label}: 101% chart should retain AP+`,
  );
  const overlayPlacement = await legacy.evaluate((tile) => {
    const art = tile.querySelector('.completion-art').getBoundingClientRect(),
      overlay = tile.querySelector('.completion-result').getBoundingClientRect();
    return {
      size: overlay.width / art.width,
      x: Math.abs(overlay.x + overlay.width / 2 - art.x - art.width / 2),
      y: Math.abs(overlay.y + overlay.height / 2 - art.y - art.height / 2),
    };
  });
  assert(
    overlayPlacement.size > 0.49 &&
      overlayPlacement.size < 0.55 &&
      overlayPlacement.x <= 2 &&
      overlayPlacement.y <= 2,
    `${label}: AP image should be a smaller centered jacket overlay`,
  );
  const imageStyles = await board.evaluate((element) =>
    ['100:0', '104:4'].map((key) => {
      const tile = element.querySelector(`[data-chart-key="${key}"]`),
        image = tile.querySelector('img');
      return {
        filter: getComputedStyle(image).filter,
        opacity: getComputedStyle(image).opacity,
        tileFilter: getComputedStyle(tile).filter,
      };
    }),
  );
  for (const imageStyle of imageStyles)
    assert.deepEqual(
      imageStyle,
      { filter: 'none', opacity: '1', tileFilter: 'none' },
      `${label}: completed and unfinished jackets must retain original color`,
    );
  const compactStyles = await tiles.evaluateAll((elements) =>
    elements.map((tile) => {
      const css = getComputedStyle(tile),
        id = tile.querySelector('.completion-id'),
        idStyle = getComputedStyle(id),
        idBox = id.getBoundingClientRect(),
        tileBox = tile.getBoundingClientRect();
      return {
        difficulty: tile.dataset.difficulty,
        borders: ['Top', 'Right', 'Bottom', 'Left'].map((side) => ({
          color: css[`border${side}Color`],
          width: css[`border${side}Width`],
        })),
        idColor: idStyle.color,
        idBackground: idStyle.backgroundColor,
        idFontSize: idStyle.fontSize,
        idLineHeight: parseFloat(idStyle.lineHeight),
        idPadding: ['Top', 'Right', 'Bottom', 'Left'].map((side) => idStyle[`padding${side}`]),
        idRadii: ['TopLeft', 'TopRight', 'BottomRight', 'BottomLeft'].map(
          (corner) => idStyle[`border${corner}Radius`],
        ),
        idRight: tileBox.right - idBox.right,
        idBottom: tileBox.bottom - idBox.bottom,
        idJoins: ['::before', '::after'].map((pseudo) => {
          const style = getComputedStyle(id, pseudo);
          return {
            width: style.width,
            height: style.height,
            position: style.position,
            gradient: style.backgroundImage,
            left: parseFloat(style.left),
            top: parseFloat(style.top),
            right: parseFloat(style.right),
            bottom: parseFloat(style.bottom),
          };
        }),
      };
    }),
  );
  const borderColors = new Map();
  for (const value of compactStyles) {
    assert(
      value.borders.every(
        (border) => border.width === '4px' && border.color === value.borders[0].color,
      ),
      `${label}: difficulty color should frame all four sides of a tile`,
    );
    if (borderColors.has(value.difficulty))
      assert.equal(value.borders[0].color, borderColors.get(value.difficulty));
    else borderColors.set(value.difficulty, value.borders[0].color);
    assert.equal(
      value.idBackground,
      value.borders[0].color,
      `${label}: ID capsule should share its difficulty frame color`,
    );
    assert.equal(value.idFontSize, '10px');
    assert(Math.abs(value.idLineHeight - 12) < 0.1);
    assert.deepEqual(value.idPadding, ['1px', '4px', '1px', '4px']);
    assert.deepEqual(
      value.idRadii,
      ['4px', '0px', '6px', '0px'],
      `${label}: ID label should join the outer difficulty frame`,
    );
    assert(
      Math.abs(value.idRight) < 0.1 && Math.abs(value.idBottom) < 0.1,
      `${label}: ID label should sit flush with the bottom-right tile edge`,
    );
    for (const join of value.idJoins) {
      assert.equal(join.position, 'absolute');
      assert.equal(join.width, '4px');
      assert.equal(join.height, '4px');
      assert.match(
        join.gradient,
        /radial-gradient/,
        `${label}: ID-to-frame joints should use concave arcs`,
      );
    }
    assert(
      Math.abs(value.idJoins[0].left + 4) < 0.1 &&
        Math.abs(value.idJoins[0].bottom - 4) < 0.1 &&
        Math.abs(value.idJoins[1].right - 4) < 0.1 &&
        Math.abs(value.idJoins[1].top + 4) < 0.1,
      `${label}: concave joints should meet the inner edges of the 4px frame at the bottom-left and top-right ID corners`,
    );
    if (['1', '4'].includes(value.difficulty)) {
      const [r, g, b] = value.idColor.match(/\d+/g).slice(0, 3).map(Number);
      assert(
        0.2126 * r + 0.7152 * g + 0.0722 * b < 150,
        `${label}: ADV/Re:Master labels need dark text`,
      );
    } else
      assert.equal(
        value.idColor,
        'rgb(255, 255, 255)',
        `${label}: other difficulty ID labels should use white text`,
      );
  }
  assert.equal(
    new Set(borderColors.values()).size,
    5,
    `${label}: all five difficulty frames should have distinct colors`,
  );
  assert.equal(
    await board.locator('.completion-format-dot').count(),
    0,
    `${label}: old STD/DX dots should be removed from tiles and legend`,
  );
  assert.doesNotMatch(
    (await board.locator('.completion-legend').allTextContents()).join(' '),
    /STD|DX/,
    `${label}: old format legend should be removed`,
  );
  await audit(page, `${label}-completion`);
  await legacy.click();
  await page.getByRole('dialog', { name: '谱面记录' }).waitFor();
  assert.equal(
    await page.locator('.detail-song h2').textContent(),
    charts[1].title,
    'Board tile should open the existing chart detail',
  );
  assert.equal(
    requests.findLast((request) => request.path.startsWith('/api/charts/')).path,
    '/api/charts/101%3A1',
    'Detail must fetch the chart key including difficulty',
  );
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await sort.selectOption('constant_asc');
  await settle(charts.length, 'ascending constant groups');
  assert.deepEqual(
    await board
      .locator('.completion-group')
      .evaluateAll((elements) => elements.map((element) => element.dataset.constant)),
    [...constants].reverse(),
  );
  await sort.selectOption('achievement_desc');
  await settle(charts.length, 'achievement order is continuous');
  assert.equal(
    await board.locator('.completion-group-heading').count(),
    0,
    'Non-constant sorting should not create constant headings',
  );
  assert.equal(
    await board.locator('.completion-grid').count(),
    1,
    'Non-constant sorting should use one uninterrupted grid',
  );
  const density = await tiles.evaluateAll((elements) => {
    const boxes = elements.map((element) => element.getBoundingClientRect());
    return {
      row: boxes.filter((box) => Math.abs(box.top - boxes[0].top) < 1).length,
      width: boxes[0].width,
    };
  });
  assert(
    label === 'mobile' ? density.row === 5 : density.row >= 10,
    `${label}: completion grid should be compact, got ${density.row} tiles per row`,
  );
  assert(density.width >= 44, `${label}: tiles must retain touchable size`);
  await audit(page, `${label}-completion-continuous`);
  if (label === 'mobile') {
    await page.setViewportSize({ width: 360, height: 844 });
    const narrow = await tiles.evaluateAll((elements) => {
      const boxes = elements.map((element) => element.getBoundingClientRect());
      return {
        row: boxes.filter((box) => Math.abs(box.top - boxes[0].top) < 1).length,
        width: boxes[0].width,
      };
    });
    assert.equal(narrow.row, 4, 'Narrow phones should use four tile columns');
    assert(narrow.width >= 44, 'Narrow phone tiles must retain touchable size');
    await audit(page, 'mobile-narrow-completion');
    await page.setViewportSize({ width: 390, height: 844 });
  }
  await version.selectOption('cn:20000');
  await settle(7, 'CN completion scope');
  await page.getByRole('textbox', { name: '搜索谱面' }).fill('Sample');
  await page.getByRole('combobox', { name: '谱面难度' }).selectOption('3');
  await page.locator('.status-row').getByRole('button', { name: '已完成', exact: true }).click();
  const lower = page.getByRole('slider', { name: '最低定数' });
  await lower.focus();
  await page.keyboard.press('ArrowRight');
  await waitFor(() => {
    const request = requests.findLast((request) => request.path === '/api/completion-table');
    return (
      request?.params.q === 'Sample' &&
      request.params.difficulty === '3' &&
      request.params.status === 'complete' &&
      request.params.constant_min === '1.1'
    );
  }, `${label}: board combines all filters`);
  const combined = await settle(1);
  assert.equal(combined.cn_version, '20000');
  assert.equal(combined.version, undefined);
  assert.equal(combined.sort, 'achievement_desc');
  await version.selectOption('UNiVERSE');
  await settle(1, 'JP completion scope');
  const jp = requests.findLast((request) => request.path === '/api/completion-table').params;
  assert.equal(jp.version, 'UNiVERSE');
  assert.equal(jp.cn_version, undefined);
  for (const key of ['q', 'difficulty', 'status', 'constant_min', 'constant_max', 'sort'])
    assert.equal(jp[key], combined[key]);
  if (label === 'desktop') {
    const lateScripts = [];
    const unavailableAssets = async (route) => {
      const url = new URL(route.request().url());
      if (url.origin === origin && url.pathname.endsWith('.js')) {
        lateScripts.push(url.pathname);
        await route.fulfill({
          status: 404,
          contentType: 'text/javascript',
          body: 'Asset removed by a subsequent deployment',
        });
      } else await route.fallback();
    };
    await page.route('**/assets/**', unavailableAssets);
    try {
      const downloadPromise = page.waitForEvent('download');
      await page.getByRole('button', { name: '导出图片', exact: true }).click();
      const download = await downloadPromise;
      assert(
        download.url().startsWith(`blob:${origin}/`),
        'Completion download must use the fetched PNG Blob',
      );
      assert.match(download.suggestedFilename(), /^AP完成表-\d{12}\.png$/);
      const file = await download.path(),
        bytes = await fs.readFile(file);
      assert.equal(
        bytes.subarray(0, 8).toString('hex'),
        '89504e470d0a1a0a',
        'Completion export should directly download an actual PNG',
      );
      assert.equal(
        bytes.readUInt32BE(16),
        3200,
        'Completion PNG keeps its fixed full-resolution width',
      );
      assert.equal(
        bytes.readUInt32BE(20),
        exportRequests[0].height,
        'Downloaded Blob retains the full uploaded strip height',
      );
      assert.deepEqual(
        bytes,
        exportImages.get('/api/completion-exports/ui-audit-export/image').image,
        'Downloaded Blob retains the actual canvas PNG bytes',
      );
      await waitFor(
        () => exportRequests.some((request) => request.method === 'DELETE'),
        'temporary export cleanup',
      );
      assert.deepEqual(
        exportRequests.map(({ method, path }) => `${method} ${path}`),
        [
          'POST /api/completion-exports',
          'PUT /api/completion-exports/ui-audit-export/parts/0',
          'POST /api/completion-exports/ui-audit-export/finish',
          'GET /api/completion-exports/ui-audit-export/image',
          'DELETE /api/completion-exports/ui-audit-export',
        ],
        'Export should create, upload, finish, download, and delete only its temporary image job',
      );
      assert.deepEqual(
        lateScripts,
        [],
        'An already open completion table must export without fetching code chunks removed by a deployment',
      );
      await waitFor(
        async () => (await page.locator('#toast').textContent()) === '已开始下载 PNG',
        'completion PNG download notice',
      );
      await download.saveAs(path.join(output, 'completion-export.png'));
    } finally {
      await page.unroute('**/assets/**', unavailableAssets);
    }
    checks.push(
      'Completion export uploads its actual 3200px PNG strip to isolated temporary-image APIs, fetches the finished HTTP image, and downloads byte-identical PNG through a Blob URL; timestamped filename and notice work without later JavaScript chunks; no personal-data writes',
    );
  }
  await clear.click();
  await settle(charts.length, 'clear completion filters');
  assert.equal(await boardToggle.getAttribute('aria-pressed'), 'true');
  assert.equal(await sort.inputValue(), 'achievement_desc');
  assert.equal(await version.inputValue(), '');
  assert.equal(await clear.isDisabled(), true);
  await version.selectOption('cn:21000');
  await settle(0, 'empty completion scope');
  assert.equal(
    await page.getByRole('button', { name: '导出图片', exact: true }).isDisabled(),
    true,
    'Empty completion view should not export',
  );
  await clear.click();
  await settle(charts.length);
  await list.click();
  await waitFor(
    async () =>
      (await page.locator('.chart-table tbody tr').count()) === 12 &&
      (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
    `${label}: return to paginated list`,
  );
  assert.equal(
    await page.locator('.pager-buttons span').textContent(),
    '1 / 2',
    'Returning from board resets pagination',
  );
  assert.equal(await sort.inputValue(), 'achievement_desc');
  await sort.selectOption('constant_desc');
  await waitFor(
    async () =>
      requests.findLast((request) => request.path === '/api/charts').params.sort ===
        'constant_desc' && (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
    `${label}: restore list ordering`,
  );
  checks.push(
    `${label}: compact completion grid with difficulty frames and canonical-ID capsules, no old format dots or captions, original-color jackets with completed-only dark masks and centered AP/AP+ images, preserved chart states, grouping/order, details, filters/statistics, and list pagination`,
  );
}
let activePage;
try {
  for (const [name, viewport] of [
    ['desktop', { width: 1440, height: 1000 }],
    ['mobile', { width: 390, height: 844 }],
  ]) {
    const context = await browser.newContext({ viewport });
    await mock(context);
    const page = await context.newPage();
    activePage = page;
    page.on('pageerror', (error) => errors.push(`${name}: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(`${name}: ${message.text()}`);
    });
    await page.goto(origin + '/#ap', { waitUntil: 'networkidle' });
    await page.locator('.chart-table tbody tr').first().waitFor();
    // Preferences intentionally initialize an explicit range; exercise clearing before the unfiltered baseline.
    await page.getByRole('button', { name: '清除筛选', exact: true }).click();
    await waitFor(
      async () =>
        !requests.findLast((request) => request.path === '/api/charts')?.params.constant_min &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
      'clear initial preference range',
    );
    await badges(page.locator('.chart-table'), `${name}-AP list`);
    await sortControls(page, name);
    await regionalVersionControls(page, name);
    await completionControls(page, name);
    await audit(page, `${name}-ap`);
    if (name === 'mobile') {
      await page
        .locator('.catalog-filters')
        .evaluate((element) => element.scrollIntoView({ block: 'center' }));
      await page.screenshot({ path: path.join(output, 'mobile-sort-controls.png') });
    }
    if (name === 'desktop') {
      await rangeRegression(page);
      await page.getByRole('combobox', { name: '谱面难度' }).selectOption('3');
      await page.getByRole('combobox', { name: '登场版本' }).selectOption('UNiVERSE');
      await waitFor(
        () =>
          requests.some(
            (request) =>
              request.path === '/api/summary' &&
              request.params.difficulty === '3' &&
              request.params.version === 'UNiVERSE',
          ),
        'difficulty and version filters',
      );
      await waitFor(
        async () =>
          (await page.locator('.chart-table tbody tr').count()) === 3 &&
          (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
        'filtered charts settle',
      );
      assert.equal(
        Number((await page.locator('.stat-value').nth(2).textContent()).replace(/[^0-9]/g, '')),
        3,
      );
      await page.getByRole('button', { name: '清除筛选' }).click();
      await waitFor(
        async () => (await page.locator('.chart-table tbody tr').count()) === 12,
        'reset filters restores all charts',
      );
      checks.push(
        'Difficulty and JP-version filters still constrain both chart rows and summary; reset restores all charts',
      );
    }
    await page.locator('.chart-table tbody tr').first().click();
    await page.getByRole('dialog', { name: '谱面记录' }).waitFor();
    await badges(page.locator('.detail-song'), `${name}-detail`);
    await audit(page, `${name}-detail-drawer`);
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByRole('button', { name: '记录 AP', exact: true }).click();
    await page.locator('.pick-song').first().waitFor();
    await badges(page.locator('.search-results'), `${name}-picker`);
    await audit(page, `${name}-picker-drawer`);
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0);
    await page.goto(origin + '/#cover', { waitUntil: 'networkidle' });
    await page.getByRole('heading', { name: '封面工坊' }).waitFor();
    await page.getByRole('button', { name: '选择谱面', exact: true }).click();
    await page.locator('.pick-song').first().click();
    await page.locator('#cover-score').fill('100.9876');
    await waitFor(
      async () =>
        (await page.locator('canvas').evaluate((canvas) => {
          const pixel = canvas.getContext('2d').getImageData(0, 0, 1, 1).data;
          return pixel[0] !== 247 || pixel[1] !== 243 || pixel[2] !== 248;
        })) &&
        (await page.locator('.canvas-frame').getAttribute('aria-busy')) === 'false' &&
        (await page.getByRole('button', { name: '导出封面', exact: true }).isEnabled()),
      'cover render',
    );
    assert.equal(await page.locator('.form-message').count(), 0);
    assert.equal(
      await page.locator('.cover-preview-tools,.cover-segment').count(),
      0,
      'Preview must not duplicate export aspect controls',
    );
    const aspect = page.getByRole('combobox', { name: '导出比例' });
    assert.deepEqual(
      await aspect
        .locator('option')
        .evaluateAll((options) => options.map((option) => option.value)),
      ['16:9', '4:3'],
    );
    for (const [value, width] of [
      ['4:3', 1440],
      ['16:9', 1920],
    ]) {
      await aspect.selectOption(value);
      await waitFor(
        async () =>
          (await page.locator('canvas').getAttribute('width')) === String(width) &&
          (await page.locator('.canvas-frame').getAttribute('aria-busy')) === 'false',
        'export aspect changes canvas',
      );
    }
    checks.push(
      `${name}: duplicate preview aspect tabs removed; export select still renders both 16:9 and 4:3`,
    );
    await badges(page.locator('.cover-song'), `${name}-cover selection`);
    await audit(page, `${name}-cover`);
    await page.goto(origin + '/#venues', { waitUntil: 'networkidle' });
    await page.locator('.venue-card').first().waitFor();
    await audit(page, `${name}-venues`);
    await page.goto(origin + '/#settings', { waitUntil: 'networkidle' });
    await page.locator('.record-review-entry').first().waitFor();
    await badges(page.locator('.record-review-list'), `${name}-pending review`);
    if (name === 'desktop') await retryControls(page);
    await audit(page, `${name}-settings`);
    if (name === 'mobile') {
      const columns = await page
        .locator('.record-review-entry')
        .first()
        .locator('thead th')
        .evaluateAll((elements) =>
          elements.map((element) => ({
            label: element.textContent.trim(),
            left: element.getBoundingClientRect().left,
            right: element.getBoundingClientRect().right,
          })),
        );
      assert.deepEqual(
        columns.map((column) => column.label),
        ['字段', '落雪', '水鱼'],
      );
      for (const column of columns)
        assert(
          column.left >= 0 && column.right <= viewport.width,
          `mobile comparison column ${column.label} requires hidden horizontal scrolling`,
        );
      checks.push('Mobile two-source comparison shows both choices without horizontal scrolling');
    }
    assert.equal(
      await page
        .locator('.record-batch-actions')
        .evaluate((element) => getComputedStyle(element).position),
      'static',
      'confirmation bar must remain in document flow',
    );
    await page.getByText('查看变化', { exact: true }).click();
    await page.locator('.catalog-change-group').first().waitFor();
    await badges(page.locator('.catalog-change-group'), `${name}-catalog changes`);
    await audit(page, `${name}-settings-expanded`);
    await page.getByRole('button', { name: '人工记录', exact: true }).click();
    await page.locator('.record-saved-table').waitFor();
    await badges(page.locator('.record-review-list'), `${name}-saved review`);
    assert.equal(
      await page.getByRole('button', { name: '撤销这张的裁决', exact: true }).count(),
      0,
    );
    assert.equal(
      await page
        .locator('.record-saved-table')
        .getByRole('button', { name: '撤销', exact: true })
        .count(),
      2,
      'Each chosen field must retain its own revoke action',
    );
    await audit(page, `${name}-settings-manual`);
    await page.getByRole('button', { name: '待入库', exact: true }).click();
    await page.locator('.record-review-entry input').first().waitFor();
    for (const entry of await page.locator('.record-review-entry').all())
      await entry.locator('input[type=radio]').first().check();
    assert.equal(await page.getByRole('button', { name: '确认选择', exact: true }).count(), 1);
    await page.getByRole('button', { name: '确认选择', exact: true }).click();
    await page.getByRole('dialog', { name: '确认这些选择' }).waitFor();
    await badges(page.locator('.record-confirm-list'), `${name}-review modal`);
    await audit(page, `${name}-review-modal`);
    const dialog = await page.getByRole('dialog').boundingBox();
    assert(
      dialog.x >= 0 &&
        dialog.y >= 0 &&
        dialog.x + dialog.width <= viewport.width + 1 &&
        dialog.y + dialog.height <= viewport.height + 1,
      `${name}: confirmation modal out of viewport`,
    );
    await page.getByRole('button', { name: '返回修改', exact: true }).click();
    assert.equal(await page.locator('.record-review-entry input:checked').count(), 3);
    checks.push(
      `${name}: single in-flow confirmation; summary modal fits viewport; cancel preserves all three chart choices and sends no request`,
    );
    checks.push(
      `${name}: shared difficulty and STD/DX badges retain identical appearance in AP list, detail, picker, cover selection and review; only per-field revoke remains`,
    );
    await context.close();
    activePage = null;
  }
  for (const width of [900, 1024]) {
    const context = await browser.newContext({ viewport: { width, height: 1000 } });
    await mock(context);
    const page = await context.newPage();
    activePage = page;
    page.on('pageerror', (error) => errors.push(`tablet-${width}: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(`tablet-${width}: ${message.text()}`);
    });
    await page.goto(origin + '/#ap', { waitUntil: 'networkidle' });
    await page.locator('.chart-table tbody tr').first().waitFor();
    await audit(page, `tablet-${width}-ap`);
    const constant = page
      .locator('.chart-table thead')
      .getByRole('columnheader', { name: '定数', exact: true });
    assert.equal(await constant.isVisible(), true, `${width}px tablet must retain constant column`);
    const box = await constant.boundingBox();
    assert(
      box.x >= 0 && box.x + box.width <= width,
      `${width}px tablet constant column must be visible without horizontal scrolling`,
    );
    checks.push(`tablet-${width}: constant column remains visible`);
    await context.close();
    activePage = null;
  }
  {
    const completionItems = Array.from({ length: 360 }, (_, index) => ({
      ...charts[index % charts.length],
      id: 1000 + index,
      song_id: 1000 + index,
      chart_key: `${1000 + index}:${index % 5}`,
      constant: 14.9,
    }));
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await mock(context, { completionItems });
    const page = await context.newPage();
    activePage = page;
    page.on('pageerror', (error) => errors.push(`completion-batches: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(`completion-batches: ${message.text()}`);
    });
    await page.goto(origin + '/#ap', { waitUntil: 'networkidle' });
    await page.locator('.chart-table tbody tr').first().waitFor();
    await page.getByRole('button', { name: '完成表', exact: true }).click();
    await waitFor(
      async () =>
        (await page.locator('.completion-tile').count()) > 0 &&
        (await page.locator('.stats').getAttribute('aria-busy')) === 'false',
      'large completion fixture mounts first batch',
    );
    const initial = await page.locator('.completion-tile').count();
    assert(
      initial >= 120 && initial < completionItems.length,
      `Completion should progressively mount, initial count ${initial}`,
    );
    assert.equal(
      Number((await page.locator('.stat-value').nth(2).textContent()).replace(/[^0-9]/g, '')),
      360,
      'All-record statistics must not shrink to the mounted slice',
    );
    const group = page.locator('.completion-group').first();
    assert.match(
      await group.locator('.completion-group-heading').textContent(),
      new RegExp(`${counts(completionItems).complete}\\s*\\/\\s*360`),
      'Group totals should include not-yet-mounted tiles',
    );
    const more = page.locator('.completion-more');
    while (true) {
      // Read count and scroll in one browser task: the final observer batch can remove the sentinel between awaited locator calls.
      const before = await page.evaluate((total) => {
        const count = document.querySelectorAll('.completion-tile').length;
        if (count >= total) return count;
        const sentinel = document.querySelector('.completion-more');
        if (!sentinel)
          throw new Error(
            `Missing completion sentinel with only ${count} / ${total} tiles mounted`,
          );
        sentinel.scrollIntoView({ block: 'center' });
        return count;
      }, completionItems.length);
      if (before >= completionItems.length) break;
      await waitFor(
        async () => (await page.locator('.completion-tile').count()) > before,
        'scroll mounts another completion batch',
      );
    }
    assert.deepEqual(
      await page
        .locator('.completion-tile')
        .evaluateAll((elements) => elements.map((element) => element.dataset.chartKey)),
      completionItems.map((chart) => chart.chart_key),
      'Progressive mounting must not repeat or skip a chart',
    );
    assert.equal(await more.count(), 0, 'Sentinel disappears after the final batch');
    checks.push(
      'Large 360-chart fixture mounts progressively with lazy jackets; totals include unloaded tiles; scrolling eventually reveals every unique chart without duplicates',
    );
    await context.close();
    activePage = null;
  }
  assert.deepEqual(mutations, [], 'Audit must not submit choices or personal data');
  assert.deepEqual(errors, [], 'Browser errors');
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify(
      {
        passed: true,
        mocked: true,
        bundle,
        createdAt: new Date().toISOString(),
        checks,
        measurements,
        exportRequests,
        mutations,
        errors,
      },
      null,
      2,
    ),
  );
  await fs.rm(path.join(output, 'failure.png'), { force: true });
  console.log(
    `UI audit passed (${checks.length} checks, desktop and mobile, all four routes; temporary-image export only, no personal-data mutations)`,
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
        mocked: true,
        bundle,
        createdAt: new Date().toISOString(),
        checks,
        measurements,
        exportRequests,
        mutations,
        errors,
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
