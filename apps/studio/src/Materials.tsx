import { useCallback, useEffect, useRef, useState } from "react";
import {
  Package,
  FileText,
  UploadCloud,
  History,
  RefreshCw,
  CheckCircle2,
  ArrowUpRight,
  Layers,
  X,
  LoaderCircle,
  Download,
  Trash2,
  RotateCcw,
} from "lucide-react";
import { api, mutation, sha256, date } from "../../web/src/api";
import { useTransientNotice } from "./useTransientNotice";

type PackageVersion = {
  package_id: string;
  revision_id: string;
  name: string;
  kind: string;
  format: string;
  created_at: string;
  documents: number;
  sections: number;
  target_count: number;
  warnings: string[];
  changes?: Record<string, number>;
  unchanged?: boolean;
};
type Material = {
  id: string;
  name: string;
  kind: string;
  active_revision_id: string | null;
  versions: PackageVersion[];
};
type Library = {
  analysis_set_id: string;
  selection_version: number;
  editable: boolean;
  primary_package_id: string | null;
  packages: Material[];
  versions: { id: string; name: string; version: string; created_at: string }[];
  trash: {
    id: string;
    name: string;
    kind: "package" | "version";
    deleted_at: string;
  }[];
  jobs: {
    id: string;
    state: string;
    progress: number;
    stage: string;
    created_at?: string;
    current_scope?: boolean;
    error?: { message: string };
    result?: {
      total?: number;
      saved?: number;
      pending?: number;
      issues?: { label: string; message: string }[];
      completed?: Record<
        string,
        { label: string; saved: boolean; message: string }
      >;
    };
  }[];
};
type HistoryData = Pick<Library, "versions" | "trash">;
type Result = {
  analysis_set_id: string;
  selection_version: number;
  checkpoint_id?: string;
  migration?: Record<string, number>;
  job_id?: string;
};
const migrating = (j: Library["jobs"][number]) =>
  ["queued", "running", "retry_wait", "cancelling"].includes(j.state);
function migrationLabel(j: Library["jobs"][number]) {
  if (j.state === "queued") return "排队等待迁移";
  if (j.state === "retry_wait") return "等待自动重试";
  if (j.state === "cancelling") return "正在停止迁移";
  if (j.state === "cancelled") return "迁移已停止";
  if (j.state === "succeeded") return "迁移分析完成";
  if (j.state === "failed") return "迁移暂未完成";
  if (j.stage === "DOCUMENT_INDEX") return "正在建立向量索引";
  if (j.stage === "ASSOCIATE") return "正在核对关联";
  return "正在准备迁移";
}
function MigrationRecord({ j }: { j: Library["jobs"][number] }) {
  return (
    <article
      className="material-job"
      data-current-scope={j.current_scope !== false}
    >
      <div>
        <b>{migrationLabel(j)}</b>
        <span>{j.progress}%</span>
      </div>
      {j.created_at && (
        <small>
          {date(j.created_at)} ·{" "}
          {j.current_scope === false ? "旧资料版本" : "当前资料"}
        </small>
      )}
      <progress value={j.progress} max={100} />
      {j.result && (
        <p>
          {j.result.total !== undefined &&
            `已处理 ${Object.keys(j.result.completed || {}).length} / ${j.result.total} · `}
          已关联 {j.result.saved || 0}
          {j.result.pending ? ` · ${j.result.pending} 项待关联` : ""}
        </p>
      )}
      {j.error && <p className="error">{j.error.message}</p>}
      {!!j.result?.issues?.length && (
        <details>
          <summary>暂未定位的位置（{j.result.issues.length}）</summary>
          {j.result.issues.map((r, i) => (
            <p key={i}>
              <b>{r.label}</b> · {r.message}
            </p>
          ))}
        </details>
      )}
      {j.result?.completed && (
        <details>
          <summary>查看逐项结果</summary>
          {Object.entries(j.result.completed).map(([id, r]) => (
            <p key={id}>
              <b>{r.label}</b> · {r.message}
            </p>
          ))}
        </details>
      )}
    </article>
  );
}
export function Materials({
  project,
  analysis,
  onChanged,
  onAI,
  selectionVersion,
}: {
  project: string;
  analysis: string;
  onChanged: (r: Result) => Promise<void>;
  onAI: () => void;
  selectionVersion?: number;
}) {
  const prefix = `/api/v3/projects/${project}/materials`;
  const [notice, setNotice] = useTransientNotice();
  const [data, setData] = useState<Library | null>(null),
    [error, setError] = useState("");
  const [kind, setKind] = useState("prototype"),
    [name, setName] = useState(""),
    [target, setTarget] = useState("choose"),
    [entry, setEntry] = useState("");
  const [files, setFiles] = useState<File[]>([]),
    [staged, setStaged] = useState<PackageVersion[]>([]),
    [replaceAll, setReplaceAll] = useState(false);
  const [busy, setBusy] = useState(""),
    [snapshotName, setSnapshotName] = useState("");
  const [exportDocumentMode, setExportDocumentMode] = useState("project");
  const fileInput = useRef<HTMLInputElement>(null);
  const customName = useRef(false);
  const mounted = useRef(true),
    working = useRef(false);
  const pending = useRef<{
    controller: AbortController;
    promise: Promise<Library>;
  } | null>(null);
  const historyAbort = useRef<AbortController | null>(null);
  const [history, setHistory] = useState<HistoryData | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [updateSuggestion, setUpdateSuggestion] = useState<{
    package_id: string;
    name: string;
  } | null>(null);
  const [exportJob, setExportJob] = useState<{
    id: string;
    state: string;
    progress?: number;
    error?: { message: string };
    result?: { download_url?: string; filename?: string };
  } | null>(null);
  useEffect(() => {
    if (
      !exportJob ||
      !["queued", "running", "retry_wait"].includes(exportJob.state)
    )
      return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      void api<typeof exportJob>(`/jobs/${exportJob.id}?project_id=${project}`)
        .then((next) => {
          if (live) {
            setExportJob(next);
            if (next?.state === "succeeded")
              setNotice("成品库已生成，可下载只读 HTML。文件不携带 API Key。");
            else if (next && ["failed", "cancelled"].includes(next.state))
              setNotice("");
          }
        })
        .catch((e) => {
          if (live) setError(e.message);
        })
        .finally(() => {
          if (live) timer = setTimeout(poll, 1800);
        });
    };
    timer = setTimeout(poll, 1800);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [exportJob, project]);
  useEffect(() => {
    setExportJob(null);
  }, [project]);
  const load = useCallback(
    (fresh = false) => {
      if (pending.current && !fresh) return pending.current.promise;
      pending.current?.controller.abort();
      const controller = new AbortController();
      const promise = api<Library>(
        `${prefix}?analysis_set_id=${analysis}&include_versions=false`,
        { signal: controller.signal },
      )
        .then((next) => {
          if (mounted.current && !controller.signal.aborted) setData(next);
          return next;
        })
        .finally(() => {
          if (pending.current?.controller === controller)
            pending.current = null;
        });
      pending.current = { controller, promise };
      return promise;
    },
    [prefix, analysis],
  );
  const loadHistory = useCallback(async () => {
    historyAbort.current?.abort();
    const controller = new AbortController();
    historyAbort.current = controller;
    setHistoryError("");
    try {
      const next = await api<HistoryData>(
        `${prefix}/versions?analysis_set_id=${analysis}`,
        { signal: controller.signal },
      );
      if (mounted.current && !controller.signal.aborted) setHistory(next);
    } catch (e) {
      if (mounted.current && !controller.signal.aborted)
        setHistoryError((e as Error).message);
    }
  }, [prefix, analysis]);
  useEffect(() => {
    mounted.current = true;
    void load().catch((e) => {
      if (mounted.current && e.name !== "AbortError") setError(e.message);
    });
    void loadHistory();
    return () => {
      mounted.current = false;
      pending.current?.controller.abort();
      pending.current = null;
      historyAbort.current?.abort();
    };
  }, [load, loadHistory, selectionVersion]);
  useEffect(() => {
    if (
      busy ||
      !data?.jobs.some((j) => j.current_scope !== false && migrating(j))
    )
      return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        await load();
      } catch (e) {
        if (live && (e as Error).name !== "AbortError")
          setError((e as Error).message);
      }
      if (live) timer = setTimeout(poll, 2500);
    };
    timer = setTimeout(poll, 2500);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [load, data, busy]);
  const jobStates = useRef<Record<string, string>>({});
  useEffect(() => {
    for (const j of (data?.jobs || []).filter(
      (j) => j.current_scope !== false,
    )) {
      if (
        ["queued", "running", "retry_wait"].includes(jobStates.current[j.id]) &&
        ["succeeded", "failed", "cancelled"].includes(j.state)
      )
        void onChanged({
          analysis_set_id: analysis,
          selection_version: data!.selection_version,
        });
      jobStates.current[j.id] = j.state;
    }
  }, [data, analysis, onChanged]);
  async function work(label: string, fn: () => Promise<void>) {
    if (working.current) return;
    working.current = true;
    setBusy(label);
    setError("");
    setUpdateSuggestion(null);
    setNotice("");
    try {
      await fn();
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
      const failure = e as {
        code?: string;
        details?: { package_id?: string; name?: string };
      };
      if (
        failure.code === "VKS_MATERIAL_UPDATE_REQUIRED" &&
        failure.details?.package_id &&
        failure.details?.name
      )
        setUpdateSuggestion({
          package_id: failure.details.package_id,
          name: failure.details.name,
        });
      if ((e as { code?: string }).code === "VKS_VERSION_CONFLICT")
        void load(true).catch(() => {});
    } finally {
      working.current = false;
      setBusy("");
    }
  }
  async function prepare() {
    await work("正在解析资料并准备版本…", async () => {
      if (!data || target === "choose") return;
      const current = data,
        ids = [];
      for (const file of files) {
        setBusy(`正在上传 ${file.name}…`);
        const pending = await api<{ artifact_id: string; upload_url: string }>(
          `/projects/${project}/upload-sessions`,
          mutation("POST", {
            filename: file.name,
            size_bytes: file.size,
            sha256: await sha256(file),
            media_type: file.type || "application/octet-stream",
          }),
        );
        await api(pending.upload_url, { method: "PUT", body: file });
        ids.push(pending.artifact_id);
      }
      setBusy("正在解析资料并准备版本…");
      const row = await api<PackageVersion>(
        `${prefix}/prepare`,
        mutation(
          "POST",
          {
            analysis_set_id: analysis,
            kind,
            name: name.trim(),
            artifact_ids: ids,
            package_id: target || null,
            entry,
          },
          current.selection_version,
        ),
      );
      if (!row.unchanged)
        setStaged((old) => [
          ...old.filter((p) => p.package_id !== row.package_id),
          row,
        ]);
      setFiles([]);
      setName("");
      customName.current = false;
      setTarget("choose");
      setEntry("");
      if (fileInput.current) fileInput.current.value = "";
      setNotice(
        row.unchanged
          ? "资料内容未变化，已保留当前版本，没有重复导入或重建知识库。"
          : "新版本已准备，可继续添加另一份资料后成套启用。当前页面仍使用原有资料。",
      );
      await load(true);
    });
  }
  async function activate(
    revisions: string[],
    remove: string[] = [],
    primary?: string,
    entire = false,
  ) {
    await work("正在切换资料并迁移关联…", async () => {
      if (!data) return;
      const current = data;
      const result = await api<Result>(
        `${prefix}/activate`,
        mutation(
          "POST",
          {
            analysis_set_id: analysis,
            revision_ids: revisions,
            remove_package_ids: remove,
            replace_all: entire,
            primary_package_id: primary || null,
            auto_migrate: true,
            prepared:
              !!staged.length &&
              revisions.every((r) => staged.some((p) => p.revision_id === r)),
          },
          current.selection_version,
        ),
      );
      await Promise.all([onChanged(result), load(true)]);
      void loadHistory();
      setStaged([]);
      setReplaceAll(false);
      setNotice(
        `资料已启用${result.checkpoint_id ? "，替换前版本已自动保存" : ""}。${result.migration?.reassociate ? ` ${result.migration.reassociate} 项关联正在后台重新分析。` : ""}${result.migration?.unresolved ? ` ${result.migration.unresolved} 项旧位置暂未定位，可在迁移结果中查看。` : ""}`,
      );
    });
  }
  async function saveVersion() {
    await work("正在保存完整版本…", async () => {
      await api(
        `${prefix}/versions`,
        mutation("POST", {
          analysis_set_id: analysis,
          name: snapshotName.trim(),
        }),
      );
      setSnapshotName("");
      await loadHistory();
      setNotice(
        "原型、文档及知识关联已保存为完整版本，当前工作区可以继续编辑。",
      );
    });
  }
  async function exportVersion(releaseId?: string) {
    await work("正在准备只读成品库…", async () => {
      const session = await api<{
        human: boolean;
        scopes: string[];
        project_permissions: Record<string, string[]>;
      }>("/auth/session");
      const canReadSource =
        session.scopes.includes("source:read") &&
        (!session.human ||
          (session.project_permissions[project] || []).includes("source:read"));
      const fixed =
        releaseId ||
        (
          await api<{ id: string }>(
            `${prefix}/versions`,
            mutation("POST", {
              analysis_set_id: analysis,
              name: `成品库导出 · ${new Date().toLocaleString("zh-CN")}`,
            }),
          )
        ).id;
      setExportJob(
        await api(
          `/projects/${project}/exports`,
          mutation("POST", {
            release_id: fixed,
            format: "readonly_html",
            include_source: canReadSource,
            document_mode: exportDocumentMode,
          }),
        ),
      );
      void loadHistory();
      setNotice(
        "正在固定资料并补齐向量索引。完成后可下载单文件 HTML，文件不携带 API Key。",
      );
    });
  }
  async function restore(id: string) {
    await work("正在恢复版本…", async () => {
      if (!data) return;
      const current = data;
      const r = await api<Result>(
        `${prefix}/restore`,
        mutation(
          "POST",
          { analysis_set_id: analysis, release_id: id },
          current.selection_version,
        ),
      );
      await onChanged(r);
    });
  }
  async function trash(
    kind: "package" | "version",
    id: string,
    deleted = true,
  ) {
    await work(deleted ? "正在移入回收站…" : "正在恢复资料…", async () => {
      if (!data) return;
      await api(
        `${prefix}/trash`,
        mutation(
          "POST",
          { analysis_set_id: analysis, kind, target_id: id, deleted },
          data.selection_version,
        ),
      );
      await Promise.all([load(true), loadHistory()]);
      if (kind === "package" && deleted) {
        setStaged((old) => old.filter((p) => p.package_id !== id));
        if (target === id) {
          setTarget("choose");
          setName("");
        }
      }
      setNotice(
        deleted
          ? "已移入回收站，可随时恢复。已有完整版本和原始修订保持可追溯。"
          : "已从回收站恢复。资料包恢复后可选择版本启用。",
      );
    });
  }
  const recycleItems = [
    ...new Map(
      [...(data?.trash || []), ...(history?.trash || [])].map((item) => [
        item.kind + ":" + item.id,
        item,
      ]),
    ).values(),
  ];
  async function purge(items: Library["trash"]) {
    if (!data || !items.length) return;
    const label =
      items.length === 1
        ? `删除“${items[0].name}”`
        : `清空回收站中的 ${items.length} 项`;
    if (
      !window.confirm(
        `${label}？删除后无法从回收站恢复，原始修订与历史版本引用仍会保留。`,
      )
    )
      return;
    await work("正在删除回收站内容…", async () => {
      await api(
        `${prefix}/trash/purge`,
        mutation(
          "POST",
          {
            analysis_set_id: analysis,
            targets: items.map((item) => ({
              kind: item.kind,
              target_id: item.id,
            })),
          },
          data.selection_version,
        ),
      );
      await Promise.all([load(true), loadHistory()]);
      setNotice(
        `已删除 ${items.length} 项回收站记录。历史引用和原始修订保持不变。`,
      );
    });
  }
  const currentJobs = (data?.jobs || []).filter(
    (j) => j.current_scope !== false,
  );
  const activeJobs = currentJobs.filter(migrating);
  const currentJob =
    [...activeJobs].sort((a, b) => b.progress - a.progress)[0] ||
    currentJobs.find((j) => j.state !== "cancelled");
  const previousJobs = (data?.jobs || []).filter(
    (j) => j.id !== currentJob?.id,
  );
  const active = data?.packages.filter((p) => p.active_revision_id) || [];
  return (
    <section className="materials-page">
      <div className="management-heading">
        <div>
          <span className="eyebrow">项目专属资料空间</span>
          <h1>原型与文档</h1>
          <p>自由组合资料包。替换前自动保存版本，知识关联在后台迁移。</p>
        </div>
        <span className="management-count">
          <Layers size={16} />
          {data ? `${active.length} 个已启用资料包` : "正在读取项目资料…"}
        </span>
      </div>
      <section className="management-panel" aria-label="只读成品库导出">
        <div className="management-panel-title">
          <Download size={19} />
          <h2>导出成品库</h2>
        </div>
        <p className="management-description">
          将当前原型、文档、知识关联与向量索引打包为一个只读
          HTML。离线可阅读、检索和跳转；联网问答由使用者自行填写 API Key。请先在
          AI 设置中启用嵌入模型。
        </p>
        <label className="export-document-mode">
          导出文档呈现
          <select
            aria-label="导出文档呈现"
            value={exportDocumentMode}
            disabled={!!busy}
            onChange={(e) => setExportDocumentMode(e.target.value)}
          >
            <option value="project">跟随项目阅读方式</option>
            <option value="original">保留导入原文档</option>
          </select>
          <small>
            原样模式保留可兼容的样式与目录；正文与原件不同、权限不完整或无法保证只读时会明确回退。成品不含发布者密钥。
          </small>
        </label>
        <button
          disabled={
            !!busy ||
            !data?.editable ||
            (!!exportJob &&
              ["queued", "running", "retry_wait"].includes(exportJob.state))
          }
          onClick={() => void exportVersion()}
        >
          <Download size={16} /> 导出当前成品库
        </button>
        {exportJob && (
          <div role="status" className="management-description">
            {["queued", "running", "retry_wait"].includes(exportJob.state) ? (
              `正在准备与建库… ${exportJob.progress || 0}%`
            ) : exportJob.state === "succeeded" &&
              exportJob.result?.download_url ? (
              <a
                href={exportJob.result.download_url}
                download={exportJob.result.filename}
              >
                下载只读 HTML
              </a>
            ) : (
              exportJob.error?.message || "导出未完成，请重试"
            )}
          </div>
        )}
      </section>
      {error && (
        <p className="banner error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <div className="banner success" role="status">
          <CheckCircle2 size={17} />
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice("")}
            aria-label="关闭提示"
          >
            <X size={14} />
          </button>
        </div>
      )}
      {busy && (
        <p role="status" className="material-progress">
          <LoaderCircle className="spin" size={17} />
          {busy}
        </p>
      )}
      <div className="materials-columns">
        <div>
          <section className="management-panel">
            <div className="management-panel-title">
              <UploadCloud size={19} />
              <h2>导入资料包</h2>
              <a
                className="handbook-template-download"
                href="/api/v3/document-templates/handbook/download"
                download
                title="本地整理 Markdown 后保存 HTML，再作为知识文档导入"
              >
                <Download size={15} />
                下载手册模板（可选）
              </a>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void prepare();
              }}
              className="material-import-form"
            >
              <div className="material-form-row">
                <label>
                  资料类型
                  <select
                    aria-label="资料类型"
                    value={kind}
                    disabled={!!busy}
                    onChange={(e) => {
                      setKind(e.target.value);
                      setTarget("choose");
                      setName("");
                      customName.current = false;
                      setFiles([]);
                      setEntry("");
                      if (fileInput.current) fileInput.current.value = "";
                    }}
                  >
                    <option value="prototype">交互原型</option>
                    <option value="documents">知识文档</option>
                  </select>
                </label>
                <label>
                  导入方式
                  <select
                    aria-label="导入方式"
                    disabled={!!busy}
                    value={target}
                    onChange={(e) => {
                      setTarget(e.target.value);
                      const p = data?.packages.find(
                        (p) => p.id === e.target.value,
                      );
                      customName.current = false;
                      setName(
                        p?.name || files[0]?.name.replace(/\.[^.]+$/, "") || "",
                      );
                    }}
                  >
                    <option value="choose" disabled>
                      请选择新增或更新
                    </option>
                    <option value="">新增独立资料包</option>
                    {data?.packages
                      .filter((p) => p.kind === kind)
                      .map((p) => (
                        <option key={p.id} value={p.id}>
                          更新：{p.name}
                        </option>
                      ))}
                  </select>
                </label>
              </div>
              <p className="management-description">
                替换现有内容请选择“更新”。“新增”会创建独立资料包；相同资料会自动拦截，未变化的更新直接复用。
              </p>
              {updateSuggestion && (
                <button
                  type="button"
                  onClick={() => {
                    setTarget(updateSuggestion.package_id);
                    setName(updateSuggestion.name);
                    customName.current = false;
                    setUpdateSuggestion(null);
                    setError("");
                  }}
                >
                  改为更新：{updateSuggestion.name}
                </button>
              )}
              <label>
                资料包名称
                <input
                  required
                  value={name}
                  maxLength={160}
                  placeholder={
                    kind === "prototype"
                      ? "例如：物业收费原型"
                      : "例如：收费业务手册"
                  }
                  onChange={(e) => {
                    customName.current = true;
                    setName(e.target.value);
                  }}
                />
              </label>
              <label className="material-file-picker">
                <UploadCloud size={26} />
                <b>
                  {files.length
                    ? files.map((f) => f.name).join("、")
                    : "选择需要导入的文件"}
                </b>
                <small>
                  {kind === "prototype"
                    ? "HTML 原型或静态站 ZIP；保留原有页面交互"
                    : "Markdown、文本、HTML 手册或文档 ZIP，可同时选择多份文件"}
                </small>
                <input
                  ref={fileInput}
                  type="file"
                  aria-label="选择资料文件"
                  multiple={kind === "documents"}
                  accept={
                    kind === "prototype"
                      ? ".html,.htm,.zip"
                      : ".md,.markdown,.txt,.html,.htm,.zip"
                  }
                  onChange={(e) => {
                    const picked = Array.from(e.target.files || []);
                    setFiles(picked);
                    if (
                      !customName.current &&
                      (!target || target === "choose") &&
                      picked[0]
                    )
                      setName(picked[0].name.replace(/\.[^.]+$/, ""));
                  }}
                />
              </label>
              {kind === "prototype" && (
                <details>
                  <summary>指定静态站入口</summary>
                  <label>
                    入口路径
                    <input
                      placeholder="自动识别 index.html"
                      value={entry}
                      onChange={(e) => setEntry(e.target.value)}
                    />
                  </label>
                </details>
              )}
              <button
                className="primary"
                disabled={
                  !!busy ||
                  target === "choose" ||
                  !files.length ||
                  !name.trim() ||
                  !data?.editable
                }
              >
                <UploadCloud size={16} />
                解析并预览
              </button>
            </form>
            {!!staged.length && (
              <div className="material-staging">
                <h3>待启用的资料</h3>
                {staged.map((row) => (
                  <article key={row.revision_id}>
                    <div>
                      <b>{row.name}</b>
                      <small>
                        {row.kind === "prototype"
                          ? `${row.target_count} 个可识别位置`
                          : `${row.documents} 篇文档 · ${row.sections} 个章节`}
                      </small>
                      {row.changes && (
                        <small>
                          新增 {row.changes.added} · 更新 {row.changes.updated}{" "}
                          · 复用 {row.changes.reused} · 移除{" "}
                          {row.changes.removed}
                        </small>
                      )}
                      {row.warnings.map((w, i) => (
                        <small key={i}>{w}</small>
                      ))}
                    </div>
                    <button
                      className="icon"
                      aria-label={`取消待启用 ${row.name}`}
                      onClick={() =>
                        setStaged((old) =>
                          old.filter((p) => p.revision_id !== row.revision_id),
                        )
                      }
                    >
                      <X size={15} />
                    </button>
                  </article>
                ))}
                <label className="material-check">
                  <input
                    type="checkbox"
                    checked={replaceAll}
                    onChange={(e) => setReplaceAll(e.target.checked)}
                  />
                  整套替换当前资料，仅启用上面这些包
                </label>
                <button
                  className="primary"
                  disabled={!!busy}
                  onClick={() =>
                    void activate(
                      staged.map((r) => r.revision_id),
                      [],
                      replaceAll
                        ? staged.find((r) => r.kind === "prototype")?.package_id
                        : undefined,
                      replaceAll,
                    )
                  }
                >
                  启用这些资料
                  <ArrowUpRight size={16} />
                </button>
                <p>
                  可以先添加原型，再添加文档，最后一起启用。知识库和原型历史不会被删除。
                </p>
              </div>
            )}
          </section>
          <section className="management-panel">
            <div className="management-panel-title">
              <Package size={19} />
              <h2>资料包</h2>
            </div>
            {data && !data.packages.length && (
              <p className="management-empty">
                这个项目还没有资料，先导入一份原型或文档。
              </p>
            )}
            {data?.packages.map((p) => (
              <article className="material-card" key={p.id}>
                <div className="material-card-heading">
                  <span className="material-kind">
                    {p.kind === "prototype" ? (
                      <Package size={19} />
                    ) : (
                      <FileText size={19} />
                    )}
                  </span>
                  <div>
                    <h3>{p.name}</h3>
                    <small>
                      {p.kind === "prototype" ? "交互原型" : "知识文档"} ·{" "}
                      {p.versions.length} 个版本
                    </small>
                  </div>
                  <span
                    className={
                      "pill " + (p.active_revision_id ? "enabled" : "")
                    }
                  >
                    {p.active_revision_id ? "已启用" : "未启用"}
                  </span>
                </div>
                <div className="material-version-actions">
                  <select
                    aria-label={`选择资料包版本 ${p.name}`}
                    disabled={!!busy || !data.editable}
                    value={p.active_revision_id || ""}
                    onChange={(e) => {
                      if (e.target.value) void activate([e.target.value]);
                    }}
                  >
                    <option value="" disabled>
                      选择版本并启用
                    </option>
                    {p.versions.map((v, i) => (
                      <option value={v.revision_id} key={v.revision_id}>
                        {i === 0 ? "最新 · " : ""}
                        {date(v.created_at)} · {v.format}
                        {v.documents ? ` · ${v.documents} 篇文档` : ""}
                      </option>
                    ))}
                  </select>
                  {p.active_revision_id ? (
                    <button
                      disabled={!!busy || !data.editable}
                      onClick={() => void activate([], [p.id])}
                    >
                      停用
                    </button>
                  ) : (
                    <button
                      disabled={!!busy || !data.editable}
                      onClick={() => void activate([p.versions[0].revision_id])}
                    >
                      启用
                    </button>
                  )}
                  <button
                    disabled={!!busy || !data.editable}
                    onClick={() => {
                      setKind(p.kind);
                      setTarget(p.id);
                      setName(p.name);
                      fileInput.current?.focus();
                    }}
                  >
                    更新资料
                  </button>
                  <button
                    disabled={
                      !!busy || !data.editable || !!p.active_revision_id
                    }
                    title={
                      p.active_revision_id
                        ? "请先停用资料包，再删除；停用前会保存完整版本"
                        : "移入回收站"
                    }
                    onClick={() => void trash("package", p.id)}
                  >
                    <Trash2 size={15} /> 删除
                  </button>
                </div>
                {p.active_revision_id && (
                  <small className="management-description">
                    删除前请先停用，停用前会自动保存完整版本。
                  </small>
                )}
                {p.kind === "prototype" && p.active_revision_id && (
                  <button
                    className="text-button"
                    disabled={!!busy || data.primary_package_id === p.id}
                    onClick={() => void activate([], [], p.id)}
                  >
                    {data.primary_package_id === p.id
                      ? "当前原型"
                      : "设为当前原型"}
                  </button>
                )}
              </article>
            ))}
          </section>
        </div>
        <div>
          <section className="management-panel">
            <div className="management-panel-title">
              <History size={19} />
              <h2>完整版本与回退</h2>
            </div>
            <p className="management-description">
              同时保存原型、文档和知识关联。恢复后创建新的可编辑工作区。
            </p>
            <form
              className="material-snapshot-form"
              onSubmit={(e) => {
                e.preventDefault();
                void saveVersion();
              }}
            >
              <input
                aria-label="版本名称"
                required
                maxLength={160}
                placeholder="例如：收费方案确认版"
                value={snapshotName}
                onChange={(e) => setSnapshotName(e.target.value)}
              />
              <button
                disabled={!!busy || !snapshotName.trim() || !data?.editable}
              >
                保存版本
              </button>
            </form>
            <div
              className="material-history"
              role="region"
              aria-label="完整版本列表"
            >
              {history?.versions.map((v) => (
                <article key={v.id}>
                  <div>
                    <b>{v.name}</b>
                    <small>
                      {v.version} · {date(v.created_at)}
                    </small>
                  </div>
                  <button
                    disabled={
                      !!busy ||
                      (!!exportJob &&
                        ["queued", "running", "retry_wait"].includes(
                          exportJob.state,
                        ))
                    }
                    onClick={() => void exportVersion(v.id)}
                  >
                    导出 HTML
                  </button>
                  <button
                    disabled={!!busy || !data?.editable}
                    onClick={() => void restore(v.id)}
                  >
                    恢复
                  </button>
                  <button
                    disabled={!!busy || !data?.editable}
                    onClick={() => void trash("version", v.id)}
                    aria-label={`删除完整版本 ${v.name}`}
                  >
                    <Trash2 size={15} /> 删除
                  </button>
                </article>
              ))}
              {!history && !historyError && (
                <p className="management-description" role="status">
                  正在读取历史版本，可先管理资料包…
                </p>
              )}
              {historyError && (
                <p role="alert">
                  {historyError}{" "}
                  <button onClick={() => void loadHistory()}>
                    重新读取版本
                  </button>
                </p>
              )}
              {history && !history.versions.length && (
                <p className="management-empty">
                  保存第一个完整版本，或在替换资料时自动保存。
                </p>
              )}
            </div>
          </section>
          <section className="management-panel">
            <details>
              <summary>
                <Trash2 size={17} /> 回收站（
                {recycleItems.length}）
              </summary>
              <p className="management-description">
                可以恢复资料，或删除回收站记录。删除后无法在回收站恢复；历史引用与原始修订保留，不清理磁盘文件。
              </p>
              <div className="material-recycle-actions">
                <button
                  disabled={
                    !!busy ||
                    !data?.editable ||
                    !history ||
                    !recycleItems.length
                  }
                  onClick={() => void purge(recycleItems)}
                >
                  <Trash2 size={15} /> 清空回收站
                </button>
                {!recycleItems.length && history && <span>回收站为空</span>}
              </div>
              <div className="material-recycle-list">
                {recycleItems.map((item) => (
                  <article key={item.kind + item.id}>
                    <div>
                      <b>{item.name}</b>
                      <small>
                        {item.kind === "package" ? "资料包" : "完整版本"} ·{" "}
                        {date(item.deleted_at)}
                      </small>
                    </div>
                    <button
                      disabled={!!busy || !data?.editable}
                      onClick={() => void trash(item.kind, item.id, false)}
                      aria-label={`从回收站恢复 ${item.name}`}
                    >
                      <RotateCcw size={15} /> 恢复
                    </button>
                    <button
                      disabled={!!busy || !data?.editable}
                      onClick={() => void purge([item])}
                      aria-label={`删除回收站记录 ${item.name}`}
                    >
                      <Trash2 size={15} /> 删除
                    </button>
                  </article>
                ))}
              </div>
            </details>
          </section>
          <section className="management-panel">
            <div className="management-panel-title">
              <RefreshCw size={19} />
              <h2>AI 自动关联迁移</h2>
            </div>
            <p className="management-description">
              未变化的知识直接复用。变更的引用由模型重新关联，人工说明保留，已移除关联不恢复。
            </p>
            <div className="form-actions">
              <button
                disabled={!!busy || !data?.editable || !!activeJobs.length}
                onClick={() =>
                  void work("正在安排关联迁移…", async () => {
                    if (!data) return;
                    const c = data;
                    await api(
                      `${prefix}/migrate`,
                      mutation(
                        "POST",
                        { analysis_set_id: analysis },
                        c.selection_version,
                      ),
                    );
                    await load(true);
                  })
                }
              >
                <RefreshCw size={15} />
                {activeJobs.length ? "迁移进行中" : "重试待关联"}
              </button>
              <button onClick={onAI}>
                模型与向量库
                <ArrowUpRight size={15} />
              </button>
            </div>
            {currentJob && <MigrationRecord j={currentJob} />}
            {!!previousJobs.length && (
              <details className="material-job-history">
                <summary>迁移历史（{previousJobs.length}）</summary>
                {previousJobs.map((j) => (
                  <MigrationRecord key={j.id} j={j} />
                ))}
              </details>
            )}
            {data && !currentJob && (
              <p className="management-empty">
                资料更新后，受影响的关联将在这里显示进度。
              </p>
            )}
          </section>
        </div>
      </div>
    </section>
  );
}
