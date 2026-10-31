import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useState } from "react";
import { RefreshCw, Sparkles, Square } from "lucide-react";
import { api, mutation } from "./api";
import { Button, Empty, Notice, Tag } from "./components";
import { usePermission, useReadScope, useWorkspace } from "./context";
import type { Dict, Job } from "./types";
import { TaskStages } from "./TaskStages";
import { ModuleCandidates, type ModuleCandidate } from "./ModuleCandidates";
import { JourneyPlanPreview, type PlannedJourney } from "./JourneyPlanPreview";
import { TaskResults } from "./TaskResults";
const templates = {
  project_overview: "项目概览",
  module_analysis: "模块解析",
  business_flow: "动作证据导航",
  evidence_gaps: "证据缺口",
  document_draft: "AI 文档草稿",
  knowledge_plan: "自然语言知识任务",
  difference_review: "差异复核准备",
};
type Task = Job & {
  attempt: number;
  task_kind: string;
  analysis_set_id: string;
  stage_history: {
    stage: string;
    state: string;
    at: string;
    cached?: string;
  }[];
  stages: string[];
  report: Dict | null;
  budget: { max_model_calls: number };
  diagnostics_restricted?: boolean;
  stage_artifacts: { stage: string; sha256: string; size_bytes: number }[];
  module_decisions: Record<string, string>;
  pause_requested: boolean;
  plan_preview_unavailable?: boolean;
  plan_preview: {
    sha256: string;
    title: string;
    analysis_set_id: string;
    selection_version: number;
    steps: (Dict & { journey?: PlannedJourney })[];
    unknowns: string[];
    catalog: Dict[];
    model_calls_reserved: number;
  } | null;
};
export function AgentWorkspace() {
  const { w, run, notify, navigate } = useWorkspace();
  const canRun = usePermission("analysis:run");
  const readScope = useReadScope();
  const [kind, setKind] = useState<keyof typeof templates>("project_overview"),
    [fromRelease, setFromRelease] = useState(w.releases[0]?.id || ""),
    [instruction, setInstruction] = useState(""),
    [actions, setActions] = useState<string[]>([]),
    [taskRow, setTaskRow] = useState<{ scope: string; value: Task } | null>(
      null,
    ),
    [taskId, setTaskId] = useState(""),
    [error, setError] = useState(""),
    [controlBusy, setControlBusy] = useState(false);
  const task = taskRow?.scope === readScope ? taskRow.value : null;
  const setTask = (value: Task | null) =>
    setTaskRow(value ? { scope: readScope, value } : null);
  const jobs = w.jobs.filter((j) => j.kind === "knowledge_task");
  useEffect(() => {
    setTask(null);
    if (!taskId) return;
    const controller = new AbortController();
    let timer: number;
    const load = () =>
      void api<Task>(`/api/v2/knowledge-tasks/${taskId}`, {
        signal: controller.signal,
      })
        .then((t) => {
          setTask(t);
          if (
            [
              "queued",
              "running",
              "retry_wait",
              "cancelling",
              "paused",
            ].includes(t.state)
          )
            timer = window.setTimeout(load, t.state === "paused" ? 4000 : 1500);
        })
        .catch((e) => {
          if (!controller.signal.aborted) setError(e.message);
        });
    load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [taskId, readScope]);
  async function create() {
    if (!w.selected_analysis_set) return;
    setError("");
    try {
      const job = await api<Job>(
        `/api/v2/projects/${w.project.id}/knowledge-tasks`,
        mutation("POST", {
          analysis_set_id: w.selected_analysis_set.id,
          selection_version: w.selected_analysis_set.selection_version,
          task_kind: kind,
          instruction,
          action_ids: actions,
          ...(kind === "difference_review"
            ? { from_release_id: fromRelease }
            : {}),
          max_entities: 100,
          max_model_calls: ["document_draft", "knowledge_plan"].includes(kind)
            ? 1
            : 0,
        }),
      );
      setTaskId(job.id);
      notify("知识任务已进入持久队列");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function control(action: "pause" | "resume") {
    if (!task) return;
    setControlBusy(true);
    setError("");
    try {
      await api(
        `/api/v2/knowledge-tasks/${task.id}/control`,
        mutation("POST", { action, expected_state: task.state }, task.attempt),
      );
      setTask(await api<Task>(`/api/v2/knowledge-tasks/${task.id}`));
      notify(action === "pause" ? "已请求在阶段边界暂停" : "已继续任务");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setControlBusy(false);
    }
  }
  return (
    <>
      <div className="agent-v2-layout">
        <Panel title="知识任务" className="agent-v2-plan">
          <span className="eyebrow">KNOWLEDGE ENGINEERING</span>
          <h2>从固定来源，整理项目知识。</h2>
          <p>
            普通模板使用现有资料和确定性分析。AI
            文档和自然语言任务使用项目配置的模型，每个任务最多调用一次。
          </p>
          {error && <Notice tone="red">{error}</Notice>}
          <label>
            任务模板
            <select
              aria-label="知识任务模板"
              value={kind}
              onChange={(e) =>
                setKind(e.target.value as keyof typeof templates)
              }
            >
              {Object.entries(templates).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            范围说明
            <textarea
              aria-label="知识任务说明"
              value={instruction}
              maxLength={4000}
              onChange={(e) => setInstruction(e.target.value)}
              placeholder="说明业务范围和希望核对的疑问"
            />
          </label>
          {kind === "difference_review" && (
            <label>
              旧知识发布版
              <select
                aria-label="任务对比发布版"
                value={fromRelease}
                onChange={(e) => setFromRelease(e.target.value)}
              >
                <option value="">请选择固定发布版</option>
                {w.releases.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.release_no}
                  </option>
                ))}
              </select>
            </label>
          )}
          {kind === "business_flow" && (
            <fieldset>
              <legend>选择独立查阅的动作（最多 30 个）</legend>
              <p>
                生成并列的动作证据入口，不声明先后或业务执行条件。完整业务流程可通过自然语言任务提出带目标、条件和来源的结构计划。
              </p>
              {w.actions.map((a) => (
                <label key={a.id}>
                  <input
                    type="checkbox"
                    checked={actions.includes(a.id)}
                    disabled={!actions.includes(a.id) && actions.length >= 30}
                    onChange={(e) =>
                      setActions((ids) =>
                        e.target.checked
                          ? [...ids, a.id]
                          : ids.filter((id) => id !== a.id),
                      )
                    }
                  />
                  {a.name}
                </label>
              ))}
            </fieldset>
          )}
          <div className="row wrap">
            <Tag>版本：{w.selected_analysis_set?.name || "未选择"}</Tag>
            <Tag>直接保存到当前知识版本</Tag>
          </div>
          <Button
            kind="primary"
            disabled={
              !canRun ||
              !w.selected_analysis_set ||
              !!w.selected_release ||
              (kind === "business_flow" && !actions.length) ||
              (kind === "difference_review" && !fromRelease) ||
              (kind === "knowledge_plan" && !instruction.trim())
            }
            onClick={() => void create()}
          >
            <Sparkles size={16} />
            预检并创建任务
          </Button>
        </Panel>
        <Panel title="近期任务">
          <div className="v2-task-list">
            {jobs.length ? (
              jobs.map((j) => (
                <button
                  key={j.id}
                  onClick={() => setTaskId(j.id)}
                  aria-pressed={taskId === j.id}
                >
                  <strong>{j.id.slice(0, 8)}</strong>
                  <Tag value={j.state} />
                  <small>{j.stage}</small>
                </button>
              ))
            ) : (
              <Empty
                title="还没有知识任务"
                description="从左侧模板开始，模型默认关闭。"
              />
            )}
          </div>
        </Panel>
      </div>
      {task && (
        <Panel
          title={`${templates[task.task_kind as keyof typeof templates] || "知识任务"} · ${task.id.slice(0, 8)}`}
          action={<Tag value={task.state} />}
        >
          <div className="v2-pipeline">
            {task.stages.map((stage) => {
              const entry = task.stage_history
                .filter((x) => x.stage === stage)
                .at(-1);
              const started = !!entry;
              const current = task.stage.toLowerCase() === stage.toLowerCase();
              return (
                <div
                  key={stage}
                  className={current ? "active" : started ? "visited" : ""}
                >
                  <strong>{stage}</strong>
                  <small>
                    {entry?.state === "failed"
                      ? "失败"
                      : entry?.state === "cancelled"
                        ? "已取消"
                        : entry?.state === "retry_wait"
                          ? "等待重试"
                          : entry?.state === "waiting_for_human"
                            ? "等待继续"
                            : entry?.state === "complete"
                              ? entry.cached === "true"
                                ? "复用已验证检查点"
                                : "已完成"
                              : current
                                ? "当前阶段"
                                : "待执行"}
                  </small>
                </div>
              );
            })}
          </div>
          <p className="muted">
            阶段状态来自服务器；任务完成代表草稿已生成。预算最多{" "}
            {task.budget.max_model_calls} 次模型调用。
          </p>
          <div className="row wrap">
            {["queued", "running", "retry_wait"].includes(task.state) && (
              <Button
                disabled={!canRun || controlBusy || task.pause_requested}
                onClick={() => void control("pause")}
              >
                {task.pause_requested ? "等待当前阶段完成后暂停" : "暂停任务"}
              </Button>
            )}
            {task.state === "paused" && (
              <Button
                disabled={!canRun || controlBusy}
                onClick={() => void control("resume")}
              >
                继续任务
              </Button>
            )}
            {["queued", "running", "retry_wait", "paused"].includes(
              task.state,
            ) && (
              <Button
                onClick={() => {
                  void run(
                    () =>
                      api(
                        `/jobs/${task.id}/cancel`,
                        mutation("POST", { reason: "用户取消知识任务" }),
                      ),
                    "已请求取消",
                  );
                  setTaskId("");
                }}
              >
                <Square size={14} />
                取消任务
              </Button>
            )}
            {["failed", "cancelled"].includes(task.state) && (
              <Button
                onClick={() => {
                  void run(
                    () => api(`/jobs/${task.id}/retry`, mutation("POST", {})),
                    "已重新排队",
                  );
                  setTaskId("");
                  window.setTimeout(() => setTaskId(task.id), 0);
                }}
              >
                <RefreshCw size={14} />
                重试任务
              </Button>
            )}
            <Button onClick={() => navigate("journeys")}>
              查看导览与证据材料
            </Button>
            <Button onClick={() => navigate("relations")}>查看关联结果</Button>
          </div>
          {task.state === "paused" && (
            <Notice>
              任务已在阶段边界暂停，继续时将重新验证来源、基线与当前权限。
            </Notice>
          )}
          {task.plan_preview_unavailable && (
            <Notice tone="red">
              计划检查点无法验证；请取消后重新创建任务。
            </Notice>
          )}
          {task.plan_preview && (
            <section className="v2-plan-preview" aria-label="任务执行计划">
              <h3>{task.plan_preview.title}</h3>
              <p>
                固定基线 {task.plan_preview.analysis_set_id.slice(0, 12)} ·
                选择版本 {task.plan_preview.selection_version} ·{" "}
                {task.plan_preview.catalog.length} 个准确输入修订
              </p>
              <p>
                计划阶段已占用 {task.plan_preview.model_calls_reserved}{" "}
                次模型调用，次调用结果会复用，产物直接保存到当前基线。
              </p>
              <ol>
                {task.plan_preview.steps.map((step, i) => (
                  <li key={String(step.id || i)}>
                    <strong>{String(step.tool)}</strong> ·{" "}
                    {String(step.rationale)}
                    {step.journey ? (
                      <JourneyPlanPreview
                        journey={step.journey}
                        entities={w.entities}
                      />
                    ) : step.tool === "propose_journey" ? (
                      <p>
                        该步骤只生成并列的动作证据导航；动作列表不代表业务先后顺序。
                      </p>
                    ) : null}
                    <pre className="v2-json">
                      {JSON.stringify(
                        {
                          target_revision_ids: step.target_revision_ids,
                          evidence_ids: step.evidence_ids,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </li>
                ))}
              </ol>
              {task.plan_preview.unknowns.map((text, i) => (
                <p key={i} className="muted">
                  {text}
                </p>
              ))}
              <details>
                <summary>核对准确输入范围</summary>
                <pre className="v2-json">
                  {JSON.stringify(task.plan_preview.catalog, null, 2)}
                </pre>
              </details>
            </section>
          )}
          {task.error && <Notice tone="red">{task.error.message}</Notice>}
          {task.diagnostics_restricted && (
            <Notice>任务产物包含当前无权资料，已隐藏正文和统计。</Notice>
          )}
          {!!task.stage_artifacts?.length && (
            <TaskStages
              key={task.id}
              jobId={task.id}
              artifacts={task.stage_artifacts}
            />
          )}
          {task.report && (
            <>
              <Notice>
                产物直接保存到当前知识版本；静态与声明信息不能证明运行因果。
              </Notice>
              {Array.isArray(task.report.module_candidates) && (
                <ModuleCandidates
                  key={task.id}
                  jobId={task.id}
                  candidates={
                    task.report.module_candidates as ModuleCandidate[]
                  }
                  decisions={task.module_decisions || {}}
                  reload={async () => {
                    setTask(
                      await api<Task>(`/api/v2/knowledge-tasks/${task.id}`),
                    );
                  }}
                />
              )}
              {Array.isArray(task.report.plan_steps) && (
                <div className="v2-domain-grid">
                  {(
                    task.report.plan_steps as {
                      id: string;
                      tool: string;
                      rationale: string;
                      evidence_ids: string[];
                    }[]
                  ).map((step) => (
                    <div className="v2-domain" key={step.id}>
                      <h3>
                        {(
                          {
                            inspect_action: "核对动作上下文",
                            inspect_evidence: "核对准确证据",
                            propose_relation: "建议知识关联",
                            propose_journey: "生成流程候选",
                            draft_document: "生成独立文档草稿",
                          } as Record<string, string>
                        )[step.tool] || step.tool}
                      </h3>
                      <p>{step.rationale}</p>
                      <small>
                        {step.evidence_ids.length} 条准确证据引用 · {step.id}
                      </small>
                    </div>
                  ))}
                </div>
              )}
              <details>
                <summary>查看结构化产物与版本依据</summary>
                <pre className="v2-json">
                  {JSON.stringify(task.report, null, 2)}
                </pre>
              </details>
            </>
          )}
          <TaskResults report={task.report} result={task.result} />
        </Panel>
      )}
    </>
  );
}
