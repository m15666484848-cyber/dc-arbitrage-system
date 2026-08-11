import { useEffect, useState, useCallback, useRef } from "react";

export function useFetch<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);
  const inFlightRef = useRef<Promise<T | undefined> | null>(null);

  const reload = useCallback(async () => {
    if (inFlightRef.current) return inFlightRef.current;

    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);

    const task = fn()
      .then((res) => {
        if (mountedRef.current && requestId === requestIdRef.current) {
          setData(res);
        }
        return res;
      })
      .catch((e) => {
        if (mountedRef.current && requestId === requestIdRef.current) {
          setError(e);
        }
        return undefined;
      })
      .finally(() => {
        if (inFlightRef.current === task) {
          inFlightRef.current = null;
        }
        if (mountedRef.current && requestId === requestIdRef.current) {
          setLoading(false);
        }
      });

    inFlightRef.current = task;
    return task;
  }, deps);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    inFlightRef.current = null;
    reload();
  }, [reload]);

  return { data, loading, error, reload };
}
