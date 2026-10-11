import {
  assetURL,
  GAME_UI_MANIFEST_URL,
  loadBackgroundManifest,
  loadGameUIManifest,
  resolveVersion,
} from './coverAssets';
import type { ImageAsset, NumberSet, ResolvedVersion } from './coverAssets';
export const PALETTE: Record<number, string> = {
  0: '#65b04b',
  1: '#e3a137',
  2: '#ef424d',
  3: '#703894',
  4: '#b870e0',
  5: '#dd629b',
};
export const COVER_SIZE = { width: 1920, height: 1080, safeLeft: 240, safeRight: 1680 };
export type CoverAspect = '16:9' | '4:3';
export const COVER_LAYOUT_4_3 = {
  card: { x: 278, y: 306, width: 1364, height: 652 },
  jacket: { x: 320, y: 382, width: 470, height: 470 },
  logo: { x: 650, y: 68, width: 620, height: 182 },
  type: { x: 310, y: 372, width: 190, height: 53 },
  title: { x: 838, y: 492, width: 760, height: 224 },
  constant: { x: 838, y: 382, width: 760, height: 93 },
  achievement: { x: 838, y: 758, width: 760, height: 94 },
};
export const COVER_LAYOUT_16_9 = {
  card: { x: 64, y: 36, width: 1792, height: 1008 },
  jacket: { x: 128, y: 156, width: 760, height: 760 },
  type: { x: 114, y: 142, width: 250, height: 70 },
  constant: { x: 944, y: 156, width: 848, height: 120 },
  title: { x: 944, y: 348, width: 848, height: 358 },
  achievement: { x: 944, y: 798, width: 848, height: 118 },
  logo: { x: 1390, y: 100, width: 380, height: 180 },
};
export const COVER_LAYOUTS: Record<CoverAspect, typeof COVER_LAYOUT_4_3> = {
  '16:9': COVER_LAYOUT_16_9,
  '4:3': COVER_LAYOUT_4_3,
};
export const COVER_LAYOUT = COVER_LAYOUT_4_3;
export function getCoverLayout(aspect: CoverAspect = '16:9') {
  return COVER_LAYOUTS[aspect];
}
export interface CoverChart {
  id?: number;
  difficulty?: number;
  title: string;
  display_title?: string;
  artist?: string;
  jacket_url: string;
  type: string;
  difficulty_index: number;
  difficulty_name: string;
  constant: number | null;
  level: string;
  jp_release_version?: string | null;
  jp_release_version_candidate?: string | null;
  jp_release_provider?: string | null;
  release_status?: string;
}
export interface CoverOptions {
  achievement: string;
  creator?: string;
  versionOverride?: string;
  aspect?: CoverAspect;
  safeGuides?: boolean;
  backgroundUrl?: string;
  versionLogoUrl?: string;
}
export interface Bounds {
  role: string;
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface TitleLayout {
  lines: string[];
  fontSize: number;
  lineHeight: number;
  widths: number[];
  text: string;
}
export interface CoverReceipt {
  width: number;
  height: number;
  version: ResolvedVersion;
  title: TitleLayout;
  bounds: Bounds[];
  notices: string[];
  gameAssets: string[];
  achievementStyle: 'sprite' | 'system_font';
  constantStyle: 'styled_text';
}
const SANS =
  '"CoverNewRodin", "Arial", "Helvetica Neue", "PingFang SC", "Hiragino Sans", sans-serif';
let coverFont: Promise<void> | undefined;
export function loadCoverFont() {
  return (coverFont ??= new FontFace(
    'CoverNewRodin',
    'url("/cover-assets/game-ui/numbers/candidates/yuri-cn1.55/FOT-NewRodin%20Pro%20EB.otf")',
    { weight: '800', style: 'normal' },
  )
    .load()
    .then((font) => {
      document.fonts.add(font);
    })
    .catch((error) => {
      coverFont = undefined;
      throw error;
    }));
}
const images = new Map<string, Promise<HTMLImageElement>>();
function loadImage(url: string) {
  let pending = images.get(url);
  if (!pending) {
    pending = new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => {
        images.delete(url);
        reject(new Error('图片未能载入'));
      };
      image.src = url;
    });
    images.set(url, pending);
  }
  return pending;
}
const graphemes = (value: string) =>
  [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(value)].map(
    (item) => item.segment,
  );
export function wrapMeasured(ctx: CanvasRenderingContext2D, text: string, width: number) {
  const result: string[] = [];
  let line = '';
  const parts = text.match(/\S+\s*|\s+/gu) || [''];
  for (const token of parts) {
    if (ctx.measureText(line + token).width <= width) {
      line += token;
      continue;
    }
    if (line.trim()) {
      result.push(line.trimEnd());
      line = '';
    }
    for (const glyph of graphemes(token)) {
      if (line && ctx.measureText(line + glyph).width > width) {
        result.push(line.trimEnd());
        line = '';
      }
      line += glyph;
    }
  }
  if (line.trim()) result.push(line.trimEnd());
  return result.length ? result : ['　'];
}
export function fitTitle(
  ctx: CanvasRenderingContext2D,
  text: string,
  width = COVER_LAYOUT.title.width,
  height = COVER_LAYOUT.title.height,
  maxFontSize = 76,
): TitleLayout {
  const normalized = text.replace(/\s+/gu, ' ').trim() || '　';
  for (let size = maxFontSize; size >= 8; size--) {
    ctx.font = `800 ${size}px ${SANS}`;
    const lines = wrapMeasured(ctx, normalized, width);
    const lineHeight = size * 1.15;
    if (
      lines.length * lineHeight <= height &&
      lines.every((line) => ctx.measureText(line).width <= width)
    )
      return {
        lines,
        fontSize: size,
        lineHeight,
        widths: lines.map((line) => ctx.measureText(line).width),
        text: normalized,
      };
  }
  throw new Error('曲名过长，暂时无法完整排入封面');
}
function rounded(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
  fill: string,
) {
  ctx.fillStyle = fill;
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
  ctx.fill();
}
function contain(
  ctx: CanvasRenderingContext2D,
  image: HTMLImageElement,
  box: Omit<Bounds, 'role'>,
  crop?: ImageAsset['crop'],
) {
  const source = crop || { x: 0, y: 0, width: image.naturalWidth, height: image.naturalHeight };
  const scale = Math.min(box.width / source.width, box.height / source.height);
  const width = source.width * scale,
    height = source.height * scale;
  const destination = {
    x: box.x + (box.width - width) / 2,
    y: box.y + (box.height - height) / 2,
    width,
    height,
  };
  ctx.drawImage(
    image,
    source.x,
    source.y,
    source.width,
    source.height,
    destination.x,
    destination.y,
    width,
    height,
  );
  return destination;
}
function fillImage(ctx: CanvasRenderingContext2D, image: HTMLImageElement) {
  const scale = Math.max(1920 / image.naturalWidth, 1080 / image.naturalHeight);
  const w = image.naturalWidth * scale,
    h = image.naturalHeight * scale;
  ctx.drawImage(image, (1920 - w) / 2, (1080 - h) / 2, w, h);
}
function measuredText(
  ctx: CanvasRenderingContext2D,
  text: string,
  box: Omit<Bounds, 'role'>,
  size: number,
  color: string,
  weight = 700,
  align: 'left' | 'center' = 'left',
) {
  ctx.font = `${weight} ${size}px ${SANS}`;
  while (ctx.measureText(text).width > box.width && size > 8) {
    size--;
    ctx.font = `${weight} ${size}px ${SANS}`;
  }
  const metrics = ctx.measureText(text);
  const x = box.x + (align === 'center' ? (box.width - metrics.width) / 2 : 0);
  ctx.fillStyle = color;
  ctx.textBaseline = 'top';
  ctx.fillText(text, x, box.y);
  ctx.textBaseline = 'alphabetic';
  return { x, y: box.y, width: metrics.width, height: size * 1.2 };
}
const alphaCache = new WeakMap<
  HTMLImageElement,
  Map<string, { x: number; y: number; width: number; height: number }>
>();
function alphaBounds(
  image: HTMLImageElement,
  rect = { x: 0, y: 0, width: image.naturalWidth, height: image.naturalHeight },
) {
  const key = [rect.x, rect.y, rect.width, rect.height].join(',');
  let cache = alphaCache.get(image);
  if (!cache) {
    cache = new Map();
    alphaCache.set(image, cache);
  }
  const known = cache.get(key);
  if (known) return known;
  const canvas = document.createElement('canvas');
  canvas.width = rect.width;
  canvas.height = rect.height;
  const ctx = canvas.getContext('2d')!;
  ctx.drawImage(image, rect.x, rect.y, rect.width, rect.height, 0, 0, rect.width, rect.height);
  const pixels = ctx.getImageData(0, 0, rect.width, rect.height).data;
  let x0 = rect.width,
    y0 = rect.height,
    x1 = -1,
    y1 = -1;
  for (let y = 0; y < rect.height; y++)
    for (let x = 0; x < rect.width; x++)
      if (pixels[(y * rect.width + x) * 4 + 3] > 0) {
        x0 = Math.min(x0, x);
        y0 = Math.min(y0, y);
        x1 = Math.max(x1, x);
        y1 = Math.max(y1, y);
      }
  const bounds =
    x1 < 0
      ? { x: 0, y: 0, width: 0, height: 0 }
      : { x: x0, y: y0, width: x1 - x0 + 1, height: y1 - y0 + 1 };
  cache.set(key, bounds);
  return bounds;
}
function visibleText(
  ctx: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  size: number,
  color: string,
  weight = 800,
) {
  ctx.font = `${weight} ${size}px ${SANS}`;
  ctx.textBaseline = 'alphabetic';
  const metrics = ctx.measureText(text);
  ctx.fillStyle = color;
  ctx.fillText(text, x + metrics.actualBoundingBoxLeft, y + metrics.actualBoundingBoxAscent);
  return {
    x,
    y,
    width: metrics.actualBoundingBoxLeft + metrics.actualBoundingBoxRight,
    height: metrics.actualBoundingBoxAscent + metrics.actualBoundingBoxDescent,
  };
}
export function spriteNumberLayout(set: NumberSet, text: string, height: number) {
  const glyphs = [...text].map((character) => ({ character, glyph: set.glyphs[character] }));
  if (glyphs.some(({ glyph }) => !glyph)) return null;
  let pen = 0,
    minX = 0,
    maxX = 0,
    minY = 0,
    maxY = 0;
  for (const { glyph } of glyphs) {
    const left = pen + (glyph.offset_x || 0),
      top = glyph.offset_y || 0;
    minX = Math.min(minX, left);
    maxX = Math.max(maxX, left + glyph.width);
    minY = Math.min(minY, top);
    maxY = Math.max(maxY, top + glyph.height);
    pen += glyph.advance ?? glyph.width;
  }
  const baseHeight = Math.max(set.line_height || 0, maxY) - minY;
  const scale = height / baseHeight;
  return {
    glyphs,
    scale,
    width: (maxX - minX) * scale,
    height,
    minX,
    minY,
    advanceWidth: pen * scale,
  };
}
export function drawSpriteNumber(
  ctx: CanvasRenderingContext2D,
  image: HTMLImageElement | null,
  set: NumberSet,
  text: string,
  box: Omit<Bounds, 'role'>,
  glyphImages: Map<string, HTMLImageElement> = new Map(),
) {
  const initial = spriteNumberLayout(set, text, box.height);
  if (
    !initial ||
    initial.glyphs.some(({ glyph }) => !(glyph.file ? glyphImages.get(glyph.file) : image))
  )
    return null;
  const fit = Math.min(1, box.width / initial.width);
  const scale = initial.scale * fit;
  let pen = 0;
  for (const { glyph: g } of initial.glyphs) {
    const source = g.file ? glyphImages.get(g.file)! : image!;
    ctx.drawImage(
      source,
      g.x || 0,
      g.y || 0,
      g.width,
      g.height,
      box.x + (pen + (g.offset_x || 0) - initial.minX) * scale,
      box.y + ((g.offset_y || 0) - initial.minY) * scale,
      g.width * scale,
      g.height * scale,
    );
    pen += g.advance ?? g.width;
  }
  return { x: box.x, y: box.y, width: initial.width * fit, height: initial.height * fit };
}
function spriteVisualBounds(
  set: NumberSet,
  text: string,
  image: HTMLImageElement | null,
  glyphImages: Map<string, HTMLImageElement>,
) {
  let pen = 0,
    left = Infinity,
    right = -Infinity,
    top = Infinity,
    bottom = -Infinity;
  for (const character of text) {
    const glyph = set.glyphs[character];
    if (!glyph) return null;
    const source = glyph.file ? glyphImages.get(glyph.file) : image;
    if (!source) return null;
    const alpha = alphaBounds(source, {
      x: glyph.x || 0,
      y: glyph.y || 0,
      width: glyph.width,
      height: glyph.height,
    });
    if (alpha.width) {
      left = Math.min(left, pen + (glyph.offset_x || 0) + alpha.x);
      right = Math.max(right, pen + (glyph.offset_x || 0) + alpha.x + alpha.width);
      top = Math.min(top, (glyph.offset_y || 0) + alpha.y);
      bottom = Math.max(bottom, (glyph.offset_y || 0) + alpha.y + alpha.height);
    }
    pen += glyph.advance ?? glyph.width;
  }
  return Number.isFinite(left)
    ? { left, right, top, bottom, width: right - left, height: bottom - top }
    : null;
}
export async function renderCover(
  canvas: HTMLCanvasElement,
  chart: CoverChart,
  options: CoverOptions,
): Promise<CoverReceipt> {
  const layout = getCoverLayout(options.aspect);
  const widescreen = options.aspect !== '4:3';
  const raw = options.achievement.trim();
  if (!/^\d{1,3}(\.\d{1,4})?$/.test(raw) || Number(raw) < 0 || Number(raw) > 101)
    throw new Error('达成率需为 0–101，最多四位小数');
  const [manifest, ui, jacket] = await Promise.all([
    loadBackgroundManifest(),
    loadGameUIManifest(),
    loadImage(chart.jacket_url),
    loadCoverFont(),
    document.fonts.ready,
  ]);
  const version = resolveVersion(chart, manifest, options.versionOverride);
  const notices: string[] = version.notice ? [version.notice] : [];
  const bgURL =
    options.backgroundUrl ||
    (version.asset?.status === 'ready' && version.asset.background_png
      ? assetURL(version.asset.background_png)
      : '');
  const logoURL =
    options.versionLogoUrl || (version.asset?.logo_file ? assetURL(version.asset.logo_file) : '');
  const isUtage = chart.type === 'utage' || chart.difficulty_name.toUpperCase() === 'UTAGE';
  const typeKey = isUtage ? '' : chart.type === 'dx' ? 'dx' : 'standard';
  const apKey = Number(raw) === 101 ? 'app' : 'ap';
  const badgeKeys = [typeKey, apKey];
  const resources = new Map<string, HTMLImageElement>();
  const requests: Array<[string, string]> = [];
  if (bgURL) requests.push(['background', bgURL]);
  if (logoURL) requests.push(['logo', logoURL]);
  for (const key of new Set(badgeKeys)) {
    const asset = ui?.assets[key];
    if (asset?.file) requests.push([key, assetURL(asset.file, GAME_UI_MANIFEST_URL)]);
  }
  const numberSet = ui?.number_sets?.achievement;
  const glyphImages = new Map<string, HTMLImageElement>();
  for (const set of [numberSet])
    if (set) {
      const files = new Set(
        [set.file, ...Object.values(set.glyphs).map((glyph) => glyph.file)].filter(
          (file): file is string => !!file,
        ),
      );
      for (const file of files)
        requests.push(['numberFile:' + file, assetURL(file, GAME_UI_MANIFEST_URL)]);
    }
  await Promise.all(
    requests.map(async ([key, url]) => {
      try {
        const image = await loadImage(url);
        resources.set(key, image);
        if (key.startsWith('numberFile:')) glyphImages.set(key.slice('numberFile:'.length), image);
      } catch {
        notices.push(
          key === 'background'
            ? '版本底图载入失败'
            : key === 'logo'
              ? '版本 Logo 载入失败'
              : '部分游戏素材暂不可用',
        );
      }
    }),
  );
  const surface = document.createElement('canvas');
  surface.width = 1920;
  surface.height = 1080;
  const ctx = surface.getContext('2d');
  if (!ctx) throw new Error('浏览器不支持封面绘制');
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.fillStyle = '#e9e4ee';
  ctx.fillRect(0, 0, 1920, 1080);
  const background = resources.get('background');
  if (background) fillImage(ctx, background);
  const bounds: Bounds[] = [];
  const record = (role: string, box: Omit<Bounds, 'role'>) => bounds.push({ role, ...box });
  const color = PALETTE[isUtage ? 5 : (chart.difficulty ?? chart.difficulty_index)] || PALETTE[4];
  const gameAssets: string[] = [];
  const card = layout.card;
  ctx.save();
  ctx.shadowColor = '#27153635';
  ctx.shadowBlur = 48;
  ctx.shadowOffsetY = 18;
  rounded(ctx, card.x, card.y, card.width, card.height, 40, 'rgba(255,255,255,.88)');
  ctx.restore();
  ctx.strokeStyle = 'rgba(255,255,255,.94)';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.roundRect(card.x, card.y, card.width, card.height, 40);
  ctx.stroke();
  const jacketBox = layout.jacket;
  ctx.save();
  ctx.shadowBlur = 26;
  ctx.shadowOffsetY = 10;
  ctx.shadowColor = '#28163328';
  rounded(ctx, jacketBox.x, jacketBox.y, jacketBox.width, jacketBox.height, 21, '#fff');
  ctx.restore();
  ctx.save();
  ctx.beginPath();
  ctx.roundRect(jacketBox.x, jacketBox.y, jacketBox.width, jacketBox.height, 21);
  ctx.clip();
  ctx.drawImage(jacket, jacketBox.x, jacketBox.y, jacketBox.width, jacketBox.height);
  ctx.restore();
  record('jacket', jacketBox);
  function badge(key: string, label: string, box: Omit<Bounds, 'role'>, size: number) {
    const image = resources.get(key),
      asset = ui?.assets[key];
    if (image && asset) {
      record(key, contain(ctx!, image, box, asset.crop));
      gameAssets.push(key);
    } else
      record(
        key,
        measuredText(ctx!, label, box, size, key === 'ap' || key === 'app' ? '#a36d14' : color),
      );
  }
  if (typeKey) badge(typeKey, chart.type === 'dx' ? 'DX' : 'STD', layout.type, 35);
  const logo = resources.get('logo');
  if (logo && !widescreen) record('version_logo', contain(ctx, logo, layout.logo));
  const name = chart.display_title || chart.title || '　';
  const titleBox = layout.title;
  const title = fitTitle(ctx, name, titleBox.width, titleBox.height, widescreen ? 108 : 76);
  const titleTop = titleBox.y + (titleBox.height - title.lines.length * title.lineHeight) / 2;
  ctx.font = `800 ${title.fontSize}px ${SANS}`;
  ctx.fillStyle = '#302637';
  ctx.textBaseline = 'top';
  title.lines.forEach((line, index) => {
    const y = titleTop + index * title.lineHeight;
    ctx.fillText(line, titleBox.x, y);
    record('title:' + index, {
      x: titleBox.x,
      y,
      width: title.widths[index],
      height: title.lineHeight,
    });
  });
  ctx.textBaseline = 'alphabetic';
  const constantBox = layout.constant;
  const difficultyText = isUtage
    ? 'Utage'
    : ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'][
        chart.difficulty ?? chart.difficulty_index
      ] || chart.difficulty_name;
  const utageLevel = isUtage ? chart.level.match(/^(\d+)(\+?\?)$/) : null;
  const bodySize = widescreen ? 60 : 52;
  const decimalSize = widescreen ? (60 * 31) / 52 : 31;
  const superscriptSize = widescreen ? (60 * 28) / 52 : 28;
  const [integer, decimal] = utageLevel
    ? [utageLevel[1], '']
    : chart.constant?.toFixed(1).split('.') || ['', ''];
  ctx.font = `800 ${bodySize}px ${SANS}`;
  const prefixAscent = ctx.measureText(difficultyText).actualBoundingBoxAscent;
  const integerAscent = ctx.measureText(integer).actualBoundingBoxAscent;
  const baseline = constantBox.y + Math.max(prefixAscent, integerAscent);
  const prefix = visibleText(
    ctx,
    difficultyText,
    constantBox.x,
    baseline - prefixAscent,
    bodySize,
    color,
    800,
  );
  record('constant_prefix', prefix);
  if (utageLevel) {
    const main = visibleText(
      ctx,
      integer,
      prefix.x + prefix.width + 18,
      baseline - integerAscent,
      bodySize,
      color,
    );
    record('utage_integer', main);
    record(
      'utage_superscript',
      visibleText(
        ctx,
        utageLevel[2],
        main.x + main.width + 5,
        constantBox.y - 4,
        superscriptSize,
        color,
      ),
    );
  } else if (chart.constant !== null) {
    const main = visibleText(
      ctx,
      integer,
      prefix.x + prefix.width + 18,
      baseline - integerAscent,
      bodySize,
      color,
    );
    record('constant_integer', main);
    const fraction = visibleText(
      ctx,
      '.' + decimal,
      main.x + main.width + 3,
      baseline - 14,
      decimalSize,
      color,
    );
    record('constant_decimal', fraction);
    if (chart.level.includes('+'))
      record(
        'constant_plus',
        visibleText(
          ctx,
          '+',
          fraction.x + fraction.width + 5,
          constantBox.y - 4,
          superscriptSize,
          color,
        ),
      );
  }
  if (logo && widescreen) {
    const constantBounds = bounds.filter(
      (box) => box.role.startsWith('constant_') || box.role.startsWith('utage_'),
    );
    const constantRight = Math.max(...constantBounds.map((box) => box.x + box.width));
    const textCenter =
      (Math.min(...constantBounds.map((box) => box.y)) +
        Math.max(...constantBounds.map((box) => box.y + box.height))) /
      2;
    const right = layout.logo.x + layout.logo.width;
    const availableWidth = Math.max(1, right - Math.max(layout.logo.x, constantRight + 18));
    const crop = alphaBounds(logo);
    const scale = Math.min(availableWidth / crop.width, layout.logo.height / crop.height);
    const box = {
      x: right - crop.width * scale,
      y: textCenter - (crop.height * scale) / 2,
      width: crop.width * scale,
      height: crop.height * scale,
    };
    record('version_logo', contain(ctx, logo, box, crop));
  }
  const score = Number(raw).toFixed(4) + '%';
  const numberImage = numberSet?.file ? glyphImages.get(numberSet.file) || null : null;
  let achievementStyle: CoverReceipt['achievementStyle'] = 'system_font';
  let scoreBounds: Omit<Bounds, 'role'> | null = null;
  const row = layout.achievement;
  const gap = widescreen ? 34 : 30;
  const bottom = jacketBox.y + jacketBox.height;
  const maxNumberWidth = row.width - row.height - gap;
  if (numberSet) {
    const visual = spriteVisualBounds(numberSet, score, numberImage, glyphImages);
    const native = spriteNumberLayout(numberSet, score, 1);
    if (visual && native) {
      const scale = Math.min(row.height / visual.height, maxNumberWidth / visual.width);
      const baseHeight = 1 / native.scale;
      const fullWidth = (native.width / native.scale) * scale;
      const x = row.x - (visual.left - native.minX) * scale,
        y = bottom - (visual.bottom - native.minY) * scale;
      const drawn = drawSpriteNumber(
        ctx,
        numberImage,
        numberSet,
        score,
        { x, y, width: fullWidth + 0.01, height: baseHeight * scale },
        glyphImages,
      );
      if (drawn) {
        scoreBounds = {
          x: row.x,
          y: bottom - visual.height * scale,
          width: visual.width * scale,
          height: visual.height * scale,
        };
        achievementStyle = 'sprite';
      }
    }
  }
  if (!scoreBounds) {
    const fallback = measuredText(
      ctx,
      score,
      { x: row.x, y: bottom - row.height, width: maxNumberWidth, height: row.height },
      92,
      '#d5a117',
      800,
    );
    scoreBounds = fallback;
  }
  record('achievement', scoreBounds);
  const apImage = resources.get(apKey),
    apAsset = ui?.assets[apKey];
  const apSize = scoreBounds.height;
  const apBox = {
    x: scoreBounds.x + scoreBounds.width + gap,
    y: bottom - apSize,
    width: apSize,
    height: apSize,
  };
  if (apImage && apAsset) {
    const alpha = alphaBounds(apImage);
    record(apKey, contain(ctx, apImage, apBox, alpha));
    gameAssets.push(apKey);
  } else badge(apKey, Number(raw) === 101 ? 'AP+' : 'AP', apBox, 46);
  if (achievementStyle === 'system_font') notices.push('达成率暂用系统字体');
  const cropped = options.aspect === '4:3';
  canvas.width = cropped ? 1440 : 1920;
  canvas.height = 1080;
  const output = canvas.getContext('2d');
  if (!output) throw new Error('浏览器不支持封面绘制');
  output.drawImage(surface, cropped ? 240 : 0, 0, canvas.width, 1080, 0, 0, canvas.width, 1080);
  return {
    width: canvas.width,
    height: canvas.height,
    version,
    title,
    bounds,
    notices: [...new Set(notices)],
    gameAssets,
    achievementStyle,
    constantStyle: 'styled_text',
  };
}
