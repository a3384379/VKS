import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useState } from "react";
import { ArrowRight } from "lucide-react";
import { api } from "./api";
import {
  Button,
  Empty,
  EvidenceList,
  Notice,
  RelationState,
  Tag,
  label,
} from "./components";
import { useReadScope, useWorkspace } from "./context";
import { ProductEntityLink } from "./ProductEntityLink";
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
  const { w, navigate } = useWorkspace();
  const scope = `${useReadScope()}:${actionId}`;
  const [result, setData] = useState<{
      scope: string;
      value: Explanation;
    } | null>(null),
    [error, setError] = useState("");
  const data = result?.scope === scope ? result.value : null;
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
      .then((value) => {
        if (!controller.signal.aborted) setData({ scope, value });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [scope]);
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
            <Tag>用户取舍、有效性、证据与因果分别保存</Tag>
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
                          <ProductEntityLink entity={node}>
                            <small>{label(node.type)}</small>
                            <strong>{node.name}</strong>
                          </ProductEntityLink>
                        </span>
                      ) : null;
                    })}
                  </div>
                  <small>{path.interpretation}</small>
                  <details>
                    <summary>
                      查看 {path.relation_ids.length} 段关系与来源证据
                    </summary>
                    {path.relation_ids.map((id, index) => {
                      const relation = data.relations.find(
                        (row) => row.id === id,
                      );
                      if (!relation) return null;
                      const from = data.entities.find(
                        (row) =>
                          row.entity_revision_id === relation.from_revision_id,
                      );
                      const to = data.entities.find(
                        (row) =>
                          row.entity_revision_id === relation.to_revision_id,
                      );
                      return (
                        <section key={id} aria-label={`技术关系 ${index + 1}`}>
                          <h4>
                            {label(relation.relation_type || relation.type)}
                          </h4>
                          <p>
                            资料中的方向：{from?.name || "准确修订"} →{" "}
                            {to?.name || "准确修订"}
                          </p>
                          <RelationState relation={relation} />
                          <Tag value={relation.causal_basis} />
                          <p>{relation.reason}</p>
                          <EvidenceList
                            evidence={data.evidence.filter((row) =>
                              relation.evidence_ids.includes(row.id),
                            )}
                          />
                        </section>
                      );
                    })}
                  </details>
                </div>
              ))}
            </div>
          ) : (
            <Empty
              title="尚无有效的技术路径"
              description="可在右侧查看关联并对照原始证据。没有运行 Trace 时不补画执行链。"
            />
          )}
          {(data.is_truncated || data.paths.length > 8) && (
            <Notice>路径已截断，可在知识地图中继续按范围查询。</Notice>
          )}
          <p className="muted">{data.unknowns.at(-1)}</p>
          <Button onClick={() => navigate("journeys")}>打开业务导览</Button>
        </>
      )}
    </Panel>
  );
}
