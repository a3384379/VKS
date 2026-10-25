import { useEffect, useRef, useState } from "react";
import { validateDiagramSource, type DiagramKind } from "./diagram-policy";
import "./diagrams.css";

export function DiagramBlock({
  kind,
  source,
}: {
  kind: DiagramKind;
  source: string;
}) {
  const target = useRef<HTMLElement>(null);
  const [visible, setVisible] = useState(false);
  const [result, setResult] = useState({
    key: "",
    image: "",
    error: "",
    width: 600,
  });
  const [zoom, setZoom] = useState(1);
  const [attempt, setAttempt] = useState(0);
  const label = kind === "mermaid" ? "Mermaid" : "PlantUML";
  const problem = validateDiagramSource(kind, source);
  const key = kind + "\0" + source;
  const image = result.key === key ? result.image : "";
  const error = result.key === key ? result.error : "";
  useEffect(() => {
    if (!target.current) return;
    const observer = new IntersectionObserver(
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
    setZoom(1);
    if (!visible || problem) return;
    const abort = new AbortController();
    let url = "";
    void import("./diagram-renderer")
      .then(({ renderDiagram }) => renderDiagram(kind, source, abort.signal))
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
        <strong>{label}</strong>
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
              aria-label="缩小图表"
              disabled={zoom <= 0.5}
              onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))}
            >
              −
            </button>
            <button
              type="button"
              aria-label="重置图表缩放"
              onClick={() => setZoom(1)}
            >
              {Math.round(zoom * 100)}%
            </button>
            <button
              type="button"
              aria-label="放大图表"
              disabled={zoom >= 3}
              onClick={() => setZoom((value) => Math.min(3, value + 0.25))}
            >
              ＋
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
            style={{ width: `min(${result.width * zoom}px, ${zoom * 100}%)` }}
          />
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
