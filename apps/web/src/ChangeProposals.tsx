import "./configuration.css";
import { useCallback, useEffect, useState } from "react";
import { GitCompareArrows, RefreshCw, ShieldCheck } from "lucide-react";
import { api, ApiError, mutation } from "./api";
import {
  Button,
  Empty,
  EvidenceList,
  Modal,
  Notice,
  Panel,
  RelationState,
  Spinner,
  Tag,
} from "./components";
import { ReadScope, usePermission, useWorkspace } from "./context";
import type { Dict, Relation } from "./types";
import { DocumentSectionDiff } from "./DocumentSectionDiff";

interface Proposal {
  id: string;
  kind: string;
  status: string;
  row_version: number;
  entity_id?: string;
  entity_type?: string;
  from_release_id?: string;
  old_revision_id?: string;
  new_revision_id?: string;
  source_relation_id?: string;
  target_relation_id?: string;
  relation?: Relation;
  details?: Dict;
  metadata?: Dict;
  reason?: string;
}
const kinds: Record<string, string> = {
  relation_migration: "关联迁移",
  entity_change: "实体修订变更",
};
export function ChangeProposals() {
  const { w, refresh, notify } = useWorkspace();
  const canRead = usePermission("knowledge:edit"),
    canReview = usePermission("links:review");
  const aid = w.selected_analysis_set?.id,
    frozen = w.selected_analysis_set?.state === "frozen";
  const [proposals, setProposals] = useState<Proposal[]>([]),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [selected, setSelected] = useState<Proposal | null>(null),
    [decision, setDecision] = useState<"accept" | "reject">("accept"),
    [reason, setReason] = useState("");
  const [relation, setRelation] = useState<Relation | null>(null),
    [evidenceIds, setEvidenceIds] = useState<string[]>([]);
  const reload = useCallback(async () => {
    if (!aid || !canRead) return;
    setLoading(true);
    setError("");
    try {
      const response = await api<Proposal[] | { items: Proposal[] }>(
        "/projects/" +
          w.project.id +
          "/change-proposals?analysis_set_id=" +
          encodeURIComponent(aid),
      );
      setProposals(Array.isArray(response) ? response : response.items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [aid, w.project.id, canRead]);
  useEffect(() => {
    setProposals([]);
    setSelected(null);
    setRelation(null);
    void reload();
  }, [reload, w.selected_analysis_set?.selection_version]);
  async function perform(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError("");
    try {
      await action();
      setSelected(null);
      setRelation(null);
      notify(message);
      await refresh();
      await reload();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "提案或关联已更新，当前意见保留。请重新载入并核对后再操作。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  const staleRelations = w.relations.filter(
    (row) =>
      row.validity_status !== "valid" && row.validity_status !== "obsolete",
  );
  if (!canRead || !aid) return null;
  return (
    <div className="configuration-stack">
      <Panel
        title="当前基线的变更提案"
        action={
          <Button
            kind="small"
            disabled={loading}
            onClick={() => {
              void reload();
            }}
          >
            <RefreshCw size={14} />
            刷新
          </Button>
        }
      >
        <Notice>
          接受迁移提案仅表示采用变更方向。原有人工审核与证据有效性分别保存，关联仍需使用当前来源版本的证据复核。
        </Notice>
        {error && <Notice tone="red">{error}</Notice>}
        {loading ? (
          <Spinner />
        ) : (
          proposals.map((row) => {
            const entity = w.entities.find((e) => e.id === row.entity_id),
              target =
                row.relation ||
                w.relations.find((r) => r.id === row.target_relation_id);
            return (
              <div className="proposal-card" key={row.id}>
                <div className="row between">
                  <div className="row">
                    <GitCompareArrows size={18} />
                    <h3>
                      {kinds[row.kind] || row.kind}
                      {entity ? " · " + entity.name : ""}
                    </h3>
                  </div>
                  <Tag
                    value={
                      row.status === "accepted"
                        ? "valid"
                        : row.status === "rejected"
                          ? "obsolete"
                          : "candidate"
                    }
                  >
                    {row.status === "accepted"
                      ? "已接受"
                      : row.status === "rejected"
                        ? "已拒绝"
                        : "待处理"}
                  </Tag>
                </div>
                <div className="proposal-revisions">
                  {row.old_revision_id && (
                    <code>原修订 {row.old_revision_id}</code>
                  )}
                  {row.new_revision_id && (
                    <code>新修订 {row.new_revision_id}</code>
                  )}
                </div>
                {target && <RelationState relation={target} />}
                {row.entity_type === "document" &&
                  row.entity_id &&
                  row.from_release_id && (
                    <ReadScope>
                      <DocumentSectionDiff
                        documentId={row.entity_id}
                        fromRelease={row.from_release_id}
                        expectedRevision={row.new_revision_id}
                      />
                    </ReadScope>
                  )}
                {(row.details || row.metadata) && (
                  <details>
                    <summary>查看变更定位与影响信息</summary>
                    <pre className="proposal-detail">
                      {JSON.stringify(row.details || row.metadata, null, 2)}
                    </pre>
                  </details>
                )}
                {row.reason && (
                  <p className="section-description">处理说明：{row.reason}</p>
                )}
                {canReview && !frozen && row.status === "pending" && (
                  <div className="configuration-actions">
                    <Button
                      kind="small"
                      disabled={busy}
                      onClick={() => {
                        setSelected(row);
                        setDecision("reject");
                        setReason("");
                      }}
                    >
                      拒绝提案
                    </Button>
                    <Button
                      kind="primary small"
                      disabled={busy}
                      onClick={() => {
                        setSelected(row);
                        setDecision("accept");
                        setReason("");
                      }}
                    >
                      接受提案
                    </Button>
                  </div>
                )}
              </div>
            );
          })
        )}
        {!loading && !proposals.length && (
          <Empty
            title="当前基线没有变更提案"
            description="从已发布版本进行增量分析后，这里列出实体变更与关联迁移建议。"
          />
        )}
      </Panel>
      <Panel
        title="关联有效性复核"
        action={<Tag>{staleRelations.length} 项需核对</Tag>}
      >
        {staleRelations.map((row) => {
          const from = w.entities.find(
              (e) => e.entity_revision_id === row.from_revision_id,
            ),
            to = w.entities.find(
              (e) => e.entity_revision_id === row.to_revision_id,
            );
          return (
            <div className="proposal-card" key={row.id}>
              <h3>
                {from?.name || "原端点未选中"} → {to?.name || "目标端点未选中"}
              </h3>
              <RelationState relation={row} />
              <EvidenceList
                evidence={w.evidence.filter((e) =>
                  row.evidence_ids.includes(e.id),
                )}
              />
              {canReview && !frozen && (
                <div className="configuration-actions">
                  <Button
                    kind="small"
                    disabled={busy || !from || !to}
                    onClick={() => {
                      setRelation(row);
                      setEvidenceIds([]);
                      setReason("");
                    }}
                  >
                    选择当前证据复核
                  </Button>
                </div>
              )}
            </div>
          );
        })}
        {!staleRelations.length && (
          <Empty
            title="当前可见关联暂无待复核有效性"
            description="来源变化后，过期或断链关联会保留原审核状态，并单独要求证据复核。"
          />
        )}
      </Panel>
      {selected && (
        <Modal
          title={decision === "accept" ? "接受变更提案" : "拒绝变更提案"}
          onClose={() => setSelected(null)}
        >
          <Notice>
            本次操作针对提案当前版本。接受提案后，仍需检查迁移关联的证据与审核状态。
          </Notice>
          <label>
            处理依据
            <textarea
              autoFocus
              required
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={() => setSelected(null)}>取消</Button>
            <Button
              kind="primary"
              disabled={busy || !reason.trim()}
              onClick={() => {
                void perform(
                  () =>
                    api(
                      "/change-proposals/" + selected.id + "/review",
                      mutation(
                        "POST",
                        { decision, reason: reason.trim() },
                        selected.row_version,
                      ),
                    ),
                  "变更提案已处理",
                );
              }}
            >
              确认{decision === "accept" ? "接受" : "拒绝"}
            </Button>
          </div>
        </Modal>
      )}
      {relation && (
        <Modal title="用当前来源证据复核关联" onClose={() => setRelation(null)}>
          <Notice>
            <ShieldCheck size={16} />
            选择覆盖当前两个关联端点的证据，最多 20
            项。复核有效性不会自动批准候选关系。
          </Notice>
          <div className="evidence-checks">
            {w.evidence.map((evidence) => (
              <label className="check-line" key={evidence.id}>
                <input
                  type="checkbox"
                  checked={evidenceIds.includes(evidence.id)}
                  disabled={
                    !evidenceIds.includes(evidence.id) &&
                    evidenceIds.length >= 20
                  }
                  onChange={(e) =>
                    setEvidenceIds((previous) =>
                      e.target.checked
                        ? [...previous, evidence.id]
                        : previous.filter((id) => id !== evidence.id),
                    )
                  }
                />
                <span>
                  {evidence.evidence_kind || evidence.kind || "证据"}
                  <small>
                    {evidence.claim ||
                      evidence.excerpt ||
                      evidence.content ||
                      evidence.id}
                  </small>
                  <small>{evidence.artifact_id}</small>
                </span>
              </label>
            ))}
          </div>
          <label>
            复核依据
            <textarea
              required
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={() => setRelation(null)}>取消</Button>
            <Button
              kind="primary"
              disabled={busy || !reason.trim() || !evidenceIds.length}
              onClick={() => {
                void perform(
                  () =>
                    api(
                      "/relations/" + relation.id + "/revalidate",
                      mutation(
                        "POST",
                        {
                          analysis_set_id: aid,
                          evidence_ids: evidenceIds,
                          reason: reason.trim(),
                        },
                        relation.row_version,
                      ),
                    ),
                  "关联有效性已复核，原审核状态保留",
                );
              }}
            >
              确认复核
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
