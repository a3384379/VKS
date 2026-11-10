import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  CheckCircle2,
  FileArchive,
  FileCode2,
  FileText,
  Image,
  LoaderCircle,
  RefreshCw,
  Send,
  ShieldCheck,
  Sparkles,
  Upload,
} from "lucide-react";
import { api, bytes, date, mutation, sha256 } from "./api";
import {
  Button,
  Empty,
  Notice,
  Panel,
  Steps,
  Tag,
  issueText,
} from "./components";
import { usePermission, useWorkspace } from "./context";
import { NewDraftButton } from "./DraftActions";
import { SourceManagement } from "./SourceManagement";
import { importDetails, importPayload, type ImportKind } from "./imports";
import type { Job, Preflight } from "./types";

const types = [
  { kind: "bundle" as const, label: "VKS 资料包", icon: FileArchive },
  { kind: "documents" as const, label: "Markdown / OpenAPI", icon: FileText },
  { kind: "prototype" as const, label: "原型 / 截图", icon: Image },
];
export function ImportCenter() {
  const { w, run, busy, navigate } = useWorkspace();
  const canImport = usePermission("source:import");
  const [kind, setKind] = useState<ImportKind>("bundle"),
    [files, setFiles] = useState<File[]>([]),
    [handles, setHandles] = useState<string[]>([]),
    [preflight, setPreflight] = useState<Preflight | null>(null),
    [uploading, setUploading] = useState(false),
    [error, setError] = useState(""),
    [drag, setDrag] = useState(false);
  const [analysisId, setAnalysisId] = useState(
    w.selected_analysis_set?.state !== "frozen"
      ? w.selected_analysis_set?.id || ""
      : "",
  );
  const receiptKey = useRef("");
  const input = useRef<HTMLInputElement>(null),
    generation = useRef(0);
  const drafts = w.analysis_sets.filter(
    (a) => a.state !== "frozen" && a.state !== "released",
  );
  useEffect(() => {
    generation.current++;
    setFiles([]);
    setHandles([]);
    setPreflight(null);
    setUploading(false);
    setError("");
    setAnalysisId(
      w.selected_analysis_set?.state !== "frozen"
        ? w.selected_analysis_set?.id || ""
        : "",
    );
  }, [w.selected_analysis_set?.id, w.selected_analysis_set?.state]);
  useEffect(
    () => () => {
      generation.current++;
    },
    [],
  );
  function changeKind(value: ImportKind) {
    setKind(value);
    setFiles([]);
    setHandles([]);
    setPreflight(null);
    setError("");
  }
  async function inspect(incoming: File[]) {
    const sequence = ++generation.current;
    setFiles(incoming);
    setHandles([]);
    setPreflight(null);
    setError("");
    if (!incoming.length) return;
    if (kind !== "documents" && incoming.length !== 1) {
      setError("资料包和原型每次选择一个文件。");
      return;
    }
    if (incoming.length > 100) {
      setError("每次最多导入 100 份文档。");
      return;
    }
    if (kind !== "bundle" && !analysisId) {
      setError("请先选择草稿基线，或创建空白基线。");
      return;
    }
    if (incoming.some((file) => !importDetails(file.name, kind))) {
      setError(
        kind === "bundle"
          ? "请选择包含 manifest.json 的 ZIP 资料包。"
          : kind === "documents"
            ? "文档支持 .md、.markdown、.json、.yaml 和 .yml。"
            : "原型支持 HTML、安全预览包、PNG、JPEG 和 WebP。",
      );
      return;
    }
    setUploading(true);
    try {
      const ids: string[] = [];
      for (const file of incoming) {
        const detail = importDetails(file.name, kind)!;
        const session = await api<{ artifact_id: string; upload_url: string }>(
          `/projects/${w.project.id}/upload-sessions`,
          mutation("POST", {
            filename: file.name,
            size_bytes: file.size,
            sha256: await sha256(file),
            media_type: detail.media_type,
          }),
        );
        await api(session.upload_url, {
          method: "PUT",
          body: file,
          headers: { "Content-Type": "application/octet-stream" },
        });
        ids.push(session.artifact_id);
      }
      const details = importDetails(incoming[0].name, kind)!;
      const report = await api<Preflight>(
        kind === "prototype"
          ? `/api/v3/projects/${w.project.id}/prototype/import-previews`
          : `/projects/${w.project.id}/imports/preflight`,
        mutation(
          "POST",
          kind === "prototype"
            ? { analysis_set_id: analysisId, artifact_id: ids[0] }
            : importPayload(kind, ids, analysisId, details.prototype_mode),
        ),
      );
      if (sequence !== generation.current) return;
      setHandles(ids);
      receiptKey.current = crypto.randomUUID();
      setPreflight(report);
    } catch (e) {
      if (sequence === generation.current) setError((e as Error).message);
    } finally {
      if (sequence === generation.current) setUploading(false);
    }
  }
  async function submit() {
    if (!files[0] || !handles.length) return;
    const detail = importDetails(files[0].name, kind)!;
    const job = await run(
      () =>
        api<Job>(
          kind === "prototype"
            ? `/api/v3/projects/${w.project.id}/prototype/imports`
            : `/projects/${w.project.id}/imports`,
          kind === "prototype"
            ? {
                method: "POST",
                body: JSON.stringify({
                  analysis_set_id: analysisId,
                  artifact_id: handles[0],
                  preview_digest: preflight?.preview_digest,
                }),
                headers: {
                  "If-Match": String(preflight?.selection_version),
                  "Idempotency-Key": receiptKey.current,
                },
              }
            : mutation(
                "POST",
                importPayload(kind, handles, analysisId, detail.prototype_mode),
              ),
        ),
      "导入任务已提交，结果和问题清单在任务中心保留",
    );
    if (job) {
      setFiles([]);
      setHandles([]);
      setPreflight(null);
    }
  }
  if (!canImport)
    return (
      <Panel>
        <Empty
          title="当前账户没有资料导入权限"
          description="你仍可浏览被授予的已发布知识。导入资料需要编辑角色及 source:import 权限。"
          action={
            <Button onClick={() => navigate("overview")}>返回项目总览</Button>
          }
        />
      </Panel>
    );
  return (
    <>
      <Steps
        steps={["选择资料", "格式预检", "解析与知识", "人工知识保存"]}
        current={preflight ? 1 : 0}
      />
      <div className="import-layout">
        <div>
          <Panel
            title="接入项目资料"
            action={<Tag value="static">只读采集与导入</Tag>}
          >
            <p className="section-description">
              上传资料包，或直接导入文档、接口规范、原型和云收费离线
              HTML。系统保留原始文件。
            </p>
            <div className="source-tabs import-kind-tabs">
              {types.map((t) => (
                <button
                  className={kind === t.kind ? "active" : ""}
                  key={t.kind}
                  onClick={() => changeKind(t.kind)}
                  disabled={uploading || busy}
                >
                  <t.icon size={16} />
                  {t.label}
                </button>
              ))}
            </div>
            {kind !== "bundle" && (
              <div className="draft-import-target">
                <label>
                  导入到草稿基线
                  <select
                    value={analysisId}
                    disabled={uploading}
                    onChange={(e) => {
                      setAnalysisId(e.target.value);
                      setFiles([]);
                      setHandles([]);
                      setPreflight(null);
                    }}
                  >
                    <option value="">选择可写草稿</option>
                    {drafts.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                  </select>
                </label>
                <NewDraftButton empty small />
                {w.selected_analysis_set?.state === "frozen" && (
                  <NewDraftButton small />
                )}
              </div>
            )}
            <input
              ref={input}
              className="sr-only"
              type="file"
              multiple={kind === "documents"}
              accept={
                kind === "bundle"
                  ? ".zip"
                  : kind === "documents"
                    ? ".md,.markdown,.json,.yaml,.yml"
                    : ".html,.htm,.zip,.png,.jpg,.jpeg,.webp"
              }
              aria-label="选择导入资料"
              onChange={(e) => {
                if (e.target.files?.length)
                  void inspect(Array.from(e.target.files));
              }}
            />
            <div
              role="button"
              tabIndex={uploading ? -1 : 0}
              aria-disabled={uploading}
              className={`upload-zone ${drag ? "dragging" : ""}`}
              onClick={() => {
                if (!uploading) input.current?.click();
              }}
              onKeyDown={(e) => {
                if (!uploading && (e.key === "Enter" || e.key === " ")) {
                  e.preventDefault();
                  input.current?.click();
                }
              }}
              onDragOver={(e) => {
                e.preventDefault();
                setDrag(true);
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDrag(false);
                if (!uploading && e.dataTransfer.files.length)
                  void inspect(Array.from(e.dataTransfer.files));
              }}
            >
              <div className="upload-icon">
                {uploading ? (
                  <LoaderCircle size={28} className="spin" />
                ) : (
                  <Upload size={28} />
                )}
              </div>
              <h3>
                {uploading
                  ? "正在上传并验证原始字节…"
                  : "点击选择，或将资料拖到这里"}
              </h3>
              <p>
                {kind === "bundle"
                  ? "包含 manifest.json 与资料的 VKS ZIP 包"
                  : kind === "documents"
                    ? "可批量选择 Markdown 或 OpenAPI JSON / YAML"
                    : "HTML、预览 ZIP 或图片；执行脚本与真实业务请求始终关闭"}
              </p>
              <span>SHA-256 校验 · 原文保留 · 先预检后确认</span>
            </div>
            {files.length > 0 && (
              <div className="uploaded-file">
                <FileArchive size={22} />
                <div>
                  <strong>
                    {files.length === 1
                      ? files[0].name
                      : `${files.length} 份文档`}
                  </strong>
                  <small>
                    {bytes(files.reduce((sum, f) => sum + f.size, 0))} ·{" "}
                    {preflight ? "预检已返回" : "等待检查"}
                  </small>
                </div>
                {preflight && <CheckCircle2 className="green-text" size={20} />}
              </div>
            )}
            {error && <Notice tone="red">{error}</Notice>}
            {preflight && (
              <div className="preflight-result">
                <div className="row between">
                  <strong>
                    <ShieldCheck size={17} />
                    预检报告
                  </strong>
                  <Tag value={preflight.valid === false ? "failed" : "valid"}>
                    {preflight.valid === false ? "未通过" : "可导入"}
                  </Tag>
                </div>
                <div className="preflight-stats">
                  <div>
                    <strong>{preflight.file_count ?? files.length}</strong>
                    <span>文件</span>
                  </div>
                  <div>
                    <strong>
                      {bytes(
                        preflight.total_size_bytes ??
                          Number(
                            preflight.size_bytes ||
                              files.reduce((sum, f) => sum + f.size, 0),
                          ),
                      )}
                    </strong>
                    <span>原始资料</span>
                  </div>
                  <div>
                    <strong>{preflight.issues?.length || 0}</strong>
                    <span>能力与问题提示</span>
                  </div>
                </div>
                {preflight.operations &&
                typeof preflight.operations === "object" ? (
                  <p>
                    新增{" "}
                    {String(
                      (preflight.operations as Record<string, number>).added,
                    )}{" "}
                    · 更新{" "}
                    {String(
                      (preflight.operations as Record<string, number>).updated,
                    )}{" "}
                    · 复用{" "}
                    {String(
                      (preflight.operations as Record<string, number>).reused,
                    )}
                  </p>
                ) : null}
                {preflight.issues?.slice(0, 20).map((issue, i) => (
                  <Notice key={i} tone="amber">
                    {issueText(issue)}
                  </Notice>
                ))}
                <details>
                  <summary>完整预检诊断</summary>
                  <pre className="json-block">
                    {JSON.stringify(preflight, null, 2)}
                  </pre>
                </details>
                <div className="row right">
                  <Button
                    kind="primary"
                    disabled={busy || uploading || preflight.valid === false}
                    onClick={() => {
                      void submit();
                    }}
                  >
                    <Send size={15} />
                    确认导入
                  </Button>
                </div>
              </div>
            )}
            <Notice>
              {kind === "bundle" ? (
                <>
                  资料包的 <code>project_key</code> 应为{" "}
                  <strong>{w.project.key}</strong>。
                </>
              ) : (
                <>独立资料导入所选草稿，历史发布版保持固定。</>
              )}{" "}
              导入仅创建事实索引和待整理候选。
            </Notice>
          </Panel>
          <SourceManagement />
          <Panel
            title="任务中心"
            action={<Tag>{w.jobs.length} 个最近任务</Tag>}
          >
            <div className="job-list">
              {w.jobs.map((job) => (
                <JobCard key={job.id} job={job} />
              ))}
              {!w.jobs.length && (
                <Empty
                  title="暂无处理任务"
                  description="确认导入后，可在这里追踪阶段、完整度与结构化诊断。"
                />
              )}
            </div>
          </Panel>
        </div>
        <div>
          <Panel title="从一个小场景开始">
            <div className="sample-art">
              <FileText size={29} />
              <Sparkles size={23} />
            </div>
            <h3 className="sample-title">体验可追溯的知识闭环</h3>
            <p className="section-description">
              内置合成材料含页面动作、Markdown、API 与 Java/MyBatis
              解析样例。运行证据与静态声明分开。
            </p>
            <Button
              kind="full"
              disabled={busy}
              onClick={() => {
                void run(
                  () =>
                    api<Job>(
                      `/projects/${w.project.id}/imports/sample`,
                      mutation("POST", {}),
                    ),
                  "合成样例任务已提交",
                );
              }}
            >
              <Sparkles size={16} />
              导入合成示例
            </Button>
            <p className="fine-print">不包含公司数据，不访问开票或支付服务。</p>
          </Panel>
          <Panel title="资料与隐私">
            <ul className="policy-list">
              <li>
                <ShieldCheck size={18} />
                <span>
                  <strong>原始文件保持完整</strong>
                  <small>BOM、换行与原文哈希保留</small>
                </span>
              </li>
              <li>
                <ShieldCheck size={18} />
                <span>
                  <strong>发布版本不会被覆盖</strong>
                  <small>修改前创建可写草稿</small>
                </span>
              </li>
            </ul>
          </Panel>
          <Panel title="下一步">
            <Button kind="subtle full" onClick={() => navigate("relations")}>
              检查候选关系
            </Button>
            <p className="fine-print">分析完成和知识发布是两个独立步骤。</p>
          </Panel>
        </div>
      </div>
    </>
  );
}

function JobCard({ job }: { job: Job }) {
  const { w, run, busy, session } = useWorkspace();
  const canManage = usePermission("project:manage");
  const canOperate = canManage || job.created_by === session.actor_id;
  function diagnostic() {
    const blob = new Blob([JSON.stringify(job, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `job-${job.id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  }
  return (
    <div className="job-card">
      <div className="row between">
        <div className="row">
          <span className="job-icon">
            <FileCode2 size={21} />
          </span>
          <div>
            <strong>
              {job.kind === "import"
                ? "导入项目资料"
                : job.kind === "export"
                  ? "导出知识包"
                  : job.kind === "capture"
                    ? "采集业务场景"
                    : job.kind}
            </strong>
            <small>
              {date(job.created_at)} · <code>{job.id.slice(0, 12)}</code>
            </small>
          </div>
        </div>
        <Tag value={job.state} />
      </div>
      <div className="job-stage">
        <span>当前阶段</span>
        <strong>{job.stage}</strong>
        <Tag value={job.completeness} />
      </div>
      {["queued", "running", "cancelling", "retry_wait"].includes(
        job.state,
      ) && (
        <div className="indeterminate">
          <i />
        </div>
      )}
      {job.error && (
        <Notice tone="red">
          {job.error.code} · {job.error.message}
        </Notice>
      )}
      {job.result && (
        <details>
          <summary>查看阶段结果与问题清单</summary>
          <pre className="json-block">
            {JSON.stringify(job.result, null, 2)}
          </pre>
        </details>
      )}
      <div className="row between job-footer">
        <span>{job.issue_count || 0} 项问题</span>
        <div className="row">
          {job.kind === "export" && job.state === "succeeded" && (
            <a
              className="text-button"
              href={`/api/v1/projects/${w.project.id}/exports/${job.id}/download`}
            >
              <ArrowDownToLine size={14} />
              下载知识包
            </a>
          )}
          {canOperate && ["queued", "running"].includes(job.state) && (
            <button
              className="text-button danger-text"
              disabled={busy}
              onClick={() => {
                void run(
                  () =>
                    api(
                      `/jobs/${job.id}/cancel`,
                      mutation("POST", { reason: "用户在任务中心取消" }),
                    ),
                  "取消请求已记录",
                );
              }}
            >
              取消任务
            </button>
          )}
          {canOperate && ["failed", "cancelled"].includes(job.state) && (
            <Button
              disabled={busy}
              onClick={() => {
                void run(
                  () => api(`/jobs/${job.id}/retry`, mutation("POST", {})),
                  "重试请求已提交",
                );
              }}
            >
              <RefreshCw size={14} />
              重试
            </Button>
          )}
          <button className="text-button" onClick={diagnostic}>
            下载诊断
            <ArrowDownToLine size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}
