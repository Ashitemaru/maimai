import { useCallback, useEffect, useRef, useState } from 'react';
import { api, messageOf } from './api';
import { useToastMessage } from './Toast';

export interface CatalogUpdateState {
  revision: string;
  decision_revision?: number;
  applied_decision_revision?: number;
  decisions_pending?: boolean;
  snapshot_at: string | null;
  main: {
    status: 'idle' | 'running' | 'succeeded' | 'failed';
    stage: string;
    message: string;
    job_id?: string;
    started_at?: string;
    finished_at?: string;
    error?: string;
    diff?: {
      songs_added: number;
      charts_added: number;
      constants_changed: number;
      songs_missing: number;
      charts_missing: number;
    };
    warnings: string[];
  };
  remy: {
    status: 'idle' | 'queued' | 'running' | 'complete' | 'pending' | 'failed';
    pending_count: number;
    verified_count: number;
    updated_count: number;
    message: string;
  };
  can_rollback: boolean;
}

type Action = 'update' | 'remy/retry' | 'decisions/reapply';
const remyRunning = (state: CatalogUpdateState | null) =>
  state?.remy.status === 'queued' || state?.remy.status === 'running';

export function useCatalogUpdate(onChanged: () => void, notify: (message: string) => void) {
  const [state, setState] = useState<CatalogUpdateState | null>(null);
  const [loadError, setLoadError] = useState('');
  useToastMessage(loadError);
  const [action, setAction] = useState<Action | null>(null);
  const previousRevision = useRef<string | null>(null);
  const previousState = useRef<CatalogUpdateState | null>(null);
  const announced = useRef(new Set<string>());
  const runs = useRef({ main: 0, remy: 0 });
  const generation = useRef(0);
  const initializing = useRef(true);
  const accept = useCallback(
    (next: CatalogUpdateState, requested?: 'main' | 'remy') => {
      const previous = previousState.current;
      if (next.main.status === 'running' && previous?.main.status !== 'running')
        runs.current.main++;
      if (remyRunning(next) && !remyRunning(previous)) runs.current.remy++;
      const mainFinished =
        (previous?.main.status === 'running' || requested === 'main') &&
        (next.main.status === 'succeeded' || next.main.status === 'failed');
      const remyFinished = (remyRunning(previous) || requested === 'remy') && !remyRunning(next);
      const messages = mainFinished
        ? [
            next.main.status === 'failed'
              ? next.main.error || next.main.message || '曲库更新失败，请重试'
              : next.main.message || '曲库已更新',
            ...(next.main.warnings || []),
          ]
        : [];
      const notices = [
        ...[...new Set(messages)].map((message) => ({
          key: `main:${next.main.job_id || next.main.started_at || runs.current.main}`,
          message,
        })),
        {
          key: `remy:${runs.current.remy}`,
          message: remyFinished
            ? next.remy.message ||
              (next.remy.status === 'failed' ? '版本核准失败，请重试' : '版本核准完成')
            : '',
        },
      ];
      const pending = notices.filter(({ key, message }) => {
        if (!message || (!requested && announced.current.has(`${key}:${message}`))) return false;
        announced.current.add(`${key}:${message}`);
        return true;
      });
      if (pending.length) notify(pending.map(({ message }) => message).join('\n'));
      previousState.current = next;
      setState(next);
      setLoadError('');
      if (previousRevision.current !== null && previousRevision.current !== next.revision)
        onChanged();
      previousRevision.current = next.revision;
    },
    [onChanged, notify],
  );
  const load = useCallback(
    async (signal?: AbortSignal) => {
      const request = ++generation.current;
      try {
        const next = await api<CatalogUpdateState>('/catalog/update', {
          signal,
          cache: 'no-store',
        });
        if (!signal?.aborted && request === generation.current) accept(next);
      } catch (error) {
        if (!signal?.aborted && request === generation.current) setLoadError(messageOf(error));
      }
    },
    [accept],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal).finally(() => {
      if (!controller.signal.aborted) initializing.current = false;
    });
    return () => controller.abort();
  }, [load]);
  useEffect(() => {
    if (action) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const resume = () => {
      clearTimeout(timer);
      if (document.visibilityState !== 'visible' || initializing.current) return;
      timer = setTimeout(() => void load(controller.signal), 180);
    };
    window.addEventListener('focus', resume);
    document.addEventListener('visibilitychange', resume);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener('focus', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, [action, load]);
  useEffect(() => {
    if (action) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      await load(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(tick, 2000);
    };
    if (state?.main.status === 'running' || remyRunning(state)) timer = setTimeout(tick, 2000);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [state, action, load]);
  const run = useCallback(
    async (nextAction: Action) => {
      if (action || state?.main.status === 'running') return;
      setAction(nextAction);
      const request = ++generation.current;
      try {
        const next = await api<CatalogUpdateState>('/catalog/' + nextAction, { method: 'POST' });
        if (request === generation.current)
          accept(next, nextAction === 'remy/retry' ? 'remy' : 'main');
        if (nextAction === 'remy/retry' ? remyRunning(next) : next.main.status === 'running')
          notify(
            nextAction === 'decisions/reapply'
              ? '正在应用已保存裁决'
              : nextAction === 'remy/retry'
                ? '已开始核准版本'
                : '已开始更新曲库',
          );
      } catch (error) {
        const message = messageOf(error);
        notify(message);
      } finally {
        setAction(null);
      }
    },
    [action, state?.main.status, accept, notify],
  );
  const receive = useCallback(
    (next: CatalogUpdateState) => {
      generation.current += 1;
      accept(next, 'main');
    },
    [accept],
  );
  return {
    state,
    action,
    run,
    reload: load,
    receive,
    mainBusy:
      action === 'update' || action === 'decisions/reapply' || state?.main.status === 'running',
    remyBusy: action === 'remy/retry' || remyRunning(state),
  };
}

export type CatalogUpdateControl = ReturnType<typeof useCatalogUpdate>;
