import { useCallback, useEffect, useState } from "react";
import {
  ArrowUpRight,
  FolderPlus,
  Search,
  Archive,
  Pencil,
  FolderOpen,
} from "lucide-react";
import { api, apiPage, mutation } from "../../web/src/api";

type Project = {
  id: string;
  key: string;
  name: string;
  description: string;
  status: string;
  row_version: number;
};
export function Projects({
  onOpen,
  onUpdate,
}: {
  onOpen: (id: string) => void;
  onUpdate: () => void;
}) {
  const [rows, setRows] = useState<Project[]>([]),
    [query, setQuery] = useState(""),
    [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null),
    [name, setName] = useState(""),
    [description, setDescription] = useState("");
  const load = useCallback(
    async (after = "") => {
      const p = await apiPage<Project[]>(
        `/projects?limit=30&q=${encodeURIComponent(query)}${after ? `&cursor=${after}` : ""}`,
      );
      setRows((old) => (after ? [...old, ...p.data] : p.data));
      setCursor(p.meta.next_cursor || null);
    },
    [query],
  );
  useEffect(() => {
    const t = setTimeout(
      () => void load().catch((e) => setError(e.message)),
      180,
    );
    return () => clearTimeout(t);
  }, [load]);
  async function save() {
    setBusy(true);
    setError("");
    try {
      const row = editing
        ? await api<Project>(
            `/projects/${editing.id}`,
            mutation("PATCH", { name, description }, editing.row_version),
          )
        : await api<Project>(
            "/projects",
            mutation("POST", {
              key: `project-${crypto.randomUUID()}`,
              name,
              description,
            }),
          );
      setCreating(false);
      setEditing(null);
      setName("");
      setDescription("");
      onUpdate();
      if (!editing) onOpen(row.id);
      else await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function archive(row: Project) {
    setBusy(true);
    setError("");
    try {
      await api(
        `/projects/${row.id}`,
        mutation(
          "PATCH",
          { status: row.status === "archived" ? "active" : "archived" },
          row.row_version,
        ),
      );
      await load();
      onUpdate();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="project-center">
      <div className="management-heading">
        <div>
          <span className="eyebrow">你的知识工作空间</span>
          <h1>项目中心</h1>
          <p>每个项目独立管理原型、文档、知识关联与 AI 连接。</p>
        </div>
        <button
          className="primary"
          onClick={() => {
            setCreating(true);
            setEditing(null);
            setName("");
            setDescription("");
          }}
        >
          <FolderPlus size={17} />
          新建项目
        </button>
      </div>
      {error && (
        <p role="alert" className="banner error">
          {error}
        </p>
      )}
      {(creating || editing) && (
        <form
          className="project-form"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <h2>{editing ? "编辑项目" : "创建项目"}</h2>
          <label>
            项目名称
            <input
              required
              maxLength={160}
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
            />
          </label>
          <label>
            项目说明
            <textarea
              maxLength={4000}
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <div className="form-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setCreating(false);
                setEditing(null);
              }}
            >
              取消
            </button>
            <button className="primary" disabled={busy || !name.trim()}>
              {busy ? "正在保存…" : editing ? "保存项目" : "创建并进入"}
            </button>
          </div>
        </form>
      )}
      <label className="management-search">
        <Search size={17} />
        <input
          aria-label="搜索项目"
          placeholder="搜索项目名称或标识"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      <div className="project-grid">
        {rows.map((row) => (
          <article
            className={
              "project-card " + (row.status === "archived" ? "archived" : "")
            }
            key={row.id}
          >
            <div className="project-card-title">
              <span className="project-emblem">
                <FolderOpen size={23} />
              </span>
              <span className="pill">
                {row.status === "archived" ? "已归档" : "使用中"}
              </span>
            </div>
            <h2>{row.name}</h2>
            <p>
              {row.description || "添加原型与文档，开始构建这个项目的知识。"}
            </p>
            <footer>
              <button className="primary" onClick={() => onOpen(row.id)}>
                打开项目
                <ArrowUpRight size={16} />
              </button>
              <div>
                <button
                  className="icon"
                  aria-label={`编辑项目 ${row.name}`}
                  onClick={() => {
                    setEditing(row);
                    setCreating(false);
                    setName(row.name);
                    setDescription(row.description);
                  }}
                >
                  <Pencil size={15} />
                </button>
                <button
                  className="icon"
                  disabled={busy}
                  aria-label={`${row.status === "archived" ? "恢复" : "归档"}项目 ${row.name}`}
                  onClick={() => void archive(row)}
                >
                  <Archive size={15} />
                </button>
              </div>
            </footer>
          </article>
        ))}
      </div>
      {!rows.length && !creating && (
        <p className="management-empty">
          {query
            ? "没有找到匹配的项目"
            : "创建第一个项目，然后导入它自己的原型与文档。"}
        </p>
      )}
      {cursor && (
        <button onClick={() => void load(cursor)}>加载更多项目</button>
      )}
    </section>
  );
}
