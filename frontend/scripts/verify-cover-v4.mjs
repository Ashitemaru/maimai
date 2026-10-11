// Legacy manual regression: requires local catalog/assets and a Vite server. Not a CI gate.
// Run with the local Vite dev server on port 5174; production is served independently on 8766.
import fs from 'node:fs/promises';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
const root = path.resolve(import.meta.dirname, '../..');
const catalog = JSON.parse(await fs.readFile(path.join(root, 'data/catalog/catalog.json'), 'utf8'));
const origin = process.env.COVER_QA_ORIGIN || 'http://127.0.0.1:5174';
const browser = await launchBrowser();
const page = await browser.newPage({
  viewport: { width: 1600, height: 1000 },
  deviceScaleFactor: 1,
});
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('response', (r) => {
  if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`);
});
try {
  await page.goto(origin + '/#cover', { waitUntil: 'networkidle' });
  const report = await page.evaluate(async (catalog) => {
    const cover = await import('/src/cover.ts');
    const assets = await import('/src/coverAssets.ts');
    const assert = (test, message) => {
      if (!test) throw new Error(message);
    };
    const ctx = document.createElement('canvas').getContext('2d');
    const titleResults = catalog.songs.map((song) => {
      const layout = cover.fitTitle(ctx, song.title || '　');
      assert(
        layout.lines.join('').replace(/\s+/gu, '') === layout.text.replace(/\s+/gu, ''),
        `lost title characters: ${song.title}`,
      );
      assert(
        layout.widths.every((w) => w <= cover.COVER_LAYOUT.title.width),
        `title width: ${song.title}`,
      );
      assert(
        layout.lines.length * layout.lineHeight <= cover.COVER_LAYOUT.title.height,
        `title height: ${song.title}`,
      );
      return { id: song.id, title: song.title, ...layout };
    });
    const longest = (filter) =>
      titleResults.filter(filter).sort((a, b) => [...b.title].length - [...a.title].length)[0];
    const extremal = {
      english: longest(
        (r) =>
          r.title.length > 0 &&
          [...r.title].every((character) => character.charCodeAt(0) <= 127) &&
          /[A-Za-z]/.test(r.title),
      ),
      japanese: longest((r) => /[\u3040-\u30ff]/u.test(r.title)),
      symbols: longest((r) => /[†★☆◆◇→↑↓▽△×∞～「」]/u.test(r.title)),
    };
    const manifest = await assets.loadBackgroundManifest();
    const allURLs = [
      ...new Set(
        Object.values(manifest.version_map).flatMap((a) =>
          [a.logo_file, a.background_png].filter(Boolean),
        ),
      ),
    ].map((file) => assets.assetURL(file));
    const images = await Promise.all(
      allURLs.map(
        (url) =>
          new Promise((resolve) => {
            const img = new Image();
            img.onload = () => resolve({ url, width: img.naturalWidth, height: img.naturalHeight });
            img.onerror = () => resolve({ url, error: true });
            img.src = url;
          }),
      ),
    );
    assert(
      images.every((i) => !i.error),
      'missing version image',
    );
    const unresolved = {
      difficulty_index: 4,
      jp_release_version: null,
      jp_release_version_candidate: 'maimai',
      release_status: 'song_source',
    };
    assert(
      assets.resolveVersion(unresolved, manifest).key === null,
      'ReMASTER must not inherit song version',
    );
    assert(
      assets.resolveVersion(unresolved, manifest, 'pink').key === 'pink',
      'manual version override',
    );
    const cnOnly = {
      difficulty_index: 3,
      jp_release_version: null,
      jp_release_version_candidate: 'PiNK',
      release_status: 'cn_source_candidate',
    };
    assert(
      assets.resolveVersion(cnOnly, manifest).key === null,
      'CN candidate must require manual version',
    );
    const toChart = (song, c) => ({
      ...c,
      title: song.title,
      display_title: song.title,
      artist: song.artist,
      jacket_url: '/jackets/' + song.id + '.png',
    });
    const master = (id) => {
      const song = catalog.songs.find((s) => s.id === id);
      return toChart(
        song,
        song.charts.find((c) => c.difficulty_index === 3 && c.type !== 'utage'),
      );
    };
    const actualRemaster = catalog.songs.flatMap((s) =>
      s.charts
        .filter((c) => c.difficulty_index === 4 && c.jp_release_version)
        .map((c) => toChart(s, c)),
    )[0];
    assert(actualRemaster, 'no actual Remaster fixture');
    const remasterCanvas = document.createElement('canvas');
    const remasterReceipt = await cover.renderCover(remasterCanvas, actualRemaster, {
      achievement: '100.9876',
      creator: 'Ashitemaru',
    });
    assert(remasterReceipt.version.source === 'verified', 'actual Remaster is not verified');
    assert(remasterReceipt.gameAssets.includes('remaster'), 'Remaster badge missing');
    const utageSong = catalog.songs.find((s) => s.charts.some((c) => c.type === 'utage'));
    const utageChart = toChart(
      utageSong,
      utageSong.charts.find((c) => c.type === 'utage'),
    );
    const utageReceipt = await cover.renderCover(document.createElement('canvas'), utageChart, {
      achievement: '101.0000',
      versionOverride: 'buddies',
    });
    assert(
      utageReceipt.gameAssets.includes('utage') && !utageReceipt.gameAssets.includes('basic'),
      'UTAGE is Basic',
    );
    const longSong = catalog.songs.find((s) => s.id === extremal.japanese.id);
    const longChart = toChart(
      longSong,
      longSong.charts.find((c) => c.difficulty_index === 3) || longSong.charts[0],
    );
    const referenceSong = catalog.songs.find((s) => s.id === 834);
    const referenceChart = toChart(
      referenceSong,
      referenceSong.charts.find((c) => c.difficulty_index === 4),
    );
    const samples = [
      { slug: 'pandora', chart: referenceChart, score: '101.0000' },
      { slug: 'pink', chart: master(456), score: '101.0000' },
      { slug: 'universe', chart: master(1314), score: '100.9615' },
      { slug: 'long-title', chart: longChart, score: '100.9876' },
    ];
    const exported = [];
    let checkedBounds = 0;
    for (const sample of samples) {
      const options = { achievement: sample.score, creator: 'Ashitemaru' };
      const full = document.createElement('canvas'),
        cropped = document.createElement('canvas');
      const texts = [];
      const original = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
        texts.push(text);
        return original.call(this, text, ...args);
      };
      let receipt;
      try {
        receipt = await cover.renderCover(full, sample.chart, options);
      } finally {
        CanvasRenderingContext2D.prototype.fillText = original;
      }
      assert(!texts.includes(sample.chart.artist), 'artist rendered on cover');
      assert(
        !texts.includes('Ashitemaru') && !receipt.bounds.some((b) => b.role === 'signature'),
        'legacy creator is rendered',
      );
      assert(cover.COVER_LAYOUT.jacket.width >= 660, 'jacket not enlarged');
      assert(
        receipt.bounds.find((b) => b.role === 'version_logo').y >=
          cover.COVER_LAYOUT.header.y + cover.COVER_LAYOUT.header.height,
        'logo is outside integrated card',
      );
      assert(
        receipt.gameAssets.includes(sample.score === '101.0000' ? 'app' : 'ap'),
        'AP icon mismatch',
      );
      for (const box of receipt.bounds) {
        assert(
          box.x >= 264 && box.x + box.width <= 1656,
          `${sample.slug} outside 4:3 safe area: ${box.role}`,
        );
        assert(box.y >= 0 && box.y + box.height <= 1080, `canvas overflow: ${box.role}`);
        checkedBounds++;
      }
      await cover.renderCover(cropped, sample.chart, { ...options, aspect: '4:3' });
      assert(
        full.width === 1920 &&
          cropped.width === 1440 &&
          full.height === 1080 &&
          cropped.height === 1080,
        'export dimensions',
      );
      const left = full.getContext('2d').getImageData(240, 0, 1440, 1080).data;
      const right = cropped.getContext('2d').getImageData(0, 0, 1440, 1080).data;
      assert(
        left.length === right.length && left.every((v, i) => v === right[i]),
        '4:3 not exact central crop',
      );
      exported.push({
        slug: sample.slug,
        title: sample.chart.title,
        chart: sample.chart,
        score: sample.score,
        score_is_sample: sample.slug !== 'universe',
        receipt,
        full: full.toDataURL('image/png'),
        crop: cropped.toDataURL('image/png'),
      });
    }
    const glyphs = Object.fromEntries(
      [...'01.%+'].map((ch, i) => [ch, { x: i * 10, y: 0, width: 10, height: 20, advance: 12 }]),
    );
    const fixture = { file: 'test-only', glyphs, line_height: 20 };
    assert(cover.spriteNumberLayout(fixture, '01.%', 40).width === 96, 'sprite width/advance');
    assert(cover.spriteNumberLayout(fixture, '2', 40) === null, 'missing glyph fallback');
    return {
      titleCount: titleResults.length,
      minTitleFont: Math.min(...titleResults.map((r) => r.fontSize)),
      extremal,
      versionImages: images.length,
      checkedBounds,
      actualRemaster: { chart: actualRemaster, version: remasterReceipt.version },
      utage: { chart: utageChart, badges: utageReceipt.gameAssets },
      exported,
    };
  }, catalog);
  for (const sample of report.exported) {
    await fs.writeFile(
      path.join(root, `output/cover-v4-${sample.slug}-16x9.png`),
      Buffer.from(sample.full.split(',')[1], 'base64'),
    );
    await fs.writeFile(
      path.join(root, `output/cover-v4-${sample.slug}-4x3.png`),
      Buffer.from(sample.crop.split(',')[1], 'base64'),
    );
    delete sample.full;
    delete sample.crop;
  }
  if (await page.locator('#cover-creator').count()) throw new Error('creator control still exists');
  await page.getByLabel('4:3 安全线').check();
  await page.getByRole('button', { name: '4:3', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('canvas').width === 1440);
  await page.screenshot({
    path: path.join(root, 'output/cover-v4-workshop-desktop.png'),
    fullPage: true,
  });
  await page.getByRole('button', { name: '16:9', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('canvas').width === 1920);
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出封面', exact: true }).click();
  const download = await downloadPromise;
  await download.saveAs(path.join(root, 'output/cover-v4-download-16x9.png'));
  const mobile = await browser.newPage({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 1,
  });
  await mobile.goto(origin + '/#cover', { waitUntil: 'networkidle' });
  await mobile.waitForFunction(() => document.querySelector('canvas')?.width === 1920);
  await mobile.screenshot({
    path: path.join(root, 'output/cover-v4-workshop-mobile.png'),
    fullPage: true,
  });
  await mobile.close();
  report.browserErrors = errors;
  report.downloadFilename = download.suggestedFilename();
  report.passed = errors.length === 0;
  await fs.writeFile(
    path.join(root, 'output/cover-v4-validation.json'),
    JSON.stringify(report, null, 2),
  );
  console.log(
    JSON.stringify(
      {
        titles: report.titleCount,
        minFont: report.minTitleFont,
        longest: report.extremal,
        versionImages: report.versionImages,
        bounds: report.checkedBounds,
        errors,
      },
      null,
      2,
    ),
  );
  if (errors.length) process.exitCode = 1;
} finally {
  await browser.close();
}
