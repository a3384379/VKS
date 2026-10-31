import { useEffect, useState } from "react";
import { api } from "./api";
import { Surface } from "./design-system/Surface";
import { Button, Notice } from "./components";
import { useWorkspace } from "./context";
type Report = {
  counts: Record<string, number>;
  gaps: {
    code: string;
    count: number;
    denominator: number;
    message: string;
    next_action: string;
  }[];
  sha256: string;
  scope: string;
  jobs: { id: string; kind: string; state: string; retry_allowed: boolean }[];
};
export function OnboardingReport() {
  const { w, navigate } = useWorkspace();
  const [report, setReport] = useState<Report | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    setReport(null);
    setError("");
    const controller = new AbortController();
    const q = new URLSearchParams(
      w.selected_release
        ? { release_id: w.selected_release.id }
        : w.selected_analysis_set
          ? { analysis_set_id: w.selected_analysis_set.id }
          : {},
    );
    void api<Report>(
      `/api/v2/projects/${w.project.id}/onboarding/report?${q}`,
      { signal: controller.signal },
    )
      .then(setReport)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [
    w.project.id,
    w.selected_analysis_set?.id,
    w.selected_analysis_set?.selection_version,
    w.selected_release?.id,
    w.jobs.map((j) => j.id + j.state).join(","),
  ]);
  return (
    <Surface title="项目接入报告">
      {error && <Notice tone="red">{error}</Notice>}
      {!report && !error && <p role="status">正在核对当前可见资料…</p>}
      {report && (
        <>
          <p>{report.scope}</p>
          <p>
            页面 {report.counts.pages} · 动作 {report.counts.actions} · 场景{" "}
            {report.counts.scenarios} · 文档 {report.counts.documents}
          </p>
          <p>
            有有效关联的动作 {report.counts.actions_with_valid_links} /{" "}
            {report.counts.action_denominator}；待审关系{" "}
            {report.counts.excluded_relations} / {report.counts.relations}。
          </p>
          <div className="v2-domain-grid">
            {report.gaps.map((gap) => (
              <div className="v2-domain" key={gap.code}>
                <p>{gap.message}</p>
                <p>
                  {gap.count} / {gap.denominator}
                </p>
                <Button
                  onClick={() =>
                    navigate(
                      gap.next_action === "manage_relations"
                        ? "relations"
                        : gap.next_action === "create_knowledge_task"
                          ? "agent"
                          : "imports",
                    )
                  }
                >
                  处理此缺口
                </Button>
              </div>
            ))}
          </div>
          {!!report.jobs.length && (
            <details>
              <summary>近期任务与重试入口</summary>
              {report.jobs.map((job) => (
                <p key={job.id}>
                  {job.id.slice(0, 8)} · {job.kind} · {job.state}{" "}
                  <Button onClick={() => navigate("imports")}>
                    {job.retry_allowed ? "查看问题并重试" : "查看任务"}
                  </Button>
                </p>
              ))}
            </details>
          )}
          <small className="v2-hash">报告摘要：{report.sha256}</small>
        </>
      )}
    </Surface>
  );
}
