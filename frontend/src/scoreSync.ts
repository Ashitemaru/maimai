import type { SyncResult } from './types';

export interface WahlapSyncResult extends SyncResult {
  source: 'wahlap';
  notice?: string | null;
  logout_status?: string | null;
  logout_confirmed?: boolean;
}
export interface DivingFishUploadResult {
  ok: boolean;
  source: 'divingfish_upload';
  uploaded_at?: string;
  attempted_at?: string;
  error?: { code: string; message: string } | null;
  records?: number;
  uploaded_count?: number;
  snapshot_records?: number;
  skipped_count?: number;
  sync_id: string;
}
export interface ScoreSyncJob {
  id: string;
  kind: 'wahlap' | 'divingfish_upload';
  state: 'running' | 'succeeded' | 'failed';
  stage: string;
  message?: string;
  error: { code: string; message: string } | null;
  result: WahlapSyncResult | DivingFishUploadResult | null;
}
export interface ScoreSyncStatus {
  wahlap: {
    available: boolean;
    reason: string | null;
    verified: boolean;
    last_sync: WahlapSyncResult | null;
    has_snapshot: boolean;
  };
  job: ScoreSyncJob | null;
  divingfish: {
    configured: boolean;
    last_upload: DivingFishUploadResult | null;
  };
}
