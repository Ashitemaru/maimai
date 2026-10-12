import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import {
  clusterMapPoints,
  fitMapPoints,
  isMapPoint,
  MAX_MAP_ZOOM,
  MIN_MAP_ZOOM,
  panView,
  pointToScreen,
  screenToPoint,
  visibleMapTiles,
  zoomView,
} from './venueMapGeometry';
import type { MapCluster, MapPixel, MapPoint, MapSize, MapView } from './venueMapGeometry';

export type VenueMapPoint = MapPoint;
type VenueMapVenue = {
  name: string;
  total: number;
  jackets: {
    chart_key: string;
    title: string;
    jacket_url: string | null;
    difficulty: number | string;
    level: string;
  }[];
  location: MapPoint | null;
};
type Props = {
  venues: VenueMapVenue[];
  selectedName?: string | null;
  onSelect?: (name: string) => void;
  point?: MapPoint | null;
  onPick?: (point: MapPoint) => void;
  focusPoint?: (MapPoint & { zoom?: number }) | null;
};
type LocatedVenue = VenueMapVenue & { location: MapPoint };

const centerOf = (points: MapPixel[]) => ({
  x: points.reduce((sum, point) => sum + point.x, 0) / points.length,
  y: points.reduce((sum, point) => sum + point.y, 0) / points.length,
});
const distance = (points: MapPixel[]) =>
  Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);

export function VenueMap({ venues, selectedName, onSelect, point, onPick, focusPoint }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const canvas = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState<MapSize>({ width: 0, height: 0 });
  const [view, setView] = useState<MapView>({ longitude: 116.4, latitude: 35, zoom: 4 });
  const viewRef = useRef(view);
  const sizeRef = useRef(size);
  const fitted = useRef(false);
  const pointers = useRef(new Map<number, MapPixel>());
  const pressedCluster = useRef<MapCluster<LocatedVenue> | null>(null);
  const gesture = useRef({ moved: false, start: { x: 0, y: 0 } });
  const [failedTiles, setFailedTiles] = useState<Set<string>>(new Set());
  const [retry, setRetry] = useState(0);
  const [overlapping, setOverlapping] = useState<LocatedVenue[]>([]);
  const clipId = `venue-map-${useId().replaceAll(':', '')}`;
  const located = useMemo(
    () => venues.filter((venue): venue is LocatedVenue => isMapPoint(venue.location)),
    [venues],
  );

  const changeView = useCallback((next: MapView) => {
    viewRef.current = next;
    setView(next);
  }, []);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const next = { width: entry.contentRect.width, height: entry.contentRect.height };
      sizeRef.current = next;
      setSize(next);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (fitted.current || !size.width || !size.height || (!located.length && !onPick)) return;
    fitted.current = true;
    changeView(fitMapPoints(point ? [point] : located.map((venue) => venue.location), size));
  }, [located, onPick, point, size, changeView]);

  const focusLatitude = focusPoint?.latitude;
  const focusLongitude = focusPoint?.longitude;
  const focusZoom = focusPoint?.zoom;
  useEffect(() => {
    if (focusLatitude === undefined || focusLongitude === undefined) return;
    const next = { latitude: focusLatitude, longitude: focusLongitude };
    if (!isMapPoint(next)) return;
    changeView({ ...next, zoom: Math.min(MAX_MAP_ZOOM, Math.max(MIN_MAP_ZOOM, focusZoom ?? 16)) });
    fitted.current = true;
  }, [focusLatitude, focusLongitude, focusZoom, changeView]);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    function wheel(event: WheelEvent) {
      event.preventDefault();
      const bounds = element!.getBoundingClientRect();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? bounds.height : 1;
      changeView(
        zoomView(
          viewRef.current,
          viewRef.current.zoom - event.deltaY * unit * 0.003,
          { x: event.clientX - bounds.left, y: event.clientY - bounds.top },
          sizeRef.current,
        ),
      );
    }
    element.addEventListener('wheel', wheel, { passive: false });
    return () => element.removeEventListener('wheel', wheel);
  }, [changeView]);

  const tiles = useMemo(() => (size.width ? visibleMapTiles(view, size) : []), [view, size]);
  const tileFailed = tiles.some((tile) => failedTiles.has(tile.key));
  const clusters = useMemo(
    () =>
      clusterMapPoints(
        located
          .map((venue) => ({ item: venue, ...pointToScreen(venue.location, view, size) }))
          .filter(
            (point) =>
              point.x >= -80 &&
              point.y >= -80 &&
              point.x <= size.width + 80 &&
              point.y <= size.height + 80,
          ),
      ),
    [located, view, size],
  );

  function localPoint(event: PointerEvent<SVGSVGElement>): MapPixel {
    const bounds = event.currentTarget.getBoundingClientRect();
    return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
  }

  function pointerDown(event: PointerEvent<SVGSVGElement>) {
    if (event.button !== 0) return;
    const current = localPoint(event);
    if (!pointers.current.size) gesture.current = { moved: false, start: current };
    const marker = (event.target as Element).closest('[data-cluster-key]');
    pressedCluster.current =
      clusters.find(
        (cluster) =>
          cluster.items
            .map((venue) => venue.name)
            .sort()
            .join('|') === marker?.getAttribute('data-cluster-key'),
      ) || null;
    pointers.current.set(event.pointerId, current);
    if (pointers.current.size > 1) gesture.current.moved = true;
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: PointerEvent<SVGSVGElement>) {
    if (!pointers.current.has(event.pointerId)) return;
    const before = [...pointers.current.values()];
    const current = localPoint(event);
    pointers.current.set(event.pointerId, current);
    const after = [...pointers.current.values()];
    if (Math.hypot(current.x - gesture.current.start.x, current.y - gesture.current.start.y) > 4)
      gesture.current.moved = true;
    const previousCenter = centerOf(before);
    const nextCenter = centerOf(after);
    let next = viewRef.current;
    if (before.length === 2 && after.length === 2 && distance(before) > 1 && distance(after) > 1) {
      next = zoomView(
        next,
        next.zoom + Math.log2(distance(after) / distance(before)),
        previousCenter,
        sizeRef.current,
      );
    }
    changeView(panView(next, nextCenter.x - previousCenter.x, nextCenter.y - previousCenter.y));
  }

  function pointerUp(event: PointerEvent<SVGSVGElement>) {
    const wasTracked = pointers.current.delete(event.pointerId);
    if (event.currentTarget.hasPointerCapture(event.pointerId))
      event.currentTarget.releasePointerCapture(event.pointerId);
    if (wasTracked && !gesture.current.moved && onPick)
      onPick(screenToPoint(localPoint(event), viewRef.current, sizeRef.current));
    else if (wasTracked && !gesture.current.moved && pressedCluster.current)
      activateCluster(pressedCluster.current);
    pressedCluster.current = null;
  }

  function activateCluster(cluster: MapCluster<LocatedVenue>) {
    if (gesture.current.moved) return;
    if (cluster.items.length === 1) {
      onSelect?.(cluster.items[0].name);
      setOverlapping([]);
      return;
    }
    if (viewRef.current.zoom >= MAX_MAP_ZOOM - 0.1) {
      setOverlapping(cluster.items);
      return;
    }
    const fit = fitMapPoints(
      cluster.items.map((venue) => venue.location),
      sizeRef.current,
    );
    changeView({
      ...fit,
      zoom: Math.min(MAX_MAP_ZOOM, Math.max(viewRef.current.zoom + 2, fit.zoom)),
    });
    setOverlapping([]);
  }

  function keyDown(event: KeyboardEvent<SVGSVGElement>) {
    if (event.target !== event.currentTarget) return;
    const shift = event.shiftKey ? 160 : 80;
    const directions: Record<string, [number, number]> = {
      ArrowLeft: [shift, 0],
      ArrowRight: [-shift, 0],
      ArrowUp: [0, shift],
      ArrowDown: [0, -shift],
    };
    if (directions[event.key]) {
      event.preventDefault();
      changeView(panView(viewRef.current, ...directions[event.key]));
    } else if (['+', '=', '-'].includes(event.key)) {
      event.preventDefault();
      changeView(
        zoomView(
          viewRef.current,
          viewRef.current.zoom + (event.key === '-' ? -1 : 1),
          { x: size.width / 2, y: size.height / 2 },
          size,
        ),
      );
    } else if (event.key === 'Enter' && onPick) {
      event.preventDefault();
      onPick({ latitude: view.latitude, longitude: view.longitude });
    }
  }

  function changeZoom(delta: number) {
    changeView(
      zoomView(
        viewRef.current,
        viewRef.current.zoom + delta,
        { x: size.width / 2, y: size.height / 2 },
        size,
      ),
    );
  }

  const picked = point && isMapPoint(point) ? pointToScreen(point, view, size) : null;
  return (
    <div className="venue-map" data-picking={!!onPick}>
      <div className="venue-map-canvas" ref={container}>
        <svg
          ref={canvas}
          className="venue-map-svg"
          viewBox={`0 0 ${size.width || 1} ${size.height || 1}`}
          role="group"
          aria-label={
            onPick
              ? '选择机厅位置，可拖动和缩放地图，点击选点'
              : 'AP 机厅地图，可拖动和缩放，点击曲绘查看机厅'
          }
          tabIndex={0}
          onPointerDown={pointerDown}
          onPointerMove={pointerMove}
          onPointerUp={pointerUp}
          onPointerCancel={(event) => {
            pointers.current.delete(event.pointerId);
            gesture.current.moved = true;
          }}
          onKeyDown={keyDown}
        >
          <defs>
            <clipPath id={clipId}>
              <rect x="-27" y="-32" width="54" height="54" rx="10" />
            </clipPath>
          </defs>
          <g className="venue-map-tiles" aria-hidden="true">
            {tiles.map((tile) => (
              <image
                key={`${tile.key}:${retry}`}
                x={tile.left}
                y={tile.top}
                width={tile.size + 0.5}
                height={tile.size + 0.5}
                href={`https://tile.openstreetmap.org/${tile.z}/${tile.x}/${tile.y}.png`}
                onError={() => setFailedTiles((previous) => new Set(previous).add(tile.key))}
                onLoad={() =>
                  setFailedTiles((previous) => {
                    if (!previous.has(tile.key)) return previous;
                    const next = new Set(previous);
                    next.delete(tile.key);
                    return next;
                  })
                }
              />
            ))}
          </g>
          {!onPick &&
            clusters.map((cluster) => {
              const items = [...cluster.items].sort(
                (a, b) => b.total - a.total || a.name.localeCompare(b.name),
              );
              const jackets = items
                .flatMap((venue) => venue.jackets)
                .filter((item) => item.jacket_url)
                .slice(0, 3);
              const jacket = jackets[0];
              const total = items.reduce((sum, venue) => sum + venue.total, 0);
              const selected = items.some((venue) => venue.name === selectedName);
              const label =
                items.length > 1
                  ? `${items.length} 家机厅，${total} AP，放大查看`
                  : `${items[0].name}，${total} AP`;
              return (
                <g
                  key={items.map((venue) => venue.name).join('|')}
                  className="venue-map-marker"
                  transform={`translate(${cluster.x} ${cluster.y})`}
                  role="button"
                  tabIndex={0}
                  aria-label={label}
                  data-selected={selected}
                  data-cluster-key={items
                    .map((venue) => venue.name)
                    .sort()
                    .join('|')}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      event.stopPropagation();
                      gesture.current.moved = false;
                      activateCluster(cluster);
                    }
                  }}
                >
                  <title>{label}</title>
                  {(items.length > 1 || jackets.length > 1) && (
                    <>
                      <rect
                        className="venue-map-stack"
                        x="-30"
                        y="-34"
                        width="60"
                        height="60"
                        rx="12"
                        transform="rotate(-12)"
                      />
                      <rect
                        className="venue-map-stack"
                        x="-30"
                        y="-34"
                        width="60"
                        height="60"
                        rx="12"
                        transform="rotate(9)"
                      />
                      {jackets
                        .slice(1)
                        .reverse()
                        .map((back, index) => (
                          <g
                            key={`${back.chart_key}:${index}`}
                            transform={`rotate(${index ? -12 : 9})`}
                          >
                            <image
                              href={back.jacket_url!}
                              x="-27"
                              y="-32"
                              width="54"
                              height="54"
                              preserveAspectRatio="xMidYMid slice"
                              clipPath={`url(#${clipId})`}
                            />
                          </g>
                        ))}
                    </>
                  )}
                  <rect
                    className="venue-map-frame"
                    x="-30"
                    y="-35"
                    width="60"
                    height="60"
                    rx="13"
                  />
                  {jacket ? (
                    <image
                      href={jacket.jacket_url!}
                      x="-27"
                      y="-32"
                      width="54"
                      height="54"
                      preserveAspectRatio="xMidYMid slice"
                      clipPath={`url(#${clipId})`}
                    />
                  ) : (
                    <text className="venue-map-placeholder" x="0" y="2" textAnchor="middle">
                      AP
                    </text>
                  )}
                  <rect
                    className="venue-map-count-pill"
                    x="-22"
                    y="14"
                    width="44"
                    height="25"
                    rx="12.5"
                  />
                  <text className="venue-map-count" x="0" y="31" textAnchor="middle">
                    {total}
                  </text>
                  {items.length === 1 && view.zoom >= 10 && (
                    <text className="venue-map-label" x="0" y="57" textAnchor="middle">
                      {items[0].name.replace(/^【[^】]+】/, '').slice(0, 15)}
                    </text>
                  )}
                </g>
              );
            })}
          {picked && (
            <g
              className="venue-map-picked"
              transform={`translate(${picked.x} ${picked.y})`}
              aria-label="选中的位置"
            >
              <circle r="15" />
              <circle r="5" />
            </g>
          )}
          {onPick && (
            <g
              className="venue-map-crosshair"
              transform={`translate(${size.width / 2} ${size.height / 2})`}
              aria-hidden="true"
            >
              <path d="M-11 0H11M0-11V11" />
              <circle r="4" />
            </g>
          )}
        </svg>
      </div>
      <div className="venue-map-controls">
        <button
          type="button"
          aria-label="放大地图"
          disabled={view.zoom >= MAX_MAP_ZOOM}
          onClick={() => changeZoom(1)}
        >
          +
        </button>
        <button
          type="button"
          aria-label="缩小地图"
          disabled={view.zoom <= MIN_MAP_ZOOM}
          onClick={() => changeZoom(-1)}
        >
          −
        </button>
        <button
          type="button"
          className="venue-map-fit"
          onClick={() => {
            setOverlapping([]);
            changeView(
              fitMapPoints(point ? [point] : located.map((venue) => venue.location), size),
            );
          }}
          aria-label={onPick ? '回到选中位置' : '显示全部机厅'}
        >
          ⌖
        </button>
      </div>
      {tileFailed && (
        <div className="venue-map-notice" role="status">
          地图暂时未加载
          <button
            type="button"
            onClick={() => {
              setFailedTiles(new Set());
              setRetry((value) => value + 1);
            }}
          >
            重试
          </button>
        </div>
      )}
      {onPick && (
        <button
          type="button"
          className="btn venue-map-pick-center"
          onClick={() => onPick({ latitude: view.latitude, longitude: view.longitude })}
        >
          选定地图中心
        </button>
      )}
      {overlapping.length > 0 && (
        <div className="venue-map-overlap" aria-label="同一位置的机厅">
          <button
            type="button"
            className="venue-map-overlap-close"
            aria-label="关闭同位置机厅"
            onClick={() => setOverlapping([])}
          >
            ×
          </button>
          {overlapping.map((venue) => (
            <button
              type="button"
              key={venue.name}
              onClick={() => {
                onSelect?.(venue.name);
                setOverlapping([]);
              }}
            >
              <span>{venue.name}</span>
              <strong>{venue.total} AP</strong>
            </button>
          ))}
        </div>
      )}
      <div className="venue-map-attribution">
        ©{' '}
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">
          OpenStreetMap
        </a>{' '}
        contributors
      </div>
    </div>
  );
}
