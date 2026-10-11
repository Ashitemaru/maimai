import { useEffect, useRef, useState } from 'react';
import { api, messageOf } from './api';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { ConstantRange } from './ConstantRange';
import type { Chart, ChartFilters, ChartList } from './types';

type Counts = { complete: number; total: number; remaining: number };
type MatrixVersion = { value: string; label: string; title: string };
type MatrixData = {
  versions: MatrixVersion[];
  rows: { constant: number; cells: Record<string, Counts>; total: Counts }[];
  totals: Record<string, Counts>;
  summary: Counts;
};
type RequestState<T> = { key: string; data?: T; error?: string };
type Display = 'complete' | 'remaining';
type Selection = {
  constant: number;
  version: MatrixVersion;
  counts: Counts;
  query: string;
  status: 'all' | 'incomplete';
};
const emptyCounts: Counts = { complete: 0, total: 0, remaining: 0 };
const difficultyNames = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'];
const pageSize = 20;
const countText = (counts: Counts, display: Display) =>
  !counts.total
    ? '—'
    : display === 'remaining'
      ? String(counts.remaining)
      : `${counts.complete} / ${counts.total}`;
const countLabel = (counts: Counts) =>
  `已完成 ${counts.complete}，共 ${counts.total} 张，剩余 ${counts.remaining} 张`;
const isComplete = (counts: Counts) => counts.complete === counts.total;

function RequestError({ message, retry }: { message: string; retry: () => void }) {
  return (
    <div className="matrix-error error-box" role="alert">
      <span>{message}</span>
      <button type="button" className="btn" onClick={retry}>
        重试
      </button>
    </div>
  );
}

function MatrixJacket({ src }: { src: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  return failed || !src ? (
    <span className="jacket jacket-placeholder" aria-hidden="true" />
  ) : (
    <img className="jacket" src={src} alt="" loading="lazy" onError={() => setFailed(true)} />
  );
}

function MatrixDetail({
  selection,
  revision,
  onClose,
  onSelect,
}: {
  selection: Selection;
  revision: number;
  onClose: () => void;
  onSelect: (chart: Chart) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [status, setStatus] = useState(selection.status);
  const [offset, setOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  const [result, setResult] = useState<RequestState<ChartList> | null>(null);
  const params = new URLSearchParams(selection.query);
  params.set('constant_min', selection.constant.toFixed(1));
  params.set('constant_max', selection.constant.toFixed(1));
  params.set('cn_version', selection.version.value);
  params.set('status', status);
  params.set('limit', String(pageSize));
  params.set('offset', String(offset));
  params.set('sort', 'constant_desc');
  const query = params.toString();
  const requestKey = `${query}|${revision}|${retry}`;
  const current = result?.key === requestKey ? result : null;
  const data = current?.data;

  useEffect(() => {
    const element = dialog.current;
    const hadOverlay = document.body.classList.contains('has-overlay');
    document.body.classList.add('has-overlay');
    element?.showModal();
    return () => {
      element?.close();
      if (!hadOverlay) document.body.classList.remove('has-overlay');
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    api<ChartList>(`/charts?${query}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, data });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, error: messageOf(error) });
      });
    return () => controller.abort();
  }, [query, requestKey]);

  function choose(chart: Chart) {
    dialog.current?.close();
    onClose();
    onSelect(chart);
  }

  return (
    <dialog
      ref={dialog}
      className="matrix-dialog"
      aria-labelledby="matrix-detail-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="matrix-dialog-content">
        <header className="matrix-detail-heading">
          <div>
            <h2 id="matrix-detail-title">
              {selection.constant.toFixed(1)} · {selection.version.label}
            </h2>
            <span>{countLabel(selection.counts)}</span>
          </div>
          <button type="button" className="close" aria-label="关闭谱面明细" onClick={onClose}>
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              aria-hidden="true"
            >
              <path d="m6 6 12 12M6 18 18 6" />
            </svg>
          </button>
        </header>
        <div className="matrix-detail-tabs">
          <button
            type="button"
            className={'status-tab' + (status === 'all' ? ' selected' : '')}
            aria-pressed={status === 'all'}
            onClick={() => {
              setStatus('all');
              setOffset(0);
            }}
          >
            全部 {selection.counts.total}
          </button>
          <button
            type="button"
            className={'status-tab' + (status === 'incomplete' ? ' selected' : '')}
            aria-pressed={status === 'incomplete'}
            onClick={() => {
              setStatus('incomplete');
              setOffset(0);
            }}
          >
            未完成 {selection.counts.remaining}
          </button>
        </div>
        <div className="matrix-detail-list" aria-busy={!current}>
          {!current && (
            <div className="empty-state" role="status">
              加载谱面…
            </div>
          )}
          {current?.error && (
            <RequestError message={current.error} retry={() => setRetry((value) => value + 1)} />
          )}
          {data && !data.items.length && (
            <div className="empty-state">
              {status === 'incomplete' ? '这个分组已全部完成' : '没有符合条件的谱面'}
            </div>
          )}
          {data?.items.map((chart) => (
            <button
              type="button"
              className="matrix-chart"
              key={chart.chart_key}
              onClick={() => choose(chart)}
              aria-label={`查看 ${chart.display_title || chart.title}，${chart.difficulty_name}，${chart.is_complete ? '已完成' : '未标记'}`}
            >
              <MatrixJacket src={chart.jacket_url} />
              <span className="matrix-chart-info">
                <strong>{chart.display_title || chart.title || '（空白曲名）'}</strong>
                <span className="chart-meta">
                  <ChartTypeBadge type={chart.type} />
                  <DifficultyBadge
                    difficulty={chart.difficulty}
                    name={chart.difficulty_name}
                    type={chart.type}
                  />
                  <span>ID {chart.id}</span>
                </span>
              </span>
              <span className={'badge ' + (chart.is_complete ? 'complete' : 'pending')}>
                {chart.is_complete ? '已完成' : '未标记'}
              </span>
            </button>
          ))}
        </div>
        {data && data.total > 0 && (
          <footer className="pager matrix-detail-pager">
            <span>
              {offset + 1}–{Math.min(offset + data.items.length, data.total)} / {data.total} 张
            </span>
            <div className="pager-buttons">
              <button
                type="button"
                disabled={offset === 0}
                aria-label="上一页谱面"
                onClick={() => setOffset((value) => Math.max(0, value - pageSize))}
              >
                ‹
              </button>
              <span>
                {Math.floor(offset / pageSize) + 1} / {Math.ceil(data.total / pageSize)}
              </span>
              <button
                type="button"
                disabled={offset + pageSize >= data.total}
                aria-label="下一页谱面"
                onClick={() => setOffset((value) => value + pageSize)}
              >
                ›
              </button>
            </div>
          </footer>
        )}
      </div>
    </dialog>
  );
}

export function CompletionMatrix({
  revision,
  defaultRange,
  onSelect,
}: {
  revision: number;
  defaultRange: [number, number];
  onSelect: (chart: Chart) => void;
}) {
  const [difficulty, setDifficulty] = useState('');
  const [range, setRange] = useState<[number, number] | null>(() => [...defaultRange]);
  const [versionMin, setVersionMin] = useState('');
  const [versionMax, setVersionMax] = useState('');
  const [display, setDisplay] = useState<Display>('complete');
  const [filters, setFilters] = useState<ChartFilters | null>(null);
  const [filtersError, setFiltersError] = useState('');
  const [filterRetry, setFilterRetry] = useState(0);
  const [retry, setRetry] = useState(0);
  const [result, setResult] = useState<RequestState<MatrixData> | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const versions =
    filters?.cn_matrix_versions?.filter((version) => version.value !== '__unknown__') ?? [];
  const constants = filters?.constants ?? [];
  const constantMin = constants.length ? Math.min(...constants) : 0;
  const constantMax = constants.length ? Math.max(...constants) : 0;
  const params = new URLSearchParams();
  if (difficulty !== '') params.set('difficulty', difficulty);
  if (range) {
    params.set('constant_min', range[0].toFixed(1));
    params.set('constant_max', range[1].toFixed(1));
  }
  if (versionMin) params.set('cn_version_min', versionMin);
  if (versionMax) params.set('cn_version_max', versionMax);
  const query = params.toString();
  const requestKey = `${query}|${revision}|${retry}`;
  const current = result?.key === requestKey ? result : null;
  const data = current?.data;

  useEffect(() => {
    const controller = new AbortController();
    setFiltersError('');
    api<ChartFilters>('/chart-filters', { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setFilters(data);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setFiltersError(messageOf(error));
      });
    return () => controller.abort();
  }, [revision, filterRetry]);

  useEffect(() => {
    const controller = new AbortController();
    api<MatrixData>(`/completion-matrix?${query}`, { signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, data });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, error: messageOf(error) });
      });
    return () => controller.abort();
  }, [query, requestKey]);

  function changeVersion(bound: 'min' | 'max', value: string) {
    if (bound === 'min') {
      setVersionMin(value);
      if (value && versionMax && Number(value) > Number(versionMax)) setVersionMax(value);
    } else {
      setVersionMax(value);
      if (value && versionMin && Number(value) < Number(versionMin)) setVersionMin(value);
    }
  }

  function resetFilters() {
    setDifficulty('');
    setRange(null);
    setVersionMin('');
    setVersionMax('');
    setDisplay('complete');
  }

  function renderCount(counts: Counts, label: string) {
    return (
      <span
        className="matrix-count"
        data-complete={isComplete(counts)}
        data-empty={!counts.total}
        aria-label={`${label}，${countLabel(counts)}`}
      >
        {countText(counts, display)}
      </span>
    );
  }

  return (
    <section className="workspace matrix-workspace" aria-label="定数与国服版本完成统计">
      <div className="matrix-filters">
        <div className="matrix-filter-row">
          <label className="matrix-field">
            <span>难度</span>
            <select value={difficulty} onChange={(event) => setDifficulty(event.target.value)}>
              <option value="">全部难度</option>
              {difficultyNames.map((name, index) => (
                <option value={index} key={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="matrix-field">
            <span>起始版本</span>
            <select
              value={versionMin}
              onChange={(event) => changeVersion('min', event.target.value)}
              disabled={!filters}
            >
              <option value="">不限</option>
              {versions.map((version) => (
                <option value={version.value} key={version.value}>
                  {version.label}
                </option>
              ))}
            </select>
          </label>
          <label className="matrix-field">
            <span>截止版本</span>
            <select
              value={versionMax}
              onChange={(event) => changeVersion('max', event.target.value)}
              disabled={!filters}
            >
              <option value="">不限</option>
              {versions.map((version) => (
                <option value={version.value} key={version.value}>
                  {version.label}
                </option>
              ))}
            </select>
          </label>
          <label className="matrix-field">
            <span>显示</span>
            <select value={display} onChange={(event) => setDisplay(event.target.value as Display)}>
              <option value="complete">完成 / 总数</option>
              <option value="remaining">剩余数量</option>
            </select>
          </label>
          <button type="button" className="btn quiet matrix-clear" onClick={resetFilters}>
            清除
          </button>
        </div>
        {constants.length > 0 && (
          <ConstantRange
            min={Math.min(constantMin, range?.[0] ?? constantMin)}
            max={Math.max(constantMax, range?.[1] ?? constantMax)}
            value={range ?? [constantMin, constantMax]}
            onChange={setRange}
          />
        )}
        {filtersError && (
          <RequestError
            message={`筛选项加载失败：${filtersError}`}
            retry={() => setFilterRetry((value) => value + 1)}
          />
        )}
      </div>
      <div aria-busy={!current}>
        {!current && (
          <div className="empty-state" role="status">
            加载完成统计…
          </div>
        )}
        {current?.error && (
          <RequestError message={current.error} retry={() => setRetry((value) => value + 1)} />
        )}
        {data && !data.summary.total && <div className="empty-state">没有符合条件的谱面</div>}
        {data && data.summary.total > 0 && (
          <div
            className="matrix-scroll"
            tabIndex={0}
            role="region"
            aria-label="完成统计表，可横向和纵向滚动"
          >
            <table
              className="matrix-table"
              aria-label={
                display === 'complete'
                  ? '各定数与国服版本的完成数及总数'
                  : '各定数与国服版本的剩余数量'
              }
            >
              <thead>
                <tr>
                  <th scope="col">定数</th>
                  {data.versions.map((version) => (
                    <th
                      scope="col"
                      key={version.value}
                      title={version.title || version.label}
                      data-complete={isComplete(data.totals[version.value] ?? emptyCounts)}
                    >
                      <span>{version.label}</span>
                    </th>
                  ))}
                  <th scope="col" data-complete={isComplete(data.summary)}>
                    合计
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <tr key={row.constant}>
                    <th scope="row" data-complete={isComplete(row.total)}>
                      {row.constant.toFixed(1)}
                    </th>
                    {data.versions.map((version) => {
                      const counts = row.cells[version.value] ?? emptyCounts;
                      return (
                        <td key={version.value} data-complete={isComplete(counts)}>
                          {counts.total ? (
                            <button
                              type="button"
                              className="matrix-cell"
                              data-complete={isComplete(counts)}
                              aria-label={`定数 ${row.constant.toFixed(1)}，${version.title || version.label}，${countLabel(counts)}`}
                              onClick={() =>
                                setSelection({
                                  constant: row.constant,
                                  version,
                                  counts,
                                  query,
                                  status: display === 'remaining' ? 'incomplete' : 'all',
                                })
                              }
                            >
                              {countText(counts, display)}
                            </button>
                          ) : (
                            <span
                              className="matrix-count"
                              data-empty="true"
                              data-complete="true"
                              aria-label="没有谱面"
                            >
                              —
                            </span>
                          )}
                        </td>
                      );
                    })}
                    <td data-complete={isComplete(row.total)}>
                      {renderCount(row.total, `定数 ${row.constant.toFixed(1)} 合计`)}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <th scope="row" data-complete={isComplete(data.summary)}>
                    合计
                  </th>
                  {data.versions.map((version) => (
                    <td
                      key={version.value}
                      data-complete={isComplete(data.totals[version.value] ?? emptyCounts)}
                    >
                      {renderCount(
                        data.totals[version.value] ?? emptyCounts,
                        `${version.title || version.label} 合计`,
                      )}
                    </td>
                  ))}
                  <td data-complete={isComplete(data.summary)}>
                    {renderCount(data.summary, '总计')}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </div>
      {selection && (
        <MatrixDetail
          selection={selection}
          revision={revision}
          onClose={() => setSelection(null)}
          onSelect={onSelect}
        />
      )}
    </section>
  );
}
