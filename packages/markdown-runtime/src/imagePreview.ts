/** Shared inert media viewer; callers retain rendering and image permissions. */
const viewers = new WeakMap<Document, (restoreFocus?: boolean) => void>();

export function openImagePreview(
  uri: string,
  alt: string,
  ownerDocument: Document = window.document,
  source?: Element,
) {
  const document = ownerDocument;
  const host = document.defaultView || window;
  const previous = document.activeElement as HTMLElement | null;
  viewers.get(document)?.(false);
  function element<K extends keyof HTMLElementTagNameMap>(
    tag: K,
    attrs: Record<string, string> = {},
    text?: string,
  ): HTMLElementTagNameMap[K] {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs))
      node.setAttribute(name, value);
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const dialog = element("dialog", {
    class: "md-image-dialog",
    "aria-label": alt || "查看大图",
  });
  const image = element("img", {
    src: uri,
    alt: alt || "文档图片",
    draggable: "false",
    referrerpolicy: "no-referrer",
  });
  const canvas = element("div", {
    class: "md-image-canvas",
    tabindex: "0",
    role: "region",
    "aria-label": "图片平移缩放画布",
  });
  const toolbar = element("div", { class: "md-image-toolbar" });
  const percent = element(
    "button",
    {
      type: "button",
      title: "恢复原始大小（100%）",
      "aria-live": "polite",
      "aria-label": "图片缩放比例",
    },
    "100%",
  );
  let mode: "fit" | "width" | "manual" = "fit";
  let zoom = 1,
    x = 0,
    y = 0,
    width = 1,
    height = 1;
  let dragging: {
    id: number;
    px: number;
    py: number;
    x: number;
    y: number;
  } | null = null;
  let closed = false;
  const clamp = (value: number, min: number, max: number) =>
    Math.max(min, Math.min(max, value));
  const label = (scale: number) => {
    const value = scale * 100;
    return `${value >= 10 ? Math.round(value) : Number(value.toFixed(value >= 1 ? 1 : 2))}%`;
  };
  function paint() {
    const maxX = Math.max(0, (canvas.clientWidth + width * zoom) / 2 - 32);
    const maxY = Math.max(0, (canvas.clientHeight + height * zoom) / 2 - 32);
    x = clamp(x, -maxX, maxX);
    y = clamp(y, -maxY, maxY);
    Object.assign(image.style, {
      width: width + "px",
      height: height + "px",
      transform: `translate(-50%, -50%) translate(${x}px, ${y}px) scale(${zoom})`,
    });
    percent.textContent = label(zoom);
    dialog.dataset.mediaScale = String(zoom);
    dialog.dataset.mediaMode = mode;
    fitWindow.setAttribute("aria-pressed", String(mode === "fit"));
    fitWidth.setAttribute("aria-pressed", String(mode === "width"));
  }
  function fit(widthOnly = false) {
    mode = widthOnly ? "width" : "fit";
    width = image.naturalWidth || 1;
    height = image.naturalHeight || 1;
    const availableWidth = Math.max(1, canvas.clientWidth - 32);
    const availableHeight = Math.max(1, canvas.clientHeight - 32);
    zoom = widthOnly
      ? clamp(availableWidth / width, 0.001, 8)
      : Math.min(1, availableWidth / width, availableHeight / height);
    x = 0;
    // Keep the beginning of a tall diagram visible in width mode.
    y = widthOnly
      ? Math.max(0, (height * zoom - canvas.clientHeight) / 2 + 16)
      : 0;
    paint();
  }
  function scale(next: number, clientX?: number, clientY?: number) {
    mode = "manual";
    next = clamp(next, 0.001, 8);
    const rect = canvas.getBoundingClientRect();
    const px =
      (clientX ?? rect.left + rect.width / 2) - rect.left - rect.width / 2;
    const py =
      (clientY ?? rect.top + rect.height / 2) - rect.top - rect.height / 2;
    x = px - ((px - x) * next) / zoom;
    y = py - ((py - y) * next) / zoom;
    zoom = next;
    paint();
  }
  function reset() {
    x = 0;
    y = 0;
    scale(1);
  }
  const control = (name: string, text: string, action: () => void) => {
    const button = element(
      "button",
      { type: "button", "aria-label": name },
      text,
    );
    button.addEventListener("click", action);
    return button;
  };
  const fitWindow = control("适应窗口", "适应窗口", () => fit());
  const fitWidth = control("按宽度", "按宽度", () => fit(true));
  percent.addEventListener("click", reset);
  toolbar.append(
    element("strong", { class: "md-image-title" }, alt || "查看大图"),
    fitWindow,
    fitWidth,
    control("缩小图片", "−", () => scale(zoom / 1.25)),
    percent,
    control("放大图片", "＋", () => scale(zoom * 1.25)),
    control("关闭大图", "关闭", () => dismiss()),
  );
  image.addEventListener("load", () => fit());
  image.addEventListener("dragstart", (event) => event.preventDefault());
  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const units =
        event.deltaMode === 1
          ? 16
          : event.deltaMode === 2
            ? canvas.clientHeight
            : 1;
      scale(
        zoom * Math.exp(-clamp(event.deltaY * units, -160, 160) * 0.0025),
        event.clientX,
        event.clientY,
      );
    },
    { passive: false },
  );
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || dragging) return;
    event.preventDefault();
    canvas.focus({ preventScroll: true });
    dragging = {
      id: event.pointerId,
      px: event.clientX,
      py: event.clientY,
      x,
      y,
    };
    canvas.classList.add("dragging");
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!dragging || event.pointerId !== dragging.id) return;
    x = dragging.x + event.clientX - dragging.px;
    y = dragging.y + event.clientY - dragging.py;
    paint();
  });
  const release = (event: PointerEvent) => {
    if (!dragging || event.pointerId !== dragging.id) return;
    dragging = null;
    canvas.classList.remove("dragging");
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("lostpointercapture", release);
  canvas.addEventListener("dblclick", () => fit());
  canvas.addEventListener("keydown", (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const delta = (
      {
        ArrowLeft: [-40, 0],
        ArrowRight: [40, 0],
        ArrowUp: [0, -40],
        ArrowDown: [0, 40],
      } as Record<string, number[]>
    )[event.key];
    if (delta) {
      event.preventDefault();
      x += delta[0];
      y += delta[1];
      paint();
    } else if (["+", "=", "-", "0", "Home", "f", "w"].includes(event.key)) {
      event.preventDefault();
      if (["0", "Home"].includes(event.key)) reset();
      else if (event.key === "f") fit();
      else if (event.key === "w") fit(true);
      else scale(event.key === "-" ? zoom / 1.25 : zoom * 1.25);
    }
  });
  const resize = new host.ResizeObserver(() => {
    if (mode !== "manual") fit(mode === "width");
    else paint();
  });
  const sourceUri = source?.getAttribute("src");
  const watchSource = source
    ? new host.MutationObserver(() => {
        if (!source.isConnected || source.getAttribute("src") !== sourceUri)
          dismiss(false);
      })
    : null;
  function cleanup(restoreFocus = true) {
    if (closed) return;
    closed = true;
    resize.disconnect();
    watchSource?.disconnect();
    dialog.remove();
    if (viewers.get(document) === dismiss) viewers.delete(document);
    if (restoreFocus && previous?.isConnected)
      previous.focus?.({ preventScroll: true });
  }
  function dismiss(restoreFocus = true) {
    dialog.close();
    cleanup(restoreFocus);
  }
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") event.stopPropagation();
  });
  dialog.addEventListener("close", () => cleanup());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dismiss();
  });
  canvas.append(image);
  dialog.append(
    toolbar,
    canvas,
    element(
      "p",
      { class: "md-image-help" },
      "拖动平移 · 滚轮缩放 · 双击适应窗口",
    ),
  );
  viewers.set(document, dismiss);
  document.body.append(dialog);
  dialog.showModal();
  resize.observe(canvas);
  watchSource?.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["src"],
  });
  if (image.complete) fit();
  return dialog;
}
