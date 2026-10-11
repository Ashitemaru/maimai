import { CatalogChanges } from './CatalogChanges';
import { CatalogReview } from './CatalogReview';
import { formatTimestamp } from './dateTime';
import type { CatalogUpdateControl } from './useCatalogUpdate';

const snapshotDate = (value: string | null | undefined) => formatTimestamp(value, '尚未更新');

function MainStatus({ control }: { control: CatalogUpdateControl }) {
  const { state, mainBusy } = control;
  if (mainBusy)
    return (
      <>
        <span className="spinner" />
        {state?.main.message || '正在更新曲库'}
      </>
    );
  if (state?.main.status === 'failed') return <span className="catalog-failed">更新失败</span>;
  if (state?.main.status === 'succeeded')
    return (
      <>
        曲库已更新 <span className="catalog-time">{snapshotDate(state.snapshot_at)}</span>
      </>
    );
  return (
    <>
      国服曲库 <span className="catalog-time">{snapshotDate(state?.snapshot_at)}</span>
    </>
  );
}

function RemyStatus({ control }: { control: CatalogUpdateControl }) {
  const { state, remyBusy } = control;
  if (!state) return null;
  const pending = state.remy.pending_count;
  return (
    <span className={'catalog-remy ' + (state.remy.status === 'failed' ? 'catalog-failed' : '')}>
      {remyBusy
        ? '版本核准中'
        : pending
          ? `${pending} 张版本待核实`
          : state.remy.status === 'pending'
            ? state.remy.message || '版本证据待核对'
            : state.remy.status === 'failed'
              ? '版本核准暂不可用'
              : '版本已核准'}
      {remyBusy && pending > 0 && <span> · {pending} 张待核实</span>}
    </span>
  );
}

export function CatalogUpdatePanel({ control }: { control: CatalogUpdateControl }) {
  const { state, error, mainBusy, remyBusy, action } = control;
  const diff = state?.main.diff;
  const warnings = state?.main.warnings || [];
  const canRetryRemy = (state?.remy.pending_count ?? 0) > 0 && !remyBusy;
  return (
    <>
      <section className="settings-panel catalog-settings">
        <div className="catalog-panel-title">
          <h2>国服曲库</h2>
          <button
            type="button"
            className="btn primary"
            disabled={mainBusy || action !== null}
            onClick={() => void control.run('update')}
          >
            {mainBusy ? (
              <>
                <span className="spinner" />
                更新中…
              </>
            ) : (
              '更新曲库'
            )}
          </button>
        </div>
        <div className="catalog-main-status" role="status" aria-live="polite">
          <MainStatus control={control} />
        </div>
        {diff && (
          <div className="catalog-diff">
            <span>
              新增歌曲 <strong>{diff.songs_added}</strong>
            </span>
            <span>
              新增谱面 <strong>{diff.charts_added}</strong>
            </span>
            <span>
              定数变化 <strong>{diff.constants_changed}</strong>
            </span>
          </div>
        )}
        {(error || state?.main.error) && (
          <div className="form-message">{error || state?.main.error}</div>
        )}
        {warnings.length > 0 && (
          <details className="catalog-warnings">
            <summary>{warnings.length} 项更新提示</summary>
            <ul>
              {warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </details>
        )}
        {state && <CatalogChanges revision={state.revision} />}
        <div className="catalog-verification">
          <div>
            <strong>Re:MASTER 版本</strong>
            <RemyStatus control={control} />
            {state && (
              <small>
                已核准 {state.remy.verified_count} 张
                {state.remy.updated_count > 0 ? ` · 本次补齐 ${state.remy.updated_count} 张` : ''}
              </small>
            )}
          </div>
          {canRetryRemy && (
            <div className="catalog-verification-actions">
              <button
                type="button"
                className="btn"
                disabled={mainBusy || action !== null}
                onClick={() => void control.run('remy/retry')}
              >
                重试
              </button>
            </div>
          )}
        </div>
      </section>
      <CatalogReview control={control} />
    </>
  );
}
