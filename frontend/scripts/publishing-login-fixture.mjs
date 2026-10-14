import assert from 'node:assert/strict';
import { fixtureAsset } from './fixtures.mjs';
import { idleScoreSync } from './score-sync-fixture.mjs';

const account = { mid: 1, name: 'Login fixture user' };
const video = {
  bvid: 'BV1iWMh6iECe',
  title: 'Login fixture video',
  cover: '/jackets/9200.png',
};

function delayedReply(json) {
  let arrive;
  let release;
  let finish;
  const arrived = new Promise((resolve) => (arrive = resolve));
  const released = new Promise((resolve) => (release = resolve));
  const finished = new Promise((resolve) => (finish = resolve));
  return {
    arrived,
    finished,
    release,
    async reply(route) {
      arrive();
      await released;
      await route.fulfill({ json });
      finish();
    },
  };
}

async function assertDisconnected(page) {
  await page.getByRole('button', { name: '扫码连接', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '断开连接', exact: true }).count(), 0);
  assert.equal(await page.getByAltText('Bilibili 登录二维码').count(), 0);
  assert.equal(await page.getByRole('heading', { name: '选择稿件', exact: true }).count(), 0);
  assert.equal(await page.getByRole('heading', { name: '稿件信息', exact: true }).count(), 0);
}

async function assertNoEnabledButton(page, name, message) {
  const buttons = page.getByRole('button', { name, exact: true });
  if (await buttons.count()) {
    assert(await buttons.isDisabled(), message);
    await buttons.evaluate((button) => button.click());
  }
}

export async function verifyPublishingLoginLifecycle(browser, origin, width, errors) {
  const context = await browser.newContext({
    viewport: { width, height: 900 },
    isMobile: width < 600,
    hasTouch: width < 600,
  });
  const requests = [];
  const pendingReplies = [];
  const hold = (json) => {
    const reply = delayedReply(json);
    pendingReplies.push(reply);
    return reply;
  };
  let connected = true;
  let loginCount = 0;
  let statusDelay = hold({ connected: true, account });
  let statusErrorOnce = false;
  let videosDelay = null;
  let listingMode = 'normal';
  const oldPoll = hold({ state: 'connected', account });
  let secondPollCount = 0;
  let title = video.title;
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
    '/api/chart-filters': { constants: [], versions: [], cn_versions: [] },
    '/api/venues': { items: [] },
    '/api/ap-records': { items: [], count: 0 },
    '/api/charts': { items: [], total: 0, limit: 20, offset: 0 },
    '/api/summary': {
      total: 0,
      complete: 0,
      ap: 0,
      remaining: 0,
      conflicts: 0,
      legacy_complete: 0,
      levels: [],
      constants: [],
      sync: null,
    },
    '/api/bilibili/collections': { items: [] },
    '/api/bilibili/jobs': { items: [] },
  };
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    const asset = fixtureAsset(url.pathname);
    return asset ? route.fulfill(asset) : route.continue();
  });
  await context.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    requests.push({ pathname, search: url.search, method: route.request().method() });
    if (pathname === '/api/bilibili/status') {
      if (statusErrorOnce) {
        statusErrorOnce = false;
        return route.fulfill({ status: 503, json: { detail: '模拟连接检查失败' } });
      }
      const delay = statusDelay;
      statusDelay = null;
      return delay
        ? delay.reply(route)
        : route.fulfill({ json: { connected, account: connected ? account : null } });
    }
    if (pathname === '/api/bilibili/session') {
      assert.equal(route.request().method(), 'DELETE');
      connected = false;
      return route.fulfill({ json: { connected: false } });
    }
    if (pathname === '/api/bilibili/login') {
      assert.equal(route.request().method(), 'POST');
      loginCount += 1;
      return route.fulfill({
        json: {
          login_id: `login-${loginCount}`,
          url: `https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=fixture-${loginCount}`,
          expires_at: '2030-01-01T00:00:00Z',
        },
      });
    }
    if (pathname === '/api/bilibili/login/login-1') return oldPoll.reply(route);
    if (pathname === '/api/bilibili/login/login-2') {
      secondPollCount += 1;
      if (secondPollCount === 1) return route.fulfill({ json: { state: 'scanned' } });
      connected = true;
      return route.fulfill({ json: { state: 'connected', account } });
    }
    if (/^\/api\/bilibili\/login\/login-[34]$/.test(pathname)) {
      connected = true;
      return route.fulfill({ json: { state: 'connected', account } });
    }
    if (pathname === '/api/bilibili/videos') {
      if (listingMode === 'error')
        return route.fulfill({ status: 503, json: { detail: '模拟合集稿件获取失败' } });
      const delay = videosDelay;
      videosDelay = null;
      return delay
        ? delay.reply(route)
        : route.fulfill({
            json: {
              items: listingMode === 'empty' ? [] : [{ ...video, title }],
              page: Number(url.searchParams.get('page') || '1'),
              has_more: listingMode === 'paged' && url.searchParams.get('page') === '1',
            },
          });
    }
    if (pathname === '/api/bilibili/videos/BV1NEW111111')
      return route.fulfill({
        json: { bvid: 'BV1NEW111111', title: 'New handplay outside collections', revision: '1' },
      });
    assert(fixtures[pathname], `Unexpected login lifecycle API: ${pathname}`);
    return route.fulfill({ json: fixtures[pathname] });
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.clock.install();
  try {
    const initialStatus = statusDelay;
    await page.goto(origin + '/#publishing');
    await initialStatus.arrived;
    await page.getByRole('heading', { name: 'Bilibili', exact: true }).waitFor();
    await assertNoEnabledButton(page, '扫码连接', 'Unknown login state must not allow QR login');
    assert.equal(loginCount, 0, 'Clicking while status is pending must not request a QR code');
    assert.equal(
      requests.filter((request) => request.pathname.startsWith('/api/bilibili/videos')).length,
      0,
      'Unknown login state must not request video data',
    );
    assert.equal(await page.getByRole('heading', { name: '选择稿件', exact: true }).count(), 0);
    initialStatus.release();
    await page.getByRole('button', { name: video.title, exact: false }).waitFor();
    assert.equal(await page.getByAltText('Bilibili 登录二维码').count(), 0);
    assert.equal(await page.getByRole('button', { name: '扫码连接', exact: true }).count(), 0);

    const checkingStatus = hold({ connected: true, account });
    statusDelay = checkingStatus;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await checkingStatus.arrived;
    await page.locator('.publishing-videos[aria-busy="false"]').waitFor();
    assert(
      await page.getByRole('button', { name: video.title, exact: false }).isDisabled(),
      'Loaded videos must remain disabled while the session check is pending',
    );
    assert(await page.getByLabel('BV 号或视频链接').isDisabled());
    await assertNoEnabledButton(
      page,
      '检查合集顺序',
      'Collection operations must wait for the session check',
    );
    checkingStatus.release();
    await page.getByRole('button', { name: video.title, exact: false }).click({ trial: true });

    const refreshListing = async () => {
      await page.getByRole('button', { name: '刷新', exact: true }).click();
      await page.getByRole('button', { name: '刷新', exact: true }).click({ trial: true });
    };
    listingMode = 'error';
    await refreshListing();
    await page.getByText('模拟合集稿件获取失败', { exact: true }).waitFor();
    assert.equal(
      await page.locator('.publishing-video').count(),
      0,
      'A failed refresh must not retain videos from the previous collection response',
    );
    assert.equal(
      await page.getByText('暂无已加入合集的手元视频', { exact: true }).count(),
      0,
      'A failed request must not claim the collection has no videos',
    );
    listingMode = 'empty';
    await refreshListing();
    await page.getByText('暂无已加入合集的手元视频', { exact: true }).waitFor();
    await page.getByLabel('BV 号或视频链接').fill('https://www.bilibili.com/video/BV1NEW111111');
    await page.getByRole('button', { name: '载入稿件', exact: true }).click();
    await page
      .getByRole('link', { name: 'New handplay outside collections', exact: true })
      .waitFor();
    assert.equal(await page.locator('.publishing-video').count(), 0);

    listingMode = 'paged';
    await refreshListing();
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await page.getByRole('button', { name: '刷新', exact: true }).click({ trial: true });
    assert.equal(
      requests.filter((request) => request.pathname === '/api/bilibili/videos').at(-1).search,
      '?page=2',
    );
    await refreshListing();
    assert.equal(
      requests.filter((request) => request.pathname === '/api/bilibili/videos').at(-1).search,
      '?page=1',
      'Refreshing collections must restart compact pagination at the first page',
    );
    assert(await page.getByRole('button', { name: '上一页', exact: true }).isDisabled());
    listingMode = 'normal';

    await page.getByRole('button', { name: '断开连接', exact: true }).click();
    await assertDisconnected(page);
    await page.getByRole('button', { name: '扫码连接', exact: true }).click();
    await page.getByAltText('Bilibili 登录二维码').waitFor();
    await page.clock.runFor(1500);
    await oldPoll.arrived;
    await page.getByRole('button', { name: '重新获取二维码', exact: true }).click();
    await page.getByRole('button', { name: '重新获取二维码', exact: true }).click({ trial: true });
    await page.clock.runFor(1500);
    await page.getByText('请在 Bilibili 确认登录', { exact: true }).waitFor();
    await page.clock.runFor(2500);
    await page.getByRole('button', { name: '断开连接', exact: true }).waitFor();
    await page.getByRole('button', { name: video.title, exact: false }).waitFor();
    assert.equal(await page.getByAltText('Bilibili 登录二维码').count(), 0);
    assert.equal(
      await page.getByRole('button', { name: '重新获取二维码', exact: true }).count(),
      0,
    );
    const pollCount = requests.filter((request) => request.pathname.includes('/login/')).length;
    await page.clock.runFor(6000);
    assert.equal(
      requests.filter((request) => request.pathname.includes('/login/')).length,
      pollCount,
      'Connected sessions must stop QR polling',
    );
    await page.getByRole('button', { name: '断开连接', exact: true }).click();
    await assertDisconnected(page);
    oldPoll.release();
    await oldPoll.finished;
    await page.clock.runFor(6000);
    await assertDisconnected(page);
    assert.equal(loginCount, 2);

    await page.getByRole('button', { name: '扫码连接', exact: true }).click();
    await page.getByAltText('Bilibili 登录二维码').waitFor();
    await page.clock.runFor(1500);
    await page.getByRole('button', { name: video.title, exact: false }).waitFor();
    const staleStatus = hold({ connected: true, account });
    const staleVideos = hold({
      items: [{ ...video, title: 'Stale video after disconnect' }],
      page: 1,
      has_more: false,
    });
    statusDelay = staleStatus;
    videosDelay = staleVideos;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await Promise.all([staleStatus.arrived, staleVideos.arrived]);
    await assertNoEnabledButton(page, '扫码连接', 'Refreshing login state must not start QR login');
    await assertNoEnabledButton(
      page,
      '载入稿件',
      'Unverified login state must disable video actions',
    );
    await page.getByRole('button', { name: '断开连接', exact: true }).click();
    await assertDisconnected(page);
    staleStatus.release();
    staleVideos.release();
    await Promise.all([staleStatus.finished, staleVideos.finished]);
    await page.clock.runFor(6000);
    await assertDisconnected(page);
    assert.equal(await page.getByText('Stale video after disconnect', { exact: true }).count(), 0);
    title = 'Fresh video after reconnect';
    await page.getByRole('button', { name: '扫码连接', exact: true }).click();
    await page.getByAltText('Bilibili 登录二维码').waitFor();
    await page.clock.runFor(1500);
    await page.getByRole('button', { name: title, exact: false }).waitFor();
    assert.equal(await page.getByText('Stale video after disconnect', { exact: true }).count(), 0);
    assert.equal(await page.getByAltText('Bilibili 登录二维码').count(), 0);
    await page.getByRole('button', { name: '断开连接', exact: true }).click();
    await assertDisconnected(page);
    statusErrorOnce = true;
    await page.reload();
    await page.getByRole('button', { name: '重试连接检查', exact: true }).waitFor();
    await assertNoEnabledButton(page, '扫码连接', 'A failed status check must not permit QR login');
    assert.equal(loginCount, 4, 'Status failures must not silently start a new login');
    await page.getByRole('button', { name: '重试连接检查', exact: true }).click();
    await page.getByRole('button', { name: '扫码连接', exact: true }).click({ trial: true });
    await assertDisconnected(page);
    return {
      width,
      initialStatusGated: true,
      refreshStatusGated: true,
      qrLifecycle: true,
      staleLoginIgnored: true,
      staleVideoIgnored: true,
      statusFailureRetry: true,
      filteredListEmpty: true,
      unlistedVideoManualLoad: true,
      failedListCleared: true,
      refreshPaginationReset: true,
    };
  } finally {
    for (const reply of pendingReplies) reply.release();
    await context.close();
  }
}
