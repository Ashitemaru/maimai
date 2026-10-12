import { createContext, useContext, useEffect, useRef } from 'react';

export const ToastContext = createContext<((message: string) => void) | null>(null);

export function useNotify() {
  const notify = useContext(ToastContext);
  if (!notify) throw new Error('ToastProvider is required');
  return notify;
}

export function useToastMessage(message: string | null | undefined) {
  const notify = useNotify();
  const previous = useRef('');
  useEffect(() => {
    const next = message || '';
    if (next && next !== previous.current) notify(next);
    previous.current = next;
  }, [message, notify]);
}
