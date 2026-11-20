import type { Preferences, Settings } from './types';
import { PreferencesPanel } from './Preferences';
import { CatalogUpdatePanel } from './CatalogUpdate';
import type { CatalogUpdateControl } from './useCatalogUpdate';
import { ScoreSyncPanel } from './ScoreSyncPanel';
import type { ScoreSyncControl } from './useScoreSync';
import { Heading } from './ui';

export function SettingsPage({
  settings,
  scoreSync,
  onSave,
  catalog,
  preferences,
  preferencesError,
  onRetryPreferences,
  onSavePreferences,
}: {
  settings: Settings | null;
  scoreSync: ScoreSyncControl;
  onSave: (token: string) => Promise<void>;
  catalog: CatalogUpdateControl;
  preferences: Preferences | null;
  preferencesError: string;
  onRetryPreferences: () => void;
  onSavePreferences: (value: Preferences) => Promise<void>;
}) {
  return (
    <>
      <Heading eyebrow="CONNECTED TO YOUR PLAY" title="数据与同步" />
      <ScoreSyncPanel control={scoreSync} settings={settings} onSaveToken={onSave} />
      <PreferencesPanel
        preferences={preferences}
        loadError={preferencesError}
        onRetry={onRetryPreferences}
        onSave={onSavePreferences}
      />
      <CatalogUpdatePanel control={catalog} />
      <section className="settings-panel export-panel" aria-labelledby="export-title">
        <h2 id="export-title">导出</h2>
        <div className="form-actions">
          <a className="btn" href="/api/export/venues.csv" download>
            导出 AP 机厅记录
          </a>
          <a className="btn" href="/api/export/venue-locations.csv" download>
            导出机厅坐标
          </a>
          <a className="btn" href="/api/export/assets.zip" download>
            导出封面素材包
          </a>
        </div>
      </section>
    </>
  );
}
