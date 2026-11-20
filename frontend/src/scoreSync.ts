import type { SyncResult } from './types';

export interface ScoreSyncJob {
  id: string;
  state: 'running' | 'succeeded' | 'failed';
  message?: string;
  error: { code: string; message: string } | null;
}
export interface ScoreSyncStatus {
  job: ScoreSyncJob | null;
  divingfish: {
    configured: boolean;
    last_sync: SyncResult | null;
  };
}
