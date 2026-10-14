import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { badgePNG, fixtureAsset, useSystemCoverFont } from './fixtures.mjs';
import { idleScoreSync } from './score-sync-fixture.mjs';
import { verifyPublishingLoginLifecycle } from './publishing-login-fixture.mjs';
import {
  capturePublishingMirrorInputs,
  verifyPublishingMirrors,
} from './publishing-mirror-fixture.mjs';

const root = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(root, 'frontend/dist'));
const output = path.resolve(
  process.env.PUBLISHING_AUDIT_OUTPUT || path.join(root, 'output/publishing-audit'),
);
await fs.mkdir(output, { recursive: true });
const checks = [];
const errors = [];
const bvid = 'BV1iWMh6iECe';
const plusBvid = 'BV1PLUS11111';
const ordinaryBvid = 'BV1PLAIN1111';
const titleFixtures = new Map([
  [plusBvid, '【maimai】Our Wrenally Expert13.0 AP＋'],
  [ordinaryBvid, '【maimai】Our Wrenally Expert13.0 AP'],
  ['BV1MUSIC1111', '【maimai】AP+ Expert13.0'],
  ['BV1bJouBSEiH', '+PA 0.31trepxE yllanerW ruO【iamiam】'],
  ['BV1OTHER1111', '+PA 0.31trepxE yllanerW ruO【iamiam】'],
]);
const appBadge =
  '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><circle cx="40" cy="40" r="35" fill="#0cfa69"/><path d="M20 40h40M40 20v40" stroke="white" stroke-width="7"/></svg>';
const chart = {
  id: 9200,
  song_id: 9200,
  chart_key: '9200:2',
  title: 'Our Wrenally',
  display_title: '',
  type: 'standard',
  difficulty: 2,
  difficulty_index: 2,
  difficulty_name: 'Expert',
  constant: 13,
  level: '13',
  jacket_url: '/jackets/9200.png',
  jp_release_version: 'UNiVERSE',
  jp_release_provider: 'arcade_songs',
  release_status: 'verified',
  provider_achievement: 100.9999,
  provider_fc: 'app',
  achievement: 100.9999,
};
const orderItem = (aid, bvid, song_title, version, position) => ({
  episode_id: aid,
  aid,
  bvid,
  title: `【maimai】${song_title} Expert13.0 AP`,
  song_title,
  chart_key: `${aid}:2`,
  version,
  position,
});
const previousVideo = orderItem(101, 'BV1AAA111111', 'Earlier song', 'PiNK', 1);
const targetVideo = orderItem(102, bvid, chart.title, 'UNiVERSE', 2);
const nextVideo = orderItem(103, 'BV1BBB111111', 'Later song', 'FESTiVAL', 3);
const desiredOrder = {
  season_id: 3,
  season_title: 'AP collection',
  section_id: 8,
  section_title: '13.0',
  status: 'changes',
  revision: 'order-fixture',
  current: [{ ...nextVideo, position: 1 }, targetVideo, { ...previousVideo, position: 3 }],
  desired: [previousVideo, targetVideo, nextVideo],
  unmatched: [],
  target_bvid: bvid,
  target_position: 2,
};
const unresolvedVideo = {
  aid: 104,
  bvid: 'BV1CCC111111',
  title: 'Unidentified song',
  reason: '无法确定日服版本',
};
const counts = { total: 1, complete: 1, ap: 1, remaining: 0, conflicts: 0, legacy_complete: 0 };
const server = http.createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const file = path.resolve(bundle, pathname === '/' ? 'index.html' : pathname.slice(1));
    assert(file.startsWith(bundle + path.sep));
    response.setHeader(
      'Content-Type',
      { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[path.extname(file)] ||
        'application/octet-stream',
    );
    response.end(await fs.readFile(file));
  } catch {
    response.writeHead(404);
    response.end();
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await launchBrowser();
try {
  for (const width of [390, 1280]) {
    const loginLifecycle = await verifyPublishingLoginLifecycle(browser, origin, width, errors);
    const context = await browser.newContext({
      viewport: { width, height: 900 },
      isMobile: width < 600,
      hasTouch: width < 600,
    });
    await useSystemCoverFont(context);
    let connected = false;
    let failLogo = true;
    let planCount = 0;
    let ambiguous = false;
    let retryCount = 0;
    let latestPlan = null;
    let persistedJob = null;
    let blockedOrder = false;
    let orderChecks = 0;
    let expectedAchievement = '100.5000';
    const requests = [];
    const prepared = [];
    const applied = [];
    await context.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      const asset = fixtureAsset(url.pathname);
      if (!asset) return route.continue();
      if (url.pathname.endsWith('/logo.svg') && failLogo)
        return route.fulfill({ status: 503, body: '' });
      if (url.pathname === '/cover-assets/game-ui/manifest.json') {
        return route.fulfill({
          json: {
            assets: Object.fromEntries(
              ['dx', 'standard', 'ap', 'app'].map((key) => [
                key,
                { file: key === 'app' ? 'app.svg' : 'badge.png' },
              ]),
            ),
            number_sets: {
              achievement: {
                file: 'badge.png',
                line_height: 80,
                glyphs: Object.fromEntries(
                  [...'0123456789.%'].map((digit) => [
                    digit,
                    { x: 0, y: 0, width: 80, height: 80, advance: 80 },
                  ]),
                ),
              },
            },
          },
        });
      }
      if (url.pathname.endsWith('/badge.png'))
        return route.fulfill({ contentType: 'image/png', body: badgePNG });
      if (url.pathname.endsWith('/app.svg'))
        return route.fulfill({ contentType: 'image/svg+xml', body: appBadge });
      return route.fulfill(asset);
    });
    await context.route('**/api/**', async (route) => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      requests.push({ pathname, method: request.method() });
      const send = (json, status = 200) => route.fulfill({ json, status });
      if (pathname === '/api/bilibili/status')
        return send({ connected, account: connected ? { mid: 1, name: 'Fixture user' } : null });
      if (pathname === '/api/bilibili/login')
        return send({
          login_id: 'opaque-test',
          url: 'https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=fixture-only',
          expires_at: '2030-01-01T00:00:00Z',
        });
      if (pathname === '/api/bilibili/login/opaque-test') {
        connected = true;
        return send({ state: 'connected', account: { mid: 1, name: 'Fixture user' } });
      }
      if (pathname === '/api/bilibili/videos')
        return send({
          items: [{ bvid, title: 'Our Wrenally old title', cover: '/jackets/9200.png' }],
          page: 1,
          has_more: false,
        });
      if (pathname === `/api/bilibili/videos/${bvid}`)
        return send({
          bvid,
          title: 'Our Wrenally old title',
          revision: '1',
          mirror_horizontal: false,
        });
      const extraBvid = pathname.match(/^\/api\/bilibili\/videos\/(BV[0-9A-Za-z]{10})$/)?.[1];
      if (titleFixtures.has(extraBvid))
        return send({
          bvid: extraBvid,
          title: titleFixtures.get(extraBvid),
          revision: '1',
          mirror_horizontal: extraBvid === 'BV1bJouBSEiH',
        });
      if (pathname === '/api/bilibili/collections') {
        if (ambiguous) await new Promise((resolve) => setTimeout(resolve, 900));
        return send({
          items: [
            {
              id: 3,
              title: 'AP collection',
              sections: [
                { id: 8, title: '13.0' },
                ...(ambiguous ? [{ id: 9, title: '13.0' }] : []),
              ],
            },
          ],
        });
      }
      if (pathname === '/api/bilibili/collections/order') {
        assert.equal(request.method(), 'GET');
        orderChecks += 1;
        return send({
          checked_at: '2026-10-14T06:00:00+08:00',
          sections: [
            desiredOrder,
            {
              ...desiredOrder,
              section_id: 9,
              section_title: '13.1',
              status: 'ordered',
              current: desiredOrder.desired,
            },
            {
              ...desiredOrder,
              section_id: 10,
              section_title: '13.2',
              status: 'blocked',
              unmatched: [unresolvedVideo],
            },
          ],
        });
      }
      if (pathname === '/api/bilibili/jobs')
        return send({ items: persistedJob ? [persistedJob] : [] });
      if (pathname === '/api/bilibili/jobs/job-1')
        return send({
          ...persistedJob,
          plan: latestPlan,
          covers: { cover16: applied[0].cover16, cover43: applied[0].cover43 },
        });
      if (pathname === '/api/bilibili/jobs/job-1/retry') {
        assert.equal(request.method(), 'POST');
        assert.equal(request.postData(), null, 'Retry must use the server stored PNGs');
        retryCount += 1;
        persistedJob = {
          ...persistedJob,
          state: 'complete',
          steps: { metadata: 'verified', collection: 'verified', order: 'verified' },
          error: null,
        };
        return send(persistedJob);
      }
      if (pathname === '/api/bilibili/prepare') {
        const input = request.postDataJSON();
        assert.equal(input.chart_key, chart.chart_key);
        assert.equal(input.achievement, expectedAchievement);
        assert.equal(input.section_id, 8);
        prepared.push(input);
        latestPlan = {
          ...input,
          plan_id: `plan-${++planCount}`,
          old_title: titleFixtures.get(input.bvid) || 'Our Wrenally old title',
          title: `【maimai】Our Wrenally Expert13.0 ${input.combo === 'app' ? 'AP+' : 'AP'}`,
          chart,
          expires_at: '2030-01-01T00:00:00Z',
          order: blockedOrder
            ? {
                ...desiredOrder,
                status: 'blocked',
                target_position: null,
                unmatched: [unresolvedVideo],
              }
            : {
                ...desiredOrder,
                target_bvid: input.bvid,
                desired: [previousVideo, { ...targetVideo, bvid: input.bvid }, nextVideo],
              },
        };
        return send(latestPlan);
      }
      if (pathname === '/api/bilibili/apply') {
        const input = request.postDataJSON();
        applied.push(input);
        for (const [key, expectedWidth] of [
          ['cover16', 1920],
          ['cover43', 1440],
        ]) {
          assert(input[key].startsWith('data:image/png;base64,'));
          const png = Buffer.from(input[key].split(',')[1], 'base64');
          assert.equal(png.readUInt32BE(16), expectedWidth);
          assert.equal(png.readUInt32BE(20), 1080);
        }
        persistedJob = {
          job_id: 'job-1',
          bvid,
          state: 'uncertain',
          steps: { metadata: 'verified', collection: 'verified', order: 'submitted' },
          error: '模拟网络中断',
        };
        return send(persistedJob);
      }
      const fixtures = {
        '/api/auth/session': { authenticated: true, auth_required: false },
        '/api/settings/divingfish': { configured: false, source: null, sync: null },
        '/api/settings/preferences': { default_constant_min: 13, default_constant_max: 13.9 },
        '/api/sync/status': idleScoreSync,
        '/api/catalog/update': {
          revision: 'fixture',
          main: { status: 'succeeded', warnings: [], diff: {} },
          remy: { status: 'complete', pending_count: 0, verified_count: 0, updated_count: 0 },
        },
        '/api/chart-filters': { constants: [13], versions: [], cn_versions: [] },
        '/api/venues': { items: [] },
        '/api/ap-records': { items: [], count: 0 },
        '/api/charts': { items: [chart], total: 1, limit: 20, offset: 0 },
        '/api/summary': { ...counts, levels: [], constants: [], sync: null },
      };
      assert(fixtures[pathname], `Unexpected API: ${pathname}`);
      return send(fixtures[pathname]);
    });
    const page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.message));
    await page.goto(origin + '/#publishing');
    await page.getByRole('button', { name: '生成封面', exact: true }).waitFor();
    assert.equal(
      await page.getByRole('navigation', { name: '主导航' }).getByRole('link').count(),
      4,
    );
    assert.equal(
      await page.getByRole('link', { name: '投稿整理', exact: true }).getAttribute('aria-current'),
      'page',
    );
    assert.equal(
      requests.filter((item) => item.method === 'POST').length,
      0,
      'Opening the cover tool must be available before Bilibili connection',
    );
    await page.getByRole('button', { name: '扫码连接' }).click();
    const qr = page.getByAltText('Bilibili 登录二维码');
    await qr.waitFor();
    assert(
      (await qr.getAttribute('src')).startsWith('data:image/png;base64,'),
      'QR must be generated locally',
    );
    await page.getByRole('button', { name: '检查合集顺序', exact: true }).waitFor();
    assert.equal(orderChecks, 0, 'Collection ordering must only be checked on demand');
    const postCount = requests.filter((item) => item.method === 'POST').length;
    await page.getByRole('button', { name: '检查合集顺序', exact: true }).click();
    await page.getByText('顺序有差异 · 3 条', { exact: true }).waitFor();
    await page.getByText('顺序一致 · 3 条', { exact: true }).waitFor();
    await page.getByText('无法确定顺序 · 3 条', { exact: true }).waitFor();
    assert.equal(orderChecks, 1);
    assert.equal(
      requests.filter((item) => item.method === 'POST').length,
      postCount,
      'Checking order must never POST',
    );
    assert.equal(
      await page.getByLabel('13.0 顺序差异').getByText('3 → 1', { exact: true }).count(),
      1,
    );
    assert.equal(
      await page.getByLabel('13.0 顺序差异').getByText('PiNK', { exact: true }).count(),
      1,
    );
    assert.equal(
      await page.getByLabel('13.0 顺序差异').getByText('1 → 3', { exact: true }).count(),
      1,
    );
    await page.getByRole('button', { name: /Our Wrenally old title/ }).click();
    await page.getByLabel('谱面', { exact: true }).fill('Our Wrenally');
    await page.getByRole('button', { name: /Our Wrenally STD/ }).click();
    assert.equal(
      await page.getByLabel('录像达成率').inputValue(),
      '',
      'Do not substitute latest PB for video achievement',
    );
    assert.equal(await page.getByLabel('完成标记').inputValue(), 'ap');
    await page.getByLabel('录像达成率').fill('100.5000');
    assert.equal(
      await page.getByLabel('合集', { exact: true }).inputValue(),
      '8',
      'Unique matching constant section is selected',
    );
    blockedOrder = true;
    await page.getByRole('button', { name: '生成预览', exact: true }).click();
    await page.getByRole('heading', { name: '合集顺序待处理', exact: true }).waitFor();
    assert(
      await page.getByRole('button', { name: '确认整理', exact: true }).isDisabled(),
      'Unknown ordering prevents submission',
    );
    assert.equal(
      await page
        .locator('.publishing-plan-order')
        .getByText('Unidentified song', { exact: true })
        .count(),
      1,
    );
    assert.equal(applied.length, 0);
    blockedOrder = false;
    await page.getByRole('button', { name: '生成预览', exact: true }).click();
    await page.getByText('版本 Logo 载入失败', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '确认整理', exact: true }).count(), 0);
    assert.equal(applied.length, 0, 'Failed assets must never cause a mutation');
    failLogo = false;
    await page.getByRole('button', { name: '生成预览', exact: true }).click();
    await page.getByAltText('16:9 投稿封面预览').waitFor();
    assert.equal(
      await page.locator('.publishing-title-comparison dd').nth(1).innerText(),
      '【maimai】Our Wrenally Expert13.0 AP',
    );
    assert.equal(applied.length, 0, 'Preview must never apply remotely');
    const orderPreview = page.locator('.publishing-plan-order');
    assert.equal(
      await orderPreview.getByText('将排在第 2 位，共 3 条', { exact: true }).count(),
      1,
    );
    assert.equal(await orderPreview.getByText('Earlier song', { exact: true }).count(), 1);
    assert.equal(await orderPreview.getByText('Later song', { exact: true }).count(), 1);
    assert.equal(
      await orderPreview.getByText('本次会调整「13.0」小节顺序', { exact: true }).count(),
      1,
    );
    await page.getByLabel('封面左右镜像').check();
    assert.equal(
      await page.getByAltText('16:9 投稿封面预览').count(),
      0,
      'Changed fields invalidate the frozen preview',
    );
    await capturePublishingMirrorInputs(page);
    await page.getByRole('button', { name: '生成预览', exact: true }).click();
    await page.getByAltText('16:9 投稿封面预览').waitFor();
    await verifyPublishingMirrors(page, output, width);
    await page.getByRole('button', { name: '确认整理', exact: true }).click();
    await page.getByRole('button', { name: '重试整理', exact: true }).waitFor();
    assert.equal(
      await page
        .locator('.publishing-preview')
        .getByText('合集排序 · 待确认', { exact: true })
        .count(),
      1,
    );
    const frozenImages = { cover16: applied[0].cover16, cover43: applied[0].cover43 };
    failLogo = true;
    await page.reload();
    await page.getByRole('button', { name: '继续整理', exact: true }).click();
    assert.equal(
      orderChecks,
      1,
      'History recovery must use the frozen plan, not a new ordering query',
    );
    assert.equal(
      await page.getByAltText('16:9 投稿封面预览').getAttribute('src'),
      frozenImages.cover16,
    );
    assert.equal(
      await page.getByAltText('4:3 投稿封面预览').getAttribute('src'),
      frozenImages.cover43,
    );
    await page.getByRole('button', { name: '重试整理', exact: true }).click();
    await page.getByRole('button', { name: '已整理', exact: true }).waitFor();
    assert.equal(
      await page
        .locator('.publishing-preview')
        .getByText('合集排序 · 已完成', { exact: true })
        .count(),
      1,
    );
    assert.equal(applied.length, 1);
    assert.equal(retryCount, 1);
    assert.equal(applied[0].plan_id, `plan-${planCount}`);
    assert.equal(prepared.at(-1).mirror_horizontal, true);
    const layout = await page.evaluate(() => ({
      width: innerWidth,
      scroll: document.documentElement.scrollWidth,
      buttons: [...document.querySelectorAll('.sidebar .nav-item')].map(
        (item) => item.getBoundingClientRect().width,
      ),
    }));
    assert(layout.scroll <= layout.width + 1, `Horizontal overflow at ${width}`);
    if (width < 600)
      assert(
        layout.buttons.every((value) => value >= 44),
        'Mobile navigation must remain tappable',
      );
    await page.screenshot({ path: path.join(output, `${width}.png`), fullPage: true });
    await page.getByRole('button', { name: /Our Wrenally old title/ }).click();
    await page.getByLabel('谱面', { exact: true }).fill('Our Wrenally');
    await page.getByRole('button', { name: /Our Wrenally STD/ }).click();
    assert.equal(await page.getByLabel('合集', { exact: true }).inputValue(), '8');
    ambiguous = true;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    assert(
      await page.getByRole('button', { name: /Our Wrenally old title/ }).isDisabled(),
      'Stale video selections must be disabled while collections refresh',
    );
    assert(
      await page.getByLabel('合集', { exact: true }).isDisabled(),
      'Stale collection choices must not be editable during refresh',
    );
    await page.getByRole('button', { name: '刷新', exact: true }).click({ trial: true });
    assert.equal(
      await page.getByLabel('合集', { exact: true }).inputValue(),
      '',
      'Automatic collection choice must recompute from the refreshed list',
    );
    await page.getByRole('button', { name: /Our Wrenally old title/ }).click();
    await page.getByLabel('谱面', { exact: true }).fill('Our Wrenally');
    await page.getByRole('button', { name: /Our Wrenally STD/ }).click();
    assert.equal(
      await page.getByLabel('合集', { exact: true }).inputValue(),
      '',
      'Ambiguous section matches must remain unselected',
    );
    persistedJob = { ...persistedJob, state: 'failed' };
    delete latestPlan.order;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.getByRole('button', { name: '继续整理', exact: true }).click();
    await page.getByText('请重新生成预览以确认合集顺序', { exact: true }).waitFor();
    assert(
      await page.getByRole('button', { name: '重试整理', exact: true }).isDisabled(),
      'Old unfinished plans without ordering evidence must not submit',
    );
    assert.equal(applied.length, 1);
    assert.equal(retryCount, 1);
    ambiguous = false;
    failLogo = false;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.getByRole('button', { name: '刷新', exact: true }).click({ trial: true });
    async function loadFixtureVideo(value) {
      await page.getByLabel('BV 号或视频链接').fill(value);
      await page.getByRole('button', { name: '载入稿件', exact: true }).click();
      await page.getByRole('button', { name: '载入稿件', exact: true }).click({ trial: true });
    }
    async function selectFixtureChart() {
      await page.getByLabel('谱面', { exact: true }).fill('Our Wrenally');
      await page.getByRole('button', { name: /Our Wrenally STD/ }).click();
    }
    async function appBadgePixels() {
      return page.evaluate(async () => {
        const images = [...document.querySelectorAll('.publishing-cover-previews img')];
        return Promise.all(
          images.map(async (image) => {
            await image.decode();
            const canvas = document.createElement('canvas');
            canvas.width = image.naturalWidth;
            canvas.height = image.naturalHeight;
            const context = canvas.getContext('2d');
            context.drawImage(image, 0, 0);
            const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
            let count = 0;
            for (let index = 0; index < pixels.length; index += 4)
              if (pixels[index] === 12 && pixels[index + 1] === 250 && pixels[index + 2] === 105)
                count += 1;
            return count;
          }),
        );
      });
    }
    await loadFixtureVideo(plusBvid);
    assert.equal(
      await page.getByLabel('完成标记').inputValue(),
      'app',
      'Read AP+ from the actual video title, including a fullwidth plus',
    );
    assert.equal(
      await page.getByLabel('录像达成率').inputValue(),
      '',
      'AP+ title must not infer the video achievement from personal best',
    );
    await selectFixtureChart();
    assert.equal(
      await page.getByLabel('完成标记').inputValue(),
      'app',
      'Choosing a chart must preserve the video combo',
    );
    expectedAchievement = '101.0000';
    await page.getByLabel('录像达成率').fill(expectedAchievement);
    await page.getByRole('button', { name: '生成预览', exact: true }).click();
    await page.getByAltText('16:9 投稿封面预览').waitFor();
    assert.equal(prepared.at(-1).combo, 'app');
    assert.equal(
      await page.locator('.publishing-title-comparison dd').nth(1).innerText(),
      '【maimai】Our Wrenally Expert13.0 AP+',
    );
    const appPixels = await appBadgePixels();
    assert.equal(appPixels.length, 2);
    assert(
      appPixels.every((count) => count > 100),
      'Both cover ratios must render the AP+ asset from the frozen combo',
    );
    await page.getByLabel('完成标记').selectOption('ap');
    expectedAchievement = '100.5000';
    await page.getByLabel('录像达成率').fill(expectedAchievement);
    await page.getByRole('button', { name: '生成预览', exact: true }).click();
    await page.getByAltText('16:9 投稿封面预览').waitFor();
    assert.equal(
      prepared.at(-1).combo,
      'ap',
      'Explicit user selection takes precedence over the video title',
    );
    assert.equal(
      await page.locator('.publishing-title-comparison dd').nth(1).innerText(),
      '【maimai】Our Wrenally Expert13.0 AP',
    );
    assert.deepEqual(await appBadgePixels(), [0, 0], 'AP must not reuse the AP+ badge');
    await loadFixtureVideo(plusBvid);
    assert.equal(
      await page.getByLabel('完成标记').inputValue(),
      'ap',
      'Reloading the same video preserves its explicit combo selection',
    );
    await page.getByLabel('完成标记').selectOption('app');
    await loadFixtureVideo(ordinaryBvid);
    assert.equal(
      await page.getByLabel('完成标记').inputValue(),
      'ap',
      'Changing videos must clear the previous video combo override',
    );
    await loadFixtureVideo('BV1MUSIC1111');
    assert.equal(
      await page.getByLabel('完成标记').inputValue(),
      'ap',
      'AP+ inside a song title is not a terminal result marker',
    );
    await loadFixtureVideo('BV1bJouBSEiH');
    assert.equal(
      await page.getByLabel('完成标记').inputValue(),
      'app',
      'Recognize the mirrored result prefix only for the known mirrored video',
    );
    await loadFixtureVideo('BV1OTHER1111');
    assert.equal(
      await page.getByLabel('完成标记').inputValue(),
      'ap',
      'Do not reverse arbitrary video titles',
    );
    assert.equal(applied.length, 1, 'AP+ previews must not trigger an extra publish');
    checks.push({
      width,
      loginLifecycle,
      prepares: prepared.length,
      applies: applied.length,
      dimensions: ['1920x1080', '1440x1080'],
      mirrored: true,
      explicitAchievement: true,
      independentOrderCheck: true,
      orderBlockedPreventsSubmission: true,
      orderRetryVerified: true,
      legacyPlanWithoutOrderBlocked: true,
      apPlusVideoTitleAndBothCovers: true,
      explicitComboPreserved: true,
    });
    await context.close();
  }
  assert.deepEqual(errors, []);
  await fs.writeFile(
    path.join(output, 'report.json'),
    JSON.stringify({ checks, errors, externalWrites: false }, null, 2),
  );
  console.log(
    'Publishing UI verified: QR login, explicit video score, strict assets, frozen dual PNGs, full mirror, single apply, mobile layout.',
  );
} catch (error) {
  if (process.env.GITHUB_ACTIONS) {
    const message = String(error?.stack || error)
      .replaceAll('%', '%25')
      .replaceAll('\r', '%0D')
      .replaceAll('\n', '%0A');
    console.error(`::error title=Publishing UI verification::${message}`);
  }
  throw error;
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
