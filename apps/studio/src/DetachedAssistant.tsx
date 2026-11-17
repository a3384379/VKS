import { useEffect, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { DetachedAssistantWindow } from "../../../packages/assistant-runtime/src/detached";

export function useDetachedAssistant(
  key: string,
  failed: (message: string) => void,
) {
  const [open, setOpen] = useState(false);
  const controller = useMemo(
    () => new DetachedAssistantWindow(key, setOpen, failed),
    [key],
  );
  useEffect(() => {
    setOpen(false);
    return () => controller.dispose();
  }, [controller]);
  return { open, controller };
}

export function DetachedPortal({
  target,
  children,
}: {
  target: HTMLElement | null;
  children: ReactNode;
}) {
  return target ? createPortal(children, target) : <>{children}</>;
}
