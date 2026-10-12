import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { messageOf } from './api';
import type { Preferences } from './types';
import { useNotify, useToastMessage } from './Toast';

export function PreferencesStatus({ error, onRetry }: { error: string; onRetry: () => void }) {
  useToastMessage(error ? `偏好设置加载失败：${error}` : null);
  return error ? (
    <div className="preferences-load-error">
      <button type="button" className="btn" onClick={onRetry}>
        重试
      </button>
    </div>
  ) : (
    <div className="empty-state" role="status">
      加载偏好设置…
    </div>
  );
}

export function PreferencesPanel({
  preferences,
  loadError,
  onRetry,
  onSave,
}: {
  preferences: Preferences | null;
  loadError: string;
  onRetry: () => void;
  onSave: (value: Preferences) => Promise<void>;
}) {
  const [minimum, setMinimum] = useState('');
  const [maximum, setMaximum] = useState('');
  const [saving, setSaving] = useState(false);
  const notify = useNotify();
  useEffect(() => {
    if (!preferences) return;
    setMinimum(preferences.default_constant_min.toFixed(1));
    setMaximum(preferences.default_constant_max.toFixed(1));
  }, [preferences]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    const lower = Number(minimum),
      upper = Number(maximum);
    if (!minimum.trim() || !maximum.trim() || !Number.isFinite(lower) || !Number.isFinite(upper)) {
      notify('请填写最低和最高定数');
      return;
    }
    if (
      Math.abs(lower * 10 - Math.round(lower * 10)) > 1e-8 ||
      Math.abs(upper * 10 - Math.round(upper * 10)) > 1e-8
    ) {
      notify('定数最多保留一位小数');
      return;
    }
    if (lower < 0 || upper > 20) {
      notify('定数范围需在 0.0–20.0 之间');
      return;
    }
    if (lower > upper) {
      notify('最低定数不能大于最高定数');
      return;
    }
    setSaving(true);
    try {
      await onSave({ default_constant_min: lower, default_constant_max: upper });
      notify('偏好设置已保存');
    } catch (error) {
      notify(messageOf(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="settings-panel preferences-panel" aria-labelledby="preferences-title">
      <h2 id="preferences-title">偏好设置</h2>
      {!preferences ? (
        <PreferencesStatus error={loadError} onRetry={onRetry} />
      ) : (
        <form onSubmit={save}>
          <fieldset className="preferences-fields" disabled={saving}>
            <legend>默认定数范围</legend>
            <div className="preferences-controls">
              <div className="field">
                <label htmlFor="default-constant-min">最低定数</label>
                <input
                  id="default-constant-min"
                  type="number"
                  min="0"
                  max="20"
                  step="0.1"
                  required
                  value={minimum}
                  onChange={(event) => {
                    setMinimum(event.target.value);
                  }}
                />
              </div>
              <div className="field">
                <label htmlFor="default-constant-max">最高定数</label>
                <input
                  id="default-constant-max"
                  type="number"
                  min="0"
                  max="20"
                  step="0.1"
                  required
                  value={maximum}
                  onChange={(event) => {
                    setMaximum(event.target.value);
                  }}
                />
              </div>
              <button type="submit" className="btn primary" disabled={saving}>
                {saving && <span className="spinner" />}
                {saving ? '保存中…' : '保存偏好'}
              </button>
            </div>
          </fieldset>
        </form>
      )}
    </section>
  );
}
