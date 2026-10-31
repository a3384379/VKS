import "./product-projections.css";
import { Notice, Tag } from "./components";
import { useWorkspace } from "./context";
import { ProductEntityLink } from "./ProductEntityLink";
import { exactResultEntity } from "./product-projections";
import { useFixedWorkspace } from "./useFixedWorkspace";
import { ImpactPaths } from "./ImpactPaths";
import type { Dict, Entity } from "./types";

const object = (value: unknown): Dict =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Dict)
    : {};
const rows = (value: unknown): Dict[] =>
  Array.isArray(value) ? value.filter((v) => v && typeof v === "object") : [];
const text = (value: unknown) =>
  typeof value === "string" || typeof value === "number"
    ? String(value)
    : "未知";
export function TaskResults({
  report,
  result,
}: {
  report?: Dict | null;
  result?: Dict;
}) {
  const { w } = useWorkspace();
  const coverage = object(report?.coverage),
    actions = object(coverage.actions),
    relations = object(coverage.relations),
    health = object(coverage.knowledge_health);
  const references = [
    ...rows(result?.result_refs),
    ...rows(report?.result_refs),
    ...rows(report?.relation_candidates)
      .filter((row) => typeof row.relation_id === "string")
      .map((row) => ({ kind: "relation", id: row.relation_id })),
  ];
  const refs = references.filter(
    (row, i) =>
      references.findIndex(
        (other) => other.kind === row.kind && other.id === row.id,
      ) === i,
  );
  const uncovered = rows(report?.uncovered_actions);
  const relationIds = Array.isArray(report?.outdated_relation_ids)
    ? report.outdated_relation_ids
    : [];
  const source = rows(report?.source_vector || coverage.source_vector);
  const unknowns = [
    ...(Array.isArray(report?.unknowns) ? report.unknowns : []),
    ...(Array.isArray(coverage.unknowns) ? coverage.unknowns : []),
  ];
  const analysisId =
    typeof result?.analysis_set_id === "string"
      ? result.analysis_set_id
      : typeof report?.analysis_set_id === "string"
        ? report.analysis_set_id
        : "";
  const baseline = analysisId ? `draft:${analysisId}` : "";
  const { workspace: target, error: targetError } = useFixedWorkspace(
    baseline,
    JSON.stringify(refs),
  );
  const entities =
    target?.entities ||
    (!analysisId ||
    (!w.selected_release && w.selected_analysis_set?.id === analysisId)
      ? w.entities
      : []);
  const targetRelations =
    target?.relations ||
    (!analysisId ||
    (!w.selected_release && w.selected_analysis_set?.id === analysisId)
      ? w.relations
      : []);
  if (!report && !refs.length) return null;
  return (
    <section className="product-results" aria-label="任务可读结果">
      <h3>任务结果与准确产物</h3>
      {targetError && (
        <Notice tone="red">任务基线读取失败：{targetError}</Notice>
      )}
      {Object.keys(coverage).length > 0 && (
        <>
          <p className="muted">
            {text(coverage.scope)} · 固定基线{" "}
            {analysisId || text(coverage.analysis_set_id)}
          </p>
          <div className="product-result-metrics">
            <div>
              <strong>
                {text(actions.linked)} / {text(actions.total)}
              </strong>
              <span>动作知识覆盖</span>
              <small>有效的知识关联；页面包含关系不计入</small>
            </div>
            <div>
              <strong>
                {text(relations.excluded)} / {text(relations.total)}
              </strong>
              <span>已移除关联</span>
            </div>
            <div>
              <strong>
                {text(health.unverified_runtime_actions)} /{" "}
                {text(health.action_denominator)}
              </strong>
              <span>运行证据未核对动作</span>
            </div>
          </div>
        </>
      )}
      {source.length > 0 && (
        <details>
          <summary>固定来源版本 · {source.length} 项</summary>
          <ul className="product-result-list">
            {source.map((s, i) => (
              <li key={i}>
                {text(s.name || s.role || s.provider)} ·{" "}
                {text(s.provider_revision || s.source_snapshot_id)}
                {typeof s.content_sha256 === "string" && (
                  <small> · 摘要 {s.content_sha256}</small>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
      {!!report?.scope && (
        <details>
          <summary>输入范围与分母</summary>
          <ul className="product-result-list">
            {Object.entries(object(report.scope)).map(([kind, value]) => {
              const scope = object(value);
              return (
                <li key={kind}>
                  {(
                    {
                      entities: "知识实体",
                      pages: "页面",
                      actions: "动作",
                      scenarios: "采集场景",
                      documents: "文档",
                      relations: "关联",
                      evidence: "证据",
                    } as Record<string, string>
                  )[kind] || kind}
                  ：纳入 {text(scope.included)} / 可见 {text(scope.total)}
                  {scope.is_truncated === true && <Tag>输入已截断</Tag>}
                </li>
              );
            })}
          </ul>
        </details>
      )}
      {report?.is_truncated === true && (
        <Notice tone="amber">
          本任务输入已截断；未纳入材料的知识覆盖与缺口尚未评估。
        </Notice>
      )}
      {Array.isArray(report?.uncovered_actions) && (
        <>
          <h4>待补充知识的动作 · {uncovered.length}</h4>
          <ul className="product-result-list">
            {uncovered.map((row, i) => {
              const entity = entities.find(
                (e) =>
                  e.entity_id === row.entity_id &&
                  e.entity_revision_id === row.revision_id,
              );
              return (
                <li key={text(row.revision_id) + i}>
                  {entity ? (
                    <ProductEntityLink
                      entity={entity}
                      baseline={baseline || undefined}
                    />
                  ) : (
                    <span>{text(row.name)} · 准确修订不在当前可见版本中</span>
                  )}
                </li>
              );
            })}
          </ul>
          {!uncovered.length && (
            <p className="muted">
              本任务纳入的动作未发现知识关联缺口；完整性以输入分母和截断标记为准。
            </p>
          )}
        </>
      )}
      {relationIds.length > 0 && (
        <>
          <h4>需核对关联 · {relationIds.length}</h4>
          <div className="product-module-cards">
            {relationIds.slice(0, 30).map((id) => {
              const relation = targetRelations.find((r) => r.id === id);
              return relation ? (
                <ProductEntityLink
                  key={relation.id}
                  entity={{ id: relation.id, type: "relation" } as Entity}
                  baseline={baseline || undefined}
                >
                  {relation.from_name} → {relation.to_name}
                </ProductEntityLink>
              ) : (
                <span key={text(id)}>准确关联不在当前可见版本中</span>
              );
            })}
          </div>
          {relationIds.length > 30 && (
            <Notice>当前展示前 30 条需核对关联。</Notice>
          )}
        </>
      )}
      {refs.some((ref) =>
        [
          "document_revision",
          "journey_revision",
          "section_revision",
          "relation",
        ].includes(text(ref.kind)),
      ) && (
        <>
          <h4>本任务生成的产物</h4>
          <ul className="product-result-list">
            {refs
              .filter((ref) =>
                [
                  "document_revision",
                  "journey_revision",
                  "section_revision",
                  "relation",
                ].includes(text(ref.kind)),
              )
              .map((ref) => {
                const entity = exactResultEntity(
                  entities,
                  text(ref.kind),
                  text(ref.id),
                );
                const relation =
                  ref.kind === "relation"
                    ? targetRelations.find((r) => r.id === ref.id)
                    : undefined;
                return (
                  <li key={text(ref.kind) + text(ref.id)}>
                    {entity ? (
                      <ProductEntityLink
                        entity={entity}
                        baseline={baseline || undefined}
                      />
                    ) : relation ? (
                      <ProductEntityLink
                        entity={{ id: relation.id, type: "relation" } as Entity}
                        baseline={baseline || undefined}
                      >
                        {relation.from_name} → {relation.to_name}
                      </ProductEntityLink>
                    ) : (
                      <Notice>
                        产物的准确修订不在当前可见版本中，请切换到任务基线或刷新；不会打开同名旧产物。
                      </Notice>
                    )}
                    <small> · {text(ref.kind)}</small>
                  </li>
                );
              })}
          </ul>
        </>
      )}
      {Object.keys(object(report?.change_report)).length > 0 && (
        <ImpactPaths report={object(report?.change_report)} />
      )}
      {unknowns.length > 0 && (
        <Notice>
          {[
            ...new Set(
              unknowns.map((value) =>
                typeof value === "string"
                  ? value
                  : text(
                      object(value).message ||
                        object(value).reason ||
                        object(value).code,
                    ),
              ),
            ),
          ].join("；")}
        </Notice>
      )}
    </section>
  );
}
