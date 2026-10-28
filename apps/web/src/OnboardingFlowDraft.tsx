import { useEffect, useState } from "react";
import { api, mutation } from "./api";
import { Button, Notice, Tag } from "./components";
import { usePermission, useReadScope, useWorkspace } from "./context";
import type { Job } from "./types";

export function OnboardingFlowDraft() {
  const { w, navigate, notify } = useWorkspace();
  const canRun = usePermission("analysis:run");
  const readScope = useReadScope();
  const aid = w.selected_analysis_set?.id;
  const version = w.selected_analysis_set?.selection_version;
  const [actions, setActions] = useState<string[]>([]),
    [scopeVersion, setScopeVersion] = useState(version),
    [jobId, setJobId] = useState(""),
    [result, setResult] = useState<{ scope: string; value: Job } | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const job = result?.scope === readScope ? result.value : null;
  useEffect(() => {
    setActions([]);
    setJobId("");
    setResult(null);
    setScopeVersion(version);
  }, [w.project.id, aid]);
  useEffect(() => {
    setResult(null);
    if (!jobId) return;
    const controller = new AbortController();
    let timer: number;
    const load = () =>
      void api<Job>(`/jobs/${jobId}`, { signal: controller.signal })
        .then((row) => {
          setResult({ scope: readScope, value: row });
          if (["queued", "running", "retry_wait"].includes(row.state))
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
  }, [jobId, readScope]);
  if (!aid || w.selected_release || !w.actions.length) return null;
  const stale =
    scopeVersion !== version ||
    actions.some((id) => !w.actions.some((row) => row.id === id));
  async function create() {
    if (!aid || stale || !actions.length) return;
    setBusy(true);
    setError("");
    try {
      const row = await api<Job>(
        `/api/v2/projects/${w.project.id}/knowledge-tasks`,
        mutation("POST", {
          analysis_set_id: aid,
          selection_version: scopeVersion,
          task_kind: "business_flow",
          instruction:
            "由接入向导中的人员选择并声明步骤顺序，生成独立待审流程候选",
          action_ids: actions,
          max_model_calls: 0,
        }),
      );
      setJobId(row.id);
      notify("已按准确基线创建流程候选任务");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function move(index: number, delta: number) {
    setActions((rows) => {
      const next = [...rows];
      const target = index + delta;
      if (target >= 0 && target < rows.length)
        [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }
  return (
    <section className="v2-onboarding-flow" aria-label="向导流程草稿">
      <h3>从已发现动作建立第一个流程草稿</h3>
      <p>
        选择步骤并调整顺序。此顺序由人员声明，尚未证明真实业务执行条件；生成结果需要审核。
      </p>
      <Tag>
        当前可见 {w.actions.length} 个动作 · 最多选择 30 个 · 无模型调用
      </Tag>
      {error && <Notice tone="red">{error}</Notice>}
      {stale && (
        <Notice>
          来源或草稿版本已变化，请重新核对当前动作范围。
          <Button
            onClick={() => {
              setActions((rows) =>
                rows.filter((id) => w.actions.some((row) => row.id === id)),
              );
              setScopeVersion(version);
            }}
          >
            已按当前基线核对范围
          </Button>
        </Notice>
      )}
      <div className="v2-action-options">
        {w.actions.map((row) => (
          <label key={row.id}>
            <input
              type="checkbox"
              aria-label={`向导流程动作 ${row.name}`}
              checked={actions.includes(row.id)}
              disabled={
                busy || (!actions.includes(row.id) && actions.length >= 30)
              }
              onChange={(e) => {
                setActions((rows) =>
                  e.target.checked
                    ? [...rows, row.id]
                    : rows.filter((id) => id !== row.id),
                );
                if (!actions.length) setScopeVersion(version);
              }}
            />
            {row.name}
          </label>
        ))}
      </div>
      <ol className="v2-ordered-actions">
        {actions.map((id, index) => {
          const title =
            w.actions.find((row) => row.id === id)?.name ||
            "当前基线已移除此动作";
          return (
            <li key={id}>
              <span>{title}</span>
              <Button
                disabled={busy || index === 0}
                onClick={() => move(index, -1)}
                aria-label={`向导动作上移 ${title}`}
              >
                上移
              </Button>
              <Button
                disabled={busy || index === actions.length - 1}
                onClick={() => move(index, 1)}
                aria-label={`向导动作下移 ${title}`}
              >
                下移
              </Button>
            </li>
          );
        })}
      </ol>
      <Button
        kind="primary"
        disabled={
          !canRun ||
          busy ||
          stale ||
          !actions.length ||
          (!!job && ["queued", "running", "retry_wait"].includes(job.state))
        }
        onClick={() => void create()}
      >
        生成向导流程草稿
      </Button>
      {job && (
        <div className="row wrap">
          <Tag value={job.state} />
          <span>任务 {job.id.slice(0, 12)}</span>
          {job.state === "succeeded" && (
            <>
              <Notice>
                已生成独立待审流程草稿，原始材料与已有流程保持不变。
              </Notice>
              <Button onClick={() => navigate("journeys")}>
                查看向导流程草稿
              </Button>
            </>
          )}
          {job.error && <Notice tone="red">{job.error.message}</Notice>}
        </div>
      )}
    </section>
  );
}
