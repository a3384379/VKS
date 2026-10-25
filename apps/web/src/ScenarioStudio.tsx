import { useCallback, useEffect, useState } from "react";
import {
  Camera,
  ChevronRight,
  FileJson,
  GitBranch,
  Plus,
  Save,
  ShieldCheck,
} from "lucide-react";
import { api, mutation } from "./api";
import { Button, Modal, Notice, Panel, Tag } from "./components";
import { useWorkspace, usePermission } from "./context";
import "./scenario-studio.css";

type Origin = "declared" | "simulated" | "observed";
type State = {
  stateKey: string;
  label: string;
  evidenceOrigin: Origin;
  captureRef?: string;
};
type Transition = {
  transitionKey: string;
  fromState: string;
  toState: string;
  actionKey: string;
  conditionLabel: string;
  evidenceOrigin: Origin;
  scenarioRef: string;
};
type DSL = {
  schema_version: "1.0";
  scenario_key: string;
  name: string;
  description?: string;
  role?: string;
  expected_results?: string[];
  preconditions?: string[];
  environment_ref: string;
  dataset_ref: string;
  risk_level: "read_only" | "test_write";
  approval_ref?: string;
  timeout_ms: number;
  steps: Array<Record<string, unknown>>;
  explanation_graph?: {
    projectKey: string;
    baselineKey: string;
    states: State[];
    transitions: Transition[];
  };
};
type Scenario = {
  scenario_id: string;
  scenario_revision_id: string;
  entity_revision_id: string;
  revision_no: number;
  content_sha256: string;
  dsl: DSL;
};
type Environment = {
  id: string;
  key: string;
  name: string;
  kind: string;
  policy: {
    base_origin: string;
    auth_ref?: string;
    auth_revision?: string;
    viewport?: { width: number; height: number };
  };
};
type Dataset = { id: string; key: string; name: string; synthetic: boolean };
type Capture = {
  id: string;
  scenario_revision_id: string;
  state: string;
  source_alignment: string;
  result: {
    snapshots?: Array<{
      name: string;
      dom_artifact_id: string;
      png_artifact_id: string;
    }>;
    blocked?: unknown[];
  };
};
type Approval = {
  approval_ref: string;
  scenario_revision_id: string;
  expires_at: string;
};
const originLabel: Record<Origin, string> = {
  declared: "人工声明",
  simulated: "模拟状态",
  observed: "运行观察",
};

export default function ScenarioStudio() {
  const { w, refresh, notify, navigate, setUnsavedChanges } = useWorkspace();
  const [items, setItems] = useState<Scenario[]>([]);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [captures, setCaptures] = useState<Capture[]>([]);
  const [selected, setSelected] = useState("");
  const [stateKey, setStateKey] = useState("");
  const [editing, setEditing] = useState<Scenario | "new" | null>(null);
  const [draft, setDraft] = useState<DSL | null>(null);
  const [stepsJSON, setStepsJSON] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [environmentModal, setEnvironmentModal] = useState(false);
  const [datasetModal, setDatasetModal] = useState(false);
  const [captureModal, setCaptureModal] = useState(false);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [reason, setReason] = useState("");
  const [stateName, setStateName] = useState("");
  const [fromState, setFromState] = useState("");
  const [toState, setToState] = useState("");
  const [actionKey, setActionKey] = useState("");
  const [condition, setCondition] = useState("");
  const editPermission = usePermission("knowledge:edit");
  const canEdit = editPermission && w.selected_analysis_set?.state !== "frozen";
  const canCapture = usePermission("capture:run");
  const canManage = usePermission("project:manage");
  const canTrace = usePermission("trace:read");
  const current =
    items.find((item) => item.scenario_id === selected) || items[0];
  const graph = current?.dsl.explanation_graph;
  const currentState =
    graph?.states.find((state) => state.stateKey === stateKey) ||
    graph?.states[0];
  const jobState = w.jobs
    .filter((job) => job.kind === "capture")
    .map((job) => `${job.id}:${job.state}`)
    .join("|");
  const scope = w.selected_release
    ? `release_id=${w.selected_release.id}`
    : `analysis_set_id=${w.selected_analysis_set?.id || ""}`;
  const load = useCallback(async () => {
    if (!w.selected_analysis_set) {
      setItems([]);
      return;
    }
    try {
      const values = await api<Scenario[]>(
        `/projects/${w.project.id}/scenarios?${scope}`,
      );
      setItems(values);
      if (canCapture) {
        const [envs, data] = await Promise.all([
          api<Environment[]>(`/projects/${w.project.id}/capture-environments`),
          api<Dataset[]>(`/projects/${w.project.id}/datasets`),
        ]);
        setEnvironments(envs);
        setDatasets(data);
      }
      if (canTrace)
        setCaptures(await api<Capture[]>(`/projects/${w.project.id}/captures`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [w.project.id, w.selected_analysis_set?.id, scope, canCapture, canTrace]);
  useEffect(() => {
    void load();
  }, [load, jobState]);
  useEffect(() => {
    setStateKey("");
    setApproval((previous) =>
      previous?.scenario_revision_id === current?.scenario_revision_id
        ? previous
        : null,
    );
  }, [current?.scenario_revision_id]);
  useEffect(() => () => setUnsavedChanges(false), [setUnsavedChanges]);
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function begin(value?: Scenario) {
    const next: DSL = value
      ? JSON.parse(JSON.stringify(value.dsl))
      : {
          schema_version: "1.0",
          scenario_key: `scenario-${Date.now()}`,
          name: "新业务场景",
          environment_ref: environments[0]?.key || "mock-demo",
          dataset_ref: datasets[0]?.key || "synthetic-demo",
          risk_level: "read_only",
          timeout_ms: 30000,
          steps: [
            {
              step_key: "open",
              kind: "goto",
              path: "/",
              timeout_ms: 5000,
              risk: "read_only",
            },
            {
              step_key: "snapshot",
              kind: "snapshot",
              name: "初始页面",
              timeout_ms: 5000,
              risk: "read_only",
            },
          ],
          explanation_graph: {
            projectKey: w.project.key,
            baselineKey: w.selected_analysis_set?.key || "draft",
            states: [
              {
                stateKey: "ready",
                label: "初始状态",
                evidenceOrigin: "declared",
              },
            ],
            transitions: [],
          },
        };
    setEditing(value || "new");
    setDraft(next);
    setStepsJSON(JSON.stringify(next.steps, null, 2));
    setError("");
    setUnsavedChanges(true);
  }
  function closeEditor() {
    setEditing(null);
    setDraft(null);
    setUnsavedChanges(false);
  }
  async function save() {
    if (!draft || !w.selected_analysis_set) return;
    await perform(async () => {
      const steps = JSON.parse(stepsJSON) as DSL["steps"];
      const payload = {
        analysis_set_id: w.selected_analysis_set!.id,
        dsl: { ...draft, steps },
      };
      const saved =
        editing && editing !== "new"
          ? await api<Scenario>(
              `/scenarios/${editing.scenario_id}/draft`,
              mutation("PUT", payload, editing.revision_no),
            )
          : await api<Scenario>(
              `/projects/${w.project.id}/scenarios`,
              mutation("POST", payload),
            );
      closeEditor();
      setSelected(saved.scenario_id);
      await refresh();
      await load();
      notify("场景已保存为不可变修订");
    });
  }
  function addState() {
    if (!draft || !stateName.trim()) return;
    const g = draft.explanation_graph || {
      projectKey: w.project.key,
      baselineKey: w.selected_analysis_set?.key || "draft",
      states: [],
      transitions: [],
    };
    setDraft({
      ...draft,
      explanation_graph: {
        ...g,
        states: [
          ...g.states,
          {
            stateKey: `state-${g.states.length + 1}`,
            label: stateName.trim(),
            evidenceOrigin: "declared",
          },
        ],
      },
    });
    setStateName("");
  }
  function addTransition() {
    if (
      !draft?.explanation_graph ||
      !fromState ||
      !toState ||
      !actionKey ||
      !condition.trim()
    )
      return;
    const g = draft.explanation_graph;
    setDraft({
      ...draft,
      explanation_graph: {
        ...g,
        transitions: [
          ...g.transitions,
          {
            transitionKey: `transition-${g.transitions.length + 1}`,
            fromState,
            toState,
            actionKey,
            conditionLabel: condition.trim(),
            evidenceOrigin: "declared",
            scenarioRef: draft.scenario_key,
          },
        ],
      },
    });
    setCondition("");
  }
  async function approve() {
    if (!current || !w.selected_analysis_set) return;
    await perform(async () => {
      const granted = await api<Approval>(
        `/scenarios/${current.scenario_id}/capture-approvals`,
        mutation("POST", {
          analysis_set_id: w.selected_analysis_set!.id,
          scenario_revision_id: current.scenario_revision_id,
          reason,
        }),
      );
      await refresh();
      await load();
      setApproval(granted);
      notify("已签发绑定当前版本的短期采集授权");
    });
  }
  async function startCapture() {
    if (!current || !approval) return;
    await perform(async () => {
      const result = await api<{ decision: string; reason?: string }>(
        `/scenarios/${current.scenario_id}/capture`,
        mutation("POST", {
          scenario_revision_id: approval.scenario_revision_id,
          environment_ref: current.dsl.environment_ref,
          approval_ref: approval.approval_ref,
        }),
      );
      if (result.decision !== "accepted")
        throw new Error(result.reason || "需要有效采集授权");
      setCaptureModal(false);
      setApproval(null);
      await refresh();
      notify("受控采集任务已提交，可在任务中心查看");
    });
  }
  const captureForState = captures.find(
    (capture) => capture.id === currentState?.captureRef,
  );
  return (
    <section className="scenario-studio">
      <Panel
        title={
          <span className="row">
            <GitBranch size={18} />
            业务场景与状态分支
          </span>
        }
        action={
          canEdit ? (
            <Button kind="small" onClick={() => begin()}>
              <Plus size={15} />
              创建场景
            </Button>
          ) : (
            <Tag>固定版本 · 只读</Tag>
          )
        }
      >
        {error &&
          !editing &&
          !captureModal &&
          !environmentModal &&
          !datasetModal && <Notice tone="red">{error}</Notice>}
        {items.length > 0 && (
          <Notice>
            讲解分支只切换状态与知识对象，不执行录制步骤。运行观察、模拟状态与人工声明分别标注。
          </Notice>
        )}
        {!items.length ? (
          <p className="scenario-empty-description">
            还没有业务场景。创建后可组织步骤、状态与分支；讲解操作只切换知识对象。
          </p>
        ) : (
          <div className="scenario-layout">
            <div className="scenario-list">
              {items.map((item) => (
                <button
                  key={item.scenario_id}
                  className={
                    item.scenario_id === current?.scenario_id ? "selected" : ""
                  }
                  onClick={() => setSelected(item.scenario_id)}
                >
                  <strong>{item.dsl.name}</strong>
                  <small>
                    修订 {item.revision_no} · {item.dsl.steps.length} 个线性步骤
                  </small>
                </button>
              ))}
            </div>
            <div className="scenario-detail">
              {current && (
                <>
                  <div className="row between">
                    <div>
                      <h3>{current.dsl.name}</h3>
                      <small>
                        {current.dsl.preconditions?.join("；") ||
                          current.dsl.description ||
                          "场景来源与执行步骤已固定"}
                      </small>
                    </div>
                    <div className="row">
                      {canEdit && (
                        <Button kind="small" onClick={() => begin(current)}>
                          编辑场景
                        </Button>
                      )}
                      {canCapture && canEdit && (
                        <Button
                          kind="small"
                          onClick={() => {
                            setCaptureModal(true);
                            setApproval(null);
                            setReason("");
                          }}
                        >
                          <Camera size={15} />
                          受控录制
                        </Button>
                      )}
                    </div>
                  </div>
                  <div className="scenario-facts">
                    <span>环境：{current.dsl.environment_ref}</span>
                    <span>数据集：{current.dsl.dataset_ref}</span>
                    {current.dsl.role && (
                      <span>合成测试角色：{current.dsl.role}</span>
                    )}
                    <span>
                      {current.dsl.risk_level === "read_only"
                        ? "只读录制"
                        : "测试写入需专门授权"}
                    </span>
                  </div>
                  {!!current.dsl.expected_results?.length && (
                    <p className="scenario-empty-description">
                      预期结果：{current.dsl.expected_results.join("；")}
                    </p>
                  )}
                  {graph?.states.length ? (
                    <>
                      <div className="scenario-states">
                        {graph.states.map((state) => (
                          <button
                            key={state.stateKey}
                            className={
                              currentState?.stateKey === state.stateKey
                                ? "selected"
                                : ""
                            }
                            onClick={() => setStateKey(state.stateKey)}
                          >
                            <span>{state.label}</span>
                            <Tag
                              value={
                                state.evidenceOrigin === "observed"
                                  ? "observed"
                                  : "manual"
                              }
                            >
                              {originLabel[state.evidenceOrigin]}
                            </Tag>
                          </button>
                        ))}
                      </div>
                      <h4>从「{currentState?.label}」可选择的讲解分支</h4>
                      <div className="scenario-transitions">
                        {graph.transitions
                          .filter((t) => t.fromState === currentState?.stateKey)
                          .map((t) => (
                            <button
                              key={t.transitionKey}
                              onClick={() => setStateKey(t.toState)}
                            >
                              <span>{t.conditionLabel}</span>
                              <ChevronRight size={15} />
                              <strong>
                                {
                                  graph.states.find(
                                    (state) => state.stateKey === t.toState,
                                  )?.label
                                }
                              </strong>
                              <small>{originLabel[t.evidenceOrigin]}</small>
                            </button>
                          ))}
                        {!graph.transitions.some(
                          (t) => t.fromState === currentState?.stateKey,
                        ) && (
                          <p className="muted">
                            当前状态没有已声明的后续分支。
                          </p>
                        )}
                      </div>
                    </>
                  ) : (
                    <p className="muted">此场景尚未配置讲解分支。</p>
                  )}
                  {captureForState?.result.snapshots?.map((snapshot, i) => (
                    <figure className="scenario-capture" key={i}>
                      <img
                        src={`/api/v1/captures/${captureForState.id}/artifacts/${snapshot.png_artifact_id}`}
                        alt={`${snapshot.name} · 已脱敏的测试环境观察`}
                      />
                      <figcaption>
                        {snapshot.name} · 运行观察 · 已脱敏 · 源码对齐未知
                      </figcaption>
                    </figure>
                  ))}
                  <details>
                    <summary>查看固定线性步骤（阅读不会执行）</summary>
                    <pre className="json-block">
                      {JSON.stringify(current.dsl.steps, null, 2)}
                    </pre>
                  </details>
                  {canTrace && (
                    <div className="capture-list">
                      <h4>最近采集</h4>
                      {captures
                        .filter(
                          (capture) =>
                            capture.scenario_revision_id ===
                            current.scenario_revision_id,
                        )
                        .map((capture) => (
                          <div key={capture.id}>
                            <Tag value={capture.state} />
                            <span>
                              {capture.result.snapshots?.length || 0} 个脱敏状态
                            </span>
                            <small>运行与源码版本未核对</small>
                          </div>
                        ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        )}
        {canManage && (
          <div className="row scenario-admin-actions">
            <Button
              kind="subtle small"
              onClick={() => setEnvironmentModal(true)}
            >
              登记测试环境
            </Button>
            <Button kind="subtle small" onClick={() => setDatasetModal(true)}>
              登记合成数据集
            </Button>
            <span>
              {environments.length} 个环境 · {datasets.length} 个数据集
            </span>
          </div>
        )}
      </Panel>
      {editing && draft && (
        <Modal
          title={editing === "new" ? "创建业务场景" : "编辑场景修订"}
          className="scenario-modal"
          onClose={closeEditor}
        >
          <div className="scenario-form-grid">
            <label>
              场景名称
              <input
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                maxLength={160}
              />
            </label>
            <label>
              稳定逻辑键
              <input
                disabled={editing !== "new"}
                value={draft.scenario_key}
                onChange={(e) =>
                  setDraft({ ...draft, scenario_key: e.target.value })
                }
              />
            </label>
            <label>
              合成测试角色（说明，不授予权限）
              <input
                value={draft.role || ""}
                maxLength={160}
                onChange={(e) => setDraft({ ...draft, role: e.target.value })}
              />
            </label>
            <label>
              录制风险范围
              <select
                value={draft.risk_level}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    risk_level: e.target.value as DSL["risk_level"],
                    ...(e.target.value === "test_write"
                      ? { approval_ref: crypto.randomUUID() }
                      : {}),
                  })
                }
              >
                <option value="read_only">只读环境与页面断言</option>
                <option value="test_write">
                  仅合成 Mock 测试写入（需另行授权）
                </option>
              </select>
            </label>
            <label>
              环境逻辑键
              <input
                list="scenario-environments"
                value={draft.environment_ref}
                onChange={(e) =>
                  setDraft({ ...draft, environment_ref: e.target.value })
                }
              />
              <datalist id="scenario-environments">
                {environments.map((env) => (
                  <option key={env.id} value={env.key}>
                    {env.name}
                  </option>
                ))}
              </datalist>
            </label>
            <label>
              合成数据集逻辑键
              <input
                list="scenario-datasets"
                value={draft.dataset_ref}
                onChange={(e) =>
                  setDraft({ ...draft, dataset_ref: e.target.value })
                }
              />
              <datalist id="scenario-datasets">
                {datasets.map((data) => (
                  <option key={data.id} value={data.key}>
                    {data.name}
                  </option>
                ))}
              </datalist>
            </label>
          </div>
          <label>
            预期结果（每行一项，供人员核对）
            <textarea
              value={(draft.expected_results || []).join("\n")}
              rows={3}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  expected_results: e.target.value.split("\n").filter(Boolean),
                })
              }
            />
          </label>
          <label>
            前置条件
            <textarea
              rows={2}
              value={(draft.preconditions || []).join("\n")}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  preconditions: e.target.value.split("\n").filter(Boolean),
                })
              }
            />
          </label>
          <div className="row between">
            <h3>讲解状态与分支</h3>
            <Tag>默认人工声明 · 不冒充录制</Tag>
          </div>
          <div className="scenario-inline-form">
            <input
              aria-label="新状态名称"
              placeholder="例如：校验失败"
              value={stateName}
              onChange={(e) => setStateName(e.target.value)}
            />
            <Button kind="small" onClick={addState}>
              添加状态
            </Button>
          </div>
          <div className="scenario-state-chips">
            {draft.explanation_graph?.states.map((state) => (
              <Tag key={state.stateKey}>{state.label}</Tag>
            ))}
          </div>
          <div className="scenario-form-grid">
            <label>
              起始状态
              <select
                value={fromState}
                onChange={(e) => setFromState(e.target.value)}
              >
                <option value="">选择状态</option>
                {draft.explanation_graph?.states.map((state) => (
                  <option key={state.stateKey} value={state.stateKey}>
                    {state.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              目标状态
              <select
                value={toState}
                onChange={(e) => setToState(e.target.value)}
              >
                <option value="">选择状态</option>
                {draft.explanation_graph?.states.map((state) => (
                  <option key={state.stateKey} value={state.stateKey}>
                    {state.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              业务动作
              <select
                value={actionKey}
                onChange={(e) => setActionKey(e.target.value)}
              >
                <option value="">选择当前版本动作</option>
                {w.actions.map((action) => (
                  <option key={action.id} value={action.stable_key}>
                    {action.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              条件说明
              <input
                placeholder="仅展示，不执行表达式"
                value={condition}
                onChange={(e) => setCondition(e.target.value)}
              />
            </label>
          </div>
          <Button kind="small" onClick={addTransition}>
            添加讲解分支
          </Button>
          <ul>
            {draft.explanation_graph?.transitions.map((t) => (
              <li key={t.transitionKey}>
                {t.fromState} → {t.conditionLabel} → {t.toState}
              </li>
            ))}
          </ul>
          <button
            className="text-button"
            onClick={() => setAdvanced(!advanced)}
          >
            <FileJson size={16} />
            {advanced ? "收起步骤配置" : "配置受控线性步骤"}
          </button>
          {advanced && (
            <>
              <Notice>
                支持
                goto、click、fill、select、wait_for、assert_visible、assert_text、snapshot。步骤须有超时；不支持脚本、任意
                URL 或凭据明文。
              </Notice>
              <Notice>
                goto 可设置 wait_until 为 domcontentloaded、load 或
                networkidle。认证初始化的写权限仍需在该步骤和测试环境中分别授权，后续断言保持只读。
              </Notice>
              <label>
                步骤 JSON
                <textarea
                  className="scenario-code"
                  rows={12}
                  value={stepsJSON}
                  onChange={(e) => setStepsJSON(e.target.value)}
                />
              </label>
            </>
          )}
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={closeEditor}>取消</Button>
            <Button
              kind="primary"
              disabled={busy || !draft.name.trim()}
              onClick={() => void save()}
            >
              <Save size={15} />
              保存不可变修订
            </Button>
          </div>
        </Modal>
      )}
      {captureModal && current && (
        <Modal
          title="授权受控环境录制"
          onClose={() => {
            setCaptureModal(false);
            setApproval(null);
          }}
        >
          <Notice tone="amber">
            录制会访问登记的测试环境，独立于只读讲解。仅使用合成数据集，所有网络按允许清单检查，输出先脱敏再保存。
          </Notice>
          <p>
            场景：{current.dsl.name} · 修订 {current.revision_no}
          </p>
          <p>
            环境：{current.dsl.environment_ref} · 数据集：
            {current.dsl.dataset_ref}
          </p>
          {!approval ? (
            <>
              <label>
                授权依据
                <textarea
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  rows={3}
                  placeholder="说明测试环境与动作范围的授权依据…"
                />
              </label>
              {error && <Notice tone="red">{error}</Notice>}
              <div className="modal-actions">
                <Button onClick={() => setCaptureModal(false)}>返回</Button>
                <Button
                  kind="primary"
                  disabled={busy || !reason.trim() || !canManage}
                  onClick={() => void approve()}
                >
                  <ShieldCheck size={16} />
                  签发本次采集授权
                </Button>
              </div>
              {!canManage && <p>需要项目管理员签发采集授权。</p>}
            </>
          ) : (
            <>
              <Notice>
                已绑定当前场景、环境策略与数据集。有效期至{" "}
                {new Date(approval.expires_at).toLocaleTimeString("zh-CN")}。
              </Notice>
              {error && <Notice tone="red">{error}</Notice>}
              <div className="modal-actions">
                <Button onClick={() => setCaptureModal(false)}>取消</Button>
                <Button
                  kind="primary"
                  disabled={busy}
                  onClick={() => void startCapture()}
                >
                  <Camera size={16} />
                  开始受控录制
                </Button>
              </div>
            </>
          )}
          <Button kind="subtle" onClick={() => navigate("imports")}>
            查看任务中心
          </Button>
        </Modal>
      )}
      {environmentModal && (
        <EnvironmentForm
          onClose={() => setEnvironmentModal(false)}
          save={(value) =>
            perform(async () => {
              await api(
                `/projects/${w.project.id}/capture-environments`,
                mutation("POST", value),
              );
              setEnvironmentModal(false);
              await load();
              notify("测试环境已登记");
            })
          }
          error={error}
          busy={busy}
        />
      )}
      {datasetModal && (
        <DatasetForm
          onClose={() => setDatasetModal(false)}
          save={(value) =>
            perform(async () => {
              await api(
                `/projects/${w.project.id}/datasets`,
                mutation("POST", value),
              );
              setDatasetModal(false);
              await load();
              notify("合成数据集已登记");
            })
          }
          error={error}
          busy={busy}
        />
      )}
    </section>
  );
}

function EnvironmentForm({
  onClose,
  save,
  error,
  busy,
}: {
  onClose: () => void;
  save: (value: unknown) => Promise<void>;
  error: string;
  busy: boolean;
}) {
  const [key, setKey] = useState("mock-demo"),
    [name, setName] = useState("合成测试环境"),
    [base, setBase] = useState(""),
    [cidrs, setCidrs] = useState("127.0.0.1/32"),
    [local, setLocal] = useState(false),
    [writePaths, setWritePaths] = useState(""),
    [authRef, setAuthRef] = useState(""),
    [serviceKey, setServiceKey] = useState(""),
    [viewportWidth, setViewportWidth] = useState(1440),
    [viewportHeight, setViewportHeight] = useState(900);
  return (
    <Modal title="登记受控测试环境" onClose={onClose}>
      <Notice>
        只接受 mock/test
        来源。回环环境须同时由实例配置启用；平台控制端口和云元数据始终阻断。
      </Notice>
      <label>
        环境名称
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        逻辑键
        <input value={key} onChange={(e) => setKey(e.target.value)} />
      </label>
      <label>
        测试站点来源
        <input
          placeholder="http://test.example:8080"
          value={base}
          onChange={(e) => setBase(e.target.value)}
        />
      </label>
      <label>
        对应服务标识（可留空，歧义时不自动关联）
        <input
          value={serviceKey}
          onChange={(e) => setServiceKey(e.target.value)}
          placeholder="billing"
        />
      </label>
      <label>
        实例登记的测试认证引用（可留空）
        <input
          value={authRef}
          onChange={(e) => setAuthRef(e.target.value)}
          placeholder="test-reader-v1"
        />
      </label>
      <Notice>
        这里只接认证引用。测试授权头和隔离会话由实例管理员登记，不粘贴凭据或用户
        Cookie。
      </Notice>
      <div className="scenario-form-grid">
        <label>
          视口宽度
          <input
            type="number"
            min={320}
            max={3840}
            value={viewportWidth}
            onChange={(e) => setViewportWidth(Number(e.target.value))}
          />
        </label>
        <label>
          视口高度
          <input
            type="number"
            min={240}
            max={2160}
            value={viewportHeight}
            onChange={(e) => setViewportHeight(Number(e.target.value))}
          />
        </label>
      </div>
      <label>
        允许出站网段
        <textarea
          rows={2}
          value={cidrs}
          onChange={(e) => setCidrs(e.target.value)}
        />
      </label>
      <label>
        允许的合成写入路径（可留空）
        <textarea
          rows={2}
          placeholder="/mock/invoice"
          value={writePaths}
          onChange={(e) => setWritePaths(e.target.value)}
        />
      </label>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={local}
          onChange={(e) => setLocal(e.target.checked)}
        />
        这是实例明确允许的本地合成测试服务
      </label>
      {error && <Notice tone="red">{error}</Notice>}
      <div className="modal-actions">
        <Button onClick={onClose}>取消</Button>
        <Button
          kind="primary"
          disabled={busy || !base || !name}
          onClick={() =>
            void save({
              key,
              name,
              kind: "mock",
              base_origin: base,
              ...(authRef.trim() ? { auth_ref: authRef.trim() } : {}),
              ...(serviceKey.trim() ? { service_key: serviceKey.trim() } : {}),
              viewport: { width: viewportWidth, height: viewportHeight },
              allowed_origins: [base],
              allowed_cidrs: cidrs.split(/[\s,]+/).filter(Boolean),
              allow_loopback: local,
              allowed_write_paths: writePaths.split(/[\s,]+/).filter(Boolean),
            })
          }
        >
          登记环境
        </Button>
      </div>
    </Modal>
  );
}
function DatasetForm({
  onClose,
  save,
  error,
  busy,
}: {
  onClose: () => void;
  save: (value: unknown) => Promise<void>;
  error: string;
  busy: boolean;
}) {
  const [key, setKey] = useState("synthetic-demo"),
    [name, setName] = useState("合成演示数据"),
    [data, setData] = useState('{\n  "client": {"name": "示例客户"}\n}'),
    [confirmed, setConfirmed] = useState(false),
    [localError, setLocalError] = useState("");
  return (
    <Modal title="登记合成数据集" onClose={onClose}>
      <label>
        数据集名称
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        逻辑键
        <input value={key} onChange={(e) => setKey(e.target.value)} />
      </label>
      <label>
        合成数据 JSON
        <textarea
          rows={7}
          className="scenario-code"
          value={data}
          onChange={(e) => setData(e.target.value)}
        />
      </label>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(e) => setConfirmed(e.target.checked)}
        />
        这是合成样本，不含真实客户信息或凭据
      </label>
      {(error || localError) && (
        <Notice tone="red">{error || localError}</Notice>
      )}
      <div className="modal-actions">
        <Button onClick={onClose}>取消</Button>
        <Button
          kind="primary"
          disabled={busy || !confirmed || !key || !name}
          onClick={() => {
            try {
              setLocalError("");
              void save({ key, name, data: JSON.parse(data), synthetic: true });
            } catch {
              setLocalError("数据必须是有效的 JSON 对象");
            }
          }}
        >
          登记数据集
        </Button>
      </div>
    </Modal>
  );
}
