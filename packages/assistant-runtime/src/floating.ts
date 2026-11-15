/** Shared pointer/geometry engine for Studio and the single-file reader. */
export type FloatingBox = {
  left: number;
  top: number;
  width: number;
  height: number;
};
export type ResizeDirection = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
type Start = Pick<
  PointerEvent,
  "button" | "isPrimary" | "pointerId" | "clientX" | "clientY"
>;
type Minimum = { width: number; height: number };
const defaults = { width: 360, height: 280 };

export function clampWindowBox(
  box: FloatingBox,
  minimum: Minimum = defaults,
  viewport = { width: innerWidth, height: innerHeight },
): FloatingBox {
  const availableW = Math.max(1, viewport.width - 16),
    availableH = Math.max(1, viewport.height - 16);
  const width = Math.min(
      Math.max(Math.min(minimum.width, availableW), box.width),
      availableW,
    ),
    height = Math.min(
      Math.max(Math.min(minimum.height, availableH), box.height),
      availableH,
    );
  return {
    width,
    height,
    left: Math.max(8, Math.min(viewport.width - width - 8, box.left)),
    top: Math.max(8, Math.min(viewport.height - height - 8, box.top)),
  };
}

export function startFloatingGesture(
  event: Start,
  {
    node,
    handle,
    direction,
    onCommit,
    onEnd,
    minimum = defaults,
    shieldClass = "assistant-drag-shield",
    activeClass = "resizing",
    threshold = 3,
  }: {
    node: HTMLElement;
    handle: HTMLElement;
    direction: ResizeDirection | "move";
    onCommit: (box: FloatingBox) => void;
    onEnd?: (moved: boolean) => void;
    minimum?: Minimum;
    shieldClass?: string;
    activeClass?: string;
    threshold?: number;
  },
): () => void {
  if (event.button !== 0 || !event.isPrimary) return () => {};
  const start = node.getBoundingClientRect(),
    pointer = event.pointerId;
  let next: FloatingBox = {
      left: start.left,
      top: start.top,
      width: start.width,
      height: start.height,
    },
    moved = false,
    frame = 0,
    ended = false,
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
  const move = (e: PointerEvent) => {
    if (e.pointerId !== pointer || ended) return;
    const dx = e.clientX - event.clientX,
      dy = e.clientY - event.clientY;
    if (!moved && Math.hypot(dx, dy) < threshold) return;
    e.preventDefault();
    if (!moved) {
      moved = true;
      shield = document.createElement("div");
      shield.className = shieldClass;
      shield.setAttribute("aria-hidden", "true");
      document.body.append(shield);
      node.classList.add(activeClass);
    }
    if (direction === "move")
      next = clampWindowBox(
        {
          left: start.left + dx,
          top: start.top + dy,
          width: start.width,
          height: start.height,
        },
        minimum,
      );
    else {
      const minW = Math.min(minimum.width, Math.max(1, innerWidth - 16)),
        minH = Math.min(minimum.height, Math.max(1, innerHeight - 16));
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
      next = clampWindowBox(
        { left, top, width: right - left, height: bottom - top },
        minimum,
      );
    }
    if (!frame) frame = requestAnimationFrame(paint);
  };
  const clean = () => {
    window.removeEventListener("pointermove", move, true);
    window.removeEventListener("pointerup", finish, true);
    window.removeEventListener("pointercancel", finish, true);
    window.removeEventListener("blur", finish);
    handle.removeEventListener("lostpointercapture", finish);
    if (frame) cancelAnimationFrame(frame);
    shield?.remove();
    node.classList.remove(activeClass);
  };
  const release = () => {
    try {
      if (handle.hasPointerCapture(pointer))
        handle.releasePointerCapture(pointer);
    } catch {
      /* Already released. */
    }
  };
  const finish = (e: Event) => {
    if (ended || ("pointerId" in e && e.pointerId !== pointer)) return;
    ended = true;
    clean();
    if (moved) {
      paint();
      onCommit(next);
    }
    onEnd?.(moved);
    release();
  };
  // Capture before the first movement so a fast gesture crossing an iframe
  // cannot lose its initial pointer events.
  try {
    handle.setPointerCapture(pointer);
  } catch {
    /* Global listeners remain. */
  }
  window.addEventListener("pointermove", move, {
    capture: true,
    passive: false,
  });
  window.addEventListener("pointerup", finish, true);
  window.addEventListener("pointercancel", finish, true);
  window.addEventListener("blur", finish);
  handle.addEventListener("lostpointercapture", finish);
  return () => {
    if (!ended) {
      ended = true;
      clean();
      release();
      onEnd?.(false);
    }
  };
}
