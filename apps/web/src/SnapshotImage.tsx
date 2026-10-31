import "./snapshot-image.css";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** A viewer for an already authorized, saved image; never replays a capture. */
export function SnapshotImage({ src, alt }: { src: string; alt: string }) {
  const [openedSource, setOpenedSource] = useState<string | null>(null);
  const open = openedSource === src;
  const [fit, setFit] = useState(true);
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(0);
  const [error, setError] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    setOpenedSource(null);
    setWidth(0);
    setError(false);
  }, [src]);
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    close.current?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setOpenedSource(null);
      }
      if (event.key === "Tab") {
        const buttons = [
          ...(dialog.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), [tabindex='0']",
          ) || []),
        ];
        const first = buttons[0],
          last = buttons.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    document.addEventListener("keydown", keydown, true);
    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", keydown, true);
      if (trigger.current?.isConnected)
        trigger.current.focus({ preventScroll: true });
    };
  }, [open, src]);
  function enlarge(delta: number) {
    setFit(false);
    setZoom((value) => Math.min(4, Math.max(0.25, value + delta)));
  }
  return (
    <>
      <button
        type="button"
        className="snapshot-thumbnail"
        ref={trigger}
        aria-label={`放大快照：${alt}`}
        onClick={() => {
          setFit(true);
          setZoom(1);
          setError(false);
          setOpenedSource(src);
        }}
      >
        <img src={src} alt={alt} />
        <span>点击查看大图</span>
      </button>
      {open &&
        createPortal(
          <div
            className="snapshot-overlay"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setOpenedSource(null);
            }}
          >
            <section
              ref={dialog}
              className="snapshot-viewer"
              role="dialog"
              aria-modal="true"
              aria-label="快照大图"
            >
              <header>
                <div>
                  <h2>{alt}</h2>
                  <p>已保存的原始快照 · 只读查看</p>
                </div>
                <button
                  ref={close}
                  type="button"
                  aria-label="关闭快照"
                  onClick={() => setOpenedSource(null)}
                >
                  关闭 ×
                </button>
              </header>
              <nav aria-label="快照缩放控制">
                <button
                  type="button"
                  aria-label="缩小快照"
                  onClick={() => enlarge(-0.25)}
                  disabled={!fit && zoom <= 0.25}
                >
                  −
                </button>
                <span aria-live="polite">
                  {fit ? "适应窗口" : `${Math.round(zoom * 100)}%`}
                </span>
                <button
                  type="button"
                  aria-label="放大快照"
                  onClick={() => enlarge(0.25)}
                  disabled={!fit && zoom >= 4}
                >
                  ＋
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setFit(true);
                    setZoom(1);
                  }}
                >
                  适应窗口
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setFit(false);
                    setZoom(1);
                  }}
                >
                  原始尺寸
                </button>
              </nav>
              <div
                className={`snapshot-viewer-stage ${fit ? "fit" : "scaled"}`}
                role="region"
                aria-label="快照大图画布"
                tabIndex={0}
              >
                {error ? (
                  <p role="alert">
                    快照不可读取或访问权限已变化，请关闭后刷新当前资料。
                  </p>
                ) : (
                  <img
                    src={src}
                    alt={alt + " · 大图"}
                    style={fit ? undefined : { width: (width || 1280) * zoom }}
                    onLoad={(event) =>
                      setWidth(event.currentTarget.naturalWidth)
                    }
                    onError={() => setError(true)}
                  />
                )}
              </div>
            </section>
          </div>,
          document.body,
        )}
    </>
  );
}
