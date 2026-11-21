import { useEffect, useRef, useState } from "react";
import { api, mutation } from "../../web/src/api";

type Props = {
  projectId: string;
  scope: { release_id: string } | { analysis_set_id: string };
  packageId: string;
  route: string;
  framePath: string[];
  state?: string;
  bindingId?: string;
};
type Job = { id: string; state: string; error?: { message?: string } };

export function PrototypePreviewAction({
  projectId,
  scope,
  packageId,
  route,
  framePath,
  state,
  bindingId,
}: Props) {
  const request = useRef<AbortController | null>(null);
  const [operation, setOperation] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const identity = JSON.stringify([
    projectId,
    scope,
    packageId,
    route,
    framePath,
    state,
    bindingId,
  ]);
  useEffect(() => {
    setOperation(null);
    setBusy(false);
    setMessage("");
    return () => {
      request.current?.abort();
      request.current = null;
    };
  }, [identity]);

  const matchBinding = Boolean(bindingId);
  const description = matchBinding
    ? "在隔离环境中生成可核验的绑定状态预览"
    : "在隔离环境中生成当前页面的默认状态预览";
  const base = `/api/v3/projects/${encodeURIComponent(projectId)}/interactive/previews`;

  async function update() {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    const options = { signal: controller.signal };
    setMessage("");
    setBusy(true);
    try {
      const params = new URLSearchParams({
        ...scope,
        package: packageId,
        route,
        frame_path: JSON.stringify(framePath),
      });
      const pages = await api<{ items: { page_ref: string }[] }>(
        `${base}/pages?${params}`,
        options,
      );
      const page = pages.items[0];
      if (!page) throw new Error("当前版本中没有可采集的登记页面");
      let job = await api<Job>(`${base}/capture`, {
        ...mutation("POST", {
          ...scope,
          package_artifact_id: packageId,
          page_ref: page.page_ref,
          ...(matchBinding ? { binding_id: bindingId } : {}),
        }),
        ...options,
      });
      setOperation(job);
      while (
        ["queued", "running", "cancelling", "retry_wait"].includes(job.state)
      ) {
        await new Promise<void>((resolve) => setTimeout(resolve, 1000));
        if (controller.signal.aborted) return;
        job = await api<Job>(
          `/jobs/${encodeURIComponent(job.id)}?project_id=${encodeURIComponent(projectId)}`,
          options,
        );
        setOperation(job);
      }
      setMessage(
        job.state === "succeeded"
          ? "release_id" in scope
            ? "预览已更新"
            : "预览已生成，可在同一资料版本保存后读取"
          : job.state === "cancelled"
            ? "已取消更新"
            : job.error?.message || "预览更新失败",
      );
    } catch (error) {
      if (!controller.signal.aborted) {
        setMessage(error instanceof Error ? error.message : "预览更新失败");
      }
    } finally {
      if (request.current === controller) {
        request.current = null;
        setBusy(false);
        setOperation(null);
      }
    }
  }

  async function cancel() {
    const controller = request.current;
    if (!controller || !operation) return;
    try {
      const next = await api<Job>(
        `/jobs/${encodeURIComponent(operation.id)}/cancel?project_id=${encodeURIComponent(projectId)}`,
        { ...mutation("POST", {}), signal: controller.signal },
      );
      if (!controller.signal.aborted) setOperation(next);
    } catch (error) {
      if (!controller.signal.aborted)
        setMessage(error instanceof Error ? error.message : "取消失败");
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => void update()}
        disabled={busy}
        title={description}
      >
        {busy ? "正在更新预览…" : "更新当前页预览"}
      </button>
      {operation && operation.state !== "cancelling" && (
        <button type="button" onClick={() => void cancel()}>
          取消预览
        </button>
      )}
      {message && <span role="status">{message}</span>}
    </>
  );
}
