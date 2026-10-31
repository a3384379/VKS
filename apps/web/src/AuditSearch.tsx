import "./configuration.css";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { api, date } from "./api";
import { Button, Empty, Notice, Panel, Spinner, Tag } from "./components";
import { usePermission, useWorkspace } from "./context";

interface AuditRow {
  id: string;
  actor_id: string;
  action: string;
  resource_id: string;
  outcome: string;
  created_at: string;
}
export interface AuditFilters {
  actor_id: string;
  action: string;
  resource_id: string;
  outcome: string;
  since: string;
  until: string;
  limit: number;
}
const initial: AuditFilters = {
  actor_id: "",
  action: "",
  resource_id: "",
  outcome: "",
  since: "",
  until: "",
  limit: 100,
};
export function auditQuery(filters: AuditFilters, cursor = "") {
  const query = new URLSearchParams({ limit: String(filters.limit) });
  for (const field of ["actor_id", "action", "resource_id", "outcome"] as const)
    if (filters[field].trim()) query.set(field, filters[field].trim());
  for (const field of ["since", "until"] as const)
    if (filters[field]) {
      const parsed = new Date(filters[field]);
      if (!Number.isFinite(parsed.getTime()))
        throw new Error("时间范围格式无效");
      query.set(field, parsed.toISOString());
    }
  if (
    filters.since &&
    filters.until &&
    new Date(filters.since) > new Date(filters.until)
  )
    throw new Error("开始时间应早于结束时间");
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}
export function AuditSearch() {
  const { w } = useWorkspace();
  const canRead = usePermission("audit:read");
  const [draft, setDraft] = useState<AuditFilters>(initial),
    [applied, setApplied] = useState<AuditFilters>(initial),
    [rows, setRows] = useState<AuditRow[]>([]),
    [cursors, setCursors] = useState<string[]>([]),
    [cursor, setCursor] = useState(""),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const load = useCallback(
    async (filters: AuditFilters, next = "") => {
      if (!canRead) return false;
      setLoading(true);
      setError("");
      try {
        setRows(
          await api<AuditRow[]>(
            "/projects/" +
              w.project.id +
              "/audit-events?" +
              auditQuery(filters, next),
          ),
        );
        return true;
      } catch (e) {
        setError((e as Error).message);
        setRows([]);
        return false;
      } finally {
        setLoading(false);
      }
    },
    [w.project.id, canRead],
  );
  useEffect(() => {
    setDraft(initial);
    setApplied(initial);
    setCursor("");
    setCursors([]);
    void load(initial);
  }, [load]);
  async function search(e: FormEvent) {
    e.preventDefault();
    if (await load(draft)) {
      setApplied({ ...draft });
      setCursor("");
      setCursors([]);
    }
  }
  async function next() {
    const last = rows.at(-1)?.id;
    if (!last) return;
    if (await load(applied, last)) {
      setCursors((previous) => [...previous, cursor]);
      setCursor(last);
    }
  }
  async function previous() {
    const prior = cursors.at(-1);
    if (prior === undefined) return;
    if (await load(applied, prior)) {
      setCursors((values) => values.slice(0, -1));
      setCursor(prior);
    }
  }
  if (!canRead) return <Notice>当前账户没有项目审计读取权限。</Notice>;
  return (
    <Panel
      className="audit-search-panel"
      title="审计检索"
      action={<Tag>服务器权限过滤</Tag>}
    >
      <form
        onSubmit={(e) => {
          void search(e);
        }}
      >
        <div className="configuration-fields">
          <label>
            操作者 ID
            <input
              value={draft.actor_id}
              onChange={(e) => setDraft({ ...draft, actor_id: e.target.value })}
              placeholder="全部操作者"
            />
          </label>
          <label>
            操作名称
            <input
              value={draft.action}
              onChange={(e) => setDraft({ ...draft, action: e.target.value })}
              placeholder="例如 relation.correct"
            />
          </label>
          <label>
            对象 ID
            <input
              value={draft.resource_id}
              onChange={(e) =>
                setDraft({ ...draft, resource_id: e.target.value })
              }
              placeholder="实体、关系、任务或凭证的 ID"
            />
          </label>
          <label>
            操作结果
            <select
              value={draft.outcome}
              onChange={(e) => setDraft({ ...draft, outcome: e.target.value })}
            >
              <option value="">全部结果</option>
              <option value="succeeded">成功</option>
              <option value="denied">拒绝</option>
              <option value="failed">失败</option>
            </select>
          </label>
          <label>
            开始时间（本机时区）
            <input
              type="datetime-local"
              value={draft.since}
              onChange={(e) => setDraft({ ...draft, since: e.target.value })}
            />
          </label>
          <label>
            结束时间（本机时区）
            <input
              type="datetime-local"
              value={draft.until}
              onChange={(e) => setDraft({ ...draft, until: e.target.value })}
            />
          </label>
        </div>
        <div className="audit-search-actions">
          <label>
            每页
            <select
              value={draft.limit}
              onChange={(e) =>
                setDraft({ ...draft, limit: Number(e.target.value) })
              }
            >
              {[50, 100, 200, 500].map((limit) => (
                <option key={limit} value={limit}>
                  {limit} 条
                </option>
              ))}
            </select>
          </label>
          <Button kind="primary" type="submit" disabled={loading}>
            <Search size={15} />
            搜索审计记录
          </Button>
        </div>
      </form>
      {error && <Notice tone="red">{error}</Notice>}
      {loading ? (
        <Spinner />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>操作</th>
                <th>操作者</th>
                <th>对象</th>
                <th>结果</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{date(row.created_at)}</td>
                  <td>{row.action}</td>
                  <td>
                    <code title={row.actor_id}>{row.actor_id}</code>
                  </td>
                  <td>
                    <code title={row.resource_id}>
                      {row.resource_id || "—"}
                    </code>
                  </td>
                  <td>
                    <Tag
                      value={
                        row.outcome === "succeeded"
                          ? "valid"
                          : row.outcome === "failed"
                            ? "rejected"
                            : "static"
                      }
                    >
                      {row.outcome === "succeeded"
                        ? "成功"
                        : row.outcome === "denied"
                          ? "拒绝"
                          : row.outcome === "failed"
                            ? "失败"
                            : row.outcome}
                    </Tag>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!rows.length && (
            <Empty
              title={cursor ? "此范围内没有更多记录" : "当前条件没有审计记录"}
              description="可调整操作者、操作、对象或时间范围重新检索。"
            />
          )}
        </div>
      )}
      <div className="audit-pagination">
        <span>
          第 {cursors.length + 1} 页 · 当前 {rows.length} 条
        </span>
        <div className="row">
          <Button
            kind="small"
            disabled={loading || !cursors.length}
            onClick={() => {
              void previous();
            }}
          >
            <ChevronLeft size={14} />
            上一页
          </Button>
          <Button
            kind="small"
            disabled={loading || rows.length < applied.limit}
            onClick={() => {
              void next();
            }}
          >
            下一页
            <ChevronRight size={14} />
          </Button>
        </div>
      </div>
    </Panel>
  );
}
