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
  let response: Response;
  try {
    response = await fetch(path, { ...init, credentials: 'same-origin' });
  } catch (error) {
    if (error instanceof TypeError) throw new ApiError('网络请求失败，请检查连接后重试', 0);
    throw error;
  }
  if (response.status === 401 && !new URL(path, location.origin).pathname.startsWith('/api/auth/'))
    reportExpiredSession(generation);
  return response;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch('/api' + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init.headers },
  });
  const fallback = response.status >= 500 ? '服务暂时不可用，请稍后重试' : '请求未完成，请稍后重试';
  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (init.signal?.aborted) throw error;
    // Gateways can return an empty body or HTML while the app is unavailable.
    throw new ApiError(response.ok ? '服务返回的数据格式异常，请重试' : fallback, response.status);
  }
  if (!response.ok) {
    const detail = data?.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : [detail?.message, detail?.error]
            .filter(
              (value, index, items) =>
                typeof value === 'string' && value && items.indexOf(value) === index,
            )
            .join(' · ');
    throw new ApiError(message || fallback, response.status);
  }
  return data as T;
}
export const messageOf = (error: unknown) =>
  error instanceof Error ? error.message : '请求未完成，请稍后重试';
