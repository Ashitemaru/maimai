export type MapPoint = { latitude: number; longitude: number };
export type MapView = MapPoint & { zoom: number };
export type MapSize = { width: number; height: number };
export type MapPixel = { x: number; y: number };

export const MIN_MAP_ZOOM = 2;
export const MAX_MAP_ZOOM = 19;
const MAX_LATITUDE = 85.05112878;
const TILE_SIZE = 256;

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const wrap = (value: number, span: number) => ((value % span) + span) % span;

export function isMapPoint(point: MapPoint | null | undefined): point is MapPoint {
  return (
    !!point &&
    Number.isFinite(point.latitude) &&
    Number.isFinite(point.longitude) &&
    Math.abs(point.latitude) <= 90 &&
    Math.abs(point.longitude) <= 180
  );
}

export function projectPoint(point: MapPoint, zoom: number): MapPixel {
  const world = TILE_SIZE * 2 ** zoom;
  const latitude = (clamp(point.latitude, -MAX_LATITUDE, MAX_LATITUDE) * Math.PI) / 180;
  return {
    x: ((point.longitude + 180) / 360) * world,
    y: ((1 - Math.asinh(Math.tan(latitude)) / Math.PI) * world) / 2,
  };
}

export function unprojectPoint(point: MapPixel, zoom: number): MapPoint {
  const world = TILE_SIZE * 2 ** zoom;
  return {
    longitude: (wrap(point.x, world) / world) * 360 - 180,
    latitude:
      (Math.atan(Math.sinh(Math.PI * (1 - 2 * clamp(point.y / world, 0, 1)))) * 180) / Math.PI,
  };
}

export function pointToScreen(point: MapPoint, view: MapView, size: MapSize): MapPixel {
  const projected = projectPoint(point, view.zoom);
  const center = projectPoint(view, view.zoom);
  const world = TILE_SIZE * 2 ** view.zoom;
  return {
    x: size.width / 2 + wrap(projected.x - center.x + world / 2, world) - world / 2,
    y: size.height / 2 + projected.y - center.y,
  };
}

export function screenToPoint(point: MapPixel, view: MapView, size: MapSize): MapPoint {
  const center = projectPoint(view, view.zoom);
  return unprojectPoint(
    { x: center.x + point.x - size.width / 2, y: center.y + point.y - size.height / 2 },
    view.zoom,
  );
}

export function panView(view: MapView, dx: number, dy: number): MapView {
  const center = projectPoint(view, view.zoom);
  return { ...unprojectPoint({ x: center.x - dx, y: center.y - dy }, view.zoom), zoom: view.zoom };
}

export function zoomView(view: MapView, zoom: number, anchor: MapPixel, size: MapSize): MapView {
  const nextZoom = clamp(zoom, MIN_MAP_ZOOM, MAX_MAP_ZOOM);
  const point = screenToPoint(anchor, view, size);
  const projected = projectPoint(point, nextZoom);
  return {
    ...unprojectPoint(
      { x: projected.x - anchor.x + size.width / 2, y: projected.y - anchor.y + size.height / 2 },
      nextZoom,
    ),
    zoom: nextZoom,
  };
}

export function fitMapPoints(points: MapPoint[], size: MapSize, padding = 80): MapView {
  const valid = points.filter(isMapPoint);
  if (!valid.length) return { longitude: 116.4, latitude: 35, zoom: 4 };
  if (valid.length === 1) return { ...valid[0], zoom: 14 };
  const projected = valid.map((point) => projectPoint(point, 0));
  const xs = projected.map((point) => wrap(point.x, TILE_SIZE)).sort((a, b) => a - b);
  let gap = -1;
  let start = xs[0];
  for (let index = 0; index < xs.length; index++) {
    const next = index + 1 === xs.length ? xs[0] + TILE_SIZE : xs[index + 1];
    if (next - xs[index] > gap) {
      gap = next - xs[index];
      start = wrap(next, TILE_SIZE);
    }
  }
  const spanX = TILE_SIZE - gap;
  const minY = Math.min(...projected.map((point) => point.y));
  const maxY = Math.max(...projected.map((point) => point.y));
  const zoom = Math.min(
    Math.log2(Math.max(1, size.width - padding * 2) / Math.max(spanX, 0.001)),
    Math.log2(Math.max(1, size.height - padding * 2) / Math.max(maxY - minY, 0.001)),
  );
  return {
    ...unprojectPoint({ x: start + spanX / 2, y: (minY + maxY) / 2 }, 0),
    zoom: clamp(zoom, MIN_MAP_ZOOM, 16),
  };
}

export type MapTile = {
  key: string;
  z: number;
  x: number;
  y: number;
  left: number;
  top: number;
  size: number;
};

export function visibleMapTiles(view: MapView, size: MapSize): MapTile[] {
  const z = Math.floor(view.zoom);
  const scale = 2 ** (view.zoom - z);
  const tileSize = TILE_SIZE * scale;
  const center = projectPoint(view, view.zoom);
  const left = center.x - size.width / 2;
  const top = center.y - size.height / 2;
  const count = 2 ** z;
  const tiles: MapTile[] = [];
  for (let x = Math.floor(left / tileSize); x <= Math.floor((left + size.width) / tileSize); x++) {
    for (
      let y = Math.max(0, Math.floor(top / tileSize));
      y <= Math.min(count - 1, Math.floor((top + size.height) / tileSize));
      y++
    ) {
      tiles.push({
        key: `${z}/${x}/${y}`,
        z,
        x: wrap(x, count),
        y,
        left: x * tileSize - left,
        top: y * tileSize - top,
        size: tileSize,
      });
    }
  }
  return tiles;
}

export type MapCluster<T> = MapPixel & { items: T[] };

/** Screen-space connected components keep nearby jackets in one tappable stack. */
export function clusterMapPoints<T>(
  points: { item: T; x: number; y: number }[],
  radius = 82,
): MapCluster<T>[] {
  const parent = points.map((_, index) => index);
  function root(index: number): number {
    while (parent[index] !== index) {
      parent[index] = parent[parent[index]];
      index = parent[index];
    }
    return index;
  }
  for (let a = 0; a < points.length; a++) {
    for (let b = a + 1; b < points.length; b++) {
      if (Math.hypot(points[a].x - points[b].x, points[a].y - points[b].y) < radius)
        parent[root(b)] = root(a);
    }
  }
  const groups = new Map<number, MapCluster<T>>();
  points.forEach((point, index) => {
    const key = root(index);
    const group = groups.get(key) || { x: 0, y: 0, items: [] };
    group.x += point.x;
    group.y += point.y;
    group.items.push(point.item);
    groups.set(key, group);
  });
  return [...groups.values()].map((group) => ({
    ...group,
    x: group.x / group.items.length,
    y: group.y / group.items.length,
  }));
}
