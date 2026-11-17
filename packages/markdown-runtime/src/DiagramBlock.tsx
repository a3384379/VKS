import { useEffect, useRef, useState } from "react";
import { validateDiagramSource, type DiagramKind } from "./diagram-policy";
import "./diagrams.css";
import { openImagePreview } from "./imagePreview";

export function DiagramBlock({
  kind,
  source,
}: {
  kind: DiagramKind;
  source: string;
}) {
  const target = useRef<HTMLElement>(null);
  const preview = useRef<HTMLDialogElement | null>(null);
  const [visible, setVisible] = useState(false);
  const [result, setResult] = useState({
    key: "",
    image: "",
    error: "",
    width: 600,
  });
  const [attempt, setAttempt] = useState(0);
  const label = kind === "mermaid" ? "Mermaid" : "PlantUML";
  const problem = validateDiagramSource(kind, source);
  const key = kind + "\0" + source;
  const image = result.key === key ? result.image : "";
  const error = result.key === key ? result.error : "";
  const showPreview = () => {
    preview.current = openImagePreview(
      image,
      `${label} 图表`,
      target.current?.ownerDocument,
      target.current?.querySelector("img") || undefined,
    );
  };
  useEffect(() => () => preview.current?.close(), [image]);
  useEffect(() => {
    if (!target.current) return;
    const host = target.current.ownerDocument.defaultView || window;
    const observer = new host.IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "400px" },
    );
    observer.observe(target.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    setResult({ key, image: "", error: "", width: 600 });
    if (!visible || problem) return;
    const abort = new AbortController();
    let url = "";
    void import("./diagram-renderer")
      .then(({ renderDiagram }) =>
        renderDiagram(
          kind,
          source,
          abort.signal,
          target.current?.ownerDocument,
        ),
      )
      .then((svg) => {
        if (abort.signal.aborted) return;
        url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
        const root = new DOMParser().parseFromString(
          svg,
          "image/svg+xml",
        ).documentElement;
        const width = Number.parseFloat(root.getAttribute("width") || "600");
        setResult({
          key,
          image: url,
          error: "",
          width:
            Number.isFinite(width) && width > 0 ? Math.min(width, 8192) : 600,
        });
      })
      .catch(() => {
        if (!abort.signal.aborted)
          setResult({
            key,
            image: "",
            error: "图表未能渲染，请检查语法或展开源码查看",
            width: 600,
          });
      });
    return () => {
      abort.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [kind, source, key, visible, problem, attempt]);
  return (
    <figure ref={target} className="diagram-block" aria-label={`${label} 图表`}>
      <figcaption>
        <strong>{kind === "mermaid" ? "流程与关系图" : "结构与时序图"}</strong>
        {error && !problem && (
          <button
            type="button"
            className="diagram-retry"
            onClick={() => setAttempt((value) => value + 1)}
          >
            重新渲染
          </button>
        )}
        {image && (
          <span className="diagram-tools">
            <button
              type="button"
              aria-label="查看图表大图"
              onClick={showPreview}
            >
              放大查看 ↗
            </button>
            <a href={image} download={`${kind}-diagram.svg`}>
              下载 SVG
            </a>
          </span>
        )}
      </figcaption>
      {image ? (
        <div className="diagram-image">
          <img
            src={image}
            alt={`${label} 图表`}
            title="点击查看大图"
            tabIndex={0}
            aria-haspopup="dialog"
            onClick={showPreview}
            onKeyDown={(event) => {
              if (["Enter", " "].includes(event.key)) {
                event.preventDefault();
                showPreview();
              }
            }}
          />
          <small className="md-media-hint">
            点图放大 · 适应窗口或按宽度查看细节
          </small>
        </div>
      ) : (
        <p className="diagram-status" role="status">
          {problem || error || "正在本地渲染图表…"}
        </p>
      )}
      <details open={!!problem || !!error} className="diagram-source">
        <summary>查看图表源码</summary>
        <pre>
          <code>{source}</code>
        </pre>
      </details>
    </figure>
  );
}
