import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, messageOf } from './api';
import { VenueMap, type VenueMapPoint } from './VenueMap';
import { venuePoint, type VenueLocation, type VenuePlace } from './venueTypes';

type SearchPlace = Pick<VenueLocation, 'place_name' | 'address'> & VenueMapPoint;

export function VenueLocationEditor({
  venue,
  venues = [],
  onClose,
  onSaved,
}: {
  venue: { name: string; location: VenueLocation | null };
  venues?: VenuePlace[];
  onClose: () => void;
  onSaved: (name: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(venue.name);
  const [placeName, setPlaceName] = useState(venue.location?.place_name || '');
  const [address, setAddress] = useState(venue.location?.address || '');
  const [point, setPoint] = useState(venuePoint(venue.location));
  const [focus, setFocus] = useState<(VenueMapPoint & { zoom?: number }) | undefined>(
    point ? { ...point, zoom: 16 } : undefined,
  );
  const [accuracy, setAccuracy] = useState<VenueLocation['accuracy']>(
    venue.location?.accuracy || 'unknown',
  );
  const [closed, setClosed] = useState<VenueLocation['closed_status']>(
    venue.location?.closed_status || 'unknown',
  );
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchPlace[]>([]);
  const [searchMessage, setSearchMessage] = useState('');
  const [searchBusy, setSearchBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const searchRequest = useRef<AbortController | null>(null);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    const previous = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    const hadOverlay = document.body.classList.contains('has-overlay');
    document.body.classList.add('has-overlay');
    element?.showModal();
    return () => {
      active.current = false;
      searchRequest.current?.abort();
      element?.close();
      if (!hadOverlay) document.body.classList.remove('has-overlay');
      previous?.focus();
    };
  }, []);

  async function search(event: FormEvent) {
    event.preventDefault();
    if (!query.trim() || searchBusy) return;
    searchRequest.current?.abort();
    const controller = new AbortController();
    searchRequest.current = controller;
    setSearchBusy(true);
    setSearchMessage('');
    setResults([]);
    try {
      const result = await api<{ items: SearchPlace[] }>(
        '/venues/search?' +
          new URLSearchParams({ q: query.trim(), city: /^【([^】]+)】/.exec(name)?.[1] || '' }),
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setResults(result.items);
      if (!result.items.length) setSearchMessage('没有找到地点，可以直接在地图上选点。');
    } catch (error) {
      if (!controller.signal.aborted) setSearchMessage(messageOf(error));
    } finally {
      if (!controller.signal.aborted) setSearchBusy(false);
    }
  }

  function pick(next: VenueMapPoint) {
    setPoint(next);
    setAccuracy('exact');
  }

  function locate() {
    if (!navigator.geolocation) {
      setError('此浏览器不支持定位，请在地图上选点。');
      return;
    }
    setError('');
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        if (!active.current) return;
        const next = { latitude: coords.latitude, longitude: coords.longitude };
        setPoint(next);
        setFocus({ ...next, zoom: 17 });
        setAccuracy('approximate');
      },
      () => {
        if (active.current) setError('无法取得当前位置，请在地图上选点。');
      },
      { enableHighAccuracy: true, timeout: 10000 },
    );
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!point && !address.trim()) {
      setError('请在地图上选点，或填写地址。');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api('/venues/location', {
        method: 'PUT',
        body: JSON.stringify({
          name: name.trim(),
          place_name: placeName.trim(),
          address: address.trim(),
          latitude: point?.latitude ?? null,
          longitude: point?.longitude ?? null,
          accuracy: point ? accuracy : 'unknown',
          closed_status: closed,
        }),
      });
      if (active.current) onSaved(name.trim());
    } catch (error) {
      if (active.current) setError(messageOf(error));
    } finally {
      if (active.current) setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialog}
      className="venue-editor"
      aria-labelledby="venue-editor-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <div className="venue-editor-body">
        <header className="venue-editor-heading">
          <h2 id="venue-editor-title">{venue.name ? '编辑机厅位置' : '新增机厅'}</h2>
          <button className="btn quiet" type="button" disabled={busy} onClick={onClose}>
            关闭
          </button>
        </header>
        <form className="venue-place-search" onSubmit={search}>
          <label className="sr-only" htmlFor="venue-place-query">
            搜索地点
          </label>
          <input
            id="venue-place-query"
            type="search"
            placeholder="搜索城市、商场或地址"
            value={query}
            maxLength={200}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="btn" disabled={searchBusy || !query.trim()}>
            {searchBusy ? '搜索中…' : '搜索'}
          </button>
          <button className="btn" type="button" onClick={locate}>
            当前位置
          </button>
        </form>
        {searchMessage && (
          <p className="form-message" role="status">
            {searchMessage}
          </p>
        )}
        {!!results.length && (
          <div className="venue-place-results">
            {results.map((result, index) => (
              <button
                type="button"
                key={index}
                onClick={() => {
                  setPlaceName(result.place_name);
                  setAddress(result.address);
                  setPoint({ latitude: result.latitude, longitude: result.longitude });
                  setFocus({ latitude: result.latitude, longitude: result.longitude, zoom: 17 });
                  setAccuracy('approximate');
                  setResults([]);
                }}
              >
                <strong>{result.place_name}</strong>
                <span>{result.address}</span>
              </button>
            ))}
            <a href="https://photon.komoot.io/" target="_blank" rel="noreferrer">
              地点搜索 · Photon / OpenStreetMap
            </a>
          </div>
        )}
        <VenueMap
          venues={venues.map((item) => ({ ...item, location: venuePoint(item.location) }))}
          point={point}
          onPick={pick}
          focusPoint={focus}
        />
        <div className="venue-editor-point">
          <span>
            {point
              ? `${point.latitude.toFixed(6)}, ${point.longitude.toFixed(6)}`
              : '点击地图选择位置'}
          </span>
          {point && (
            <button type="button" className="btn quiet" onClick={() => setPoint(null)}>
              清除点位
            </button>
          )}
        </div>
        <form onSubmit={save} className="venue-editor-form">
          <label className="field">
            机厅名称
            <input
              required
              maxLength={120}
              value={name}
              readOnly={!!venue.name}
              placeholder="例如：【北京】上地"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="field">
            实际门店
            <input
              value={placeName}
              maxLength={200}
              onChange={(event) => setPlaceName(event.target.value)}
            />
          </label>
          <label className="field venue-editor-address">
            地址
            <input
              value={address}
              maxLength={500}
              onChange={(event) => setAddress(event.target.value)}
            />
          </label>
          <label className="field">
            营业状态
            <select
              value={closed}
              onChange={(event) => setClosed(event.target.value as VenueLocation['closed_status'])}
            >
              <option value="unknown">未记录</option>
              <option value="open">营业中</option>
              <option value="closed">已闭店</option>
              <option value="probably_closed">可能已闭店</option>
            </select>
          </label>
          <details className="venue-editor-coordinates">
            <summary>经纬度（WGS84）</summary>
            <label>
              纬度
              <input
                aria-label="纬度"
                type="number"
                step="any"
                min={-85}
                max={85}
                value={point?.latitude ?? ''}
                onChange={(event) => {
                  if (event.target.value !== '')
                    pick({
                      latitude: Number(event.target.value),
                      longitude: point?.longitude ?? 0,
                    });
                }}
              />
            </label>
            <label>
              经度
              <input
                aria-label="经度"
                type="number"
                step="any"
                min={-180}
                max={180}
                value={point?.longitude ?? ''}
                onChange={(event) => {
                  if (event.target.value !== '')
                    pick({ longitude: Number(event.target.value), latitude: point?.latitude ?? 0 });
                }}
              />
            </label>
          </details>
          {error && (
            <div className="error-box venue-editor-address" role="alert">
              {error}
            </div>
          )}
          <footer className="venue-editor-actions">
            <button type="button" className="btn" disabled={busy} onClick={onClose}>
              取消
            </button>
            <button className="btn primary" disabled={busy}>
              {busy ? '保存中…' : '保存位置'}
            </button>
          </footer>
        </form>
      </div>
    </dialog>
  );
}
