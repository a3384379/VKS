import { useEffect, useState } from "react";
import { api } from "./api";
import { Button, Notice, Tag } from "./components";
import { useWorkspace } from "./context";
import type { Entity } from "./types";
export function BusinessReferences({ entityId }: { entityId: string }) {
  const { w, baseline, navigate, navigateExplanation } = useWorkspace();
  const [data, setData] = useState<{
      actions: Entity[];
      is_truncated: boolean;
    } | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError("");
    const q = new URLSearchParams({ project_id: w.project.id });
    if (w.selected_release) q.set("release_id", w.selected_release.id);
    else if (w.selected_analysis_set)
      q.set("analysis_set_id", w.selected_analysis_set.id);
    void api<{ actions: Entity[]; is_truncated: boolean }>(
      `/api/v2/entities/${entityId}/business-references?${q}`,
      { signal: controller.signal },
    )
      .then(setData)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [
    entityId,
    w.project.id,
    baseline,
    w.selected_analysis_set?.selection_version,
  ]);
  return (
    <div className="v2-business-references">
      <h3>关联业务动作</h3>
      <Tag>已确认且有效 · 同版 3 跳</Tag>
      {error && <Notice tone="red">{error}</Notice>}
      {data?.actions.map((a) => (
        <Button
          key={a.id}
          onClick={() => {
            navigateExplanation({ actionId: a.id, followTarget: false });
            navigate("workbench");
          }}
        >
          {a.name} →
        </Button>
      ))}
      {data && !data.actions.length && (
        <p className="muted">当前范围没有已确认业务动作，不能凭名称推断。</p>
      )}
      {data?.is_truncated && (
        <Notice>关联范围已截断，可缩小范围后继续查询。</Notice>
      )}
    </div>
  );
}
