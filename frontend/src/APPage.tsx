import { useEffect, useRef, useState } from 'react';
import { api, messageOf } from './api';
import type {
  Chart,
  ChartFilters,
  ChartList,
  CompletionTableData,
  Preferences,
  Summary,
} from './types';
import { CompletionTable } from './CompletionTable';
import { PreferencesStatus } from './Preferences';
import { ConstantRange } from './ConstantRange';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { useToastMessage } from './Toast';
import { chartName, difficultyNames } from './chartLabels';
import { Badge, Heading, Icon, JacketImage } from './ui';
import { useDebounce } from './useDebounce';

const num = (value = 0) => value.toLocaleString('en-US');
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

export function APPage({
  active,
  revision,
  catalogRevision,
  preferences,
  preferencesError,
  onRetryPreferences,
  onSelect,
  onRecord,
  notify,
}: {
  active: boolean;
  revision: number;
  catalogRevision: number;
  preferences: Preferences | null;
  preferencesError: string;
  onRetryPreferences: () => void;
  onSelect: (chart: Pick<Chart, 'chart_key'>) => void;
  onRecord: () => void;
  notify: (message: string) => void;
}) {
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
  const completionKeyRef = useRef('');
  const [listError, setListError] = useState('');
  const [constantRange, setConstantRange] = useState<[number, number] | null>(null);
  const rangeQuery = useDebounce(constantRange, 160);
  const [version, setVersion] = useState('');
  const [filterOptions, setFilterOptions] = useState<ChartFilters>({ constants: [], versions: [] });
  const [charts, setCharts] = useState<Chart[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [listBusy, setListBusy] = useState(true);
  const [retry, setRetry] = useState(0);
  useToastMessage(listError);
  // A saved default range and a catalog publication restart the list, as before the split.
  const [seenPreferences, setSeenPreferences] = useState(preferences);
  if (preferences !== seenPreferences) {
    setSeenPreferences(preferences);
    if (preferences) {
      setConstantRange([preferences.default_constant_min, preferences.default_constant_max]);
      if (seenPreferences) {
        setOffset(0);
        setCharts([]);
        setCompletionData(null);
        setSummary(null);
        setTotal(0);
        setListBusy(true);
      }
    }
  }
  const [seenCatalogRevision, setSeenCatalogRevision] = useState(catalogRevision);
  if (catalogRevision !== seenCatalogRevision) {
    setSeenCatalogRevision(catalogRevision);
    setOffset(0);
  }
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
    if (!active || !preferences || constantRange !== rangeQuery) return;
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
      // A focus refresh only bumps `revision`; keep the table so the page keeps its scroll.
      const completionKey = listParams.toString();
      if (completionKey !== completionKeyRef.current) setCompletionData(null);
      completionKeyRef.current = completionKey;
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
    active,
    query,
    status,
    offset,
    difficulty,
    constantRange,
    rangeQuery,
    version,
    sort,
    revision,
    retry,
    catalogView,
    preferences,
  ]);
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
      difficulty && difficultyNames[Number(difficulty)],
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
  if (!active) return null;
  return (
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
            <button className="btn primary" onClick={onRecord}>
              <Icon name="plus" />
              记录 AP
            </button>
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
                  {difficultyNames.slice(0, 5).map((label, index) => (
                    <option key={label} value={index}>
                      {label}
                    </option>
                  ))}
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
                    onClick={() => setRetry((value) => value + 1)}
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
                    onSelect={onSelect}
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
                          onClick={() => onSelect(c)}
                          tabIndex={0}
                          role="button"
                          aria-label={`查看 ${chartName(c)} ${c.difficulty_name}`}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              onSelect(c);
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
                              className={'venue ' + (!c.requires_venue || !c.venue ? 'empty' : '')}
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
                      {Math.floor(offset / limit) + 1} / {Math.max(1, Math.ceil(total / limit))}
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
        <PreferencesStatus error={preferencesError} onRetry={onRetryPreferences} />
      )}
    </>
  );
}
