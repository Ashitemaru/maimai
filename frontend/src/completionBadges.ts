export const COMPLETION_BADGES = {
  // Center the solid ring at (39.5, 39.5), excluding the asymmetric faint glow.
  ap: {
    src: '/cover-assets/game-ui/game-badges-v2/nick/ComboStatus/3.png',
    crop: { x: 4, y: 4, width: 71, height: 71 },
  },
  app: {
    src: '/cover-assets/game-ui/game-badges-v2/nick/ComboStatus/4.png',
    crop: { x: 4, y: 4, width: 71, height: 71 },
  },
} as const;

export function completionBadgeKey(chart: {
  is_complete: boolean;
  is_ap: boolean;
  status: string;
}): keyof typeof COMPLETION_BADGES | null {
  return chart.is_complete ? (chart.is_ap && chart.status === 'app' ? 'app' : 'ap') : null;
}
