import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { api, messageOf } from './api';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';

interface SongChange {
  song_id: number;
  title: string;
}
interface ChartChange extends SongChange {
  chart_key: string;
  type: string;
  difficulty: number;
  constant?: number | null;
  changes?: Record<string, { before: unknown; after: unknown }>;
}
interface UpdateReport {
  diff: {
    added_songs?: SongChange[];
    added_charts?: ChartChange[];
    changed_charts?: ChartChange[];
  } | null;
  issues: Record<string, unknown>[];
  sources: unknown;
}
const LIMIT = 50;
const valueText = (value: unknown) =>
  value === null || value === undefined
    ? '—'
    : typeof value === 'number'
      ? value.toFixed(1)
      : String(value);

function ChangeGroup<T>({
  title,
  rows,
  render,
}: {
  title: string;
  rows: T[];
  render: (row: T, index: number) => ReactNode;
}) {
  if (!rows.length) return null;
  return (
    <section className="catalog-change-group">
      <h3>
        {title}
        <span>{rows.length}</span>
      </h3>
      <ul>
        {rows.slice(0, LIMIT).map((row, index) => (
          <li key={index}>{render(row, index)}</li>
        ))}
      </ul>
      {rows.length > LIMIT && <small>显示前 {LIMIT} 项，完整内容见 JSON</small>}
    </section>
  );
}

export function CatalogChanges({ revision }: { revision: string }) {
  const [cached, setCached] = useState<{ revision: string; data: UpdateReport } | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (cached?.revision === revision) return;
    const controller = new AbortController();
    setLoading(true);
    setError('');
    api<UpdateReport>('/catalog/update/report', { signal: controller.signal, cache: 'no-store' })
      .then((data) => {
        if (!controller.signal.aborted) setCached({ revision, data });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision, cached?.revision, attempt]);
  const data = cached?.revision === revision ? cached.data : null;
  const constants = (data?.diff?.changed_charts || []).filter((row) => row.changes?.level_value);
  return (
    <section className="catalog-changes" aria-label="本次曲库变化">
      <div className="catalog-changes-content">
        {loading && (
          <div className="catalog-change-empty" role="status">
            载入中…
          </div>
        )}
        {error && (
          <div className="form-message">
            {error}{' '}
            <button
              type="button"
              className="btn quiet"
              onClick={() => setAttempt((value) => value + 1)}
            >
              重试
            </button>
          </div>
        )}
        {data && (
          <>
            <ChangeGroup
              title="定数变化"
              rows={constants}
              render={(row) => (
                <>
                  <strong>{row.title}</strong>
                  <div className="chart-meta">
                    <ChartTypeBadge type={row.type} />
                    <DifficultyBadge difficulty={row.difficulty} type={row.type} />
                    <span>
                      {valueText(row.changes!.level_value.before)} →{' '}
                      {valueText(row.changes!.level_value.after)}
                    </span>
                  </div>
                </>
              )}
            />
            <ChangeGroup
              title="新增歌曲"
              rows={data.diff?.added_songs || []}
              render={(row) => (
                <>
                  <strong>{row.title}</strong>
                  <span>ID {row.song_id}</span>
                </>
              )}
            />
            <ChangeGroup
              title="新增谱面"
              rows={data.diff?.added_charts || []}
              render={(row) => (
                <>
                  <strong>{row.title}</strong>
                  <div className="chart-meta">
                    <ChartTypeBadge type={row.type} />
                    <DifficultyBadge difficulty={row.difficulty} type={row.type} />
                    <span>{valueText(row.constant)}</span>
                  </div>
                </>
              )}
            />
            <a
              className="catalog-report-download"
              href="#settings"
              onClick={(event) => {
                event.preventDefault();
                document
                  .getElementById('catalog-record-review')
                  ?.scrollIntoView({ behavior: 'smooth' });
              }}
            >
              处理曲库差异 →
            </a>
            <a
              className="catalog-report-download"
              href="/api/catalog/update/report"
              download="catalog-update-report.json"
            >
              下载完整 JSON ↓
            </a>
          </>
        )}
      </div>
    </section>
  );
}
