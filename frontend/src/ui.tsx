import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import type { Chart } from './types';

const paths: Record<string, string> = {
  clock: 'M12 8v4l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  table: 'M3 3h18v18H3zM3 9h18M9 3v18M15 9v12M3 15h18',
  image: 'M4 3h16v18H4zM4 15l5-5 5 5 3-3 3 3M8 7h.01',
  pin: 'M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 0 1 16 0ZM9 10a3 3 0 1 0 6 0 3 3 0 1 0-6 0',
  settings: 'M4 7h16M4 17h16M8 4v6M16 14v6',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  sync: 'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M18 18A8 8 0 0 1 5 16',
  plus: 'M12 5v14M5 12h14',
  check: 'm5 12 4 4L19 6',
  close: 'm6 6 12 12M6 18 18 6',
  spark: 'm12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3Z',
};
export function Icon({ name }: { name: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || paths.grid} />
    </svg>
  );
}
export function Heading({
  title,
  eyebrow,
  actions,
}: {
  title: string;
  eyebrow: string;
  actions?: ReactNode;
}) {
  return (
    <div className="heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
      </div>
      <div className="heading-actions">{actions}</div>
    </div>
  );
}
export function Badge({ chart }: { chart: Chart }) {
  return chart.is_ap ? (
    <span className="badge ap">
      <Icon name="check" />
      {chart.status === 'app' ? 'AP+' : 'AP'}
    </span>
  ) : chart.is_complete ? (
    <span className="badge complete">✦ 已完成</span>
  ) : (
    <span className="badge pending">{chart.pending_record ? '待核实' : '未标记'}</span>
  );
}
export function JacketImage({
  src,
  className = '',
  loading,
  retryKey,
}: {
  src: string;
  className?: string;
  loading?: 'lazy' | 'eager';
  retryKey?: number;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src, retryKey]);
  return failed ? (
    <span className={'jacket-placeholder ' + className} aria-hidden="true" />
  ) : (
    <img className={className} src={src} alt="" loading={loading} onError={() => setFailed(true)} />
  );
}
