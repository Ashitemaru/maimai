#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { launchBrowser } from './browser.mjs';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const root = path.resolve(import.meta.dirname, '../..');
const frontend = path.join(root, 'frontend');
const versionArg = process.argv.indexOf('--version');
const version = versionArg >= 0 ? process.argv[versionArg + 1] : 'v7';
if (!['v7', 'v8'].includes(version)) throw new Error('Unsupported layout version');
const output = path.join(root, 'output/bilibili-covers-' + version);
const previous = path.join(root, 'output/bilibili-covers-v6');
const manifest = JSON.parse(
  await fs.readFile(path.join(root, 'data/bilibili-regenerate/render-manifest.json'), 'utf8'),
);
const sample = process.argv.includes('--samples');
const videos = sample
  ? manifest.videos.filter((_, index) => [0, 1, 17, 19].includes(index))
  : manifest.videos;
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const origin = 'http://127.0.0.1:5175';
await fs.mkdir(path.join(output, '16x9'), { recursive: true });
await fs.mkdir(path.join(output, '4x3'), { recursive: true });
const report = {
  layout: version + '-16x9_v6-4x3',
  started_at: new Date().toISOString(),
  total: videos.length,
  successful: 0,
  errors: [],
  videos: [],
  preserved_4x3_from: previous,
};
let browser;
const vite = spawn(
  process.execPath,
  [
    path.join(frontend, 'node_modules/vite/bin/vite.js'),
    '--host',
    '127.0.0.1',
    '--port',
    '5175',
    '--strictPort',
  ],
  { cwd: frontend, stdio: 'ignore' },
);
try {
  let ready = false;
  for (let i = 0; i < 60; i++) {
    try {
      const response = await fetch(origin + '/src/cover.ts', { signal: AbortSignal.timeout(600) });
      if (response.ok) {
        ready = true;
        break;
      }
    } catch {
      // The dev server may not be listening yet; retry until the bounded deadline.
    }
    await delay(200);
  }
  if (!ready) throw new Error('Vite did not start');
  browser = await launchBrowser({ softwareRendering: false });
  const page = await browser.newPage();
  await page.route('**/__cover-v7.html', (route) =>
    route.fulfill({
      contentType: 'text/html',
      body: '<!doctype html><meta charset="utf-8"><link rel="icon" href="data:,">',
    }),
  );
  await page.goto(origin + '/__cover-v7.html');
  for (const video of videos) {
    try {
      const result = await page.evaluate(async (video) => {
        const { renderCover } = await import('/src/cover.ts');
        const canvas = document.createElement('canvas');
        const receipt = await renderCover(canvas, video.chart, {
          achievement: video.achievement,
          versionOverride: video.versionOverride,
          aspect: '16:9',
        });
        if (video.mirror_horizontal) {
          const copy = document.createElement('canvas');
          copy.width = canvas.width;
          copy.height = canvas.height;
          copy.getContext('2d').drawImage(canvas, 0, 0);
          const ctx = canvas.getContext('2d');
          ctx.clearRect(0, 0, canvas.width, canvas.height);
          ctx.translate(canvas.width, 0);
          ctx.scale(-1, 1);
          ctx.drawImage(copy, 0, 0);
        }
        return { receipt, png: canvas.toDataURL('image/png') };
      }, video);
      const full = Buffer.from(result.png.split(',')[1], 'base64');
      await fs.writeFile(path.join(output, '16x9', video.bvid + '.png'), full);
      await fs.copyFile(
        path.join(previous, '4x3', video.bvid + '.png'),
        path.join(output, '4x3', video.bvid + '.png'),
      );
      const crop = await fs.readFile(path.join(output, '4x3', video.bvid + '.png'));
      report.videos.push({
        bvid: video.bvid,
        mirror_horizontal: !!video.mirror_horizontal,
        receipt: result.receipt,
        outputs: {
          '16x9': { sha256: hash(full), bytes: full.length },
          '4x3': { sha256: hash(crop), bytes: crop.length, preserved: true },
        },
      });
      report.successful++;
    } catch (error) {
      report.errors.push({ bvid: video.bvid, error: String(error) });
    }
    if (report.successful % 10 === 0) console.log(`${report.successful}/${videos.length}`);
  }
  manifest.layout = report.layout;
  await fs.writeFile(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2));
} finally {
  if (browser) await browser.close();
  vite.kill('SIGTERM');
  report.finished_at = new Date().toISOString();
  await fs.writeFile(
    path.join(output, sample ? 'sample-report.json' : 'report.json'),
    JSON.stringify(report, null, 2),
  );
}
console.log(JSON.stringify({ generated: report.successful, errors: report.errors, output }));
if (report.errors.length) process.exitCode = 1;
