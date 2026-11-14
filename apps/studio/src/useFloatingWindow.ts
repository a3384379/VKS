import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent,
} from "react";

type Box = { left: number; top: number; width: number; height: number };
export type ResizeDirection = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
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
function bound(box: Box): Box {
  const width = Math.min(
    Math.max(Math.min(360, innerWidth - 16), box.width),
    innerWidth - 16,
  );
  const height = Math.min(
    Math.max(Math.min(280, innerHeight - 16), box.height),
    innerHeight - 16,
  );
  return {
    width,
    height,
    left: Math.max(8, Math.min(innerWidth - width - 8, box.left)),
    top: Math.max(8, Math.min(innerHeight - height - 8, box.top)),
  };
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
    const node = ref.current,
      handle = event.currentTarget,
      start = node.getBoundingClientRect();
    const x = event.clientX,
      y = event.clientY,
      pointer = event.pointerId;
    let next: Box = {
      left: start.left,
      top: start.top,
      width: start.width,
      height: start.height,
    };
    let moved = false,
      frame = 0,
      shield: HTMLDivElement | undefined;
    const paint = () => {
      frame = 0;
      Object.assign(node.style, {
        left: next.left + "px",
        top: next.top + "px",
        width: next.width + "px",
        height: next.height + "px",
        right: "auto",
        bottom: "auto",
      });
    };
    const move = (e: globalThis.PointerEvent) => {
      if (e.pointerId !== pointer) return;
      const dx = e.clientX - x,
        dy = e.clientY - y;
      if (!moved && Math.hypot(dx, dy) < 3) return;
      e.preventDefault();
      if (!moved) {
        moved = true;
        shield = document.createElement("div");
        shield.className = "assistant-drag-shield";
        shield.setAttribute("aria-hidden", "true");
        document.body.append(shield);
        node.classList.add("resizing");
      }
      if (direction === "move")
        next = bound({
          left: start.left + dx,
          top: start.top + dy,
          width: start.width,
          height: start.height,
        });
      else {
        const minW = Math.min(360, innerWidth - 16),
          minH = Math.min(280, innerHeight - 16);
        let left = start.left,
          top = start.top,
          right = start.right,
          bottom = start.bottom;
        if (direction.includes("w"))
          left = Math.max(8, Math.min(right - minW, start.left + dx));
        if (direction.includes("e"))
          right = Math.min(
            innerWidth - 8,
            Math.max(left + minW, start.right + dx),
          );
        if (direction.includes("n"))
          top = Math.max(8, Math.min(bottom - minH, start.top + dy));
        if (direction.includes("s"))
          bottom = Math.min(
            innerHeight - 8,
            Math.max(top + minH, start.bottom + dy),
          );
        next = bound({ left, top, width: right - left, height: bottom - top });
      }
      if (!frame) frame = requestAnimationFrame(paint);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", move, true);
      window.removeEventListener("pointerup", finish, true);
      window.removeEventListener("pointercancel", finish, true);
      window.removeEventListener("blur", finish);
      handle.removeEventListener("lostpointercapture", finish);
      if (frame) cancelAnimationFrame(frame);
      shield?.remove();
      node.classList.remove("resizing");
      cleanupRef.current = null;
    };
    const finish = (e: Event) => {
      if ("pointerId" in e && e.pointerId !== pointer) return;
      cleanup();
      if (moved) {
        paint();
        commit(next);
      }
      try {
        if (handle.hasPointerCapture(pointer))
          handle.releasePointerCapture(pointer);
      } catch {
        /* The browser may already have released it. */
      }
    };
    cleanupRef.current = cleanup;
    handle.setPointerCapture(pointer);
    window.addEventListener("pointermove", move, {
      capture: true,
      passive: false,
    });
    window.addEventListener("pointerup", finish, true);
    window.addEventListener("pointercancel", finish, true);
    window.addEventListener("blur", finish);
    handle.addEventListener("lostpointercapture", finish);
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
