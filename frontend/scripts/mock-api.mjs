// Shared harness for the browser checks: one static server for dist/, one in-memory API
// (small synthetic dataset, no private data or network) and a page factory that blocks
// every external request. Sections override single routes with `app.api.on(...)`.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { fixtureAsset, useSystemCoverFont } from './fixtures.mjs';

const dist = path.resolve(process.env.UI_DIST || path.join(import.meta.dirname, '../dist'));
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };
const tile = '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"/>';
export const MASTER_KEY = 'correct-master-key';
const names = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'];

function chart(id, difficulty, title, constant, extra = {}) {
  return {
    id,
    song_id: id,
    difficulty,
    difficulty_index: difficulty,
    chart_key: `${id}:${difficulty}`,
    title,
    display_title: '',
    artist: 'Fixture Artist',
    type: id % 2 ? 'dx' : 'standard',
    difficulty_name: names[difficulty],
    level: `${Math.floor(constant)}${constant % 1 >= 0.6 ? '+' : ''}`,
    constant,
    jacket_url: `/jackets/${id}.png`,
    jp_release_version: 'UNiVERSE',
    cn_version: '25500',
    requires_venue: false,
    venue: '',
    provider_achievement: null,
    provider_fc: null,
    pending_record: null,
    record_revision: 0,
    is_ap: false,
    is_complete: false,
    status: 'incomplete',
    conflict: false,
    ...extra,
  };
}
const ap = (venue = '') => ({
  is_ap: true,
  is_complete: true,
  status: 'ap',
  provider_achievement: 100.6,
  provider_fc: 'ap',
  requires_venue: Boolean(venue),
  venue,
});
const location = (latitude, longitude) => ({ latitude, longitude, coordinate_system: 'WGS84' });
const field = (key, label, a, b) => ({
  field_key: key,
  label,
  required: true,
  options: [
    { option_id: `${key}-lxns`, source: 'lxns', value: a },
    { option_id: `${key}-divingfish`, source: 'divingfish', value: b },
  ],
});

function dataset() {
  return {
    auth: { required: false, sessions: new Set() },
    charts: [
      chart(1, 3, 'Alpha Rhythm', 14.6, { cn_version: '20000', ...ap('【上海】星光机厅') }),
      chart(2, 3, 'Bravo Beat', 14.2, { cn_version: '20000', ...ap(), status: 'app' }),
      chart(3, 3, 'Charlie Step', 13.9, { cn_version: '23000', requires_venue: true }),
      chart(4, 2, 'Delta Drive', 13.9, { cn_version: '23000', provider_achievement: 99.5 }),
      chart(5, 4, 'Echo Lights', 13.5, {
        jp_release_version: 'FESTiVAL',
        is_complete: true,
        status: 'legacy',
      }),
      ...Array.from({ length: 9 }, (_, k) =>
        chart(6 + k, 3, `Fixture Song ${6 + k}`, Number((13.4 - k * 0.1).toFixed(1))),
      ),
      chart(15, 4, 'Golf Groove', 13.0, { cn_version: '23000', ...ap('【北京】测试机厅') }),
    ],
    preferences: { default_constant_min: 12, default_constant_max: 15 },
    divingfish: { configured: false, last_sync: null },
    job: null,
    syncOutcome: 'succeeded',
    venues: [
      { name: '【上海】星光机厅', city: '上海', location: location(31.22, 121.46) },
      { name: '【北京】测试机厅', city: '北京', location: location(39.9, 116.4) },
      { name: '【广州】新机厅', city: '广州', location: null },
    ],
    catalog: {
      revision: 'r1',
      decision_revision: 0,
      applied_decision_revision: 0,
      decisions_pending: false,
      snapshot_at: '2026-11-01T01:00:00Z',
      main: { status: 'succeeded', stage: '', message: '曲库已更新', warnings: [] },
      remy: { status: 'complete', pending_count: 0, verified_count: 12, updated_count: 0 },
    },
    review: [0, 1].map((difficulty) => ({
      id: 383,
      difficulty,
      chart_key: `383:${difficulty}`,
      title: 'Link',
      type: 'standard',
      difficulty_name: names[difficulty],
      status: 'pending',
      fields: [field('title', '曲名', 'Link', 'Link(CoF)'), field('constant', '定数', 4, 4.1)],
    })),
    decisions: [],
    bilibili: {
      connected: false,
      account: { mid: 1, name: 'Fixture user' },
      videos: [{ bvid: 'BV1iWMh6iECe', title: 'Delta Drive old title', cover: '/jackets/4.png' }],
      collections: [{ id: 3, title: 'AP collection', sections: [{ id: 8, title: '13.9' }] }],
      stale: [],
      plans: new Map(),
      jobs: [],
    },
  };
}

const counts = (items) => {
  const complete = items.filter((row) => row.is_complete).length;
  const ap = items.filter((row) => row.is_ap).length;
  return { total: items.length, complete, ap, remaining: items.length - complete, conflicts: 0 };
};
const summary = (items) => ({
  ...counts(items),
  legacy_complete: items.filter((row) => row.status === 'legacy').length,
  levels: [],
  constants: [...new Set(items.map((row) => row.constant))].map((constant) => ({
    constant,
    ...counts(items.filter((row) => row.constant === constant)),
  })),
  sync: null,
});
const matrixCounts = (items) => {
  const { total, complete, remaining } = counts(items);
  return { total, complete, remaining };
};
const cnVersions = [
  { value: '20000', label: 'DX', title: '舞萌DX' },
  { value: '23000', label: 'DX2023', title: '舞萌DX 2023' },
  { value: '25500', label: 'DX2026', title: '舞萌DX 2026' },
];
const orderItem = (aid, bvid, song, position) => ({
  episode_id: aid,
  aid,
  bvid,
  title: `【maimai】${song} AP`,
  song_title: song,
  chart_key: `${aid}:3`,
  version: 'UNiVERSE',
  position,
});

function filterCharts(charts, q) {
  const has = (key) => q.get(key) !== null && q.get(key) !== '';
  const status = q.get('status') || 'all';
  const items = charts.filter(
    (row) =>
      (!has('q') || `${row.id} ${row.title}`.toLowerCase().includes(q.get('q').toLowerCase())) &&
      (!has('difficulty') || row.difficulty === Number(q.get('difficulty'))) &&
      (!has('version') || row.jp_release_version === q.get('version')) &&
      (!has('cn_version') || row.cn_version === q.get('cn_version')) &&
      (!has('cn_version_min') || row.cn_version >= q.get('cn_version_min')) &&
      (!has('cn_version_max') || row.cn_version <= q.get('cn_version_max')) &&
      (!has('constant_min') || row.constant >= Number(q.get('constant_min'))) &&
      (!has('constant_max') || row.constant <= Number(q.get('constant_max'))) &&
      (status === 'all' ||
        (status === 'complete' && row.is_complete) ||
        (status === 'incomplete' && !row.is_complete) ||
        (status === 'conflict' && row.conflict)),
  );
  const direction = q.get('sort') === 'constant_asc' ? 1 : -1;
  return items.sort((a, b) => direction * (a.constant - b.constant) || a.id - b.id);
}

class Reply {
  constructor(status, json) {
    Object.assign(this, { status, json });
  }
}
export const reply = (status, json) => new Reply(status, json);

function routes(s) {
  const find = (key) => s.charts.find((row) => row.chart_key === key);
  const venueCharts = (name) => s.charts.filter((row) => row.is_ap && row.venue === name);
  const recordChange = (row, expected, pending) => {
    if (!row) return reply(404, { detail: '谱面不存在' });
    if (Number(expected) !== row.record_revision)
      return reply(409, {
        detail: { code: 'record_conflict', message: '记录已在其他页面更新，请重新载入后再保存。' },
      });
    row.record_revision += 1;
    row.pending_record = pending && {
      id: `pending-${row.chart_key}`,
      venue: pending.venue,
      created_at: '2026-11-20T10:00:00+08:00',
      updated_at: '2026-11-20T10:00:00+08:00',
      revision: row.record_revision,
      reason: '待同步核实',
    };
    return { chart: row };
  };
  const catalogChanged = (patch) => {
    const c = s.catalog;
    s.catalog = { ...c, revision: 'r' + (Number(c.revision.slice(1)) + 1), ...patch };
    return s.catalog;
  };
  const job = (id, state, extra = {}) => ({
    job_id: id,
    state,
    steps: { metadata: 'verified', collection: 'verified', order: 'verified' },
    error: null,
    ...extra,
  });
  return {
    'GET /api/auth/session': ({ authenticated }) => ({
      authenticated: authenticated || !s.auth.required,
      auth_required: s.auth.required,
    }),
    'POST /api/auth/login': ({ body, setCookie }) => {
      if (body.master_key === 'limited') return reply(429, { detail: 'Too many attempts' });
      if (body.master_key !== MASTER_KEY) return reply(401, { detail: 'Invalid key' });
      const id = crypto.randomUUID();
      s.auth.sessions.add(id);
      setCookie(`session=${id}; HttpOnly; SameSite=Strict; Path=/`);
      return { authenticated: true, auth_required: s.auth.required };
    },
    'POST /api/auth/logout': ({ cookie, setCookie }) => {
      s.auth.sessions.delete(cookie);
      setCookie('session=; Max-Age=0; Path=/');
      return { authenticated: false, auth_required: s.auth.required };
    },
    'GET /api/version': () => ({ token: crypto.randomUUID() }),
    'GET /api/settings/preferences': () => s.preferences,
    'PUT /api/settings/preferences': ({ body }) => (s.preferences = body),
    'GET /api/settings/divingfish': () => ({ configured: s.divingfish.configured }),
    'PUT /api/settings/divingfish': ({ body }) => {
      s.divingfish.configured = Boolean(body.token);
      return { configured: s.divingfish.configured };
    },
    'GET /api/sync/status': () => {
      // A running job finishes on its second status read.
      if (s.job?.state === 'running' && ++s.job.reads > 1) {
        s.job = { ...s.job, state: s.syncOutcome, message: '' };
        if (s.syncOutcome === 'failed') s.job.error = { code: 'x', message: '水鱼暂时不可用' };
        else
          s.divingfish.last_sync = {
            matched: 15,
            confirmed: 1,
            pending: 0,
            synced_at: '2026-11-20T10:00:00+08:00',
          };
      }
      return { job: s.job && { ...s.job, reads: undefined }, divingfish: s.divingfish };
    },
    'POST /api/sync/divingfish': () => {
      if (!s.divingfish.configured) return reply(400, { detail: '请先保存水鱼 Token' });
      s.job = {
        id: crypto.randomUUID(),
        state: 'running',
        message: '正在读取水鱼成绩',
        error: null,
        reads: 0,
      };
      return { ...s.job, reads: undefined };
    },
    'GET /api/chart-filters': () => ({
      constants: [1, ...new Set(s.charts.map((row) => row.constant).sort((a, b) => a - b)), 15],
      versions: [
        { value: 'UNiVERSE', label: 'UNiVERSE' },
        { value: 'FESTiVAL', label: 'FESTiVAL' },
      ],
      cn_versions: cnVersions.map(({ value, label }) => ({ value, label })),
      cn_matrix_versions: cnVersions,
    }),
    'GET /api/charts': ({ query }) => {
      const items = filterCharts(s.charts, query);
      const offset = Number(query.get('offset') || 0),
        limit = Number(query.get('limit') || 12);
      return { items: items.slice(offset, offset + limit), total: items.length, offset, limit };
    },
    'GET /api/summary': ({ query }) => summary(filterCharts(s.charts, query)),
    'GET /api/completion-table': ({ query }) => {
      const items = filterCharts(s.charts, query);
      return {
        items,
        total: items.length,
        summary: summary(items),
        generated_at: '2026-11-20T10:00:00+08:00',
      };
    },
    'GET /api/completion-matrix': ({ query }) => {
      const items = filterCharts(s.charts, query);
      const rows = [...new Set(items.map((row) => row.constant))].map((constant) => {
        const same = items.filter((row) => row.constant === constant);
        const cells = Object.fromEntries(
          cnVersions.map(({ value }) => [
            value,
            matrixCounts(same.filter((row) => row.cn_version === value)),
          ]),
        );
        return { constant, cells, total: matrixCounts(same) };
      });
      const totals = Object.fromEntries(
        cnVersions.map(({ value }) => [
          value,
          matrixCounts(items.filter((row) => row.cn_version === value)),
        ]),
      );
      return { versions: cnVersions, rows, totals, summary: matrixCounts(items) };
    },
    'GET /api/charts/:key': ({ params }) =>
      find(params.key) || reply(404, { detail: '谱面不存在' }),
    'PUT /api/charts/:key/ap-record': ({ params, body }) =>
      recordChange(find(params.key), body.expected_revision, body),
    'DELETE /api/charts/:key/ap-record': ({ params, query }) =>
      recordChange(find(params.key), query.get('expected_revision'), null),
    'GET /api/ap-records': () => {
      const items = s.charts.filter((row) => row.pending_record);
      return { items, count: items.length };
    },
    'GET /api/venues': () => ({
      items: s.venues.map(({ name }) => ({ name, count: venueCharts(name).length })),
    }),
    'GET /api/venues/map': () => ({
      items: s.venues.map((venue) => ({
        ...venue,
        total: venueCharts(venue.name).length,
        pending_count: s.charts.filter((row) => row.pending_record?.venue === venue.name).length,
        jackets: venueCharts(venue.name).slice(0, 3),
      })),
    }),
    'GET /api/venues/charts': ({ query }) => {
      const items = venueCharts(query.get('name'));
      return { items, total: items.length, offset: 0, limit: 48 };
    },
    'GET /api/venues/summary': () => {
      const items = s.venues.map(({ name, city }) => {
        const rows = venueCharts(name);
        const levels = {};
        for (const row of rows) levels[row.level] = (levels[row.level] || 0) + 1;
        return { name, city, counts: levels, total: rows.length, pending_count: 0 };
      });
      return {
        levels: ['13', '13+', '14', '14+'].map((value) => ({ value, label: value })),
        items,
        totals: {},
        total: items.reduce((sum, row) => sum + row.total, 0),
        pending_count: 0,
      };
    },
    'GET /api/venues/search': () => ({
      items: [
        { place_name: '搜索结果广场', address: '测试路 8 号', latitude: 31.24, longitude: 121.49 },
      ],
    }),
    'PUT /api/venues/location': ({ body }) => {
      const saved = location(body.latitude, body.longitude);
      const venue = s.venues.find((item) => item.name === body.name);
      if (venue) venue.location = saved;
      else s.venues.push({ name: body.name, city: '测试', location: saved });
      return { name: body.name, location: saved };
    },
    'GET /api/catalog/update': () => s.catalog,
    'POST /api/catalog/update': () => {
      s.catalog = {
        ...s.catalog,
        main: { status: 'running', stage: 'fetch', message: '正在更新曲库', warnings: [] },
      };
      // The next status read publishes the new catalog revision.
      s.catalogRunning = true;
      return s.catalog;
    },
    'GET /api/catalog/update/report': () => ({
      diff: {
        added_songs: [{ song_id: 16, title: 'Hotel New' }],
        added_charts: [16, 16].map((id, difficulty) => ({
          song_id: id,
          title: 'Hotel New',
          chart_key: `${id}:${difficulty + 2}`,
          type: 'dx',
          difficulty: difficulty + 2,
          constant: 12.5 + difficulty,
        })),
        changed_charts: [
          {
            song_id: 4,
            title: 'Delta Drive',
            chart_key: '4:2',
            type: 'standard',
            difficulty: 2,
            changes: { level_value: { before: 13.8, after: 13.9 } },
          },
        ],
      },
      issues: [],
      sources: {},
    }),
    'GET /api/catalog/records/review': () => ({
      schema_version: 2,
      revision: s.catalog.revision,
      decision_revision: s.catalog.decision_revision,
      decisions_pending: false,
      counts: { total: 100, admitted: 100 - s.review.length, pending: s.review.length },
      records: s.review,
    }),
    'GET /api/catalog/records/decisions': () => ({
      revision: s.catalog.revision,
      decision_revision: s.catalog.decision_revision,
      decisions_pending: false,
      records: s.decisions,
    }),
    'POST /api/catalog/records/decisions/batch': ({ body }) => {
      if (
        body.base_catalog_revision !== s.catalog.revision ||
        body.base_decision_revision !== s.catalog.decision_revision
      )
        return reply(409, { detail: '曲库已变化，请刷新后重新选择。' });
      for (const item of body.records) {
        const record = s.review.find((row) => row.chart_key === `${item.id}:${item.difficulty}`);
        assert(record, 'Submitted chart must still be pending');
        s.review = s.review.filter((row) => row !== record);
        s.decisions.push({
          ...record,
          status: 'manual',
          pending: record,
          fields: item.choices.map(({ field_key, option_id }) => {
            const options = record.fields.find((row) => row.field_key === field_key).options;
            const option = options.find((row) => row.option_id === option_id);
            return { field_key, label: field_key, value: option.value, source: option.source };
          }),
        });
      }
      return catalogChanged({ decision_revision: s.catalog.decision_revision + 1 });
    },
    'POST /api/catalog/records/decisions/revoke': ({ body }) => {
      const saved = s.decisions.find(
        (row) => row.id === body.id && row.difficulty === body.difficulty,
      );
      s.decisions = s.decisions.filter((row) => row !== saved);
      s.review.push(saved.pending);
      return catalogChanged({ decision_revision: s.catalog.decision_revision + 1 });
    },
    'GET /api/bilibili/status': () => ({
      connected: s.bilibili.connected,
      account: s.bilibili.connected ? s.bilibili.account : null,
    }),
    'POST /api/bilibili/login': () => ({
      login_id: 'login-1',
      url: 'https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=fixture',
      expires_at: '2030-01-01T00:00:00Z',
    }),
    'GET /api/bilibili/login/:id': () => {
      s.bilibili.connected = true;
      return { state: 'connected', account: s.bilibili.account };
    },
    'DELETE /api/bilibili/session': () => {
      s.bilibili.connected = false;
      return { ok: true };
    },
    'GET /api/bilibili/videos': () => ({
      items: s.bilibili.videos.map((video) => ({ combo: null, ...video })),
      page: 1,
      has_more: false,
      collections: s.bilibili.collections,
      unavailable_collections: [],
    }),
    'GET /api/bilibili/videos/:bvid': ({ params }) => {
      const video = s.bilibili.videos.find((item) => item.bvid === params.bvid);
      return video
        ? { combo: null, mirror_horizontal: false, ...video }
        : reply(404, { detail: '找不到稿件' });
    },
    'GET /api/bilibili/collections/order': () => ({
      checked_at: '2026-11-20T10:00:00+08:00',
      sections: [
        {
          season_id: 3,
          season_title: 'AP collection',
          section_id: 8,
          section_title: '13.9',
          status: 'changes',
          revision: 'order-1',
          current: [
            orderItem(103, 'BV1BBB111111', 'Later', 1),
            orderItem(4, 'BV1iWMh6iECe', 'Delta Drive', 2),
            orderItem(101, 'BV1AAA111111', 'Earlier', 3),
          ],
          desired: [
            orderItem(101, 'BV1AAA111111', 'Earlier', 1),
            orderItem(4, 'BV1iWMh6iECe', 'Delta Drive', 2),
            orderItem(103, 'BV1BBB111111', 'Later', 3),
          ],
          unmatched: [],
        },
      ],
    }),
    'GET /api/bilibili/stale': () => ({
      connected: s.bilibili.connected,
      items: s.bilibili.stale,
      collections: s.bilibili.collections,
    }),
    'POST /api/bilibili/prepare': ({ body }) => {
      const row = find(body.chart_key);
      const desired = [
        orderItem(101, 'BV1AAA111111', 'Earlier', 1),
        orderItem(4, body.bvid, row.title, 2),
        orderItem(103, 'BV1BBB111111', 'Later', 3),
      ];
      const plan = {
        ...body,
        plan_id: `plan-${s.bilibili.plans.size + 1}`,
        old_title: `${row.title} old title`,
        title: `【maimai】${row.title} ${row.difficulty_name}${body.constant} ${body.combo === 'app' ? 'AP+' : 'AP'}`,
        chart: row,
        order: {
          season_id: 3,
          season_title: 'AP collection',
          section_id: body.section_id,
          section_title: '13.9',
          status: 'changes',
          revision: 'order-1',
          current: [desired[2], desired[1], desired[0]],
          desired,
          unmatched: [],
          target_bvid: body.bvid,
          target_position: 2,
        },
      };
      s.bilibili.plans.set(plan.plan_id, plan);
      return plan;
    },
    'POST /api/bilibili/apply': ({ body }) => {
      const plan = s.bilibili.plans.get(body.plan_id);
      assert(plan, 'Apply must reference a prepared plan');
      for (const [key, width] of [
        ['cover16', 1920],
        ['cover43', 1440],
      ]) {
        const png = Buffer.from(body[key].split(',')[1], 'base64');
        assert.equal(png.readUInt32BE(16), width, `${key} width`);
        assert.equal(png.readUInt32BE(20), 1080, `${key} height`);
      }
      // `applyOutcome: 'uncertain'` simulates a lost result that must be retried server-side.
      const uncertain = s.bilibili.applyOutcome === 'uncertain';
      const result = job(
        `job-${s.bilibili.jobs.length + 1}`,
        uncertain ? 'uncertain' : 'complete',
        {
          bvid: plan.bvid,
          plan,
          covers: body,
          ...(uncertain && { error: '模拟网络中断' }),
        },
      );
      if (uncertain) result.steps.order = 'submitted';
      s.bilibili.jobs.unshift(result);
      return result;
    },
    'GET /api/bilibili/jobs': () => ({ items: s.bilibili.jobs }),
    'GET /api/bilibili/jobs/:id': ({ params }) =>
      s.bilibili.jobs.find((item) => item.job_id === params.id),
    'POST /api/bilibili/jobs/:id/retry': ({ params }) => {
      const index = s.bilibili.jobs.findIndex((item) => item.job_id === params.id);
      s.bilibili.jobs[index] = { ...s.bilibili.jobs[index], ...job(params.id, 'complete') };
      return s.bilibili.jobs[index];
    },
  };
}

function createApi() {
  const state = dataset();
  const table = Object.entries(routes(state)).map(([key, handler]) => {
    const [method, pattern] = key.split(' ');
    const regex = new RegExp('^' + pattern.replace(/:(\w+)/g, '(?<$1>[^/]+)') + '$');
    return { method, regex, handler };
  });
  const overrides = [];
  const api = {
    state,
    calls: [],
    errors: [],
    /** Override one route; returning undefined falls through to the default handler. */
    on(key, handler, { once = false } = {}) {
      overrides.unshift({ key, handler, once });
    },
    last: (pathname) => api.calls.findLast((call) => call.path === pathname),
    count: (pathname, method = 'GET') =>
      api.calls.filter((call) => call.path === pathname && call.method === method).length,
    async handle(request, response) {
      const url = new URL(request.url, 'http://localhost');
      const pathname = decodeURIComponent(url.pathname);
      let raw = '';
      for await (const chunk of request) raw += chunk;
      const body = raw ? JSON.parse(raw) : null;
      const cookie = /(?:^|;\s*)session=([^;]+)/.exec(request.headers.cookie || '')?.[1];
      const authenticated = state.auth.sessions.has(cookie);
      const call = { method: request.method, path: pathname, query: url.searchParams, body };
      api.calls.push(call);
      if (state.catalogRunning && pathname === '/api/catalog/update' && request.method === 'GET') {
        state.catalogRunning = false;
        state.catalog = {
          ...state.catalog,
          revision: 'r' + (Number(state.catalog.revision.slice(1)) + 1),
          main: {
            status: 'succeeded',
            stage: '',
            message: '曲库已更新',
            diff: { songs_added: 1, charts_added: 2, constants_changed: 1 },
            warnings: [],
          },
        };
      }
      const ctx = {
        ...call,
        cookie,
        authenticated,
        setCookie: (value) => response.setHeader('Set-Cookie', value),
      };
      let result;
      if (state.auth.required && !authenticated && !pathname.startsWith('/api/auth/'))
        result = reply(401, { detail: 'Expired' });
      for (const override of overrides) {
        if (result !== undefined) break;
        if (override.key !== `${request.method} ${pathname}`) continue;
        result = await override.handler(ctx);
        if (override.once) overrides.splice(overrides.indexOf(override), 1);
      }
      for (const route of table) {
        if (result !== undefined) break;
        const match = route.method === request.method && route.regex.exec(pathname);
        if (match) result = await route.handler({ ...ctx, params: match.groups || {} });
      }
      if (result === undefined) {
        api.errors.push(`Unexpected API: ${request.method} ${pathname}`);
        result = reply(500, { detail: 'Unmocked API' });
      }
      const { status, json } = result instanceof Reply ? result : { status: 200, json: result };
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify(json));
    },
  };
  return api;
}

async function startApp(browser) {
  const api = createApi();
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
      if (pathname.startsWith('/api/')) return await api.handle(request, response);
      const asset = fixtureAsset(pathname);
      if (asset) {
        response.writeHead(200, { 'Content-Type': asset.contentType });
        return response.end(asset.body);
      }
      const file = path.resolve(dist, pathname === '/' ? 'index.html' : pathname.slice(1));
      assert(file.startsWith(dist + path.sep), 'Path escapes dist');
      const bytes = await fs.readFile(file);
      response.writeHead(200, {
        'Content-Type': mime[path.extname(file)] || 'application/octet-stream',
      });
      response.end(bytes);
    } catch (error) {
      if (!response.headersSent) response.writeHead(error.code === 'ENOENT' ? 404 : 500);
      response.end();
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const contexts = [];
  return {
    api,
    origin,
    /** Open `target` (path + hash) in a fresh, isolated browser context. */
    async open(target = '/', { width = 1280, height = 900, mobile = false } = {}) {
      const context = await browser.newContext({
        viewport: { width, height },
        ...(mobile ? { isMobile: true, hasTouch: true, deviceScaleFactor: 2 } : {}),
      });
      contexts.push(context);
      await useSystemCoverFont(context);
      await context.route('**/*', async (route) => {
        const url = new URL(route.request().url());
        if (url.origin === origin || url.protocol === 'data:') return route.fallback();
        if (url.hostname === 'tile.openstreetmap.org')
          return route.fulfill({ contentType: 'image/svg+xml', body: tile });
        api.errors.push(`External request blocked: ${url.href}`);
        return route.abort();
      });
      const page = await context.newPage();
      page.on('pageerror', (error) => api.errors.push(`Page error: ${error.message}`));
      await page.goto(origin + target);
      return page;
    },
    async close() {
      await Promise.all(contexts.map((context) => context.close()));
      await new Promise((resolve) => server.close(resolve));
    },
  };
}

export const toast = (page, text) => page.locator('#toast').filter({ hasText: text }).waitFor();

export async function until(predicate, label, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`Timed out: ${label}`);
}

export const noHorizontalOverflow = (page) =>
  page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1);

/** Run `sections` (name → async (app) => void); CLI: [--webkit] [section ...]. */
export async function runSections(sections) {
  const args = process.argv.slice(2);
  if (args[0] === '--webkit') process.env.PLAYWRIGHT_BROWSER = args.shift().slice(2);
  const selected = args.length ? args : Object.keys(sections);
  for (const name of selected) assert(sections[name], `Unknown section: ${name}`);
  const browser = await launchBrowser();
  const engine = process.env.PLAYWRIGHT_BROWSER || 'chromium';
  try {
    for (const name of selected) {
      const app = await startApp(browser);
      try {
        await sections[name](app);
        assert.deepEqual(app.api.errors, [], 'page errors, unmocked APIs or external requests');
      } catch (error) {
        error.message = `[${engine}:${name}] ${error.message}`;
        throw error;
      } finally {
        await app.close();
      }
      console.log(`${engine} ${name}: passed`);
    }
  } finally {
    await browser.close();
  }
}
