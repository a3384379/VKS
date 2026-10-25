import { useEffect, useState } from "react";
import { Notice, Spinner } from "./components";
export function ArtifactImage({
  projectId,
  artifactId,
  name,
  bare = false,
}: {
  projectId: string;
  artifactId: string;
  name: string;
  bare?: boolean;
}) {
  const [url, setUrl] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    setUrl("");
    setError("");
    void fetch(
      `/api/v1/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/download`,
      { credentials: "same-origin", signal: controller.signal },
    )
      .then(async (response) => {
        if (!response.ok)
          throw new Error("当前截图无法读取，请检查资料与访问权限");
        const raw = await response.arrayBuffer();
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(new Blob([raw]));
        setUrl(objectUrl);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, artifactId]);
  return (
    <div className={bare ? "raster-attachment-preview" : "screenshot-preview"}>
      {error ? (
        <Notice tone="red">{error}</Notice>
      ) : url ? (
        <img
          className="static-screenshot"
          src={url}
          alt={name}
          onError={() => setError("图片格式无法预览，可通过原始文件导出检查")}
        />
      ) : (
        <Spinner />
      )}
      {!bare && <p>静态截图 · 不执行页面操作或外部请求</p>}
    </div>
  );
}
