import { useRef, useState } from 'react';
import { api, ApiError, messageOf } from './api';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { formatTimestamp } from './dateTime';
import type { APRecordItem, APRecords } from './types';

export function PendingRecords({
  records,
  onSelect,
  onChanged,
  notify,
}: {
  records: APRecords;
  onSelect: (record: APRecordItem) => void;
  onChanged: () => Promise<void>;
  notify: (message: string) => void;
}) {
  const [cancelling, setCancelling] = useState<string | null>(null);
  const writing = useRef(false);
  async function cancel(record: APRecordItem) {
    if (writing.current) return;
    writing.current = true;
    setCancelling(record.chart_key);
    try {
      await api(
        '/charts/' +
          encodeURIComponent(record.chart_key) +
          '/ap-record?' +
          new URLSearchParams({ expected_revision: String(record.record_revision) }),
        { method: 'DELETE' },
      );
      await onChanged();
      notify('已取消待核记录');
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) await onChanged().catch(() => {});
      notify(messageOf(error));
    } finally {
      writing.current = false;
      setCancelling(null);
    }
  }
  return (
    <section className="pending-panel" aria-label="待核记录列表">
      <div className="pending-summary">{records.count} 条待核记录</div>
      {records.items.length ? (
        <div className="pending-list">
          {records.items.map((record) => (
            <article className="pending-card" key={record.chart_key}>
              <button className="pending-chart" onClick={() => onSelect(record)}>
                <img src={record.thumbnail_url || record.jacket_url} alt="" loading="lazy" />
                <span className="pending-chart-info">
                  <strong>{record.title}</strong>
                  <span className="detail-badges">
                    <DifficultyBadge
                      difficulty={Number(record.chart_key.split(':').at(-1))}
                      name={record.difficulty_name}
                      constant={record.constant}
                      type={record.type}
                    />
                    <ChartTypeBadge type={record.type} />
                  </span>
                </span>
              </button>
              <div className="pending-record-info">
                <strong>{record.pending_record.venue || '机厅不记录'}</strong>
                <span>{record.pending_record.reason}</span>
                <time dateTime={record.pending_record.created_at}>
                  {formatTimestamp(record.pending_record.created_at)}
                </time>
              </div>
              <div className="pending-actions">
                <button className="btn" onClick={() => onSelect(record)}>
                  修改登记
                </button>
                <button
                  className="btn quiet"
                  disabled={cancelling !== null}
                  onClick={() => void cancel(record)}
                >
                  {cancelling === record.chart_key ? '取消中…' : '取消登记'}
                </button>
              </div>
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-state">暂无待核记录</div>
      )}
    </section>
  );
}
