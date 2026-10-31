import type { Entity } from "./types";

export type PlannedJourney = {
  purpose: string;
  preconditions: string[];
  outcome: string;
  entry: string;
  source_revision_ids: string[];
  nodes: {
    key: string;
    kind: string;
    title: string;
    description: string | null;
    target_revision_id: string | null;
  }[];
  edges: { source: string; target: string; label: string }[];
};
const kinds: Record<string, string> = {
  page: "页面",
  action: "动作",
  step: "业务说明",
  decision: "判断",
  branch: "分支",
  end: "结束",
  reference: "资料引用",
};

export function JourneyPlanPreview({
  journey,
  entities,
}: {
  journey: PlannedJourney;
  entities: Entity[];
}) {
  function reference(revisionId: string) {
    const entity = entities.find(
      (row) => row.entity_revision_id === revisionId,
    );
    return (
      <span>
        {entity?.name || "准确输入修订当前不可见"} · <code>{revisionId}</code>
      </span>
    );
  }
  return (
    <section className="v2-journey-plan" aria-label="计划中的业务流程">
      <h4>业务流程候选 · 推断依据</h4>
      <p>确认此计划只授权生成候选；结构与条件仍需对照原始资料核对。</p>
      <h5>业务目标</h5>
      <p>{journey.purpose}</p>
      <h5>前置条件</h5>
      {journey.preconditions.length ? (
        <ul>
          {journey.preconditions.map((text, i) => (
            <li key={i}>{text}</li>
          ))}
        </ul>
      ) : (
        <p>计划未说明，不应假定角色、权限或数据条件。</p>
      )}
      <h5>预期结果</h5>
      <p>{journey.outcome}</p>
      <h5>步骤与继续条件</h5>
      <ul>
        {journey.nodes.map((node) => (
          <li key={node.key}>
            <strong>{node.title}</strong> · {kinds[node.kind] || node.kind}
            {node.key === journey.entry && " · 起点"}
            {node.description && <p>{node.description}</p>}
            {node.target_revision_id && (
              <p>绑定资料：{reference(node.target_revision_id)}</p>
            )}
            {journey.edges
              .filter((edge) => edge.source === node.key)
              .map((edge, index) => (
                <p key={index}>
                  {edge.label || "继续条件未说明"} →{" "}
                  {journey.nodes.find((target) => target.key === edge.target)
                    ?.title || edge.target}
                </p>
              ))}
          </li>
        ))}
      </ul>
      <h5>准确依据资料</h5>
      <ul>
        {journey.source_revision_ids.map((revisionId) => (
          <li key={revisionId}>{reference(revisionId)}</li>
        ))}
      </ul>
    </section>
  );
}
