import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useState } from "react";
import { api, ApiError, mutation } from "./api";
import { Button, Empty, Notice, Tag } from "./components";
import { usePermission, useReadScope, useWorkspace } from "./context";
import type { Dict, View } from "./types";

type Kind = "relation" | "journey" | "migration" | "document";
type Decision = "approve" | "reject" | "submit" | "reopen";
type Item = {
  kind: Kind;
  subject_id: string;
  title: string;
  status: string;
  revision_id: string | null;
  version: string;
  review_token: string;
  actions: Decision[];
  batch_actions: Decision[];
  data: Dict;
};
type Inbox = {
  selection_version: number;
  scope: string;
  items: Item[];
  total_visible: number;
  counts: Record<Kind, number>;
  offset: number;
  limit: number;
  next_offset: number | null;
};
const names: Record<Kind, string> = {
  relation: "关联候选",
  journey: "业务流程",
  migration: "迁移提案",
  document: "文档修订",
};
const labels: Record<Decision, string> = {
  approve: "批准 / 接受",
  reject: "拒绝 / 请求修改",
  submit: "提交文档审核",
  reopen: "重新打开关联审核",
};
const statusNames: Record<string, string> = {
  pending: "待审核",
  unsubmitted: "待提交",
  rejected: "已拒绝 / 待修改",
  approved: "已批准 / 接受",
};
const identity = (row: Item) => `${row.kind}:${row.subject_id}`;

export function ReviewInbox() {
  const { w, session, navigate, refresh, notify } = useWorkspace();
  const canReview = usePermission("links:review");
  const readScope = useReadScope();
  const [kind, setKind] = useState("all"),
    [status, setStatus] = useState("pending"),
    [offset, setOffset] = useState(0),
    [reload, setReload] = useState(0),
    [result, setResult] = useState<{ scope: string; value: Inbox } | null>(
      null,
    ),
    [selected, setSelected] = useState<string[]>([]),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false);
  const inbox = result?.scope === readScope ? result.value : null;
  const aid = w.selected_analysis_set?.id;
  useEffect(() => {
    setResult(null);
    setSelected([]);
    setError("");
    if (!canReview || !aid || w.selected_release) return;
    const controller = new AbortController();
    const query = new URLSearchParams({
      analysis_set_id: aid,
      kind,
      status,
      offset: String(offset),
      limit: "25",
    });
    setLoading(true);
    void api<Inbox>(`/api/v2/projects/${w.project.id}/review-items?${query}`, {
      signal: controller.signal,
    })
      .then((value) => setResult({ scope: readScope, value }))
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [w.project.id, aid, canReview, readScope, kind, status, offset, reload]);
  if (!canReview || w.selected_release) return null;
  const items = inbox?.items || [];
  const chosen = items.filter((row) => selected.includes(identity(row)));
  const overlapping = chosen.some(
    (row) =>
      row.kind === "migration" &&
      selected.includes(`relation:${row.data.target_relation_id}`),
  );
  const canApply = (decision: Decision) =>
    !!chosen.length &&
    !overlapping &&
    !busy &&
    !loading &&
    !!session.human &&
    !!reason.trim() &&
    chosen.every((row) =>
      (chosen.length > 1 ? row.batch_actions : row.actions).includes(decision),
    );
  async function apply(decision: Decision) {
    if (!inbox || !aid || !canApply(decision)) return;
    setBusy(true);
    setError("");
    try {
      const response = await api<{ count: number }>(
        `/api/v2/projects/${w.project.id}/review-batches`,
        mutation(
          "POST",
          {
            analysis_set_id: aid,
            reason: reason.trim(),
            items: chosen.map((row) => ({
              kind: row.kind,
              subject_id: row.subject_id,
              review_token: row.review_token,
              decision,
            })),
          },
          inbox.selection_version,
        ),
      );
      setSelected([]);
      setReload((value) => value + 1);
      await refresh();
      notify(`已原子处理 ${response.count} 项，每项已保存独立人员审核记录`);
    } catch (e) {
      if (e instanceof ApiError && [409, 412].includes(e.status)) {
        setError(`${e.message}。本批未提交，请刷新后重新核对并选择。`);
      } else setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function open(row: Item) {
    const entity = w.entities.find((entry) => entry.id === row.subject_id);
    const view: View =
      row.kind === "journey"
        ? "journeys"
        : row.kind === "document"
          ? "documents"
          : row.kind === "relation"
            ? "reviews"
            : "versions";
    navigate(view, entity);
  }
  return (
    <Panel
      title="审核收件箱"
      className="review-v2-inbox"
      action={<Tag>当前可见草稿 · {inbox?.total_visible ?? "—"} 项</Tag>}
    >
      {error && <Notice tone="red">{error}</Notice>}
      <p className="muted">
        每次提交绑定准确修订与人员决定；任一项冲突时整批不提交。批准关联不改变有效性或运行因果。
      </p>
      {overlapping && (
        <Notice>
          所选迁移提案与目标关联重叠，请分开核对，避免同一关联在一批中收到重复决定。
        </Notice>
      )}
      <div className="review-v2-toolbar">
        <label>
          对象类型
          <select
            aria-label="审核对象类型"
            value={kind}
            disabled={busy}
            onChange={(e) => {
              setKind(e.target.value);
              setOffset(0);
            }}
          >
            <option value="all">全部类型</option>
            {Object.entries(names).map(([value, name]) => (
              <option key={value} value={value}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          审核状态
          <select
            aria-label="收件箱审核状态"
            value={status}
            disabled={busy}
            onChange={(e) => {
              setStatus(e.target.value);
              setOffset(0);
            }}
          >
            <option value="pending">待处理（含待提交文档）</option>
            <option value="rejected">已拒绝 / 待修改</option>
            <option value="approved">已批准 / 接受</option>
            <option value="all">全部可见状态</option>
          </select>
        </label>
        <Button disabled={busy} onClick={() => setReload((value) => value + 1)}>
          刷新审核对象
        </Button>
      </div>
      {inbox && (
        <p className="muted">
          {Object.entries(names)
            .map(([key, name]) => `${name} ${inbox.counts[key as Kind]}`)
            .join(" · ")}
          {" · 草稿选择版本 "}
          {inbox.selection_version}
        </p>
      )}
      <label className="review-v2-reason">
        人员审核原因
        <textarea
          aria-label="收件箱审核原因"
          value={reason}
          maxLength={2000}
          disabled={busy}
          onChange={(e) => setReason(e.target.value)}
          placeholder="记录核对依据和需要修改的内容"
        />
      </label>
      <div className="row wrap">
        <label>
          <input
            type="checkbox"
            aria-label="选择本页可审核对象"
            disabled={
              busy || loading || !items.some((row) => row.actions.length)
            }
            checked={
              !!selected.length &&
              items
                .filter((row) => row.actions.length)
                .every((row) => selected.includes(identity(row)))
            }
            onChange={(e) =>
              setSelected(
                e.target.checked
                  ? items.filter((row) => row.actions.length).map(identity)
                  : [],
              )
            }
          />
          选择本页 · 已选 {chosen.length} 项
        </label>
        {(Object.keys(labels) as Decision[]).map((decision) => (
          <Button
            key={decision}
            disabled={!canApply(decision)}
            onClick={() => void apply(decision)}
          >
            {labels[decision]}
          </Button>
        ))}
      </div>
      <p className="muted">
        业务解释、运行因果或失效证据的批准需要逐条核对；未提交的文档须先提交审核。拒绝决定可在状态筛选中查看。
      </p>
      {loading && <Notice>正在读取当前有权审核的对象…</Notice>}
      {!loading && !items.length && (
        <Empty
          title="当前范围没有审核对象"
          description="可调整类型与状态；无权对象不计入数量。"
        />
      )}
      <div className="review-v2-list">
        {items.map((row) => (
          <article
            className="review-v2-item"
            key={identity(row)}
            data-review-key={identity(row)}
          >
            <div className="review-v2-heading">
              <input
                type="checkbox"
                aria-label={`选择审核项 ${row.title}`}
                disabled={busy || !row.actions.length}
                checked={selected.includes(identity(row))}
                onChange={(e) =>
                  setSelected((rows) =>
                    e.target.checked
                      ? [...rows, identity(row)]
                      : rows.filter((id) => id !== identity(row)),
                  )
                }
              />
              <strong>{row.title}</strong>
              <Tag>{names[row.kind]}</Tag>
              <Tag>{statusNames[row.status] || row.status}</Tag>
            </div>
            <p className="muted">
              修订 {row.revision_id?.slice(0, 12) || "关联行"} · 版本{" "}
              {row.version}
            </p>
            {row.kind === "relation" && (
              <p className="muted">
                审核 {String(row.data.review_status)} · 有效性{" "}
                {String(row.data.validity_status)} · 因果{" "}
                {String(row.data.causal_basis)}
                {!row.batch_actions.includes("approve") &&
                  " · 批准需单独选择此条"}
              </p>
            )}
            <div className="row wrap">
              <Button disabled={busy} onClick={() => open(row)}>
                打开{names[row.kind]}
              </Button>
              <details>
                <summary>核对修订、证据与变更依据</summary>
                <pre className="v2-json">
                  {JSON.stringify(row.data, null, 2)}
                </pre>
              </details>
            </div>
          </article>
        ))}
      </div>
      {inbox && (
        <div className="row wrap">
          <Button
            disabled={busy || loading || offset === 0}
            onClick={() => setOffset(Math.max(0, offset - inbox.limit))}
          >
            审核上一页
          </Button>
          <span>
            显示 {items.length ? offset + 1 : 0}–{offset + items.length} /{" "}
            {inbox.total_visible} 项当前可见对象
          </span>
          <Button
            disabled={busy || loading || inbox.next_offset === null}
            onClick={() => setOffset(inbox.next_offset!)}
          >
            审核下一页
          </Button>
        </div>
      )}
    </Panel>
  );
}
