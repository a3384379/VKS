import type { Entity } from "./types";
import type { JourneyDSL, JourneyNode, JourneySemantics } from "./journeys";

export const journeyKinds: Record<
  JourneySemantics["kind"],
  { label: string; plural: string; description: string }
> = {
  business_process: {
    label: "业务场景",
    plural: "业务场景",
    description:
      "围绕一个业务目标阅读步骤、条件与结果。这里的业务说明仍需结合来源核对。",
  },
  operation_sequence: {
    label: "操作序列",
    plural: "操作序列",
    description: "保存的操作顺序不代表完整业务规则或所有成功、失败路径。",
  },
  evidence_navigation: {
    label: "证据导航",
    plural: "证据材料",
    description: "这些动作和知识供独立查阅，条目之间没有业务先后含义。",
  },
  observation_comparison: {
    label: "观察对比",
    plural: "观察对比",
    description: "比较已保存的观察状态；状态差异不证明业务条件或执行因果。",
  },
  unclassified: {
    label: "待分类材料",
    plural: "待分类材料",
    description:
      "这份材料尚未说明业务目标和流程性质，原图连接不能证明真实业务顺序。",
  },
};
export const journeyBases: Record<JourneySemantics["basis"], string> = {
  declared: "人员声明",
  observed: "保存的观察",
  inferred: "推断候选",
  unknown: "依据未说明",
};

// No title, review status, node coordinates or entity name can establish business meaning.
export function journeySemantics(dsl: JourneyDSL): JourneySemantics {
  return dsl.semantics || { kind: "unclassified", basis: "unknown" };
}

export function journeyPath(dsl: JourneyDSL, choices: Record<string, string>) {
  const path: JourneyNode[] = [];
  const seen = new Set<string>();
  let key: string | undefined = dsl.entry;
  let branches: JourneyDSL["edges"] = [];
  while (key && !seen.has(key) && path.length < 120) {
    const node = dsl.nodes.find((n) => n.key === key);
    if (!node) break;
    seen.add(key);
    path.push(node);
    const outgoing = dsl.edges.filter((e) => e.source === key);
    if (outgoing.length > 1) {
      const choice = outgoing.find((e) => e.key === choices[node.key]);
      if (!choice) {
        branches = outgoing;
        break;
      }
      key = choice.target;
    } else key = outgoing[0]?.target;
  }
  return { path, branches };
}

export type JourneyTarget =
  | {
      kind: "capture";
      reference: NonNullable<JourneyNode["capture_ref"]>;
      entity: Entity;
    }
  | {
      kind: "state";
      reference: NonNullable<JourneyNode["state_ref"]>;
      entity: Entity;
    }
  | { kind: "entity"; entity: Entity }
  | { kind: "unavailable"; reason: string };

/** Navigation is passive and must stay within the exact visible baseline selection. */
export function journeyTarget(
  node: JourneyNode,
  entities: Entity[],
): JourneyTarget {
  if (node.capture_ref || node.state_ref) {
    const ref = node.capture_ref || node.state_ref!;
    const scenario = entities.find(
      (e) =>
        e.entity_id === ref.scenario_id &&
        e.entity_revision_id === ref.scenario_revision_id &&
        e.type === "scenario",
    );
    if (!scenario)
      return {
        kind: "unavailable",
        reason: "此状态的准确来源修订不在当前可见版本中",
      };
    return node.capture_ref
      ? { kind: "capture", reference: node.capture_ref, entity: scenario }
      : { kind: "state", reference: node.state_ref!, entity: scenario };
  }
  if (!node.binding)
    return {
      kind: "unavailable",
      reason:
        node.kind === "step"
          ? "声明步骤 · 尚无独立动作绑定，请结合本场景的来源材料核对"
          : "尚未关联页面、动作或知识来源",
    };
  const entity = entities.find(
    (e) =>
      e.entity_id === node.binding!.entity_id &&
      e.entity_revision_id === node.binding!.revision_id,
  );
  return entity
    ? { kind: "entity", entity }
    : { kind: "unavailable", reason: "此条目的准确来源修订不在当前可见版本中" };
}

export function journeySource(node: JourneyNode, entities: Entity[]) {
  const target = journeyTarget(node, entities);
  if (target.kind === "unavailable")
    return {
      available: false,
      label: target.reason,
      action: "尚无可打开的来源",
    };
  if (target.kind === "capture")
    return {
      available: true,
      label: `录制快照 · ${target.entity.name}`,
      action: "查看保存的快照",
    };
  if (target.kind === "state")
    return {
      available: true,
      label: `保存状态 · ${target.entity.name}`,
      action: "查看页面状态",
    };
  const types: Record<string, string> = {
    page: "页面",
    action: "动作",
    document: "文档",
    section: "文档章节",
    api: "接口",
    symbol: "实现",
    sql: "SQL",
    table: "数据对象",
    scenario: "场景",
  };
  const type = types[target.entity.type] || "知识";
  return {
    available: true,
    label: `${type} · ${target.entity.name}`,
    action: `打开同版${type}`,
  };
}
