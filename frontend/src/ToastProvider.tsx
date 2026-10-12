import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { ToastContext } from './Toast';

type Notification = { id: number; message: string };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<Notification | null>(null);
  const sequence = useRef(0);
  const element = useRef<HTMLDivElement>(null);
  const notify = useCallback((message: string) => {
    if (!message.trim()) return;
    const notification = { id: ++sequence.current, message };
    setCurrent(notification);
  }, []);

  useEffect(() => {
    const toast = element.current;
    if (current) {
      // Native dialogs use the top layer; keep their notifications visible above them.
      if (toast?.showPopover) {
        if (toast.matches(':popover-open')) toast.hidePopover();
        toast.showPopover();
      }
      const timer = setTimeout(() => {
        setCurrent((previous) => (previous?.id === current.id ? null : previous));
      }, 4000);
      return () => clearTimeout(timer);
    }
    if (toast?.hidePopover && toast.matches(':popover-open')) toast.hidePopover();
  }, [current]);

  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div
        ref={element}
        id="toast"
        popover="manual"
        className={current ? 'show' : ''}
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {current?.message || ''}
      </div>
    </ToastContext.Provider>
  );
}
