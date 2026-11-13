import "./product-projections.css";
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import {
  Button,
  EvidenceList,
  Notice,
  RelationState,
  Spinner,
  Tag,
} from "./components";
import { useWorkspace } from "./context";
import { ProductEntityLink } from "./ProductEntityLink";
import { useFixedWorkspace } from "./useFixedWorkspace";
import type { Dict, Entity, Relation } from "./types";

const objects = (value: unknown): Dict[] =>
  Array.isArray(value) ? value.filter((v) => v && typeof v === "object") : [];
const obj = (value: unknown): Dict =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Dict)
    : {};
const value = (v: unknown) =>
  typeof v === "string" || typeof v === "number" ? String(v) : "未知";
export function ImpactPaths({
  report,
  entityId,
}: {
  report: Dict;
  entityId?: string;
}) {
  const from =
    typeof report.from_release_id === "string"
      ? `release:${report.from_release_id}`
      : "";
  const to =
    typeof report.to_analysis_set_id === "string"
      ? `draft:${report.to_analysis_set_id}`
      : "";
  const { workspace: before, error: beforeError } = useFixedWorkspace(from);
  const { workspace: after, error: afterError } = useFixedWorkspace(to);
  const changes = objects(report.changes).filter(
    (change) => !entityId || change.entity_id === entityId,
  );
  const bindings = objects(report.binding_changes).filter(
    (change) =>
      !entityId ||
      change.from_entity_id === entityId ||
      change.to_entity_id === entityId,
  );
  const paths = objects(report.impact_paths).filter(
    (path) => !entityId || path.changed_entity_id === entityId,
  );
  const affected = new Set([
    ...(entityId ? [entityId] : changes.map((c) => value(c.entity_id))),
    ...paths.map((path) => value(path.impacted_entity_id)),
  ]);
  const journeys = objects(report.journey_impacts).filter(
    (impact) => !entityId || affected.has(value(impact.binding_entity_id)),
  );
  const scope = obj(report.impact_scope);
  const stale =
    typeof report.to_selection_version === "number" &&
    !!after &&
    after.selected_analysis_set?.selection_version !==
      report.to_selection_version;
  function find(
    id: unknown,
    revision?: unknown,
  ): { entity: Entity; baseline: string } | null {
    // Resolve the target baseline before deciding an object exists only in history.
    if (!after || !before) return null;
    const current = after?.entities.find(
      (e) =>
        e.entity_id === id && (!revision || e.entity_revision_id === revision),
    );
    if (current) return { entity: current, baseline: to };
    const previous = before?.entities.find(
      (e) =>
        e.entity_id === id && (!revision || e.entity_revision_id === revision),
    );
    return previous ? { entity: previous, baseline: from } : null;
  }
  function link(id: unknown, revision?: unknown, stepKey?: string) {
    if (stale)
      return <span className="muted">目标基线已变化，请重新比较后打开</span>;
    if (report.to_selection_version === undefined && !revision)
      return (
        <span className="muted">
          历史报告未固定目标选择版本，请重新比较后打开
        </span>
      );
    const target = find(id, revision);
    return target ? (
      <ProductEntityLink
        entity={target.entity}
        baseline={target.baseline}
        selectionVersion={
          target.baseline === to &&
          typeof report.to_selection_version === "number"
            ? report.to_selection_version
            : undefined
        }
        stepKey={stepKey}
      />
    ) : (
      <span className="muted">准确对象不在当前可见版本中</span>
    );
  }
  function bindingSide(row: Dict, workspace: typeof before, baseline: string) {
    if (!Object.keys(row).length) return <p className="muted">无关联</p>;
    const binding = row as unknown as Relation;
    const detail = obj(row.prototype_scope);
    const states = Array.isArray(detail.states) ? detail.states.map(value) : [];
    return (
      <div>
        <div className="product-impact-route">
          {[row.from_revision_id, row.to_revision_id].map((id, index) => {
            const endpoint = workspace?.entities.find(
              (entry) => entry.entity_revision_id === id,
            );
            return (
              <span key={index}>
                {index === 1 && <span aria-hidden="true"> → </span>}
                {endpoint && !stale ? (
                  <ProductEntityLink
                    entity={endpoint}
                    baseline={baseline}
                    selectionVersion={
                      baseline === to &&
                      typeof report.to_selection_version === "number"
                        ? report.to_selection_version
                        : undefined
                    }
                  />
                ) : (
                  <span className="muted">准确对象暂不可定位</span>
                )}
              </span>
            );
          })}
        </div>
        <RelationState relation={binding} />
        {row.prototype_scope != null && (
          <p>
            适用状态 {states.length ? states.join("、") : "全部"} · 优先级{" "}
            {value(detail.priority)}
          </p>
        )}
      </div>
    );
  }
  const relations = new Map<string, Relation>(
    [
      ...(before?.relations || []),
      ...(after?.relations || []),
      ...(objects(report.impacted_relations) as unknown as Relation[]),
    ].map((r) => [r.id, r]),
  );
  const sources = obj(report.source_vectors);
  return (
    <section className="product-impact" aria-label="业务影响路径">
      <h3>业务影响路径</h3>
      <p>
        固定版本：
        {before?.selected_release?.release_no ||
          value(report.from_release_id)}{" "}
        →{" "}
        {after?.selected_analysis_set?.name || value(report.to_analysis_set_id)}
      </p>
      <div className="product-result-metrics">
        <div>
          <strong>
            {changes.filter((c) => c.change_kind !== "non_semantic").length}
          </strong>
          <span>直接材料变化</span>
          <small>材料变化尚不等于业务含义改变</small>
        </div>
        <div>
          <strong>
            {paths.length} 条 / {journeys.length} 项
          </strong>
          <span>潜在关联路径 / 流程引用</span>
        </div>
        <div>
          <strong>
            {changes.filter((c) => c.change_kind === "non_semantic").length}
          </strong>
          <span>纯注释或格式变化</span>
        </div>
      </div>
      {Object.keys(scope).length ? (
        <Notice>
          后端范围：最多 {value(scope.max_hops)} 跳，每变化最多{" "}
          {value(scope.max_nodes_per_change)} 个节点；
          {scope.is_truncated === true
            ? "后端结果已截断"
            : scope.is_truncated === false
              ? "本次在声明边界内未截断"
              : "后端截断状态未知"}
          。
          {scope.journey_limit
            ? `流程引用上限 ${value(scope.journey_limit)} 项。`
            : ""}
        </Notice>
      ) : (
        <Notice>
          此历史报告未提供后端遍历边界与截断标记，结果完整性未知。
        </Notice>
      )}
      {stale && (
        <Notice tone="amber">
          目标分析基线的选择版本已变化，旧报告的对象入口已停用。请重新比较固定版本。
        </Notice>
      )}
      {report.to_selection_version === undefined && (
        <p className="muted">
          历史报告未记录目标选择版本，打开前请重新计算以核对准确修订。
        </p>
      )}
      {(beforeError || afterError) && (
        <Notice tone="red">
          固定版本详情读取失败：{beforeError || afterError}
        </Notice>
      )}
      {!before && !after && !beforeError && !afterError && <Spinner />}
      <details>
        <summary>来源版本与范围</summary>
        {(
          [
            [
              "旧版",
              Array.isArray(sources.from)
                ? objects(sources.from)
                : before?.sources || [],
            ],
            [
              "新版",
              Array.isArray(sources.to)
                ? objects(sources.to)
                : after?.sources || [],
            ],
          ] as const
        ).map(([title, source]) => (
          <section key={title}>
            <h4>
              {title}来源 · {source.length}
            </h4>
            <ul className="product-result-list">
              {source.map((row, i) => (
                <li key={i}>
                  {value(row.name || row.role || row.provider)} ·{" "}
                  {value(row.provider_revision || row.source_snapshot_id)}
                </li>
              ))}
            </ul>
            {!source.length && <p>来源范围尚未读取或当前无可见来源。</p>}
          </section>
        ))}
      </details>
      {bindings.length > 0 && (
        <section aria-label="原型关联变更">
          <h4>原型关联变更 · {bindings.length} 项</h4>
          {bindings.map((change) => (
            <article
              key={`${value(change.relation_type)}:${value(change.from_entity_id)}:${value(change.to_entity_id)}`}
              className="proposal-card"
            >
              <Tag>
                {{ added: "新增", modified: "更新", removed: "移除" }[
                  value(change.change_kind)
                ] || "变更"}
              </Tag>
              <Tag>
                {
                  {
                    documents: "动作与相关文档",
                    has_business_point: "动作与业务点",
                    supported_by_section: "业务点与原文",
                  }[value(change.relation_type)]
                }
              </Tag>
              <details>
                <summary>查看关联的原版本和目标基线</summary>
                <h5>原版本</h5>
                {bindingSide(obj(change.before), before, from)}
                <h5>目标基线</h5>
                {bindingSide(obj(change.after), after, to)}
              </details>
            </article>
          ))}
        </section>
      )}
      <h4>
        潜在影响关联 · 显示 {Math.min(paths.length, 30)} / {paths.length} 条
      </h4>
      {paths.slice(0, 30).map((path, i) => {
        const ids = Array.isArray(path.entity_ids) ? path.entity_ids : [];
        const edgeIds = Array.isArray(path.relation_ids)
          ? path.relation_ids
          : [];
        const edges = edgeIds
          .map((id) => relations.get(String(id)))
          .filter(Boolean) as Relation[];
        const evidenceIds = new Set(edges.flatMap((edge) => edge.evidence_ids));
        const evidence = [
          ...new Map(
            [...(before?.evidence || []), ...(after?.evidence || [])]
              .filter((item) => evidenceIds.has(item.id))
              .map((item) => [item.id, item]),
          ).values(),
        ];
        return (
          <article
            className="product-impact-path"
            key={i}
            data-impact-target={value(path.impacted_entity_id)}
          >
            <Tag>潜在影响 · {value(path.depth)} 跳</Tag>
            <div className="product-impact-route">
              {ids.map((id, index) => (
                <span key={value(id)} data-impact-entity={value(id)}>
                  {index > 0 && <span aria-hidden="true"> ↔ </span>}
                  {link(id)}
                </span>
              ))}
            </div>
            <details>
              <summary>路径关系与证据 · {edgeIds.length} 段</summary>
              {edges.map((edge) => (
                <div key={edge.id}>
                  <p>
                    {edge.from_name} → {edge.to_name} ·{" "}
                    {edge.relation_type || edge.type}
                  </p>
                  <RelationState relation={edge} />
                </div>
              ))}
              {edges.length < edgeIds.length && (
                <Notice>部分关系详情不在当前可见版本中，未补造证据。</Notice>
              )}
              <EvidenceList evidence={evidence} />
            </details>
          </article>
        );
      })}
      {!paths.length && (
        <p className="muted">
          当前报告没有返回可见的关联影响路径；未证明其他对象不受影响。
        </p>
      )}
      {paths.length > 30 && (
        <Notice>
          前端列表已截断，当前显示前 30 / {paths.length} 条可见路径。
        </Notice>
      )}
      <h4>
        受影响流程引用 · 显示 {Math.min(journeys.length, 20)} /{" "}
        {journeys.length} 项
      </h4>
      <ul className="product-result-list">
        {journeys.slice(0, 20).map((impact, i) => (
          <li key={i}>
            <div className="product-impact-route">
              {link(impact.binding_entity_id, impact.binding_revision_id)}
              <span>关联至</span>
              {link(
                impact.journey_id,
                impact.journey_revision_id,
                impact.step_key === "semantics"
                  ? undefined
                  : value(impact.step_key),
              )}
            </div>
            <p>
              准确步骤：
              {impact.step_key === "semantics"
                ? "流程来源"
                : value(impact.step_key)}{" "}
              ·{" "}
              {impact.basis === "explicit_journey_binding"
                ? "显式步骤绑定"
                : "显式流程来源"}{" "}
              · 需核对业务影响
            </p>
          </li>
        ))}
      </ul>
      {journeys.length > 20 && (
        <Notice>
          前端流程引用列表已截断，当前显示前 20 / {journeys.length} 项。
        </Notice>
      )}
      {Array.isArray(report.unknowns) && report.unknowns.length > 0 && (
        <Notice>{report.unknowns.map(value).join("；")}</Notice>
      )}
    </section>
  );
}

export function ProposalImpactPaths({
  entityId,
  fromRelease,
}: {
  entityId?: string;
  fromRelease?: string;
}) {
  const { w } = useWorkspace();
  const key = `${w.project.id}:${fromRelease}:${w.selected_analysis_set?.id}:${w.selected_analysis_set?.selection_version}`;
  const current = useRef(key);
  current.current = key;
  const [report, setReport] = useState<Dict | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    setReport(null);
    setBusy(false);
    setError("");
  }, [key]);
  async function load() {
    if (!fromRelease || !w.selected_analysis_set) return;
    const requested = key;
    setBusy(true);
    setError("");
    try {
      const result = await api<Dict>(
        `/projects/${w.project.id}/releases/${fromRelease}/diff?to_analysis_set_id=${w.selected_analysis_set.id}`,
      );
      if (current.current === requested) setReport(result);
    } catch (e) {
      if (current.current === requested) setError((e as Error).message);
    } finally {
      if (current.current === requested) setBusy(false);
    }
  }
  if (!fromRelease) return null;
  return (
    <div>
      <Button kind="small" disabled={busy} onClick={() => void load()}>
        {busy ? "读取影响路径…" : "查看业务影响路径"}
      </Button>
      {error && <Notice tone="red">{error}</Notice>}
      {report && <ImpactPaths report={report} entityId={entityId} />}
    </div>
  );
}
