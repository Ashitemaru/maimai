import { useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError, messageOf } from './api';
import type { CatalogUpdateControl, CatalogUpdateState } from './useCatalogUpdate';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { formatDate } from './dateTime';

type Value = unknown;
interface SourceOption {
  option_id: string;
  source: string;
  value: Value;
  date?: string | null;
  external_id?: string;
  sources?: string[];
}
interface Field {
  field_key: string;
  label: string;
  required: boolean;
  options: SourceOption[];
}
interface ChartRecord {
  id: number;
  difficulty: number;
  chart_key: string;
  title: string;
  type: string | null;
  difficulty_name: string | null;
  status: string;
  decision_pending?: boolean;
  fields: Field[];
}
interface ReviewData {
  revision: string;
  decision_revision: number;
  decisions_pending?: boolean;
  initialized?: boolean;
  records: ChartRecord[];
  counts?: { admitted: number; pending: number };
  admitted_count?: number;
  pending_count?: number;
}
interface SavedField {
  field_key: string;
  label?: string;
  value: Value;
  source: string;
  date?: string | null;
  updated_at?: string;
}
interface SavedRecord extends Omit<ChartRecord, 'fields'> {
  fields: SavedField[];
}
interface DecisionsData {
  revision: string;
  decision_revision: number;
  decisions_pending?: boolean;
  records: SavedRecord[];
}
interface Choice {
  field_key: string;
  option_id: string;
}
type Draft = Record<string, Choice>;
type Operation = { chartKeys: string[]; phase: 'submitting' | 'applying' };
interface PreviewField {
  field_key: string;
  label: string;
  option: SourceOption;
}
interface PreviewRecord extends Omit<ChartRecord, 'fields'> {
  fields: PreviewField[];
}
interface BatchPreview {
  revision: string;
  decision_revision: number;
  records: PreviewRecord[];
}
function reconcileDrafts(drafts: Record<string, Draft>, records: ChartRecord[]) {
  const next: Record<string, Draft> = {};
  for (const record of records) {
    if (record.decision_pending) continue;
    const draft = drafts[record.chart_key];
    if (!draft) continue;
    const choices = Object.fromEntries(
      record.fields.flatMap((field) => {
        const choice = draft[field.field_key];
        return choice && field.options.some((option) => option.option_id === choice.option_id)
          ? [[field.field_key, choice]]
          : [];
      }),
    );
    if (Object.keys(choices).length) next[record.chart_key] = choices;
  }
  return next;
}
const normalized = (value: string) => value.normalize('NFKC').toLocaleLowerCase();
const sourceKey = (source: string) =>
  ({
    divingfish: 'diving_fish',
    waterfish: 'diving_fish',
    arcade: 'arcade_songs',
    remy: 'remywiki',
  })[source] || source;
const sourceName = (source: string) =>
  ({
    lxns: '落雪',
    diving_fish: '水鱼',
    arcade_songs: '日服曲库',
    remywiki: 'Remy',
    manual: '人工记录',
  })[sourceKey(source)] || source;
const difficultyName = (record: Pick<ChartRecord, 'difficulty' | 'difficulty_name'>) =>
  record.difficulty_name ||
  ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master'][record.difficulty] ||
  '宴';
const fieldNames: Record<string, string> = {
  title: '曲名',
  artist: '作者',
  bpm: 'BPM',
  genre: '分类',
  level: '等级',
  constant: '定数',
  note_designer: '谱师',
  notes: '物量',
  jp_release: '日服版本 / 日期',
  jp_release_version: '日服版本 / 日期',
  version: '日服版本 / 日期',
  release: '日服版本 / 日期',
};
function ValueDisplay({ value }: { value: Value }) {
  if (value === null || value === undefined || value === '')
    return <span className="record-empty-value">未提供</span>;
  if (typeof value === 'boolean') return <>{value ? '是' : '否'}</>;
  if (typeof value !== 'object') return <>{String(value)}</>;
  if (Array.isArray(value))
    return (
      <>
        {value.map((item, index) => (
          <span className="record-value-part" key={index}>
            <ValueDisplay value={item} />
          </span>
        ))}
      </>
    );
  const object = value as Record<string, Value>;
  if ('version' in object || 'jp_release_version' in object) {
    const version = object.version ?? object.jp_release_version,
      date = object.date ?? object.jp_release_date;
    return (
      <>
        <span>{String(version || '未提供')}</span>
        {Boolean(date) && <small className="record-value-date">{formatDate(String(date))}</small>}
      </>
    );
  }
  const names: Record<string, string> = {
    tap: 'Tap',
    hold: 'Hold',
    slide: 'Slide',
    touch: 'Touch',
    break: 'Break',
    total: '总计',
    left: '左',
    right: '右',
  };
  return (
    <span className="record-structured-value">
      {Object.entries(object).map(([key, item]) => (
        <span key={key}>
          <small>{names[key] || key}</small> <ValueDisplay value={item} />
        </span>
      ))}
    </span>
  );
}
function ChartHeading({
  record,
}: {
  record: Pick<ChartRecord, 'id' | 'difficulty' | 'difficulty_name' | 'title' | 'type'>;
}) {
  return (
    <div>
      <h3>{record.title}</h3>
      <div className="chart-meta">
        <span>ID {record.id}</span>
        <ChartTypeBadge type={record.type} />
        <DifficultyBadge
          difficulty={record.difficulty}
          name={difficultyName(record)}
          type={record.type}
        />
      </div>
    </div>
  );
}
function PendingEntry({
  record,
  draft,
  choicesDisabled,
  progress,
  onDraft,
}: {
  record: ChartRecord;
  draft: Draft;
  choicesDisabled: boolean;
  progress?: Operation['phase'];
  onDraft: (value: Draft) => void;
}) {
  const sources = [
    ...new Set(
      record.fields.flatMap((field) => field.options.map((option) => sourceKey(option.source))),
    ),
  ].sort((a, b) => {
    const order = ['lxns', 'diving_fish', 'arcade_songs', 'remywiki'];
    return (
      (order.includes(a) ? order.indexOf(a) : 99) - (order.includes(b) ? order.indexOf(b) : 99)
    );
  });
  const required = record.fields.filter((field) => field.required !== false);
  const selected = required.filter((field) =>
    field.options.some((option) => option.option_id === draft[field.field_key]?.option_id),
  ).length;
  const complete = required.length > 0 && selected === required.length;
  return (
    <article className="record-review-entry">
      <header className="record-review-heading">
        <ChartHeading record={record} />
        <span className="record-field-count">
          {record.decision_pending ? '选择已保存' : `${record.fields.length} 个待选字段`}
        </span>
      </header>
      <div
        className="record-table-scroll"
        role="region"
        aria-label={`${record.title} ${difficultyName(record)}的字段选择`}
        tabIndex={0}
      >
        <table className="record-diff-table">
          <thead>
            <tr>
              <th scope="col">字段</th>
              {sources.map((source) => (
                <th scope="col" key={source}>
                  {sourceName(source)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {record.fields.map((field) => (
              <tr key={field.field_key}>
                <th scope="row">{field.label || fieldNames[field.field_key] || field.field_key}</th>
                {sources.map((source) => (
                  <td key={source}>
                    {field.options
                      .filter((option) => sourceKey(option.source) === source)
                      .map((option) => {
                        const checked = draft[field.field_key]?.option_id === option.option_id;
                        const multiple =
                          field.options.filter((row) => sourceKey(row.source) === source).length >
                          1;
                        return (
                          <label
                            key={option.option_id}
                            className={'record-option' + (checked ? ' is-selected' : '')}
                          >
                            <input
                              type="radio"
                              name={`${record.chart_key}:${field.field_key}`}
                              value={option.option_id}
                              checked={checked}
                              disabled={choicesDisabled}
                              onChange={() =>
                                onDraft({
                                  ...draft,
                                  [field.field_key]: {
                                    field_key: field.field_key,
                                    option_id: option.option_id,
                                  },
                                })
                              }
                              aria-label={`${field.label || field.field_key}，选择${sourceName(option.source)}${multiple ? ` ${option.external_id || option.option_id}` : ''}`}
                            />
                            <span>
                              <ValueDisplay value={option.value} />
                              {option.date && (
                                <small className="record-value-date">
                                  {formatDate(option.date)}
                                </small>
                              )}
                              {multiple && option.external_id && (
                                <small className="record-option-id">{option.external_id}</small>
                              )}
                              {option.sources && option.sources.length > 1 && (
                                <small className="record-option-id">
                                  {option.sources.map(sourceName).join(' · ')}
                                </small>
                              )}
                            </span>
                          </label>
                        );
                      })}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <footer className="record-review-footer">
        <span role="status">
          {progress === 'submitting'
            ? '正在保存选择'
            : progress === 'applying'
              ? '选择已保存，正在入库'
              : record.decision_pending
                ? '已保存，等待应用'
                : complete
                  ? '所有字段已选择'
                  : `已选 ${selected} / ${required.length}`}
        </span>
        <div>
          {Object.keys(draft).length > 0 && (
            <button
              type="button"
              className="btn quiet"
              disabled={choicesDisabled}
              onClick={() => onDraft({})}
            >
              清空选择
            </button>
          )}
        </div>
      </footer>
    </article>
  );
}
function SavedEntry({
  record,
  disabled,
  onRevoke,
}: {
  record: SavedRecord;
  disabled: boolean;
  onRevoke: (fields: string[]) => void;
}) {
  return (
    <article className="record-review-entry">
      <header className="record-review-heading">
        <ChartHeading record={record} />
      </header>
      <div
        className="record-table-scroll"
        role="region"
        aria-label={`${record.title}的人工记录`}
        tabIndex={0}
      >
        <table className="record-diff-table record-saved-table">
          <thead>
            <tr>
              <th scope="col">字段</th>
              <th scope="col">已选值</th>
              <th scope="col">来源</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {record.fields.map((field) => (
              <tr key={field.field_key}>
                <th scope="row">{field.label || fieldNames[field.field_key] || field.field_key}</th>
                <td>
                  <ValueDisplay value={field.value} />
                  {field.date && (
                    <small className="record-value-date">{formatDate(field.date)}</small>
                  )}
                </td>
                <td>{sourceName(field.source)}</td>
                <td>
                  <button
                    type="button"
                    className="btn quiet"
                    disabled={disabled}
                    onClick={() => onRevoke([field.field_key])}
                  >
                    撤销
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </article>
  );
}

export function CatalogReview({ control }: { control: CatalogUpdateControl }) {
  const [data, setData] = useState<ReviewData | null>(null),
    [saved, setSaved] = useState<DecisionsData | null>(null);
  const [loading, setLoading] = useState(true),
    [submitting, setSubmitting] = useState(false),
    [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [operation, setOperation] = useState<Operation | null>(null),
    [preview, setPreview] = useState<BatchPreview | null>(null);
  const [error, setError] = useState(''),
    [refreshNeeded, setRefreshNeeded] = useState(false),
    [message, setMessage] = useState('');
  const [reload, setReload] = useState(0),
    [filter, setFilter] = useState<'pending' | 'manual'>('pending'),
    [search, setSearch] = useState(''),
    [page, setPage] = useState(0);
  const requestId = useRef(0),
    submission = useRef(false),
    draftsRef = useRef(drafts),
    dialog = useRef<HTMLDialogElement>(null);
  draftsRef.current = drafts;
  const mainRunning = control.state?.main.status === 'running';
  useEffect(() => {
    if (mainRunning) return;
    const controller = new AbortController(),
      request = ++requestId.current;
    setLoading(true);
    Promise.all([
      api<ReviewData>('/catalog/records/review', { signal: controller.signal, cache: 'no-store' }),
      api<DecisionsData>('/catalog/records/decisions', {
        signal: controller.signal,
        cache: 'no-store',
      }),
    ])
      .then(([result, decisions]) => {
        if (controller.signal.aborted || request !== requestId.current) return;
        const remaining = reconcileDrafts(draftsRef.current, result.records);
        draftsRef.current = remaining;
        setDrafts(remaining);
        setData(result);
        setSaved(decisions);
        setRefreshNeeded(false);
        setError('');
        setOperation((previous) => (previous?.phase === 'submitting' ? previous : null));
      })
      .catch((error) => {
        if (!controller.signal.aborted && request === requestId.current) setError(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted && request === requestId.current) setLoading(false);
      });
    return () => controller.abort();
  }, [control.state?.revision, control.state?.decision_revision, mainRunning, reload]);
  useEffect(() => {
    if (!preview || submission.current) return;
    const changed =
      (control.state?.revision !== undefined && control.state.revision !== preview.revision) ||
      (control.state?.decision_revision !== undefined &&
        control.state.decision_revision !== preview.decision_revision) ||
      (data !== null &&
        (data.revision !== preview.revision ||
          data.decision_revision !== preview.decision_revision));
    if (changed) {
      setPreview(null);
      setError('曲库已变化，请重新核对选择后提交。');
    }
  }, [preview, control.state?.revision, control.state?.decision_revision, data]);
  useEffect(() => {
    const element = dialog.current;
    if (preview && element && !element.open) element.showModal();
    return () => {
      if (element?.open) element.close();
    };
  }, [preview]);
  const selected = useMemo(() => {
    const records = (data?.records || []).filter(
      (record) =>
        !record.decision_pending && Object.keys(drafts[record.chart_key] || {}).length > 0,
    );
    let fields = 0;
    const incomplete = records.filter((record) => {
      const chosen = (field: Field) =>
        field.options.some(
          (option) => option.option_id === drafts[record.chart_key]?.[field.field_key]?.option_id,
        );
      fields += record.fields.filter(chosen).length;
      const required = record.fields.filter((field) => field.required !== false);
      return required.length === 0 || !required.every(chosen);
    }).length;
    return { records, fields, incomplete };
  }, [data, drafts]);
  const filtered = useMemo(() => {
    const needle = normalized(search.trim());
    return (filter === 'pending' ? data?.records || [] : saved?.records || []).filter(
      (record) =>
        !needle ||
        normalized(`${record.id} ${record.title} ${difficultyName(record)}`).includes(needle),
    );
  }, [data, saved, search, filter]);
  const pages = Math.max(1, Math.ceil(filtered.length / 4)),
    currentPage = Math.min(page, pages - 1);
  const disabled =
    submitting || loading || control.mainBusy || Boolean(control.action) || refreshNeeded;
  const decisionsPending = control.state?.decisions_pending ?? data?.decisions_pending ?? false;
  const applying =
    operation?.phase === 'applying' && (control.mainBusy || loading || decisionsPending);
  function openPreview() {
    if (!data || disabled || !selected.records.length || selected.incomplete) return;
    setError('');
    setMessage('');
    const records = selected.records.map((record) => ({
      ...record,
      fields: record.fields.flatMap((field) => {
        const option = field.options.find(
          (option) => option.option_id === drafts[record.chart_key]?.[field.field_key]?.option_id,
        );
        return option
          ? [
              {
                field_key: field.field_key,
                label: field.label || fieldNames[field.field_key] || field.field_key,
                option,
              },
            ]
          : [];
      }),
    }));
    setPreview(
      structuredClone({
        revision: data.revision,
        decision_revision: data.decision_revision,
        records,
      }),
    );
  }
  async function submitBatch() {
    if (!preview || disabled || submission.current) return;
    const confirmed = preview;
    if (
      (control.state?.revision !== undefined && control.state.revision !== confirmed.revision) ||
      (control.state?.decision_revision !== undefined &&
        control.state.decision_revision !== confirmed.decision_revision)
    ) {
      setPreview(null);
      setError('曲库已变化，请重新核对选择后提交。');
      return;
    }
    submission.current = true;
    setSubmitting(true);
    setOperation({
      chartKeys: confirmed.records.map((record) => record.chart_key),
      phase: 'submitting',
    });
    setError('');
    setMessage('');
    try {
      const body = {
        base_catalog_revision: confirmed.revision,
        base_decision_revision: confirmed.decision_revision,
        records: confirmed.records.map((record) => ({
          id: record.id,
          difficulty: record.difficulty,
          choices: record.fields.map((field) => ({
            field_key: field.field_key,
            option_id: field.option.option_id,
          })),
        })),
      };
      const next = await api<CatalogUpdateState>('/catalog/records/decisions/batch', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      const remaining = { ...draftsRef.current };
      for (const record of confirmed.records) delete remaining[record.chart_key];
      draftsRef.current = remaining;
      setDrafts(remaining);
      setPreview(null);
      setOperation({
        chartKeys: confirmed.records.map((record) => record.chart_key),
        phase: 'applying',
      });
      control.receive(next);
      setMessage(`${confirmed.records.length} 张谱面的选择已保存，正在统一入库`);
      await control.reload();
      setReload((value) => value + 1);
    } catch (error) {
      setOperation(null);
      setError(messageOf(error));
      if (error instanceof ApiError && error.status === 409) setRefreshNeeded(true);
    } finally {
      submission.current = false;
      setSubmitting(false);
    }
  }
  async function revoke(record: SavedRecord, fields: string[]) {
    if (!data || disabled || submission.current) return;
    submission.current = true;
    setSubmitting(true);
    setOperation({ chartKeys: [record.chart_key], phase: 'submitting' });
    setError('');
    setMessage('');
    try {
      const body = {
        base_catalog_revision: data.revision,
        base_decision_revision: data.decision_revision,
        id: record.id,
        difficulty: record.difficulty,
        field_keys: fields,
      };
      const next = await api<CatalogUpdateState>('/catalog/records/decisions/revoke', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      setOperation({ chartKeys: [record.chart_key], phase: 'applying' });
      control.receive(next);
      setMessage('裁决已撤销，正在更新');
      await control.reload();
      setReload((value) => value + 1);
    } catch (error) {
      setOperation(null);
      setError(messageOf(error));
      if (error instanceof ApiError && error.status === 409) setRefreshNeeded(true);
    } finally {
      submission.current = false;
      setSubmitting(false);
    }
  }
  function refresh() {
    setMessage('');
    void control.reload();
    setReload((value) => value + 1);
  }
  function updateDraft(chartKey: string, value: Draft) {
    const next = { ...draftsRef.current, [chartKey]: value };
    draftsRef.current = next;
    setDrafts(next);
  }
  const admitted = data?.counts?.admitted ?? data?.admitted_count,
    pendingCount = data?.counts?.pending ?? data?.pending_count ?? data?.records.length;
  return (
    <section className="settings-panel catalog-review" id="catalog-record-review">
      <div className="catalog-panel-title">
        <h2>曲库差异</h2>
        <button
          className="btn quiet"
          type="button"
          disabled={submitting || control.mainBusy}
          onClick={refresh}
        >
          刷新
        </button>
      </div>
      <div className="review-counts">
        <span>
          已入库 <strong>{admitted ?? '—'}</strong>
        </span>
        <span>
          待入库 <strong>{pendingCount ?? '—'}</strong>
        </span>
      </div>
      {decisionsPending && (
        <div className="review-pending" role="status">
          <span>{control.mainBusy ? '正在应用已保存的选择' : '选择已保存，尚未应用到曲库'}</span>
          <button
            className="btn"
            type="button"
            disabled={disabled}
            onClick={() => void control.run('decisions/reapply')}
          >
            重新应用
          </button>
        </div>
      )}
      {data?.initialized !== false && (
        <div className="review-toolbar">
          <div className="review-tabs" aria-label="曲库差异筛选">
            {(
              [
                ['pending', '待入库'],
                ['manual', '人工记录'],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                className={filter === value ? 'selected' : ''}
                onClick={() => {
                  setFilter(value);
                  setPage(0);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <input
            type="search"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setPage(0);
            }}
            placeholder="曲名或 ID"
            aria-label="搜索曲库差异"
          />
        </div>
      )}
      {error && !preview && (
        <div className="form-message" role="alert">
          {error}
          {refreshNeeded && (
            <button
              className="btn quiet"
              type="button"
              disabled={submitting || control.mainBusy}
              onClick={refresh}
            >
              刷新差异
            </button>
          )}
        </div>
      )}
      {message && !decisionsPending && (
        <div className="review-message" role="status">
          {control.mainBusy
            ? message
            : control.state?.main.status === 'failed'
              ? '选择已保存，应用失败，可重新应用。'
              : '曲库已更新'}
        </div>
      )}
      <div className="record-review-list" aria-busy={loading || submitting || control.mainBusy}>
        {filtered
          .slice(currentPage * 4, currentPage * 4 + 4)
          .map((record) =>
            filter === 'pending' ? (
              <PendingEntry
                key={record.chart_key}
                record={record as ChartRecord}
                draft={drafts[record.chart_key] || {}}
                choicesDisabled={
                  refreshNeeded ||
                  Boolean(record.decision_pending) ||
                  Boolean(operation?.chartKeys.includes(record.chart_key))
                }
                progress={
                  operation?.chartKeys.includes(record.chart_key) &&
                  (submitting || control.mainBusy || loading)
                    ? operation.phase
                    : undefined
                }
                onDraft={(value) => updateDraft(record.chart_key, value)}
              />
            ) : (
              <SavedEntry
                key={record.chart_key}
                record={record as SavedRecord}
                disabled={disabled}
                onRevoke={(fields) => void revoke(record as SavedRecord, fields)}
              />
            ),
          )}
      </div>
      {!filtered.length && (
        <div className="review-empty">
          {loading
            ? '载入中…'
            : data?.initialized === false
              ? '更新一次曲库后可查看差异'
              : error
                ? '差异数据未能载入'
                : search
                  ? '没有匹配的谱面'
                  : filter === 'pending'
                    ? '没有待入库的谱面'
                    : '暂无人工记录'}
        </div>
      )}
      {filtered.length > 0 && (
        <div className="review-pager">
          <span>{filtered.length} 张谱面</span>
          <div>
            <button
              type="button"
              className="btn quiet"
              disabled={currentPage === 0}
              onClick={() => setPage((value) => Math.max(0, value - 1))}
              aria-label="上一页曲库差异"
            >
              ‹
            </button>
            <span>
              {currentPage + 1} / {pages}
            </span>
            <button
              type="button"
              className="btn quiet"
              disabled={currentPage >= pages - 1}
              onClick={() => setPage(currentPage + 1)}
              aria-label="下一页曲库差异"
            >
              ›
            </button>
          </div>
        </div>
      )}
      {data?.initialized !== false &&
        (filter === 'pending' || selected.records.length > 0 || operation) && (
          <div className="record-batch-actions">
            <div role="status" aria-live="polite">
              <strong>
                {submitting
                  ? '正在提交选择'
                  : applying
                    ? '选择已保存，正在统一入库'
                    : `已选 ${selected.records.length} 张谱面 · ${selected.fields} 个字段`}
              </strong>
              {selected.incomplete > 0 && (
                <span>还有 {selected.incomplete} 张谱面的字段未选完</span>
              )}
            </div>
            <button
              type="button"
              className="btn primary"
              disabled={disabled || selected.records.length === 0 || selected.incomplete > 0}
              onClick={openPreview}
            >
              {submitting || applying ? (
                <>
                  <span className="spinner" />
                  {submitting ? '提交中…' : '入库中…'}
                </>
              ) : (
                '确认选择'
              )}
            </button>
          </div>
        )}
      {preview && (
        <dialog
          ref={dialog}
          className="record-confirm-dialog"
          aria-labelledby="record-review-confirm-title"
          aria-describedby="record-review-confirm-description"
          onCancel={(event) => {
            event.preventDefault();
            if (!submission.current) setPreview(null);
          }}
        >
          <header className="record-confirm-heading">
            <h2 id="record-review-confirm-title">确认这些选择</h2>
            <p id="record-review-confirm-description">
              {preview.records.length} 张谱面 ·{' '}
              {preview.records.reduce((total, record) => total + record.fields.length, 0)} 个字段
            </p>
          </header>
          <div className="record-confirm-list">
            {preview.records.map((record) => (
              <article className="record-confirm-entry" key={record.chart_key}>
                <ChartHeading record={record} />
                <table className="record-confirm-table">
                  <thead>
                    <tr>
                      <th scope="col">字段</th>
                      <th scope="col">选择</th>
                      <th scope="col">来源</th>
                    </tr>
                  </thead>
                  <tbody>
                    {record.fields.map((field) => (
                      <tr key={field.field_key}>
                        <th scope="row">{field.label}</th>
                        <td>
                          <ValueDisplay value={field.option.value} />
                          {field.option.date && (
                            <small className="record-value-date">
                              {formatDate(field.option.date)}
                            </small>
                          )}
                        </td>
                        <td>
                          {(field.option.sources?.length
                            ? field.option.sources
                            : [field.option.source]
                          )
                            .map(sourceName)
                            .join(' · ')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </article>
            ))}
          </div>
          {error && (
            <div className="form-message" role="alert">
              {error}
            </div>
          )}
          <footer className="record-confirm-actions">
            <button
              autoFocus
              type="button"
              className="btn"
              disabled={submitting}
              onClick={() => setPreview(null)}
            >
              返回修改
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={disabled}
              onClick={() => void submitBatch()}
            >
              {submitting ? (
                <>
                  <span className="spinner" />
                  提交中…
                </>
              ) : (
                '确认提交'
              )}
            </button>
          </footer>
        </dialog>
      )}
    </section>
  );
}
