import { useEffect, useState } from 'react';
import { api, messageOf } from './api';
import { useNotify } from './Toast';
import {
  matchingCollectionSection,
  publishingComplete,
  publishingState,
  renderPublishingImages,
} from './publishing';
import type {
  PublishingCollection,
  PublishingImages,
  PublishingJob,
  PublishingPlan,
} from './publishing';

interface StaleVideo {
  bvid: string;
  title: string;
  proposed_title: string;
  reasons: { code: string; text: string }[];
  chart_key: string;
  combo: 'ap' | 'app';
  achievement: string | null;
  constant_old: string;
  constant_new: string;
}
interface StaleList {
  connected: boolean;
  items: StaleVideo[];
  collections: PublishingCollection[];
}
interface Result {
  plan?: PublishingPlan;
  images?: PublishingImages;
  state?: string;
  error?: string;
}

const jobError = (job: PublishingJob) =>
  typeof job.error === 'string' ? job.error : job.error?.message || '';

export function BilibiliStale({ revision }: { revision: string }) {
  const notify = useNotify();
  const [loaded, setLoaded] = useState<{ revision: string; list: StaleList } | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [achievements, setAchievements] = useState<Record<string, string>>({});
  const [results, setResults] = useState<Record<string, Result>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (loaded?.revision === revision) return;
    const controller = new AbortController();
    api<StaleList>('/bilibili/stale', { signal: controller.signal })
      .then((list) => {
        if (controller.signal.aborted) return;
        setLoaded({ revision, list });
        setSelected([]);
        setResults({});
      })
      .catch((error) => {
        if (!controller.signal.aborted) notify(messageOf(error));
      });
    return () => controller.abort();
  }, [revision, loaded?.revision, notify]);
  const list = loaded?.list;
  if (!list || (list.connected && !list.items.length)) return null;
  const update = (bvid: string, result: Result) =>
    setResults((current) => ({ ...current, [bvid]: result }));
  const ready = list.items.filter(
    (item) =>
      selected.includes(item.bvid) &&
      results[item.bvid]?.images &&
      !publishingComplete(results[item.bvid].state || ''),
  );

  async function preview() {
    if (!list || busy) return;
    setBusy(true);
    try {
      for (const item of list.items.filter((row) => selected.includes(row.bvid))) {
        const section = matchingCollectionSection(list.collections, item.constant_new);
        if (!section) {
          update(item.bvid, { error: `找不到「${item.constant_new}」合集小节` });
          continue;
        }
        try {
          const plan = await api<PublishingPlan>('/bilibili/prepare', {
            method: 'POST',
            body: JSON.stringify({
              bvid: item.bvid,
              chart_key: item.chart_key,
              achievement: item.achievement ?? achievements[item.bvid] ?? '',
              combo: item.combo,
              constant: item.constant_new,
              section_id: Number(section),
            }),
          });
          if (plan.order?.status === 'blocked')
            update(item.bvid, { plan, error: '合集存在无法确定谱面或版本的稿件' });
          else update(item.bvid, { plan, images: await renderPublishingImages(plan) });
        } catch (error) {
          update(item.bvid, { error: messageOf(error) });
        }
      }
    } finally {
      setBusy(false);
    }
  }

  async function apply() {
    if (busy) return;
    setBusy(true);
    try {
      for (const item of ready) {
        const { plan, images } = results[item.bvid];
        try {
          const job = await api<PublishingJob>('/bilibili/apply', {
            method: 'POST',
            body: JSON.stringify({ plan_id: plan!.plan_id, ...images }),
          });
          const failed = jobError(job) || (!publishingComplete(job.state) && '更新未完成');
          update(item.bvid, { plan, images, state: job.state, error: failed || undefined });
          if (failed) break;
        } catch (error) {
          update(item.bvid, { plan, images, error: messageOf(error) });
          break;
        }
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="catalog-change-group" aria-label="B 站稿件待更新" aria-busy={busy}>
      <h3>
        B 站稿件待更新<span>{list.connected ? list.items.length : '未连接 B 站'}</span>
      </h3>
      {list.connected && (
        <>
          <ul>
            {list.items.map((item) => {
              const result = results[item.bvid];
              return (
                <li key={item.bvid}>
                  <label className="publishing-mirror">
                    <input
                      type="checkbox"
                      checked={selected.includes(item.bvid)}
                      disabled={busy}
                      onChange={(event) =>
                        setSelected((current) =>
                          event.target.checked
                            ? [...current, item.bvid]
                            : current.filter((bvid) => bvid !== item.bvid),
                        )
                      }
                    />
                    <span>
                      {item.title} → <strong>{item.proposed_title}</strong>
                    </span>
                  </label>
                  <span>{item.reasons.map((reason) => reason.text).join(' · ')}</span>
                  {item.achievement === null && (
                    <input
                      aria-label={`${item.bvid} 录像达成率`}
                      inputMode="decimal"
                      placeholder="录像达成率"
                      value={achievements[item.bvid] || ''}
                      disabled={busy}
                      onChange={(event) =>
                        setAchievements((current) => ({
                          ...current,
                          [item.bvid]: event.target.value,
                        }))
                      }
                    />
                  )}
                  {result?.images && (
                    <div className="publishing-preview publishing-cover-previews">
                      <figure>
                        <img src={result.images.cover16} alt={`${item.bvid} 16:9 封面预览`} />
                        <figcaption>16:9</figcaption>
                      </figure>
                      <figure>
                        <img src={result.images.cover43} alt={`${item.bvid} 4:3 封面预览`} />
                        <figcaption>4:3</figcaption>
                      </figure>
                    </div>
                  )}
                  {(result?.state || result?.error) && (
                    <span role="status">
                      {[result.state && publishingState(result.state), result.error]
                        .filter(Boolean)
                        .join(' · ')}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          <div className="form-actions">
            <button
              type="button"
              className="btn"
              disabled={busy || !selected.length}
              onClick={() => void preview()}
            >
              预览
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={busy || !ready.length}
              onClick={() => void apply()}
            >
              确认更新
            </button>
          </div>
        </>
      )}
    </section>
  );
}
