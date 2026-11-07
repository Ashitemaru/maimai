import type { SyncResult } from './types';

export interface ScoreSyncJob {
  id: string;
  kind: 'divingfish';
  state: 'running' | 'succeeded' | 'failed';
  stage: string;
  message?: string;
  error: { code: string; message: string } | null;
  result: SyncResult | null;
}
export interface ScoreSyncStatus {
  job: ScoreSyncJob | null;
  divingfish: {
    configured: boolean;
    last_sync: SyncResult | null;
  };
}
