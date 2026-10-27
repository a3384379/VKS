import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { api } from "./api";
import { Button, Empty, Notice, Tag } from "./components";
import { useWorkspace } from "./context";
import type { Entity, Evidence, Relation } from "./types";
type Explanation = {
  action_revision_id: string;
  source_alignment: string;
  entities: Entity[];
  relations: Relation[];
  evidence: Evidence[];
  paths: {
    revision_ids: string[];
    relation_ids: string[];
    causal_basis: string[];
    interpretation: string;
  }[];
  unknowns: string[];
  is_truncated: boolean;
};
export function ActionChain({ actionId }: { actionId: string }) {
  const { w, baseline, navigate } = useWorkspace();
  const [data, setData] = useState<Explanation | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError("");
    if (!actionId) return;
    const q = new URLSearchParams({ project_id: w.project.id });
    if (w.selected_release) q.set("release_id", w.selected_release.id);
    else if (w.selected_analysis_set)
      q.set("analysis_set_id", w.selected_analysis_set.id);
    void api<Explanation>(`/api/v2/actions/${actionId}/explanation?${q}`, {
      signal: controller.signal,
    })
      .then(setData)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [
    actionId,
    w.project.id,
    baseline,
    w.selected_analysis_set?.selection_version,
  ]);
  return (
    <Panel title="当前动作的技术关联路径" action={<Tag>同版 · 有界 3 跳</Tag>}>
      {error ? (
        <Notice tone="red">{error}</Notice>
      ) : !data ? (
        <p className="muted">正在读取同版关联…</p>
      ) : (
        <>
          <div className="row wrap">
            <Tag>运行对齐：{data.source_alignment}</Tag>
            <Tag>审核、有效性、证据与因果分别保存</Tag>
          </div>
          {data.paths.length ? (
            <div className="v2-paths">
              {data.paths.slice(0, 8).map((path, i) => (
                <div key={i} className="v2-path">
                  <div className="row wrap">
                    {path.revision_ids.map((rid, j) => {
                      const node = data.entities.find(
                        (e) => e.entity_revision_id === rid,
                      );
                      return node ? (
                        <span className="row" key={rid}>
                          {j > 0 && <ArrowRight size={13} />}
                          <button
                            onClick={() =>
                              navigate(
                                node.type === "document" ||
                                  node.type === "section"
                                  ? "documents"
                                  : "graph",
                                node,
                              )
                            }
                          >
                            <small>{node.type}</small>
                            <strong>{node.name}</strong>
                          </button>
                        </span>
                      ) : null;
                    })}
                  </div>
                  <small>
                    {path.causal_basis.join(" → ")} · {path.interpretation}
                  </small>
                </div>
              ))}
            </div>
          ) : (
            <Empty
              title="尚无已确认且有效的技术路径"
              description="可在右侧查看候选，再对照原始证据审核。没有运行 Trace 时不补画执行链。"
            />
          )}
          {(data.is_truncated || data.paths.length > 8) && (
            <Notice>路径已截断，可在知识地图中继续按范围查询。</Notice>
          )}
          <p className="muted">{data.unknowns.at(-1)}</p>
          <Button onClick={() => navigate("journeys")}>
            在流程画布组织当前知识
          </Button>
        </>
      )}
    </Panel>
  );
}
