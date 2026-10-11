import type { CompletionChart } from './types';
import { formatTimestamp } from './dateTime';
import { COMPLETION_BADGES, completionBadgeKey } from './completionBadges';
import { api } from './api';

interface ExportOptions {
  grouped: boolean;
  description: string;
  onProgress?: (value: string) => void;
}

interface Row {
  items: CompletionChart[];
  y: number;
  heading?: { label: string; complete: number; total: number };
}
interface ExportLayout {
  rows: Row[];
  logicalHeight: number;
  width: number;
  height: number;
  tileSize: number;
  columns: number;
}
interface ExportStrip {
  rows: Row[];
  startY: number;
  height: number;
}
interface Palette {
  text: string;
  muted: string;
  page: string;
  surface: string;
  line: string;
  warning: string;
  warningSurface: string;
  font: string;
  difficulties: string[];
  badgeScale: number;
  badgeOpacity: number;
  shadeColor: string;
  shadeOpacity: number;
  tileSize: number;
  frameWidth: number;
  idFontSize: number;
  idInset: number;
  idPaddingX: number;
  idPaddingY: number;
  idLineHeight: number;
  idRadius: number;
}

const WIDTH = 1600;
const PADDING = 44;
const COLUMNS = 16;
const OUTPUT_SCALE = 2;
const GAP = 8;
const TILE = (WIDTH - PADDING * 2 - GAP * (COLUMNS - 1)) / COLUMNS;
const HEADING_HEIGHT = 44;
const HEADER_HEIGHT = 141;
const FOOTER_HEIGHT = 54;
const MAX_HEIGHT = 200000;
const MAX_STRIP_HEIGHT = 4096;
const CHUNK_ITEMS = 160;

function constantLabel(value: number): string {
  return Number.isFinite(value) ? value.toFixed(1) : '未定数';
}

function layoutImage(items: CompletionChart[], grouped: boolean): ExportLayout {
  const counts = new Map<string, { complete: number; total: number }>();
  for (const item of items) {
    const key = constantLabel(item.constant);
    const count = counts.get(key) || { complete: 0, total: 0 };
    count.total++;
    if (item.is_complete) count.complete++;
    counts.set(key, count);
  }
  const rows: Row[] = [];
  let y = HEADER_HEIGHT;
  let offset = 0;
  let previousGroup = '';
  while (offset < items.length) {
    const key = constantLabel(items[offset].constant);
    const rowItems: CompletionChart[] = [];
    for (let index = offset; index < items.length && rowItems.length < COLUMNS; index++) {
      if (grouped && constantLabel(items[index].constant) !== key) break;
      rowItems.push(items[index]);
    }
    const needsHeading = grouped && previousGroup !== key;
    rows.push({
      items: rowItems,
      y,
      ...(needsHeading ? { heading: { label: key, ...counts.get(key)! } } : {}),
    });
    y += TILE + GAP + (needsHeading ? HEADING_HEIGHT : 0);
    offset += rowItems.length;
    previousGroup = key;
  }
  const logicalHeight = Math.ceil(y + FOOTER_HEIGHT);
  const height = logicalHeight * OUTPUT_SCALE;
  if (height > MAX_HEIGHT) throw new Error('完成表高度超过 200000 像素，请缩小筛选范围');
  return {
    rows,
    logicalHeight,
    width: WIDTH * OUTPUT_SCALE,
    height,
    tileSize: TILE,
    columns: COLUMNS,
  };
}

function layoutStrips(layout: ExportLayout): ExportStrip[] {
  const strips: ExportStrip[] = [];
  let rowIndex = 0;
  let startY = 0;
  while (rowIndex < layout.rows.length) {
    const rows: Row[] = [];
    let endY = startY;
    while (rowIndex < layout.rows.length) {
      const nextEnd =
        rowIndex + 1 < layout.rows.length
          ? layout.rows[rowIndex + 1].y * OUTPUT_SCALE
          : layout.height;
      if (rows.length && nextEnd - startY > MAX_STRIP_HEIGHT) break;
      rows.push(layout.rows[rowIndex++]);
      endY = nextEnd;
    }
    if (endY - startY > MAX_STRIP_HEIGHT) throw new Error('完成表分段高度超过限制');
    strips.push({ rows, startY, height: endY - startY });
    startY = endY;
  }
  return strips;
}

function readPalette(): Palette {
  const root = getComputedStyle(document.documentElement);
  const color = (name: string) => root.getPropertyValue(name).trim() || root.color;
  const pixels = (name: string, fallback: number) => {
    const value = Number.parseFloat(root.getPropertyValue(name));
    return Number.isFinite(value) ? value : fallback;
  };
  const probe = document.createElement('span');
  probe.hidden = true;
  document.body.append(probe);
  try {
    const difficulties = Array.from({ length: 6 }, (_, index) => {
      probe.dataset.difficulty = String(index);
      return getComputedStyle(probe).getPropertyValue('--diff').trim() || color('--purple');
    });
    difficulties[4] = color('--completion-frame-remaster');
    return {
      text: color('--text'),
      muted: color('--muted'),
      page: color('--surface-page'),
      surface: color('--surface'),
      line: color('--line'),
      warning: color('--warning'),
      warningSurface: color('--warning-soft'),
      font: root.fontFamily,
      difficulties,
      badgeScale: Number(root.getPropertyValue('--completion-badge-scale').trim()) || 0.52,
      badgeOpacity: Number(root.getPropertyValue('--completion-badge-opacity').trim()) || 1,
      shadeColor: color('--completion-shade-color'),
      shadeOpacity: Number(root.getPropertyValue('--completion-shade-opacity').trim()) || 0.64,
      tileSize: pixels('--completion-tile-size', 64),
      frameWidth: pixels('--completion-frame-width', 4),
      idFontSize: pixels('--font-completion-id', 10),
      idInset: pixels('--completion-id-inset', 0),
      idPaddingX: pixels('--completion-id-padding-x', 4),
      idPaddingY: pixels('--completion-id-padding-y', 1),
      idLineHeight: Number(root.getPropertyValue('--completion-id-line-height').trim()) || 1.2,
      idRadius: pixels('--completion-id-radius', 4),
    };
  } finally {
    probe.remove();
  }
}

function rounded(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  ctx.beginPath();
  ctx.roundRect(x, y, width, height, radius);
}

function fitText(ctx: CanvasRenderingContext2D, value: string, width: number): string {
  if (ctx.measureText(value).width <= width) return value;
  const chars = Array.from(value);
  while (chars.length && ctx.measureText(chars.join('') + '…').width > width) chars.pop();
  return chars.join('') + '…';
}

function pill(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  background: string,
  color: string,
  palette: Palette,
) {
  ctx.font = `700 15px ${palette.font}`;
  const width = ctx.measureText(text).width + 14;
  ctx.fillStyle = background;
  rounded(ctx, x, y, width, 24, 5);
  ctx.fill();
  ctx.fillStyle = color;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x + 7, y + 12);
  return width;
}

async function loadSameOriginImage(source: string): Promise<HTMLImageElement | null> {
  let url: URL;
  try {
    url = new URL(source, window.location.href);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol))
    return null;
  const img = new Image();
  img.decoding = 'async';
  return await new Promise<HTMLImageElement | null>((resolve) => {
    const timer = window.setTimeout(() => {
      img.src = '';
      finish(false);
    }, 10000);
    function finish(ok: boolean) {
      window.clearTimeout(timer);
      img.onload = null;
      img.onerror = null;
      resolve(ok && img.naturalWidth ? img : null);
    }
    img.onload = () => finish(true);
    img.onerror = () => finish(false);
    img.src = url.href;
  });
}

async function loadBadgeImages(items: CompletionChart[]): Promise<Map<string, HTMLImageElement>> {
  const keys = [...new Set(items.map(completionBadgeKey).filter((key) => key !== null))];
  const images = new Map<string, HTMLImageElement>();
  await Promise.all(
    keys.map(async (key) => {
      const source = COMPLETION_BADGES[key].src;
      const image = await loadSameOriginImage(source);
      if (!image) throw new Error(`${key === 'app' ? 'AP+' : 'AP'} 徽章加载失败，请重试导出`);
      images.set(source, image);
    }),
  );
  return images;
}

async function loadBatchImages(items: CompletionChart[]): Promise<Map<string, HTMLImageElement>> {
  const urls = [...new Set(items.map((item) => item.jacket_url).filter(Boolean))];
  const images = new Map<string, HTMLImageElement>();
  let cursor = 0;
  await Promise.all(
    Array.from({ length: Math.min(6, urls.length) }, async () => {
      while (cursor < urls.length) {
        const source = urls[cursor++];
        const image = await loadSameOriginImage(source);
        if (image) images.set(source, image);
      }
    }),
  );
  return images;
}

function drawTile(
  ctx: CanvasRenderingContext2D,
  item: CompletionChart,
  x: number,
  y: number,
  image: HTMLImageElement | undefined,
  palette: Palette,
  badgeImages: Map<string, HTMLImageElement>,
  tileSize: number,
) {
  const difficulty = item.type === 'utage' ? 5 : item.difficulty;
  const accent = palette.difficulties[difficulty] || palette.muted;
  ctx.save();
  rounded(ctx, x, y, tileSize, tileSize, 7);
  ctx.clip();
  ctx.fillStyle = palette.line;
  ctx.fillRect(x, y, tileSize, tileSize);
  if (image) {
    const crop = Math.min(image.naturalWidth, image.naturalHeight);
    ctx.drawImage(
      image,
      (image.naturalWidth - crop) / 2,
      (image.naturalHeight - crop) / 2,
      crop,
      crop,
      x,
      y,
      tileSize,
      tileSize,
    );
  } else {
    ctx.fillStyle = palette.muted;
    ctx.font = `600 14px ${palette.font}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const title = item.display_title || item.title;
    const chars = Array.from(title);
    const lines = [chars.slice(0, 6).join(''), chars.slice(6, 12).join('')];
    lines.forEach((line, index) =>
      ctx.fillText(line, x + tileSize / 2, y + tileSize / 2 + (index - 0.5) * 20),
    );
    ctx.textAlign = 'left';
  }
  const badgeKey = completionBadgeKey(item);
  if (badgeKey) {
    ctx.globalAlpha = palette.shadeOpacity;
    ctx.fillStyle = palette.shadeColor;
    ctx.fillRect(x, y, tileSize, tileSize);
    const badge = COMPLETION_BADGES[badgeKey];
    const badgeImage = badgeImages.get(badge.src);
    if (!badgeImage) throw new Error('AP 徽章未加载，请重试导出');
    const size = tileSize * palette.badgeScale;
    const offset = (tileSize - size) / 2;
    ctx.globalAlpha = palette.badgeOpacity;
    ctx.drawImage(
      badgeImage,
      badge.crop.x,
      badge.crop.y,
      badge.crop.width,
      badge.crop.height,
      x + offset,
      y + offset,
      size,
      size,
    );
    ctx.globalAlpha = 1;
  }
  const scale = tileSize / palette.tileSize;
  const frameWidth = palette.frameWidth * scale;
  const frameInset = frameWidth / 2;
  ctx.lineWidth = frameWidth;
  ctx.strokeStyle = accent;
  rounded(
    ctx,
    x + frameInset,
    y + frameInset,
    tileSize - frameWidth,
    tileSize - frameWidth,
    Math.max(0, 7 - frameInset),
  );
  ctx.stroke();
  const label = String(item.id);
  const fontSize = palette.idFontSize * scale;
  const labelInset = palette.idInset * scale;
  const paddingX = palette.idPaddingX * scale;
  const paddingY = palette.idPaddingY * scale;
  ctx.font = `700 ${fontSize}px ${palette.font}`;
  const labelWidth = ctx.measureText(label).width + paddingX * 2;
  const labelHeight = fontSize * palette.idLineHeight + paddingY * 2;
  const labelX = x + tileSize - labelInset - labelWidth;
  const labelY = y + tileSize - labelInset - labelHeight;
  const labelRight = labelX + labelWidth;
  const labelBottom = labelY + labelHeight;
  const radius = palette.idRadius * scale;
  ctx.fillStyle = accent;
  ctx.beginPath();
  ctx.moveTo(labelX + radius, labelY);
  ctx.lineTo(labelRight - frameWidth - radius, labelY);
  ctx.arcTo(labelRight - frameWidth, labelY, labelRight - frameWidth, labelY - radius, radius);
  ctx.lineTo(labelRight, labelY - radius);
  ctx.lineTo(labelRight, labelBottom);
  ctx.lineTo(labelX - radius, labelBottom);
  ctx.lineTo(labelX - radius, labelBottom - frameWidth);
  ctx.arcTo(labelX, labelBottom - frameWidth, labelX, labelBottom - frameWidth - radius, radius);
  ctx.lineTo(labelX, labelY + radius);
  ctx.arcTo(labelX, labelY, labelX + radius, labelY, radius);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = difficulty === 1 || difficulty === 4 ? palette.text : palette.surface;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(label, labelX + labelWidth / 2, labelY + labelHeight / 2);
  ctx.textAlign = 'left';
  if (item.conflict)
    pill(ctx, '!', x + tileSize - 26, y + 5, palette.warningSurface, palette.warning, palette);
  ctx.restore();
}

async function renderStrip(
  strip: ExportStrip,
  layout: ExportLayout,
  items: CompletionChart[],
  options: ExportOptions,
  palette: Palette,
  badgeImages: Map<string, HTMLImageElement>,
  drawnBefore: number,
): Promise<Blob> {
  const canvas = document.createElement('canvas');
  canvas.width = layout.width;
  canvas.height = strip.height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('浏览器无法创建导出图片');
  try {
    ctx.scale(OUTPUT_SCALE, OUTPUT_SCALE);
    ctx.translate(0, -strip.startY / OUTPUT_SCALE);
    ctx.fillStyle = palette.page;
    ctx.fillRect(0, strip.startY / OUTPUT_SCALE, WIDTH, strip.height / OUTPUT_SCALE);
    ctx.textBaseline = 'middle';
    if (strip.startY === 0) {
      ctx.fillStyle = palette.text;
      ctx.font = `750 35px ${palette.font}`;
      ctx.fillText('AP 完成表', PADDING, 57);
      ctx.font = `600 24px ${palette.font}`;
      ctx.textAlign = 'right';
      ctx.fillText(
        `${items.filter((item) => item.is_complete).length} / ${items.length}`,
        WIDTH - PADDING,
        57,
      );
      ctx.textAlign = 'left';
      ctx.fillStyle = palette.muted;
      ctx.font = `400 18px ${palette.font}`;
      ctx.fillText(
        fitText(ctx, options.description || '全部谱面', WIDTH - 2 * PADDING),
        PADDING,
        100,
      );
    }
    let rowIndex = 0;
    let drawn = drawnBefore;
    while (rowIndex < strip.rows.length) {
      const batch: Row[] = [];
      let batchItems = 0;
      while (
        rowIndex < strip.rows.length &&
        batchItems + strip.rows[rowIndex].items.length <= CHUNK_ITEMS
      ) {
        const row = strip.rows[rowIndex++];
        batch.push(row);
        batchItems += row.items.length;
      }
      options.onProgress?.(`加载曲绘 ${drawn} / ${items.length}`);
      const images = await loadBatchImages(batch.flatMap((row) => row.items));
      try {
        for (const row of batch) {
          let y = row.y;
          if (row.heading) {
            ctx.fillStyle = palette.text;
            ctx.font = `700 23px ${palette.font}`;
            ctx.fillText(row.heading.label, PADDING, y + 17);
            ctx.font = `500 18px ${palette.font}`;
            ctx.textAlign = 'right';
            ctx.fillStyle = palette.muted;
            ctx.fillText(`${row.heading.complete} / ${row.heading.total}`, WIDTH - PADDING, y + 17);
            ctx.textAlign = 'left';
            y += HEADING_HEIGHT;
          }
          row.items.forEach((item, index) =>
            drawTile(
              ctx,
              item,
              PADDING + index * (layout.tileSize + GAP),
              y,
              images.get(item.jacket_url),
              palette,
              badgeImages,
              layout.tileSize,
            ),
          );
        }
      } finally {
        images.clear();
      }
      drawn += batchItems;
      options.onProgress?.(`已绘制 ${drawn} / ${items.length}`);
      await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
    }
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error('图片生成失败'))),
        'image/png',
      ),
    );
  } finally {
    canvas.width = 0;
    canvas.height = 0;
  }
}

export async function exportCompletionTable(
  items: CompletionChart[],
  options: ExportOptions,
): Promise<{ url: string; name: string }> {
  if (!items.length) throw new Error('没有可导出的谱面');
  const timestamp = formatTimestamp(new Date().toISOString());
  const name = `AP完成表-${timestamp.replaceAll(/[-: ]/g, '')}.png`;
  const palette = readPalette();
  const layout = layoutImage(items, options.grouped);
  const strips = layoutStrips(layout);
  options.onProgress?.('准备图片…');
  await document.fonts.ready;
  const badgeImages = await loadBadgeImages(items);
  try {
    const job = await api<{ id: string }>('/completion-exports', {
      method: 'POST',
      body: JSON.stringify({
        name,
        width: layout.width,
        height: layout.height,
        parts: strips.length,
      }),
    });
    const path = `/completion-exports/${encodeURIComponent(job.id)}`;
    let drawn = 0;
    for (let index = 0; index < strips.length; index++) {
      const strip = strips[index];
      const blob = await renderStrip(strip, layout, items, options, palette, badgeImages, drawn);
      options.onProgress?.(`上传图片 ${index + 1} / ${strips.length}`);
      await api<{ ok: boolean }>(`${path}/parts/${index}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'image/png' },
        body: blob,
      });
      drawn += strip.rows.reduce((total, row) => total + row.items.length, 0);
    }
    options.onProgress?.('合成完整 PNG…');
    return await api<{ url: string; name: string }>(`${path}/finish`, { method: 'POST' });
  } finally {
    badgeImages.clear();
  }
}
