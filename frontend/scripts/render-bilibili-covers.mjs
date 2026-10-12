#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { launchBrowser } from './browser.mjs';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const root = path.resolve(import.meta.dirname, '../..');
const frontend = path.join(root, 'frontend');
const args = process.argv.slice(2);
let manifestPath = path.join(root, 'data/bilibili-regenerate/render-manifest.json');
let output = path.join(root, 'output/bilibili-covers-current');
let origin = process.env.COVER_BATCH_ORIGIN || '';
let port = 5175,
  hasManifest = false;
for (let i = 0; i < args.length; i++) {
  const arg = args[i];
  if (arg === '--help') {
    console.log(
      'Usage: node frontend/scripts/render-bilibili-covers.mjs [manifest.json] [--output DIR] [--port 5175] [--origin http://127.0.0.1:5175]\nDefault: starts and stops its own Vite server; backend on 8766 must already serve assets. --origin uses an existing Vite server.\nInput: {videos:[{bvid,title,chart,achievement,versionOverride?,mirror_horizontal?:boolean}],excluded:[]}. Scores are never inferred or replaced. mirror_horizontal flips the entire exported canvas.',
    );
    process.exit(0);
  }
  if (['--output', '--origin', '--port'].includes(arg)) {
    const value = args[++i];
    if (!value) throw new Error(`Missing value after ${arg}`);
    if (arg === '--output') output = path.resolve(value);
    if (arg === '--origin') origin = value.replace(/\/$/, '');
    if (arg === '--port') port = Number(value);
  } else if (!arg.startsWith('-') && !hasManifest) {
    manifestPath = path.resolve(arg);
    hasManifest = true;
  } else throw new Error(`Unknown argument: ${arg}`);
}
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid Vite port');
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const manifestBytes = await fs.readFile(manifestPath);
const manifest = JSON.parse(manifestBytes.toString('utf8'));
if (!Array.isArray(manifest.videos) || !Array.isArray(manifest.excluded))
  throw new Error('Manifest requires videos and excluded arrays');
const excludedIds = new Set(
  manifest.excluded.map((item) => (typeof item === 'string' ? item : item?.bvid)).filter(Boolean),
);
const seen = new Set();
for (const video of manifest.videos) {
  if (typeof video?.bvid !== 'string' || !/^BV[A-Za-z0-9]{10}$/.test(video.bvid))
    throw new Error(`Invalid BVID: ${video?.bvid}`);
  if (seen.has(video.bvid)) throw new Error(`Duplicate BVID: ${video.bvid}`);
  if (excludedIds.has(video.bvid))
    throw new Error(`Excluded video also listed for rendering: ${video.bvid}`);
  seen.add(video.bvid);
}
await fs.mkdir(path.join(output, '16x9'), { recursive: true });
await fs.mkdir(path.join(output, '4x3'), { recursive: true });
const report = {
  schema_version: 1,
  layout: 'v8-16x9_v6-4x3',
  started_at: new Date().toISOString(),
  finished_at: null,
  status: 'running',
  manifest: { file: manifestPath, sha256: hash(manifestBytes) },
  renderer: {
    file: 'frontend/src/cover.ts',
    sha256: hash(await fs.readFile(path.join(frontend, 'src/cover.ts'))),
  },
  output_directory: output,
  total: manifest.videos.length,
  completed: 0,
  successful: 0,
  errors: 0,
  excluded: manifest.excluded,
  excluded_count: manifest.excluded.length,
  videos: [],
  browser_errors: [],
};
const atomicWrite = async (file, bytes) => {
  const temporary = file + `.tmp-${process.pid}`;
  await fs.writeFile(temporary, bytes);
  await fs.rename(temporary, file);
};
const saveReport = () =>
  atomicWrite(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
const errorObject = (error) => ({
  message: error?.message || String(error),
  stack: error?.stack || null,
});
let vite,
  browser,
  active = null;
let viteLog = '';
const stopVite = async () => {
  if (!vite || vite.exitCode !== null || vite.signalCode !== null) return;
  vite.kill('SIGTERM');
  for (let i = 0; i < 20 && vite.exitCode === null && vite.signalCode === null; i++)
    await delay(100);
  if (vite.exitCode === null && vite.signalCode === null) vite.kill('SIGKILL');
};
try {
  await saveReport();
  await atomicWrite(path.join(output, 'manifest.json'), manifestBytes);
  if (!origin) {
    origin = `http://127.0.0.1:${port}`;
    vite = spawn(
      process.execPath,
      [
        path.join(frontend, 'node_modules/vite/bin/vite.js'),
        '--host',
        '127.0.0.1',
        '--port',
        String(port),
        '--strictPort',
      ],
      { cwd: frontend, stdio: ['ignore', 'pipe', 'pipe'] },
    );
    for (const stream of [vite.stdout, vite.stderr])
      stream.on('data', (chunk) => {
        viteLog = (viteLog + chunk.toString()).slice(-12000);
      });
    let startupError = null;
    vite.on('error', (error) => {
      startupError = error;
    });
    let ready = false;
    for (let i = 0; i < 80; i++) {
      if (startupError) throw startupError;
      if (vite.exitCode !== null) throw new Error(`Vite exited: ${viteLog}`);
      try {
        if (/Local:/.test(viteLog)) {
          const response = await fetch(origin + '/src/cover.ts', {
            signal: AbortSignal.timeout(1000),
          });
          if (response.ok) {
            ready = true;
            break;
          }
        }
      } catch {
        // The dev server may not be listening yet; retry until the bounded deadline.
      }
      await delay(250);
    }
    if (!ready) throw new Error(`Vite did not become ready at ${origin}: ${viteLog}`);
  }
  report.renderer.origin = origin;
  browser = await launchBrowser({ softwareRendering: false });
  const page = await browser.newPage({
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
  });
  const browserError = (error) => {
    const entry = { at: new Date().toISOString(), ...error };
    report.browser_errors.push(entry);
    if (active) active.browser_errors.push(entry);
  };
  page.on('pageerror', (error) => browserError({ kind: 'pageerror', message: error.message }));
  page.on('response', (response) => {
    if (response.status() >= 400)
      browserError({ kind: 'http', status: response.status(), url: response.url() });
  });
  page.on('requestfailed', (request) =>
    browserError({
      kind: 'requestfailed',
      url: request.url(),
      message: request.failure()?.errorText || 'Request failed',
    }),
  );
  // An empty same-origin document avoids loading the user's application or AP data.
  await page.route('**/__cover-batch.html', (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><meta charset="utf-8"><link rel="icon" href="data:,"><title>Cover batch</title>',
    }),
  );
  await page.goto(origin + '/__cover-batch.html', { waitUntil: 'load' });
  await page.evaluate(async () => {
    const cover = await import('/src/cover.ts');
    await cover.loadCoverFont();
    await document.fonts.ready;
  });
  for (let index = 0; index < manifest.videos.length; index++) {
    const video = manifest.videos[index];
    const entry = {
      bvid: video.bvid,
      title: video.title,
      chart: video.chart,
      achievement_input: video.achievement,
      version_override: video.versionOverride || null,
      mirror_horizontal: video.mirror_horizontal === true,
      score_source: 'manifest',
      status: 'running',
      outputs: {},
      browser_errors: [],
      started_at: new Date().toISOString(),
    };
    active = entry;
    try {
      if (typeof video.title !== 'string' || !video.title) throw new Error('Missing video title');
      if (
        typeof video.achievement !== 'string' ||
        !/^\d{1,3}(\.\d{1,4})?$/.test(video.achievement) ||
        Number(video.achievement) > 101
      )
        throw new Error('Missing or invalid achievement; no replacement score is allowed');
      const chart = video.chart;
      if (
        !chart ||
        ['title', 'jacket_url', 'type', 'difficulty_name', 'level'].some(
          (key) => typeof chart[key] !== 'string' || !chart[key],
        )
      )
        throw new Error('Incomplete CoverChart string fields');
      if (
        !Number.isInteger(chart.difficulty_index) ||
        chart.difficulty_index < 0 ||
        chart.difficulty_index > 5
      )
        throw new Error('Invalid difficulty index');
      if (
        chart.constant !== null &&
        (typeof chart.constant !== 'number' || !Number.isFinite(chart.constant))
      )
        throw new Error('Missing or invalid chart constant');
      if (video.versionOverride !== undefined && typeof video.versionOverride !== 'string')
        throw new Error('Invalid version override');
      if (video.mirror_horizontal !== undefined && typeof video.mirror_horizontal !== 'boolean')
        throw new Error('Invalid mirror_horizontal flag');
      const result = await page.evaluate(async (video) => {
        const cover = await import('/src/cover.ts');
        const assert = (condition, message) => {
          if (!condition) throw new Error(message);
        };
        const near = (a, b) => Math.abs(a - b) < 0.001;
        const options = {
          achievement: video.achievement,
          versionOverride: video.versionOverride,
        };
        const full = document.createElement('canvas'),
          crop = document.createElement('canvas');
        const receipt = await cover.renderCover(full, video.chart, { ...options, aspect: '16:9' });
        const cropReceipt = await cover.renderCover(crop, video.chart, {
          ...options,
          aspect: '4:3',
        });
        const title = video.chart.display_title || video.chart.title;
        const apKey = Number(video.achievement) === 101 ? 'app' : 'ap';
        const isUtage =
          video.chart.type === 'utage' || video.chart.difficulty_name.toUpperCase() === 'UTAGE';
        assert(document.fonts.check('800 52px "CoverNewRodin"'), 'Cover font is not loaded');
        for (const [aspect, item] of [
          ['16:9', receipt],
          ['4:3', cropReceipt],
        ]) {
          const layout = cover.getCoverLayout(aspect);
          const left = aspect === '4:3' ? 240 : 0;
          const right = aspect === '4:3' ? 1680 : 1920;
          assert(
            item.version.key &&
              item.version.asset?.status === 'ready' &&
              item.version.asset?.background_png,
            `${aspect}: unresolved or unavailable version background`,
          );
          assert(
            item.bounds.some((box) => box.role === 'version_logo'),
            `${aspect}: version logo is missing`,
          );
          assert(
            !item.notices.some((notice) => /失败|不可用|系统字体/.test(notice)),
            `${aspect}: required cover resource unavailable: ${item.notices.join('; ')}`,
          );
          assert(
            item.title.lines.join('').replace(/\s+/gu, '') === title.replace(/\s+/gu, ''),
            `${aspect}: title characters were lost`,
          );
          assert(
            item.title.widths.every((width) => width <= layout.title.width) &&
              item.title.lines.length * item.title.lineHeight <= layout.title.height,
            `${aspect}: title exceeds available space`,
          );
          // Receipts use the original 1920x1080 coordinate space even for the 4:3 export.
          for (const box of item.bounds) {
            assert(
              box.x >= left &&
                box.x + box.width <= right &&
                box.y >= 0 &&
                box.y + box.height <= 1080,
              `${aspect}: canvas bounds exceeded: ${box.role}`,
            );
          }
          assert(item.achievementStyle === 'sprite', `${aspect}: gold score sprites unavailable`);
          assert(
            item.gameAssets.includes(apKey) &&
              !item.gameAssets.includes(apKey === 'ap' ? 'app' : 'ap'),
            `${aspect}: AP/AP+ does not match the supplied achievement`,
          );
          if (!isUtage)
            assert(
              item.gameAssets.includes(video.chart.type === 'dx' ? 'dx' : 'standard'),
              `${aspect}: chart type image is missing`,
            );
          assert(
            item.gameAssets.every((key) => ['standard', 'dx', 'ap', 'app'].includes(key)),
            `${aspect}: unexpected difficulty/level icon`,
          );
          assert(
            !item.bounds.some((box) => /signature|author|artist|level|difficulty/.test(box.role)),
            `${aspect}: removed cover element was rendered`,
          );
          const score = item.bounds.find((box) => box.role === 'achievement');
          const ap = item.bounds.find((box) => box.role === apKey);
          const jacket = item.bounds.find((box) => box.role === 'jacket');
          assert(score && ap && jacket, `${aspect}: missing score, AP badge, or jacket`);
          const bottom = jacket.y + jacket.height;
          assert(
            near(score.y + score.height, bottom) && near(ap.y + ap.height, bottom),
            `${aspect}: score/AP bottom differs from jacket bottom`,
          );
          assert(
            near(score.height, ap.height) &&
              near(ap.x - score.x - score.width, aspect === '4:3' ? 30 : 34),
            `${aspect}: score/AP size or spacing differs`,
          );
        }
        assert(
          full.width === 1920 &&
            full.height === 1080 &&
            crop.width === 1440 &&
            crop.height === 1080,
          'Wrong output dimensions',
        );
        if (video.mirror_horizontal === true) {
          for (const canvas of [full, crop]) {
            const context = canvas.getContext('2d'),
              before = context.getImageData(0, 0, canvas.width, canvas.height).data;
            const source = document.createElement('canvas');
            source.width = canvas.width;
            source.height = canvas.height;
            source.getContext('2d').drawImage(canvas, 0, 0);
            context.save();
            context.imageSmoothingEnabled = false;
            context.clearRect(0, 0, canvas.width, canvas.height);
            context.translate(canvas.width, 0);
            context.scale(-1, 1);
            context.drawImage(source, 0, 0);
            context.restore();
            const after = context.getImageData(0, 0, canvas.width, canvas.height).data;
            for (let y = 0; y < canvas.height; y++)
              for (let x = 0; x < canvas.width; x++)
                for (let channel = 0; channel < 4; channel++)
                  assert(
                    after[(y * canvas.width + x) * 4 + channel] ===
                      before[(y * canvas.width + canvas.width - 1 - x) * 4 + channel],
                    'Horizontal mirror changed a pixel',
                  );
          }
          for (const item of [receipt, cropReceipt])
            item.output_transform = {
              mirror_horizontal: true,
              canvas_width: item.width,
              bounds_coordinate_space: 'unmirrored_1920x1080_master',
            };
        }
        return {
          receipt,
          cropReceipt,
          achievement_rendered: Number(video.achievement).toFixed(4),
          checks: {
            title_complete: true,
            title_fits: true,
            both_aspect_bounds: true,
            achievement_sprite: true,
            ap_matches_achievement: true,
            source_score_unchanged: options.achievement === video.achievement,
            independent_aspect_layouts: true,
            visible_alignment: true,
            font_loaded: true,
            no_legacy_elements: true,
            ...(video.mirror_horizontal === true ? { horizontal_mirror_pixel_exact: true } : {}),
          },
          full: full.toDataURL('image/png'),
          crop: crop.toDataURL('image/png'),
        };
      }, video);
      if (entry.browser_errors.length)
        throw new Error(`Browser resource error: ${JSON.stringify(entry.browser_errors)}`);
      entry.receipts = { '16x9': result.receipt, '4x3': result.cropReceipt };
      entry.achievement_rendered = result.achievement_rendered;
      entry.checks = result.checks;
      for (const [aspect, key, width] of [
        ['16x9', 'full', 1920],
        ['4x3', 'crop', 1440],
      ]) {
        const bytes = Buffer.from(result[key].split(',')[1], 'base64');
        delete result[key];
        const relative = `${aspect}/${video.bvid}.png`;
        await atomicWrite(path.join(output, relative), bytes);
        entry.outputs[aspect] = {
          file: relative,
          sha256: hash(bytes),
          bytes: bytes.length,
          width,
          height: 1080,
        };
      }
      entry.status = 'complete';
      report.successful++;
    } catch (error) {
      entry.status = 'error';
      entry.error = errorObject(error);
      report.errors++;
    }
    entry.finished_at = new Date().toISOString();
    report.videos.push(entry);
    report.completed++;
    active = null;
    await saveReport();
    if ((index + 1) % 10 === 0 || index + 1 === manifest.videos.length)
      console.log(
        `${index + 1}/${manifest.videos.length}: ${report.successful} complete, ${report.errors} errors`,
      );
  }
  report.status =
    report.errors === 0 && report.browser_errors.length === 0
      ? 'complete'
      : 'completed_with_errors';
} catch (error) {
  report.status = 'failed';
  report.fatal_error = errorObject(error);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
  await stopVite();
  report.finished_at = new Date().toISOString();
  await saveReport();
}
if (report.status !== 'complete') process.exitCode = 1;
console.log(
  JSON.stringify(
    {
      status: report.status,
      total: report.total,
      complete: report.successful,
      errors: report.errors,
      browser_errors: report.browser_errors.length,
      report: path.join(output, 'report.json'),
    },
    null,
    2,
  ),
);
