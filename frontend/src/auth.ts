export type AuthSession = { authenticated: boolean; auth_required: boolean };

export const AUTH_EXPIRED_EVENT = 'mai-journal:auth-expired';
let sessionGeneration = 0;

export function authGeneration() {
  return sessionGeneration;
}

export function advanceAuthGeneration() {
  sessionGeneration += 1;
}

export function reportExpiredSession(generation: number) {
  if (generation === sessionGeneration) {
    advanceAuthGeneration();
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
}
