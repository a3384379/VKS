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
import { ApiError, api, bytes, date, mutation, sha256 } from "./api";
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
import type { Job, Preflight, Workspace } from "./types";
import { workspaceBaseline } from "./workspace-baseline";

const types = [
  { kind: "bundle" as const, label: "VKS 资料包", icon: FileArchive },
  { kind: "documents" as const, label: "Markdown / OpenAPI", icon: FileText },
  { kind: "prototype" as const, label: "原型 / 混合资料", icon: Image },
];

type Operations = { added: number; updated: number; reused: number };
type MaterialPreview = {
  artifact_id: string;
  filename: string;
  format: string;
  status: "ready" | "blocked";
  issues: unknown[];
  conflicts_with: string[];
  operations?: Operations;
  file_count?: number;
  entity_count?: number;
  relation_count?: number;
  prototype_import?: Record<string, unknown>;
  mappings?: {
    kind: string;
    key: string;
    name: string;
    operation: keyof Operations;
    artifact_path?: string;
    missing_surface_count: number;
  }[];
};
type BatchPreview = {
  analysis_set_id: string;
  preview_digest: string;
  selection_version: number;
  materials: MaterialPreview[];
};
type BatchResult = {
  analysis_set_id: string;
  first_page_key: string | null;
  skipped_count: number;
  materials: {
    artifact_id: string;
    filename: string;
    status: "imported" | "reused";
    operations: Operations;
    issues: unknown[];
  }[];
};
const operationLabels = { added: "新增", updated: "更新", reused: "复用" };
const activeJob = (job?: Job) =>
  !!job &&
  ["queued", "running", "cancelling", "retry_wait"].includes(job.state);

function fileDetails(filename: string, kind: ImportKind) {
  return (
    importDetails(filename, kind) ||
    (kind === "prototype" ? importDetails(filename, "documents") : null)
  );
}

function OperationCounts({ operations }: { operations: Operations }) {
  return (
    <>
      {Object.entries(operationLabels)
        .map(
          ([key, label]) => `${label} ${operations[key as keyof Operations]}`,
        )
        .join(" · ")}
    </>
  );
}

function BatchReport({
  batch,
  selected,
  busy,
  stale,
  onToggle,
  onConfirm,
  onRecheck,
}: {
  batch: BatchPreview;
  selected: string[];
  busy: boolean;
  stale: boolean;
  onToggle: (id: string) => void;
  onConfirm: () => void;
  onRecheck?: () => void;
}) {
  return (
    <div className="preflight-result">
      <div className="row between">
        <strong>
          <ShieldCheck size={17} /> 预检报告
        </strong>
        <Tag>{selected.length} 份已选</Tag>
      </div>
      {batch.materials.map((row) => {
        const conflicts = row.conflicts_with.some((id) =>
          selected.includes(id),
        );
        return (
          <div className="upload-batch-material" key={row.artifact_id}>
            <label className="row">
              <input
                type="checkbox"
                checked={selected.includes(row.artifact_id)}
                disabled={busy || row.status === "blocked" || conflicts}
                onChange={() => onToggle(row.artifact_id)}
              />
              <strong>{row.filename}</strong>
              {row.status === "blocked" ? (
                <Tag value="failed">需处理</Tag>
              ) : conflicts ? (
                <Tag value="failed">与已选资料冲突</Tag>
              ) : null}
            </label>
            {row.operations && (
              <p>
                <OperationCounts operations={row.operations} /> ·{" "}
                {row.file_count} 个制品 · {row.relation_count} 条关系
              </p>
            )}
            {row.prototype_import?.format === "charge-prototype" && (
              <p>
                {String(row.prototype_import.routes)} 条路由 ·{" "}
                {String(row.prototype_import.pages)} 个页面 ·{" "}
                {String(row.prototype_import.missing_surfaces)} 个待补画面
              </p>
            )}
            {row.prototype_import?.format === "charge-handbook" && (
              <p>{String(row.prototype_import.documents)} 份原文</p>
            )}
            {row.issues.length > 0 && (
              <details open={row.status === "blocked"}>
                <summary>{row.issues.length} 项预检问题</summary>
                <ul>
                  {row.issues.map((issue, i) => (
                    <li key={i}>{issueText(issue)}</li>
                  ))}
                </ul>
              </details>
            )}
            {!!row.mappings?.length && (
              <details>
                <summary>查看页、章节和动作映射</summary>
                <ul>
                  {row.mappings.map((item) => (
                    <li key={`${item.kind}:${item.key}`}>
                      {operationLabels[item.operation]} · {item.name}{" "}
                      {item.artifact_path && <code>{item.artifact_path}</code>}
                      {!!item.missing_surface_count &&
                        ` · ${item.missing_surface_count} 个待补画面`}
                    </li>
                  ))}
                </ul>
                {!!row.entity_count &&
                  row.entity_count > row.mappings.length && (
                    <p>
                      共 {row.entity_count} 项，显示前 {row.mappings.length}{" "}
                      项。
                    </p>
                  )}
              </details>
            )}
          </div>
        );
      })}
      {stale && <Notice tone="amber">草稿已更新，请重新预检后导入。</Notice>}
      <div className="row right">
        {stale && onRecheck ? (
          <Button kind="primary" disabled={busy} onClick={onRecheck}>
            重新预检
          </Button>
        ) : (
          <>
            {onRecheck && (
              <Button kind="subtle" disabled={busy} onClick={onRecheck}>
                重新预检
              </Button>
            )}
            <Button
              kind="primary"
              disabled={busy || stale || !selected.length}
              onClick={onConfirm}
            >
              <Send size={15} /> 导入选中资料
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

export function ImportCenter() {
  const { w, run, busy, navigate, navigateReadEntity, session, refresh } =
    useWorkspace();
  const canImport = usePermission("source:import");
  const [kind, setKind] = useState<ImportKind>("bundle"),
    [files, setFiles] = useState<File[]>([]),
    [handles, setHandles] = useState<string[]>([]),
    [preflight, setPreflight] = useState<Preflight | null>(null),
    [uploading, setUploading] = useState(false),
    [error, setError] = useState(""),
    [drag, setDrag] = useState(false);
  const [previewJob, setPreviewJob] = useState<Job>();
  const [importJob, setImportJob] = useState<Job>();
  const [selectedMaterials, setSelectedMaterials] = useState<string[]>([]);
  const currentPreview =
    w.jobs.find((job) => job.id === previewJob?.id) || previewJob;
  const currentImport =
    w.jobs.find((job) => job.id === importJob?.id) || importJob;
  const batch =
    currentPreview?.state === "succeeded" && currentPreview.result?.materials
      ? (currentPreview.result as unknown as BatchPreview)
      : undefined;
  const imported =
    currentImport?.state === "succeeded" && currentImport.result?.materials
      ? (currentImport.result as unknown as BatchResult)
      : undefined;
  const uploadDisabled = uploading || busy || activeJob(currentImport);
  const [analysisId, setAnalysisId] = useState(
    w.selected_analysis_set?.state !== "frozen"
      ? w.selected_analysis_set?.id || ""
      : "",
  );
  const receiptKey = useRef("");
  const recheckSelection = useRef<string[] | undefined>(undefined);
  const input = useRef<HTMLInputElement>(null),
    generation = useRef(0);
  const drafts = w.analysis_sets.filter(
    (a) => a.state !== "frozen" && a.state !== "released",
  );
  const target = drafts.find(
    (a) => a.id === (batch?.analysis_set_id || analysisId),
  );
  const stale =
    !!batch && target?.selection_version !== batch.selection_version;
  const readPermissions = (
    session.project_permissions?.[w.project.id] ||
    session.scopes ||
    []
  )
    .slice()
    .sort()
    .join(" ");
  const tracked = importJob || previewJob;
  const pendingOutsideList =
    activeJob(tracked) && !w.jobs.some((job) => job.id === tracked?.id)
      ? tracked
      : undefined;
  useEffect(() => {
    generation.current++;
    setFiles([]);
    setHandles([]);
    setPreflight(null);
    setPreviewJob(undefined);
    setImportJob(undefined);
    setSelectedMaterials([]);
    recheckSelection.current = undefined;
    setUploading(false);
    setError("");
    setAnalysisId(
      w.selected_analysis_set?.state !== "frozen"
        ? w.selected_analysis_set?.id || ""
        : "",
    );
  }, [
    w.project.id,
    session.actor_id,
    readPermissions,
    w.selected_analysis_set?.id,
    w.selected_analysis_set?.state,
  ]);
  useEffect(() => {
    if (!batch) return;
    const selected: string[] = [];
    for (const row of batch.materials) {
      if (
        row.status === "ready" &&
        (!recheckSelection.current ||
          recheckSelection.current.includes(row.artifact_id)) &&
        !row.conflicts_with.some((id) => selected.includes(id))
      )
        selected.push(row.artifact_id);
    }
    setSelectedMaterials(selected);
    recheckSelection.current = undefined;
    receiptKey.current = crypto.randomUUID();
  }, [currentPreview?.id, batch?.preview_digest]);
  useEffect(
    () => () => {
      generation.current++;
    },
    [],
  );
  useEffect(() => {
    if (!pendingOutsideList) return;
    const job = pendingOutsideList;
    const store = job.kind === "import_preview" ? setPreviewJob : setImportJob;
    let active = true;
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const result = await api<Job>(
          `/jobs/${job.id}?project_id=${encodeURIComponent(w.project.id)}`,
        );
        if (active)
          store((current) => (current?.id === job.id ? result : current));
      } catch (error) {
        if (
          active &&
          error instanceof ApiError &&
          [401, 403, 404].includes(error.status)
        ) {
          store((current) => (current?.id === job.id ? undefined : current));
          setError(error.message);
        }
      } finally {
        inFlight = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1800);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [pendingOutsideList?.id, w.project.id, readPermissions]);
  function changeKind(value: ImportKind) {
    generation.current++;
    setKind(value);
    setFiles([]);
    setHandles([]);
    setPreflight(null);
    setPreviewJob(undefined);
    setImportJob(undefined);
    setSelectedMaterials([]);
    recheckSelection.current = undefined;
    setError("");
  }
  async function queuePreview(ids: string[], aid: string, sequence: number) {
    const queued = await run(() =>
      api<Job>(
        `/api/v3/projects/${w.project.id}/prototype/import-previews`,
        mutation("POST", { analysis_set_id: aid, artifact_ids: ids }),
      ),
    );
    if (sequence === generation.current && queued) {
      setHandles(ids);
      setImportJob(undefined);
      setSelectedMaterials([]);
      setPreviewJob(queued);
    }
  }
  function recheck() {
    const ids = batch?.materials.map((row) => row.artifact_id) || handles;
    if (target && ids.length) {
      recheckSelection.current = batch ? selectedMaterials : undefined;
      void queuePreview(ids, target.id, ++generation.current);
    }
  }
  async function inspect(incoming: File[]) {
    if (uploadDisabled) return;
    const sequence = ++generation.current;
    setFiles(incoming);
    setHandles([]);
    setPreflight(null);
    setPreviewJob(undefined);
    setImportJob(undefined);
    setSelectedMaterials([]);
    recheckSelection.current = undefined;
    setError("");
    if (!incoming.length) return;
    if (kind === "bundle" && incoming.length !== 1) {
      setError("按资料包创建基线时，请选择一个 ZIP 文件。");
      return;
    }
    if (incoming.length > (kind === "prototype" ? 32 : 100)) {
      setError(
        kind === "prototype"
          ? "每批最多选择 32 份混合资料。"
          : "每次最多导入 100 份文档。",
      );
      return;
    }
    if (kind !== "bundle" && !analysisId) {
      setError("请先选择草稿基线，或创建空白基线。");
      return;
    }
    if (incoming.some((file) => !fileDetails(file.name, kind))) {
      setError(
        kind === "bundle"
          ? "请选择包含 manifest.json 的 ZIP 资料包。"
          : kind === "documents"
            ? "文档支持 .md、.markdown、.json、.yaml 和 .yml。"
            : "混合资料支持 Markdown、OpenAPI、HTML、ZIP、PNG、JPEG 和 WebP。",
      );
      return;
    }
    setUploading(true);
    try {
      const ids: string[] = [];
      for (const file of incoming) {
        if (sequence !== generation.current) return;
        const detail = fileDetails(file.name, kind)!;
        const digest = await sha256(file);
        if (sequence !== generation.current) return;
        const session = await api<{ artifact_id: string; upload_url: string }>(
          `/projects/${w.project.id}/upload-sessions`,
          mutation("POST", {
            filename: file.name,
            size_bytes: file.size,
            sha256: digest,
            media_type: detail.media_type,
          }),
        );
        if (sequence !== generation.current) return;
        await api(session.upload_url, {
          method: "PUT",
          body: file,
          headers: { "Content-Type": "application/octet-stream" },
        });
        ids.push(session.artifact_id);
      }
      if (sequence !== generation.current) return;
      if (kind === "prototype") {
        await queuePreview(ids, analysisId, sequence);
        return;
      }
      const report = await api<Preflight>(
        `/projects/${w.project.id}/imports/preflight`,
        mutation("POST", importPayload(kind, ids, analysisId)),
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
    const sequence = generation.current;
    if (kind === "prototype") {
      if (!batch || stale || !selectedMaterials.length || !currentPreview)
        return;
      const job = await run(() =>
        api<Job>(`/api/v3/projects/${w.project.id}/prototype/imports`, {
          method: "POST",
          body: JSON.stringify({
            analysis_set_id: batch.analysis_set_id,
            preview_id: currentPreview.id,
            preview_digest: batch.preview_digest,
            selected_artifact_ids: selectedMaterials,
          }),
          headers: {
            "If-Match": String(batch.selection_version),
            "Idempotency-Key": receiptKey.current,
          },
        }),
      );
      if (job && sequence === generation.current) setImportJob(job);
      return;
    }
    if (!files[0] || !handles.length) return;
    const job = await run(
      () =>
        api<Job>(
          `/projects/${w.project.id}/imports`,
          mutation("POST", importPayload(kind, handles, analysisId)),
        ),
      "导入任务已提交，结果和问题清单在任务中心保留",
    );
    if (job && sequence === generation.current) {
      setFiles([]);
      setHandles([]);
      setPreflight(null);
    }
  }
  function toggleMaterial(id: string) {
    receiptKey.current = crypto.randomUUID();
    setSelectedMaterials((current) =>
      current.includes(id)
        ? current.filter((key) => key !== id)
        : [...current, id],
    );
  }
  function resume(job: Job) {
    changeKind("prototype");
    if (typeof job.result?.analysis_set_id === "string")
      setAnalysisId(job.result.analysis_set_id);
    if (job.kind === "import_preview") setPreviewJob(job);
    else setImportJob(job);
  }
  async function openImportedPage() {
    if (!imported?.first_page_key || !navigateReadEntity) return;
    const sequence = generation.current;
    const target = await run(() =>
      api<Workspace>(
        `/projects/${w.project.id}/workspace?analysis_set_id=${encodeURIComponent(imported.analysis_set_id)}`,
      ),
    );
    if (!target || sequence !== generation.current) return;
    const page = target.pages.find(
      (row) => row.stable_key === imported.first_page_key,
    );
    if (page) navigateReadEntity(target, page, workspaceBaseline(target));
    else setError("当前范围内没有可打开的导入页面。");
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
        steps={["选择资料", "预检", "入库", "导入结果"]}
        current={
          imported ? 3 : currentImport ? 2 : preflight || currentPreview ? 1 : 0
        }
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
                  disabled={uploading || busy || activeJob(currentImport)}
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
                    disabled={uploading || activeJob(currentImport)}
                    onChange={(e) => {
                      generation.current++;
                      setAnalysisId(e.target.value);
                      setFiles([]);
                      setHandles([]);
                      setPreflight(null);
                      setPreviewJob(undefined);
                      setImportJob(undefined);
                      setSelectedMaterials([]);
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
              multiple={kind !== "bundle"}
              accept={
                kind === "bundle"
                  ? ".zip"
                  : kind === "documents"
                    ? ".md,.markdown,.json,.yaml,.yml"
                    : ".md,.markdown,.json,.yaml,.yml,.html,.htm,.zip,.png,.jpg,.jpeg,.webp"
              }
              aria-label="选择导入资料"
              onChange={(e) => {
                if (e.target.files?.length)
                  void inspect(Array.from(e.target.files));
                e.target.value = "";
              }}
            />
            <div
              role="button"
              tabIndex={uploadDisabled ? -1 : 0}
              aria-disabled={uploadDisabled}
              className={`upload-zone ${drag ? "dragging" : ""}`}
              onClick={() => {
                if (!uploadDisabled) input.current?.click();
              }}
              onKeyDown={(e) => {
                if (!uploadDisabled && (e.key === "Enter" || e.key === " ")) {
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
                if (!uploadDisabled && e.dataTransfer.files.length)
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
                    : "可混合选择文档、原型资料包、离线 HTML 和截图"}
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
                      : `${files.length} 份资料`}
                  </strong>
                  <small>
                    {bytes(files.reduce((sum, f) => sum + f.size, 0))} ·{" "}
                    {preflight || batch ? "预检已返回" : "等待检查"}
                  </small>
                </div>
                {preflight && <CheckCircle2 className="green-text" size={20} />}
              </div>
            )}
            {error && <Notice tone="red">{error}</Notice>}
            {kind === "prototype" && (
              <>
                {currentPreview && !batch && !currentImport && (
                  <JobCard job={currentPreview} />
                )}
                {batch && !currentImport && (
                  <BatchReport
                    batch={batch}
                    selected={selectedMaterials}
                    busy={busy}
                    stale={stale}
                    onToggle={toggleMaterial}
                    onConfirm={() => void submit()}
                    onRecheck={target ? recheck : undefined}
                  />
                )}
                {currentImport && !imported && <JobCard job={currentImport} />}
                {(currentImport || currentPreview)?.error?.code ===
                  "VKS_VERSION_CONFLICT" &&
                  target &&
                  !!handles.length && (
                    <Button disabled={busy} onClick={recheck}>
                      重新预检
                    </Button>
                  )}
                {imported && (
                  <div className="preflight-result">
                    <strong>导入结果</strong>
                    {imported.materials.map((row) => (
                      <p key={row.artifact_id}>
                        {row.filename} ·{" "}
                        <OperationCounts operations={row.operations} />
                        {row.issues.length
                          ? ` · ${row.issues.length} 项需处理`
                          : ""}
                      </p>
                    ))}
                    {!!imported.skipped_count && (
                      <p>跳过 {imported.skipped_count} 份资料</p>
                    )}
                    <details>
                      <summary>查看导入报告</summary>
                      <pre className="json-block">
                        {JSON.stringify(imported, null, 2)}
                      </pre>
                    </details>
                    <div className="row right">
                      <Button onClick={() => changeKind("prototype")}>
                        继续导入
                      </Button>
                      {imported.first_page_key && navigateReadEntity && (
                        <Button
                          kind="primary"
                          disabled={busy}
                          onClick={() => void openImportedPage()}
                        >
                          打开首个页面
                        </Button>
                      )}
                    </div>
                  </div>
                )}
              </>
            )}
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
            action={
              <Button
                kind="subtle"
                disabled={busy}
                onClick={() =>
                  void refresh().catch((e: Error) => setError(e.message))
                }
              >
                刷新任务
              </Button>
            }
          >
            <div className="job-list">
              {w.jobs.map((job) => (
                <JobCard key={job.id} job={job} onOpen={resume} />
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

function JobCard({ job, onOpen }: { job: Job; onOpen?: (job: Job) => void }) {
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
              {job.kind === "import_preview"
                ? "资料预检"
                : job.kind === "import"
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
        <span>
          {job.issue_count === undefined ? "" : `${job.issue_count} 项问题`}
        </span>
        <div className="row">
          {onOpen && Array.isArray(job.result?.materials) && (
            <Button kind="subtle" onClick={() => onOpen(job)}>
              {job.kind === "import_preview" ? "打开预检" : "打开导入结果"}
            </Button>
          )}
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
          {canOperate &&
            ["failed", "cancelled"].includes(job.state) &&
            job.error?.code !== "VKS_VERSION_CONFLICT" && (
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
