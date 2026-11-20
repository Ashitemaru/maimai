import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, messageOf } from './api';
import type { APRecords, Chart, Page, Preferences, Settings, Venue } from './types';
import { PendingRecords } from './PendingRecords';
import { CompletionMatrix } from './CompletionMatrix';
import { VenuesPage } from './VenuesPage';
import { PreferencesStatus } from './Preferences';
import { CoverPage } from './CoverWorkshop';
import type { CoverState } from './CoverWorkshop';
import { useCatalogUpdate } from './useCatalogUpdate';
import { useScoreSync } from './useScoreSync';
import { useNotify, useToastMessage } from './Toast';
import { APPage } from './APPage';
import { ChartPicker, Detail } from './Detail';
import { SettingsPage } from './SettingsPage';
import { Heading, Icon } from './ui';

const PublishingPage = lazy(() =>
  import('./PublishingPage').then((module) => ({ default: module.PublishingPage })),
);

const pageLabels: Record<Page, string> = {
  ap: 'AP 图鉴',
  statistics: '完成统计',
  pending: '待核记录',
  cover: '生成封面',
  publishing: '投稿整理',
  venues: '我的机厅',
  settings: '数据与同步',
};
const primaryNavigation = [
  { page: 'ap', label: 'AP 图鉴', mobileLabel: '图鉴', icon: 'grid' },
  { page: 'venues', label: '我的机厅', mobileLabel: '机厅', icon: 'pin' },
  { page: 'publishing', label: '投稿整理', mobileLabel: '投稿', icon: 'sync' },
  { page: 'settings', label: '设置', mobileLabel: '设置', icon: 'settings' },
] as const;
const collectionNavigation = [
  { page: 'ap', label: '谱面图鉴' },
  { page: 'statistics', label: '完成统计' },
  { page: 'pending', label: '待核记录' },
] as const;

export default function App({ onLogout }: { onLogout?: () => void }) {
  const initialPage = location.hash.slice(1) as Page;
  const [page, setPage] = useState<Page>(
    Object.hasOwn(pageLabels, initialPage) ? initialPage : 'ap',
  );
  const [preferences, setPreferences] = useState<Preferences | null>(null);
  const [preferencesError, setPreferencesError] = useState('');
  const [preferencesRetry, setPreferencesRetry] = useState(0);
  const [venues, setVenues] = useState<Venue[]>([]);
  const [records, setRecords] = useState<APRecords>({ items: [], count: 0 });
  const refreshSequence = useRef(0);
  const refreshFlight = useRef<Promise<void> | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const lastVersion = useRef<string | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [detail, setDetail] = useState<Chart | null>(null);
  const detailRequest = useRef<{ controller: AbortController; sequence: number } | null>(null);
  const detailSequence = useRef(0);
  const [picker, setPicker] = useState<'record' | 'cover' | null>(null);
  const [cover, setCover] = useState<CoverState>({
    chart: null,
    score: '',
    combo: 'none',
    version: '',
    aspect: '16:9',
  });
  const [catalogRevision, setCatalogRevision] = useState(0);
  const notify = useNotify();
  useToastMessage(error);
  const closeModal = useCallback(() => {
    detailRequest.current?.controller.abort();
    detailRequest.current = null;
    detailSequence.current += 1;
    setDetail(null);
    setPicker(null);
  }, []);
  useEffect(() => () => detailRequest.current?.controller.abort(), []);
  const catalogChanged = useCallback(() => {
    setRevision((value) => value + 1);
    setCatalogRevision((value) => value + 1);
  }, []);
  const catalog = useCatalogUpdate(catalogChanged, notify);
  useEffect(() => {
    const change = () => {
      const p = location.hash.slice(1) as Page;
      if (Object.hasOwn(pageLabels, p)) {
        setPage(p);
        closeModal();
      }
    };
    window.addEventListener('hashchange', change);
    return () => window.removeEventListener('hashchange', change);
  }, [closeModal]);
  const refresh = useCallback((invalidate = true) => {
    const sequence = ++refreshSequence.current;
    const request = Promise.all([
      api<{ items: Venue[] }>('/venues'),
      api<Settings>('/settings/divingfish'),
      api<APRecords>('/ap-records'),
    ]).then(([nextVenues, nextSettings, nextRecords]) => {
      if (sequence !== refreshSequence.current) return;
      setRecords(nextRecords);
      setVenues(nextVenues.items);
      setSettings(nextSettings);
      if (invalidate) setRevision((value) => value + 1);
      setError('');
    });
    refreshFlight.current = request;
    return request.finally(() => {
      if (refreshFlight.current === request) refreshFlight.current = null;
    });
  }, []);
  const scoreSync = useScoreSync(refresh, notify);
  useEffect(() => {
    void refresh(false).catch((error) => setError(messageOf(error)));
  }, [refresh]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let sequence = 0;
    const update = () => {
      const request = ++sequence;
      clearTimeout(timer);
      if (document.visibilityState !== 'visible') return;
      timer = setTimeout(async () => {
        // A request started before the app regained focus may contain old device data.
        await refreshFlight.current?.catch(() => undefined);
        if (request !== sequence || document.visibilityState !== 'visible') return;
        try {
          // An unchanged version token means no catalog or personal write since the last refresh.
          const token = await api<{ token: string }>('/version').then(
            (value) => value.token,
            () => null,
          );
          if (request !== sequence || document.visibilityState !== 'visible') return;
          if (token !== null && token === lastVersion.current) return;
          await refresh();
          if (token !== null) lastVersion.current = token;
        } catch (error) {
          if (request === sequence) notify(messageOf(error));
        }
      }, 180);
    };
    window.addEventListener('focus', update);
    document.addEventListener('visibilitychange', update);
    return () => {
      sequence += 1;
      clearTimeout(timer);
      window.removeEventListener('focus', update);
      document.removeEventListener('visibilitychange', update);
    };
  }, [refresh, notify]);
  useEffect(() => {
    const controller = new AbortController();
    setPreferencesError('');
    api<Preferences>('/settings/preferences', { signal: controller.signal, cache: 'no-store' })
      .then((value) => {
        if (controller.signal.aborted) return;
        setPreferences(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setPreferencesError(messageOf(error));
      });
    return () => controller.abort();
  }, [preferencesRetry]);
  const coverChartKey = cover.chart?.chart_key;
  useEffect(() => {
    if (!catalogRevision || !coverChartKey) return;
    const controller = new AbortController();
    api<Chart>('/charts/' + encodeURIComponent(coverChartKey), {
      signal: controller.signal,
      cache: 'no-store',
    })
      .then((chart) =>
        setCover((value) =>
          value.chart?.chart_key === chart.chart_key ? { ...value, chart } : value,
        ),
      )
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404)
          setCover((value) =>
            value.chart?.chart_key === coverChartKey ? { ...value, chart: null } : value,
          );
        notify(messageOf(error));
      });
    return () => controller.abort();
  }, [catalogRevision, coverChartKey, notify]);
  async function saveToken(token: string) {
    const result = await api<Settings>('/settings/divingfish', {
      method: 'PUT',
      body: JSON.stringify({ token }),
    });
    setSettings(result);
    notify(token ? '已保存水鱼 Token' : '已移除水鱼 Token');
  }
  async function savePreferences(value: Preferences) {
    const saved = await api<Preferences>('/settings/preferences', {
      method: 'PUT',
      body: JSON.stringify(value),
    });
    setPreferences(saved);
  }
  async function openDetail(c: Pick<Chart, 'chart_key'>, preserveExisting = false) {
    detailRequest.current?.controller.abort();
    const controller = new AbortController();
    const sequence = ++detailSequence.current;
    detailRequest.current = { controller, sequence };
    const requestedPage = location.hash;
    if (!preserveExisting) setDetail(null);
    try {
      const fresh = await api<Chart>('/charts/' + encodeURIComponent(c.chart_key), {
        signal: controller.signal,
        cache: 'no-store',
      });
      if (
        controller.signal.aborted ||
        detailSequence.current !== sequence ||
        location.hash !== requestedPage
      )
        return;
      setPicker(null);
      setDetail(fresh);
    } catch (error) {
      if (!controller.signal.aborted && detailSequence.current === sequence)
        notify(messageOf(error));
    } finally {
      if (detailRequest.current?.sequence === sequence) detailRequest.current = null;
    }
  }
  function chooseCover(chart: Chart) {
    const achievement = chart.provider_achievement;
    const savedCombo = chart.is_ap ? (chart.status === 'app' ? 'app' : 'ap') : 'none';
    setCover((value) => ({
      ...value,
      chart,
      score: achievement == null ? '' : achievement.toFixed(4),
      combo:
        chart.provider_fc != null || chart.provider_achievement != null
          ? chart.provider_fc === 'app'
            ? 'app'
            : chart.provider_fc === 'ap'
              ? 'ap'
              : 'none'
          : savedCombo,
      version: '',
    }));
    closeModal();
    location.hash = 'cover';
    setPage('cover');
  }
  const retryPreferences = () => setPreferencesRetry((value) => value + 1);
  function openPicker(mode: 'record' | 'cover') {
    closeModal();
    setPicker(mode);
  }
  const isCollectionPage = collectionNavigation.some((item) => item.page === page);
  const activePrimaryPage = isCollectionPage ? 'ap' : page === 'cover' ? 'publishing' : page;
  return (
    <>
      <div className="shell">
        <aside className="sidebar">
          <a className="brand" href="#ap">
            <img src="/icon.svg" alt="" />
            <div>
              <div className="brand-name">
                mai<span> journal</span>
              </div>
              <small>PERSONAL PLAY RECORD</small>
            </div>
          </a>
          <nav className="primary-navigation" aria-label="主导航">
            {primaryNavigation.map(({ page: destination, label, mobileLabel, icon }) => (
              <a
                key={destination}
                href={'#' + destination}
                className={'nav-item ' + (activePrimaryPage === destination ? 'active' : '')}
                aria-label={label}
                aria-current={activePrimaryPage === destination ? 'page' : undefined}
              >
                <Icon name={icon} />
                <span className="nav-full-label" aria-hidden="true">
                  {label}
                </span>
                <span className="nav-mobile-label" aria-hidden="true">
                  {mobileLabel}
                </span>
              </a>
            ))}
          </nav>
          <div className="sidebar-bottom">
            <div className="profile">
              <div className="avatar">AS</div>
              <div>
                <strong>Ashitemaru</strong>
                <small>Every perfect counts.</small>
              </div>
            </div>
          </div>
        </aside>
        <main className="main">
          <div className="topbar">
            <div className="mobile-brand">
              <img src="/icon.svg" alt="" />
              mai journal
            </div>
            <div className="breadcrumb">
              我的空间 <span>/</span>
              <strong>{pageLabels[page]}</strong>
            </div>
            <div className="topbar-right">
              <span>maimai でらっくす</span>
              {onLogout && (
                <button className="btn quiet auth-logout" onClick={onLogout}>
                  退出
                </button>
              )}
              <span className="pill">
                <span className="dot" />
                国服曲库
              </span>
            </div>
          </div>
          {isCollectionPage && (
            <nav className="section-nav" aria-label="AP 图鉴页面">
              {collectionNavigation.map(({ page: destination, label }) => (
                <a
                  key={destination}
                  href={'#' + destination}
                  className="section-nav-link"
                  aria-current={page === destination ? 'page' : undefined}
                >
                  {label}
                  {destination === 'pending' && records.count > 0 && (
                    <span className="section-nav-count">{records.count}</span>
                  )}
                </a>
              ))}
            </nav>
          )}
          {error && (
            <div className="empty-state">
              <button
                className="btn"
                onClick={() => {
                  setError('');
                  void refresh().catch((error) => setError(messageOf(error)));
                }}
              >
                重试
              </button>
            </div>
          )}
          <APPage
            active={!error && page === 'ap'}
            revision={revision}
            catalogRevision={catalogRevision}
            preferences={preferences}
            preferencesError={preferencesError}
            onRetryPreferences={retryPreferences}
            onSelect={(chart) => void openDetail(chart)}
            onRecord={() => openPicker('record')}
            notify={notify}
          />
          {!error && (
            <>
              {page === 'statistics' && (
                <>
                  <Heading
                    eyebrow="YOUR COMPLETION AT A GLANCE"
                    title="完成统计"
                    actions={
                      <>
                        <a className="btn sync-button" href="#settings">
                          <Icon name="sync" />
                          同步成绩
                        </a>
                      </>
                    }
                  />
                  {preferences ? (
                    <CompletionMatrix
                      revision={revision}
                      defaultRange={[
                        preferences.default_constant_min,
                        preferences.default_constant_max,
                      ]}
                      onSelect={(chart) => void openDetail(chart)}
                    />
                  ) : (
                    <PreferencesStatus error={preferencesError} onRetry={retryPreferences} />
                  )}
                </>
              )}
              {page === 'pending' && (
                <>
                  <Heading
                    eyebrow="WAITING FOR YOUR NEXT SYNC"
                    title="待核记录"
                    actions={
                      <a className="btn primary" href="#settings">
                        <Icon name="sync" />
                        同步成绩
                      </a>
                    }
                  />
                  <PendingRecords
                    records={records}
                    onSelect={(record) => void openDetail(record)}
                    onChanged={refresh}
                    notify={notify}
                  />
                </>
              )}
              {page === 'cover' && (
                <CoverPage
                  value={cover}
                  onChange={setCover}
                  onPick={() => openPicker('cover')}
                  notify={notify}
                />
              )}
              {page === 'publishing' && (
                <Suspense fallback={<div className="empty-state">加载投稿整理…</div>}>
                  <PublishingPage
                    onOpenCover={() => {
                      location.hash = 'cover';
                    }}
                  />
                </Suspense>
              )}
              {page === 'venues' && (
                <>
                  <Heading eyebrow="PLACES & PERFECTS" title="我的机厅" />
                  <VenuesPage
                    revision={revision}
                    onChanged={refresh}
                    onSelect={(chart) => void openDetail(chart)}
                    notify={notify}
                  />
                </>
              )}
              {page === 'settings' && (
                <SettingsPage
                  settings={settings}
                  scoreSync={scoreSync}
                  onSave={saveToken}
                  catalog={catalog}
                  preferences={preferences}
                  preferencesError={preferencesError}
                  onRetryPreferences={retryPreferences}
                  onSavePreferences={savePreferences}
                />
              )}
            </>
          )}
        </main>
      </div>
      {detail && (
        <Detail
          key={`${detail.chart_key}:${detail.record_revision ?? 0}`}
          chart={detail}
          venues={venues}
          onClose={closeModal}
          onSaved={refresh}
          onReload={() => openDetail(detail, true)}
          notify={notify}
        />
      )}
      {picker && (
        <ChartPicker
          mode={picker}
          revision={catalogRevision}
          onClose={closeModal}
          onPick={(chart) => (picker === 'cover' ? chooseCover(chart) : void openDetail(chart))}
        />
      )}
    </>
  );
}
