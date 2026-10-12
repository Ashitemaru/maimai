import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import App from './App';
import { api, ApiError } from './api';
import { advanceAuthGeneration, AUTH_EXPIRED_EVENT } from './auth';
import type { AuthSession } from './auth';
import { useNotify, useToastMessage } from './Toast';

type GateState = 'checking' | 'locked' | 'ready' | 'unavailable' | 'logging-out';

function EyeIcon({ visible }: { visible: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z" />
      <circle cx="12" cy="12" r="3" />
      {visible && <path d="m3 3 18 18" />}
    </svg>
  );
}

export default function AuthGate() {
  const [state, setState] = useState<GateState>('checking');
  const [authRequired, setAuthRequired] = useState(true);
  const [masterKey, setMasterKey] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const notify = useNotify();
  useToastMessage(error);
  const request = useRef<AbortController | null>(null);
  const keyInput = useRef<HTMLInputElement>(null);

  const acceptSession = useCallback((session: AuthSession) => {
    if (typeof session.authenticated !== 'boolean' || typeof session.auth_required !== 'boolean')
      throw new Error('Invalid session response');
    advanceAuthGeneration();
    setAuthRequired(session.auth_required);
    setState(session.authenticated || !session.auth_required ? 'ready' : 'locked');
  }, []);

  const checkSession = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState('checking');
    setError('');
    try {
      const session = await api<AuthSession>('/auth/session', {
        signal: controller.signal,
        cache: 'no-store',
      });
      if (!controller.signal.aborted) acceptSession(session);
    } catch {
      if (!controller.signal.aborted) {
        setState('unavailable');
        notify('暂时无法连接，请检查网络后重试。');
      }
    }
  }, [acceptSession, notify]);

  useEffect(() => {
    void checkSession();
    function expired() {
      request.current?.abort();
      setMasterKey('');
      setVisible(false);
      setBusy(false);
      setError('登录已过期，请重新输入 Master Key。');
      setState('locked');
    }
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    return () => {
      request.current?.abort();
      window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
    };
  }, [checkSession]);

  async function login(event: FormEvent) {
    event.preventDefault();
    if (!masterKey || busy) return;
    setBusy(true);
    setError('');
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      const session = await api<AuthSession>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ master_key: masterKey }),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      acceptSession(session);
      setMasterKey('');
      setVisible(false);
    } catch (error) {
      if (controller.signal.aborted) return;
      setError(
        error instanceof ApiError && error.status === 401
          ? 'Master Key 不正确，请再试一次。'
          : error instanceof ApiError && error.status === 429
            ? '尝试次数较多，请稍后再试。'
            : '暂时无法登录，请检查网络后重试。',
      );
      keyInput.current?.focus();
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function logout() {
    advanceAuthGeneration();
    setState('logging-out');
    setError('');
    setMasterKey('');
    setVisible(false);
    try {
      await api<AuthSession>('/auth/logout', { method: 'POST' });
      setState('locked');
    } catch {
      setState('ready');
      setError('退出未完成，请检查网络后重试。');
    }
  }

  if (state === 'ready') return <App onLogout={authRequired ? () => void logout() : undefined} />;

  const loading = state === 'checking' || state === 'logging-out';
  return (
    <main className="auth-page">
      <section className="auth-card" aria-labelledby="auth-title">
        <div className="brand auth-brand">
          <img src="/icon.svg" alt="" />
          <div>
            <div className="brand-name">
              mai<span> journal</span>
            </div>
            <small>PERSONAL PLAY RECORD</small>
          </div>
        </div>
        <div className="auth-heading">
          <div className="auth-motif" aria-hidden="true">
            ✦
          </div>
          <h1 id="auth-title">欢迎回来</h1>
          <p>你的舞萌手账，每一次 AP 都值得记录。</p>
        </div>
        {loading ? (
          <div className="auth-status" role="status">
            <span className="spinner" aria-hidden="true" />
            {state === 'logging-out' ? '正在退出…' : '正在打开手账…'}
          </div>
        ) : state === 'unavailable' ? (
          <div className="auth-retry">
            <button className="btn primary auth-submit" onClick={() => void checkSession()}>
              重新连接
            </button>
          </div>
        ) : (
          <form className="auth-form" onSubmit={(event) => void login(event)}>
            <label htmlFor="master-key">Master Key</label>
            <div className="auth-key-field">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                aria-hidden="true"
              >
                <rect x="5" y="10" width="14" height="11" rx="3" />
                <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" />
              </svg>
              <input
                ref={keyInput}
                id="master-key"
                name="master-key"
                type={visible ? 'text' : 'password'}
                value={masterKey}
                autoComplete="current-password"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                placeholder="输入你的密钥"
                required
                disabled={busy}
                aria-invalid={Boolean(error)}
                aria-describedby={error ? 'toast' : undefined}
                onChange={(event) => {
                  setMasterKey(event.target.value);
                  setError('');
                }}
              />
              <button
                className="auth-visibility"
                type="button"
                aria-label={visible ? '隐藏密钥' : '显示密钥'}
                aria-pressed={visible}
                onClick={() => setVisible((value) => !value)}
              >
                <EyeIcon visible={visible} />
              </button>
            </div>
            <button className="btn primary auth-submit" type="submit" disabled={busy || !masterKey}>
              {busy ? (
                <>
                  <span className="spinner" aria-hidden="true" />
                  正在进入…
                </>
              ) : (
                <>
                  进入手账<span aria-hidden="true">→</span>
                </>
              )}
            </button>
          </form>
        )}
        <div className="auth-footer">Every perfect counts.</div>
      </section>
    </main>
  );
}
