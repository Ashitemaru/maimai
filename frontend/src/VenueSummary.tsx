import { useEffect, useState } from 'react';
import { api, messageOf } from './api';

type VenueRow = {
  name: string;
  city: string;
  counts: Record<string, number>;
  total: number;
  pending_count: number;
};
type VenueTotals = {
  levels: { value: string; label: string }[];
  items: VenueRow[];
  totals: Record<string, number>;
  total: number;
  pending_count: number;
};

function sum(items: VenueRow[], level?: string) {
  return items.reduce((total, row) => total + (level ? row.counts[level] || 0 : row.total), 0);
}

export function VenueSummary({ revision }: { revision: number }) {
  const [data, setData] = useState<VenueTotals | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    setLoading(true);
    api<VenueTotals>('/venues/summary', { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setData(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision, retry]);

  const query = search.trim().toLocaleLowerCase();
  const rows = (data?.items || []).filter((row) =>
    `${row.city} ${row.name}`.toLocaleLowerCase().includes(query),
  );
  const cities = new Map<string, VenueRow[]>();
  for (const row of rows) {
    const group = cities.get(row.city) || [];
    group.push(row);
    cities.set(row.city, group);
  }
  return (
    <section className="workspace venue-summary" aria-label="机厅 AP 汇总" aria-busy={loading}>
      <div className="venue-summary-toolbar">
        <label className="venue-summary-search">
          <span className="sr-only">搜索机厅或城市</span>
          <input
            type="search"
            placeholder="搜索机厅或城市"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        {data && (
          <span className="venue-summary-total" aria-live="polite">
            {rows.length} 家机厅 · {sum(rows)} AP
          </span>
        )}
      </div>
      {error && (
        <div className="error-box venue-summary-error" role="alert">
          <span>{error}</span>
          <button className="btn" type="button" onClick={() => setRetry((value) => value + 1)}>
            重试
          </button>
        </div>
      )}
      {!data && !error && (
        <div className="empty-state" role="status">
          正在加载机厅…
        </div>
      )}
      {data && (
        <div className="venue-summary-scroll" tabIndex={0} aria-label="机厅汇总表，可横向滚动">
          <table className="venue-summary-table">
            <caption className="sr-only">各机厅按谱面等级统计的 AP 数量</caption>
            <thead>
              <tr>
                <th scope="col">机厅</th>
                {data.levels.map((level) => (
                  <th key={level.value} scope="col">
                    AP {level.label}
                  </th>
                ))}
                <th scope="col">合计</th>
              </tr>
            </thead>
            {[...cities].map(([city, group]) => {
              return (
                <tbody key={city}>
                  <tr className="venue-summary-city">
                    <th scope="row">
                      {city}
                      <span>小计</span>
                    </th>
                    {data.levels.map((level) => (
                      <td key={level.value}>{sum(group, level.value)}</td>
                    ))}
                    <td>{sum(group)}</td>
                  </tr>
                  {group.map((row) => (
                    <tr key={row.name} className="venue-summary-entry">
                      <th scope="row">{row.name.replace(/^【[^】]+】/, '')}</th>
                      {data.levels.map((level) => (
                        <td key={level.value} data-empty={!row.counts[level.value]}>
                          {row.counts[level.value] || 0}
                        </td>
                      ))}
                      <td>{row.total}</td>
                    </tr>
                  ))}
                </tbody>
              );
            })}
            {!rows.length && (
              <tbody>
                <tr>
                  <td colSpan={data.levels.length + 2} className="venue-summary-empty">
                    没有匹配的机厅
                  </td>
                </tr>
              </tbody>
            )}
            <tfoot>
              <tr>
                <th scope="row">合计</th>
                {data.levels.map((level) => (
                  <td key={level.value}>{sum(rows, level.value)}</td>
                ))}
                <td>{sum(rows)}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      )}
    </section>
  );
}
