import { useCallback, useEffect, useRef, useState } from 'react';
import QRCode from 'qrcode';
import { api, messageOf } from './api';
import { useNotify } from './Toast';
import { ChartTypeBadge, DifficultyBadge } from './ChartBadges';
import { loadBackgroundManifest } from './coverAssets';
import type { BackgroundManifest } from './coverAssets';
import { formatTimestamp } from './dateTime';
import type { Chart, ChartList } from './types';
import {
  parseBvid,
  videoTitleCombo,
  matchingCollectionSection,
  publishingState,
  publishingComplete,
  renderPublishingImages,
  type BilibiliSession,
  type PublishingCollection,
  type PublishingImages,
  type PublishingJob,
  type PublishingPlan,
  type PublishingVideo,
  type PublishingOrder,
  type PublishingOrderCheck,
  type PublishingOrderItem,
} from './publishing';

interface LoginRequest {
  login_id: string;
  url: string;
  expires_at: string;
  image: string;
  state: string;
}

function ChartLabel({ chart }: { chart: Chart }) {
  return (
    <>
      <img src={chart.thumbnail_url || chart.jacket_url} alt="" />
      <span className="publishing-chart-label">
        <strong>{chart.display_title || chart.title}</strong>
        <span className="chart-meta">
          <ChartTypeBadge type={chart.type} />
          <DifficultyBadge
            difficulty={chart.difficulty}
            name={chart.difficulty_name}
            type={chart.type}
          />
          <span>{chart.constant?.toFixed(1)}</span>
        </span>
      </span>
    </>
  );
}

function OrderVideo({ item }: { item: PublishingOrderItem }) {
  return (
    <span className="publishing-order-video">
      <a href={`https://www.bilibili.com/video/${item.bvid}`} target="_blank" rel="noreferrer">
        {item.song_title || item.title}
      </a>
      {item.version && <span>{item.version}</span>}
    </span>
  );
}

function UnmatchedOrderVideos({ items }: { items: PublishingOrder['unmatched'] }) {
  return (
    <ul className="publishing-order-unmatched" aria-label="待处理稿件">
      {items.map((item) => (
        <li key={item.bvid || item.aid}>
          <a href={`https://www.bilibili.com/video/${item.bvid}`} target="_blank" rel="noreferrer">
            {item.title}
          </a>
          <span>{item.reason}</span>
        </li>
      ))}
    </ul>
  );
}

function OrderReport({ report }: { report: PublishingOrderCheck }) {
  return (
    <div className="publishing-order-report">
      <time>{formatTimestamp(report.checked_at)}</time>
      {!report.sections.length && <p>暂无合集小节</p>}
      {report.sections.map((section) => {
        const current = new Map(section.current.map((item) => [item.bvid, item.position]));
        const changed = section.desired.filter((item) => current.get(item.bvid) !== item.position);
        return (
          <section className="publishing-order-section" key={section.section_id}>
            <div className="publishing-order-heading">
              <strong>
                {section.season_title} · {section.section_title}
              </strong>
              <span data-order-status={section.status}>
                {
                  { ordered: '顺序一致', changes: '顺序有差异', blocked: '无法确定顺序' }[
                    section.status
                  ]
                }
                {' · '}
                {section.current.length} 条
              </span>
            </div>
            {section.status === 'changes' && (
              <ul
                className="publishing-order-changes"
                aria-label={`${section.section_title} 顺序差异`}
              >
                {changed.map((item) => (
                  <li key={item.bvid}>
                    <span
                      className="publishing-order-position"
                      aria-label={`当前位置 ${current.get(item.bvid) ?? '新增'}，应有位置 ${item.position}`}
                    >
                      {current.get(item.bvid) ?? '新增'} → {item.position}
                    </span>
                    <OrderVideo item={item} />
                  </li>
                ))}
              </ul>
            )}
            {section.status === 'blocked' && <UnmatchedOrderVideos items={section.unmatched} />}
          </section>
        );
      })}
    </div>
  );
}

function PlanOrder({ order }: { order: PublishingOrder | undefined }) {
  if (!order) return <p className="publishing-order-blocked">请重新生成预览以确认合集顺序</p>;
  if (order.status === 'blocked')
    return (
      <div className="publishing-plan-order">
        <h3>合集顺序待处理</h3>
        <UnmatchedOrderVideos items={order.unmatched} />
      </div>
    );
  const index = order.desired.findIndex((item) => item.bvid === order.target_bvid);
  const before = index > 0 ? order.desired[index - 1] : null;
  const after = index >= 0 ? order.desired[index + 1] : null;
  return (
    <div className="publishing-plan-order">
      <h3>合集顺序</h3>
      <p>
        将排在第 {order.target_position} 位，共 {order.desired.length} 条
      </p>
      <dl className="publishing-order-neighbors">
        <div>
          <dt>上一条</dt>
          <dd>{before ? <OrderVideo item={before} /> : '已是首位'}</dd>
        </div>
        <div>
          <dt>下一条</dt>
          <dd>{after ? <OrderVideo item={after} /> : '已是末位'}</dd>
        </div>
      </dl>
      <p>
        {order.status === 'changes'
          ? `本次会调整「${order.section_title}」小节顺序`
          : '合集顺序已一致'}
      </p>
    </div>
  );
}

function PublishingSteps({ steps }: { steps: PublishingJob['steps'] }) {
  const labels: Record<string, string> = {
    metadata: '标题与封面',
    collection: '归入合集',
    order: '合集排序',
  };
  const states: Record<string, string> = {
    sending: '提交中',
    submitted: '待确认',
    verified: '已完成',
    rejected: '未完成',
  };
  if (!Object.keys(labels).some((key) => typeof steps[key] === 'string')) return null;
  return (
    <ul className="publishing-job-steps" aria-label="处理步骤">
      {Object.entries(labels).map(
        ([key, label]) =>
          typeof steps[key] === 'string' && (
            <li key={key} data-complete={steps[key] === 'verified'}>
              {label} · {states[steps[key]] || '待处理'}
            </li>
          ),
      )}
    </ul>
  );
}

export function PublishingPage({ onOpenCover }: { onOpenCover?: () => void } = {}) {
  const notify = useNotify();
  const [session, setSession] = useState<BilibiliSession | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [login, setLogin] = useState<LoginRequest | null>(null);
  const [videos, setVideos] = useState<PublishingVideo[]>([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  const [collections, setCollections] = useState<PublishingCollection[]>([]);
  const [orderCheck, setOrderCheck] = useState<PublishingOrderCheck | null>(null);
  const [checkingOrder, setCheckingOrder] = useState(false);
  const [jobs, setJobs] = useState<PublishingJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [input, setInput] = useState('');
  const [video, setVideo] = useState<PublishingVideo | null>(null);
  const [query, setQuery] = useState('');
  const [charts, setCharts] = useState<Chart[]>([]);
  const [chart, setChart] = useState<Chart | null>(null);
  const [achievement, setAchievement] = useState('');
  const [constant, setConstant] = useState('');
  const [combo, setCombo] = useState<'ap' | 'app'>('ap');
  const [mirror, setMirror] = useState(false);
  const [section, setSection] = useState('');
  const [automaticSection, setAutomaticSection] = useState(true);
  const [version, setVersion] = useState('');
  const [manifest, setManifest] = useState<BackgroundManifest | null>(null);
  const [prepared, setPrepared] = useState<{
    plan: PublishingPlan;
    images: PublishingImages | null;
    retryJob?: string;
  } | null>(null);
  const [job, setJob] = useState<PublishingJob | null>(null);
  const videoRequest = useRef(0);
  const sessionGeneration = useRef(0);
  const manualCombo = useRef<{ bvid: string; value: 'ap' | 'app' } | null>(null);
  const loginId = login?.login_id;
  const loginExpired = login?.state === 'expired';
  const publishingBusy = busy || loading || checkingSession;
  const selectedSection = automaticSection
    ? matchingCollectionSection(collections, constant)
    : collections.some((collection) =>
          collection.sections.some((item) => String(item.id) === section),
        )
      ? section
      : '';

  const clearSession = useCallback(() => {
    sessionGeneration.current += 1;
    setCheckingSession(false);
    setSession({ connected: false, account: null });
    setLogin(null);
    setVideo(null);
    setVideos([]);
    setCollections([]);
    setJobs([]);
    setPage(1);
    setHasMore(false);
    setLoading(false);
    setBusy(false);
    setCheckingOrder(false);
    setCharts([]);
    setChart(null);
    setOrderCheck(null);
    setPrepared(null);
    setJob(null);
    manualCombo.current = null;
    videoRequest.current += 1;
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const generation = sessionGeneration.current;
    setCheckingSession(true);
    api<BilibiliSession>('/bilibili/status', { signal: controller.signal })
      .then((next) => {
        if (controller.signal.aborted || generation !== sessionGeneration.current) return;
        if (next.connected) {
          setLogin(null);
          setSession(next);
        } else clearSession();
      })
      .catch((error) => {
        if (!controller.signal.aborted && generation === sessionGeneration.current)
          notify(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted && generation === sessionGeneration.current)
          setCheckingSession(false);
      });
    loadBackgroundManifest()
      .then(setManifest)
      .catch((error) => {
        if (!controller.signal.aborted) notify(messageOf(error));
      });
    return () => controller.abort();
  }, [clearSession, notify, refresh]);

  useEffect(() => {
    if (!session?.connected) return;
    const controller = new AbortController();
    const generation = sessionGeneration.current;
    setLoading(true);
    setCollections([]);
    Promise.all([
      api<{ items: PublishingVideo[]; has_more: boolean }>(`/bilibili/videos?page=${page}`, {
        signal: controller.signal,
      }),
      api<{ items: PublishingCollection[] }>('/bilibili/collections', {
        signal: controller.signal,
      }),
      api<{ items: PublishingJob[] }>('/bilibili/jobs', { signal: controller.signal }),
    ])
      .then(([listing, groups, history]) => {
        if (controller.signal.aborted || generation !== sessionGeneration.current) return;
        setVideos(listing.items);
        setHasMore(listing.has_more);
        setCollections(groups.items);
        setJobs(history.items);
      })
      .catch((error) => {
        if (!controller.signal.aborted && generation === sessionGeneration.current)
          notify(messageOf(error));
      })
      .finally(() => {
        if (!controller.signal.aborted && generation === sessionGeneration.current)
          setLoading(false);
      });
    return () => controller.abort();
  }, [session?.connected, page, refresh, notify]);

  useEffect(() => {
    if (!loginId || loginExpired || session?.connected) return;
    const controller = new AbortController();
    const generation = sessionGeneration.current;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api<{ state: string; account?: BilibiliSession['account'] }>(
          `/bilibili/login/${encodeURIComponent(loginId)}`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted || generation !== sessionGeneration.current) return;
        if (next.state === 'connected') {
          sessionGeneration.current += 1;
          setLogin(null);
          setSession({ connected: true, account: next.account || null });
          notify('Bilibili 已连接');
          return;
        }
        setLogin((current) => (current ? { ...current, state: next.state } : null));
        if (next.state === 'expired') return;
        timer = setTimeout(poll, 2500);
      } catch (error) {
        if (!controller.signal.aborted && generation === sessionGeneration.current) {
          notify(messageOf(error));
          setLogin((current) => (current ? { ...current, state: 'expired' } : null));
        }
      }
    };
    timer = setTimeout(poll, 1500);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [loginId, loginExpired, session?.connected, notify]);

  useEffect(() => {
    if (!video || !query.trim()) {
      setCharts([]);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api<ChartList>(
        '/charts?' + new URLSearchParams({ q: query, group: 'catalog', limit: '20' }),
        { signal: controller.signal },
      )
        .then((result) => {
          if (!controller.signal.aborted) setCharts(result.items);
        })
        .catch((error) => {
          if (!controller.signal.aborted) notify(messageOf(error));
        });
    }, 250);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, video, notify]);

  function invalidate() {
    setPrepared(null);
    setJob(null);
  }

  function changeConstant(value: string) {
    setConstant(value);
    setAutomaticSection(true);
  }

  async function checkOrder() {
    if (!session?.connected || checkingOrder || publishingBusy) return;
    const generation = sessionGeneration.current;
    setCheckingOrder(true);
    try {
      const result = await api<PublishingOrderCheck>('/bilibili/collections/order');
      if (generation === sessionGeneration.current) setOrderCheck(result);
    } catch (error) {
      if (generation === sessionGeneration.current) notify(messageOf(error));
    } finally {
      if (generation === sessionGeneration.current) setCheckingOrder(false);
    }
  }

  async function connect() {
    if (!session || session.connected || checkingSession || busy) return;
    const generation = ++sessionGeneration.current;
    setLogin(null);
    setBusy(true);
    try {
      const result = await api<Omit<LoginRequest, 'image' | 'state'>>('/bilibili/login', {
        method: 'POST',
      });
      const image = await QRCode.toDataURL(result.url, { width: 320, margin: 2 });
      if (generation !== sessionGeneration.current) return;
      setLogin({ ...result, image, state: 'pending' });
    } catch (error) {
      if (generation === sessionGeneration.current) notify(messageOf(error));
    } finally {
      if (generation === sessionGeneration.current) setBusy(false);
    }
  }

  async function disconnect() {
    if (!session?.connected || busy || checkingOrder) return;
    const generation = ++sessionGeneration.current;
    setBusy(true);
    try {
      await api('/bilibili/session', { method: 'DELETE' });
      if (generation === sessionGeneration.current) clearSession();
    } catch (error) {
      notify(messageOf(error));
      setRefresh((value) => value + 1);
    } finally {
      setBusy(false);
    }
  }

  async function selectVideo(value: string) {
    if (!session?.connected || publishingBusy) return;
    const bvid = parseBvid(value);
    if (!bvid) return notify('请填写 BV 号或视频链接');
    const request = ++videoRequest.current;
    setBusy(true);
    invalidate();
    try {
      const result = await api<PublishingVideo>(`/bilibili/videos/${bvid}`);
      if (request !== videoRequest.current) return;
      setVideo(result);
      setInput(result.bvid);
      setChart(null);
      setQuery('');
      setAchievement('');
      if (manualCombo.current?.bvid === result.bvid) setCombo(manualCombo.current.value);
      else {
        manualCombo.current = null;
        setCombo(videoTitleCombo(result) || 'ap');
      }
      setConstant('');
      setVersion('');
      setSection('');
      setAutomaticSection(true);
      setMirror(result.mirror_horizontal || result.bvid === 'BV1bJouBSEiH');
    } catch (error) {
      if (request === videoRequest.current) notify(messageOf(error));
    } finally {
      if (request === videoRequest.current) setBusy(false);
    }
  }

  async function preview(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session?.connected || !video || !chart || publishingBusy || !selectedSection) return;
    const generation = sessionGeneration.current;
    setBusy(true);
    invalidate();
    try {
      const plan = await api<PublishingPlan>('/bilibili/prepare', {
        method: 'POST',
        body: JSON.stringify({
          bvid: video.bvid,
          chart_key: chart.chart_key,
          achievement,
          combo,
          constant,
          mirror_horizontal: mirror,
          version_override: version,
          section_id: Number(selectedSection),
        }),
      });
      if (generation !== sessionGeneration.current) return;
      const images = plan.order?.status === 'blocked' ? null : await renderPublishingImages(plan);
      if (generation === sessionGeneration.current) setPrepared({ plan, images });
    } catch (error) {
      if (generation === sessionGeneration.current) notify(messageOf(error));
    } finally {
      if (generation === sessionGeneration.current) setBusy(false);
    }
  }

  async function apply() {
    if (
      !session?.connected ||
      !prepared?.images ||
      !prepared.plan.order ||
      prepared.plan.order.status === 'blocked' ||
      publishingBusy
    )
      return;
    const generation = sessionGeneration.current;
    setBusy(true);
    try {
      const result = prepared.retryJob
        ? await api<PublishingJob>(
            `/bilibili/jobs/${encodeURIComponent(prepared.retryJob)}/retry`,
            { method: 'POST' },
          )
        : await api<PublishingJob>('/bilibili/apply', {
            method: 'POST',
            body: JSON.stringify({ plan_id: prepared.plan.plan_id, ...prepared.images }),
          });
      if (generation !== sessionGeneration.current) return;
      setJob(result);
      setJobs((current) => [result, ...current.filter((item) => item.job_id !== result.job_id)]);
      if (result.error)
        notify(
          typeof result.error === 'string' ? result.error : result.error.message || '整理未完成',
        );
      else notify(publishingState(result.state));
    } catch (error) {
      if (generation === sessionGeneration.current) notify(messageOf(error));
    } finally {
      if (generation === sessionGeneration.current) setBusy(false);
    }
  }

  async function resume(previous: PublishingJob) {
    if (!session?.connected || publishingBusy) return;
    const generation = sessionGeneration.current;
    setBusy(true);
    try {
      const result = await api<PublishingJob & { plan: PublishingPlan; covers: PublishingImages }>(
        `/bilibili/jobs/${encodeURIComponent(previous.job_id)}`,
      );
      if (generation !== sessionGeneration.current) return;
      if (publishingComplete(result.state)) {
        setJobs((current) =>
          current.map((item) => (item.job_id === result.job_id ? result : item)),
        );
        notify('这项稿件已经整理完成');
        return;
      }
      if (!result.plan || !result.covers?.cover16 || !result.covers.cover43)
        throw new Error('请重新选择稿件并生成预览');
      setPrepared({ plan: result.plan, images: result.covers, retryJob: previous.job_id });
      setJob(result);
    } catch (error) {
      if (generation === sessionGeneration.current) notify(messageOf(error));
    } finally {
      if (generation === sessionGeneration.current) setBusy(false);
    }
  }

  return (
    <div className="publishing-page">
      <div className="heading">
        <h1>投稿整理</h1>
        {onOpenCover && (
          <button type="button" className="btn" onClick={onOpenCover}>
            生成封面
          </button>
        )}
      </div>
      <section className="settings-panel publishing-account">
        <div>
          <h2>Bilibili</h2>
          <span>
            {session?.connected
              ? session.account?.name || '已连接'
              : checkingSession
                ? '检查连接中…'
                : session
                  ? '尚未连接'
                  : '连接状态未确认'}
          </span>
        </div>
        {session?.connected ? (
          <button className="btn" disabled={busy || checkingOrder} onClick={disconnect}>
            断开连接
          </button>
        ) : !session && !checkingSession ? (
          <button className="btn" onClick={() => setRefresh((value) => value + 1)}>
            重试连接检查
          </button>
        ) : (
          <button
            className="btn primary"
            disabled={busy || checkingSession || !session}
            onClick={connect}
          >
            {login ? '重新获取二维码' : '扫码连接'}
          </button>
        )}
        {!session?.connected && login && (
          <div className="publishing-login">
            <img src={login.image} alt="Bilibili 登录二维码" />
            <span role="status">
              {login.state === 'expired'
                ? '二维码已过期'
                : login.state === 'scanned'
                  ? '请在 Bilibili 确认登录'
                  : '使用 Bilibili 扫码'}
            </span>
            <div className="form-actions">
              <a className="btn" href={login.image} download="bilibili-login.png">
                保存二维码
              </a>
              <a className="btn" href={login.url} target="_blank" rel="noreferrer">
                打开登录链接
              </a>
            </div>
          </div>
        )}
      </section>
      {session?.connected && (
        <>
          <section className="settings-panel" aria-busy={checkingOrder}>
            <div className="publishing-panel-heading">
              <h2>合集顺序</h2>
              <button
                className="btn"
                disabled={publishingBusy || checkingOrder}
                onClick={() => void checkOrder()}
              >
                {checkingOrder ? '检查中…' : '检查合集顺序'}
              </button>
            </div>
            {orderCheck && <OrderReport report={orderCheck} />}
          </section>
          <section className="settings-panel">
            <div className="publishing-panel-heading">
              <h2>选择稿件</h2>
              <button
                className="btn"
                disabled={publishingBusy}
                onClick={() => {
                  setLoading(true);
                  setRefresh((value) => value + 1);
                }}
              >
                刷新
              </button>
            </div>
            <form
              className="publishing-video-input"
              onSubmit={(event) => {
                event.preventDefault();
                void selectVideo(input);
              }}
            >
              <input
                aria-label="BV 号或视频链接"
                placeholder="BV 号或视频链接"
                value={input}
                onChange={(event) => setInput(event.target.value)}
                disabled={publishingBusy}
              />
              <button className="btn" disabled={publishingBusy || !input.trim()}>
                载入稿件
              </button>
            </form>
            <div className="publishing-videos" aria-busy={loading}>
              {videos.map((item) => (
                <button
                  type="button"
                  key={item.bvid}
                  className="publishing-video"
                  aria-pressed={video?.bvid === item.bvid}
                  disabled={publishingBusy}
                  onClick={() => void selectVideo(item.bvid)}
                >
                  <img src={item.cover} alt="" loading="lazy" referrerPolicy="no-referrer" />
                  <span>
                    <strong>{item.title}</strong>
                    <span>{item.bvid}</span>
                  </span>
                </button>
              ))}
            </div>
            {(page > 1 || hasMore) && (
              <div className="publishing-pagination">
                <button
                  className="btn"
                  disabled={page === 1 || publishingBusy}
                  onClick={() => setPage((value) => value - 1)}
                >
                  上一页
                </button>
                <span>{page}</span>
                <button
                  className="btn"
                  disabled={!hasMore || publishingBusy}
                  onClick={() => setPage((value) => value + 1)}
                >
                  下一页
                </button>
              </div>
            )}
          </section>
          {video && (
            <section className="settings-panel">
              <h2>稿件信息</h2>
              <a
                href={`https://www.bilibili.com/video/${video.bvid}`}
                target="_blank"
                rel="noreferrer"
                className="publishing-original-title"
              >
                {video.title}
              </a>
              <form onSubmit={preview}>
                <fieldset
                  className="publishing-fields"
                  disabled={publishingBusy}
                  onChange={invalidate}
                >
                  <label className="field publishing-wide">
                    谱面
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="搜索曲名或 ID"
                    />
                  </label>
                  {charts.length > 0 && (
                    <div className="publishing-chart-results publishing-wide">
                      {charts.map((item) => (
                        <button
                          key={item.chart_key}
                          className="publishing-chart"
                          type="button"
                          aria-pressed={chart?.chart_key === item.chart_key}
                          onClick={() => {
                            setChart(item);
                            changeConstant(
                              item.type === 'utage' ? item.level : item.constant?.toFixed(1) || '',
                            );
                            setAchievement('');
                            setVersion('');
                            setQuery('');
                            invalidate();
                          }}
                        >
                          <ChartLabel chart={item} />
                        </button>
                      ))}
                    </div>
                  )}
                  {chart && (
                    <div className="publishing-chart publishing-wide">
                      <ChartLabel chart={chart} />
                    </div>
                  )}
                  <label className="field">
                    录像达成率
                    <input
                      value={achievement}
                      onChange={(event) => setAchievement(event.target.value)}
                      inputMode="decimal"
                      placeholder="100.0000"
                      pattern="[0-9]{1,3}(\.[0-9]{1,4})?"
                      required
                    />
                  </label>
                  <label className="field">
                    完成标记
                    <select
                      aria-label="完成标记"
                      value={combo}
                      onChange={(event) => {
                        const value = event.target.value as 'ap' | 'app';
                        manualCombo.current = { bvid: video.bvid, value };
                        setCombo(value);
                      }}
                    >
                      <option value="ap">AP</option>
                      <option value="app">AP+</option>
                    </select>
                  </label>
                  <label className="field">
                    {chart?.type === 'utage' ? '录像等级' : '录像定数'}
                    <input
                      value={constant}
                      onChange={(event) => changeConstant(event.target.value)}
                      inputMode={chart?.type === 'utage' ? 'text' : 'decimal'}
                      pattern={
                        chart?.type === 'utage' ? '[0-9]{1,2}[+]?[?]?' : '[0-9]{1,2}(\\.[0-9])?'
                      }
                      required
                    />
                  </label>
                  <label className="field">
                    封面版本
                    <select
                      aria-label="封面版本"
                      value={version}
                      onChange={(event) => setVersion(event.target.value)}
                    >
                      <option value="">{chart?.jp_release_version || '请选择版本'}</option>
                      {Object.entries(manifest?.version_map || {}).map(([key, value]) => (
                        <option key={key} value={key}>
                          {value.version_name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field publishing-wide">
                    合集
                    <select
                      aria-label="合集"
                      value={selectedSection}
                      onChange={(event) => {
                        setSection(event.target.value);
                        setAutomaticSection(false);
                      }}
                      required
                    >
                      <option value="">请选择合集</option>
                      {collections.map((collection) => (
                        <optgroup key={collection.id} label={collection.title}>
                          {collection.sections.map((item) => (
                            <option key={item.id} value={item.id}>
                              {collection.title}
                              {item.title && item.title !== collection.title
                                ? ` · ${item.title}`
                                : ''}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                  </label>
                  <label className="publishing-mirror publishing-wide">
                    <input
                      type="checkbox"
                      checked={mirror}
                      onChange={(event) => setMirror(event.target.checked)}
                    />
                    封面左右镜像
                  </label>
                  <div className="form-actions publishing-wide">
                    <button
                      className="btn"
                      disabled={!chart || publishingBusy || !achievement || !selectedSection}
                    >
                      {busy ? '处理中…' : '生成预览'}
                    </button>
                  </div>
                </fieldset>
              </form>
            </section>
          )}
          {prepared && (
            <section className="settings-panel publishing-preview" aria-busy={busy}>
              <h2>确认整理</h2>
              <dl className="publishing-title-comparison">
                <div>
                  <dt>原标题</dt>
                  <dd>{prepared.plan.old_title}</dd>
                </div>
                <div>
                  <dt>新标题</dt>
                  <dd>{prepared.plan.title}</dd>
                </div>
                <div>
                  <dt>合集</dt>
                  <dd>
                    {prepared.plan.section_title ||
                      collections
                        .flatMap((item) =>
                          item.sections.map((part) => ({
                            id: part.id,
                            title: `${item.title} · ${part.title}`,
                          })),
                        )
                        .find((item) => item.id === prepared.plan.section_id)?.title ||
                      '已选择'}
                  </dd>
                </div>
              </dl>
              <PlanOrder order={prepared.plan.order} />
              {prepared.images && (
                <div className="publishing-cover-previews">
                  <figure>
                    <img src={prepared.images.cover16} alt="16:9 投稿封面预览" />
                    <figcaption>16:9</figcaption>
                  </figure>
                  <figure>
                    <img src={prepared.images.cover43} alt="4:3 投稿封面预览" />
                    <figcaption>4:3</figcaption>
                  </figure>
                </div>
              )}
              <div className="publishing-submit">
                <button
                  type="button"
                  className="btn primary"
                  disabled={
                    publishingBusy ||
                    !prepared.images ||
                    !prepared.plan.order ||
                    prepared.plan.order.status === 'blocked' ||
                    publishingComplete(job?.state || '')
                  }
                  onClick={() => void apply()}
                >
                  {busy
                    ? '正在整理…'
                    : publishingComplete(job?.state || '')
                      ? '已整理'
                      : job
                        ? '重试整理'
                        : '确认整理'}
                </button>
                {job && !publishingComplete(job.state) && (
                  <span role="status">{publishingState(job.state)}</span>
                )}
              </div>
              {job && <PublishingSteps steps={job.steps} />}
            </section>
          )}
          {jobs.length > 0 && (
            <section className="settings-panel publishing-history">
              <h2>处理记录</h2>
              {jobs.slice(0, 10).map((item) => (
                <div key={item.job_id}>
                  <span>{item.bvid || '稿件'}</span>
                  <span>{publishingState(item.state)}</span>
                  {!publishingComplete(item.state) && item.state !== 'superseded' && (
                    <button
                      type="button"
                      className="btn"
                      disabled={publishingBusy}
                      onClick={() => void resume(item)}
                    >
                      继续整理
                    </button>
                  )}
                  {item.updated_at && <time>{formatTimestamp(item.updated_at)}</time>}
                  <PublishingSteps steps={item.steps} />
                </div>
              ))}
            </section>
          )}
        </>
      )}
    </div>
  );
}
