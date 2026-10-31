import "./journey-step-context.css";
import { useEffect, useState } from "react";
import { api } from "./api";
import { ActionChain } from "./ActionChain";
import { ArtifactImage } from "./ArtifactImage";
import { ObservedStateSurface } from "./CaptureResults";
import {
  Button,
  EvidenceList,
  Markdown,
  Notice,
  SafePrototype,
  Tag,
} from "./components";
import { ReadScope, useWorkspace } from "./context";
import { journeyTarget } from "./journey-reading";
import type { JourneyNode } from "./journeys";
import type { Entity } from "./types";
import { documentContext } from "./document-context";

type Props = {
  node: JourneyNode;
  onOpen: (node: JourneyNode) => void;
  sourceRefs?: { entity_id: string; revision_id: string }[];
};
type SavedState = {
  stateKey: string;
  label: string;
  evidenceOrigin: string;
  pageKey?: string;
  captureRef?: string;
  snapshotIndex?: number;
};

function PassivePage({ page, action }: { page: Entity; action?: Entity }) {
  const { w } = useWorkspace();
  return (
    <section className="journey-step-page" aria-label="步骤对应页面">
      <h3>{page.name}</h3>
      <Tag>保存的页面资料</Tag>
      {page.metadata.artifact_kind === "screenshot" && page.artifact_id ? (
        <ArtifactImage
          projectId={w.project.id}
          artifactId={page.artifact_id}
          name={page.name}
        />
      ) : page.content ? (
        <SafePrototype
          html={page.content}
          actions={action ? [action] : []}
          selected={action?.id}
          onSelect={() => undefined}
        />
      ) : (
        <p>当前页面没有可展示的正文或截图。</p>
      )}
      <p className="muted">页面仅供被动阅读，不执行来源脚本或业务请求。</p>
    </section>
  );
}

function StateContext({ node }: { node: JourneyNode }) {
  const { w } = useWorkspace();
  const reference = node.state_ref!;
  const query = new URLSearchParams(
    w.selected_release
      ? { release_id: w.selected_release.id }
      : { analysis_set_id: w.selected_analysis_set?.id || "" },
  );
  const key = `${w.project.id}:${query}:${reference.scenario_id}:${reference.scenario_revision_id}:${reference.state_key}`;
  const [loaded, setLoaded] = useState<{
    key: string;
    state?: SavedState;
  } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void api<{
      scenarios: {
        entity_id: string;
        revision_id: string;
        states: SavedState[];
      }[];
    }>(`/api/v2/projects/${w.project.id}/state-replay?${query}`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (controller.signal.aborted) return;
        const scenario = result.scenarios.find(
          (row) =>
            row.entity_id === reference.scenario_id &&
            row.revision_id === reference.scenario_revision_id,
        );
        setLoaded({
          key,
          state: scenario?.states.find(
            (row) => row.stateKey === reference.state_key,
          ),
        });
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(reason.message);
      });
    return () => controller.abort();
  }, [key]);
  const state = loaded?.key === key ? loaded.state : undefined;
  if (error) return <Notice tone="red">{error}</Notice>;
  if (loaded?.key !== key)
    return <p role="status">正在读取该步骤的准确保存状态…</p>;
  if (!state)
    return (
      <Notice>这个准确状态当前不可读，未替换为其他场景或最新修订。</Notice>
    );
  const page = w.pages.find((row) => row.stable_key === state.pageKey);
  return (
    <section aria-label="步骤保存状态">
      <h3>{state.label}</h3>
      <Tag>
        {(
          {
            observed: "运行观察",
            simulated: "模拟状态",
            declared: "声明状态",
          } as Record<string, string>
        )[state.evidenceOrigin] || "来源未说明"}
      </Tag>
      {state.evidenceOrigin === "observed" && state.captureRef ? (
        <ObservedStateSurface
          projectId={w.project.id}
          captureId={state.captureRef}
          index={state.snapshotIndex || 0}
          analysisId={w.selected_analysis_set?.id}
          releaseId={w.selected_release?.id}
        />
      ) : page ? (
        <PassivePage page={page} />
      ) : (
        <p>该状态没有已绑定页面画面，保留资料缺口。</p>
      )}
    </section>
  );
}

function StepContext({ node, onOpen, sourceRefs = [] }: Props) {
  const { w } = useWorkspace();
  const target = journeyTarget(node, w.entities);
  const [tab, setTab] = useState("说明");
  if (target.kind === "unavailable") {
    const sources = sourceRefs.flatMap((ref) => {
      const source = w.entities.find(
        (row) =>
          row.entity_id === ref.entity_id &&
          row.entity_revision_id === ref.revision_id,
      );
      return source ? [source] : [];
    });
    if (node.kind === "step" && sources.length)
      return (
        <section
          className="journey-step-context journey-step-declaration"
          aria-label="当前步骤的声明依据"
        >
          <h3>当前步骤：{node.title}</h3>
          <Tag>材料支持的业务说明</Tag>
          <p>{node.description}</p>
          <p className="muted">
            此步骤尚无独立的页面动作绑定，下面保留该业务说明所依据的同版资料。
          </p>
          {sources.map((source) => (
            <details key={source.entity_revision_id}>
              <summary>{source.name}</summary>
              <Markdown
                content={source.content}
                {...documentContext(source, w.documents)}
              />
              <Button
                onClick={() =>
                  onOpen({
                    key: node.key,
                    title: source.name,
                    kind: "reference",
                    x: 0,
                    y: 0,
                    binding: {
                      entity_id: source.entity_id,
                      revision_id: source.entity_revision_id,
                    },
                  })
                }
              >
                打开准确来源
              </Button>
            </details>
          ))}
        </section>
      );
    return (
      <Notice>
        {target.reason}。{node.description && <p>{node.description}</p>}
      </Notice>
    );
  }
  // Playback is bound to a scenario; knowledge remains bound to the exact
  // action/page independently. Do not replace action context with its scenario.
  const binding =
    node.binding &&
    w.entities.find(
      (row) =>
        row.entity_id === node.binding!.entity_id &&
        row.entity_revision_id === node.binding!.revision_id,
    );
  const entity = binding || target.entity;
  const action = entity.type === "action" ? entity : undefined;
  const pageKey = action?.metadata.page_key || action?.metadata.pageKey;
  const page =
    entity.type === "page"
      ? entity
      : w.pages.find((row) => row.stable_key === pageKey);
  const related = w.relations.filter(
    (row) =>
      !row.excluded &&
      row.validity_status === "valid" &&
      (row.from_revision_id === entity.entity_revision_id ||
        row.to_revision_id === entity.entity_revision_id),
  );
  const linked = w.entities.filter(
    (row) =>
      row.id !== entity.id &&
      related.some(
        (edge) =>
          row.entity_revision_id === edge.from_revision_id ||
          row.entity_revision_id === edge.to_revision_id,
      ),
  );
  const evidenceIds = new Set(related.flatMap((row) => row.evidence_ids));
  const documents = linked.filter((row) =>
    ["document", "section", "document_section"].includes(row.type),
  );
  const apis = linked.filter((row) => row.type === "api");
  const implementations = linked.filter((row) =>
    ["symbol", "sql", "table"].includes(row.type),
  );
  const evidence = w.evidence.filter((row) => evidenceIds.has(row.id));
  function open(entity: Entity) {
    onOpen({
      ...node,
      binding: {
        entity_id: entity.entity_id,
        revision_id: entity.entity_revision_id,
      },
      state_ref: undefined,
      capture_ref: undefined,
    });
  }
  function cards(rows: Entity[], empty: string) {
    return rows.length ? (
      rows.map((row) => (
        <article className="journey-step-source" key={row.id}>
          <h4>{row.name}</h4>
          <Markdown
            content={row.content}
            {...documentContext(row, w.documents)}
          />
          <Button onClick={() => open(row)}>打开准确来源</Button>
        </article>
      ))
    ) : (
      <p className="muted">{empty}</p>
    );
  }
  return (
    <div className="journey-step-context" aria-label="当前步骤的页面与知识">
      <div className="journey-step-context-grid">
        <section className="journey-step-main">
          <header>
            <h3>当前步骤：{node.title}</h3>
            <Button onClick={() => onOpen(node)}>打开完整来源</Button>
          </header>
          {target.kind === "capture" ? (
            <ObservedStateSurface
              projectId={w.project.id}
              captureId={target.reference.capture_id}
              index={target.reference.snapshot_index}
              analysisId={w.selected_analysis_set?.id}
              releaseId={w.selected_release?.id}
            />
          ) : target.kind === "state" ? (
            <StateContext key={JSON.stringify(target.reference)} node={node} />
          ) : page ? (
            <PassivePage page={page} action={action} />
          ) : (
            <section className="journey-step-source" aria-label="步骤来源内容">
              <h3>{entity.name}</h3>
              <Markdown
                content={entity.content}
                {...documentContext(entity, w.documents)}
              />
            </section>
          )}
        </section>
        <aside className="journey-step-knowledge" aria-label="当前步骤知识">
          <nav aria-label="步骤知识类型">
            {["说明", "接口", "实现与数据", "文档", "证据"].map((name) => (
              <button
                key={name}
                aria-current={tab === name ? "page" : undefined}
                onClick={() => setTab(name)}
              >
                {name}
              </button>
            ))}
          </nav>
          {tab === "说明" ? (
            <>
              <h3>这一步做什么</h3>
              <p>
                {node.description || "此步骤尚未补充业务说明，请核对来源。"}
              </p>
              <h4>对应资料</h4>
              <p>{entity.name}</p>
              <p className="muted">
                关联仅使用当前版本有效的可见资料；源材料中的描述仍需结合业务范围核对。
              </p>
              <p>
                接口 {apis.length} · 文档 {documents.length} · 证据{" "}
                {evidence.length}
              </p>
            </>
          ) : tab === "接口" ? (
            cards(apis, "该步骤尚无有效的接口关联。")
          ) : tab === "实现与数据" ? (
            cards(
              implementations,
              "该步骤尚无直接关联的实现或数据对象，可在下方技术路径继续核对。",
            )
          ) : tab === "文档" ? (
            cards(documents, "该步骤尚无有效的文档关联。")
          ) : (
            <EvidenceList evidence={evidence} />
          )}
        </aside>
      </div>
      {action && <ActionChain actionId={action.id} />}
    </div>
  );
}

export function JourneyStepContext(props: Props) {
  return (
    <ReadScope>
      <StepContext
        key={`${props.node.key}:${JSON.stringify(props.node.binding)}:${JSON.stringify(props.node.state_ref)}:${JSON.stringify(props.node.capture_ref)}`}
        {...props}
      />
    </ReadScope>
  );
}
