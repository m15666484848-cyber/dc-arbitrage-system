import { useCallback, useEffect, useRef } from "react";

export function useDebouncedReload(reload: () => void | Promise<void>, delay = 500) {
  const timerRef = useRef<number | null>(null);

  const debouncedReload = useCallback(() => {
    if (timerRef.current) {
      window.clearTimeout(timerRef.current);
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      reload();
    }, delay);
  }, [reload, delay]);

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  return debouncedReload;
}
