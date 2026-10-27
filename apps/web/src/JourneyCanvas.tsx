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
import { usePermission, useWorkspace } from "./context";
import { CaptureResults, type Capture } from "./CaptureResults";
import { JourneyMerge } from "./JourneyMerge";
import { arrangeJourney } from "./journey-editing";
import {
  newJourney,
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
  decision: "判断",
  branch: "分支",
  end: "结束",
  reference: "知识引用",
};
export function JourneyCanvas() {
  const {
    w,
    baseline,
    run,
    notify,
    navigate,
    navigateExplanation,
    setUnsavedChanges,
    session,
    selected: selectedEntity,
  } = useWorkspace();
  const canEdit = usePermission("knowledge:edit");
  const canReview = usePermission("links:review") && !!session.human;
  const [small, setSmall] = useState(window.innerWidth < 768);
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
    capture: Capture;
    index: number;
  } | null>(null);
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
  dirtyRef.current = dirty;
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
        if (controller.signal.aborted || dirtyRef.current) return;
        setRows(data);
        setLoaded(true);
        const target =
          data.find((row) => row.journey_id === selectedEntity) || data[0];
        if (target) open(target);
      })
      .catch((e) => {
        if (!controller.signal.aborted) {
          setError(e.message);
          setLoaded(true);
        }
      });
    return () => controller.abort();
  }, [
    w.project.id,
    baseline,
    w.selected_analysis_set?.id,
    w.selected_analysis_set?.selection_version,
    selectedEntity,
  ]);
  function open(row: JourneyRevision) {
    setActive(row);
    setHistory({ past: [], present: row.dsl, future: [] });
    setSaved(JSON.stringify(row.dsl));
    setSelected(row.dsl.entry);
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
  async function save() {
    if (!dsl || !w.selected_analysis_set) return;
    setError("");
    const path = active
      ? `/api/v2/journeys/${active.journey_id}/revisions`
      : `/api/v2/projects/${w.project.id}/journeys`;
    try {
      const init = mutation("POST", {
        analysis_set_id: w.selected_analysis_set.id,
        dsl,
      });
      if (active)
        (init.headers as Record<string, string>)["If-Match"] =
          `"${active.revision_id}"`;
      const result = await api<JourneyRevision>(path, init);
      setUnsavedChanges(false);
      open(result);
      setRows((r) => [
        result,
        ...r.filter((x) => x.journey_id !== result.journey_id),
      ]);
      notify("流程已保存为不可变修订，等待人员审核");
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
  return (
    <div className="journey-workspace">
      <Panel
        title="业务流程"
        action={
          <div className="row">
            <Tag>被动流程 · 人员审核</Tag>
            {editable && (
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
          流程中的引用固定到准确修订。点击只打开知识；受控采集继续使用独立的场景审批。
        </p>
        {error && (
          <Notice tone="red">
            {error}。本地编辑保留；可比较并合并更新，或导出 DSL 另存。
          </Notice>
        )}
        {small && (
          <Notice>窄屏提供只读流程与知识导航，请在桌面编辑流程。</Notice>
        )}
        <div className="journey-list">
          {rows.map((row) => (
            <button
              key={row.journey_id}
              aria-pressed={active?.journey_id === row.journey_id}
              disabled={dirty}
              onClick={() => open(row)}
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
                  onChange={(e) => change({ ...dsl, title: e.target.value })}
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
                          if (f.size > 200000) throw Error("DSL 超出大小上限");
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
            <div className="journey-editor">
              <div className="journey-scroll" aria-label="业务流程画布">
                <div
                  className="journey-board"
                  style={{
                    width: Math.max(740, ...dsl.nodes.map((n) => n.x + 240)),
                    height: Math.max(360, ...dsl.nodes.map((n) => n.y + 120)),
                  }}
                >
                  <svg className="journey-edges" aria-hidden="true">
                    {dsl.edges.map((edge) => {
                      const from = dsl.nodes.find((n) => n.key === edge.source),
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
                                    binding: {
                                      entity_id: entity.id,
                                      revision_id: entity.entity_revision_id,
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
                          onClick={() => change({ ...dsl, entry: node.key })}
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
                    {node.binding && (
                      <Button
                        onClick={() => {
                          if (node.capture_ref) {
                            const ref = node.capture_ref;
                            const q = new URLSearchParams(
                              w.selected_release
                                ? { release_id: w.selected_release.id }
                                : {
                                    analysis_set_id:
                                      w.selected_analysis_set?.id || "",
                                  },
                            );
                            void api<Capture>(
                              `/projects/${w.project.id}/captures/${ref.capture_id}?${q}`,
                            )
                              .then((capture) =>
                                setViewing({
                                  capture,
                                  index: ref.snapshot_index,
                                }),
                              )
                              .catch((e) => setError(e.message));
                            return;
                          }
                          if (node.state_ref) {
                            navigateExplanation({
                              scenarioId: node.state_ref.scenario_id,
                              stateKey: node.state_ref.state_key,
                              followTarget: false,
                            });
                            navigate("workbench");
                            return;
                          }
                          const e = w.entities.find(
                            (e) => e.id === node.binding?.entity_id,
                          );
                          if (!e) return;
                          if (e.type === "action") {
                            navigateExplanation({
                              actionId: e.id,
                              followTarget: false,
                            });
                            navigate("workbench");
                          } else
                            navigate(
                              e.type === "document" || e.type === "section"
                                ? "documents"
                                : "graph",
                              e,
                            );
                        }}
                      >
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
                        const key = "node-" + crypto.randomUUID().slice(0, 8);
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
                              key: "edge-" + crypto.randomUUID().slice(0, 8),
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
                            edges: dsl.edges.filter((e) => e.key !== edge.key),
                          })
                        }
                      >
                        {edge.source} → {edge.target} · {edge.label || "无条件"}{" "}
                        ×
                      </button>
                    ))}
                  </>
                )}
                {active && canReview && !dirty && editable && (
                  <Button
                    onClick={() => {
                      void run(() => {
                        const init = mutation("POST", {
                          analysis_set_id: w.selected_analysis_set?.id,
                          revision_id: active.revision_id,
                          decision: "approve",
                          reason: "人员已核对流程结构和同版引用",
                        });
                        (init.headers as Record<string, string>)["If-Match"] =
                          `"${active.revision_id}"`;
                        return api(
                          `/api/v2/journeys/${active.journey_id}/review`,
                          init,
                        );
                      }, "当前流程修订已人工确认");
                    }}
                  >
                    确认当前修订
                  </Button>
                )}
              </aside>
            </div>
            <p className="muted">
              拖动节点或使用方向键移动；连接、分支和引用由服务器校验。
              {active && ` 修订 ${active.revision_id}`}
            </p>
          </>
        )}
      </Panel>
      {viewing && (
        <CaptureResults
          capture={viewing.capture}
          initialIndex={viewing.index}
          onClose={() => setViewing(null)}
        />
      )}
    </div>
  );
}
