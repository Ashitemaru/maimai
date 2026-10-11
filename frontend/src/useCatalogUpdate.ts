import { useCallback, useEffect, useRef, useState } from 'react';
import { api, messageOf } from './api';

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
  const [error, setError] = useState('');
  const [action, setAction] = useState<Action | null>(null);
  const previousRevision = useRef<string | null>(null);
  const generation = useRef(0);
  const accept = useCallback(
    (next: CatalogUpdateState) => {
      setState(next);
      setError('');
      if (previousRevision.current !== null && previousRevision.current !== next.revision)
        onChanged();
      previousRevision.current = next.revision;
    },
    [onChanged],
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
        if (!signal?.aborted && request === generation.current) setError(messageOf(error));
      }
    },
    [accept],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);
  useEffect(() => {
    if (action) return;
    const controller = new AbortController();
    const focus = () => void load(controller.signal);
    window.addEventListener('focus', focus);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      await load(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(tick, 2000);
    };
    if (state?.main.status === 'running' || remyRunning(state)) timer = setTimeout(tick, 2000);
    return () => {
      controller.abort();
      clearTimeout(timer);
      window.removeEventListener('focus', focus);
    };
  }, [state, action, load]);
  const run = useCallback(
    async (nextAction: Action) => {
      if (action || state?.main.status === 'running') return;
      setAction(nextAction);
      setError('');
      const request = ++generation.current;
      try {
        const next = await api<CatalogUpdateState>('/catalog/' + nextAction, { method: 'POST' });
        if (request === generation.current) accept(next);
        notify(
          nextAction === 'decisions/reapply'
            ? '正在应用已保存裁决'
            : nextAction === 'remy/retry'
              ? '已开始核准版本'
              : '已开始更新曲库',
        );
      } catch (error) {
        const message = messageOf(error);
        setError(message);
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
      accept(next);
    },
    [accept],
  );
  return {
    state,
    error,
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
