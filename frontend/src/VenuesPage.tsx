import { useEffect, useState } from 'react';
import { api, messageOf } from './api';
import { VenueMap } from './VenueMap';
import { VenueSummary } from './VenueSummary';
import { VenueLocationEditor } from './VenueLocationEditor';
import { venuePoint, type VenuePlace } from './venueTypes';
import { DifficultyBadge } from './ChartBadges';
import type { Chart, ChartList } from './types';
import { useToastMessage } from './Toast';

function VenueAlbum({
  venue,
  revision,
  onSelect,
}: {
  venue: VenuePlace;
  revision: number;
  onSelect: (chart: Chart) => void;
}) {
  const [charts, setCharts] = useState<Chart[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useToastMessage(error);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError('');
    setCharts([]);
    api<ChartList>(
      '/venues/charts?' +
        new URLSearchParams({ name: venue.name, offset: String(offset), limit: '48' }),
      { signal: controller.signal },
    )
      .then((data) => {
        if (controller.signal.aborted) return;
        setCharts(data.items);
        setTotal(data.total);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [venue.name, revision, offset, retry]);
  return (
    <div className="venue-album" aria-busy={busy}>
      {busy ? (
        <p className="empty-state" role="status">
          正在加载曲绘…
        </p>
      ) : error ? (
        <div className="venue-album-error">
          <button
            type="button"
            className="btn"
            aria-label="重试加载机厅曲目"
            onClick={() => setRetry((value) => value + 1)}
          >
            重试
          </button>
        </div>
      ) : (
        <>
          <div className="venue-album-grid">
            {charts.map((chart) => (
              <button
                type="button"
                className="venue-album-chart"
                key={chart.chart_key}
                onClick={() => onSelect(chart)}
                aria-label={`${chart.title} ${chart.difficulty_name}`}
              >
                <img src={chart.thumbnail_url || chart.jacket_url} alt="" loading="lazy" />
                <strong>{chart.title}</strong>
                <DifficultyBadge
                  difficulty={chart.difficulty}
                  name={chart.difficulty_name}
                  constant={chart.constant}
                  type={chart.type}
                />
              </button>
            ))}
          </div>
          {!charts.length && <p className="empty-state">还没有已确认的 AP</p>}
          {total > 48 && (
            <div className="venue-album-pagination">
              <button
                className="btn"
                disabled={!offset}
                onClick={() => setOffset((value) => Math.max(0, value - 48))}
              >
                上一页
              </button>
              <span>
                {offset + 1}–{Math.min(offset + 48, total)} / {total}
              </span>
              <button
                className="btn"
                disabled={offset + 48 >= total}
                onClick={() => setOffset((value) => value + 48)}
              >
                下一页
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function VenuesPage({
  revision,
  onChanged,
  onSelect,
  notify,
}: {
  revision: number;
  onChanged: () => Promise<void>;
  onSelect: (chart: Chart) => void;
  notify: (message: string) => void;
}) {
  const [view, setView] = useState<'map' | 'summary'>('map');
  const [venues, setVenues] = useState<VenuePlace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  useToastMessage(error);
  const [search, setSearch] = useState('');
  const [selection, setSelection] = useState<string | null>(() =>
    new URLSearchParams(location.search).get('venue'),
  );
  const [editing, setEditing] = useState<{ name: string; location: VenuePlace['location'] } | null>(
    null,
  );
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    api<{ items: VenuePlace[] }>('/venues/map', { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setVenues(data.items);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision, reload]);
  const rows = venues.filter((venue) =>
    venue.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  );
  const selected = venues.find((venue) => venue.name === selection);
  const [focus, setFocus] = useState<
    { latitude: number; longitude: number; zoom?: number } | undefined
  >();
  function select(name: string) {
    setSelection(name);
    const url = new URL(window.location.href);
    url.searchParams.set('venue', name);
    history.replaceState(null, '', url);
    const location = venuePoint(venues.find((venue) => venue.name === name)?.location || null);
    if (location) setFocus({ ...location, zoom: 15 });
  }
  return (
    <div className="venues-page">
      <div className="venues-page-toolbar">
        <div className="venue-view-switch" aria-label="机厅视图">
          <button className="btn" aria-pressed={view === 'map'} onClick={() => setView('map')}>
            地图
          </button>
          <button
            className="btn"
            aria-pressed={view === 'summary'}
            onClick={() => setView('summary')}
          >
            汇总表
          </button>
        </div>
        <button className="btn primary" onClick={() => setEditing({ name: '', location: null })}>
          新增机厅
        </button>
      </div>
      {view === 'summary' ? (
        <VenueSummary revision={revision} />
      ) : (
        <>
          <div className="venue-atlas-toolbar">
            <label>
              <span className="sr-only">搜索地图中的机厅</span>
              <input
                type="search"
                placeholder="搜索机厅或城市"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <span>
              {rows.length} 家机厅 · {rows.reduce((n, venue) => n + venue.total, 0)} AP
            </span>
          </div>
          {error && (
            <div>
              <button className="btn" onClick={() => setReload((value) => value + 1)}>
                重试
              </button>
            </div>
          )}
          {loading && !venues.length ? (
            <div className="empty-state" role="status">
              正在加载机厅…
            </div>
          ) : (
            <>
              <div className="venue-atlas-layout">
                <VenueMap
                  key={search.trim()}
                  venues={rows.map((venue) => ({
                    ...venue,
                    location: venuePoint(venue.location),
                    jackets: venue.jackets.map((jacket) => ({
                      ...jacket,
                      jacket_url: jacket.thumbnail_url || jacket.jacket_url,
                    })),
                  }))}
                  selectedName={selection || undefined}
                  onSelect={select}
                  focusPoint={focus}
                />
                <div className="venue-atlas-list" aria-label="机厅列表">
                  {rows.map((venue) => (
                    <button
                      type="button"
                      key={venue.name}
                      className="venue-atlas-card"
                      aria-pressed={venue.name === selection}
                      onClick={() => select(venue.name)}
                    >
                      <div className="venue-atlas-thumb">
                        {venue.jackets[0] ? (
                          <img
                            src={venue.jackets[0].thumbnail_url || venue.jackets[0].jacket_url}
                            alt=""
                            loading="lazy"
                          />
                        ) : (
                          <span>AP</span>
                        )}
                      </div>
                      <div>
                        <strong>{venue.name}</strong>
                        <span>
                          {venue.total} AP
                          {venue.pending_count ? ` · ${venue.pending_count} 待核` : ''}
                        </span>
                        {!venuePoint(venue.location) && <span>未设置位置</span>}
                      </div>
                    </button>
                  ))}
                  {!rows.length && <p className="empty-state">没有匹配的机厅</p>}
                </div>
              </div>
              {selected && (
                <section className="workspace venue-selected" aria-label="机厅详情">
                  <header className="venue-selected-heading">
                    <div>
                      <h2>{selected.name}</h2>
                      <div className="venue-selected-meta">
                        <span>{selected.total} AP</span>
                        {!!selected.pending_count && <span>{selected.pending_count} 待核</span>}
                      </div>
                    </div>
                    <button className="btn" onClick={() => setEditing(selected)}>
                      编辑位置
                    </button>
                  </header>
                  <VenueAlbum
                    key={`${selected.name}:${revision}`}
                    venue={selected}
                    revision={revision}
                    onSelect={onSelect}
                  />
                </section>
              )}
            </>
          )}
        </>
      )}
      {editing && (
        <VenueLocationEditor
          venue={editing}
          venues={venues}
          onClose={() => setEditing(null)}
          onSaved={(name) => {
            setEditing(null);
            select(name);
            setReload((value) => value + 1);
            notify('机厅位置已保存');
            void onChanged().catch((error) => notify(messageOf(error)));
          }}
        />
      )}
    </div>
  );
}
