import { useEffect, useRef, useState, type FormEvent } from 'react';
import { messageOf } from './api';
import { formatTimestamp } from './dateTime';
import { readQrImage } from './readQrImage';
import { useNotify } from './Toast';
import type { ScoreSyncControl } from './useScoreSync';
import type { Settings } from './types';
import { uploadSummary } from './scoreSync';

const stages: Record<string, string> = {
  starting: '正在开始同步…',
  fetching: '正在读取华立成绩…',
  validating: '正在核对成绩和待核记录…',
  uploading: '正在上传水鱼…',
  verifying: '正在读回核对…',
  completed: '已完成',
  failed: '未完成',
};

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
  const [qrText, setQrText] = useState('');
  const [reading, setReading] = useState(false);
  const [token, setToken] = useState('');
  const [savingToken, setSavingToken] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const active = useRef(true);
  const imageRequest = useRef(0);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      imageRequest.current += 1;
    };
  }, []);
  const uploading =
    control.action === 'divingfish_upload' ||
    (state?.job?.state === 'running' && state.job.kind === 'divingfish_upload');
  const lastSync = state?.wahlap.last_sync;
  const lastUpload = state?.divingfish.last_upload;
  const job = state?.job;

  async function readImage(file: File) {
    const request = ++imageRequest.current;
    setReading(true);
    setQrText('');
    try {
      const value = await readQrImage(file);
      if (value.length > 4096) throw new Error('二维码内容过长，请选择游戏二维码');
      if (active.current && request === imageRequest.current) {
        setQrText(value);
        notify('二维码已识别');
      }
    } catch (error) {
      if (active.current && request === imageRequest.current) notify(messageOf(error));
    } finally {
      if (active.current && request === imageRequest.current) setReading(false);
    }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || reading) return;
    const value = qrText.trim();
    setQrText('');
    void control.start(value);
  }
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
      <section className="settings-panel" aria-labelledby="wahlap-sync-title">
        <h2 id="wahlap-sync-title">同步游戏成绩</h2>
        {loadError && (
          <button className="btn" onClick={() => void control.reload()}>
            重试
          </button>
        )}
        {!state && !loadError && <p role="status">正在获取同步状态…</p>}
        <form className="qr-sync-form" onSubmit={submit}>
          <div className="qr-image-row">
            <input
              ref={fileInput}
              className="sr-only"
              type="file"
              accept="image/*"
              aria-label="二维码截图"
              tabIndex={-1}
              disabled={busy || reading}
              onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                event.currentTarget.value = '';
                if (file) void readImage(file);
              }}
            />
            <button
              className="btn"
              type="button"
              disabled={busy || reading}
              onClick={() => fileInput.current?.click()}
            >
              {reading ? '正在识别…' : '选择二维码截图'}
            </button>
          </div>
          <div className="field">
            <label htmlFor="wahlap-qr-text">或粘贴二维码内容</label>
            <input
              id="wahlap-qr-text"
              type="password"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={4096}
              disabled={busy || reading}
              value={qrText}
              onChange={(event) => {
                setQrText(event.target.value);
              }}
            />
          </div>
          <div className="form-actions">
            <button className="btn primary" disabled={busy || reading}>
              {control.action === 'wahlap' || (job?.state === 'running' && !uploading)
                ? '正在同步…'
                : '同步华立成绩'}
            </button>
            <button
              className="btn"
              type="button"
              disabled={busy || reading}
              onClick={() => void control.probe()}
            >
              {control.action === 'wahlap_probe' || state?.wahlap.checking
                ? '正在检查…'
                : '检查连接'}
            </button>
          </div>
        </form>
        {job?.state === 'running' && job.kind === 'wahlap' && (
          <div className="score-sync-progress" role="status">
            <span className="spinner" />
            <span>{job.message || stages[job.stage] || '正在处理…'}</span>
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
      </section>
      <section className="settings-panel" aria-labelledby="divingfish-upload-title">
        <h2 id="divingfish-upload-title">上传水鱼</h2>
        <div className="form-actions">
          <button
            className="btn primary"
            disabled={busy || savingToken}
            onClick={() => void control.upload()}
          >
            {uploading ? '上传中…' : '上传到水鱼'}
          </button>
          {lastUpload?.ok && (
            <time dateTime={lastUpload.uploaded_at}>{formatTimestamp(lastUpload.uploaded_at)}</time>
          )}
        </div>
        {uploading && (
          <p role="status">
            {job?.kind === 'divingfish_upload' ? job.message || '正在上传水鱼…' : '正在开始上传…'}
          </p>
        )}
        {lastUpload?.ok && <p className="score-upload-receipt">{uploadSummary(lastUpload)}</p>}
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
