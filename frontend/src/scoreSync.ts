import type { SyncResult } from './types';

interface WahlapSyncResult extends SyncResult {
  source: 'wahlap';
  notice?: string | null;
  logout_status?: string | null;
  logout_confirmed?: boolean;
}
interface DivingFishUploadResult {
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
  error: {
    code: string;
    message: string;
    stage?: 'configuration' | 'authorization' | 'preview' | 'login' | 'scores';
    http_status?: number;
  } | null;
  result: WahlapSyncResult | DivingFishUploadResult | null;
}
export interface ScoreSyncStatus {
  wahlap: {
    available: boolean;
    checking?: boolean;
    reason: string | null;
    verified: boolean;
    last_sync: WahlapSyncResult | null;
    has_snapshot: boolean;
    maintenance: {
      active: boolean;
      timezone: 'Asia/Shanghai';
      start_time: string;
      end_time: string;
      retry_at: string | null;
      retry_after_seconds: number;
      message: string | null;
    };
  };
  job: ScoreSyncJob | null;
  divingfish: {
    configured: boolean;
    last_upload: DivingFishUploadResult | null;
  };
}
