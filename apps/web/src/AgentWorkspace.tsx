import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useState } from "react";
import { RefreshCw, Sparkles, Square } from "lucide-react";
import { api, mutation } from "./api";
import { Button, Empty, Notice, Tag } from "./components";
import { usePermission, useReadScope, useWorkspace } from "./context";
import type { Dict, Job } from "./types";
import { TaskStages } from "./TaskStages";
import { ModuleCandidates, type ModuleCandidate } from "./ModuleCandidates";
const templates = {
  project_overview: "项目概览",
  module_analysis: "模块解析",
  business_flow: "业务流程草稿",
  evidence_gaps: "证据缺口",
  document_draft: "AI 文档草稿",
  knowledge_plan: "自然语言知识任务",
  difference_review: "差异复核准备",
};
type Task = Job & {
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
};
export function AgentWorkspace() {
  const { w, run, notify, navigate } = useWorkspace();
  const canRun = usePermission("analysis:run");
  const readScope = useReadScope();
  const [kind, setKind] = useState<keyof typeof templates>("project_overview"),
    [fromRelease, setFromRelease] = useState(w.releases[0]?.id || ""),
    [instruction, setInstruction] = useState(""),
    [actions, setActions] = useState<string[]>([]),
    [modelConsent, setConsent] = useState(false),
    [taskRow, setTaskRow] = useState<{ scope: string; value: Task } | null>(
      null,
    ),
    [taskId, setTaskId] = useState(""),
    [error, setError] = useState("");
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
            ["queued", "running", "retry_wait", "cancelling"].includes(t.state)
          )
            timer = window.setTimeout(load, 1500);
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
          task_kind: kind,
          instruction,
          action_ids: actions,
          ...(kind === "difference_review"
            ? { from_release_id: fromRelease }
            : {}),
          max_entities: 100,
          max_model_calls:
            ["document_draft", "knowledge_plan"].includes(kind) && modelConsent
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
  return (
    <>
      <div className="agent-v2-layout">
        <Panel title="知识任务" className="agent-v2-plan">
          <span className="eyebrow">KNOWLEDGE ENGINEERING</span>
          <h2>从固定来源，整理可审核的知识。</h2>
          <p>
            普通模板使用现有资料和确定性分析。AI
            文档模板单独消耗模型额度，产物需要人员审核。
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
              <legend>按顺序选择动作（声明顺序，最多 30 个）</legend>
              {w.actions.map((a) => (
                <label key={a.id}>
                  <input
                    type="checkbox"
                    checked={actions.includes(a.id)}
                    onChange={(e) =>
                      setActions((ids) =>
                        e.target.checked
                          ? [...ids, a.id]
                          : ids.filter((id) => id !== a.id),
                      )
                    }
                  />
                  {actions.includes(a.id) && `${actions.indexOf(a.id) + 1}. `}
                  {a.name}
                </label>
              ))}
            </fieldset>
          )}
          {["document_draft", "knowledge_plan"].includes(kind) && (
            <label>
              <input
                type="checkbox"
                checked={modelConsent}
                onChange={(e) => setConsent(e.target.checked)}
              />
              授权本任务至多一次模型调用，受项目资料策略及成本预算限制
            </label>
          )}
          <div className="row wrap">
            <Tag>版本：{w.selected_analysis_set?.name || "未选择"}</Tag>
            <Tag>仅草稿与候选</Tag>
          </div>
          <Button
            kind="primary"
            disabled={
              !canRun ||
              !w.selected_analysis_set ||
              !!w.selected_release ||
              (kind === "business_flow" && !actions.length) ||
              (kind === "difference_review" && !fromRelease) ||
              (["document_draft", "knowledge_plan"].includes(kind) &&
                !modelConsent) ||
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
                            ? "等待人员审核"
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
            <Button onClick={() => navigate("journeys")}>查看流程草稿</Button>
            <Button onClick={() => navigate("reviews")}>打开人工审核</Button>
          </div>
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
                所有产物为待审草稿；静态与声明信息不能证明运行因果。
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
        </Panel>
      )}
    </>
  );
}
