// Legacy manual regression: requires local catalog/assets and a Vite server. Not a CI gate.
// Use the local Vite server for module imports; production remains on 8766.
import fs from 'node:fs/promises';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
const root = path.resolve(import.meta.dirname, '../..');
const output = path.join(root, 'output');
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
const report = {
  version: 6,
  createdAt: new Date().toISOString(),
  samples: [],
  browserErrors: errors,
  passed: false,
};
try {
  await page.goto(origin + '/#cover', { waitUntil: 'networkidle' });
  await page.evaluate(async () => {
    const cover = await import('/src/cover.ts');
    await cover.loadCoverFont();
    await document.fonts.ready;
  });
  const samples = [
    { slug: 'universe', id: 1314, difficulty: 3, score: '100.9615' },
    { slug: 'pink', id: 456, difficulty: 3, score: '101.0000' },
    { slug: 'pandora', id: 834, difficulty: 4, score: '101.0000' },
    { slug: 'long-title', id: 1722, difficulty: 3, score: '100.9876' },
  ];
  for (const sample of samples) {
    const result = await page.evaluate(
      async ({ catalog, sample }) => {
        const cover = await import('/src/cover.ts');
        const assert = (ok, message) => {
          if (!ok) throw new Error(`${sample.slug}: ${message}`);
        };
        const near = (a, b) => Math.abs(a - b) < 0.001;
        const song = catalog.songs.find((s) => s.id === sample.id);
        assert(song, 'song fixture missing');
        const source = song.charts.find(
          (c) => c.difficulty_index === sample.difficulty && c.type !== 'utage',
        );
        assert(source, 'chart fixture missing');
        const chart = {
          ...source,
          title: song.title,
          display_title: song.title,
          artist: song.artist,
          jacket_url: '/jackets/' + song.id + '.png',
        };
        const options = { achievement: sample.score, creator: 'MUST_NOT_RENDER_CREATOR' };
        const full = document.createElement('canvas'),
          crop = document.createElement('canvas');
        const calls = [];
        const original = CanvasRenderingContext2D.prototype.fillText;
        CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...args) {
          calls.push({ text, font: this.font, x, y });
          return original.call(this, text, x, y, ...args);
        };
        let receipt;
        try {
          receipt = await cover.renderCover(full, chart, options);
        } finally {
          CanvasRenderingContext2D.prototype.fillText = original;
        }
        const find = (role) => receipt.bounds.find((box) => box.role === role);
        const jacket = find('jacket'),
          score = find('achievement'),
          ap = find(sample.score === '101.0000' ? 'app' : 'ap');
        assert(jacket && score && ap, 'missing cover elements');
        assert(
          jacket.x === 320 && jacket.y === 382 && jacket.width === 470 && jacket.height === 470,
          'jacket geometry changed',
        );
        assert(
          near(score.y + score.height, 852) && near(ap.y + ap.height, 852),
          'score/AP visible bottom does not align with jacket',
        );
        assert(
          near(score.height, ap.height) && score.height <= 94,
          'score/AP visible heights differ',
        );
        assert(near(ap.x - score.x - score.width, 30), 'score/AP visible gap is not 30');
        assert(receipt.achievementStyle === 'sprite', 'achievement sprite unavailable');
        assert(receipt.constantStyle === 'styled_text', 'constant style mismatch');
        assert(
          receipt.gameAssets.length === 2 &&
            receipt.gameAssets.every((key) => ['standard', 'dx', 'ap', 'app'].includes(key)),
          'unexpected difficulty/level icon',
        );
        assert(
          !calls.some(
            (c) =>
              c.text === 'MUST_NOT_RENDER_CREATOR' ||
              c.text === song.artist ||
              c.text === 'Lv' ||
              c.text === '定数' ||
              c.text === String(song.id),
          ),
          'removed cover text rendered',
        );
        assert(
          !receipt.bounds.some((b) => /signature|author|artist|level|difficulty/.test(b.role)),
          'removed cover element rendered',
        );
        const prefixText = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'][
          chart.difficulty_index
        ];
        const prefix = calls.find((c) => c.text === prefixText),
          integer = calls.find((c) => c.text === chart.constant.toFixed(1).split('.')[0]);
        const decimal = calls.find((c) => c.text === '.' + chart.constant.toFixed(1).split('.')[1]);
        assert(prefix && integer && decimal, 'difficulty/constant text missing');
        assert(
          prefix.font.includes('52px') && integer.font === prefix.font && near(prefix.y, integer.y),
          'prefix and integer must share 52px font and baseline',
        );
        assert(
          decimal.font.includes('31px') && decimal.y > integer.y,
          'decimal must be smaller and lower',
        );
        assert(
          near(Math.min(find('constant_prefix').y, find('constant_integer').y), 382),
          'difficulty body top not aligned with jacket',
        );
        const plus = calls.find((c) => c.text === '+');
        assert(
          chart.level.includes('+')
            ? plus?.font.includes('28px') && !near(plus.y, integer.y)
            : !plus,
          'constant superscript mismatch',
        );
        assert(document.fonts.check('800 52px "CoverNewRodin"'), 'NewRodin not loaded');
        assert(!receipt.notices.includes('达成率暂用系统字体'), 'obsolete font notice');
        if (sample.difficulty === 4)
          assert(receipt.version.source === 'verified', 'ReMASTER chart release not verified');
        for (const box of receipt.bounds) {
          assert(
            box.x >= 264 && box.x + box.width <= 1656,
            `outside central safe area: ${box.role}`,
          );
          assert(box.y >= 0 && box.y + box.height <= 1080, `canvas overflow: ${box.role}`);
        }
        await cover.renderCover(crop, chart, { ...options, aspect: '4:3' });
        assert(
          full.width === 1920 &&
            crop.width === 1440 &&
            full.height === 1080 &&
            crop.height === 1080,
          'export dimensions',
        );
        const left = full.getContext('2d').getImageData(240, 0, 1440, 1080).data,
          right = crop.getContext('2d').getImageData(0, 0, 1440, 1080).data;
        assert(
          left.length === right.length && left.every((value, index) => value === right[index]),
          '4:3 is not exact central crop',
        );
        return {
          slug: sample.slug,
          title: song.title,
          chart: source,
          score: sample.score,
          score_is_sample: sample.slug !== 'universe',
          receipt,
          alignment: {
            jacketBottom: 852,
            scoreBottom: score.y + score.height,
            apBottom: ap.y + ap.height,
            scoreHeight: score.height,
            apHeight: ap.height,
            gap: ap.x - score.x - score.width,
          },
          typography: { prefix, integer, decimal, plus },
          full: full.toDataURL('image/png'),
          crop: crop.toDataURL('image/png'),
        };
      },
      { catalog, sample },
    );
    for (const [aspect, key] of [
      ['16x9', 'full'],
      ['4x3', 'crop'],
    ]) {
      await fs.writeFile(
        path.join(output, `cover-v6-${sample.slug}-${aspect}.png`),
        Buffer.from(result[key].split(',')[1], 'base64'),
      );
      delete result[key];
    }
    report.samples.push(result);
    console.log(
      `Rendered ${sample.slug}: 1920x1080 + 1440x1080; visible score/AP bottom 852, gap 30`,
    );
  }
  report.catalog = await page.evaluate(async (catalog) => {
    const cover = await import('/src/cover.ts');
    const assets = await import('/src/coverAssets.ts');
    await cover.loadCoverFont();
    await document.fonts.ready;
    const assert = (ok, message) => {
      if (!ok) throw new Error(message);
    };
    const ctx = document.createElement('canvas').getContext('2d');
    const results = catalog.songs.map((song) => {
      const layout = cover.fitTitle(ctx, song.title || '　');
      assert(
        layout.lines.join('').replace(/\s+/gu, '') === layout.text.replace(/\s+/gu, ''),
        `title characters lost: ${song.title}`,
      );
      assert(
        layout.widths.every((width) => width <= cover.COVER_LAYOUT.title.width),
        `title width: ${song.title}`,
      );
      assert(
        layout.lines.length * layout.lineHeight <= cover.COVER_LAYOUT.title.height,
        `title height: ${song.title}`,
      );
      return { id: song.id, title: song.title, ...layout };
    });
    const longest = (filter) =>
      results.filter(filter).sort((a, b) => [...b.title].length - [...a.title].length)[0];
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
    const remaster = {
      difficulty_index: 4,
      jp_release_version: null,
      jp_release_version_candidate: 'maimai',
      release_status: 'song_source',
    };
    assert(
      assets.resolveVersion(remaster, manifest).key === null,
      'unverified ReMASTER inherits song version',
    );
    assert(
      assets.resolveVersion(remaster, manifest, 'pink').key === 'pink',
      'manual version override unavailable',
    );
    assert(
      assets.resolveVersion(
        {
          difficulty_index: 3,
          jp_release_version: null,
          jp_release_version_candidate: 'PiNK',
          release_status: 'cn_source_candidate',
        },
        manifest,
      ).key === null,
      'unverified CN candidate assigned',
    );
    const glyphs = Object.fromEntries(
      [...'01.%'].map((character, index) => [
        character,
        { x: index * 10, y: 0, width: 10, height: 20, advance: 12 },
      ]),
    );
    const fixture = { file: 'test-only', glyphs, line_height: 20 };
    assert(
      cover.spriteNumberLayout(fixture, '01.%', 40).width === 92,
      'sprite width ignores last glyph extent',
    );
    assert(
      cover.spriteNumberLayout(
        { glyphs: { 0: { width: 10, height: 20, advance: 6 } }, line_height: 20 },
        '00',
        40,
      ).width === 32,
      'overlapping advance clips final glyph',
    );
    assert(cover.spriteNumberLayout(fixture, '2', 40) === null, 'missing glyph fallback');
    return {
      titleCount: results.length,
      minTitleFont: Math.min(...results.map((r) => r.fontSize)),
      font: 'CoverNewRodin',
      fontLoadedBeforeScan: document.fonts.check('800 52px "CoverNewRodin"'),
      extremal,
      versionRulesPassed: true,
      spriteGeometryPassed: true,
    };
  }, catalog);
  report.utage = await page.evaluate(async (catalog) => {
    const cover = await import('/src/cover.ts');
    await cover.loadCoverFont();
    const assert = (ok, message) => {
      if (!ok) throw new Error(message);
    };
    const near = (a, b) => Math.abs(a - b) < 0.001;
    const results = [];
    for (const fixture of [
      { key: '111597:utage:0', level: '13+?', suffix: '+?' },
      { key: '100230:utage:0', level: '13?', suffix: '?' },
    ]) {
      const song = catalog.songs.find((song) =>
        song.charts.some((chart) => chart.chart_key === fixture.key),
      );
      const source = song?.charts.find((chart) => chart.chart_key === fixture.key);
      assert(
        source && source.constant === null && source.level === fixture.level,
        `UTAGE fixture changed: ${fixture.key}`,
      );
      const chart = {
        ...source,
        title: song.title,
        display_title: song.title,
        artist: song.artist,
        jacket_url: '/jackets/' + song.id + '.png',
      };
      const full = document.createElement('canvas'),
        crop = document.createElement('canvas');
      const options = { achievement: '101.0000', versionOverride: 'buddies', safeGuides: false };
      const calls = [];
      const original = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.fillText = function (text, x, y, ...args) {
        calls.push({ text, font: this.font, x, y });
        return original.call(this, text, x, y, ...args);
      };
      let receipt;
      try {
        receipt = await cover.renderCover(full, chart, options);
      } finally {
        CanvasRenderingContext2D.prototype.fillText = original;
      }
      const prefix = calls.find((call) => call.text === 'Utage'),
        integer = calls.find((call) => call.text === '13');
      const suffix = calls.filter((call) => call.text === fixture.suffix);
      assert(
        prefix &&
          integer &&
          integer.font.includes('52px') &&
          prefix.font === integer.font &&
          near(prefix.y, integer.y),
        `UTAGE prefix/integer baseline or size: ${fixture.key}`,
      );
      assert(
        suffix.length === 1 &&
          suffix[0].font.includes('28px') &&
          suffix[0].x > integer.x &&
          suffix[0].y < integer.y,
        `UTAGE suffix must be one 28px superscript: ${fixture.key}`,
      );
      assert(
        !calls.some((call) => call.text === fixture.level || /^\.\d+$/.test(call.text)),
        `UTAGE has inline uncertainty or fabricated decimal: ${fixture.key}`,
      );
      if (fixture.suffix === '+?')
        assert(
          !calls.some((call) => call.text === '+' || call.text === '?'),
          `UTAGE +? must remain one suffix: ${fixture.key}`,
        );
      assert(
        !receipt.bounds.some((box) => box.role === 'constant_decimal'),
        `UTAGE fake decimal bounds: ${fixture.key}`,
      );
      assert(
        chart.constant === null && source.constant === null,
        `UTAGE constant was changed: ${fixture.key}`,
      );
      assert(
        !receipt.gameAssets.some((key) => ['standard', 'dx', 'basic'].includes(key)) &&
          !receipt.bounds.some((box) => ['standard', 'dx', 'basic'].includes(box.role)),
        `UTAGE rendered a STD/DX/BASIC badge: ${fixture.key}`,
      );
      assert(
        !calls.some((call) => ['STD', 'DX', 'Lv', '定数'].includes(call.text)),
        `UTAGE rendered removed labels: ${fixture.key}`,
      );
      assert(
        receipt.achievementStyle === 'sprite' && receipt.gameAssets.includes('app'),
        `UTAGE AP+ or gold score missing: ${fixture.key}`,
      );
      const jacket = receipt.bounds.find((box) => box.role === 'jacket'),
        score = receipt.bounds.find((box) => box.role === 'achievement'),
        ap = receipt.bounds.find((box) => box.role === 'app');
      assert(
        jacket &&
          score &&
          ap &&
          near(score.y + score.height, 852) &&
          near(ap.y + ap.height, 852) &&
          near(score.height, ap.height) &&
          near(ap.x - score.x - score.width, 30),
        `UTAGE score/AP alignment: ${fixture.key}`,
      );
      assert(
        receipt.title.lines.join('').replace(/\s+/gu, '') === song.title.replace(/\s+/gu, ''),
        `UTAGE title lost characters: ${fixture.key}`,
      );
      for (const box of receipt.bounds)
        assert(
          box.x >= 264 && box.x + box.width <= 1656 && box.y >= 0 && box.y + box.height <= 1080,
          `UTAGE unsafe element ${box.role}: ${fixture.key}`,
        );
      await cover.renderCover(crop, chart, { ...options, aspect: '4:3' });
      assert(
        full.width === 1920 && full.height === 1080 && crop.width === 1440 && crop.height === 1080,
        `UTAGE dimensions: ${fixture.key}`,
      );
      const fullPixels = full.getContext('2d').getImageData(240, 0, 1440, 1080).data,
        cropPixels = crop.getContext('2d').getImageData(0, 0, 1440, 1080).data;
      assert(
        fullPixels.every((value, index) => value === cropPixels[index]),
        `UTAGE not exact central crop: ${fixture.key}`,
      );
      results.push({
        chart_key: fixture.key,
        title: song.title,
        constant: chart.constant,
        level: chart.level,
        test_achievement: options.achievement,
        typography: { prefix, integer, suffix: suffix[0] },
        receipt,
        passed: true,
      });
    }
    return results;
  }, catalog);
  if (await page.locator('#cover-creator').count()) throw new Error('creator control still exists');
  report.passed = errors.length === 0;
  await fs.writeFile(
    path.join(output, 'cover-v6-validation.json'),
    JSON.stringify(report, null, 2),
  );
  console.log(
    JSON.stringify(
      {
        passed: report.passed,
        samples: report.samples.length,
        utage: report.utage.length,
        titles: report.catalog.titleCount,
        minTitleFont: report.catalog.minTitleFont,
        browserErrors: errors,
      },
      null,
      2,
    ),
  );
  if (!report.passed) process.exitCode = 1;
} catch (error) {
  report.failure = error.stack || String(error);
  await fs.writeFile(
    path.join(output, 'cover-v6-validation.json'),
    JSON.stringify(report, null, 2),
  );
  throw error;
} finally {
  await browser.close();
}
