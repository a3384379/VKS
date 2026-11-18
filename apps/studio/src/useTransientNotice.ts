import { useCallback, useEffect, useState } from "react";

const noticeDuration = 6000;

export function useTransientNotice(visible = true) {
  const [notice, setNotice] = useState<{ message: string } | null>(null);
  const showNotice = useCallback((message: string) => {
    setNotice(message ? { message } : null);
  }, []);

  useEffect(() => {
    if (!notice || !visible) return;
    const timer = window.setTimeout(() => {
      setNotice((current) => (current === notice ? null : current));
    }, noticeDuration);
    return () => window.clearTimeout(timer);
  }, [notice, visible]);

  return [notice?.message ?? "", showNotice] as const;
}
