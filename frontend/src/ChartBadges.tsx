const difficultyNames = ['Basic', 'Advanced', 'Expert', 'Master', 'Re:Master', 'UTAGE'];

export function ChartTypeBadge({ type }: { type: string | null | undefined }) {
  const format = type?.toLowerCase() === 'std' ? 'standard' : type?.toLowerCase();
  const label = format === 'standard' ? 'STD' : format?.toUpperCase() || '谱面';
  return (
    <span className="type-label" data-chart-type={format}>
      {label}
    </span>
  );
}

export function DifficultyBadge({
  difficulty,
  name,
  constant,
  type,
}: {
  difficulty: number;
  name?: string | null;
  constant?: number;
  type?: string | null;
}) {
  const index = type?.toLowerCase() === 'utage' ? 5 : difficulty;
  return (
    <span className="difficulty" data-difficulty={index}>
      {name || difficultyNames[index] || '宴'}
      {constant !== undefined && ` ${constant.toFixed(1)}`}
    </span>
  );
}
