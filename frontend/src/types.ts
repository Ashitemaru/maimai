export type Page = 'ap' | 'statistics' | 'cover' | 'venues' | 'settings';
export interface Chart {
  id: number;
  difficulty: number;
  chart_key: string;
  song_id: number;
  title: string;
  display_title: string;
  artist: string;
  type: 'standard' | 'dx' | 'utage';
  difficulty_index: number;
  difficulty_name: string;
  level: string;
  constant: number;
  jacket_url: string;
  jp_release_version: string | null;
  jp_release_version_candidate: string | null;
  cn_version?: number | null;
  jp_release_provider?: 'arcade_songs' | 'remywiki' | 'manual' | null;
  jp_release_date?: string | null;
  release_status: string;
  requires_venue: boolean;
  in_original_table: boolean;
  legacy_complete: boolean;
  legacy_venue: string;
  manual_ap: boolean | null;
  manual_venue: string;
  venue: string;
  note: string;
  manual_date: string | null;
  achievement: number | null;
  provider_achievement: number | null;
  provider_fc: string | null;
  provider_synced_at: string | null;
  is_ap: boolean;
  is_complete: boolean;
  status: 'app' | 'ap' | 'legacy' | 'incomplete';
  conflict: boolean;
  warnings: string[];
}
export interface Counts {
  total: number;
  complete: number;
  ap: number;
  remaining: number;
  conflicts: number;
  legacy_complete: number;
}
export interface SyncResult {
  ok: boolean;
  records: number;
  matched: number;
  unmatched: number;
  conflicts: number;
  synced_at: string;
}
export interface Summary extends Counts {
  levels: (Counts & { level: string })[];
  constants: (Counts & { constant: number })[];
  sync: SyncResult | null;
}
export interface Settings {
  configured: boolean;
  source: 'environment' | 'local' | null;
  sync: SyncResult | null;
}
export interface Preferences {
  default_constant_min: number;
  default_constant_max: number;
}
export interface Venue {
  name: string;
  count: number;
}
export interface ChartList {
  items: Chart[];
  total: number;
  offset: number;
  limit: number;
}
export type CompletionChart = Pick<
  Chart,
  | 'id'
  | 'difficulty'
  | 'chart_key'
  | 'title'
  | 'display_title'
  | 'type'
  | 'difficulty_name'
  | 'level'
  | 'constant'
  | 'jacket_url'
  | 'is_complete'
  | 'is_ap'
  | 'status'
  | 'conflict'
  | 'achievement'
  | 'provider_achievement'
>;
export interface CompletionTableData {
  items: CompletionChart[];
  total: number;
  summary: Summary;
  generated_at: string;
}
export interface ChartFilters {
  constants: number[];
  versions: { value: string; label: string }[];
  cn_versions?: { value: string; label: string }[];
  cn_matrix_versions?: { value: string; label: string; title: string }[];
}
