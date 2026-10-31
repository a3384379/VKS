import { useState } from "react";
import { ArrowRight, BookOpen, Check, GitBranch, Search } from "lucide-react";
import { Empty, Notice, Tag } from "./components";
import type { Entity } from "./types";
import type {
  JourneyDSL,
  JourneyNode,
  JourneyRevision,
  JourneySemantics,
} from "./journeys";
import {
  journeyBases,
  journeyKinds,
  journeyPath,
  journeySemantics,
  journeySource,
} from "./journey-reading";
import { JourneyStepContext } from "./JourneyStepContext";
import {
  resolveJourneyReading,
  type JourneyNavigation,
} from "./journey-navigation";

type Props = {
  rows: JourneyRevision[];
  active: JourneyRevision | null;
  dsl?: JourneyDSL;
  entities: Entity[];
  loaded: boolean;
  dirty: boolean;
  onSelect: (row: JourneyRevision) => void;
  onOpen: (node: JourneyNode) => void;
  navigation: JourneyNavigation;
  onNavigate: (navigation: JourneyNavigation) => void;
};

function ReadingContent({
  dsl,
  entities,
  onOpen,
  active,
  dirty,
  navigation,
  onNavigate,
}: Pick<
  Props,
  "entities" | "onOpen" | "active" | "dirty" | "navigation" | "onNavigate"
> & { dsl: JourneyDSL }) {
  const [preview, setPreview] = useState({
    stepKey: dsl.entry,
    choices: {} as Record<string, string>,
  });
  const reading = resolveJourneyReading(
    dsl,
    active?.revision_id || "",
    navigation,
  );
  const { choices, stepKey: selectedStep } = dirty ? preview : reading;
  function select(stepKey: string, nextChoices = choices) {
    if (dirty || !active) setPreview({ stepKey, choices: nextChoices });
    else
      onNavigate({
        journeyId: active.journey_id,
        revisionId: active.revision_id,
        stepKey,
        choices: nextChoices,
      });
  }
  const semantics = journeySemantics(dsl);
  const ordered =
    semantics.kind === "business_process" ||
    semantics.kind === "operation_sequence";
  const { path, branches } = journeyPath(dsl, choices);
  const material = dsl.nodes.filter(
    (node) =>
      node.binding || node.state_ref || node.capture_ref || node.description,
  );
  const nodes = ordered ? path : material.length ? material : dsl.nodes;
  const selectedNode =
    nodes.find((node) => node.key === selectedStep) || nodes[0];
  const chosenBranches = path.filter((node) =>
    Object.hasOwn(choices, node.key),
  );
  const bindings = dsl.nodes.filter(
    (node) => node.binding || node.state_ref || node.capture_ref,
  );
  const openSources = bindings.filter(
    (node) => journeySource(node, entities).available,
  ).length;
  const declaredSteps = dsl.nodes.filter((node) => node.kind === "step").length;

  function card(node: JourneyNode, index: number) {
    const source = journeySource(node, entities);
    return (
      <li key={node.key} className="journey-reading-step">
        <div className="journey-reading-marker" aria-hidden="true">
          {ordered ? index + 1 : <BookOpen size={17} />}
        </div>
        <div>
          <button
            className="journey-reading-open"
            aria-pressed={selectedNode?.key === node.key}
            onClick={() => select(node.key)}
          >
            <strong>{node.title}</strong>
            {source.available && <ArrowRight size={16} />}
          </button>
          {node.description && <p>{node.description}</p>}
          <div className="journey-reading-source">
            <span>
              {node.kind === "decision" ||
              node.kind === "branch" ||
              node.kind === "end"
                ? "条件与结果声明 · 请结合步骤说明和本场景依据核对"
                : `来源：${source.label}`}
            </span>
            {source.available && <span>{source.action}</span>}
          </div>
          {ordered && node.kind === "end" && (
            <span className="journey-reading-terminal">
              <Check size={13} /> 此路径结束
            </span>
          )}
          {ordered &&
            dsl.edges.filter((e) => e.source === node.key).length === 1 &&
            dsl.edges.find((e) => e.source === node.key)?.label && (
              <p className="journey-reading-transition">
                继续条件：{dsl.edges.find((e) => e.source === node.key)!.label}
              </p>
            )}
        </div>
      </li>
    );
  }

  return (
    <article className="journey-reading-content" aria-label="当前导览">
      <header className="journey-reading-heading">
        <div>
          <div className="journey-reading-tags">
            <Tag>{journeyKinds[semantics.kind].label}</Tag>
            <Tag>{journeyBases[semantics.basis]}</Tag>
          </div>
          <h2>{dsl.title}</h2>
        </div>
        <span className="journey-reading-count">
          {openSources}/{bindings.length} 个直接绑定可读
          {declaredSteps > 0 && ` · ${declaredSteps} 个声明步骤`}
        </span>
      </header>
      <p className="journey-reading-boundary">
        {journeyKinds[semantics.kind].description}
      </p>
      {semantics.kind === "unclassified" && (
        <Notice>
          现有材料不足以作为业务流程导览。请先补充真实场景的目标、前置条件、步骤及结果，并核对来源。
        </Notice>
      )}
      {semantics.kind === "business_process" && (
        <Notice>
          这是{journeyBases[semantics.basis]}
          的业务说明；结构保存不代表真实业务验收完成。
        </Notice>
      )}
      <div className="journey-reading-context">
        <section>
          <h3>
            {semantics.kind === "business_process" ? "业务目标" : "材料用途"}
          </h3>
          <p>{semantics.purpose || "尚未说明"}</p>
        </section>
        <section>
          <h3>前置条件</h3>
          {semantics.preconditions?.length ? (
            <ul>
              {semantics.preconditions.map((value, i) => (
                <li key={i}>{value}</li>
              ))}
            </ul>
          ) : (
            <p>尚未记录，不能假定角色、权限或数据条件。</p>
          )}
        </section>
        <section>
          <h3>
            {semantics.kind === "business_process" ? "预期结果" : "结果说明"}
          </h3>
          <p>{semantics.outcome || "尚未说明，不能据此判断业务成功。"}</p>
        </section>
      </div>
      {!!semantics.source_refs?.length && (
        <details
          className="journey-reading-support"
          open={semantics.source_refs.length <= 3}
        >
          <summary>本场景的依据资料</summary>
          <ul>
            {semantics.source_refs.map((binding, index) => {
              const entity = entities.find(
                (item) =>
                  item.entity_id === binding.entity_id &&
                  item.entity_revision_id === binding.revision_id,
              );
              return (
                <li key={index}>
                  {entity ? (
                    <button
                      onClick={() =>
                        onOpen({
                          key: "support",
                          kind:
                            entity.type === "page"
                              ? "page"
                              : entity.type === "action"
                                ? "action"
                                : "reference",
                          title: entity.name,
                          x: 0,
                          y: 0,
                          binding,
                        })
                      }
                    >
                      {entity.name}
                      <ArrowRight size={14} />
                    </button>
                  ) : (
                    <span>一项准确来源当前不可读；未替换为其他修订。</span>
                  )}
                </li>
              );
            })}
          </ul>
        </details>
      )}
      <div className="journey-reading-explorer">
        <section
          className="journey-reading-path"
          aria-label={ordered ? "步骤与分支" : "可读材料"}
        >
          <div className="journey-reading-section-heading">
            <h3>{ordered ? "步骤与分支" : "独立查阅材料"}</h3>
            <span>
              {ordered
                ? "选择步骤，在右侧连续阅读同版页面与知识"
                : "选择条目查看来源；条目不代表业务步骤"}
            </span>
          </div>
          {chosenBranches.length > 0 && (
            <div
              className="journey-reading-decisions"
              aria-label="已选阅读分支"
            >
              {chosenBranches.map((node) => (
                <button
                  key={node.key}
                  onClick={() => {
                    // A changed earlier branch invalidates all choices below it.
                    const earlier = path
                      .slice(0, path.indexOf(node))
                      .map((item) => item.key);
                    select(
                      node.key,
                      Object.fromEntries(
                        Object.entries(choices).filter(([key]) =>
                          earlier.includes(key),
                        ),
                      ),
                    );
                  }}
                >
                  <GitBranch size={13} />
                  {node.title}：
                  {dsl.edges.find((e) => e.key === choices[node.key])?.label ||
                    "未说明条件"}{" "}
                  · 重新选择
                </button>
              ))}
            </div>
          )}
          {ordered ? (
            <ol className="journey-reading-steps">{nodes.map(card)}</ol>
          ) : (
            <ul className="journey-reading-steps journey-reading-materials">
              {nodes.map(card)}
            </ul>
          )}
          {ordered && branches.length > 0 && (
            <div className="journey-reading-branches" aria-label="选择阅读分支">
              <h4>
                <GitBranch size={17} /> 选择阅读分支
              </h4>
              <p>条件来自当前材料；只切换讲解。</p>
              {branches.map((edge) => (
                <button
                  key={edge.key}
                  onClick={() => {
                    select(edge.target, {
                      ...choices,
                      [edge.source]: edge.key,
                    });
                  }}
                >
                  <span>{edge.label || "条件尚未说明"}</span>
                  <strong>
                    {dsl.nodes.find((node) => node.key === edge.target)
                      ?.title || "目标未记录"}
                  </strong>
                  <ArrowRight size={16} />
                </button>
              ))}
            </div>
          )}
        </section>
        {selectedNode && (
          <JourneyStepContext
            key={`${dsl.key}:${selectedNode.key}`}
            node={selectedNode}
            onOpen={onOpen}
            sourceRefs={semantics.source_refs}
          />
        )}
      </div>
      <section className="journey-reading-gaps">
        <h3>依据与待补充内容</h3>
        <p>
          每个条目单独保留来源。页面、动作、状态或快照的存在，不能证明它们之间的业务因果。
        </p>
        {semantics.unknowns?.length ? (
          <ul>
            {semantics.unknowns.map((value, i) => (
              <li key={i}>{value}</li>
            ))}
          </ul>
        ) : (
          <p>尚未记录未覆盖路径和业务验收结论；请结合源文档与实现核对。</p>
        )}
      </section>
    </article>
  );
}

export function JourneyReader({
  rows,
  active,
  dsl,
  entities,
  loaded,
  dirty,
  onSelect,
  onOpen,
  navigation,
  onNavigate,
}: Props) {
  const [search, setSearch] = useState("");
  const visible = rows.filter((row) =>
    `${row.dsl.title} ${journeySemantics(row.dsl).purpose || ""}`
      .toLocaleLowerCase()
      .includes(search.trim().toLocaleLowerCase()),
  );
  const kinds = Object.keys(journeyKinds) as JourneySemantics["kind"][];
  const businessCount = rows.filter(
    (row) => journeySemantics(row.dsl).kind === "business_process",
  ).length;
  return (
    <div className="journey-reading">
      <aside className="journey-reading-library" aria-label="导览目录">
        <label className="journey-reading-search">
          <Search size={16} />
          <span className="sr-only">查找业务场景或材料</span>
          <input
            aria-label="查找业务场景或材料"
            placeholder="查找场景或材料"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        {loaded && !businessCount && (
          <p className="journey-reading-library-empty">
            尚无明确的业务场景导览。现有操作与证据材料可分开查阅。
          </p>
        )}
        {kinds.map((kind) => {
          const items = visible.filter(
            (row) => journeySemantics(row.dsl).kind === kind,
          );
          if (!items.length) return null;
          return (
            <section key={kind}>
              <h3>
                {journeyKinds[kind].plural} <span>{items.length}</span>
              </h3>
              {items.map((row) => (
                <button
                  key={row.journey_id}
                  aria-pressed={active?.journey_id === row.journey_id}
                  disabled={dirty}
                  onClick={() => onSelect(row)}
                >
                  <strong>{row.dsl.title}</strong>
                  <span>
                    {journeySemantics(row.dsl).purpose || "用途尚未说明"}
                  </span>
                  <small>
                    {journeyBases[journeySemantics(row.dsl).basis]} ·{" "}
                    {row.dsl.nodes.filter((node) => node.kind !== "end").length}{" "}
                    项
                  </small>
                </button>
              ))}
            </section>
          );
        })}
        {loaded && rows.length > 0 && !visible.length && (
          <p>没有匹配的场景或材料。</p>
        )}
      </aside>
      {dsl ? (
        <div>
          {dirty && (
            <Notice>
              正在预览尚未保存的维护内容。保存或还原后可切换其他材料。
            </Notice>
          )}
          <ReadingContent
            key={active?.revision_id || dsl.key}
            dsl={dsl}
            entities={entities}
            onOpen={onOpen}
            active={active}
            dirty={dirty}
            navigation={navigation}
            onNavigate={onNavigate}
          />
        </div>
      ) : (
        <Empty
          title={loaded ? "还没有业务导览材料" : "正在读取导览"}
          description="先接入项目页面、源码和业务文档，再围绕一个真实目标整理场景；证据列表不会自动成为业务流程。"
        />
      )}
    </div>
  );
}
