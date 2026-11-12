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
} from "lucide-react";
import { api, mutation, sha256, date } from "../../web/src/api";

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
  jobs: {
    id: string;
    state: string;
    progress: number;
    stage: string;
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
type Result = {
  analysis_set_id: string;
  selection_version: number;
  checkpoint_id?: string;
  migration?: Record<string, number>;
  job_id?: string;
};
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
  const [data, setData] = useState<Library | null>(null),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [kind, setKind] = useState("prototype"),
    [name, setName] = useState(""),
    [target, setTarget] = useState(""),
    [entry, setEntry] = useState("");
  const [files, setFiles] = useState<File[]>([]),
    [staged, setStaged] = useState<PackageVersion[]>([]),
    [replaceAll, setReplaceAll] = useState(false);
  const [busy, setBusy] = useState(""),
    [snapshotName, setSnapshotName] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const load = useCallback(async () => {
    const next = await api<Library>(`${prefix}?analysis_set_id=${analysis}`);
    setData(next);
    return next;
  }, [prefix, analysis]);
  useEffect(() => {
    let live = true;
    void load().catch((e) => {
      if (live) setError(e.message);
    });
    return () => {
      live = false;
    };
  }, [load, selectionVersion]);
  useEffect(() => {
    if (
      !data?.jobs.some((j) =>
        ["queued", "running", "retry_wait"].includes(j.state),
      )
    )
      return;
    const t = setInterval(
      () => void load().catch((e) => setError(e.message)),
      2500,
    );
    return () => clearInterval(t);
  }, [load, data]);
  const jobStates = useRef<Record<string, string>>({});
  useEffect(() => {
    for (const j of data?.jobs || []) {
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
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }
  async function prepare() {
    await work("正在解析资料并准备版本…", async () => {
      const current = await load(),
        ids = [];
      for (const file of files) {
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
      setStaged((old) => [
        ...old.filter((p) => p.package_id !== row.package_id),
        row,
      ]);
      setFiles([]);
      setName("");
      setTarget("");
      setEntry("");
      if (fileInput.current) fileInput.current.value = "";
      setNotice(
        "新版本已准备，可继续添加另一份资料后成套启用。当前页面仍使用原有资料。",
      );
      await load();
    });
  }
  async function activate(
    revisions: string[],
    remove: string[] = [],
    primary?: string,
    entire = false,
  ) {
    await work("正在切换资料并迁移关联…", async () => {
      const current = await load();
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
      await onChanged(result);
      setStaged([]);
      setReplaceAll(false);
      await load();
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
      await load();
      setNotice(
        "原型、文档及知识关联已保存为完整版本，当前工作区可以继续编辑。",
      );
    });
  }
  async function restore(id: string) {
    await work("正在恢复版本…", async () => {
      const current = await load();
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
      {error && (
        <p className="banner error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="banner success" role="status">
          <CheckCircle2 size={17} />
          {notice}
        </p>
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
                    onChange={(e) => {
                      setKind(e.target.value);
                      setTarget("");
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
                    value={target}
                    onChange={(e) => {
                      setTarget(e.target.value);
                      const p = data?.packages.find(
                        (p) => p.id === e.target.value,
                      );
                      if (p) setName(p.name);
                    }}
                  >
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
                  onChange={(e) => setName(e.target.value)}
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
                    if (!name && picked[0])
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
                  !!busy || !files.length || !name.trim() || !data?.editable
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
                </div>
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
            <div className="material-history">
              {data?.versions.map((v) => (
                <article key={v.id}>
                  <div>
                    <b>{v.name}</b>
                    <small>
                      {v.version} · {date(v.created_at)}
                    </small>
                  </div>
                  <button
                    disabled={!!busy || !data.editable}
                    onClick={() => void restore(v.id)}
                  >
                    恢复
                  </button>
                </article>
              ))}
            </div>
            {data && !data.versions.length && (
              <p className="management-empty">
                保存第一个完整版本，或在替换资料时自动保存。
              </p>
            )}
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
                disabled={!!busy || !data?.editable}
                onClick={() =>
                  void work("正在安排关联迁移…", async () => {
                    const c = await load();
                    await api(
                      `${prefix}/migrate`,
                      mutation(
                        "POST",
                        { analysis_set_id: analysis },
                        c.selection_version,
                      ),
                    );
                    await load();
                  })
                }
              >
                <RefreshCw size={15} />
                重试待关联
              </button>
              <button onClick={onAI}>
                模型与向量库
                <ArrowUpRight size={15} />
              </button>
            </div>
            {data?.jobs.map((j) => (
              <article className="material-job" key={j.id}>
                <div>
                  <b>
                    {["queued", "running"].includes(j.state)
                      ? "正在迁移"
                      : j.state === "succeeded"
                        ? "迁移分析完成"
                        : j.state === "failed"
                          ? "迁移暂未完成"
                          : "迁移任务"}
                  </b>
                  <span>{j.progress}%</span>
                </div>
                <progress value={j.progress} max={100} />
                {j.result && (
                  <p>
                    已关联 {j.result.saved || 0} / {j.result.total || 0}
                    {j.result.pending ? ` · ${j.result.pending} 项待关联` : ""}
                  </p>
                )}
                {j.error && <p className="error">{j.error.message}</p>}
                {!!j.result?.issues?.length && (
                  <details>
                    <summary>
                      暂未定位的位置（{j.result.issues.length}）
                    </summary>
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
            ))}
            {data && !data.jobs.length && (
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
