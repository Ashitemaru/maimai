// Core user flows against the production bundle and the shared in-memory API.
// Usage: node scripts/verify-app.mjs [--webkit] [section ...]
import assert from 'node:assert/strict';
import { MASTER_KEY, noHorizontalOverflow, reply, runSections, toast, until } from './mock-api.mjs';

const rows = (page) => page.locator('.chart-table tbody tr');
const drawer = (page) => page.getByRole('dialog', { name: '谱面记录' });
const button = (scope, name) => scope.getByRole('button', { name, exact: true });

async function login(page, key = MASTER_KEY) {
  const input = page.getByLabel('Master Key', { exact: true });
  await input.fill(key);
  await input.press('Enter');
}

const sections = {
  async auth(app) {
    app.api.state.auth.required = true;
    const page = await app.open('/#ap');
    await page.getByLabel('Master Key', { exact: true }).waitFor();
    assert.equal(await page.locator('.shell').count(), 0);
    assert(
      app.api.calls.every((call) => call.path.startsWith('/api/auth/')),
      'no data before login',
    );
    await login(page, 'wrong-key');
    await toast(page, 'Master Key 不正确');
    await login(page, 'limited');
    await toast(page, '尝试次数较多');
    await login(page);
    await rows(page).first().waitFor();
    const stored = await page.evaluate(() =>
      JSON.stringify([{ ...localStorage }, { ...sessionStorage }, document.cookie]),
    );
    assert(!stored.includes(MASTER_KEY), 'Master Key must not reach browser storage');
    await page.reload();
    await rows(page).first().waitFor();
    await button(page, '退出').click();
    await page.getByLabel('Master Key', { exact: true }).waitFor();
    await page.reload();
    await page.getByLabel('Master Key', { exact: true }).waitFor();
    // An expired session on any personal API returns to the login page.
    await login(page);
    await rows(page).first().waitFor();
    app.api.state.auth.sessions.clear();
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await toast(page, '登录已过期');
    assert.equal(await page.locator('.shell').count(), 0);
  },

  async list(app) {
    const page = await app.open('/#ap');
    await until(async () => (await rows(page).count()) === 12, 'first page');
    assert.equal(await page.locator('.pager-buttons span').innerText(), '1 / 2');
    const alpha = rows(page).filter({ hasText: 'Alpha Rhythm' });
    assert.match(await alpha.innerText(), /100\.6000%/);
    assert.match(await alpha.innerText(), /【上海】星光机厅/);
    await button(page, '下一页').click();
    await until(
      async () => (await page.locator('.pager-buttons span').innerText()) === '2 / 2',
      'page 2',
    );
    await page.getByLabel('搜索谱面').fill('charlie');
    await until(async () => (await rows(page).count()) === 1, 'search');
    assert.equal(app.api.last('/api/charts').query.get('offset'), '0', 'search resets paging');
    await page.getByLabel('搜索谱面').fill('');
    await page.getByLabel('谱面难度').selectOption('2');
    await until(
      async () =>
        (await rows(page).allInnerTexts()).join().includes('Delta Drive') &&
        (await rows(page).count()) === 1,
      'difficulty filter',
    );
    assert.equal(app.api.last('/api/summary').query.get('difficulty'), '2', 'stats share filters');
    await button(page, '清除筛选').click();
    await page.locator('.status-tab', { hasText: '已完成' }).click();
    await until(async () => (await rows(page).count()) === 4, 'completed tab');
    const stats = await page.locator('.stat-value').allInnerTexts();
    assert.match(stats[0], /^4/);
  },

  async detail(app) {
    const page = await app.open('/#ap');
    await rows(page).filter({ hasText: 'Charlie Step' }).click();
    await drawer(page).getByRole('heading', { name: 'Charlie Step' }).waitFor();
    assert(await button(drawer(page), '保存待核记录').isDisabled(), 'venue is required');
    await drawer(page).getByLabel('AP 机厅').selectOption('【上海】星光机厅');
    await button(drawer(page), '保存待核记录').click();
    await toast(page, '已加入待核记录');
    await drawer(page).waitFor({ state: 'hidden' });
    const put = app.api.last('/api/charts/3:3/ap-record');
    assert.equal(put.body.expected_revision, 0);
    assert.equal(put.body.venue, '【上海】星光机厅');
    await page.locator('.section-nav-count', { hasText: '1' }).waitFor();
    await page.getByRole('link', { name: /待核记录/ }).click();
    await page
      .locator('.pending-card', { hasText: 'Charlie Step' })
      .getByText('【上海】星光机厅')
      .waitFor();

    // Another device changes the record: the stale drawer gets 409, keeps the server state
    // and only writes again after an explicit reload.
    await button(page, '修改登记').click();
    await drawer(page).waitFor();
    app.api.state.charts[2].record_revision += 1;
    await button(drawer(page), '取消登记').click();
    await toast(page, '记录已在其他页面更新');
    assert(await button(drawer(page), '保存待核记录').isDisabled());
    assert.equal(app.api.state.charts[2].pending_record.venue, '【上海】星光机厅');
    await button(drawer(page), '重新载入记录').click();
    await button(drawer(page), '取消登记').click();
    await toast(page, '已取消待核记录');
    await page.getByText('暂无待核记录').waitFor();
    assert.equal(app.api.last('/api/charts/3:3/ap-record').query.get('expected_revision'), '2');
  },

  async completion(app) {
    const page = await app.open('/?view=completion#ap');
    const tiles = page.locator('.completion-tile');
    await until(async () => (await tiles.count()) === 15, 'completion tiles');
    assert.equal(await page.locator('.completion-group').count(), 13, 'grouped by constant');
    assert.match(await tiles.first().getAttribute('aria-label'), /Alpha Rhythm .* AP/);
    assert(await button(page, '导出图片').isEnabled());
    await page.getByRole('button', { name: /查看 Charlie Step/ }).click();
    await drawer(page).getByRole('heading', { name: 'Charlie Step' }).waitFor();
    await button(drawer(page), '关闭').click();
    await button(page, '列表').click();
    await until(async () => (await rows(page).count()) === 12, 'back to list');
    assert.equal(new URL(page.url()).searchParams.get('view'), null);
  },

  async matrix(app) {
    const page = await app.open('/#statistics');
    const cell = page.getByRole('button', { name: /^定数 13\.9，舞萌DX 2023，/ });
    await cell.waitFor();
    assert.equal(
      await page.getByLabel('显示').inputValue(),
      'remaining',
      'default shows remaining',
    );
    assert.equal((await cell.innerText()).trim(), '2');
    const request = app.api.last('/api/completion-matrix').query;
    assert.deepEqual([request.get('constant_min'), request.get('constant_max')], ['12.0', '15.0']);
    await page.getByLabel('显示').selectOption('complete');
    assert.equal((await cell.innerText()).trim(), '0 / 2');
    await page.getByLabel('显示').selectOption('remaining');
    await cell.click();
    const dialog = page.locator('dialog.matrix-dialog');
    await dialog.getByRole('button', { name: /^查看 Delta Drive/ }).waitFor();
    assert.equal(
      await dialog.getByRole('button', { name: /未完成 2/ }).getAttribute('aria-pressed'),
      'true',
    );
    assert.equal(app.api.last('/api/charts').query.get('status'), 'incomplete');
    await dialog.getByRole('button', { name: /^查看 Delta Drive/ }).click();
    await drawer(page).getByRole('heading', { name: 'Delta Drive' }).waitFor();
  },

  async preferences(app) {
    const page = await app.open('/#settings');
    const min = page.getByLabel('最低定数', { exact: true });
    const max = page.getByLabel('最高定数', { exact: true });
    await until(async () => (await min.inputValue()) === '12.0', 'loaded preferences');
    await min.fill('15');
    await max.fill('14');
    await button(page, '保存偏好').click();
    await toast(page, '最低定数不能大于最高定数');
    assert.equal(app.api.count('/api/settings/preferences', 'PUT'), 0);
    await min.fill('13');
    await button(page, '保存偏好').click();
    await toast(page, '偏好设置已保存');
    assert.deepEqual(app.api.state.preferences, {
      default_constant_min: 13,
      default_constant_max: 14,
    });
    await page.reload();
    await page.getByRole('link', { name: 'AP 图鉴' }).click();
    await until(async () => (await rows(page).count()) === 9, 'list uses saved default range');
    const query = app.api.last('/api/charts').query;
    assert.deepEqual([query.get('constant_min'), query.get('constant_max')], ['13.0', '14.0']);
    assert.equal(
      await page.getByRole('slider', { name: '最低定数' }).getAttribute('aria-valuenow'),
      '13',
    );
    await page.goto(app.origin + '/#statistics');
    await page.locator('.matrix-cell').first().waitFor();
    assert.equal(app.api.last('/api/completion-matrix').query.get('constant_min'), '13.0');
  },

  async 'score-sync'(app) {
    const page = await app.open('/#settings');
    const panel = page.getByRole('region', { name: '同步水鱼成绩', exact: true });
    const sync = panel.getByRole('button', { name: /从水鱼同步|正在同步…/ });
    await panel.getByLabel(/Import-Token/).waitFor();
    assert(await sync.isDisabled(), 'sync needs a saved token');
    await panel.getByLabel(/Import-Token/).fill('fixture-token');
    await button(panel, '保存 Token').click();
    await panel.getByText('Import-Token · 已保存', { exact: true }).waitFor();
    assert.equal(app.api.last('/api/settings/divingfish').body.token, 'fixture-token');
    const refreshes = app.api.count('/api/ap-records');
    await sync.click();
    await panel.getByRole('status').filter({ hasText: '正在读取水鱼成绩' }).waitFor();
    await panel.locator('.score-sync-receipt').getByText('15').waitFor();
    await toast(page, '水鱼成绩已同步');
    assert(app.api.count('/api/ap-records') > refreshes, 'a finished sync refreshes records');
    app.api.state.syncOutcome = 'failed';
    await sync.click();
    await toast(page, '水鱼暂时不可用');
    assert(await sync.isEnabled(), 'a failed job can be retried');
  },

  async venues(app) {
    const page = await app.open('/#venues');
    await page.getByText('3 家机厅 · 2 AP', { exact: true }).waitFor();
    const map = page.locator('.venue-atlas-layout .venue-map-svg');
    const centered = (label) =>
      page.waitForFunction((name) => {
        const svg = document.querySelector('.venue-atlas-layout .venue-map-svg');
        const marker = [...svg.querySelectorAll('[role=button]')].find(
          (item) => item.getAttribute('aria-label') === name,
        );
        const [x, y] = (marker?.getAttribute('transform').match(/-?[\d.]+/g) || []).map(Number);
        return marker && Math.hypot(x - svg.clientWidth / 2, y - svg.clientHeight / 2) < 2;
      }, label);
    await map.getByRole('button', { name: '【上海】星光机厅，1 AP', exact: true }).click();
    const selected = page.getByRole('region', { name: '机厅详情' });
    await selected.getByRole('button', { name: 'Alpha Rhythm Master' }).waitFor();
    await centered('【上海】星光机厅，1 AP');
    await page.locator('.venue-atlas-card', { hasText: '【北京】测试机厅' }).click();
    await centered('【北京】测试机厅，1 AP');
    assert.equal(new URL(page.url()).searchParams.get('venue'), '【北京】测试机厅');
    await page.getByPlaceholder('搜索机厅或城市').fill('上海');
    await page.getByText('1 家机厅 · 1 AP', { exact: true }).waitFor();
    await page.getByPlaceholder('搜索机厅或城市').fill('');

    // A confirmed venue record is locked in the detail drawer.
    await selected.getByRole('button', { name: 'Golf Groove Re:Master' }).click();
    await drawer(page).locator('.detail-rule', { hasText: '【北京】测试机厅' }).waitFor();
    assert.equal(await button(drawer(page), '保存待核记录').count(), 0);
    await button(drawer(page), '关闭').click();

    await button(page, '编辑位置').click();
    const editor = page.getByRole('dialog');
    await editor.getByLabel('搜索地点').fill('测试广场');
    await button(editor, '搜索').click();
    await editor.getByRole('button', { name: /搜索结果广场/ }).click();
    app.api.on('PUT /api/venues/location', () => reply(503, { detail: '保存暂时失败' }), {
      once: true,
    });
    await button(editor, '保存').click();
    await toast(page, '保存暂时失败');
    await button(editor, '保存').click();
    await toast(page, '机厅位置已保存');
    assert.deepEqual(app.api.last('/api/venues/location').body, {
      name: '【北京】测试机厅',
      latitude: 31.24,
      longitude: 121.49,
    });
    await button(page, '新增机厅').click();
    await editor.getByLabel('显示名称').fill('【测试】新门店');
    await editor.getByLabel('搜索地点').fill('测试广场');
    await button(editor, '搜索').click();
    await editor.getByRole('button', { name: /搜索结果广场/ }).click();
    await button(editor, '保存').click();
    await page.getByText('4 家机厅 · 2 AP', { exact: true }).waitFor();
    await button(page, '汇总表').click();
    const summary = page.getByRole('region', { name: '机厅 AP 汇总' });
    await summary.locator('.venue-summary-entry', { hasText: '星光机厅' }).waitFor();
    assert.match(await summary.locator('.venue-summary-total').innerText(), /2 AP/);
  },

  async catalog(app) {
    const { state } = app.api;
    state.bilibili.connected = true;
    state.bilibili.stale = [
      {
        bvid: 'BV1STALE1111',
        title: '【maimai】Delta Drive Expert13.8 AP',
        proposed_title: '【maimai】Delta Drive Expert13.9 AP',
        reasons: [{ code: 'constant', text: '定数 13.8 → 13.9' }],
        chart_key: '4:2',
        combo: 'ap',
        achievement: null,
        constant_old: '13.8',
        constant_new: '13.9',
      },
    ];
    const page = await app.open('/#settings');
    await page.locator('.catalog-main-status', { hasText: '曲库已更新' }).waitFor();
    await button(page, '更新曲库').click();
    await toast(page, '已开始更新曲库');
    await page.locator('.catalog-diff', { hasText: '新增谱面 2' }).waitFor({ timeout: 8000 });
    await page
      .getByRole('region', { name: '本次曲库变化' })
      .getByText('Hotel New')
      .first()
      .waitFor();

    // Adjudication: choose every field, confirm in the modal, then revoke from 人工记录.
    const review = page.locator('.catalog-review');
    const entry = (difficulty) =>
      review
        .locator('.record-review-entry')
        .filter({ has: page.locator(`.difficulty[data-difficulty="${difficulty}"]`) });
    const choose = async (difficulty, source = 'lxns') => {
      for (const key of ['title', 'constant'])
        await entry(difficulty).locator(`input[value="${key}-${source}"]`).check();
    };
    const confirm = button(review, '确认选择');
    await entry(0).waitFor();
    assert(await confirm.isDisabled());
    await choose(0, 'divingfish');
    await confirm.click();
    const dialog = page.getByRole('dialog');
    assert.match(await dialog.innerText(), /Link\(CoF\)/);
    await button(dialog, '确认提交').click();
    await entry(0).waitFor({ state: 'detached' });
    const batch = app.api.last('/api/catalog/records/decisions/batch').body;
    assert.deepEqual(
      batch.records[0].choices.map((choice) => choice.option_id),
      ['title-divingfish', 'constant-divingfish'],
    );
    assert.equal(batch.base_catalog_revision, 'r2');
    await button(review, '人工记录').click();
    await button(entry(0), '撤销').first().click();
    await until(
      () => app.api.count('/api/catalog/records/decisions/revoke', 'POST') === 1,
      'revoke',
    );
    await button(review, '待入库').click();
    await entry(0).waitFor();

    app.api.on(
      'POST /api/catalog/records/decisions/batch',
      () => reply(409, { detail: '曲库已变化，请刷新后重新选择。' }),
      { once: true },
    );
    await choose(1);
    await confirm.click();
    await button(dialog, '确认提交').click();
    await toast(page, '曲库已变化');
    if (await dialog.isVisible()) await button(dialog, '返回修改').click();
    assert.equal(await entry(1).locator('input:checked').count(), 2, '409 keeps the draft');

    // Bilibili videos made stale by the catalog change: preview covers, then apply.
    const stale = page.getByRole('region', { name: 'B 站稿件待更新' });
    await stale.getByRole('checkbox').check();
    await stale.getByLabel('BV1STALE1111 录像达成率').fill('100.5000');
    await button(stale, '预览').click();
    await stale.getByAltText('BV1STALE1111 16:9 封面预览').waitFor({ timeout: 20000 });
    const prepared = app.api.last('/api/bilibili/prepare').body;
    assert.deepEqual(
      [prepared.constant, prepared.section_id, prepared.achievement],
      ['13.9', 8, '100.5000'],
    );
    await button(stale, '确认更新').click();
    await stale.getByRole('status').filter({ hasText: '已整理' }).waitFor();
  },

  async publishing(app) {
    const page = await app.open('/#publishing');
    await button(page, '扫码连接').click();
    assert.match(
      await page.getByAltText('Bilibili 登录二维码').getAttribute('src'),
      /^data:image\/png/,
    );
    await page.getByText('Fixture user', { exact: true }).waitFor();
    await button(page, '检查合集顺序').click();
    await page.getByText('顺序有差异 · 3 条', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('13.9 顺序差异').getByText('3 → 1').count(), 1);

    await page.getByRole('button', { name: /Delta Drive old title/ }).click();
    await page.getByLabel('谱面', { exact: true }).fill('Delta');
    await page
      .locator('.publishing-chart-results')
      .getByRole('button', { name: /Delta Drive/ })
      .click();
    assert.equal(await page.getByLabel('录像定数').inputValue(), '13.9');
    assert.equal(
      await page.getByLabel('合集', { exact: true }).inputValue(),
      '8',
      'matching section',
    );
    assert.equal(await page.getByLabel('录像达成率').inputValue(), '', 'never inferred from PB');
    await page.getByLabel('录像达成率').fill('100.5000');
    await button(page, '生成预览').click();
    await page.getByAltText('16:9 投稿封面预览').waitFor({ timeout: 20000 });
    await page.getByAltText('4:3 投稿封面预览').waitFor();
    assert.equal(
      await page.locator('.publishing-title-comparison dd').nth(1).innerText(),
      '【maimai】Delta Drive Expert13.9 AP',
    );
    await page.getByText('将排在第 2 位，共 3 条', { exact: true }).waitFor();
    assert.equal(app.api.count('/api/bilibili/apply', 'POST'), 0, 'preview never publishes');

    // A lost apply result is retried from the stored job, not re-uploaded.
    app.api.state.bilibili.applyOutcome = 'uncertain';
    await button(page, '确认整理').click();
    await page.locator('.publishing-preview').getByText('合集排序 · 待确认').waitFor();
    await page.reload();
    await button(page, '继续整理').click();
    await page.getByAltText('16:9 投稿封面预览').waitFor();
    await button(page, '重试整理').click();
    await button(page, '已整理').waitFor();
    assert.equal(app.api.count('/api/bilibili/apply', 'POST'), 1);
    assert.equal(app.api.count('/api/bilibili/jobs/job-1/retry', 'POST'), 1);
    await button(page, '断开连接').click();
    await button(page, '扫码连接').waitFor();
  },

  async mobile(app) {
    const page = await app.open('/#ap', { width: 390, height: 844, mobile: true });
    await rows(page).first().waitFor();
    assert(await noHorizontalOverflow(page), 'AP list fits 390px');
    const nav = page.getByRole('navigation', { name: '主导航' }).getByRole('link');
    assert.equal(await nav.count(), 4);
    for (const box of await nav.evaluateAll((items) =>
      items.map((item) => item.getBoundingClientRect()),
    ))
      assert(box.width >= 44 && box.height >= 44, 'tappable navigation');
    await rows(page).filter({ hasText: 'Charlie Step' }).tap();
    await drawer(page).waitFor();
    await button(drawer(page), '关闭').tap();
    for (const hash of ['#statistics', '#settings', '#publishing']) {
      await page.goto(app.origin + '/' + hash);
      await page.locator('.main h1').first().waitFor();
      assert(await noHorizontalOverflow(page), `${hash} fits 390px`);
    }
    // Touch selects a coordinate on the location picker map.
    await page.goto(app.origin + '/#venues');
    await button(page, '新增机厅').tap();
    const editor = page.getByRole('dialog');
    await editor.getByLabel('搜索地点').fill('测试广场');
    await button(editor, '搜索').tap();
    await editor.getByRole('button', { name: /搜索结果广场/ }).tap();
    const box = await editor.locator('.venue-map-svg').boundingBox();
    await page.touchscreen.tap(box.x + box.width * 0.6, box.y + box.height * 0.5);
    await editor.locator('.venue-map-picked').waitFor();
    assert(await editor.evaluate((element) => element.scrollWidth <= element.clientWidth));
  },
};

await runSections(sections);
