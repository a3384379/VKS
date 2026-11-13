import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Download,
  GitBranch,
  Plus,
  Redo2,
  Save,
  Undo2,
} from "lucide-react";
import { api, mutation } from "./api";
import { Button, Empty, Notice, Tag } from "./components";
import { usePermission, useReadScope, useWorkspace } from "./context";
import { CaptureResults, type Capture } from "./CaptureResults";
import { JourneyMerge } from "./JourneyMerge";
import { arrangeJourney } from "./journey-editing";
import { JourneyReader } from "./JourneyReader";
import { JourneySourcePicker } from "./JourneySourcePicker";
import { resolveJourneyReading } from "./journey-navigation";
import { prototypeManifest } from "./prototype";
import { workspaceBaseline } from "./workspace-baseline";
import {
  journeyListRouteKey,
  journeyListRouteMatches,
} from "./journey-list-route";
import {
  journeyKinds,
  journeySemantics,
  journeyTarget,
} from "./journey-reading";
import "./journey-reading.css";
import {
  newJourney,
  prepareJourneyDraft,
  travelJourney,
  updateJourney,
  type JourneyDSL,
  type JourneyHistory,
  type JourneyNode,
  type JourneyRevision,
} from "./journeys";

const names = {
  page: "页面",
  action: "动作",
  step: "业务声明步骤",
  decision: "判断",
  branch: "分支",
  end: "结束",
  reference: "知识引用",
};
export function JourneyCanvas() {
  const {
    w,
    baseline,
    notify,
    refresh,
    navigate,
    navigateExplanation,
    journey,
    navigateJourney,
    setUnsavedChanges,
    session,
    selected: selectedEntity,
  } = useWorkspace();
  const canEdit = usePermission("knowledge:edit");
  const [small, setSmall] = useState(window.innerWidth < 768);
  const [mode, setMode] = useState<"read" | "maintain">("read");
  const actualBaseline = workspaceBaseline(w);
  const authorityScope = JSON.stringify(
    [
      ...(session.project_permissions?.[w.project.id] ?? session.scopes ?? []),
    ].sort(),
  );
  const visibleEntityIds = new Set(w.entities.map((row) => row.entity_id));
  const visibleScope = JSON.stringify([...visibleEntityIds].sort());
  const listScope = JSON.stringify([
    w.project.id,
    baseline,
    w.selected_analysis_set?.id,
    w.selected_release?.id,
    w.selected_analysis_set?.selection_version,
    selectedEntity,
    authorityScope,
    visibleScope,
  ]);
  const listScopeRef = useRef(listScope);
  listScopeRef.current = listScope;
  const routeState = {
    projectId: w.project.id,
    baseline,
    selectedEntity,
    journey,
  };
  const routeSignature = journeyListRouteKey(routeState);
  const latestRoute = useRef({
    signature: routeSignature,
    epoch: 0,
    state: routeState,
  });
  if (latestRoute.current.signature !== routeSignature)
    latestRoute.current = {
      signature: routeSignature,
      epoch: latestRoute.current.epoch + 1,
      state: routeState,
    };
  const captureScope = `${useReadScope()}:${selectedEntity}`;
  const captureScopeRef = useRef(captureScope);
  captureScopeRef.current = captureScope;
  const captureRequest = useRef<AbortController | null>(null);
  const [loadedAuthority, setLoadedAuthority] = useState(authorityScope);
  const editable =
    canEdit &&
    !w.selected_release &&
    w.selected_analysis_set?.state !== "frozen" &&
    !small;
  const [rows, setRows] = useState<JourneyRevision[]>([]),
    [loaded, setLoaded] = useState(false),
    [error, setError] = useState("");
  const [active, setActive] = useState<JourneyRevision | null>(null),
    [history, setHistory] = useState<JourneyHistory | null>(null),
    [saved, setSaved] = useState("");
  const [selected, setSelected] = useState(""),
    [edgeFrom, setEdgeFrom] = useState(""),
    [edgeTo, setEdgeTo] = useState(""),
    [edgeLabel, setEdgeLabel] = useState("");
  const [viewing, setViewing] = useState<{
    scope: string;
    capture: Capture;
    index: number;
  } | null>(null);
  useEffect(() => {
    setViewing(null);
    return () => captureRequest.current?.abort();
  }, [captureScope]);
  const drag = useRef<{
    key: string;
    x: number;
    y: number;
    startX: number;
    startY: number;
  } | null>(null);
  const dsl = history?.present,
    dirty = !!dsl && JSON.stringify(dsl) !== saved;
  const dirtyRef = useRef(dirty);
  const activeRef = useRef(active);
  dirtyRef.current = dirty;
  activeRef.current = active;
  useEffect(() => {
    if (!dirtyRef.current) setMode("read");
  }, [w.project.id, actualBaseline]);
  useEffect(() => {
    const listener = () => setSmall(window.innerWidth < 768);
    window.addEventListener("resize", listener);
    return () => window.removeEventListener("resize", listener);
  }, []);
  useEffect(() => {
    setUnsavedChanges(dirty);
    return () => setUnsavedChanges(false);
  }, [dirty, setUnsavedChanges]);
  useEffect(() => {
    if (dirtyRef.current) return;
    const controller = new AbortController();
    const requestedScope = listScope;
    const previousJourneyId = activeRef.current?.journey_id;
    setLoaded(false);
    setRows([]);
    setActive(null);
    setHistory(null);
    setError("");
    const query = new URLSearchParams();
    if (w.selected_release) query.set("release_id", w.selected_release.id);
    else if (w.selected_analysis_set)
      query.set("analysis_set_id", w.selected_analysis_set.id);
    void api<JourneyRevision[]>(
      `/api/v2/projects/${w.project.id}/journeys?${query}`,
      { signal: controller.signal },
    )
      .then((data) => {
        if (
          controller.signal.aborted ||
          dirtyRef.current ||
          listScopeRef.current !== requestedScope
        )
          return;
        // Reading position may change many times while this list is in flight.
        // It does not invalidate the list, but only the latest route may be normalized.
        const route = latestRoute.current;
        const currentJourney = route.state.journey;
        setRows(data);
        setLoadedAuthority(authorityScope);
        setLoaded(true);
        const target =
          data.find((row) => row.journey_id === route.state.selectedEntity) ||
          data.find((row) => row.journey_id === currentJourney.journeyId) ||
          data.find((row) => row.journey_id === previousJourneyId) ||
          data.find(
            (row) => journeySemantics(row.dsl).kind === "business_process",
          ) ||
          data[0];
        if (target) {
          open(target);
          if (!journeyListRouteMatches(window.location.hash, route.state))
            return;
          if (
            currentJourney.journeyId === target.journey_id &&
            currentJourney.revisionId &&
            currentJourney.revisionId !== target.revision_id
          )
            setError(
              "流程修订已变化，已回到当前可见版本起点；此前的步骤与分支未复用。",
            );
          const reading = resolveJourneyReading(
            target.dsl,
            target.revision_id,
            currentJourney.journeyId === target.journey_id
              ? currentJourney
              : {
                  journeyId: target.journey_id,
                  revisionId: "",
                  stepKey: "",
                  choices: {},
                },
          );
          if (
            latestRoute.current.epoch === route.epoch &&
            listScopeRef.current === requestedScope
          )
            navigateJourney({ journeyId: target.journey_id, ...reading }, true);
        }
      })
      .catch((e) => {
        if (
          !controller.signal.aborted &&
          listScopeRef.current === requestedScope
        ) {
          setError(e.message);
          setLoaded(true);
        }
      });
    return () => controller.abort();
  }, [listScope]);
  function open(row: JourneyRevision) {
    setActive(row);
    setHistory({ past: [], present: row.dsl, future: [] });
    setSaved(JSON.stringify(row.dsl));
    setSelected(row.dsl.entry);
  }
  function selectJourney(row: JourneyRevision) {
    open(row);
    navigateJourney({
      journeyId: row.journey_id,
      revisionId: row.revision_id,
      stepKey: row.dsl.entry,
      choices: {},
    });
  }
  function change(next: JourneyDSL) {
    setHistory((h) => (h ? updateJourney(h, next) : h));
  }
  function patchNode(key: string, patch: Partial<JourneyNode>) {
    if (dsl)
      change({
        ...dsl,
        nodes: dsl.nodes.map((n) => (n.key === key ? { ...n, ...patch } : n)),
      });
  }
  function openKnowledge(node: JourneyNode) {
    const target = journeyTarget(node, w.entities);
    if (target.kind === "unavailable") {
      setError(target.reason);
      return;
    }
    if (target.kind === "capture") {
      captureRequest.current?.abort();
      const controller = new AbortController();
      captureRequest.current = controller;
      const requestedScope = captureScope;
      const ref = target.reference;
      const query = new URLSearchParams(
        w.selected_release
          ? { release_id: w.selected_release.id }
          : { analysis_set_id: w.selected_analysis_set?.id || "" },
      );
      void api<Capture>(
        `/projects/${w.project.id}/captures/${ref.capture_id}?${query}`,
        { signal: controller.signal },
      )
        .then((capture) => {
          if (
            !controller.signal.aborted &&
            captureScopeRef.current === requestedScope
          )
            setViewing({
              scope: requestedScope,
              capture,
              index: ref.snapshot_index,
            });
        })
        .catch((e) => {
          if (!controller.signal.aborted) setError(e.message);
        });
      return;
    }
    if (target.kind === "state") {
      if (
        navigateExplanation({
          scenarioId: target.reference.scenario_id,
          stateKey: target.reference.state_key,
          followTarget: false,
        })
      )
        navigate("workbench");
      return;
    }
    const entity = target.entity;
    if (entity.type === "action" || entity.type === "page") {
      if (
        navigateExplanation(
          entity.type === "action"
            ? {
                actionId: entity.id,
                followTarget: false,
                ...(node.prototype_state_key
                  ? {
                      stateKey: node.prototype_state_key,
                      triggerStateKey: node.prototype_state_key,
                    }
                  : {}),
              }
            : {
                pageId: entity.id,
                followTarget: false,
                actionId: "",
                stateKey: node.prototype_state_key || "",
              },
        )
      )
        navigate("workbench");
    } else
      navigate(
        entity.type === "document" || entity.type === "section"
          ? "documents"
          : "graph",
        entity,
      );
  }
  async function save() {
    if (!dsl || !w.selected_analysis_set) return;
    setError("");
    const path = active
      ? `/api/v2/journeys/${active.journey_id}/revisions`
      : `/api/v2/projects/${w.project.id}/journeys`;
    try {
      const init = mutation("POST", {
        analysis_set_id: w.selected_analysis_set.id,
        dsl: prepareJourneyDraft(dsl),
      });
      if (active)
        (init.headers as Record<string, string>)["If-Match"] =
          `"${active.revision_id}"`;
      const result = await api<JourneyRevision>(path, init);
      setUnsavedChanges(false);
      open(result);
      // Re-read the permission-filtered selection before exposing a newly
      // created Journey; the preceding workspace cannot contain its identity.
      await refresh();
      navigateJourney(
        {
          journeyId: result.journey_id,
          revisionId: result.revision_id,
          stepKey: result.dsl.entry,
          choices: {},
        },
        true,
      );
      setRows((r) => [
        result,
        ...r.filter((x) => x.journey_id !== result.journey_id),
      ]);
      notify("流程已保存为不可变修订");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const node = dsl?.nodes.find((n) => n.key === selected);
  const bindingEntities = w.entities.filter((e) =>
    node?.kind === "page"
      ? e.type === "page"
      : node?.kind === "action"
        ? e.type === "action"
        : [
            "document",
            "section",
            "api",
            "symbol",
            "sql",
            "table",
            "scenario",
          ].includes(e.type),
  );
  const readableRows = rows.filter((row) =>
    visibleEntityIds.has(row.journey_id),
  );
  const requiredIds = dsl
    ? [
        ...(dsl.semantics?.source_refs || []).map((ref) => ref.entity_id),
        ...dsl.nodes.flatMap((node) =>
          [
            node.binding?.entity_id,
            node.state_ref?.scenario_id,
            node.capture_ref?.scenario_id,
          ].filter((id): id is string => !!id),
        ),
      ]
    : [];
  const readableDraft =
    (!active || visibleEntityIds.has(active.journey_id)) &&
    requiredIds.every((id) => visibleEntityIds.has(id));
  return (
    <div className="journey-workspace">
      <Panel
        title={mode === "read" ? "业务导览" : "导览维护"}
        action={
          <div className="row">
            <Tag>只读讲解 · 当前知识版本</Tag>
            {canEdit && !small && (
              <Button
                onClick={() => setMode(mode === "read" ? "maintain" : "read")}
              >
                {mode === "read" ? "维护导览与结构" : "返回业务导览"}
              </Button>
            )}
            {editable && mode === "maintain" && (
              <Button
                kind="primary small"
                disabled={dirty}
                onClick={() => {
                  const next = newJourney(
                    w.actions.find((a) => a.id === selectedEntity) ||
                      w.actions[0],
                  );
                  setActive(null);
                  setHistory({ past: [], present: next, future: [] });
                  setSaved("");
                  setSelected(next.entry);
                }}
              >
                <Plus size={14} />
                新建流程
              </Button>
            )}
          </div>
        }
      >
        <p className="muted">
          {mode === "read"
            ? "从真实业务目标理解页面、规则、接口、实现与文档。点击步骤只打开已保存的同版知识。"
            : "维护用于补充业务含义、来源及结构。结构分类不能替代真实业务验收。"}
        </p>
        {error && (
          <Notice tone="red">
            {error}
            {dirty && "。本地编辑保留；可在维护模式比较更新或另存。"}
          </Notice>
        )}
        {small && (
          <Notice>窄屏提供业务导览与知识导航；维护请使用桌面视图。</Notice>
        )}
        {loadedAuthority !== authorityScope || !readableDraft ? (
          <Notice>
            当前可见资料或权限已变化，已暂停显示此前读取的导览。
            {dirty
              ? "未保存内容保留在本地；恢复原权限后可继续维护。"
              : "正在读取当前可见材料。"}
          </Notice>
        ) : mode === "read" ? (
          <JourneyReader
            rows={readableRows}
            active={active}
            dsl={dsl}
            entities={w.entities}
            loaded={loaded}
            dirty={dirty}
            navigation={journey}
            onNavigate={navigateJourney}
            onSelect={selectJourney}
            onOpen={openKnowledge}
          />
        ) : (
          <>
            <div className="journey-list">
              {readableRows.map((row) => (
                <button
                  key={row.journey_id}
                  aria-pressed={active?.journey_id === row.journey_id}
                  disabled={dirty}
                  onClick={() => selectJourney(row)}
                >
                  <GitBranch size={16} />
                  {row.dsl.title}
                  <small>{row.dsl.nodes.length} 节点</small>
                </button>
              ))}
            </div>
            {!dsl ? (
              <Empty
                title={loaded ? "还没有业务流程" : "正在读取流程"}
                description="从已有动作创建流程，或运行知识构建中的业务流程模板。"
              />
            ) : (
              <>
                <div className="journey-toolbar">
                  <label>
                    流程名称
                    <input
                      aria-label="流程名称"
                      value={dsl.title}
                      readOnly={!editable}
                      onChange={(e) =>
                        change({ ...dsl, title: e.target.value })
                      }
                    />
                  </label>
                  <Tag>{dirty ? "未保存" : "固定修订"}</Tag>
                  {editable && (
                    <Button
                      onClick={() => {
                        try {
                          change(arrangeJourney(dsl));
                        } catch (e) {
                          setError((e as Error).message);
                        }
                      }}
                    >
                      自动排列
                    </Button>
                  )}
                  {editable && active && saved && dirty && (
                    <JourneyMerge
                      key={active.revision_id}
                      active={active}
                      base={JSON.parse(saved) as JourneyDSL}
                      local={dsl}
                      onApply={(next, remote) => {
                        setActive(remote);
                        setSaved(JSON.stringify(remote.dsl));
                        change(next);
                        setError("");
                      }}
                    />
                  )}
                  <Button
                    disabled={!history?.past.length || !editable}
                    onClick={() =>
                      setHistory((h) => (h ? travelJourney(h, "undo") : h))
                    }
                  >
                    <Undo2 size={14} />
                    撤销
                  </Button>
                  <Button
                    disabled={!history?.future.length || !editable}
                    onClick={() =>
                      setHistory((h) => (h ? travelJourney(h, "redo") : h))
                    }
                  >
                    <Redo2 size={14} />
                    重做
                  </Button>
                  <Button
                    onClick={() => {
                      const url = URL.createObjectURL(
                        new Blob([JSON.stringify(dsl, null, 2)], {
                          type: "application/json",
                        }),
                      );
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = dsl.key + ".json";
                      a.click();
                      URL.revokeObjectURL(url);
                    }}
                  >
                    <Download size={14} />
                    导出 DSL
                  </Button>
                  {editable && (
                    <>
                      <label className="button">
                        导入 DSL
                        <input
                          aria-label="导入流程 DSL"
                          type="file"
                          accept="application/json,.json"
                          onChange={async (e) => {
                            const f = e.target.files?.[0];
                            if (!f) return;
                            try {
                              if (f.size > 200000)
                                throw Error("DSL 超出大小上限");
                              const obj = JSON.parse(await f.text());
                              if (
                                !obj ||
                                obj.schema_version !== "2.0" ||
                                !Array.isArray(obj.nodes) ||
                                obj.nodes.length > 120 ||
                                !Array.isArray(obj.edges) ||
                                obj.edges.length > 240
                              )
                                throw Error("无效流程 DSL");
                              const checked = await api<{ dsl: JourneyDSL }>(
                                `/api/v2/projects/${w.project.id}/journeys/validate`,
                                mutation("POST", {
                                  analysis_set_id: w.selected_analysis_set?.id,
                                  dsl: { ...obj, key: dsl.key },
                                }),
                              );
                              change(checked.dsl);
                            } catch (e) {
                              setError((e as Error).message);
                            }
                          }}
                        />
                      </label>
                      <Button
                        kind="primary"
                        onClick={() => void save()}
                        disabled={!dirty}
                      >
                        <Save size={14} />
                        保存流程
                      </Button>
                      <Button
                        disabled={!dirty}
                        onClick={() => {
                          if (active) open(active);
                          else {
                            setHistory(null);
                            setUnsavedChanges(false);
                          }
                        }}
                      >
                        还原
                      </Button>
                    </>
                  )}
                </div>
                <fieldset className="journey-meaning" disabled={!editable}>
                  <legend>导览内容与依据</legend>
                  <label>
                    材料类型
                    <select
                      aria-label="材料类型"
                      value={journeySemantics(dsl).kind}
                      onChange={(e) =>
                        change({
                          ...dsl,
                          semantics: {
                            ...journeySemantics(dsl),
                            kind: e.target.value as NonNullable<
                              JourneyDSL["semantics"]
                            >["kind"],
                          },
                        })
                      }
                    >
                      {Object.entries(journeyKinds).map(([value, kind]) => (
                        <option key={value} value={value}>
                          {kind.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    说明依据
                    <select
                      aria-label="说明依据"
                      value={journeySemantics(dsl).basis}
                      onChange={(e) =>
                        change({
                          ...dsl,
                          semantics: {
                            ...journeySemantics(dsl),
                            basis: e.target.value as NonNullable<
                              JourneyDSL["semantics"]
                            >["basis"],
                          },
                        })
                      }
                    >
                      <option value="declared">人员声明</option>
                      <option value="observed">保存的观察</option>
                      <option value="inferred">推断候选</option>
                      <option value="unknown">依据未说明</option>
                    </select>
                  </label>
                  <label>
                    业务目标或材料用途
                    <textarea
                      aria-label="业务目标或材料用途"
                      maxLength={2000}
                      value={dsl.semantics?.purpose || ""}
                      onChange={(e) =>
                        change({
                          ...dsl,
                          semantics: {
                            ...journeySemantics(dsl),
                            purpose: e.target.value,
                          },
                        })
                      }
                    />
                  </label>
                  <label>
                    前置条件（每行一项）
                    <textarea
                      aria-label="前置条件"
                      value={dsl.semantics?.preconditions?.join("\n") || ""}
                      onChange={(e) =>
                        change({
                          ...dsl,
                          semantics: {
                            ...journeySemantics(dsl),
                            preconditions: e.target.value.split("\n"),
                          },
                        })
                      }
                    />
                  </label>
                  <label>
                    预期结果
                    <textarea
                      aria-label="预期结果"
                      maxLength={2000}
                      value={dsl.semantics?.outcome || ""}
                      onChange={(e) =>
                        change({
                          ...dsl,
                          semantics: {
                            ...journeySemantics(dsl),
                            outcome: e.target.value,
                          },
                        })
                      }
                    />
                  </label>
                  <label>
                    缺口与待核对内容（每行一项）
                    <textarea
                      aria-label="缺口与待核对内容"
                      value={dsl.semantics?.unknowns?.join("\n") || ""}
                      onChange={(e) =>
                        change({
                          ...dsl,
                          semantics: {
                            ...journeySemantics(dsl),
                            unknowns: e.target.value.split("\n"),
                          },
                        })
                      }
                    />
                  </label>
                  <JourneySourcePicker
                    entities={w.entities}
                    value={dsl.semantics?.source_refs || []}
                    onChange={(source_refs) =>
                      change({
                        ...dsl,
                        semantics: { ...journeySemantics(dsl), source_refs },
                      })
                    }
                  />
                </fieldset>
                <div className="journey-editor">
                  <div className="journey-scroll" aria-label="业务流程画布">
                    <div
                      className="journey-board"
                      style={{
                        width: Math.max(
                          740,
                          ...dsl.nodes.map((n) => n.x + 240),
                        ),
                        height: Math.max(
                          360,
                          ...dsl.nodes.map((n) => n.y + 120),
                        ),
                      }}
                    >
                      <svg className="journey-edges" aria-hidden="true">
                        {dsl.edges.map((edge) => {
                          const from = dsl.nodes.find(
                              (n) => n.key === edge.source,
                            ),
                            to = dsl.nodes.find((n) => n.key === edge.target);
                          if (!from || !to) return null;
                          return (
                            <g key={edge.key}>
                              <path
                                d={`M${from.x + 180} ${from.y + 35} C${from.x + 220} ${from.y + 35},${to.x - 30} ${to.y + 35},${to.x} ${to.y + 35}`}
                              />
                              <text
                                x={(from.x + 180 + to.x) / 2}
                                y={(from.y + to.y) / 2 + 24}
                              >
                                {edge.label}
                              </text>
                            </g>
                          );
                        })}
                      </svg>
                      {dsl.nodes.map((n) => (
                        <button
                          key={n.key}
                          className={`journey-node ${n.kind} ${selected === n.key ? "selected" : ""}`}
                          style={{ left: n.x, top: n.y }}
                          aria-label={`${names[n.kind]}：${n.title}`}
                          aria-pressed={selected === n.key}
                          onClick={() => setSelected(n.key)}
                          onPointerDown={(e) => {
                            setSelected(n.key);
                            if (editable) {
                              drag.current = {
                                key: n.key,
                                x: n.x,
                                y: n.y,
                                startX: e.clientX,
                                startY: e.clientY,
                              };
                              e.currentTarget.setPointerCapture(e.pointerId);
                            }
                          }}
                          onPointerUp={(e) => {
                            const p = drag.current;
                            e.currentTarget.style.transform = "";
                            if (p && p.key === n.key) {
                              patchNode(n.key, {
                                x: Math.max(
                                  0,
                                  Math.min(
                                    4000,
                                    Math.round(p.x + e.clientX - p.startX),
                                  ),
                                ),
                                y: Math.max(
                                  0,
                                  Math.min(
                                    4000,
                                    Math.round(p.y + e.clientY - p.startY),
                                  ),
                                ),
                              });
                              drag.current = null;
                            }
                          }}
                          onPointerMove={(e) => {
                            const p = drag.current;
                            if (p && p.key === n.key)
                              e.currentTarget.style.transform = `translate(${e.clientX - p.startX}px, ${e.clientY - p.startY}px)`;
                          }}
                          onPointerCancel={(e) => {
                            e.currentTarget.style.transform = "";
                            drag.current = null;
                          }}
                          onKeyDown={(e) => {
                            const delta: Record<string, [number, number]> = {
                              ArrowLeft: [-20, 0],
                              ArrowRight: [20, 0],
                              ArrowUp: [0, -20],
                              ArrowDown: [0, 20],
                            };
                            if (editable && delta[e.key]) {
                              e.preventDefault();
                              const [x, y] = delta[e.key];
                              patchNode(n.key, {
                                x: Math.min(4000, Math.max(0, n.x + x)),
                                y: Math.min(4000, Math.max(0, n.y + y)),
                              });
                            }
                          }}
                        >
                          <span>
                            {names[n.kind]}
                            {dsl.entry === n.key ? " · 入口" : ""}
                          </span>
                          <strong>{n.title}</strong>
                          <small>{n.binding ? "固定修订" : "声明节点"}</small>
                        </button>
                      ))}
                    </div>
                  </div>
                  <aside className="journey-inspector">
                    <h3>节点与知识</h3>
                    {node && (
                      <>
                        <label>
                          节点名称
                          <input
                            aria-label="节点名称"
                            readOnly={!editable}
                            value={node.title}
                            onChange={(e) =>
                              patchNode(node.key, { title: e.target.value })
                            }
                          />
                        </label>
                        <p>
                          {names[node.kind]} · {node.key}
                        </p>
                        <label>
                          步骤说明
                          <textarea
                            aria-label="步骤说明"
                            readOnly={!editable}
                            value={node.description || ""}
                            maxLength={2000}
                            onChange={(e) =>
                              patchNode(node.key, {
                                description: e.target.value,
                              })
                            }
                          />
                        </label>
                        {editable && (
                          <>
                            <label>
                              节点类型
                              <select
                                aria-label="节点类型"
                                value={node.kind}
                                onChange={(e) => {
                                  const {
                                    binding: _,
                                    state_ref: _s,
                                    capture_ref: _c,
                                    prototype_state_key: _p,
                                    ...rest
                                  } = node;
                                  change({
                                    ...dsl,
                                    nodes: dsl.nodes.map((n) =>
                                      n.key === node.key
                                        ? {
                                            ...rest,
                                            kind: e.target
                                              .value as JourneyNode["kind"],
                                          }
                                        : n,
                                    ),
                                  });
                                }}
                              >
                                {Object.entries(names).map(([kind, title]) => (
                                  <option key={kind} value={kind}>
                                    {title}
                                  </option>
                                ))}
                              </select>
                            </label>
                            {["page", "action", "reference"].includes(
                              node.kind,
                            ) && (
                              <label>
                                固定知识引用
                                <select
                                  aria-label="固定知识引用"
                                  value={node.binding?.entity_id || ""}
                                  onChange={(e) => {
                                    const entity = bindingEntities.find(
                                      (x) => x.id === e.target.value,
                                    );
                                    if (entity)
                                      patchNode(node.key, {
                                        title: entity.name,
                                        prototype_state_key: undefined,
                                        binding: {
                                          entity_id: entity.id,
                                          revision_id:
                                            entity.entity_revision_id,
                                        },
                                      });
                                  }}
                                >
                                  <option value="">请选择当前版本知识</option>
                                  {bindingEntities.map((e) => (
                                    <option key={e.id} value={e.id}>
                                      {e.type} · {e.name}
                                    </option>
                                  ))}
                                </select>
                              </label>
                            )}
                            {node.binding &&
                              !node.state_ref &&
                              !node.capture_ref &&
                              (() => {
                                const selected = bindingEntities.find(
                                  (item) => item.id === node.binding?.entity_id,
                                );
                                const page =
                                  selected?.type === "page"
                                    ? selected
                                    : w.pages.find(
                                        (item) =>
                                          item.stable_key ===
                                          selected?.metadata.page_key,
                                      );
                                const states = prototypeManifest(
                                  page,
                                )?.states.filter(
                                  (state) =>
                                    selected?.type === "page" ||
                                    state.anchors.some(
                                      (anchor) =>
                                        anchor.actionKey ===
                                        selected?.stable_key,
                                    ),
                                );
                                return states?.length ? (
                                  <label>
                                    原型状态
                                    <select
                                      value={node.prototype_state_key || ""}
                                      onChange={(event) =>
                                        patchNode(node.key, {
                                          prototype_state_key:
                                            event.target.value || undefined,
                                        })
                                      }
                                    >
                                      <option value="">默认状态</option>
                                      {states.map((state) => (
                                        <option
                                          key={state.stateKey}
                                          value={state.stateKey}
                                        >
                                          {state.name}
                                        </option>
                                      ))}
                                    </select>
                                  </label>
                                ) : null;
                              })()}
                            {(node.state_ref || node.capture_ref) && (
                              <Button
                                onClick={() => {
                                  const {
                                    state_ref: _s,
                                    capture_ref: _c,
                                    ...rest
                                  } = node;
                                  change({
                                    ...dsl,
                                    nodes: dsl.nodes.map((n) =>
                                      n.key === node.key ? rest : n,
                                    ),
                                  });
                                }}
                              >
                                移除回放引用
                              </Button>
                            )}
                            <Button
                              onClick={() =>
                                change({ ...dsl, entry: node.key })
                              }
                            >
                              设为入口
                            </Button>
                            <Button
                              onClick={() =>
                                change({
                                  ...dsl,
                                  nodes: dsl.nodes.filter(
                                    (n) => n.key !== node.key,
                                  ),
                                  edges: dsl.edges.filter(
                                    (e) =>
                                      e.source !== node.key &&
                                      e.target !== node.key,
                                  ),
                                })
                              }
                            >
                              删除节点
                            </Button>
                          </>
                        )}
                        {(node.binding ||
                          node.state_ref ||
                          node.capture_ref) && (
                          <Button onClick={() => openKnowledge(node)}>
                            {node.capture_ref
                              ? "查看录制快照"
                              : node.state_ref
                                ? "查看被动状态"
                                : "查看同版知识"}{" "}
                            <ArrowRight size={14} />
                          </Button>
                        )}
                      </>
                    )}
                    {editable && (
                      <>
                        <hr />
                        <Button
                          onClick={() => {
                            const key =
                              "node-" + crypto.randomUUID().slice(0, 8);
                            change({
                              ...dsl,
                              nodes: [
                                ...dsl.nodes,
                                {
                                  key,
                                  kind: "branch",
                                  title: "新分支",
                                  x: 60,
                                  y: 80 + dsl.nodes.length * 60,
                                },
                              ],
                            });
                            setSelected(key);
                          }}
                          disabled={dsl.nodes.length >= 120}
                        >
                          <Plus size={14} />
                          添加节点
                        </Button>
                        <label>
                          连接起点
                          <select
                            aria-label="连接起点"
                            value={edgeFrom}
                            onChange={(e) => setEdgeFrom(e.target.value)}
                          >
                            <option value="">选择节点</option>
                            {dsl.nodes.map((n) => (
                              <option key={n.key} value={n.key}>
                                {n.title}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          连接终点
                          <select
                            aria-label="连接终点"
                            value={edgeTo}
                            onChange={(e) => setEdgeTo(e.target.value)}
                          >
                            <option value="">选择节点</option>
                            {dsl.nodes.map((n) => (
                              <option key={n.key} value={n.key}>
                                {n.title}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          分支条件
                          <input
                            aria-label="分支条件"
                            value={edgeLabel}
                            onChange={(e) => setEdgeLabel(e.target.value)}
                          />
                        </label>
                        <Button
                          disabled={!edgeFrom || !edgeTo || edgeFrom === edgeTo}
                          onClick={() => {
                            change({
                              ...dsl,
                              edges: [
                                ...dsl.edges,
                                {
                                  key:
                                    "edge-" + crypto.randomUUID().slice(0, 8),
                                  source: edgeFrom,
                                  target: edgeTo,
                                  label: edgeLabel,
                                },
                              ],
                            });
                            setEdgeLabel("");
                          }}
                        >
                          连接节点
                        </Button>
                        {dsl.edges.map((edge) => (
                          <button
                            className="journey-edge-delete"
                            key={edge.key}
                            onClick={() =>
                              change({
                                ...dsl,
                                edges: dsl.edges.filter(
                                  (e) => e.key !== edge.key,
                                ),
                              })
                            }
                          >
                            {edge.source} → {edge.target} ·{" "}
                            {edge.label || "无条件"} ×
                          </button>
                        ))}
                      </>
                    )}
                  </aside>
                </div>
                <p className="muted">
                  拖动节点或使用方向键移动；连接、分支和引用由服务器校验。
                  {active && ` 修订 ${active.revision_id}`}
                </p>
              </>
            )}
          </>
        )}
      </Panel>
      {viewing?.scope === captureScope &&
        loadedAuthority === authorityScope &&
        readableDraft && (
          <CaptureResults
            key={`${viewing.scope}:${viewing.capture.id}`}
            capture={viewing.capture}
            initialIndex={viewing.index}
            onClose={() => setViewing(null)}
          />
        )}
    </div>
  );
}
