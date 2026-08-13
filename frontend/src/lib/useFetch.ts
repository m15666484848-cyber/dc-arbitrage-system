import { useEffect, useState, useCallback, useRef } from "react";

export function useFetch<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);
  const inFlightRef = useRef<Promise<T | undefined> | null>(null);
  // S16: use ref to store latest fn, avoid useCallback dep churn
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const reload = useCallback(async () => {
    if (inFlightRef.current) return inFlightRef.current;

    const requestId = ++requestIdRef.current;
    setLoading(true);
    setError(null);

    const task = fnRef.current()
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
  }, []); // S16: stable, no deps

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // S16: deps drive the effect, not reload
  useEffect(() => {
    inFlightRef.current = null;
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, error, reload };
}
