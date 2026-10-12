import { useEffect, useRef, useState, type FormEvent } from 'react';
import { api, messageOf } from './api';
import { VenueMap, type VenueMapPoint } from './VenueMap';
import { venuePoint, type VenueLocation, type VenuePlace } from './venueTypes';
import { useNotify } from './Toast';

type SearchPlace = VenueMapPoint & { place_name: string; address: string };

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
  const [point, setPoint] = useState(venuePoint(venue.location));
  const [focus, setFocus] = useState<(VenueMapPoint & { zoom?: number }) | undefined>(
    point ? { ...point, zoom: 16 } : undefined,
  );
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchPlace[]>([]);
  const [searchBusy, setSearchBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const notify = useNotify();
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
    setResults([]);
    try {
      const result = await api<{ items: SearchPlace[] }>(
        '/venues/search?' +
          new URLSearchParams({ q: query.trim(), city: /^【([^】]+)】/.exec(name)?.[1] || '' }),
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setResults(result.items);
      if (!result.items.length) notify('没有找到地点，请加上城市或商场名称再试。');
    } catch (error) {
      if (!controller.signal.aborted) notify(messageOf(error));
    } finally {
      if (!controller.signal.aborted) setSearchBusy(false);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!point) {
      notify('请先搜索并选择地点。');
      return;
    }
    setBusy(true);
    try {
      await api('/venues/location', {
        method: 'PUT',
        body: JSON.stringify({
          name: name.trim(),
          latitude: point.latitude,
          longitude: point.longitude,
        }),
      });
      if (active.current) onSaved(name.trim());
    } catch (error) {
      if (active.current) notify(messageOf(error));
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
            placeholder="搜索机厅、商场或地点名称"
            value={query}
            maxLength={200}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="btn" disabled={searchBusy || !query.trim()}>
            {searchBusy ? '搜索中…' : '搜索'}
          </button>
        </form>
        {!!results.length && (
          <div className="venue-place-results">
            {results.map((result, index) => (
              <button
                type="button"
                key={index}
                onClick={() => {
                  setQuery(result.place_name);
                  if (!name.trim()) setName(result.place_name);
                  setPoint({ latitude: result.latitude, longitude: result.longitude });
                  setFocus({ latitude: result.latitude, longitude: result.longitude, zoom: 17 });
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
        {point && (
          <VenueMap
            venues={venues.map((item) => ({ ...item, location: venuePoint(item.location) }))}
            point={point}
            onPick={setPoint}
            focusPoint={focus}
          />
        )}
        <form onSubmit={save} className="venue-editor-form">
          <label className="field">
            显示名称
            <input
              required
              maxLength={120}
              value={name}
              readOnly={!!venue.name}
              placeholder="例如：【北京】上地"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <footer className="venue-editor-actions">
            <button type="button" className="btn" disabled={busy} onClick={onClose}>
              取消
            </button>
            <button className="btn primary" disabled={busy || !point || !name.trim()}>
              {busy ? '保存中…' : '保存'}
            </button>
          </footer>
        </form>
      </div>
    </dialog>
  );
}
