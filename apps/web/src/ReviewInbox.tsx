import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useState } from "react";
import { api } from "./api";
import { Button, Notice, Tag } from "./components";
import { usePermission, useWorkspace } from "./context";
import type { Dict } from "./types";
const names: Record<string, string> = {
  relation: "关联候选",
  journey: "业务流程",
  migration: "迁移提案",
  document: "文档修订",
};
export function ReviewInbox() {
  const { w, navigate } = useWorkspace();
  const canReview = usePermission("links:review");
  const [items, setItems] = useState<
      { kind: string; subject_id: string; data: Dict }[]
    >([]),
    [error, setError] = useState("");
  useEffect(() => {
    setItems([]);
    if (!canReview || !w.selected_analysis_set || w.selected_release) return;
    const controller = new AbortController();
    void api<typeof items>(
      `/api/v2/projects/${w.project.id}/review-inbox?analysis_set_id=${w.selected_analysis_set.id}`,
      { signal: controller.signal },
    )
      .then(setItems)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [
    w.project.id,
    w.selected_analysis_set?.id,
    w.selected_analysis_set?.selection_version,
    canReview,
  ]);
  if (!canReview || w.selected_release) return null;
  return (
    <Panel
      title="审核收件箱"
      action={<Tag>当前可见草稿 · {items.length} 项</Tag>}
    >
      {error && <Notice tone="red">{error}</Notice>}
      <p className="muted">
        审核绑定准确修订。确认流程、文档与关系后，再做发布预检。
      </p>
      <div className="v2-domain-grid">
        {Object.entries(names).map(([kind, name]) => (
          <div className="v2-domain" key={kind}>
            <h3>{name}</h3>
            <strong>{items.filter((i) => i.kind === kind).length}</strong>
            <p>当前身份可核对的对象</p>
            <Button
              onClick={() =>
                navigate(
                  kind === "journey"
                    ? "journeys"
                    : kind === "document"
                      ? "documents"
                      : kind === "relation"
                        ? "reviews"
                        : "versions",
                )
              }
            >
              打开{name}
            </Button>
          </div>
        ))}
      </div>
    </Panel>
  );
}
