import { useEffect, useMemo, useRef, useState } from 'react';
import type { CompletionChart, CompletionTableData } from './types';
import { apiFetch, messageOf } from './api';
import { COMPLETION_BADGES, completionBadgeKey } from './completionBadges';
import { discardCompletionExport, exportCompletionTable } from './completionExport';
import { ImageSaveActions } from './ImageSaveActions';
import { downloadImage, needsSaveGesture, prepareImage } from './imageSave';
import type { PreparedImage } from './imageSave';

const batchSize = 48;
const chartName = (chart: CompletionChart) => chart.display_title || chart.title || '（空白曲名）';

function CompletionTile({
  chart,
  onSelect,
  retryKey,
}: {
  chart: CompletionChart;
  onSelect: (chart: CompletionChart) => void;
  retryKey: number;
}) {
  const [failed, setFailed] = useState(false);
  const [nearViewport, setNearViewport] = useState(false);
  const image = useRef<HTMLImageElement>(null);
  const preview = chart.thumbnail_url || chart.jacket_url;
  useEffect(() => setFailed(false), [preview, retryKey]);
  useEffect(() => {
    if (nearViewport || !image.current) return;
    if (!('IntersectionObserver' in window)) {
      setNearViewport(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNearViewport(true);
          observer.disconnect();
        }
      },
      { rootMargin: '80px' },
    );
    observer.observe(image.current);
    return () => observer.disconnect();
  }, [nearViewport]);
  const status = chart.is_ap
    ? chart.status === 'app'
      ? 'AP+'
      : 'AP'
    : chart.is_complete
      ? '已完成'
      : '未标记';
  const badgeKey = completionBadgeKey(chart);
  const badge = badgeKey ? COMPLETION_BADGES[badgeKey] : null;
  const label = `${chartName(chart)} · ${chart.type === 'standard' ? 'STD' : chart.type.toUpperCase()} · ${chart.difficulty_name} ${chart.constant.toFixed(1)} · ${status}${chart.pending_record ? ' · 待核实' : ''}${chart.conflict ? ' · 待核对' : ''}`;
  return (
    <button
      type="button"
      className="completion-tile"
      data-chart-key={chart.chart_key}
      data-difficulty={chart.difficulty}
      data-status={chart.status}
      data-complete={chart.is_complete}
      data-pending={Boolean(chart.pending_record)}
      aria-label={`查看 ${label}`}
      title={label}
      onClick={() => onSelect(chart)}
    >
      <span className="completion-art">
        {failed ? (
          <span className="completion-placeholder">{chartName(chart)}</span>
        ) : (
          <img
            ref={image}
            src={nearViewport ? preview : undefined}
            alt=""
            loading="lazy"
            decoding="async"
            onError={() => setFailed(true)}
          />
        )}
        {badge && <span className="completion-shade" aria-hidden="true" />}
        {badge && (
          <span className="completion-result" data-badge={badgeKey} aria-label={status}>
            <svg
              viewBox={`${badge.crop.x} ${badge.crop.y} ${badge.crop.width} ${badge.crop.height}`}
              aria-hidden="true"
            >
              <image href={badge.src} width="80" height="80" />
            </svg>
          </span>
        )}
        {chart.pending_record && (
          <span className="completion-pending" aria-label="待核实">
            待核
          </span>
        )}
        {chart.conflict && (
          <span className="completion-warning" aria-label="待核对">
            !
          </span>
        )}
      </span>
      <span className="completion-id" aria-hidden="true">
        {chart.id}
      </span>
    </button>
  );
}

export function CompletionTable({
  data,
  sort,
  description,
  onSelect,
  retryKey,
  notify,
}: {
  data: CompletionTableData;
  sort: string;
  description: string;
  onSelect: (chart: CompletionChart) => void;
  retryKey: number;
  notify: (message: string) => void;
}) {
  const [visibleCount, setVisibleCount] = useState(batchSize);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState('');
  const [readyImage, setReadyImage] = useState<PreparedImage | null>(null);
  const exportController = useRef<AbortController | null>(null);
  useEffect(() => {
    setReadyImage(null);
    setExporting(false);
    return () => {
      exportController.current?.abort();
      exportController.current = null;
    };
  }, [data, description, sort]);
  useEffect(
    () => () => {
      if (readyImage) URL.revokeObjectURL(readyImage.url);
    },
    [readyImage],
  );
  const sentinel = useRef<HTMLDivElement>(null);
  const grouped = sort === 'constant_desc' || sort === 'constant_asc';
  useEffect(() => setVisibleCount(batchSize), [data]);
  useEffect(() => {
    if (
      visibleCount >= data.items.length ||
      !sentinel.current ||
      !('IntersectionObserver' in window)
    )
      return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting))
          setVisibleCount((count) => Math.min(count + batchSize, data.items.length));
      },
      { rootMargin: '80px' },
    );
    observer.observe(sentinel.current);
    return () => observer.disconnect();
  }, [data, visibleCount]);
  const groups = useMemo(() => {
    const visible = data.items.slice(0, visibleCount);
    if (!grouped) return [{ key: 'all', items: visible }];
    const result: { key: string; items: CompletionChart[] }[] = [];
    for (const chart of visible) {
      const key = chart.constant.toFixed(1);
      if (result.at(-1)?.key !== key) result.push({ key, items: [] });
      result[result.length - 1].items.push(chart);
    }
    return result;
  }, [data, visibleCount, grouped]);
  const counts = useMemo(
    () => new Map(data.summary.constants.map((group) => [group.constant.toFixed(1), group])),
    [data],
  );
  function cancelExport() {
    exportController.current?.abort();
    setExporting(false);
    setExportProgress('');
  }
  async function download() {
    if (exporting) return;
    const controller = new AbortController();
    exportController.current?.abort();
    exportController.current = controller;
    setReadyImage(null);
    setExporting(true);
    setExportProgress('准备图片…');
    let artifactId: string | undefined;
    try {
      const { url, name, id } = await exportCompletionTable(data.items, {
        grouped,
        description,
        signal: controller.signal,
        onProgress: (value) => {
          if (!controller.signal.aborted) setExportProgress(value);
        },
      });
      artifactId = id;
      setExportProgress('准备下载…');
      const response = await apiFetch(url, { signal: controller.signal, cache: 'no-store' });
      if (!response.ok) throw new Error('下载图片失败，请重试');
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const image = prepareImage(blob, name);
      setReadyImage(image);
      if (needsSaveGesture()) notify('整张图片已生成，请选择分享或保存');
      else {
        downloadImage(image);
        notify('已开始下载 PNG');
      }
    } catch (error) {
      if (!controller.signal.aborted) notify(messageOf(error));
    } finally {
      if (exportController.current === controller) {
        exportController.current = null;
        setExporting(false);
        setExportProgress('');
      }
      if (artifactId)
        await discardCompletionExport(artifactId).catch(() => {
          // Successful PNG bytes are already local; the server also expires abandoned jobs.
        });
    }
  }

  return (
    <div className="completion-board" data-grouped={grouped}>
      <div className="completion-toolbar">
        <div className="completion-legend">
          <span>
            <svg
              className="completion-legend-badge"
              viewBox={`${COMPLETION_BADGES.ap.crop.x} ${COMPLETION_BADGES.ap.crop.y} ${COMPLETION_BADGES.ap.crop.width} ${COMPLETION_BADGES.ap.crop.height}`}
              aria-hidden="true"
            >
              <image href={COMPLETION_BADGES.ap.src} width="80" height="80" />
            </svg>
            已完成
          </span>
        </div>
        <div className="completion-export-actions">
          <button
            type="button"
            className="btn"
            disabled={exporting || !data.items.length}
            onClick={() => void download()}
          >
            {exporting ? exportProgress : '导出图片'}
          </button>
          {exporting && (
            <button type="button" className="btn quiet" onClick={cancelExport}>
              取消导出
            </button>
          )}
        </div>
      </div>
      {readyImage && <ImageSaveActions image={readyImage} key={readyImage.url} />}

      {groups.map((group) => {
        const count = counts.get(group.key);
        return (
          <section
            className="completion-group"
            data-constant={group.key}
            key={group.key}
            aria-label={grouped ? `定数 ${group.key}` : '完成表谱面'}
          >
            {grouped && (
              <div className="completion-group-heading">
                <h2>{group.key}</h2>
                <span aria-label={`${count?.complete ?? 0} / ${count?.total ?? 0} 张已完成`}>
                  <strong>{count?.complete ?? 0}</strong> / {count?.total ?? 0}
                </span>
                <progress
                  value={count?.complete ?? 0}
                  max={Math.max(1, count?.total ?? 0)}
                  aria-label={`定数 ${group.key} 完成进度`}
                />
              </div>
            )}
            <div className="completion-grid">
              {group.items.map((chart) => (
                <CompletionTile
                  key={chart.chart_key}
                  chart={chart}
                  onSelect={onSelect}
                  retryKey={retryKey}
                />
              ))}
            </div>
          </section>
        );
      })}
      {!data.items.length && <div className="empty-state">没有符合条件的谱面</div>}
      {visibleCount < data.items.length && (
        <div className="completion-more" ref={sentinel}>
          <button
            type="button"
            className="btn quiet"
            onClick={() => setVisibleCount((count) => count + batchSize)}
          >
            加载更多 · {Math.min(visibleCount, data.total)} / {data.total}
          </button>
        </div>
      )}
    </div>
  );
}
