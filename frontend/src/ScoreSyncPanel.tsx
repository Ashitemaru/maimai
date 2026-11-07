import { useEffect, useRef, useState, type FormEvent } from 'react';
import { messageOf } from './api';
import { formatTimestamp } from './dateTime';
import { useNotify } from './Toast';
import type { ScoreSyncControl } from './useScoreSync';
import type { Settings } from './types';

export function ScoreSyncPanel({
  control,
  settings,
  onSaveToken,
}: {
  control: ScoreSyncControl;
  settings: Settings | null;
  onSaveToken: (token: string) => Promise<void>;
}) {
  const { state, busy, loadError } = control;
  const notify = useNotify();
  const [token, setToken] = useState('');
  const [savingToken, setSavingToken] = useState(false);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  const lastSync = state?.divingfish.last_sync;
  const job = state?.job;

  async function saveToken(event: FormEvent) {
    event.preventDefault();
    if (savingToken || !token.trim()) return;
    setSavingToken(true);
    try {
      await onSaveToken(token.trim());
      if (active.current) setToken('');
      await control.reload();
    } catch (error) {
      if (active.current) notify(messageOf(error));
    } finally {
      if (active.current) setSavingToken(false);
    }
  }
  async function removeToken() {
    setSavingToken(true);
    try {
      await onSaveToken('');
      await control.reload();
    } catch (error) {
      if (active.current) notify(messageOf(error));
    } finally {
      if (active.current) setSavingToken(false);
    }
  }
  return (
    <div className="score-sync-panels">
      <section className="settings-panel" aria-labelledby="divingfish-sync-title">
        <h2 id="divingfish-sync-title">同步水鱼成绩</h2>
        {loadError && (
          <button className="btn" onClick={() => void control.reload()}>
            重试
          </button>
        )}
        {!state && !loadError && <p role="status">正在获取同步状态…</p>}
        <div className="form-actions">
          <button
            className="btn primary"
            disabled={busy || savingToken || !state?.divingfish.configured}
            onClick={() => void control.start()}
          >
            {busy ? '正在同步…' : '从水鱼同步'}
          </button>
        </div>
        {job?.state === 'running' && (
          <div className="score-sync-progress" role="status">
            <span className="spinner" />
            <span>{job.message || '正在同步…'}</span>
          </div>
        )}
        {lastSync && (
          <div className="score-sync-receipt">
            <time dateTime={lastSync.synced_at}>{formatTimestamp(lastSync.synced_at)}</time>
            <div className="score-sync-counts">
              <span>
                已同步 <strong>{lastSync.matched}</strong>
              </span>
              <span>
                核准 <strong>{lastSync.confirmed ?? 0}</strong>
              </span>
              <span>
                待核 <strong>{lastSync.pending ?? 0}</strong>
              </span>
            </div>
          </div>
        )}
        <form className="score-token-form" onSubmit={saveToken}>
          <div className="field">
            <label htmlFor="import-token">
              Import-Token{settings?.configured ? ' · 已保存' : ''}
            </label>
            <input
              id="import-token"
              type="password"
              autoComplete="off"
              autoCapitalize="none"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder={settings?.configured ? '输入新 Token 以替换' : '水鱼个人资料中的 Token'}
            />
          </div>
          <div className="form-actions">
            <button className="btn" disabled={savingToken || busy || !token.trim()}>
              {savingToken ? '保存中…' : '保存 Token'}
            </button>
            {settings?.configured && (
              <button
                type="button"
                className="btn quiet"
                disabled={savingToken || busy}
                onClick={() => void removeToken()}
              >
                移除 Token
              </button>
            )}
            <a
              href="https://maimai.diving-fish.com/"
              target="_blank"
              rel="noopener noreferrer"
              className="btn quiet"
            >
              打开水鱼 ↗
            </a>
          </div>
        </form>
      </section>
    </div>
  );
}
