import { authGeneration, reportExpiredSession } from './auth';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const generation = authGeneration();
  const response = await fetch(path, { ...init, credentials: 'same-origin' });
  if (response.status === 401 && !new URL(path, location.origin).pathname.startsWith('/api/auth/'))
    reportExpiredSession(generation);
  return response;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch('/api' + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = data.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : [detail?.message, detail?.error]
            .filter(
              (value, index, items) =>
                typeof value === 'string' && value && items.indexOf(value) === index,
            )
            .join(' · ');
    throw new ApiError(message || '请求未完成，请稍后重试', response.status);
  }
  return data as T;
}
export const messageOf = (error: unknown) =>
  error instanceof Error ? error.message : '请求未完成，请稍后重试';
