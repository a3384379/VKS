import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent,
} from "react";

import {
  clampWindowBox as bound,
  startFloatingGesture,
  type FloatingBox as Box,
  type ResizeDirection,
} from "../../../packages/assistant-runtime/src/floating";
export type { ResizeDirection } from "../../../packages/assistant-runtime/src/floating";
const storageKey = "vks.studio.assistant-window.v1";
function savedBox(): Box | null {
  try {
    const box = JSON.parse(localStorage.getItem(storageKey) || "null");
    return box &&
      [box.left, box.top, box.width, box.height].every(Number.isFinite) &&
      box.width > 0 &&
      box.height > 0
      ? box
      : null;
  } catch {
    return null;
  }
}
export function useFloatingWindow(docked: boolean) {
  const [initial] = useState(savedBox);
  const [position, setPosition] = useState<{
    left: number;
    top: number;
  } | null>(initial);
  const [size, setSize] = useState<{ width: number; height: number } | null>(
    initial,
  );
  const [, viewportChanged] = useState(0);
  const ref = useRef<HTMLElement>(null),
    cleanupRef = useRef<(() => void) | null>(null);
  useEffect(() => {
    const resize = () => viewportChanged((v) => v + 1);
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      cleanupRef.current?.();
    };
  }, []);
  const commit = (box: Box) => {
    const next = bound(box);
    setPosition({ left: next.left, top: next.top });
    setSize({ width: next.width, height: next.height });
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
    } catch {
      /* Optional layout preference. */
    }
  };
  const begin = (
    event: PointerEvent<HTMLElement>,
    direction: ResizeDirection | "move",
  ) => {
    if (docked || event.button !== 0 || !event.isPrimary || !ref.current)
      return;
    cleanupRef.current?.();
    cleanupRef.current = startFloatingGesture(event, {
      node: ref.current,
      handle: event.currentTarget,
      direction,
      onCommit: commit,
    });
  };
  const keyboardResize = (dx: number, dy: number) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box || docked) return;
    commit({
      ...box,
      left: box.left,
      top: box.top,
      width: box.width + dx,
      height: box.height + dy,
    });
  };
  let style: CSSProperties | undefined;
  if (!docked) {
    const dimensions = size || {
      width: 430,
      height: Math.min(640, innerHeight - 205),
    };
    const point = position || {
      left: innerWidth - dimensions.width - 24,
      top: innerHeight - dimensions.height - 90,
    };
    style = {
      ...bound({ ...dimensions, ...point }),
      right: "auto",
      bottom: "auto",
    };
  }
  return {
    ref,
    position,
    setPosition,
    style,
    begin,
    keyboardResize,
    reset: () => {
      setPosition(null);
      setSize(null);
      try {
        localStorage.removeItem(storageKey);
      } catch {
        /* Optional layout preference. */
      }
    },
  };
}
