import { useCallback, useEffect, useRef, useState } from 'react';
import { api, messageOf } from './api';
import type { ScoreSyncJob, ScoreSyncStatus } from './scoreSync';

export function useScoreSync(onChanged: () => Promise<void>, notify: (message: string) => void) {
  const [state, setState] = useState<ScoreSyncStatus | null>(null);
  const [loadError, setLoadError] = useState('');
  const [starting, setStarting] = useState(false);
  const current = useRef<ScoreSyncStatus | null>(null);
  const active = useRef(true);
  const writing = useRef(false);
  const generation = useRef(0);
  const lastLoadError = useRef('');
  const observedJobs = useRef(new Set<string>());
  const completedJobs = useRef(new Set<string>());

  const accept = useCallback(
    (next: ScoreSyncStatus) => {
      if (!active.current) return;
      current.current = next;
      setState(next);
      const job = next.job;
      if (!job) return;
      if (job.state === 'running') observedJobs.current.add(job.id);
      else if (observedJobs.current.has(job.id) && !completedJobs.current.has(job.id)) {
        completedJobs.current.add(job.id);
        if (job.state === 'failed') notify(job.error?.message || '同步未完成，请重试');
        else
          void onChanged().then(
            () => notify('水鱼成绩已同步，待核记录已核对'),
            (error) => notify('水鱼成绩已保存，页面刷新失败：' + messageOf(error)),
          );
      }
    },
    [onChanged, notify],
  );
  const reload = useCallback(
    async (signal?: AbortSignal) => {
      const sequence = ++generation.current;
      try {
        const next = await api<ScoreSyncStatus>('/sync/status', { signal, cache: 'no-store' });
        if (!signal?.aborted && sequence === generation.current && active.current) {
          lastLoadError.current = '';
          setLoadError('');
          accept(next);
        }
      } catch (error) {
        if (!signal?.aborted && sequence === generation.current && active.current) {
          const message = messageOf(error);
          setLoadError(message);
          if (message !== lastLoadError.current) notify(message);
          lastLoadError.current = message;
        }
      }
    },
    [accept, notify],
  );
  useEffect(() => {
    active.current = true;
    const controller = new AbortController();
    void reload(controller.signal);
    return () => {
      active.current = false;
      controller.abort();
    };
  }, [reload]);
  useEffect(() => {
    const controller = new AbortController();
    const resume = () => {
      if (document.visibilityState === 'visible') void reload(controller.signal);
    };
    window.addEventListener('focus', resume);
    document.addEventListener('visibilitychange', resume);
    return () => {
      controller.abort();
      window.removeEventListener('focus', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, [reload]);
  useEffect(() => {
    if (state?.job?.state !== 'running' || starting) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await reload(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(poll, 1500);
    };
    timer = setTimeout(poll, 800);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [state?.job?.id, state?.job?.state, starting, reload]);

  const start = useCallback(async () => {
    if (writing.current || current.current?.job?.state === 'running') return false;
    if (!current.current) {
      notify('同步状态尚未取得，请重试');
      return false;
    }
    if (!current.current.divingfish.configured) {
      notify('请先保存水鱼 Token');
      return false;
    }
    writing.current = true;
    setStarting(true);
    generation.current += 1;
    let submitted = false;
    try {
      submitted = true;
      const job = await api<ScoreSyncJob>('/sync/divingfish', { method: 'POST' });
      observedJobs.current.add(job.id);
      if (current.current) accept({ ...current.current, job });
      await reload();
      return true;
    } catch (error) {
      if (active.current) notify(messageOf(error));
      // A lost response does not imply that the server stopped the job.
      if (submitted) await reload();
      return false;
    } finally {
      writing.current = false;
      if (active.current) setStarting(false);
    }
  }, [accept, reload, notify]);
  return {
    state,
    loadError,
    busy: starting || state?.job?.state === 'running',
    reload,
    start,
  };
}

export type ScoreSyncControl = ReturnType<typeof useScoreSync>;
