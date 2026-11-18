import "./markdown-extras.css";
import { useEffect, useRef, useState } from "react";
import { openImagePreview } from "./imagePreview";

const rasterTypes = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);
export function attachmentUrl(
  documentId: string | undefined,
  revisionId: string | undefined,
  path: string | undefined,
  scope?: {
    analysisSetId?: string;
    releaseId?: string;
    selectionVersion?: number;
  },
) {
  if (!documentId || !revisionId || !path) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(path).trim();
  } catch {
    return null;
  }
  if (
    !decoded.trim() ||
    /^(?:[a-z][a-z\d+.-]*:|[\\/])/i.test(decoded) ||
    /[\u0000-\u001f\u007f\\]/.test(decoded)
  )
    return null;
  const relative = decoded.split(/[?#]/, 1)[0];
  if (!relative) return null;
  return (
    "/api/v1/documents/" +
    encodeURIComponent(documentId) +
    "/revisions/" +
    encodeURIComponent(revisionId) +
    "/attachments?path=" +
    encodeURIComponent(relative) +
    (scope?.releaseId
      ? "&release_id=" + encodeURIComponent(scope.releaseId)
      : scope?.analysisSetId
        ? "&analysis_set_id=" + encodeURIComponent(scope.analysisSetId)
        : "") +
    (scope?.releaseId || scope?.selectionVersion === undefined
      ? ""
      : "&selection_version=" + encodeURIComponent(scope.selectionVersion))
  );
}
export function rasterContentType(value: string | null) {
  const mediaType = (value || "").split(";", 1)[0].trim().toLowerCase();
  return rasterTypes.has(mediaType) ? mediaType : null;
}

export function MarkdownAttachment({
  source,
  alt,
}: {
  source: string;
  alt: string;
}) {
  const preview = useRef<HTMLDialogElement | null>(null);
  const [url, setUrl] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    setUrl("");
    setError("");
    void fetch(source, {
      credentials: "same-origin",
      signal: controller.signal,
      redirect: "error",
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("附件不存在或当前账户没有读取权限");
        const mediaType = rasterContentType(
          response.headers.get("Content-Type"),
        );
        if (!mediaType)
          throw new Error("附件不是允许的位图格式，可通过授权附件下载检查");
        const size = Number(response.headers.get("Content-Length"));
        if (size > 20 * 1024 * 1024)
          throw new Error("图片超过预览大小，请下载附件查看");
        const bytes = await response.arrayBuffer();
        if (bytes.byteLength > 20 * 1024 * 1024)
          throw new Error("图片超过预览大小，请下载附件查看");
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(new Blob([bytes], { type: mediaType }));
        setUrl(objectUrl);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => {
      preview.current?.close();
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [source]);
  return (
    <span className="markdown-attachment">
      {error ? (
        <span className="attachment-note" role="status">
          {alt || "图片"}：{error}
        </span>
      ) : url ? (
        <img
          src={url}
          alt={alt}
          loading="lazy"
          title="点击查看大图"
          tabIndex={0}
          aria-haspopup="dialog"
          onClick={(event) =>
            (preview.current = openImagePreview(
              url,
              alt,
              event.currentTarget.ownerDocument,
              event.currentTarget,
            ))
          }
          onKeyDown={(event) => {
            if (["Enter", " "].includes(event.key)) {
              event.preventDefault();
              preview.current = openImagePreview(
                url,
                alt,
                event.currentTarget.ownerDocument,
                event.currentTarget,
              );
            }
          }}
          onError={() => setError("位图无法解码，请下载原始附件查看")}
        />
      ) : (
        <span className="attachment-note" role="status">
          正在读取授权图片：{alt || "图片"}
        </span>
      )}
    </span>
  );
}
