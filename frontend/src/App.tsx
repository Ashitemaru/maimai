import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, ApiError, messageOf } from './api';
import type {
  APRecords,
  Chart,
  ChartFilters,
  ChartList,
  CompletionTableData,
  Page,
  Preferences,
  Settings,
  Summary,
  Venue,
} from './types';
import { PendingRecords } from './PendingRecords';
import { CompletionTable } from './CompletionTable';
import { CompletionMatrix } from './CompletionMatrix';
import { VenuesPage } from './VenuesPage';
import { PreferencesPanel, PreferencesStatus } from './Preferences';
import { CoverPage } from './CoverWorkshop';
import type { CoverState } from './CoverWorkshop';
import { ConstantRange } from './ConstantRange';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { CatalogUpdatePanel } from './CatalogUpdate';
import { useCatalogUpdate } from './useCatalogUpdate';
import type { CatalogUpdateControl } from './useCatalogUpdate';
import { ScoreSyncPanel } from './ScoreSyncPanel';
import { useScoreSync, type ScoreSyncControl } from './useScoreSync';
import { useNotify, useToastMessage } from './Toast';

const paths: Record<string, string> = {
  clock: 'M12 8v4l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  table: 'M3 3h18v18H3zM3 9h18M9 3v18M15 9v12M3 15h18',
  image: 'M4 3h16v18H4zM4 15l5-5 5 5 3-3 3 3M8 7h.01',
  pin: 'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 0 1 16 0ZM9 10a3 3 0 1 0 6 0 3 3 0 1 0-6 0',
  settings: 'M4 7h16M4 17h16M8 4v6M16 14v6',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  sync: 'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M18 18A8 8 0 0 1 5 16',
  plus: 'M12 5v14M5 12h14',
  check: 'm5 12 4 4L19 6',
  close: 'm6 6 12 12M6 18 18 6',
  spark: 'm12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3Z',
};
const pageLabels: Record<Page, string> = {
  ap: 'AP 图鉴',
  statistics: '完成统计',
  pending: '待核记录',
  cover: '封面工坊',
  venues: '我的机厅',
  settings: '数据与同步',
};
const mobilePageLabels: Record<Page, string> = {
  ap: '图鉴',
  statistics: '统计',
  pending: '待核',
  cover: '封面',
  venues: '机厅',
  settings: '设置',
};
const num = (value = 0) => value.toLocaleString('en-US');
const chartName = (c: Chart) => c.display_title || c.title || '（空白曲名）';
function Icon({ name }: { name: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || paths.grid} />
    </svg>
  );
}
function Heading({
  title,
  eyebrow,
  actions,
}: {
  title: string;
  eyebrow: string;
  actions?: ReactNode;
}) {
  return (
    <div className="heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
      </div>
      <div className="heading-actions">{actions}</div>
    </div>
  );
}
function Badge({ chart }: { chart: Chart }) {
  return chart.is_ap ? (
    <span className="badge ap">
      <Icon name="check" />
      {chart.status === 'app' ? 'AP+' : 'AP'}
    </span>
  ) : chart.is_complete ? (
    <span className="badge complete">✦ 已完成</span>
  ) : (
    <span className="badge pending">{chart.pending_record ? '待核实' : '未标记'}</span>
  );
}
function JacketImage({
  src,
  className = '',
  loading,
  retryKey,
}: {
  src: string;
  className?: string;
  loading?: 'lazy' | 'eager';
  retryKey?: number;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src, retryKey]);
  return failed ? (
    <span className={'jacket-placeholder ' + className} aria-hidden="true" />
  ) : (
    <img className={className} src={src} alt="" loading={loading} onError={() => setFailed(true)} />
  );
}
function Stat({
  label,
  value,
  foot,
  featured,
  total = 0,
}: {
  label: string;
  value: number;
  foot?: string;
  featured?: boolean;
  total?: number;
}) {
  return (
    <div className={'stat' + (featured ? ' feature' : '')}>
      <div className="stat-label">
        {featured && <Icon name="spark" />}
        {label}
      </div>
      <div className="stat-value">
        {num(value)}
        <small>谱面</small>
      </div>
      {featured && (
        <progress
          className="progress-line"
          value={value}
          max={Math.max(total, 1)}
          aria-label={`${label} ${num(value)} / ${num(total)}`}
        />
      )}
      {foot && <div className="stat-foot">{foot}</div>}
    </div>
  );
}
function useDebounce<T>(value: T, delay = 220) {
  const [result, setResult] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setResult(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return result;
}

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

function ChartPicker({
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

function Detail({
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
  const achievement = c.provider_achievement ?? c.achievement;
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
      await onSaved();
      if (active.current) onClose();
      notify('已加入待核记录');
    } catch (error) {
      showError(error);
    } finally {
      if (active.current) setBusy(false);
    }
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
      await onSaved();
      if (active.current) onClose();
      notify('已取消待核记录');
    } catch (error) {
      showError(error);
    } finally {
      if (active.current) setBusy(false);
    }
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

function SettingsPage({
  settings,
  scoreSync,
  onSave,
  catalog,
  preferences,
  preferencesError,
  onRetryPreferences,
  onSavePreferences,
}: {
  settings: Settings | null;
  scoreSync: ScoreSyncControl;
  onSave: (token: string) => Promise<void>;
  catalog: CatalogUpdateControl;
  preferences: Preferences | null;
  preferencesError: string;
  onRetryPreferences: () => void;
  onSavePreferences: (value: Preferences) => Promise<void>;
}) {
  return (
    <>
      <Heading eyebrow="CONNECTED TO YOUR PLAY" title="数据与同步" />
      <ScoreSyncPanel control={scoreSync} settings={settings} onSaveToken={onSave} />
      <PreferencesPanel
        preferences={preferences}
        loadError={preferencesError}
        onRetry={onRetryPreferences}
        onSave={onSavePreferences}
      />
      <CatalogUpdatePanel control={catalog} />
    </>
  );
}

export default function App({ onLogout }: { onLogout?: () => void }) {
  const initialPage = location.hash.slice(1) as Page;
  const [page, setPage] = useState<Page>(initialPage in pageLabels ? initialPage : 'ap');
  const [q, setQ] = useState('');
  const query = useDebounce(q);
  const [difficulty, setDifficulty] = useState('');
  const [status, setStatus] = useState('all');
  const [offset, setOffset] = useState(0);
  const limit = 12;
  const [sort, setSort] = useState('constant_desc');
  const [catalogView, setCatalogView] = useState<'list' | 'completion'>(() =>
    new URLSearchParams(location.search).get('view') === 'completion' ? 'completion' : 'list',
  );
  const [completionData, setCompletionData] = useState<CompletionTableData | null>(null);
  const [listError, setListError] = useState('');
  const [constantRange, setConstantRange] = useState<[number, number] | null>(null);
  const rangeQuery = useDebounce(constantRange, 160);
  const [version, setVersion] = useState('');
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [preferencesError, setPreferencesError] = useState('');
  const [preferencesRetry, setPreferencesRetry] = useState(0);
  const [filterOptions, setFilterOptions] = useState<ChartFilters>({ constants: [], versions: [] });
  const [charts, setCharts] = useState<Chart[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [venues, setVenues] = useState<Venue[]>([]);
  const [records, setRecords] = useState<APRecords>({ items: [], count: 0 });
  const refreshSequence = useRef(0);
  const refreshFlight = useRef<Promise<void> | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [listBusy, setListBusy] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [detail, setDetail] = useState<Chart | null>(null);
  const detailRequest = useRef<{ controller: AbortController; sequence: number } | null>(null);
  const detailSequence = useRef(0);
  const [picker, setPicker] = useState<'record' | 'cover' | null>(null);
  const [cover, setCover] = useState<CoverState>({
    chart: null,
    score: '100.9615',
    version: '',
    aspect: '16:9',
  });
  const [catalogRevision, setCatalogRevision] = useState(0);
  const notify = useNotify();
  useToastMessage(error);
  useToastMessage(listError);
  const closeModal = useCallback(() => {
    detailRequest.current?.controller.abort();
    detailRequest.current = null;
    detailSequence.current += 1;
    setDetail(null);
    setPicker(null);
  }, []);
  useEffect(() => () => detailRequest.current?.controller.abort(), []);
  const catalogChanged = useCallback(() => {
    setRevision((value) => value + 1);
    setCatalogRevision((value) => value + 1);
    setOffset(0);
  }, []);
  const catalog = useCatalogUpdate(catalogChanged, notify);
  useEffect(() => {
    const change = () => {
      const p = location.hash.slice(1) as Page;
      if (p in pageLabels) {
        setPage(p);
        closeModal();
      }
    };
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, [closeModal]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (
        e.key === '/' &&
        !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName || '')
      ) {
        e.preventDefault();
        document.getElementById('chart-search')?.focus();
      }
    };
    document.addEventListener('keydown', key);
    return () => document.removeEventListener('keydown', key);
  }, []);
  const refresh = useCallback((invalidate = true) => {
    const sequence = ++refreshSequence.current;
    const request = Promise.all([
      api<{ items: Venue[] }>('/venues'),
      api<Settings>('/settings/divingfish'),
      api<APRecords>('/ap-records'),
    ]).then(([nextVenues, nextSettings, nextRecords]) => {
      if (sequence !== refreshSequence.current) return;
      setRecords(nextRecords);
      setVenues(nextVenues.items);
      setSettings(nextSettings);
      if (invalidate) setRevision((value) => value + 1);
      setError('');
    });
    refreshFlight.current = request;
    return request.finally(() => {
      if (refreshFlight.current === request) refreshFlight.current = null;
    });
  }, []);
  const scoreSync = useScoreSync(refresh, notify);
  useEffect(() => {
    void refresh(false).catch((error) => setError(messageOf(error)));
  }, [refresh]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let sequence = 0;
    const update = () => {
      const request = ++sequence;
      clearTimeout(timer);
      if (document.visibilityState !== 'visible') return;
      timer = setTimeout(async () => {
        // A request started before the app regained focus may contain old device data.
        await refreshFlight.current?.catch(() => undefined);
        if (request !== sequence || document.visibilityState !== 'visible') return;
        try {
          await refresh();
        } catch (error) {
          if (request === sequence) notify(messageOf(error));
        }
      }, 180);
    };
    window.addEventListener('focus', update);
    document.addEventListener('visibilitychange', update);
    return () => {
      sequence += 1;
      clearTimeout(timer);
      window.removeEventListener('focus', update);
      document.removeEventListener('visibilitychange', update);
    };
  }, [refresh, notify]);
  useEffect(() => {
    const controller = new AbortController();
    setPreferencesError('');
    api<Preferences>('/settings/preferences', { signal: controller.signal, cache: 'no-store' })
      .then((value) => {
        if (controller.signal.aborted) return;
        setPreferences(value);
        setConstantRange([value.default_constant_min, value.default_constant_max]);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setPreferencesError(messageOf(error));
      });
    return () => controller.abort();
  }, [preferencesRetry]);
  const coverChartKey = cover.chart?.chart_key;
  useEffect(() => {
    if (!catalogRevision || !coverChartKey) return;
    const controller = new AbortController();
    api<Chart>('/charts/' + encodeURIComponent(coverChartKey), {
      signal: controller.signal,
      cache: 'no-store',
    })
      .then((chart) =>
        setCover((value) =>
          value.chart?.chart_key === chart.chart_key ? { ...value, chart } : value,
        ),
      )
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404)
          setCover((value) =>
            value.chart?.chart_key === coverChartKey ? { ...value, chart: null } : value,
          );
        notify(messageOf(error));
      });
    return () => controller.abort();
  }, [catalogRevision, coverChartKey, notify]);
  useEffect(() => {
    const controller = new AbortController();
    api<ChartFilters>('/chart-filters', { signal: controller.signal })
      .then(setFilterOptions)
      .catch((error) => {
        if (!controller.signal.aborted) notify(messageOf(error));
      });
    return () => controller.abort();
  }, [catalogRevision, notify]);
  useEffect(() => {
    if (page !== 'ap' || !preferences || constantRange !== rangeQuery) return;
    const controller = new AbortController();
    setListBusy(true);
    setListError('');
    const params = new URLSearchParams({ q: query, status });
    if (difficulty) params.set('difficulty', difficulty);
    if (rangeQuery) {
      params.set('constant_min', rangeQuery[0].toFixed(1));
      params.set('constant_max', rangeQuery[1].toFixed(1));
    }
    if (version.startsWith('cn:')) params.set('cn_version', version.slice(3));
    else if (version) params.set('version', version);
    const listParams = new URLSearchParams(params);
    listParams.set('sort', sort);
    let request: Promise<void>;
    if (catalogView === 'completion') {
      setCompletionData(null);
      request = api<CompletionTableData>('/completion-table?' + listParams, {
        signal: controller.signal,
      }).then((data) => {
        if (controller.signal.aborted) return;
        setCompletionData(data);
        setTotal(data.total);
        setSummary(data.summary);
      });
    } else {
      listParams.set('offset', String(offset));
      listParams.set('limit', String(limit));
      request = Promise.all([
        api<ChartList>('/charts?' + listParams, { signal: controller.signal }),
        api<Summary>('/summary?' + params, { signal: controller.signal }),
      ]).then(([data, counts]) => {
        if (controller.signal.aborted) return;
        setCharts(data.items);
        setTotal(data.total);
        setSummary(counts);
      });
    }
    request
      .catch((error) => {
        if (!controller.signal.aborted) setListError(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setListBusy(false);
      });
    return () => controller.abort();
  }, [
    page,
    query,
    status,
    offset,
    difficulty,
    constantRange,
    rangeQuery,
    version,
    sort,
    revision,
    catalogView,
    preferences,
  ]);
  async function saveToken(token: string) {
    const result = await api<Settings>('/settings/divingfish', {
      method: 'PUT',
      body: JSON.stringify({ token }),
    });
    setSettings(result);
    notify(token ? '已保存上传 Token' : '已移除上传 Token');
  }
  async function savePreferences(value: Preferences) {
    const saved = await api<Preferences>('/settings/preferences', {
      method: 'PUT',
      body: JSON.stringify(value),
    });
    setPreferences(saved);
    setConstantRange([saved.default_constant_min, saved.default_constant_max]);
    setOffset(0);
    setCharts([]);
    setCompletionData(null);
    setSummary(null);
    setTotal(0);
    setListBusy(true);
  }
  async function openDetail(c: Pick<Chart, 'chart_key'>, preserveExisting = false) {
    detailRequest.current?.controller.abort();
    const controller = new AbortController();
    const sequence = ++detailSequence.current;
    detailRequest.current = { controller, sequence };
    const requestedPage = location.hash;
    if (!preserveExisting) setDetail(null);
    try {
      const fresh = await api<Chart>('/charts/' + encodeURIComponent(c.chart_key), {
        signal: controller.signal,
        cache: 'no-store',
      });
      if (
        controller.signal.aborted ||
        detailSequence.current !== sequence ||
        location.hash !== requestedPage
      )
        return;
      setPicker(null);
      setDetail(fresh);
    } catch (error) {
      if (!controller.signal.aborted && detailSequence.current === sequence)
        notify(messageOf(error));
    } finally {
      if (detailRequest.current?.sequence === sequence) detailRequest.current = null;
    }
  }
  function chooseCover(chart: Chart) {
    setCover((value) => ({ ...value, chart, score: '', version: '' }));
    closeModal();
    location.hash = 'cover';
    setPage('cover');
  }
  function openPicker(mode: 'record' | 'cover') {
    closeModal();
    setPicker(mode);
  }
  const filter = (setter: (value: string) => void, value: string) => {
    setter(value);
    setOffset(0);
  };
  const constantMin = filterOptions.constants[0] ?? 1;
  const constantMax = filterOptions.constants.at(-1) ?? 15;
  const cnVersions = filterOptions.cn_versions || [];
  const knownVersion = version.startsWith('cn:')
    ? cnVersions.some((option) => `cn:${option.value}` === version)
    : filterOptions.versions.some((option) => option.value === version);
  const activeFilters = Boolean(q || difficulty || constantRange || version || status !== 'all');
  const completionDescription =
    [
      q && `搜索：${q}`,
      difficulty && ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'][Number(difficulty)],
      version &&
        (version.startsWith('cn:')
          ? `国服 ${cnVersions.find((option) => `cn:${option.value}` === version)?.label || version.slice(3)}`
          : `日服 ${filterOptions.versions.find((option) => option.value === version)?.label || version}`),
      constantRange && `定数 ${constantRange[0].toFixed(1)}–${constantRange[1].toFixed(1)}`,
      status !== 'all' &&
        (
          { complete: '已完成', incomplete: '未标记', conflict: '待核对' } as Record<string, string>
        )[status],
    ]
      .filter(Boolean)
      .join(' · ') || '全部谱面';
  function changeView(value: 'list' | 'completion') {
    setCatalogView(value);
    setOffset(0);
    const url = new URL(location.href);
    if (value === 'completion') url.searchParams.set('view', 'completion');
    else url.searchParams.delete('view');
    history.replaceState(null, '', url);
  }
  function clearFilters() {
    setQ('');
    setDifficulty('');
    setConstantRange(null);
    setVersion('');
    setStatus('all');
    setOffset(0);
  }
  function changeRange(value: [number, number]) {
    setConstantRange((previous) =>
      value[0] === constantMin && value[1] === constantMax
        ? null
        : previous?.[0] === value[0] && previous?.[1] === value[1]
          ? previous
          : value,
    );
    setOffset(0);
  }
  const recordButton = (
    <button className="btn primary" onClick={() => openPicker('record')}>
      <Icon name="plus" />
      记录 AP
    </button>
  );
  return (
    <>
      <div className="shell">
        <aside className="sidebar">
          <a className="brand" href="#ap">
            <img src="/icon.svg" alt="" />
            <div>
              <div className="brand-name">
                mai<span> journal</span>
              </div>
              <small>PERSONAL PLAY RECORD</small>
            </div>
          </a>
          <div className="nav-label">MY COLLECTION</div>
          {(
            [
              ['ap', 'grid'],
              ['statistics', 'table'],
              ['pending', 'clock'],
              ['cover', 'image'],
              ['venues', 'pin'],
            ] as const
          ).map(([p, icon]) => (
            <a
              key={p}
              href={'#' + p}
              className={'nav-item ' + (page === p ? 'active' : '')}
              aria-label={pageLabels[p]}
            >
              <Icon name={icon} />
              <span className="nav-full-label" aria-hidden="true">
                {pageLabels[p]}
              </span>
              <span className="nav-mobile-label" aria-hidden="true">
                {mobilePageLabels[p]}
              </span>
              {p === 'pending' && records.count > 0 && (
                <small className="nav-count">{records.count}</small>
              )}
            </a>
          ))}
          <div className="nav-sep" />
          <div className="nav-label">PREFERENCES</div>
          <a
            href="#settings"
            className={'nav-item ' + (page === 'settings' ? 'active' : '')}
            aria-label={pageLabels.settings}
          >
            <Icon name="settings" />
            <span className="nav-full-label" aria-hidden="true">
              数据与同步
            </span>
            <span className="nav-mobile-label" aria-hidden="true">
              设置
            </span>
          </a>
          <div className="sidebar-bottom">
            <div className="profile">
              <div className="avatar">AS</div>
              <div>
                <strong>Ashitemaru</strong>
                <small>Every perfect counts.</small>
              </div>
            </div>
          </div>
        </aside>
        <main className="main">
          <div className="topbar">
            <div className="mobile-brand">
              <img src="/icon.svg" alt="" />
              mai journal
            </div>
            <div className="breadcrumb">
              我的空间 <span>/</span>
              <strong>{pageLabels[page]}</strong>
            </div>
            <div className="topbar-right">
              <span>maimai でらっくす</span>
              {onLogout && (
                <button className="btn quiet auth-logout" onClick={onLogout}>
                  退出
                </button>
              )}
              <span className="pill">
                <span className="dot" />
                国服曲库
              </span>
            </div>
          </div>
          {error ? (
            <div className="empty-state">
              <button
                className="btn"
                onClick={() => {
                  setError('');
                  void refresh().catch((error) => setError(messageOf(error)));
                }}
              >
                重试
              </button>
            </div>
          ) : (
            <>
              {page === 'ap' && (
                <>
                  <Heading
                    eyebrow="YOUR ALL PERFECT JOURNEY"
                    title="我的 AP 图鉴"
                    actions={
                      <>
                        <a className="btn sync-button" href="#settings">
                          <Icon name="sync" />
                          同步成绩
                        </a>
                        {recordButton}
                      </>
                    }
                  />
                  {preferences ? (
                    <>
                      <div
                        className="stats"
                        aria-busy={listBusy || constantRange !== rangeQuery || q !== query}
                      >
                        <Stat
                          label="已记录完成"
                          value={summary?.complete || 0}
                          featured
                          total={summary?.total || 0}
                          foot={
                            summary
                              ? `${(summary.total ? (summary.complete / summary.total) * 100 : 0).toFixed(1)}%`
                              : ''
                          }
                        />
                        <Stat label="尚未标记" value={summary?.remaining || 0} />
                        <Stat label="筛选谱面" value={summary?.total || 0} />
                        <Stat label="待核对" value={summary?.conflicts || 0} />
                      </div>
                      <section className="workspace">
                        <div className="filter-area">
                          <div className="search-row">
                            <label className="search">
                              <Icon name="search" />
                              <input
                                id="chart-search"
                                value={q}
                                onChange={(e) => filter(setQ, e.target.value)}
                                placeholder="搜索曲名、别名或 ID"
                                aria-label="搜索谱面"
                              />
                              <kbd>/</kbd>
                            </label>
                          </div>
                          <div className="catalog-filters">
                            <select
                              aria-label="谱面难度"
                              className="filter-select"
                              value={difficulty}
                              onChange={(e) => filter(setDifficulty, e.target.value)}
                            >
                              <option value="">全部难度</option>
                              {['BASIC', 'ADVANCED', 'EXPERT', 'MASTER', 'Re:MASTER'].map(
                                (label, index) => (
                                  <option key={label} value={index}>
                                    {label}
                                  </option>
                                ),
                              )}
                            </select>
                            <select
                              aria-label="登场版本"
                              className="filter-select"
                              value={version}
                              onChange={(e) => filter(setVersion, e.target.value)}
                            >
                              <option value="">全部版本</option>
                              {version && !knownVersion && (
                                <option value={version}>
                                  {version.startsWith('cn:')
                                    ? '所选国服版本'
                                    : version === '__unknown__'
                                      ? '版本待核实'
                                      : version}
                                </option>
                              )}
                              <optgroup label="日服">
                                {filterOptions.versions.map((option) => (
                                  <option key={option.value} value={option.value}>
                                    {option.label}
                                  </option>
                                ))}
                              </optgroup>
                              {cnVersions.length > 0 && (
                                <optgroup label="国服">
                                  {cnVersions.map((option) => (
                                    <option key={option.value} value={`cn:${option.value}`}>
                                      {option.label}
                                    </option>
                                  ))}
                                </optgroup>
                              )}
                            </select>
                            <select
                              id="chart-sort"
                              className="filter-select"
                              aria-label="谱面排序"
                              value={sort}
                              onChange={(event) => filter(setSort, event.target.value)}
                            >
                              <option value="constant_desc">定数降序</option>
                              <option value="constant_asc">定数升序</option>
                              <option value="achievement_desc">达成率降序</option>
                              <option value="achievement_asc">达成率升序</option>
                              <option value="ap_desc">AP 优先</option>
                              <option value="ap_asc">非 AP 优先</option>
                            </select>
                            <button
                              type="button"
                              className="btn clear-filters"
                              disabled={!activeFilters}
                              onClick={clearFilters}
                            >
                              清除筛选
                            </button>
                          </div>
                          <ConstantRange
                            min={Math.min(constantMin, constantRange?.[0] ?? constantMin)}
                            max={Math.max(constantMax, constantRange?.[1] ?? constantMax)}
                            value={constantRange ?? [constantMin, constantMax]}
                            onChange={changeRange}
                          />
                          <div className="status-row">
                            {[
                              ['all', '全部谱面'],
                              ['complete', '已完成'],
                              ['incomplete', '未标记'],
                              ['conflict', '待核对'],
                            ].map(([value, label]) => (
                              <button
                                key={value}
                                className={'status-tab ' + (status === value ? 'selected' : '')}
                                onClick={() => filter(setStatus, value)}
                              >
                                {label}
                              </button>
                            ))}
                            <div className="catalog-view-switch" role="group" aria-label="图鉴视图">
                              <button
                                type="button"
                                className="btn"
                                aria-pressed={catalogView === 'list'}
                                onClick={() => changeView('list')}
                              >
                                列表
                              </button>
                              <button
                                type="button"
                                className="btn"
                                aria-pressed={catalogView === 'completion'}
                                onClick={() => changeView('completion')}
                              >
                                完成表
                              </button>
                            </div>
                          </div>
                        </div>
                        {listError ? (
                          <div className="empty-state">
                            <div>
                              <button
                                type="button"
                                className="btn"
                                onClick={() => setRevision((value) => value + 1)}
                              >
                                重试
                              </button>
                            </div>
                          </div>
                        ) : catalogView === 'completion' ? (
                          <div aria-busy={listBusy || constantRange !== rangeQuery || q !== query}>
                            {completionData && constantRange === rangeQuery && q === query ? (
                              <CompletionTable
                                data={completionData}
                                sort={sort}
                                description={completionDescription}
                                onSelect={(chart) => void openDetail(chart)}
                                retryKey={catalogRevision}
                                notify={notify}
                              />
                            ) : (
                              <div className="empty-state" role="status">
                                加载完成表…
                              </div>
                            )}
                          </div>
                        ) : (
                          <>
                            <div className="table-wrap" aria-busy={listBusy}>
                              <table className="chart-table">
                                <thead>
                                  <tr>
                                    <th>曲目</th>
                                    <th>难度</th>
                                    <th>定数</th>
                                    <th>达成率</th>
                                    <th>状态</th>
                                    <th>AP 机厅</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {charts.map((c) => (
                                    <tr
                                      key={c.chart_key}
                                      onClick={() => void openDetail(c)}
                                      tabIndex={0}
                                      role="button"
                                      aria-label={`查看 ${chartName(c)} ${c.difficulty_name}`}
                                      onKeyDown={(e) => {
                                        if (e.key === 'Enter' || e.key === ' ') {
                                          e.preventDefault();
                                          void openDetail(c);
                                        }
                                      }}
                                    >
                                      <td>
                                        <div className="song-cell">
                                          <JacketImage
                                            className="jacket"
                                            src={c.thumbnail_url || c.jacket_url}
                                            loading="lazy"
                                            retryKey={catalogRevision}
                                          />
                                          <div className="song-info">
                                            <div className="song-title">{chartName(c)}</div>
                                            <div className="song-sub">
                                              <ChartTypeBadge type={c.type} />
                                              <span className="artist">{c.artist}</span>
                                              <span className="difficulty-inline">
                                                <DifficultyBadge
                                                  difficulty={c.difficulty}
                                                  name={c.difficulty_name}
                                                  constant={c.constant}
                                                  type={c.type}
                                                />
                                              </span>
                                            </div>
                                          </div>
                                        </div>
                                      </td>
                                      <td>
                                        <DifficultyBadge
                                          difficulty={c.difficulty}
                                          name={c.difficulty_name}
                                          type={c.type}
                                        />
                                      </td>
                                      <td>
                                        <span className="constant">
                                          {c.constant.toFixed(1)}
                                          <small>{c.level}</small>
                                        </span>
                                      </td>
                                      <td>
                                        <span className="achievement">
                                          {c.provider_achievement != null
                                            ? `${c.provider_achievement.toFixed(4)}%`
                                            : '—'}
                                        </span>
                                      </td>
                                      <td>
                                        <Badge chart={c} />
                                        {c.conflict && <span className="venue-source">待核对</span>}
                                        {c.is_ap && c.pending_record && (
                                          <span className="venue-source">机厅待核实</span>
                                        )}
                                      </td>
                                      <td>
                                        <span
                                          className={
                                            'venue ' +
                                            (!c.requires_venue || !c.venue ? 'empty' : '')
                                          }
                                        >
                                          {c.requires_venue
                                            ? c.venue || (c.venue_pending ? '机厅待补' : '—')
                                            : c.is_complete
                                              ? '不记录'
                                              : '—'}
                                        </span>
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                              {!listBusy && !charts.length && (
                                <div className="empty-state">没有符合条件的谱面</div>
                              )}
                            </div>
                            <div className="pager">
                              <span>
                                {num(total)} 张 · {total ? offset + 1 : 0}–
                                {Math.min(total, offset + charts.length)}
                              </span>
                              <div className="pager-buttons">
                                <button
                                  disabled={offset === 0 || listBusy}
                                  onClick={() => setOffset((value) => Math.max(0, value - limit))}
                                  aria-label="上一页"
                                >
                                  ‹
                                </button>
                                <span>
                                  {Math.floor(offset / limit) + 1} /{' '}
                                  {Math.max(1, Math.ceil(total / limit))}
                                </span>
                                <button
                                  disabled={offset + limit >= total || listBusy}
                                  onClick={() => setOffset((value) => value + limit)}
                                  aria-label="下一页"
                                >
                                  ›
                                </button>
                              </div>
                            </div>
                          </>
                        )}
                      </section>
                    </>
                  ) : (
                    <PreferencesStatus
                      error={preferencesError}
                      onRetry={() => setPreferencesRetry((value) => value + 1)}
                    />
                  )}
                </>
              )}
              {page === 'statistics' && (
                <>
                  <Heading
                    eyebrow="YOUR COMPLETION AT A GLANCE"
                    title="完成统计"
                    actions={
                      <>
                        <a className="btn sync-button" href="#settings">
                          <Icon name="sync" />
                          同步成绩
                        </a>
                      </>
                    }
                  />
                  {preferences ? (
                    <CompletionMatrix
                      revision={revision}
                      defaultRange={[
                        preferences.default_constant_min,
                        preferences.default_constant_max,
                      ]}
                      onSelect={(chart) => void openDetail(chart)}
                    />
                  ) : (
                    <PreferencesStatus
                      error={preferencesError}
                      onRetry={() => setPreferencesRetry((value) => value + 1)}
                    />
                  )}
                </>
              )}
              {page === 'pending' && (
                <>
                  <Heading
                    eyebrow="WAITING FOR YOUR NEXT SYNC"
                    title="待核记录"
                    actions={
                      <a className="btn primary" href="#settings">
                        <Icon name="sync" />
                        同步成绩
                      </a>
                    }
                  />
                  <PendingRecords
                    records={records}
                    onSelect={(record) => void openDetail(record)}
                    onChanged={refresh}
                    notify={notify}
                  />
                </>
              )}
              {page === 'cover' && (
                <CoverPage
                  value={cover}
                  onChange={setCover}
                  onPick={() => openPicker('cover')}
                  notify={notify}
                />
              )}
              {page === 'venues' && (
                <>
                  <Heading eyebrow="PLACES & PERFECTS" title="我的机厅" />
                  <VenuesPage
                    revision={revision}
                    onChanged={refresh}
                    onSelect={(chart) => void openDetail(chart)}
                    notify={notify}
                  />
                </>
              )}
              {page === 'settings' && (
                <SettingsPage
                  settings={settings}
                  scoreSync={scoreSync}
                  onSave={saveToken}
                  catalog={catalog}
                  preferences={preferences}
                  preferencesError={preferencesError}
                  onRetryPreferences={() => setPreferencesRetry((value) => value + 1)}
                  onSavePreferences={savePreferences}
                />
              )}
            </>
          )}
        </main>
      </div>
      {detail && (
        <Detail
          key={`${detail.chart_key}:${detail.record_revision ?? 0}`}
          chart={detail}
          venues={venues}
          onClose={closeModal}
          onSaved={refresh}
          onReload={() => openDetail(detail, true)}
          notify={notify}
        />
      )}{' '}
      {picker && (
        <ChartPicker
          mode={picker}
          revision={catalogRevision}
          onClose={closeModal}
          onPick={(chart) => (picker === 'cover' ? chooseCover(chart) : void openDetail(chart))}
        />
      )}
    </>
  );
}
