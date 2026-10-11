const timestampFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});

export function formatTimestamp(value: string | null | undefined, empty = '—'): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return empty;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return empty;
  const parts = Object.fromEntries(
    timestampFormatter.formatToParts(date).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

export function formatDate(value: string | null | undefined, empty = '—'): string {
  if (!value) return empty;
  // Calendar dates have no timezone and must retain their original day.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  return formatTimestamp(value, empty);
}
