import "./configuration.css";
import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Send } from "lucide-react";
import { api, ApiError, date, mutation } from "./api";
import { Button, Modal, Notice, Tag } from "./components";
import { usePermission, useWorkspace } from "./context";
import type { Entity } from "./types";

interface Workflow {
  document_id: string;
  revision_id: string;
  state: string;
  row_version: number;
  submitted_by?: string;
  reviewed_by?: string;
  approval_sha256?: string;
  reason?: string;
  updated_at?: string;
}
const states: Record<string, string> = {
  imported: "原始导入",
  editing: "编辑中",
  draft: "编辑中",
  submitted: "待审核",
  changes_requested: "需修改",
  approved: "已批准",
};
export function DocumentWorkflow({
  document,
  dirty,
}: {
  document: Entity;
  dirty: boolean;
}) {
  const { w, refresh, notify } = useWorkspace();
  const canEdit = usePermission("knowledge:edit"),
    canReview = usePermission("links:review");
  const aid = w.selected_analysis_set?.id,
    frozen = w.selected_analysis_set?.state === "frozen";
  const [workflow, setWorkflow] = useState<Workflow | null>(null),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [decision, setDecision] = useState<
      "submit" | "approve" | "request_changes" | null
    >(null),
    [reason, setReason] = useState("");
  const reload = useCallback(async () => {
    if (!aid || !canEdit) return;
    setLoading(true);
    setError("");
    try {
      setWorkflow(
        await api<Workflow>(
          "/documents/" +
            document.id +
            "/workflow?analysis_set_id=" +
            encodeURIComponent(aid),
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [aid, document.id, document.entity_revision_id, canEdit]);
  useEffect(() => {
    setWorkflow(null);
    setDecision(null);
    void reload();
  }, [reload]);
  async function save() {
    if (!aid || !decision || !workflow) return;
    setBusy(true);
    setError("");
    try {
      const version =
        decision === "submit"
          ? document.row_version || Number(document.metadata?.row_version) || 1
          : workflow.row_version;
      const result = await api<Workflow>(
        "/documents/" +
          document.id +
          (decision === "submit" ? "/submit" : "/review"),
        mutation(
          "POST",
          {
            analysis_set_id: aid,
            revision_id: document.entity_revision_id,
            reason: reason.trim(),
            ...(decision === "submit" ? {} : { decision }),
          },
          version,
        ),
      );
      setWorkflow(result);
      setDecision(null);
      setReason("");
      notify(
        decision === "submit"
          ? "文档已提交审核"
          : decision === "approve"
            ? "当前文档修订已批准"
            : "文档已退回修改",
      );
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "文档或审核状态已更新。审核意见已保留，请重新载入并核对当前修订后操作。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  if (!aid || !canEdit) return null;
  const stale =
    !!workflow && workflow.revision_id !== document.entity_revision_id;
  return (
    <div className="workflow-panel">
      <div className="row between">
        <div className="row">
          <CheckCircle2 size={17} />
          <strong>文档审核</strong>
          <Tag
            value={
              workflow?.state === "approved" && !stale ? "valid" : "candidate"
            }
          >
            {loading
              ? "读取中"
              : stale
                ? "修订已变化"
                : states[workflow?.state || ""] || "等待状态"}
          </Tag>
        </div>
        {!frozen && (
          <div className="row wrap">
            <Button
              kind="small"
              disabled={
                busy ||
                loading ||
                dirty ||
                !workflow ||
                workflow.state === "submitted" ||
                (workflow.state === "approved" && !stale)
              }
              onClick={() => {
                setDecision("submit");
                setReason("");
              }}
            >
              <Send size={14} />
              提交审核
            </Button>
            {canReview && workflow?.state === "submitted" && !stale && (
              <>
                <Button
                  kind="small"
                  disabled={busy || dirty}
                  onClick={() => {
                    setDecision("request_changes");
                    setReason("");
                  }}
                >
                  要求修改
                </Button>
                <Button
                  kind="primary small"
                  disabled={busy || dirty}
                  onClick={() => {
                    setDecision("approve");
                    setReason("");
                  }}
                >
                  批准当前修订
                </Button>
              </>
            )}
          </div>
        )}
      </div>
      <p>
        {dirty
          ? "请先保存本地修改，再提交准确的文档修订。"
          : frozen
            ? "本次发布固定了文档修订和审核结果。"
            : "审核针对准确的文档修订；编辑正文后需要重新提交。批准文档不会自动确认其关联关系。"}
      </p>
      {workflow?.reason && <p>最近意见：{workflow.reason}</p>}
      {workflow?.approval_sha256 && (
        <code className="workflow-hash">
          批准摘要 {workflow.approval_sha256}
        </code>
      )}
      {workflow?.updated_at && (
        <small className="muted">更新于 {date(workflow.updated_at)}</small>
      )}
      {error && (
        <Notice tone="red">
          <p>{error}</p>
          <Button
            kind="small"
            onClick={() => {
              void reload();
            }}
          >
            重新载入审核状态
          </Button>
        </Notice>
      )}
      {decision && (
        <Modal
          title={
            decision === "submit"
              ? "提交文档审核"
              : decision === "approve"
                ? "批准文档修订"
                : "退回文档修改"
          }
          onClose={() => setDecision(null)}
        >
          <Notice>
            {document.name}
            <code className="workflow-hash">{document.entity_revision_id}</code>
            请确认已阅读当前修订，并说明本次操作的依据。
          </Notice>
          <label>
            审核说明
            <textarea
              autoFocus
              required
              minLength={1}
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={
                decision === "request_changes"
                  ? "请写明需要修改的内容"
                  : "填写检查内容与依据"
              }
            />
          </label>
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={() => setDecision(null)}>取消</Button>
            <Button
              kind="primary"
              disabled={busy || dirty || !reason.trim()}
              onClick={() => {
                void save();
              }}
            >
              确认
              {decision === "submit"
                ? "提交"
                : decision === "approve"
                  ? "批准"
                  : "退回"}
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
