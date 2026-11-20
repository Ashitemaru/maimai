import { useEffect, useState } from 'react';

export function useDebounce<T>(value: T, delay = 220) {
  const [result, setResult] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setResult(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return result;
}
