import { useEffect, useState } from "react";
import { api } from "./api";
import { Notice, Panel, Tag } from "./components";
import { useWorkspace } from "./context";

type Diagnostics = {
  declared_alignment: string;
  runtime_alignment: string;
  comparisons: {
    service_key: string;
    axis: string;
    status: string;
    values: { source_id: string; value: string | null }[];
  }[];
  unknowns: string[];
};
export function SourceDiagnostics() {
  const { w } = useWorkspace();
  const [report, setReport] = useState<Diagnostics | null>(null),
    [error, setError] = useState("");
  const key = `${w.project.id}:${w.selected_analysis_set?.id}:${w.selected_analysis_set?.selection_version}:${w.selected_release?.id}`;
  useEffect(() => {
    const controller = new AbortController();
    setReport(null);
    setError("");
    const q = new URLSearchParams(
      w.selected_release
        ? { release_id: w.selected_release.id }
        : w.selected_analysis_set
          ? { analysis_set_id: w.selected_analysis_set.id }
          : {},
    );
    void api<Diagnostics>(
      `/api/v2/projects/${w.project.id}/source-diagnostics?${q}`,
      { signal: controller.signal },
    )
      .then((x) => {
        if (!controller.signal.aborted) setReport(x);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [key]);
  return (
    <Panel
      title="来源版本诊断"
      action={
        report && (
          <Tag>
            {report.declared_alignment === "mismatched"
              ? "版本声明不一致"
              : report.declared_alignment === "matched"
                ? "版本声明一致"
                : "版本信息不足"}
          </Tag>
        )
      }
    >
      {error ? (
        <Notice tone="red">{error}</Notice>
      ) : !report ? (
        <p role="status">正在检查当前可见来源版本…</p>
      ) : (
        <>
          <p>
            运行部署对齐：{report.runtime_alignment}
            。来源声明的一致性与运行验证分别保存。
          </p>
          {report.comparisons.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>服务</th>
                    <th>版本维度</th>
                    <th>来源声明</th>
                    <th>比较结果</th>
                  </tr>
                </thead>
                <tbody>
                  {report.comparisons.map((c) => (
                    <tr key={`${c.service_key}:${c.axis}`}>
                      <td>{c.service_key}</td>
                      <td>
                        {c.axis === "code_revision" ? "代码修订" : "构建标识"}
                      </td>
                      <td>
                        {c.values
                          .map((v) => `${v.source_id}: ${v.value || "未提供"}`)
                          .join("；")}
                      </td>
                      <td>{c.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p>尚无可比较的当前来源。</p>
          )}
          <p className="muted">{report.unknowns.join("；")}</p>
        </>
      )}
    </Panel>
  );
}
