import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { badgePNG, fixtureAsset, useSystemCoverFont } from './fixtures.mjs';
import { idleScoreSync } from './score-sync-fixture.mjs';

const root = path.resolve(import.meta.dirname, '../..');
const bundle = path.resolve(process.env.UI_DIST || path.join(root, 'frontend/dist'));
const output = path.resolve(
  process.env.PUBLISHING_AUDIT_OUTPUT || path.join(root, 'output/publishing-audit'),
);
await fs.mkdir(output, { recursive: true });
const checks = [];
const errors = [];
const bvid = 'BV1iWMh6iECe';
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
              ['dx', 'standard', 'ap', 'app'].map((key) => [key, { file: 'badge.png' }]),
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
      return route.fulfill(asset);
    });
    await context.route('**/api/**', async (route) => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      const send = (json, status = 200) => route.fulfill({ json, status });
      if (pathname === '/api/bilibili/status')
        return send({ connected, account: connected ? { mid: 1, name: 'Fixture user' } : null });
      if (pathname === '/api/bilibili/login')
        return send({
          login_id: 'opaque-test',
          url: 'https://passport.bilibili.com/fixture-only',
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
      if (pathname === '/api/bilibili/collections')
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
        persistedJob = { ...persistedJob, state: 'complete', error: null };
        return send(persistedJob);
      }
      if (pathname === '/api/bilibili/prepare') {
        const input = request.postDataJSON();
        assert.equal(input.chart_key, chart.chart_key);
        assert.equal(input.achievement, '100.5000');
        assert.equal(input.section_id, 8);
        prepared.push(input);
        latestPlan = {
          ...input,
          plan_id: `plan-${++planCount}`,
          old_title: 'Our Wrenally old title',
          title: '【maimai】Our Wrenally Expert13.0 AP',
          chart,
          expires_at: '2030-01-01T00:00:00Z',
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
          steps: {},
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
    await page.getByRole('button', { name: '扫码连接' }).click();
    const qr = page.getByAltText('Bilibili 登录二维码');
    await qr.waitFor();
    assert(
      (await qr.getAttribute('src')).startsWith('data:image/png;base64,'),
      'QR must be generated locally',
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
    const normal = await page.getByAltText('16:9 投稿封面预览').getAttribute('src');
    await page.getByLabel('封面左右镜像').check();
    assert.equal(
      await page.getByAltText('16:9 投稿封面预览').count(),
      0,
      'Changed fields invalidate the frozen preview',
    );
    await page.getByRole('button', { name: '生成预览', exact: true }).click();
    await page.getByAltText('16:9 投稿封面预览').waitFor();
    const mirrorMatches = await page.evaluate(async (before) => {
      async function pixels(src) {
        const image = new Image();
        image.src = src;
        await image.decode();
        const canvas = document.createElement('canvas');
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        const context = canvas.getContext('2d');
        context.drawImage(image, 0, 0);
        return context.getImageData(0, 0, canvas.width, canvas.height).data;
      }
      const original = await pixels(before);
      const mirrored = await pixels(document.querySelector('img[alt="16:9 投稿封面预览"]').src);
      for (let y = 0; y < 1080; y += 31)
        for (let x = 0; x < 1920; x += 29)
          for (let c = 0; c < 4; c++)
            if (original[(y * 1920 + x) * 4 + c] !== mirrored[(y * 1920 + 1919 - x) * 4 + c])
              return false;
      return true;
    }, normal);
    assert(mirrorMatches, 'The entire rendered cover must mirror, not only the jacket');
    await page.getByRole('button', { name: '确认整理', exact: true }).click();
    await page.getByRole('button', { name: '重试整理', exact: true }).waitFor();
    const frozenImages = { cover16: applied[0].cover16, cover43: applied[0].cover43 };
    failLogo = true;
    await page.reload();
    await page.getByRole('button', { name: '继续整理', exact: true }).click();
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
    ambiguous = true;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.getByRole('button', { name: /Our Wrenally old title/ }).click();
    await page.getByLabel('谱面', { exact: true }).fill('Our Wrenally');
    await page.getByRole('button', { name: /Our Wrenally STD/ }).click();
    assert.equal(
      await page.getByLabel('合集', { exact: true }).inputValue(),
      '',
      'Ambiguous section matches must remain unselected',
    );
    checks.push({
      width,
      prepares: prepared.length,
      applies: applied.length,
      dimensions: ['1920x1080', '1440x1080'],
      mirrored: true,
      explicitAchievement: true,
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
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
