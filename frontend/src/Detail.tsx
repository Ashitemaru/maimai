import { useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, ApiError, messageOf } from './api';
import type { Chart, ChartList, Venue } from './types';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { useToastMessage } from './Toast';
import { chartName } from './chartLabels';
import { Badge, Icon, JacketImage } from './ui';
import { useDebounce } from './useDebounce';

function Drawer({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const hadOverlay = document.body.classList.contains('has-overlay');
    document.body.classList.add('has-overlay');
    panel.current?.querySelector<HTMLElement>('input,button')?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key === 'Tab') {
        const elements = Array.from(
          panel.current?.querySelectorAll<HTMLElement>(
            'button:not(:disabled),input,select,textarea,a[href]',
          ) || [],
        );
        const first = elements[0],
          last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener('keydown', key);
    return () => {
      if (!hadOverlay) document.body.classList.remove('has-overlay');
      document.removeEventListener('keydown', key);
      previous?.focus();
    };
  }, [onClose]);
  return (
    <div
      className="scrim"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section ref={panel} className="drawer" role="dialog" aria-modal="true" aria-label={title}>
        <div className="drawer-top">
          {title}
          <button className="close" onClick={onClose} aria-label="关闭">
            <Icon name="close" />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

export function ChartPicker({
  mode,
  onClose,
  onPick,
  revision,
}: {
  mode: 'record' | 'cover';
  onClose: () => void;
  onPick: (chart: Chart) => void;
  revision: number;
}) {
  const [q, setQ] = useState('');
  const query = useDebounce(q);
  const [items, setItems] = useState<Chart[]>([]);
  const [error, setError] = useState('');
  useToastMessage(error);
  useEffect(() => {
    const control = new AbortController();
    api<ChartList>(
      '/charts?' +
        new URLSearchParams({ q: query, group: mode === 'cover' ? 'catalog' : 'all', limit: '40' }),
      { signal: control.signal },
    )
      .then((data) => {
        setItems(data.items);
        setError('');
      })
      .catch((error) => {
        if (!control.signal.aborted) setError(messageOf(error));
      });
    return () => control.abort();
  }, [query, mode, revision]);
  return (
    <Drawer title={mode === 'cover' ? '选择谱面' : '记录 AP'} onClose={onClose}>
      <h2 className="picker-heading">选择谱面</h2>
      <label className="search">
        <Icon name="search" />
        <input
          autoFocus
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="曲名、别名或 ID"
          aria-label="查找谱面"
        />
      </label>
      <div className="search-results">
        {items.map((c) => (
          <button key={c.chart_key} className="pick-song" onClick={() => onPick(c)}>
            <JacketImage src={c.thumbnail_url || c.jacket_url} loading="lazy" retryKey={revision} />
            <div>
              <strong>{chartName(c)}</strong>
              <div className="chart-meta">
                <ChartTypeBadge type={c.type} />
                <DifficultyBadge difficulty={c.difficulty} name={c.difficulty_name} type={c.type} />
                <span>{c.constant.toFixed(1)}</span>
              </div>
            </div>
          </button>
        ))}
        {!items.length && <div className="empty-state">没有找到谱面</div>}
      </div>
    </Drawer>
  );
}

export function Detail({
  chart: c,
  venues,
  onClose,
  onSaved,
  onReload,
  notify,
}: {
  chart: Chart;
  venues: Venue[];
  onClose: () => void;
  onSaved: () => Promise<void>;
  onReload: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [venue, setVenue] = useState(c.pending_record?.venue || '');
  const venueRecorded = c.is_complete && !!c.venue;
  const request = useRef<{ venue: string; id: string } | null>(null);
  const venueChoices = [...venues].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
  const achievement = c.provider_achievement;
  const combo = ({ ap: 'AP', app: 'AP+', fc: 'FC', fcp: 'FC+' } as Record<string, string>)[
    c.provider_fc || ''
  ];
  const syncStatus = (
    { sync: 'SYNC', fs: 'FS', fsp: 'FS+', fsd: 'FDX', fsdp: 'FDX+' } as Record<string, string>
  )[c.provider_fs || ''];
  const [busy, setBusy] = useState(false);
  const [conflicted, setConflicted] = useState(false);
  useToastMessage(c.conflict ? '成绩待核对 · 个人记录已保留' : '');
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  function showError(error: unknown) {
    if (!active.current) return;
    notify(messageOf(error));
    if (error instanceof ApiError && error.status === 409) setConflicted(true);
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || conflicted || venueRecorded) return;
    if (c.requires_venue && !venues.some((item) => item.name === venue)) {
      notify('请选择 AP 机厅');
      return;
    }
    const submittedVenue = c.requires_venue ? venue.trim() : '';
    if (request.current?.venue !== submittedVenue)
      request.current = { venue: submittedVenue, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api<{ chart: Chart }>('/charts/' + encodeURIComponent(c.chart_key) + '/ap-record', {
        method: 'PUT',
        body: JSON.stringify({
          expected_revision: c.record_revision ?? 0,
          request_id: request.current.id,
          venue: submittedVenue,
        }),
      });
    } catch (error) {
      showError(error);
      if (active.current) setBusy(false);
      return;
    }
    committed('已加入待核记录');
  }
  // The write already advanced the server revision, so this drawer's revision is stale:
  // close it before refreshing, and report a failed refresh without reopening a stale form.
  function committed(message: string) {
    if (active.current) onClose();
    notify(message);
    onSaved().catch((error) => notify('记录已保存，但列表刷新失败：' + messageOf(error)));
  }
  async function reset() {
    if (busy || conflicted) return;
    setBusy(true);
    try {
      await api(
        '/charts/' +
          encodeURIComponent(c.chart_key) +
          '/ap-record?' +
          new URLSearchParams({ expected_revision: String(c.record_revision ?? 0) }),
        { method: 'DELETE' },
      );
    } catch (error) {
      showError(error);
      if (active.current) setBusy(false);
      return;
    }
    committed('已取消待核记录');
  }
  async function reload() {
    setBusy(true);
    try {
      await onReload();
    } finally {
      if (active.current) setBusy(false);
    }
  }
  return (
    <Drawer title="谱面记录" onClose={onClose}>
      <div className="detail-song">
        <JacketImage src={c.thumbnail_url || c.jacket_url} />
        <div>
          <h2>{chartName(c)}</h2>
          <div className="detail-badges">
            <DifficultyBadge
              difficulty={c.difficulty}
              name={c.difficulty_name}
              constant={c.constant}
              type={c.type}
            />
            <ChartTypeBadge type={c.type} />
          </div>
          <p>{c.artist}</p>
        </div>
      </div>
      <div className="record-status">
        <div className="provider-results">
          <strong>{achievement != null ? `${achievement.toFixed(4)}%` : '—'}</strong>
          {combo && <span>{combo}</span>}
          {syncStatus && <span>{syncStatus}</span>}
        </div>
      </div>
      {!combo && (
        <div className="record-status">
          <Badge chart={c} />
        </div>
      )}
      {venueRecorded ? (
        <div className="detail-rule">
          <strong>{c.venue}</strong>
        </div>
      ) : (
        <>
          {c.pending_record && (
            <div className="detail-rule">
              <strong>{c.pending_record.venue || '机厅不记录'}</strong>
              <span>{c.pending_record.reason}</span>
            </div>
          )}
          <form onSubmit={save}>
            {c.requires_venue && (
              <div className="field">
                <label htmlFor="venue-input">AP 机厅</label>
                <select
                  id="venue-input"
                  required
                  value={venue}
                  disabled={busy || conflicted}
                  onChange={(event) => setVenue(event.target.value)}
                >
                  <option value="">选择机厅</option>
                  {venueChoices.map((item) => (
                    <option key={item.name} value={item.name}>
                      {item.name}
                    </option>
                  ))}
                </select>
                {!venueChoices.length && <small>请先在“我的机厅”新增机厅。</small>}
              </div>
            )}
            {conflicted && (
              <div className="form-actions">
                <button type="button" className="btn" disabled={busy} onClick={reload}>
                  重新载入记录
                </button>
              </div>
            )}
            <button
              type="submit"
              className="btn primary full"
              disabled={busy || conflicted || (c.requires_venue && !venue)}
            >
              <Icon name="check" />
              保存待核记录
            </button>
          </form>
          {c.pending_record && (
            <div className="drawer-actions">
              <button className="btn" disabled={busy || conflicted} onClick={reset}>
                取消登记
              </button>
            </div>
          )}
        </>
      )}
    </Drawer>
  );
}
