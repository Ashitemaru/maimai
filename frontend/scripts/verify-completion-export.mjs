import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { launchBrowser } from './browser.mjs';
import { badgePNG } from './fixtures.mjs';
import ts from 'typescript';

const root = path.resolve(import.meta.dirname, '..');
const dateTime = await fs.readFile(path.join(root, 'src/dateTime.ts'), 'utf8');
const badges = await fs.readFile(path.join(root, 'src/completionBadges.ts'), 'utf8');
const apiSource = await fs.readFile(path.join(root, 'src/api.ts'), 'utf8');
const source = (await fs.readFile(path.join(root, 'src/completionExport.ts'), 'utf8')).replace(
  /^import .*;\n/gm,
  '',
);
const { outputText } = ts.transpileModule(
  `${dateTime}\n${badges}\n${apiSource}\n${source}\nexport {layoutImage, layoutStrips, renderStrip, loadBadgeImages, drawTile, readPalette};`,
  {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  },
);
const {
  layoutImage,
  layoutStrips,
  formatTimestamp,
  completionBadgeKey,
  COMPLETION_BADGES,
  loadBadgeImages,
  drawTile,
  readPalette,
} = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`);
const charts = Array.from({ length: 5600 }, (_, index) => ({
  chart_key: `${index}:3`,
  constant: Number((15 - Math.floor(index / 80) * 0.1).toFixed(1)),
  is_complete: index % 3 === 0,
}));
for (const [name, items, grouped] of [
  ['descending constants', charts, true],
  ['ascending constants', [...charts].reverse(), true],
  ['score sorting', charts, false],
  [
    'alternating constants',
    charts.slice(0, 600).map((item, index) => ({ ...item, constant: index % 2 ? 13 : 14 })),
    true,
  ],
  ['large single group', charts.map((item) => ({ ...item, constant: 14 })), true],
  [
    'distinct groups',
    charts.slice(0, 600).map((item, index) => ({ ...item, constant: index / 10 })),
    true,
  ],
]) {
  const layout = layoutImage(items, grouped);
  assert.deepEqual(
    layout.rows.flatMap((row) => row.items),
    items,
    `${name}: each chart remains in order exactly once`,
  );
  assert.ok(layout.height <= 200000, `${name}: output height stays bounded`);
  assert.equal(layout.width, 3200, `${name}: fixed 2x output width`);
  assert.equal(layout.columns, 16, `${name}: fixed column count`);
  assert.equal(layout.tileSize, 87, `${name}: fixed chart size`);
  const strips = layoutStrips(layout);
  let bottom = 0;
  for (const strip of strips) {
    assert.equal(strip.startY, bottom, `${name}: strips join without gaps or overlaps`);
    assert.ok(strip.height > 0 && strip.height <= 4096, `${name}: bounded canvas height`);
    assert.ok(
      strip.rows.every(
        (row) =>
          row.y * 2 >= strip.startY &&
          (row.y + (row.heading ? 44 : 0) + 87) * 2 <= strip.startY + strip.height,
      ),
      `${name}: whole rows stay within strips`,
    );
    bottom += strip.height;
  }
  assert.equal(bottom, layout.height, `${name}: full image height is covered`);
  assert.deepEqual(
    strips.flatMap((strip) => strip.rows),
    layout.rows,
    `${name}: every row is drawn once`,
  );
  assert.ok(
    layout.rows.every((row) => row.items.length <= layout.columns),
    `${name}: column limit`,
  );
  if (grouped) assert.ok(layout.rows[0].heading, `${name}: starts with a group heading`);
  const actualGroups = layout.rows.filter((row) => row.heading).map((row) => row.heading.label);
  const expectedGroups = grouped
    ? items
        .filter((item, index) => !index || item.constant !== items[index - 1].constant)
        .map((item) => item.constant.toFixed(1))
    : [];
  assert.deepEqual(actualGroups, expectedGroups, `${name}: every group heading is retained`);
  const counts = new Map();
  for (const item of items) {
    const key = item.constant.toFixed(1),
      count = counts.get(key) || { total: 0, complete: 0 };
    count.total++;
    count.complete += Number(item.is_complete);
    counts.set(key, count);
  }
  for (const row of layout.rows) {
    if (!row.heading) continue;
    const count = counts.get(row.heading.label);
    assert.equal(row.heading.total, count.total, `${name}: group denominator`);
    assert.equal(row.heading.complete, count.complete, `${name}: group numerator`);
  }
  const last = layout.rows.at(-1);
  const lastBottom = last.y + (last.heading ? 44 : 0) + layout.tileSize;
  assert.ok(
    (lastBottom * layout.width) / 1600 < layout.height,
    `${name}: final row is not clipped`,
  );
}
assert.deepEqual(layoutImage([], true).rows, []);
for (const count of [3, 80, 320, 321, 5502, 5600]) {
  const layout = layoutImage(charts.slice(0, count), true);
  assert.equal(layout.rows.flatMap((row) => row.items).length, count);
  assert.equal(layout.width, 3200);
  assert.equal(layout.columns, 16);
  assert.equal(layout.tileSize, 87);
  assert.equal(layout.height, layout.logicalHeight * 2);
}
const filteredLayout = layoutImage(
  charts.filter((_, index) => index % 3 === 0),
  true,
);
assert.equal(filteredLayout.width, 3200);
assert.equal(filteredLayout.columns, 16);
assert.equal(filteredLayout.tileSize, 87);
assert.throws(
  () =>
    layoutImage(
      charts.map((item, index) => ({ ...item, constant: index / 10 })),
      true,
    ),
  /200000/,
  'oversized layouts fail explicitly without shrinking',
);
const TILE = layoutImage(charts.slice(0, 1), true).tileSize;
assert.equal(formatTimestamp('2026-10-10T16:21:00Z'), '2026-10-11 00:21');
const completed = { is_complete: true, is_ap: true, status: 'ap' };
const completedPlus = { ...completed, status: 'app' };
const legacy = { ...completed, is_ap: false, status: 'legacy' };
const incomplete = { is_complete: false, is_ap: false, status: 'none' };
assert.equal(completionBadgeKey(completedPlus), 'app');
assert.equal(completionBadgeKey(completed), 'ap');
assert.equal(completionBadgeKey(legacy), 'ap');
assert.equal(completionBadgeKey({ ...legacy, status: 'app' }), 'ap');
assert.equal(completionBadgeKey(incomplete), null);

const requestedImages = [];
let failedImage = '';
globalThis.window = {
  location: new URL('http://completion-export.test/'),
  setTimeout,
  clearTimeout,
};
globalThis.Image = class {
  naturalWidth = 88;
  naturalHeight = 88;
  set src(value) {
    requestedImages.push(value);
    queueMicrotask(() =>
      value.includes(failedImage) && failedImage ? this.onerror?.() : this.onload?.(),
    );
  }
};
const badgeImages = await loadBadgeImages([
  ...Array(200).fill(legacy),
  completedPlus,
  completed,
  incomplete,
]);
assert.equal(requestedImages.length, 2, 'AP and AP+ images each load once for the full export');
assert.equal(badgeImages.size, 2);
requestedImages.length = 0;
assert.equal((await loadBadgeImages([incomplete])).size, 0, 'unfinished charts do not load badges');
assert.equal(requestedImages.length, 0);
failedImage = '3.png';
await assert.rejects(
  loadBadgeImages([legacy]),
  /AP 徽章加载失败/,
  'missing badge aborts the export',
);
failedImage = '';

const tokenValues = {
  '--font-completion-id': '10px',
  '--completion-id-inset': '0px',
  '--completion-id-radius': '4px',
};
globalThis.document = {
  documentElement: {},
  body: { append() {} },
  createElement: () => ({ dataset: {}, remove() {} }),
};
globalThis.getComputedStyle = () => ({
  color: '#123',
  fontFamily: 'sans-serif',
  getPropertyValue: (name) => tokenValues[name] || '',
});
const tokenPalette = readPalette();
assert.equal(tokenPalette.idInset, 0, 'zero-valued inset token is preserved');
assert.equal(tokenPalette.idFontSize, 10);
assert.equal(tokenPalette.idRadius, 4);
delete globalThis.document;
delete globalThis.getComputedStyle;

const palette = {
  text: '#123',
  muted: '#456',
  page: '#fff',
  surface: '#fff',
  line: '#eee',
  difficulties: ['#090', '#e90', '#e22', '#929', '#e4c5f3', '#dd629b'],
  font: 'sans-serif',
  badgeScale: 0.52,
  badgeOpacity: 1,
  shadeColor: '#101419',
  shadeOpacity: 0.64,
  tileSize: 64,
  frameWidth: 4,
  idFontSize: 10,
  idInset: 0,
  idPaddingX: 4,
  idPaddingY: 1,
  idLineHeight: 1.2,
  idRadius: 4,
};
const drawCalls = [];
const layers = [];
const shapes = [];
const texts = [];
const ctx = {
  globalAlpha: 1,
  filter: 'none',
  save() {},
  restore() {},
  clip() {},
  beginPath() {
    this.shape = { kind: 'path', segments: [] };
  },
  moveTo(...args) {
    this.shape.segments.push(['moveTo', ...args]);
  },
  lineTo(...args) {
    this.shape.segments.push(['lineTo', ...args]);
  },
  arcTo(...args) {
    this.shape.segments.push(['arcTo', ...args]);
  },
  closePath() {
    this.shape.segments.push(['closePath']);
  },
  roundRect(...args) {
    this.shape = { kind: 'rounded', args };
  },
  fill() {
    shapes.push({ ...this.shape, action: 'fill', color: this.fillStyle });
  },
  stroke() {
    shapes.push({
      ...this.shape,
      action: 'stroke',
      color: this.strokeStyle,
      width: this.lineWidth,
    });
  },
  fillRect(...args) {
    layers.push({ kind: 'fill', args, color: this.fillStyle, alpha: this.globalAlpha });
  },
  measureText: (value) => ({ width: value.length * 8 }),
  fillText(value, x, y) {
    texts.push({ value, x, y, color: this.fillStyle, font: this.font });
  },
  drawImage(...args) {
    const call = { kind: 'image', args, alpha: this.globalAlpha, filter: this.filter };
    drawCalls.push(call);
    layers.push(call);
  },
};
const tile = { ...legacy, id: 123, type: 'standard', difficulty: 3, constant: 13.9, title: 'Test' };
const jacket = { naturalWidth: 100, naturalHeight: 100 };
drawTile(ctx, tile, 20, 30, jacket, palette, badgeImages, TILE);
assert.equal(drawCalls.length, 2, 'legacy completion overlays AP on the jacket');
assert.equal(drawCalls[0].args[0], jacket);
assert.equal(drawCalls[0].filter, 'none', 'completed jackets remain full color');
assert.equal(drawCalls[0].alpha, 1);
assert.equal(drawCalls[1].args[0], badgeImages.get(COMPLETION_BADGES.ap.src));
for (const badge of Object.values(COMPLETION_BADGES)) {
  assert.deepEqual(
    badge.crop,
    { x: 4, y: 4, width: 71, height: 71 },
    'crop centers the visible badge body',
  );
}
assert.deepEqual(drawCalls[1].args.slice(1, 5), [4, 4, 71, 71]);
const badgeSize = TILE * palette.badgeScale;
const badgeOffset = (TILE - badgeSize) / 2;
assert.deepEqual(drawCalls[1].args.slice(5), [
  20 + badgeOffset,
  30 + badgeOffset,
  badgeSize,
  badgeSize,
]);
assert.equal(drawCalls[1].alpha, 1, 'badge itself remains opaque');
assert.deepEqual(
  layers[2],
  { kind: 'fill', args: [20, 30, TILE, TILE], color: '#101419', alpha: 0.64 },
  'gray-black shade sits between jacket and badge',
);
assert.equal(layers[1], drawCalls[0]);
assert.equal(layers[3], drawCalls[1]);
const geometryScale = TILE / 64;
const frameWidth = 4 * geometryScale;
const frameInset = frameWidth / 2;
assert.deepEqual(
  shapes[0],
  {
    kind: 'rounded',
    args: [20 + frameInset, 30 + frameInset, TILE - frameWidth, TILE - frameWidth, 7 - frameInset],
    action: 'stroke',
    color: '#929',
    width: frameWidth,
  },
  'difficulty frame scales from the global 4px width',
);
const labelWidth = 3 * 8 + 8 * geometryScale;
const labelHeight = 10 * geometryScale * 1.2 + 2 * geometryScale;
const labelX = 20 + TILE - labelWidth;
const labelY = 30 + TILE - labelHeight;
const right = labelX + labelWidth;
const bottom = labelY + labelHeight;
const radius = 4 * geometryScale;
assert.equal(shapes.length, 2, 'frame and joined ID label replace the format dot');
assert.deepEqual(
  shapes[1],
  {
    kind: 'path',
    segments: [
      ['moveTo', labelX + radius, labelY],
      ['lineTo', right - frameWidth - radius, labelY],
      ['arcTo', right - frameWidth, labelY, right - frameWidth, labelY - radius, radius],
      ['lineTo', right, labelY - radius],
      ['lineTo', right, bottom],
      ['lineTo', labelX - radius, bottom],
      ['lineTo', labelX - radius, bottom - frameWidth],
      ['arcTo', labelX, bottom - frameWidth, labelX, bottom - frameWidth - radius, radius],
      ['lineTo', labelX, labelY + radius],
      ['arcTo', labelX, labelY, labelX + radius, labelY, radius],
      ['closePath'],
    ],
    action: 'fill',
    color: '#929',
  },
  'ID label joins bottom and right rails with concave arcs',
);
assert.deepEqual(
  texts,
  [
    {
      value: '123',
      x: labelX + labelWidth / 2,
      y: labelY + labelHeight / 2,
      color: '#fff',
      font: `700 ${10 * geometryScale}px sans-serif`,
    },
  ],
  'only canonical ID is labeled, using smaller global typography',
);
drawCalls.length = 0;
layers.length = 0;
shapes.length = 0;
texts.length = 0;
drawTile(
  ctx,
  { ...tile, ...incomplete, id: 10123, type: 'dx' },
  20,
  30,
  jacket,
  palette,
  badgeImages,
  TILE,
);
assert.equal(drawCalls.length, 1, 'unfinished charts only show their jacket');
assert.equal(drawCalls[0].args[0], jacket);
assert.equal(drawCalls[0].filter, 'none', 'unfinished jackets remain full color');
assert.equal(drawCalls[0].alpha, 1);
assert.ok(
  !layers.some((layer) => layer.color === palette.shadeColor || layer.alpha !== 1),
  'unfinished jackets have no gray or white overlay',
);
assert.equal(texts[0].value, '10123', 'DX canonical ID is displayed unchanged');
for (const difficulty of [1, 4]) {
  shapes.length = 0;
  texts.length = 0;
  drawTile(ctx, { ...tile, ...incomplete, difficulty }, 20, 30, jacket, palette, badgeImages, TILE);
  assert.equal(
    shapes[1].color,
    palette.difficulties[difficulty],
    'light ID capsule retains its difficulty color',
  );
  assert.equal(texts[0].color, palette.text, 'ADV and Re:MASTER IDs use dark text');
}

const css = await fs.readFile(path.join(root, 'styles.css'), 'utf8');
const badgeBodies = new Map(Object.values(COMPLETION_BADGES).map((badge) => [badge.src, badgePNG]));
const browser = await launchBrowser();
const pngChecks = [];
try {
  const page = await browser.newPage();
  const errors = [],
    downloads = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('download', (download) => downloads.push(download.suggestedFilename()));
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://completion-export.test') {
      errors.push(`External request blocked: ${url.origin}`);
      return route.abort();
    }
    const pathname = url.pathname;
    if (pathname === '/')
      return route.fulfill({
        contentType: 'text/html',
        body: `<html><head><style>${css}</style></head><body></body></html>`,
      });
    if (pathname === '/jacket.svg')
      return route.fulfill({
        contentType: 'image/svg+xml',
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#48abd5"/><path d="M0 80 80 0v80Z" fill="#d74783"/></svg>',
      });
    if (badgeBodies.has(pathname))
      return route.fulfill({ contentType: 'image/png', body: badgeBodies.get(pathname) });
    return route.fulfill({ status: 404 });
  });
  await page.goto('http://completion-export.test/');
  const moduleUrl = `data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`;
  for (const count of [3, 80, 400, 5502, 5600]) {
    const result = await page.evaluate(
      async ({ moduleUrl, count }) => {
        const {
          exportCompletionTable,
          layoutImage,
          layoutStrips,
          renderStrip,
          loadBadgeImages,
          readPalette,
        } = await import(moduleUrl);
        const items = Array.from({ length: count }, (_, index) => ({
          id: 10000 + index,
          chart_key: `${10000 + index}:${index % 5}`,
          difficulty: index % 5,
          title: `Chart ${index}`,
          type: index % 2 ? 'dx' : 'standard',
          constant: Number((15 - Math.floor(index / 80) * 0.1).toFixed(1)),
          is_complete: index % 3 === 0,
          is_ap: index % 3 === 0,
          status: index % 6 === 0 ? 'app' : 'ap',
          jacket_url: '/jacket.svg',
        }));
        const ids = [],
          progress = [],
          uploads = [],
          transforms = [],
          partBlobs = [];
        let canvases = 0,
          jacketDraws = 0,
          encodes = 0,
          created = null,
          finished = 0;
        const originalCreate = document.createElement,
          originalFetch = window.fetch;
        const json = (data, status = 200) =>
          new Response(JSON.stringify(data), {
            status,
            headers: { 'Content-Type': 'application/json' },
          });
        window.fetch = async function (input, init) {
          const pathname = String(input);
          if (pathname === '/api/completion-exports' && init.method === 'POST') {
            if (created) throw new Error('Duplicate export job');
            created = JSON.parse(init.body);
            return json({ id: 'fixture' });
          }
          if (pathname.startsWith('/api/completion-exports/fixture/parts/')) {
            const index = Number(pathname.split('/').at(-1));
            if (
              init.method !== 'PUT' ||
              init.headers['Content-Type'] !== 'image/png' ||
              index !== uploads.length
            )
              throw new Error('Invalid strip request');
            const bytes = new Uint8Array(await init.body.slice(0, 24).arrayBuffer());
            const view = new DataView(bytes.buffer);
            const image = await createImageBitmap(init.body);
            uploads.push({
              index,
              signature: Array.from(bytes.slice(0, 8)),
              width: view.getUint32(16),
              height: view.getUint32(20),
              decodedWidth: image.width,
              decodedHeight: image.height,
              bytes: init.body.size,
            });
            image.close();
            if (count === 400) partBlobs.push(init.body);
            return json({ ok: true });
          }
          if (pathname === '/api/completion-exports/fixture/finish') {
            if (
              init.method !== 'POST' ||
              uploads.length !== created.parts ||
              uploads.reduce((sum, part) => sum + part.height, 0) !== created.height
            )
              throw new Error('Incomplete assembly request');
            finished++;
            return json({ url: '/api/completion-exports/fixture/image', name: created.name });
          }
          return originalFetch.call(window, input, init);
        };
        document.createElement = function (tagName, ...rest) {
          const element = originalCreate.call(document, tagName, ...rest);
          if (String(tagName).toLowerCase() === 'canvas') {
            canvases++;
            const originalContext = element.getContext;
            element.getContext = function (...args) {
              const ctx = originalContext.apply(element, args);
              if (ctx) {
                const originalText = ctx.fillText,
                  originalImage = ctx.drawImage,
                  originalScale = ctx.scale,
                  originalTranslate = ctx.translate;
                ctx.fillText = function (value, ...args) {
                  if (/^\d{5}$/.test(value)) ids.push(Number(value));
                  return originalText.call(ctx, value, ...args);
                };
                ctx.drawImage = function (image, ...args) {
                  if (image.src?.includes('/jacket.svg')) jacketDraws++;
                  return originalImage.call(ctx, image, ...args);
                };
                ctx.scale = function (...args) {
                  transforms.push(['scale', ...args]);
                  return originalScale.apply(ctx, args);
                };
                ctx.translate = function (...args) {
                  transforms.push(['translate', ...args]);
                  return originalTranslate.apply(ctx, args);
                };
              }
              return ctx;
            };
            const originalBlob = element.toBlob;
            element.toBlob = function (...args) {
              encodes++;
              return originalBlob.apply(element, args);
            };
          }
          return element;
        };
        try {
          const artifact = await exportCompletionTable(items, {
            grouped: true,
            description: '完整曲库',
            onProgress: (value) => progress.push(value),
          });
          document.createElement = originalCreate;
          const layout = layoutImage(items, true),
            strips = layoutStrips(layout);
          let pixelMatched = null;
          if (count === 400) {
            const badges = await loadBadgeImages(items);
            const reference = await createImageBitmap(
              await renderStrip(
                { rows: layout.rows, startY: 0, height: layout.height },
                layout,
                items,
                { grouped: true, description: '完整曲库' },
                readPalette(),
                badges,
                0,
              ),
            );
            for (let index = 0; index < partBlobs.length; index++) {
              const strip = strips[index],
                actualImage = await createImageBitmap(partBlobs[index]);
              const actualCanvas = document.createElement('canvas'),
                expectedCanvas = document.createElement('canvas');
              actualCanvas.width = expectedCanvas.width = layout.width;
              actualCanvas.height = expectedCanvas.height = strip.height;
              const actualCtx = actualCanvas.getContext('2d'),
                expectedCtx = expectedCanvas.getContext('2d');
              actualCtx.drawImage(actualImage, 0, 0);
              expectedCtx.drawImage(
                reference,
                0,
                strip.startY,
                layout.width,
                strip.height,
                0,
                0,
                layout.width,
                strip.height,
              );
              const actualPixels = new Uint32Array(
                actualCtx.getImageData(0, 0, layout.width, strip.height).data.buffer,
              );
              const expectedPixels = new Uint32Array(
                expectedCtx.getImageData(0, 0, layout.width, strip.height).data.buffer,
              );
              for (let pixel = 0; pixel < actualPixels.length; pixel++) {
                if (actualPixels[pixel] !== expectedPixels[pixel])
                  throw new Error(
                    `Strip ${index} pixel ${pixel} (${pixel % layout.width},${Math.floor(pixel / layout.width) + strip.startY}) differs: ${actualPixels[pixel].toString(16)} vs ${expectedPixels[pixel].toString(16)}`,
                  );
              }
              actualImage.close();
              actualCanvas.width = expectedCanvas.width = 0;
            }
            reference.close();
            badges.clear();
            pixelMatched = true;
          }
          return {
            count,
            artifact,
            created,
            finished,
            uploads,
            transforms,
            canvases,
            encodes,
            jacketDraws,
            ids,
            progress,
            pixelMatched,
            tileSize: layout.tileSize,
            columns: layout.columns,
          };
        } finally {
          document.createElement = originalCreate;
          window.fetch = originalFetch;
        }
      },
      { moduleUrl, count },
    );
    assert.match(result.artifact.name, /^AP完成表-\d{12}\.png$/, 'final download is one named PNG');
    assert.equal(result.artifact.url, '/api/completion-exports/fixture/image');
    assert.equal(result.created.width, 3200);
    assert.equal(result.columns, 16);
    assert.equal(result.tileSize, 87);
    assert.equal(result.canvases, result.created.parts);
    assert.equal(result.encodes, result.created.parts);
    assert.equal(result.finished, 1);
    let startY = 0;
    for (const [index, part] of result.uploads.entries()) {
      assert.deepEqual(part.signature, [137, 80, 78, 71, 13, 10, 26, 10]);
      assert.equal(part.width, 3200);
      assert.equal(part.width, part.decodedWidth);
      assert.equal(part.height, part.decodedHeight);
      assert.ok(part.height <= 4096 && part.height > 0);
      assert.deepEqual(result.transforms[index * 2], ['scale', 2, 2]);
      assert.deepEqual(result.transforms[index * 2 + 1], ['translate', 0, -startY / 2]);
      startY += part.height;
    }
    assert.equal(startY, result.created.height, 'strip heights cover the complete output');
    assert.equal(result.jacketDraws, count, 'every jacket is drawn once');
    assert.deepEqual(
      result.ids,
      Array.from({ length: count }, (_, index) => 10000 + index),
      'every canonical ID appears once in order',
    );
    const steps = result.progress
      .filter((value) => value.startsWith('已绘制 '))
      .map((value) => Number(value.match(/^已绘制 (\d+)/)[1]));
    assert.equal(steps.at(-1), count);
    assert.ok(
      steps.every(
        (value, index) =>
          value - (steps[index - 1] || 0) > 0 && value - (steps[index - 1] || 0) <= 160,
      ),
      'image batches stay bounded',
    );
    if (count === 400)
      assert.equal(result.pixelMatched, true, 'all strip pixels match unsegmented reference');
    pngChecks.push({
      count,
      width: result.created.width,
      height: result.created.height,
      parts: result.created.parts,
      tilePixels: result.tileSize * 2,
      bytes: result.uploads.reduce((sum, part) => sum + part.bytes, 0),
    });
  }
  const error = await page.evaluate(async (moduleUrl) => {
    const { exportCompletionTable } = await import(moduleUrl);
    const previousFetch = window.fetch;
    window.fetch = async () =>
      new Response(JSON.stringify({ detail: '导出服务测试错误' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    try {
      await exportCompletionTable(
        [
          {
            id: 1,
            title: 'Test',
            type: 'standard',
            difficulty: 3,
            constant: 13,
            is_complete: false,
            is_ap: false,
            status: 'none',
            jacket_url: '/jacket.svg',
          },
        ],
        { grouped: true, description: '' },
      );
      return null;
    } catch (error) {
      return error.message;
    } finally {
      window.fetch = previousFetch;
    }
  }, moduleUrl);
  assert.equal(error, '导出服务测试错误', 'backend errors propagate to caller');
  assert.deepEqual(errors, []);
  assert.deepEqual(downloads, [], 'caller owns browser delivery of the returned attachment URL');
} finally {
  await browser.close();
}
console.log(
  '完成表导出验证通过：固定16列/3200px/174px曲绘、连续条带与逐像素参考比对、全量顺序和分组、分批绘制上传、标准附件URL、API错误、ID/AP/蒙版、字体参数和上海日期。',
);
console.log(JSON.stringify(pngChecks));
