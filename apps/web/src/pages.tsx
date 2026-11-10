import { layeredMap } from "./graph-layout";
import { BusinessReferences } from "./BusinessReferences";

import { StateReplay } from "./StateReplay";

import { moduleWorkspace } from "./module-context";
import { BusinessModuleExplorer } from "./BusinessModuleExplorer";
import { ActionChain } from "./ActionChain";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  Check,
  CheckCircle2,
  ChevronRight,
  Code2,
  Copy,
  FileArchive,
  FileCheck2,
  FileText,
  Folder,
  Globe2,
  Info,
  Layers3,
  Link2,
  LockKeyhole,
  Maximize,
  MousePointer2,
  Network,
  Plus,
  Save,
  Search,
  ShieldCheck,
  Sparkles,
  SquareArrowOutUpRight,
  Terminal,
  Upload,
} from "lucide-react";
import { api, date, mutation } from "./api";
import {
  ArrowLink,
  Button,
  Empty,
  EntityCard,
  EvidenceList,
  Markdown,
  Modal,
  Notice,
  Panel,
  PreviewBoundary,
  RelationState,
  SafePrototype,
  Tag,
  entityIcon,
  issueText,
  label,
} from "./components";
import {
  ReadScope,
  useWorkspace,
  usePermission,
  workspaceReadScope,
} from "./context";
import { TeamSettings } from "./TeamSettings";
import { SourceManagement } from "./SourceManagement";
import { NewDraftButton } from "./DraftActions";
import { ArtifactImage } from "./ArtifactImage";
import { PrototypeExplorer } from "./PrototypeExplorer";
import { prototypeManifest } from "./prototype";
import { workspaceBaseline } from "./workspace-baseline";
import { HotspotEditor, HotspotSurface } from "./HotspotEditor";
import ScenarioStudio from "./ScenarioStudio";
import { ObservedStateSurface } from "./CaptureResults";
import { OAuthSettings } from "./OAuthSettings";
import { ModelSettings } from "./ModelSettings";

import { ModuleTree } from "./ModuleTree";
import { AuditSearch } from "./AuditSearch";
import { ProjectProfile } from "./ProjectProfile";

import type { Entity, Evidence, Relation, Token } from "./types";

function field(e: Entity | undefined, key: string) {
  return String(e?.metadata?.[key] ?? "");
}
function relationType(r: Relation) {
  return r.relation_type || r.type || "";
}

function relationEvidence(relations: Relation[], evidence: Evidence[]) {
  const ids = new Set(relations.flatMap((r) => r.evidence_ids || []));
  return evidence.filter((e) => ids.has(e.id));
}
function downloadable(
  name: string,
  content: string,
  type = "application/json",
) {
  const u = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = u;
  a.download = name;
  a.click();
  URL.revokeObjectURL(u);
}

export { ImportCenter as Imports } from "./ImportCenter";

export function Workbench() {
  const { w, session, explanation } = useWorkspace();
  const page = w.pages.find((row) => row.id === explanation.pageId);
  const manifest = prototypeManifest(page);
  if (page && manifest && !explanation.scenarioId)
    return (
      <PrototypeExplorer
        key={workspaceReadScope(w, session, workspaceBaseline(w))}
        page={page}
        manifest={manifest}
      />
    );
  return <LegacyWorkbench />;
}

function LegacyWorkbench() {
  const {
    w,
    moduleId,
    moduleEntities,
    selected,
    navigate,
    notify,
    run,
    busy,
    baseline,
    explanation,
    navigateExplanation,
  } = useWorkspace();
  const setSelected = (actionId: string) =>
    navigateExplanation({ actionId, followTarget: true });
  const [tab, setTab] = useState("业务说明"),
    [question, setQuestion] = useState(""),
    [useModel, setUseModel] = useState(false),
    [answer, setAnswer] = useState<{
      answer: string;
      unknowns: string[];
      evidence_ids: string[];
      model_id?: string;
    } | null>(null),
    [contextError, setContextError] = useState("");
  const scopeWorkspace = moduleId
    ? moduleWorkspace(w, moduleEntities || null)
    : w;
  const scopePages = scopeWorkspace.pages;
  const pageId = explanation?.pageId || "";
  const stateGraph = (
    scopeWorkspace.scenarios.find(
      (scene) => scene.id === explanation?.scenarioId,
    )?.metadata.dsl as
      | {
          explanation_graph?: {
            states: Array<{
              stateKey: string;
              evidenceOrigin: string;
              captureRef?: string;
              snapshotIndex?: number;
            }>;
          };
        }
      | undefined
  )?.explanation_graph;
  const explanationState = stateGraph?.states.find(
    (state) => state.stateKey === explanation?.stateKey,
  );
  const activeActions = scopeWorkspace.actions.filter(
    (item) => item.metadata?.status !== "removed",
  );
  const preferredAction =
    activeActions.find((a) => a.id === selected) || activeActions[0];
  const explicitlySelectedAction = activeActions.find((a) => a.id === selected);
  const page =
    scopePages.find(
      (p) =>
        explicitlySelectedAction &&
        (field(explicitlySelectedAction, "page_key") === p.stable_key ||
          field(explicitlySelectedAction, "pageKey") === p.stable_key),
    ) ||
    scopePages.find((p) => p.id === pageId) ||
    scopePages.find(
      (p) =>
        preferredAction &&
        (field(preferredAction, "page_key") === p.stable_key ||
          field(preferredAction, "pageKey") === p.stable_key),
    ) ||
    scopePages[0];
  const actions = activeActions.filter(
    (a) =>
      !page ||
      field(a, "page_key") === page.stable_key ||
      field(a, "pageKey") === page.stable_key,
  );
  const availableActions = page ? actions : activeActions;
  const action =
    availableActions.find((item) => item.id === selected) ||
    availableActions[0];
  const [relationPolicy, setRelationPolicy] = useState("valid");
  const contextKey = [
    w.project.id,
    baseline,
    w.selected_analysis_set?.id,
    w.selected_analysis_set?.selection_version,
    action?.entity_revision_id,
    relationPolicy,
    w.relations.map((r) => `${r.id}:${r.row_version}`).join(","),
  ].join("|");
  const [loadedContext, setLoadedContext] = useState<{
    key: string;
    value: {
      entities: Array<{ entity_id: string }>;
      relations: Relation[];
      evidence: Evidence[];
    };
  } | null>(null);
  const context =
    loadedContext?.key === contextKey ? loadedContext.value : null;
  const allRelations = context?.relations || [];
  const direct = allRelations.filter(
    (r) =>
      action &&
      (r.from_revision_id === action.entity_revision_id ||
        r.to_revision_id === action.entity_revision_id),
  );
  const entityIds = new Set(context?.entities.map((e) => e.entity_id) || []);
  const entities = w.entities.filter(
    (e) => entityIds.has(e.entity_id) && e.id !== action?.id,
  );
  const apis = entities.filter((e) => ["api", "api_endpoint"].includes(e.type)),
    symbols = entities.filter((e) =>
      ["symbol", "code_symbol", "sql"].includes(e.type),
    ),
    sections = entities.filter((e) =>
      ["section", "document_section", "document"].includes(e.type),
    );
  const evidence = context?.evidence || [];
  useEffect(() => {
    setAnswer(null);
    setUseModel(false);
  }, [action?.id, baseline]);
  useEffect(() => {
    if (!action || !w.selected_analysis_set) return;
    setContextError("");
    const query = new URLSearchParams({
      analysis_set_id: w.selected_analysis_set.id,
      depth: "2",
      max_nodes: "50",
    });
    if (baseline.startsWith("release:")) {
      query.delete("analysis_set_id");
      query.set("release_id", baseline.slice(8));
    }
    query.set("relation_policy", relationPolicy);
    const controller = new AbortController();
    void api<{
      entities: Array<{ entity_id: string }>;
      relations: Relation[];
      evidence: Evidence[];
    }>(`/actions/${action.id}/context?${query}`, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted)
          setLoadedContext({ key: contextKey, value });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setContextError(e.message);
      });
    return () => controller.abort();
  }, [contextKey]);
  if (!activeActions.length && !scopePages.length)
    return (
      <>
        <ReadScope>
          <BusinessModuleExplorer />
        </ReadScope>
        {!moduleId && <ScenarioStudio />}
        <Panel>
          <Empty
            title={
              moduleId ? "当前模块尚无可探索的动作" : "还没有可探索的业务动作"
            }
            description={
              moduleId
                ? "请在当前模块范围中打开已有材料，或选择其他业务模块。"
                : "导入带有稳定动作标识的原型与 UI manifest，便可从页面操作进入项目知识。"
            }
            action={
              <Button kind="primary" onClick={() => navigate("imports")}>
                <Upload size={16} />
                接入业务原型
              </Button>
            }
          />
        </Panel>
      </>
    );
  return (
    <>
      <ReadScope>
        <BusinessModuleExplorer />
      </ReadScope>
      {!moduleId && <ScenarioStudio />}
      <ReadScope>{!moduleId && <StateReplay />}</ReadScope>
      <Notice>
        <strong>讲解模式只选择知识对象</strong>
        <span className="notice-detail">
          当前显示静态原型；点击动作不会向业务系统发送请求。原型脚本与外部资源不执行。
        </span>
      </Notice>
      <div className="workbench-layout">
        <Panel
          className="scenario-tree"
          title="业务场景"
          action={<span className="tag">{scopePages.length} 页</span>}
        >
          <div className="tree-label">
            <Folder size={15} />
            当前项目资料
          </div>
          {scopePages.map((p) => (
            <button
              key={p.id}
              className={`tree-item ${page?.id === p.id ? "selected" : ""}`}
              onClick={() => {
                const a = activeActions.find(
                  (a) => field(a, "page_key") === p.stable_key,
                );
                navigateExplanation({
                  pageId: p.id,
                  actionId: a?.id || "",
                  followTarget: false,
                });
              }}
            >
              <FileText size={16} />
              {p.name}
            </button>
          ))}
          <div className="tree-divider" />
          <div className="tree-label">页面动作</div>
          {availableActions.map((a, i) => (
            <button
              key={a.id}
              className={`action-tree-item ${action?.id === a.id ? "selected" : ""}`}
              onClick={() => setSelected(a.id)}
            >
              <span>{String(i + 1).padStart(2, "0")}</span>
              <strong>{a.name}</strong>
              {action?.id === a.id && <MousePointer2 size={14} />}
            </button>
          ))}
          <div className="tree-bottom">
            <Tag value="manual">导入原型</Tag>
            <small>页面与源码运行版本尚未核对</small>
          </div>
        </Panel>
        <div className="workbench-center">
          <Panel className="preview-panel">
            <div className="preview-toolbar">
              <span>
                <Globe2 size={15} />
                {page?.name || "业务页面"}
              </span>
              <div className="row">
                {page && (
                  <HotspotEditor page={page} actions={availableActions} />
                )}
                <Tag value="static">
                  <ShieldCheck size={12} />
                  安全讲解
                </Tag>
              </div>
            </div>
            <div className="preview-canvas">
              <div className="prototype-window">
                <div className="prototype-window-bar">
                  <span>
                    <i />
                    <i />
                    <i />
                  </span>
                  <code>
                    {field(page || action, "route") ||
                      field(page || action, "route_pattern") ||
                      page?.stable_key}
                  </code>
                  <LockKeyhole size={12} />
                </div>
                {explanationState?.evidenceOrigin === "observed" &&
                explanationState.captureRef ? (
                  <ObservedStateSurface
                    projectId={w.project.id}
                    captureId={explanationState.captureRef}
                    index={explanationState.snapshotIndex || 0}
                    analysisId={w.selected_analysis_set?.id}
                    releaseId={w.selected_release?.id}
                  />
                ) : (
                  page && (
                    <HotspotSurface
                      page={page}
                      actions={availableActions}
                      selected={action?.id}
                      onSelect={setSelected}
                    >
                      {page.artifact_id &&
                      field(page, "artifact_kind") === "screenshot" ? (
                        <ArtifactImage
                          projectId={w.project.id}
                          artifactId={page.artifact_id}
                          name={page.name}
                          bare
                        />
                      ) : page?.content ? (
                        <PreviewBoundary key={page.entity_revision_id}>
                          <SafePrototype
                            html={page.content}
                            actions={availableActions}
                            selected={action?.id}
                            onSelect={(a) => setSelected(a.id)}
                          />
                        </PreviewBoundary>
                      ) : (
                        <Empty
                          title="当前页面没有可渲染的原型内容"
                          description="仍可通过下方动作列表查看已关联知识。"
                        />
                      )}
                    </HotspotSurface>
                  )
                )}
              </div>
              <div className="selected-hotspot">
                <MousePointer2 size={15} />
                <span>已选择动作</span>
                <strong>{action?.name}</strong>
              </div>
            </div>
            <div className="preview-caption">
              <Info size={13} />
              静态安全预览 · 保留文本结构，交互脚本已禁用
            </div>
          </Panel>
          <Panel
            title="场景动作"
            action={<Tag>{availableActions.length} 个动作</Tag>}
          >
            <div className="scenario-steps">
              {availableActions.map((a, i) => (
                <button
                  key={a.id}
                  className={action?.id === a.id ? "selected" : ""}
                  onClick={() => setSelected(a.id)}
                >
                  <span>{String(i + 1).padStart(2, "0")}</span>
                  <div>
                    <strong>{a.name}</strong>
                    <small>
                      {field(a, "event") || "click"} · 选择并查看知识
                    </small>
                  </div>
                  {action?.id === a.id && <CheckCircle2 size={15} />}
                </button>
              ))}
            </div>
          </Panel>
        </div>
        <Panel className="knowledge-detail">
          <div className="action-title">
            <span className="eyebrow">当前业务操作</span>
            <h2>{action?.name}</h2>
            <div className="row wrap">
              {direct.length ? (
                <Tag>
                  {relationPolicy === "valid"
                    ? "有效关联"
                    : relationPolicy === "all"
                      ? "全部关联"
                      : "失效关联"}
                </Tag>
              ) : (
                <Tag>尚无关联</Tag>
              )}
              <Tag>{direct.length} 条直接关系</Tag>
            </div>
            {relationPolicy === "outdated" &&
              direct.map((relation) => (
                <div key={relation.id}>
                  <RelationState relation={relation} />
                  <Tag value={relation.causal_basis} />
                </div>
              ))}
          </div>
          <div className="row wrap" role="group" aria-label="讲解关联范围">
            {[
              ["valid", "有效知识"],
              ["all", "全部关联"],
              ["outdated", "失效关联"],
            ].map(([value, name]) => (
              <Button
                key={value}
                kind={relationPolicy === value ? "primary" : "secondary"}
                onClick={() => setRelationPolicy(value)}
              >
                {name}
              </Button>
            ))}
          </div>
          {relationPolicy !== "valid" && (
            <Notice>
              {relationPolicy === "all"
                ? "包含有效与失效关联，状态由来源版本决定。"
                : "以下关联的来源已变化或缺失，可纠错或移除。"}
            </Notice>
          )}
          <div className="detail-tabs">
            {["业务说明", "接口", "实现", "数据", "文档", "证据"].map((t) => (
              <button
                key={t}
                className={tab === t ? "active" : ""}
                onClick={() => setTab(t)}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="detail-scroll">
            {contextError && <Notice tone="red">{contextError}</Notice>}
            {tab === "业务说明" ? (
              <>
                <div className="detail-block">
                  <h3>操作上下文</h3>
                  <p>
                    {action?.content ||
                      field(action, "description") ||
                      "此动作由导入的 UI manifest 声明。业务含义以关联文档和来源证据为准。"}
                  </p>
                  <code className="logical-key">{action?.stable_key}</code>
                </div>
                <div className="context-counts">
                  {[
                    { n: apis.length, t: "接口", i: Network },
                    { n: sections.length, t: "文档章节", i: FileText },
                    { n: evidence.length, t: "证据", i: ShieldCheck },
                  ].map((x) => (
                    <button
                      key={x.t}
                      onClick={() => setTab(x.t === "文档章节" ? "文档" : x.t)}
                    >
                      <x.i size={16} />
                      <span>{x.t}</span>
                      <strong>{x.n}</strong>
                      <ChevronRight size={14} />
                    </button>
                  ))}
                </div>
                <h3 className="detail-section-title">关联文档</h3>
                {sections.slice(0, 3).map((e) => (
                  <EntityCard
                    key={e.id}
                    entity={e}
                    onClick={() => navigate("documents", e)}
                  />
                ))}
                {!sections.length && (
                  <p className="muted">当前动作尚未关联文档章节。</p>
                )}
                <div className="context-ask">
                  <h3>
                    <Sparkles size={16} />
                    继续查找上下文
                  </h3>
                  <p>
                    {w.runtime_status?.model_enabled
                      ? "默认确定性检索 · 可主动选择模型辅助"
                      : "确定性检索 · 模型辅助未启用"}
                  </p>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      void run(() =>
                        api<{
                          answer: string;
                          unknowns: string[];
                          evidence_ids: string[];
                        }>(
                          `/projects/${w.project.id}/questions`,
                          mutation("POST", {
                            question,
                            ...(useModel && w.runtime_status?.model_enabled
                              ? { use_model: true }
                              : {}),
                            ...(w.selected_release
                              ? { release_id: w.selected_release.id }
                              : {
                                  analysis_set_id: w.selected_analysis_set?.id,
                                }),
                            action_id: action?.id,
                          }),
                        ),
                      ).then((r) => {
                        if (r) setAnswer(r);
                      });
                    }}
                  >
                    <label className="check-line">
                      <input
                        type="checkbox"
                        checked={useModel}
                        disabled={!w.runtime_status?.model_enabled || busy}
                        onChange={(e) => setUseModel(e.target.checked)}
                      />
                      本次使用模型辅助（按项目范围与预算发送上下文）
                    </label>
                    <div className="inline-input">
                      <input
                        value={question}
                        onChange={(e) => setQuestion(e.target.value)}
                        placeholder="输入准确业务或接口关键词"
                        required
                      />
                      <button
                        aria-label="检索上下文"
                        type="submit"
                        disabled={busy}
                      >
                        <ArrowRight size={16} />
                      </button>
                    </div>
                  </form>
                  {answer && (
                    <div className="answer">
                      {answer.model_id && (
                        <Tag>
                          {answer.model_id.startsWith("none:")
                            ? "未调用模型"
                            : "模型辅助 · " + answer.model_id}
                        </Tag>
                      )}
                      <Markdown content={answer.answer} />
                      {answer.unknowns.map((x) => (
                        <small key={x}>{x}</small>
                      ))}
                      {answer.evidence_ids.length > 0 && (
                        <EvidenceList
                          evidence={w.evidence.filter((item) =>
                            answer.evidence_ids.includes(item.id),
                          )}
                        />
                      )}
                    </div>
                  )}
                </div>
              </>
            ) : tab === "接口" ? (
              <>
                {apis.map((e) => (
                  <div key={e.id} className="api-card">
                    <div>
                      <span className="method">
                        {field(e, "method") || "API"}
                      </span>
                      <code>
                        {field(e, "normalized_route") ||
                          field(e, "path") ||
                          field(e, "route") ||
                          e.name}
                      </code>
                    </div>
                    <h3>{e.name}</h3>
                    <p>{e.content || field(e, "description")}</p>
                    <Tag value={field(e, "evidence_kind") || "static"} />
                    <code className="logical-key">
                      {field(e, "service_key") || e.stable_key}
                    </code>
                    <ArrowLink onClick={() => navigate("graph", e)}>
                      在知识地图定位
                    </ArrowLink>
                  </div>
                ))}
                {!apis.length && (
                  <Empty
                    title="尚无接口依据"
                    description="当前版本没有与此动作关联的 API，不能据名称推断真实调用。"
                    icon="link"
                  />
                )}
              </>
            ) : tab === "实现" ? (
              <>
                {symbols.map((e) => (
                  <div className="code-evidence" key={e.id}>
                    <div>
                      {entityIcon(e.type)}
                      <strong>{e.name}</strong>
                      <Tag value={field(e, "evidence_kind") || "manual"} />
                    </div>
                    <code>{field(e, "qualified_name") || e.stable_key}</code>
                    {e.content && <pre>{e.content}</pre>}
                    <ArrowLink onClick={() => navigate("graph", e)}>
                      查看关联路径
                    </ArrowLink>
                  </div>
                ))}
                {!symbols.length && (
                  <Empty
                    title="暂无实现证据"
                    description="需要源码锚点或受支持的分析结果。当前不补画未验证调用链。"
                  />
                )}
              </>
            ) : tab === "数据" ? (
              <>
                {entities
                  .filter((e) => ["sql", "table"].includes(e.type))
                  .map((e) => (
                    <div className="code-evidence" key={e.id}>
                      <strong>{e.name}</strong>
                      <code>{e.stable_key}</code>
                      <pre>{e.content}</pre>
                      <ArrowLink onClick={() => navigate("graph", e)}>
                        查看原始定位
                      </ArrowLink>
                    </div>
                  ))}
                {!entities.some((e) => ["sql", "table"].includes(e.type)) && (
                  <Empty
                    title="尚无数据对象依据"
                    description="动态 SQL 与参数不按接口名称推断。"
                  />
                )}
              </>
            ) : tab === "文档" ? (
              <>
                {sections.map((e) => (
                  <div className="section-preview" key={e.id}>
                    <div className="row between">
                      <Tag value="static">{label(e.type)}</Tag>
                      <button
                        className="icon-button"
                        aria-label="打开章节"
                        onClick={() => navigate("documents", e)}
                      >
                        <ArrowUpRight size={16} />
                      </button>
                    </div>
                    <h3>{e.name}</h3>
                    <Markdown content={e.content} />
                    <ArrowLink onClick={() => navigate("documents", e)}>
                      定位到文档章节
                    </ArrowLink>
                  </div>
                ))}
                {!sections.length && (
                  <Empty
                    title="暂无关联章节"
                    description="通过显式绑定清单补充动作到文档章节的候选关联。"
                  />
                )}
              </>
            ) : (
              <EvidenceList evidence={evidence} />
            )}
          </div>
          <div className="detail-footer">
            <ArrowLink onClick={() => navigate("graph", action)}>
              在知识地图中探索
            </ArrowLink>
            <button
              className="icon-button"
              aria-label="复制当前上下文链接"
              onClick={() => {
                void navigator.clipboard
                  .writeText(location.href)
                  .then(() => notify("当前上下文链接已复制"));
              }}
            >
              <Copy size={15} />
            </button>
          </div>
        </Panel>
      </div>
      {action && (
        <ReadScope>
          <ActionChain actionId={action.id} />
        </ReadScope>
      )}
    </>
  );
}

export function Documents() {
  const canEdit = usePermission("knowledge:edit");
  const {
    w,
    selected,
    setSelected,
    navigate,
    run,
    busy,
    notify,
    setUnsavedChanges,
  } = useWorkspace();
  const [filter, setFilter] = useState(""),
    [mode, setMode] = useState("阅读"),
    [draft, setDraft] = useState(""),
    [dirty, setDirty] = useState(false),
    [lockedSections, setLockedSections] = useState<string[]>([]),
    [savedVersion, setSavedVersion] = useState(0),
    [baseRevision, setBaseRevision] = useState(""),
    [saveError, setSaveError] = useState(""),
    [scrollRequest, setScrollRequest] = useState(0),
    [anchorError, setAnchorError] = useState("");
  const selectedEntity = w.entities.find((e) => e.id === selected);
  const doc =
    w.documents.find((d) => d.id === selected) ||
    w.documents.find(
      (d) =>
        selectedEntity &&
        (field(selectedEntity, "document_key") === d.stable_key ||
          selectedEntity.artifact_id === d.artifact_id),
    ) ||
    w.documents[0];
  const sections = w.entities
    .filter(
      (e) =>
        ["section", "document_section"].includes(e.type) &&
        doc &&
        (field(e, "document_key") === doc.stable_key ||
          e.artifact_id === doc.artifact_id),
    )
    .sort((left, right) => {
      const line = (entity: Entity) => {
        const value = entity.locator?.startLine ?? entity.locator?.line_start;
        return typeof value === "number" && Number.isFinite(value) && value > 0
          ? value
          : Number.MAX_SAFE_INTEGER;
      };
      return line(left) - line(right);
    });
  const section = sections.find((s) => s.id === selected);
  const current = section || doc;
  const related = w.relations.filter(
    (r) =>
      current &&
      (r.from_revision_id === current.entity_revision_id ||
        r.to_revision_id === current.entity_revision_id),
  );
  const linkedEntities = w.entities.filter((e) =>
    related.some(
      (r) =>
        (r.from_revision_id === e.entity_revision_id ||
          r.to_revision_id === e.entity_revision_id) &&
        e.id !== current?.id,
    ),
  );
  const loadedDocument = useRef("");
  useEffect(() => {
    const key = `${w.selected_analysis_set?.id}:${doc?.id}:${doc?.entity_revision_id}`;
    if (loadedDocument.current === key) return;
    if (dirty) {
      setSaveError(
        "远端文档修订发生变化，本地修改已保留。请先复制或对比草稿，再还原并合并。",
      );
      return;
    }
    loadedDocument.current = key;
    setDraft(doc?.content || "");
    setLockedSections(
      Array.isArray(doc?.metadata?.locked_section_ids)
        ? doc.metadata.locked_section_ids.map(String)
        : [],
    );
    setBaseRevision(doc?.entity_revision_id || "");
    setSavedVersion(
      doc?.row_version || Number(doc?.metadata?.row_version) || 1,
    );
    setSaveError("");
    setMode("阅读");
  }, [doc?.id, doc?.entity_revision_id, w.selected_analysis_set?.id, dirty]);
  useEffect(() => {
    setUnsavedChanges(dirty);
    return () => setUnsavedChanges(false);
  }, [dirty, setUnsavedChanges]);
  const reader = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (mode !== "阅读" || dirty || !reader.current) return;
    const line = Number(
      section?.locator?.startLine ?? section?.locator?.line_start,
    );
    const heading =
      Number.isInteger(line) && line > 0
        ? reader.current.querySelector<HTMLElement>(
            `[data-source-line="${line}"]`,
          )
        : null;
    // A heading-less document has one section beginning at the first line.
    const target =
      heading ||
      (section && line === 1
        ? reader.current.querySelector<HTMLElement>(".markdown")
        : !section
          ? reader.current
          : null);
    setAnchorError(
      section && !target
        ? "当前章节缺少准确的正文定位，已保留完整文档供阅读。"
        : "",
    );
    target?.scrollIntoView({ block: "start", behavior: "auto" });
    heading?.focus({ preventScroll: true });
  }, [section?.id, doc?.entity_revision_id, mode, scrollRequest, dirty]);
  function selectChapter(id: string) {
    if (dirty) {
      notify("当前草稿尚未保存，目录定位前请先保存或还原", "error");
      return;
    }
    setMode("阅读");
    setSelected(id);
    setScrollRequest((value) => value + 1);
  }
  async function save() {
    if (!doc || !w.selected_analysis_set) return;
    setSaveError("");
    try {
      const response = await api<{
        row_version: number;
        entity_revision_id: string;
      }>(
        `/documents/${doc.id}/draft`,
        mutation(
          "PUT",
          {
            analysis_set_id: w.selected_analysis_set.id,
            base_revision_id: baseRevision,
            markdown: draft,
            locked_section_ids: lockedSections,
          },
          savedVersion,
        ),
      );
      setSavedVersion(response.row_version);
      setBaseRevision(response.entity_revision_id);
      setUnsavedChanges(false);
      setDirty(false);
      await run(async () => response, "文档草稿已保存；受影响关联已标记待复核");
    } catch (e) {
      setSaveError((e as Error).message);
    }
  }

  if (!w.documents.length)
    return (
      <Panel>
        <Empty
          title="文档知识，从一份 Markdown 开始"
          description="导入原始 Markdown，保留章节身份与来源，逐步建立到业务动作和技术对象的双向关联。"
          icon="document"
          action={
            <Button kind="primary" onClick={() => navigate("imports")}>
              <Upload size={16} />
              导入文档资料
            </Button>
          }
        />
      </Panel>
    );
  return (
    <div className="documents-layout">
      <Panel
        className="document-tree"
        title="知识目录"
        action={<Tag>{w.documents.length} 份</Tag>}
      >
        <div className="filter-input">
          <Search size={15} />
          <input
            placeholder="筛选文档标题…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <div className="tree-label">
          <Folder size={15} />
          项目文档
        </div>
        <div className="document-tree-content">
          {w.documents
            .filter((d) => d.name.includes(filter))
            .map((d) => (
              <div key={d.id}>
                <button
                  className={`tree-item ${doc.id === d.id ? "selected" : ""}`}
                  onClick={() => {
                    if (dirty) {
                      notify("当前草稿尚未保存，切换前请先保存或还原", "error");
                      return;
                    }
                    setMode("阅读");
                    setSelected(d.id);
                    setScrollRequest((value) => value + 1);
                  }}
                >
                  <FileText size={16} />
                  {d.name}
                </button>
                {doc.id === d.id && (
                  <div className="section-tree">
                    {sections.map((s, i) => (
                      <button
                        key={s.id}
                        className={section?.id === s.id ? "selected" : ""}
                        onClick={() => selectChapter(s.id)}
                      >
                        <span>{String(i + 1).padStart(2, "0")}</span>
                        {s.name}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ))}
        </div>
        <div className="tree-bottom">
          <ShieldCheck size={16} />
          <small>
            原始文件保留
            <br />
            草稿编辑创建独立修订
          </small>
        </div>
      </Panel>
      <Panel className="document-main">
        {canEdit && w.selected_analysis_set?.state === "frozen" && (
          <div className="draft-banner">
            <span>当前是固定发布版本。编辑前请创建独立草稿。</span>
            <NewDraftButton small />
          </div>
        )}
        <div className="document-toolbar">
          <div className="detail-tabs">
            {(canEdit ? ["阅读", "编辑", "对比"] : ["阅读", "对比"]).map(
              (t) => (
                <button
                  key={t}
                  className={mode === t ? "active" : ""}
                  onClick={() => setMode(t)}
                >
                  {t}
                </button>
              ),
            )}
          </div>
          <div className="row">
            {dirty && (
              <button
                className="text-button"
                onClick={() => {
                  setDraft(doc.content || "");
                  setLockedSections(
                    Array.isArray(doc.metadata?.locked_section_ids)
                      ? doc.metadata.locked_section_ids.map(String)
                      : [],
                  );
                  setUnsavedChanges(false);
                  setDirty(false);
                  setSaveError("");
                  setBaseRevision(doc.entity_revision_id);
                  setSavedVersion(
                    doc.row_version || Number(doc.metadata?.row_version) || 1,
                  );
                }}
              >
                还原修改
              </button>
            )}
            {doc.artifact_id && (
              <a
                className="icon-button"
                aria-label="下载原始文件"
                title="下载保留原始字节的文件"
                href={`/api/v1/projects/${w.project.id}/artifacts/${doc.artifact_id}/download`}
              >
                <FileArchive size={16} />
              </a>
            )}
            <button
              className="icon-button"
              aria-label="下载当前阅读层 Markdown"
              title="下载当前阅读层 Markdown"
              onClick={() => {
                downloadable(
                  `${doc.name.replace(/\.md$/, "")}-knowledge.md`,
                  draft,
                  "text/markdown;charset=utf-8",
                );
                notify("已导出知识阅读层，非原始字节文件");
              }}
            >
              <ArrowDownToLine size={16} />
            </button>
            <Button
              kind="primary small"
              onClick={() => {
                void save();
              }}
              disabled={
                busy ||
                !dirty ||
                !canEdit ||
                w.selected_analysis_set?.state === "frozen"
              }
            >
              <Save size={14} />
              保存草稿
            </Button>
          </div>
        </div>
        <div className="document-meta">
          <Tag
            value={
              w.selected_analysis_set?.state === "frozen"
                ? "published"
                : "draft"
            }
          />
          <span>{doc.name}</span>
          <code>{doc.content_sha256?.slice(0, 12)}</code>
        </div>
        {saveError && (
          <Notice tone="red">
            <strong>草稿未保存</strong>
            <p>{saveError}</p>
            <span>本地修改已保留，可在“对比”中检查差异，再刷新合并。</span>
          </Notice>
        )}
        {mode === "阅读" ? (
          <div className="document-reader" ref={reader}>
            <span className="eyebrow">原始资料阅读层</span>
            <h2>{doc.name}</h2>
            {section && (
              <div className="chapter-indicator">
                <FileCheck2 size={14} />
                {section.stable_key}
                <Tag>准确章节定位</Tag>
              </div>
            )}
            {anchorError && <Notice tone="amber">{anchorError}</Notice>}
            <Markdown
              content={dirty ? draft : doc.content}
              documentId={doc.id}
              revisionId={doc.entity_revision_id}
            />
            {!section && sections.length > 0 && (
              <div className="chapter-outline">
                <h3>可关联章节</h3>
                {sections.map((s) => (
                  <button key={s.id} onClick={() => selectChapter(s.id)}>
                    {s.name}
                    <ArrowRight size={14} />
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : mode === "编辑" ? (
          <div className="editor-wrap">
            <div className="editor-note">
              <Code2 size={14} />
              Markdown 源码 · 编辑创建平台草稿，不回写原始材料
            </div>
            {sections.length > 0 && (
              <details className="chapter-locks">
                <summary>人工章节锁（{lockedSections.length} 项）</summary>
                <p className="section-description">
                  锁定章节保留稳定身份和正文；需要修改时，由人员明确取消锁定后保存。
                </p>
                {sections.map((item) => (
                  <label className="check-line" key={item.entity_id || item.id}>
                    <input
                      type="checkbox"
                      checked={lockedSections.includes(
                        item.entity_id || item.id,
                      )}
                      disabled={
                        !canEdit || w.selected_analysis_set?.state === "frozen"
                      }
                      onChange={(event) => {
                        const id = item.entity_id || item.id;
                        setLockedSections((previous) =>
                          event.target.checked
                            ? [...previous, id]
                            : previous.filter((value) => value !== id),
                        );
                        setUnsavedChanges(true);
                        setDirty(true);
                      }}
                    />
                    {item.name}
                  </label>
                ))}
              </details>
            )}
            <textarea
              aria-label="Markdown 草稿编辑器"
              className="markdown-editor"
              value={draft}
              spellCheck={false}
              onChange={(e) => {
                setDraft(e.target.value);
                setUnsavedChanges(true);
                setDirty(true);
              }}
              readOnly={!canEdit || w.selected_analysis_set?.state === "frozen"}
            />
            {w.selected_analysis_set?.state === "frozen" && (
              <Notice tone="amber">
                此基线已发布。请导入新版本资料，建立可编辑的新草稿。
              </Notice>
            )}
          </div>
        ) : (
          <div className="diff-layout">
            <div>
              <h3>当前固定修订</h3>
              <pre>{doc.content}</pre>
            </div>
            <div>
              <h3>本地草稿 {dirty && <Tag value="candidate">未保存</Tag>}</h3>
              <pre>{draft}</pre>
            </div>
          </div>
        )}

        <div className="document-status">
          <span>
            {dirty ? (
              <>
                <span className="status-dot amber" />
                本地修改尚未保存
              </>
            ) : (
              <>
                <CheckCircle2 size={14} />
                当前修订已保存
              </>
            )}
          </span>
          <span>Markdown · {draft.length} 字符</span>
        </div>
      </Panel>
      <Panel
        className="chapter-relations"
        title="当前章节的关联"
        action={<Link2 size={18} />}
      >
        <ReadScope>
          <BusinessReferences entityId={current.entity_id || current.id} />
        </ReadScope>
        <div className="current-chapter">
          <FileText size={18} />
          <strong>{current.name}</strong>
        </div>
        {linkedEntities.map((e) => (
          <div className="chapter-link" key={e.id}>
            <Tag>{label(e.type)}</Tag>
            <h3>{e.name}</h3>
            <code>{e.stable_key}</code>
            <ArrowLink
              onClick={() =>
                navigate(
                  ["action", "ui_action"].includes(e.type)
                    ? "workbench"
                    : "graph",
                  e,
                )
              }
            >
              {["action", "ui_action"].includes(e.type)
                ? "在工作台定位"
                : "在知识地图定位"}
            </ArrowLink>
          </div>
        ))}
        {!linkedEntities.length && (
          <Empty
            title="当前章节暂无关联"
            description="在资料包中补充章节显式绑定后，可从这里回到业务操作。"
            icon="link"
          />
        )}
        {related.map((r) => (
          <div className="chapter-relation-status" key={r.id}>
            <RelationState relation={r} />
            <small>{label(relationType(r))}</small>
          </div>
        ))}
        <div className="chapter-evidence">
          <h3>来源与证据</h3>
          <EvidenceList evidence={relationEvidence(related, w.evidence)} />
        </div>
        <div className="chapter-bottom">
          <Button kind="full" onClick={() => navigate("relations")}>
            <Link2 size={15} />
            查看关联管理
          </Button>
          <p>
            原始字节文件通过发布知识包导出保留。此处“下载”导出当前 Markdown
            阅读层。
          </p>
        </div>
      </Panel>
    </div>
  );
}

export function Graph() {
  const { w, selected, setSelected, navigate } = useWorkspace();
  const [depth, setDepth] = useState(1),
    [query, setQuery] = useState(""),
    [validOnly, setValidOnly] = useState(false),
    [zoom, setZoom] = useState(1),
    [rootId, setRootId] = useState(selected);
  // Inspecting a node must not replace the local graph's expansion root.
  const root =
    w.entities.find((e) => e.id === rootId) || w.actions[0] || w.entities[0];
  const center = w.entities.find((e) => e.id === selected) || root;
  type MapData = {
    nodes: Entity[];
    edges: Relation[];
    truncated: boolean;
    unknowns: string[];
  };
  const [mapResult, setMapResult] = useState<{
      key: string;
      value: MapData;
    } | null>(null),
    [mapError, setMapError] = useState("");
  const visibility = w.entities.map((row) => row.entity_revision_id).join(",");
  const mapKey = `${w.project.id}:${w.selected_analysis_set?.id}:${w.selected_analysis_set?.selection_version}:${w.selected_release?.id}:${root?.entity_revision_id}:${depth}:${validOnly}:${visibility}`;
  useEffect(() => {
    const controller = new AbortController();
    setMapResult(null);
    setMapError("");
    if (!root) return;
    const q = new URLSearchParams({
      project_id: w.project.id,
      depth: String(depth),
      max_nodes: "100",
      valid_only: String(validOnly),
      ...(w.selected_release
        ? { release_id: w.selected_release.id }
        : { analysis_set_id: w.selected_analysis_set?.id || "" }),
    });
    void api<MapData>(`/api/v2/entities/${root.id}/knowledge-map?${q}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setMapResult({ key: mapKey, value });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setMapError(e.message);
      });
    return () => controller.abort();
  }, [mapKey]);
  const mapData = mapResult?.key === mapKey ? mapResult.value : null;
  const graph = {
    nodes: mapData?.nodes || [],
    edges: mapData?.edges || [],
    total: mapData?.nodes.length || 0,
    truncated: mapData?.truncated || false,
  };
  const layout = useMemo(
    () =>
      layeredMap(
        (mapData?.nodes || []).map((row) => row.entity_revision_id),
        mapData?.edges || [],
        root?.entity_revision_id || "",
      ),
    [mapData, root?.entity_revision_id],
  );
  const points = layout.points;
  const mapScroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (mapData && mapScroller.current) {
      mapScroller.current.scrollTop = Math.max(
        0,
        (points.get(root?.entity_revision_id || "")?.y || 0) - 300,
      );
      mapScroller.current.scrollLeft = 0;
    }
  }, [mapResult?.key]);
  const related = graph.edges.filter(
    (r) =>
      center &&
      (r.from_revision_id === center.entity_revision_id ||
        r.to_revision_id === center.entity_revision_id),
  );
  if (!center)
    return (
      <Panel>
        <Empty
          title="知识地图尚未形成"
          description="接入资料后，从一个动作、章节或接口开始，逐层查看有依据的局部关系。"
          action={
            <Button kind="primary" onClick={() => navigate("imports")}>
              接入项目资料
            </Button>
          }
        />
      </Panel>
    );
  return (
    <>
      <div className="graph-toolbar">
        <div className="filter-input">
          <Search size={16} />
          <input
            placeholder="查找并定位实体…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="row">
          <label className="inline-select">
            展开层级
            <select
              value={depth}
              onChange={(e) => setDepth(Number(e.target.value))}
            >
              {[1, 2, 3].map((d) => (
                <option key={d} value={d}>
                  {d} 跳
                </option>
              ))}
            </select>
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={validOnly}
              onChange={(e) => setValidOnly(e.target.checked)}
            />
            仅有效关系
          </label>
          <Button
            onClick={() =>
              downloadable(
                "vks-local-graph.json",
                JSON.stringify(
                  {
                    baseline: w.selected_analysis_set?.id,
                    nodes: graph.nodes,
                    edges: graph.edges,
                  },
                  null,
                  2,
                ),
              )
            }
          >
            <ArrowDownToLine size={15} />
            导出局部图
          </Button>
        </div>
      </div>
      {query && (
        <Panel className="graph-search-results">
          {w.entities
            .filter((e) => `${e.name} ${e.stable_key}`.includes(query))
            .slice(0, 8)
            .map((e) => (
              <EntityCard
                key={e.id}
                entity={e}
                onClick={() => {
                  setSelected(e.id);
                  setRootId(e.id);
                  setQuery("");
                }}
              />
            ))}
        </Panel>
      )}
      {mapError && <Notice tone="red">{mapError}</Notice>}
      {!mapData && !mapError && (
        <p role="status">正在读取最多 100 个同版可见图谱节点…</p>
      )}
      <div className="graph-layout">
        <div className="graph-canvas">
          <div className="graph-baseline">
            <Layers3 size={14} />
            {w.selected_analysis_set?.name}
            <Tag>局部视图</Tag>
          </div>
          <div
            className="v2-graph-scroll"
            ref={mapScroller}
            tabIndex={0}
            aria-label="可滚动知识图谱"
          >
            <div
              className="graph-inner"
              style={{
                transform: `scale(${zoom})`,
                width: layout.width,
                height: layout.height,
              }}
            >
              <svg
                className="graph-edges"
                width={layout.width}
                height={layout.height}
                viewBox={`0 0 ${layout.width} ${layout.height}`}
              >
                <defs>
                  <marker
                    id="arrow"
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 10 5 L 0 10 z" fill="#8ca3c7" />
                  </marker>
                  <marker
                    id="arrow-pending"
                    viewBox="0 0 10 10"
                    refX="9"
                    refY="5"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 10 5 L 0 10 z" fill="#dc981f" />
                  </marker>
                </defs>
                {graph.edges.map((r) => {
                  const verifiedRuntime =
                    r.causal_basis === "correlated_runtime" &&
                    !r.excluded &&
                    r.validity_status === "valid" &&
                    w.selected_analysis_set?.runtime_alignment === "matched";
                  const from = points.get(r.from_revision_id),
                    to = points.get(r.to_revision_id);
                  if (!from || !to) return null;
                  const dx = to.x - from.x,
                    dy = to.y - from.y,
                    l = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
                  const x1 = from.x + (dx / l) * 65,
                    y1 = from.y + (dy / l) * 40,
                    x2 = to.x - (dx / l) * 80,
                    y2 = to.y - (dy / l) * 45;
                  return (
                    <g key={r.id} className={!r.excluded ? "pending" : ""}>
                      <line
                        x1={x1}
                        y1={y1}
                        x2={x2}
                        y2={y2}
                        strokeDasharray={
                          verifiedRuntime
                            ? undefined
                            : r.causal_basis === "static_potential"
                              ? "6 4"
                              : "2 5"
                        }
                        style={{
                          stroke:
                            r.validity_status !== "valid" || r.excluded
                              ? "#c65d67"
                              : verifiedRuntime
                                ? "#138a72"
                                : r.causal_basis === "static_potential"
                                  ? "#6293d5"
                                  : "#8c6cbb",
                        }}
                        markerEnd={
                          !r.excluded ? "url(#arrow-pending)" : "url(#arrow)"
                        }
                      />
                      <rect
                        x={(x1 + x2) / 2 - 33}
                        y={(y1 + y2) / 2 - 11}
                        width="66"
                        height="22"
                        rx="11"
                      />
                      <text
                        x={(x1 + x2) / 2}
                        y={(y1 + y2) / 2 + 4}
                        textAnchor="middle"
                      >
                        {label(relationType(r))}
                      </text>
                    </g>
                  );
                })}
              </svg>
              {graph.nodes.map((e) => {
                const pos = points.get(e.entity_revision_id)!;
                return (
                  <button
                    className={`graph-node ${e.entity_revision_id === center.entity_revision_id ? "central" : ""} ${e.type}`}
                    key={e.id}
                    aria-pressed={e.id === center.id}
                    style={{ left: pos.x, top: pos.y }}
                    onClick={() => setSelected(e.id)}
                  >
                    <span className="graph-node-type">
                      {entityIcon(e.type, 15)}
                      {label(e.type)}
                    </span>
                    <strong>{e.name}</strong>
                    <small>{e.stable_key}</small>
                  </button>
                );
              })}
            </div>
          </div>
          <div className="graph-controls">
            <button
              aria-label="放大图谱"
              onClick={() => setZoom(Math.min(1.3, zoom + 0.1))}
            >
              <Plus size={17} />
            </button>
            <button
              aria-label="缩小图谱"
              onClick={() => setZoom(Math.max(0.6, zoom - 0.1))}
            >
              −
            </button>
            <button aria-label="重置图谱缩放" onClick={() => setZoom(1)}>
              <Maximize size={16} />
            </button>
            <span>{Math.round(zoom * 100)}%</span>
          </div>
          <div className="graph-count">
            显示 {graph.nodes.length} / {graph.total} 个局部节点 ·{" "}
            {graph.edges.length} 条关系
            {graph.truncated &&
              " · 已达到 100 节点上限，结果已截断，可通过搜索重新定位"}
          </div>
        </div>
        <div>
          <Panel
            className="graph-detail"
            title="当前对象"
            action={<Tag>{label(center.type)}</Tag>}
          >
            <Button
              disabled={center.id === root?.id}
              onClick={() => setRootId(center.id)}
            >
              以此节点展开
            </Button>
            <ReadScope>
              <BusinessReferences entityId={center.id} />
            </ReadScope>
            <h2>{center.name}</h2>
            <code className="logical-key">{center.stable_key}</code>
            {center.content && (
              <div className="graph-entity-preview">
                <Markdown content={center.content.slice(0, 1000)} />
              </div>
            )}
            <div className="detail-data">
              <div>
                <span>版本</span>
                <strong>{w.selected_analysis_set?.name}</strong>
              </div>
              <div>
                <span>修订指纹</span>
                <code>{center.content_sha256?.slice(0, 14)}…</code>
              </div>
              <div>
                <span>直接关系</span>
                <strong>{related.length}</strong>
              </div>
            </div>
            <Notice>
              静态关系表示固定材料中的结构，候选或人工确认均不等于运行因果。
            </Notice>
            <Button
              kind="primary full"
              onClick={() =>
                navigate(
                  ["action", "ui_action"].includes(center.type)
                    ? "workbench"
                    : ["section", "document", "document_section"].includes(
                          center.type,
                        )
                      ? "documents"
                      : "relations",
                  center,
                )
              }
            >
              <SquareArrowOutUpRight size={15} />
              打开对象上下文
            </Button>
          </Panel>
          <Panel title="关系图例">
            <div className="graph-legend">
              <div>
                <span className="legend-line" />
                <section>
                  <strong>已核对运行关联</strong>
                  <small>关联有效且运行版本匹配；不表示完整执行链</small>
                </section>
              </div>
              <div>
                <span className="legend-line dashed" />
                <section>
                  <strong>静态候选 / 未知关联</strong>
                  <small>
                    虚线为静态潜在关联，点线为未建立运行依据；有效性和因果依据分别显示
                  </small>
                </section>
              </div>
              <div>
                <ShieldCheck size={18} />
                <section>
                  <strong>固定版本与证据</strong>
                  <small>不自动补齐没有依据的技术链路</small>
                </section>
              </div>
            </div>
          </Panel>
        </div>
      </div>
    </>
  );
}

interface ToolEntry {
  name: string;
  description: string;
  requiredScope?: string;
  required_scopes?: string[];
  scope?: string;
  annotations?: { readOnlyHint?: boolean };
  [key: string]: unknown;
}
export function Settings() {
  const { w, run, busy, notify, session } = useWorkspace();
  const canManage = usePermission("project:manage"),
    canAudit = usePermission("audit:read");
  const [tokens, setTokens] = useState<Token[]>([]),
    [tools, setTools] = useState<ToolEntry[]>([]),
    [catalogError, setCatalogError] = useState(""),
    [modal, setModal] = useState(false),
    [tokenName, setTokenName] = useState(""),
    [scopes, setScopes] = useState(["knowledge:read"]),
    [newToken, setNewToken] = useState(""),
    [activeTab, setActiveTab] = useState(canManage ? "接入与权限" : "工具目录");
  useEffect(() => {
    setCatalogError("");
    if (canManage)
      void api<Token[]>(`/projects/${w.project.id}/tokens`)
        .then(setTokens)
        .catch((e) => setCatalogError(e.message));
    else {
      setTokens([]);
      setActiveTab((previous) =>
        previous === "接入与权限" ? "工具目录" : previous,
      );
    }
    void api<{ tools: ToolEntry[] }>("/mcp/catalog")
      .then((c) => setTools(c.tools || []))
      .catch((e) => setCatalogError(e.message));
  }, [w.project.id, canManage]);
  async function create() {
    const t = await run(
      () =>
        api<Token & { token: string }>(
          `/projects/${w.project.id}/tokens`,
          mutation("POST", { name: tokenName, scopes, expires_in_days: 30 }),
        ),
      "客户端凭证已创建，仅本次展示明文",
    );
    if (t) {
      setNewToken(t.token);
      setTokens((previous) => [t, ...previous]);
      setTokenName("");
    }
  }
  const config = JSON.stringify(
    {
      mcpServers: {
        vks: {
          command: "uv",
          args: ["run", "python", "-m", "vks_api.mcp_gateway"],
          env: { PYTHONPATH: "services/api:." },
        },
      },
    },
    null,
    2,
  );
  const capabilityNames: Record<string, string> = {
    bundle_import: "离线资料包导入",
    explicit_bindings: "显式绑定清单",
    markdown: "Markdown 章节解析",
    openapi_index: "OpenAPI 接口索引",
    safe_explanation: "安全原型讲解",
    direct_knowledge_save: "直接保存知识",
    immutable_releases: "不可变知识发布",
    scoped_tokens: "按项目授权的令牌",
    mcp_stdio: "MCP stdio 接入",
    source_connectors: "目录 / Git / SVN 只读采集",
    direct_import: "独立文档与原型导入",
    java_ast: "Java AST 保守静态解析",
    java_static_analysis: "Java AST 保守静态解析",
    version_proposals: "变更提案与有效性复核",
    oauth: "登记公钥的 MCP OAuth",
    controlled_runner: "受控浏览器录制",
    model_gateway: "按项目策略的模型网关",
    mcp_http_oauth: "MCP HTTP OAuth",
    java_spring_mybatis_ast: "Java / Spring / MyBatis 保守 AST 解析",
    immutable_scenarios: "不可变录制与讲解场景",
    controlled_browser_runner: "按环境配置的浏览器录制",
    source_snapshots: "正规化不可变来源快照",
    client_readonly_collectors: "目录 / Git / SVN 客户端只读采集",
    optional_model_gateway: "按预算开放的可选模型辅助",
  };
  const capabilities = Array.isArray(w.capabilities)
    ? (w.capabilities as string[])
    : Object.entries(w.capabilities)
        .filter(([, v]) => v)
        .map(([k]) => k);
  return (
    <>
      <div className="settings-tabs">
        {[
          ...(canManage ? ["接入与权限", "来源连接", "模型与预算"] : []),
          ...(canManage || session.user?.instance_admin ? ["OAuth 授权"] : []),
          "团队与账户",
          "项目资料",
          "项目模块",
          "工具目录",
          ...(canAudit ? ["审计日志"] : []),
          "项目能力",
        ].map((t) => (
          <button
            key={t}
            className={activeTab === t ? "active" : ""}
            onClick={() => setActiveTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      {catalogError && <Notice tone="red">{catalogError}</Notice>}
      {activeTab === "OAuth 授权" ? (
        <OAuthSettings />
      ) : activeTab === "模型与预算" ? (
        <ModelSettings />
      ) : activeTab === "团队与账户" ? (
        <TeamSettings />
      ) : activeTab === "项目资料" ? (
        <>
          <ProjectProfile />
        </>
      ) : activeTab === "项目模块" ? (
        <ModuleTree />
      ) : activeTab === "来源连接" ? (
        <SourceManagement />
      ) : activeTab === "接入与权限" ? (
        <>
          <div className="settings-stats">
            <div>
              <span className="stat-icon blue">
                <Link2 size={24} />
              </span>
              <section>
                <span>有效客户端凭证</span>
                <strong>
                  {
                    tokens.filter(
                      (t) =>
                        !(t as Token & { revoked: boolean }).revoked &&
                        !t.revoked_at,
                    ).length
                  }
                </strong>
              </section>
            </div>
            <div>
              <span className="stat-icon green">
                <ShieldCheck size={24} />
              </span>
              <section>
                <span>项目授权范围</span>
                <strong>当前项目</strong>
              </section>
            </div>
            <div>
              <span className="stat-icon amber">
                <LockKeyhole size={24} />
              </span>
              <section>
                <span>知识保存</span>
                <strong>修改直接生效，版本一键保存</strong>
              </section>
            </div>
          </div>
          <div className="settings-layout">
            <div>
              <Panel
                title="客户端与权限"
                action={
                  <Button
                    kind="primary small"
                    onClick={() => {
                      setModal(true);
                      setNewToken("");
                      setScopes(["knowledge:read"]);
                    }}
                  >
                    <Plus size={15} />
                    创建客户端
                  </Button>
                }
              >
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>客户端</th>
                        <th>授予权限</th>
                        <th>到期时间</th>
                        <th>状态</th>
                        <th>操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tokens.map((t) => (
                        <tr key={t.id}>
                          <td>
                            <span className="table-name">
                              <Terminal size={17} />
                              {t.name}
                            </span>
                          </td>
                          <td>
                            <div className="row wrap">
                              {t.scopes.map((s) => (
                                <Tag key={s}>{s}</Tag>
                              ))}
                            </div>
                          </td>
                          <td>{date(t.expires_at)}</td>
                          <td>
                            <Tag
                              value={
                                (t as Token & { revoked: boolean }).revoked ||
                                t.revoked_at
                                  ? "obsolete"
                                  : "valid"
                              }
                            >
                              {(t as Token & { revoked: boolean }).revoked ||
                              t.revoked_at
                                ? "已撤销"
                                : "有效"}
                            </Tag>
                          </td>
                          <td>
                            <button
                              className="text-button danger-text"
                              disabled={
                                busy ||
                                !!(t as Token & { revoked: boolean }).revoked ||
                                !!t.revoked_at
                              }
                              onClick={() => {
                                void run(
                                  () =>
                                    api(
                                      `/projects/${w.project.id}/tokens/${t.id}`,
                                      { method: "DELETE" },
                                    ),
                                  "令牌已撤销，后续请求将被拒绝",
                                ).then((r) => {
                                  if (r)
                                    setTokens((old) =>
                                      old.map((x) =>
                                        x.id === t.id
                                          ? {
                                              ...x,
                                              revoked_at:
                                                new Date().toISOString(),
                                            }
                                          : x,
                                      ),
                                    );
                                });
                              }}
                            >
                              撤销
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!tokens.length && (
                    <Empty
                      title="还没有 AI 客户端凭证"
                      description="创建只读令牌，让客户端在当前项目范围内读取已发布知识。"
                      icon="link"
                    />
                  )}
                </div>
              </Panel>
              <Panel
                title="连接配置"
                action={
                  <Button
                    kind="small"
                    onClick={() => {
                      void navigator.clipboard
                        .writeText(config)
                        .then(() =>
                          notify(
                            "MCP 配置已复制；启动客户端前请设置 VKS_TOKEN 环境变量",
                          ),
                        );
                    }}
                  >
                    <Copy size={14} />
                    复制配置
                  </Button>
                }
              >
                <div className="connection-info">
                  <span>
                    <Terminal size={15} />
                    官方 MCP SDK · stdio
                  </span>
                  <Tag>本机接入</Tag>
                </div>
                <pre className="config-block">{config}</pre>
                <Notice>
                  在项目根目录运行客户端命令。启动 MCP
                  客户端前，请在客户端所在进程环境中设置 <code>VKS_TOKEN</code>
                  ；服务会继承该变量，配置 JSON 不包含秘密。接入详情见项目
                  README。
                </Notice>
              </Panel>
            </div>
            <div>
              <Panel title="权限边界">
                <div className="permission-illustration">
                  <ShieldCheck size={42} />
                </div>
                <h3>读取、导入与提案分别授权</h3>
                <p className="section-description">
                  新凭证默认只有知识读取权限。每次请求同时校验工具
                  scope、项目范围与对象归属。
                </p>
                <div className="permission-rule">
                  <Check size={16} />
                  AI 可以提交候选关系
                </div>
                <div className="permission-rule">
                  <Check size={16} />
                  后台任务完成后可断线重连
                </div>
                <div className="permission-rule">
                  <LockKeyhole size={16} />
                  人工知识保存不向令牌开放
                </div>
              </Panel>
              <Panel title="运行环境">
                <div className="environment-row">
                  <span>模型辅助</span>
                  <Tag
                    value={w.runtime_status?.model_enabled ? "valid" : "static"}
                  >
                    {w.runtime_status?.model_enabled ? "项目已启用" : "未启用"}
                  </Tag>
                </div>
                <div className="environment-row">
                  <span>受控浏览器 Runner</span>
                  <Tag
                    value={
                      w.runtime_status?.runner_enabled ? "valid" : "static"
                    }
                  >
                    {w.runtime_status?.runner_enabled
                      ? "可用 · 按环境配置执行"
                      : "未启用"}
                  </Tag>
                </div>
                <div className="environment-row">
                  <span>MCP OAuth</span>
                  <Tag
                    value={w.runtime_status?.oauth_enabled ? "valid" : "static"}
                  >
                    {w.runtime_status?.oauth_enabled
                      ? "已登记授权服务"
                      : "未登记授权服务"}
                  </Tag>
                </div>
                <div className="environment-row">
                  <span>Java / Spring / MyBatis</span>
                  <Tag value="static">保守 AST 解析</Tag>
                </div>
                <div className="environment-row">
                  <span>任务调度</span>
                  <Tag>
                    {w.runtime_status?.task_mode === "celery"
                      ? "Celery"
                      : w.runtime_status?.task_mode === "external"
                        ? "外部 Worker"
                        : "本地持久化"}
                  </Tag>
                </div>
                <Notice>
                  无模型时，仍可导入、精确检索、编辑知识与读取固定版本。
                </Notice>
              </Panel>
            </div>
          </div>
        </>
      ) : activeTab === "工具目录" ? (
        <Panel title="MCP 工具目录" action={<Tag>{tools.length} 个工具</Tag>}>
          <Notice>
            工具 annotations
            是客户端提示；服务端仍逐次检查权限。工具与网页共用知识读写服务。
          </Notice>
          <div className="tool-catalog">
            {tools.map((t) => (
              <div key={t.name}>
                <span className="tool-icon">
                  <Terminal size={19} />
                </span>
                <section>
                  <h3>{t.name}</h3>
                  <p>{t.description}</p>
                </section>
                <Tag value={t.annotations?.readOnlyHint ? "static" : "manual"}>
                  {t.annotations?.readOnlyHint ? "只读" : "受控操作"}
                </Tag>
              </div>
            ))}
          </div>
        </Panel>
      ) : activeTab === "审计日志" ? (
        <AuditSearch />
      ) : (
        <div className="settings-layout">
          <Panel title="当前可用能力">
            <div className="capabilities-list">
              {capabilities.map((c) => (
                <div key={c}>
                  <CheckCircle2 size={18} />
                  <strong>{capabilityNames[c] || c}</strong>
                  <Tag
                    value={
                      c === "mcp_http_oauth"
                        ? w.runtime_status?.oauth_enabled
                          ? "valid"
                          : "static"
                        : c === "optional_model_gateway"
                          ? w.runtime_status?.model_enabled
                            ? "valid"
                            : "static"
                          : c === "controlled_browser_runner"
                            ? w.runtime_status?.runner_enabled
                              ? "valid"
                              : "static"
                            : "valid"
                    }
                  >
                    {c === "mcp_http_oauth"
                      ? w.runtime_status?.oauth_enabled
                        ? "已登记"
                        : "待登记服务"
                      : c === "optional_model_gateway"
                        ? w.runtime_status?.model_enabled
                          ? "项目已启用"
                          : "待配置启用"
                        : c === "controlled_browser_runner"
                          ? w.runtime_status?.runner_enabled
                            ? "可用 · 可直接执行"
                            : "待启用"
                          : "已接入"}
                  </Tag>
                </div>
              ))}
            </div>
          </Panel>
          <Panel title="已知边界">
            <Notice tone="amber">
              当前为 1.0 候选版。真实业务试点、跨环境兼容和性能门禁仍需验收。
            </Notice>
            {w.unknowns.map((u, i) => (
              <p key={i} className="capability-gap">
                <Info size={16} />
                {issueText(u)}
              </p>
            ))}
            <p className="capability-gap">
              <Info size={16} />
              未自动运行任何来源项目构建或安装脚本。
            </p>
          </Panel>
        </div>
      )}
      {modal && (
        <Modal
          title={newToken ? "保存客户端凭证" : "创建 MCP 客户端凭证"}
          onClose={() => {
            setModal(false);
            setNewToken("");
          }}
        >
          {newToken ? (
            <>
              <Notice tone="amber">
                令牌明文仅展示这一次。关闭后无法重新读取，请保存到本机受控的凭证配置中。
              </Notice>
              <div className="one-time-token">
                <code>{newToken}</code>
                <Button
                  onClick={() => {
                    void navigator.clipboard
                      .writeText(newToken)
                      .then(() => notify("客户端令牌已复制"));
                  }}
                >
                  <Copy size={15} />
                  复制令牌
                </Button>
              </div>
              <Button
                kind="primary full"
                onClick={() => {
                  setModal(false);
                  setNewToken("");
                }}
              >
                已安全保存
              </Button>
            </>
          ) : (
            <>
              <p className="muted">凭证仅可访问当前项目：{w.project.name}</p>
              <label>
                客户端名称
                <input
                  autoFocus
                  value={tokenName}
                  onChange={(e) => setTokenName(e.target.value)}
                  placeholder="例如：我的开发助手"
                  required
                  maxLength={160}
                />
              </label>
              <label>授予权限</label>
              <div className="scope-options">
                {[
                  {
                    s: "knowledge:read",
                    t: "读取知识",
                    d: "检索已发布知识、动作上下文和证据",
                  },
                  {
                    s: "knowledge:edit",
                    t: "访问与编辑草稿",
                    d: "读取未发布分析集合，并允许通过授权接口编辑草稿",
                  },
                  {
                    s: "source:read",
                    t: "读取源码证据",
                    d: "允许访问敏感源码与定位内容，默认不授予",
                  },
                  {
                    s: "source:import",
                    t: "导入资料",
                    d: "提交离线资料包，创建草稿",
                  },
                  {
                    s: "analysis:run",
                    t: "发起分析",
                    d: "运行受支持的确定性分析任务",
                  },
                  {
                    s: "links:propose",
                    t: "提交关联提案",
                    d: "生成并保存关联知识",
                  },
                  {
                    s: "knowledge:export",
                    t: "导出知识",
                    d: "导出授权版本中的知识材料",
                  },
                ].map((s) => (
                  <label className="scope-option" key={s.s}>
                    <input
                      type="checkbox"
                      checked={scopes.includes(s.s)}
                      disabled={s.s === "knowledge:read"}
                      onChange={(e) =>
                        setScopes((old) =>
                          e.target.checked
                            ? [...old, s.s]
                            : old.filter((x) => x !== s.s),
                        )
                      }
                    />
                    <span>
                      <strong>{s.t}</strong>
                      <small>{s.d}</small>
                    </span>
                    <code>{s.s}</code>
                  </label>
                ))}
              </div>
              <Notice>
                令牌有效期 30 天。AI 客户端仅使用所选择的项目权限。
              </Notice>
              <div className="modal-actions">
                <Button onClick={() => setModal(false)}>取消</Button>
                <Button
                  kind="primary"
                  disabled={busy || !tokenName.trim()}
                  onClick={() => {
                    void create();
                  }}
                >
                  <Plus size={15} />
                  创建凭证
                </Button>
              </div>
            </>
          )}
        </Modal>
      )}
    </>
  );
}

export { Relations } from "./Relations";
export { Versions } from "./Versions";
