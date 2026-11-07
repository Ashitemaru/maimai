import type { Chart } from './types';

export const difficultyNames = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master', 'UTAGE'];

export const chartName = (chart: Pick<Chart, 'title' | 'display_title'>) =>
  chart.display_title || chart.title || '（空白曲名）';
