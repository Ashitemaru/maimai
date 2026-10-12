export type Page = 'ap' | 'statistics' | 'pending' | 'cover' | 'venues' | 'settings';
interface PendingRecord {
  id: string;
  venue: string;
  created_at: string;
  updated_at: string;
  revision: number;
  reason: string;
}
export interface APRecordItem {
  chart_key: string;
  title: string;
  type: Chart['type'];
  difficulty_name: string;
  constant: number;
  jacket_url: string;
  thumbnail_url?: string | null;
  record_revision: number;
  pending_record: PendingRecord;
}
export interface APRecords {
  items: APRecordItem[];
  count: number;
}
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
  thumbnail_url?: string | null;
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
  manual_revision: number;
  manual_venue: string;
  venue: string;
  note: string;
  manual_date: string | null;
  achievement: number | null;
  provider_achievement: number | null;
  provider_fc: string | null;
  provider_synced_at: string | null;
  provider_fs?: string | null;
  provider_dx_score?: number | null;
  provider_rate?: string | null;
  provider_ra?: number | null;
  pending_record?: PendingRecord | null;
  record_revision?: number;
  venue_pending?: boolean;
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
  confirmed?: number;
  pending?: number;
  venue_pending?: number;
  sync_id?: string;
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
  last_used_at?: string | null;
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
  | 'thumbnail_url'
  | 'is_complete'
  | 'is_ap'
  | 'status'
  | 'conflict'
  | 'achievement'
  | 'provider_achievement'
  | 'pending_record'
  | 'record_revision'
  | 'venue_pending'
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
