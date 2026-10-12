import { useCallback, useEffect, useRef, useState } from 'react';
import { api, messageOf } from './api';
import type { ScoreSyncJob, ScoreSyncStatus } from './scoreSync';

export function useScoreSync(onChanged: () => Promise<void>, notify: (message: string) => void) {
  const [state, setState] = useState<ScoreSyncStatus | null>(null);
  const [loadError, setLoadError] = useState('');
  const [action, setAction] = useState<ScoreSyncJob['kind'] | null>(null);
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
        else if (job.kind === 'wahlap') {
          const result = job.result;
          const notice = result?.source === 'wahlap' ? result.notice : null;
          void onChanged().then(
            () => notify(['华立成绩已同步，待核记录已核对', notice].filter(Boolean).join('；')),
            (error) =>
              notify(
                ['华立成绩已保存，页面刷新失败：' + messageOf(error), notice]
                  .filter(Boolean)
                  .join('；'),
              ),
          );
        } else {
          const result = job.result;
          if (result?.source === 'divingfish_upload')
            notify(
              `已上传 ${result.uploaded_count ?? result.records ?? 0} 条${result.skipped_count ? ` · 跳过 ${result.skipped_count} 条` : ''}`,
            );
          else notify('已上传水鱼');
        }
      }
    },
    [onChanged, notify],
  );
  const reload = useCallback(
    async (signal?: AbortSignal) => {
      const sequence = ++generation.current;
      try {
        const next = await api<ScoreSyncStatus>('/sync/status', {
          signal,
          cache: 'no-store',
        });
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
    if (state?.job?.state !== 'running' || action) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await reload(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(poll, 1800);
    };
    timer = setTimeout(poll, 800);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [state?.job?.id, state?.job?.state, action, reload]);

  const run = useCallback(
    async (kind: ScoreSyncJob['kind'], qrText?: string) => {
      if (writing.current || current.current?.job?.state === 'running') return false;
      writing.current = true;
      setAction(kind);
      generation.current += 1;
      let submitted = false;
      try {
        let status = current.current;
        if (kind === 'wahlap') {
          // The server's current window wins over a tab left open overnight.
          status = await api<ScoreSyncStatus>('/sync/status', { cache: 'no-store' });
          if (!active.current) return false;
          lastLoadError.current = '';
          setLoadError('');
          accept(status);
          if (status.job?.state === 'running') return false;
        }
        let prerequisite = '';
        if (!status) prerequisite = '同步状态尚未取得，请重试';
        else if (kind === 'wahlap') {
          if (status.wahlap.maintenance?.active)
            prerequisite =
              status.wahlap.maintenance.message || '华立每日04:00–07:00维护，请07:00后重试';
          else if (!status.wahlap.available)
            prerequisite = status.wahlap.reason || '华立同步暂不可用';
          else if (!qrText?.trim()) prerequisite = '请选择二维码截图或粘贴二维码内容';
        } else if (!status.divingfish.configured) prerequisite = '请先保存水鱼 Token';
        else if (!status.wahlap.has_snapshot) prerequisite = '请先完成一次华立同步';
        if (prerequisite) {
          notify(prerequisite);
          return false;
        }
        submitted = true;
        const job = await api<ScoreSyncJob>(
          kind === 'wahlap' ? '/sync/wahlap' : '/sync/divingfish/upload',
          {
            method: 'POST',
            ...(kind === 'wahlap' ? { body: JSON.stringify({ qr_text: qrText }) } : {}),
          },
        );
        observedJobs.current.add(job.id);
        if (current.current) accept({ ...current.current, job });
        await reload();
        return true;
      } catch (error) {
        if (active.current) {
          notify(messageOf(error));
        }
        // A lost response does not imply that the server stopped the job.
        if (submitted) await reload();
        return false;
      } finally {
        writing.current = false;
        if (active.current) setAction(null);
      }
    },
    [accept, reload, notify],
  );
  return {
    state,
    loadError,
    action,
    busy: action !== null || state?.job?.state === 'running',
    reload,
    start: (qrText: string) => run('wahlap', qrText),
    upload: () => run('divingfish_upload'),
  };
}

export type ScoreSyncControl = ReturnType<typeof useScoreSync>;
