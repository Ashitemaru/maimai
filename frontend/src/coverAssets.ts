const BACKGROUND_MANIFEST_URL = '/cover-assets/version-backgrounds/manifest.json';
export const GAME_UI_MANIFEST_URL = '/cover-assets/game-ui/manifest.json';
interface VersionAsset {
  family_key: string;
  version_name: string;
  logo_file: string;
  background_png: string | null;
  status: string;
  jp_version_code?: number;
}
export interface BackgroundManifest {
  version_map: Record<string, VersionAsset>;
}
export interface ImageAsset {
  file: string;
  crop?: { x: number; y: number; width: number; height: number };
  [key: string]: unknown;
}
interface Glyph {
  file?: string;
  x?: number;
  y?: number;
  width: number;
  height: number;
  advance?: number;
  offset_x?: number;
  offset_y?: number;
}
export interface NumberSet {
  file?: string;
  glyphs: Record<string, Glyph>;
  line_height?: number;
  [key: string]: unknown;
}
interface GameUIManifest {
  assets: Record<string, ImageAsset>;
  number_sets: Record<string, NumberSet>;
}
interface VersionChart {
  difficulty_index: number;
  jp_release_version?: string | null;
  jp_release_version_candidate?: string | null;
  jp_release_provider?: string | null;
  release_status?: string;
}
export interface ResolvedVersion {
  key: string | null;
  asset: VersionAsset | null;
  notice: string;
  source: 'verified' | 'arcade_songs' | 'manual' | 'unresolved';
  versionName?: string;
}
let backgrounds: Promise<BackgroundManifest> | undefined;
let gameUI: Promise<GameUIManifest | null> | undefined;
export function loadBackgroundManifest() {
  return (backgrounds ??= fetch(BACKGROUND_MANIFEST_URL)
    .then(async (response) => {
      if (!response.ok) throw new Error('版本素材暂时无法载入');
      const data = (await response.json()) as BackgroundManifest;
      if (!data.version_map) throw new Error('版本素材目录格式不完整');
      return data;
    })
    .catch((error) => {
      backgrounds = undefined;
      throw error;
    }));
}
export function loadGameUIManifest() {
  return (gameUI ??= fetch(GAME_UI_MANIFEST_URL)
    .then(async (response) => {
      if (!response.ok) throw new Error('游戏素材暂时无法载入');
      const data = (await response.json()) as GameUIManifest;
      if (!data.assets) throw new Error('游戏素材目录格式不完整');
      return data;
    })
    .catch(() => {
      gameUI = undefined;
      return null;
    }));
}
export function assetURL(file: string, manifestURL = BACKGROUND_MANIFEST_URL) {
  return new URL(file, new URL(manifestURL, location.origin)).href;
}
function normalizeVersion(value: string) {
  return (
    value
      .toLowerCase()
      .replace(/maimai\s*/g, '')
      .replace(/でらっくす|deluxe/g, 'dx')
      .replace(/[^a-z0-9]/g, '') || 'maimai'
  );
}
export function resolveVersion(
  chart: VersionChart,
  manifest: BackgroundManifest,
  override = '',
): ResolvedVersion {
  const value = override || chart.jp_release_version || '';
  const source: ResolvedVersion['source'] =
    override || chart.jp_release_provider === 'manual'
      ? 'manual'
      : !value
        ? 'unresolved'
        : chart.jp_release_provider === 'arcade_songs'
          ? 'arcade_songs'
          : 'verified';
  if (!value)
    return { key: null, asset: null, source: 'unresolved', notice: '谱面版本待核实，请选择版本' };
  const normalized = normalizeVersion(value);
  const entry = Object.entries(manifest.version_map).find(
    ([key, asset]) =>
      key === value ||
      normalizeVersion(asset.version_name) === normalized ||
      normalizeVersion(key) === normalized,
  );
  const notices = [];
  if (!override && chart.release_status === 'conflict') notices.push('版本有冲突，暂用已保留版本');
  if (!entry)
    return {
      key: null,
      asset: null,
      source,
      versionName: value,
      notice: [...notices, `${value} 的版本素材尚未收录`].join(' · '),
    };
  const [key, asset] = entry;
  if (asset.status !== 'ready' || !asset.background_png) notices.push('这个版本的底图尚未就绪');
  return { key, asset, source, versionName: value, notice: notices.join(' · ') };
}
