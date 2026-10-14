import { renderCover } from './cover';
import type { CoverChart } from './cover';

export interface BilibiliSession {
  connected: boolean;
  account: { mid: number; name: string } | null;
}
export interface PublishingVideo {
  bvid: string;
  title: string;
  cover: string;
  created_at?: string;
  cover43?: string;
  revision?: string;
  mirror_horizontal?: boolean;
}
export interface PublishingCollection {
  id: number;
  title: string;
  sections: { id: number; title: string }[];
}
export interface PublishingOrderItem {
  episode_id: number | null;
  aid: number;
  bvid: string;
  title: string;
  song_title: string | null;
  chart_key: string | null;
  version: string | null;
  position: number;
}
export interface PublishingOrder {
  season_id: number;
  season_title: string;
  section_id: number;
  section_title: string;
  status: 'ordered' | 'changes' | 'blocked';
  revision: string;
  current: PublishingOrderItem[];
  desired: PublishingOrderItem[];
  unmatched: { bvid: string; aid: number; title: string; reason: string }[];
  target_position?: number | null;
  target_bvid?: string;
}
export interface PublishingOrderCheck {
  checked_at: string;
  sections: PublishingOrder[];
}
export interface PublishingPlan {
  plan_id: string;
  bvid: string;
  old_title: string;
  title: string;
  chart: CoverChart;
  achievement: string;
  combo: 'ap' | 'app';
  constant: string;
  mirror_horizontal: boolean;
  version_override?: string;
  section_id: number;
  section_title?: string;
  expires_at: string;
  order?: PublishingOrder;
}
export interface PublishingJob {
  job_id: string;
  bvid?: string;
  state: string;
  steps: Record<string, unknown>;
  error?: string | { message?: string } | null;
  updated_at?: string;
}
export interface PublishingImages {
  cover16: string;
  cover43: string;
}

export function parseBvid(value: string) {
  return value.match(/BV[0-9A-Za-z]{10}/)?.[0] || '';
}

export function videoTitleCombo(
  video: Pick<PublishingVideo, 'bvid' | 'title'>,
): 'ap' | 'app' | null {
  const title = video.title.normalize('NFKC').trim();
  const ordinary = /(?:^|\s)AP(\s*\+)?$/i.exec(title);
  if (ordinary) return ordinary[1] ? 'app' : 'ap';
  if (video.bvid === 'BV1bJouBSEiH') {
    const mirrored = /^(\+\s*)?PA(?:\s|$)/i.exec(title);
    if (mirrored) return mirrored[1] ? 'app' : 'ap';
  }
  return null;
}

export function matchingCollectionSection(collections: PublishingCollection[], constant: string) {
  const matches = collections.flatMap((collection) =>
    collection.sections.filter((section) => section.title.trim() === constant.trim()),
  );
  return matches.length === 1 ? String(matches[0].id) : '';
}

// Use the established v8/v6 renderer without changing its geometry or typography.
// A partial asset load must never silently publish a fallback cover.
export async function renderPublishingImages(plan: PublishingPlan): Promise<PublishingImages> {
  const chart = {
    ...plan.chart,
    constant: plan.chart.type === 'utage' ? null : Number(plan.constant),
    level: plan.chart.type === 'utage' ? plan.constant : plan.chart.level,
  };
  async function render(aspect: '16:9' | '4:3') {
    const canvas = document.createElement('canvas');
    try {
      const receipt = await renderCover(canvas, chart, {
        achievement: plan.achievement,
        combo: plan.combo,
        aspect,
        versionOverride: plan.version_override,
      });
      const type = chart.type === 'dx' ? 'dx' : chart.type === 'utage' ? '' : 'standard';
      if (
        receipt.notices.length ||
        receipt.achievementStyle !== 'sprite' ||
        !receipt.gameAssets.includes(plan.combo) ||
        (type && !receipt.gameAssets.includes(type)) ||
        !receipt.bounds.some((bound) => bound.role === 'version_logo')
      )
        throw new Error(receipt.notices.join(' · ') || '封面素材不完整，请重试');
      if (plan.mirror_horizontal) {
        const mirrored = document.createElement('canvas');
        mirrored.width = canvas.width;
        mirrored.height = canvas.height;
        const context = mirrored.getContext('2d');
        if (!context) throw new Error('浏览器不支持封面绘制');
        context.translate(canvas.width, 0);
        context.scale(-1, 1);
        context.drawImage(canvas, 0, 0);
        const image = mirrored.toDataURL('image/png');
        mirrored.width = mirrored.height = 0;
        return image;
      }
      return canvas.toDataURL('image/png');
    } finally {
      canvas.width = canvas.height = 0;
    }
  }
  // Render in sequence to bound memory on iPhone.
  const cover16 = await render('16:9');
  const cover43 = await render('4:3');
  return { cover16, cover43 };
}

export const publishingState = (state: string) =>
  ({
    complete: '已整理',
    succeeded: '已整理',
    completed: '已整理',
    running: '整理中',
    pending: '等待处理',
    failed: '未完成',
    uncertain: '待确认结果',
    superseded: '已由新任务替代',
    partial: '部分完成',
    needs_attention: '需要处理',
  })[state] || state;

export const publishingComplete = (state: string) =>
  ['complete', 'completed', 'succeeded'].includes(state);
