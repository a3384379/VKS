import "./configuration.css";
import { useEffect, useRef, useState } from "react";
import {
  GitMerge,
  GitPullRequestArrow,
  MessageSquareWarning,
  Plus,
  Split,
  X,
} from "lucide-react";
import { api, ApiError, mutation } from "./api";
import { Button, Modal, Notice, RelationState } from "./components";
import { usePermission, useWorkspace } from "./context";
import type { Relation } from "./types";
export type CorrectionOperation = "retarget" | "split" | "merge" | "feedback";
interface Target {
  to_revision_id: string;
  evidence_ids: string[];
}
const typeOf = (relation: Relation) =>
  relation.relation_type || relation.type || "";
export const deterministicRelationTypes = new Set([
  "contains",
  "renders",
  "handler_of",
  "implements",
  "maps_to_sql",
  "reads_table",
  "writes_table",
]);
export function correctionPayload(
  analysisId: string,
  operation: CorrectionOperation,
  reason: string,
  targets: Target[],
  originals: Relation[],
  correctTarget: string,
) {
  return {
    analysis_set_id: analysisId,
    operation,
    reason: reason.trim(),
    targets: operation === "feedback" ? [] : targets,
    ...(operation === "merge"
      ? {
          merge_relation_ids: originals.map((row) => row.id),
          expected_versions: Object.fromEntries(
            originals.map((row) => [row.id, row.row_version]),
          ),
        }
      : {}),
    ...(operation === "feedback" && correctTarget
      ? { correct_target_revision_id: correctTarget }
      : {}),
  };
}
const names: Record<CorrectionOperation, string> = {
  retarget: "改选目标",
  split: "拆分关联",
  merge: "合并关联",
  feedback: "关联纠错反馈",
};
export function RelationOperations({ relation }: { relation: Relation }) {
  const { w, notify, refresh } = useWorkspace();
  const canEdit = usePermission("knowledge:edit"),
    canRead = usePermission("knowledge:read");
  const writable = w.selected_analysis_set?.state !== "frozen",
    aid = w.selected_analysis_set?.id;
  const [operation, setOperation] = useState<CorrectionOperation | null>(null),
    [original, setOriginal] = useState(relation),
    [targets, setTargets] = useState<Target[]>([]),
    [siblings, setSiblings] = useState<Relation[]>([]),
    [merged, setMerged] = useState<string[]>([]),
    [correctTarget, setCorrectTarget] = useState(""),
    [filter, setFilter] = useState(""),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const active = useRef(true);
  const retry = useRef<{
    signature: string;
    key: string;
  } | null>(null);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  function open(next: CorrectionOperation) {
    retry.current = null;
    setOperation(next);
    setOriginal(relation);
    setReason("");
    setError("");
    setFilter("");
    setCorrectTarget("");
    setTargets(
      next === "split"
        ? [
            { to_revision_id: "", evidence_ids: [] },
            { to_revision_id: "", evidence_ids: [] },
          ]
        : [
            {
              to_revision_id: next === "merge" ? relation.to_revision_id : "",
              evidence_ids: [],
            },
          ],
    );
    setSiblings(
      w.relations.filter(
        (row) =>
          row.id !== relation.id &&
          row.from_revision_id === relation.from_revision_id &&
          typeOf(row) === typeOf(relation) &&
          row.validity_status !== "obsolete",
      ),
    );
    setMerged([relation.id]);
  }
  async function submit() {
    if (!operation || !aid) return;
    setBusy(true);
    setError("");
    try {
      const originals = [
        original,
        ...siblings.filter((row) => merged.includes(row.id)),
      ];
      const payload = correctionPayload(
        aid,
        operation,
        reason,
        targets,
        originals,
        correctTarget,
      );
      const version = original.row_version;
      const signature = JSON.stringify([original.id, version, payload]);
      if (retry.current?.signature !== signature)
        retry.current = { signature, key: crypto.randomUUID() };
      const request = mutation("POST", payload, version);
      request.headers = {
        ...request.headers,
        "Idempotency-Key": retry.current.key,
      };
      await api("/relations/" + original.id + "/corrections", request);
      if (!active.current) return;
      notify(operation === "feedback" ? "纠错备注已保存" : "关联已更新");
      setOperation(null);
      await refresh();
    } catch (e) {
      if (!active.current) return;
      setError(
        e instanceof ApiError && e.status === 412
          ? "关系已被其他人更新。当前意见、目标和证据保留，请复制需要保留的内容，再刷新核对当前版本。"
          : (e as Error).message,
      );
    } finally {
      if (active.current) setBusy(false);
    }
  }
  const filteredEntities = w.entities
    .filter(
      (entity) =>
        !filter ||
        (entity.name + " " + entity.stable_key)
          .toLowerCase()
          .includes(filter.toLowerCase()),
    )
    .slice(0, 100);
  function options(selected: string) {
    const current = w.entities.find(
        (entity) => entity.entity_revision_id === selected,
      ),
      rows =
        current && !filteredEntities.includes(current)
          ? [current, ...filteredEntities]
          : filteredEntities;
    return (
      <>
        <option value="">选择当前基线中的目标</option>
        {rows.map((entity) => (
          <option
            key={entity.entity_revision_id}
            value={entity.entity_revision_id}
          >
            {entity.name} · {entity.stable_key}
          </option>
        ))}
      </>
    );
  }
  const invalidTargets =
    !targets.length ||
    targets.some(
      (target) => !target.to_revision_id || !target.evidence_ids.length,
    ) ||
    new Set(targets.map((target) => target.to_revision_id)).size !==
      targets.length;
  if (!canRead || !aid) return null;
  return (
    <div className="relation-operations">
      <div className="row wrap">
        {canEdit && writable && (
          <>
            <Button kind="small" onClick={() => open("retarget")}>
              <GitPullRequestArrow size={14} />
              改选目标
            </Button>
            <Button kind="small" onClick={() => open("split")}>
              <Split size={14} />
              拆分
            </Button>
            {
              <Button kind="small" onClick={() => open("merge")}>
                <GitMerge size={14} />
                合并
              </Button>
            }
          </>
        )}
        {
          <Button kind="small" onClick={() => open("feedback")}>
            <MessageSquareWarning size={14} />
            关联不正确
          </Button>
        }
      </div>
      {operation && (
        <Modal title={names[operation]} onClose={() => setOperation(null)}>
          <Notice>
            {original.from_name} → {original.to_name}
            <code className="workflow-hash">
              {original.id} · v{original.row_version}
            </code>
            {operation === "feedback"
              ? "反馈供人工核验及本地评测使用，不自动更改关系，不向外部服务上传或训练。"
              : "改选、拆分和合并会保留原关系并生成新关联，保存后直接可用；有效性与因果分别核验。"}
          </Notice>
          {operation === "merge" && (
            <>
              <h3 className="detail-section-title">
                选择同来源对象、同类型的原关系（至少 2 项）
              </h3>
              <label className="check-line">
                <input type="checkbox" checked disabled />
                当前关联：{original.to_name}
              </label>
              <div className="evidence-checks">
                {siblings.map((row) => (
                  <label className="check-line" key={row.id}>
                    <input
                      type="checkbox"
                      checked={merged.includes(row.id)}
                      disabled={!merged.includes(row.id) && merged.length >= 20}
                      onChange={(e) =>
                        setMerged((previous) =>
                          e.target.checked
                            ? [...previous, row.id]
                            : previous.filter((id) => id !== row.id),
                        )
                      }
                    />
                    <span>
                      {row.to_name}
                      <RelationState relation={row} />
                    </span>
                  </label>
                ))}
              </div>
              {!siblings.length && (
                <Notice>当前基线没有可合并的同来源、同类型关系。</Notice>
              )}
            </>
          )}
          <label>
            筛选目标对象
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="对象名称或稳定标识，显示前 100 项"
            />
          </label>
          {operation === "feedback" ? (
            <label>
              建议正确目标（可选）
              <select
                value={correctTarget}
                onChange={(e) => setCorrectTarget(e.target.value)}
              >
                {options(correctTarget)}
              </select>
            </label>
          ) : (
            targets.map((target, index) => (
              <div className="correction-target" key={index}>
                <div className="row between">
                  <strong>
                    {operation === "split"
                      ? "拆分目标 " + (index + 1)
                      : "新目标"}
                  </strong>
                  {operation === "split" && targets.length > 2 && (
                    <Button
                      kind="small"
                      onClick={() =>
                        setTargets((previous) =>
                          previous.filter((_, i) => i !== index),
                        )
                      }
                    >
                      <X size={14} />
                      移除
                    </Button>
                  )}
                </div>
                <select
                  required
                  aria-label={
                    operation === "split" ? "拆分目标 " + (index + 1) : "新目标"
                  }
                  value={target.to_revision_id}
                  onChange={(e) =>
                    setTargets((previous) =>
                      previous.map((row, i) =>
                        i === index
                          ? { ...row, to_revision_id: e.target.value }
                          : row,
                      ),
                    )
                  }
                >
                  {options(target.to_revision_id)}
                </select>
                <details>
                  <summary>
                    选择该目标的当前证据（{target.evidence_ids.length} 项）
                  </summary>
                  <p className="section-description">
                    证据需支持当前来源与新目标，最多 20 项。
                  </p>
                  <div className="evidence-checks">
                    {w.evidence.map((evidence) => (
                      <label className="check-line" key={evidence.id}>
                        <input
                          type="checkbox"
                          checked={target.evidence_ids.includes(evidence.id)}
                          disabled={
                            !target.evidence_ids.includes(evidence.id) &&
                            target.evidence_ids.length >= 20
                          }
                          onChange={(e) =>
                            setTargets((previous) =>
                              previous.map((row, i) =>
                                i === index
                                  ? {
                                      ...row,
                                      evidence_ids: e.target.checked
                                        ? [...row.evidence_ids, evidence.id]
                                        : row.evidence_ids.filter(
                                            (id) => id !== evidence.id,
                                          ),
                                    }
                                  : row,
                              ),
                            )
                          }
                        />
                        <span>
                          {evidence.evidence_kind || evidence.kind}
                          <small>
                            {evidence.claim || evidence.excerpt || evidence.id}
                          </small>
                          <small>
                            {evidence.artifact_path || evidence.artifact_id}
                          </small>
                        </span>
                      </label>
                    ))}
                  </div>
                </details>
              </div>
            ))
          )}
          {operation === "split" && (
            <Button
              kind="small"
              disabled={targets.length >= 20}
              onClick={() =>
                setTargets((previous) => [
                  ...previous,
                  { to_revision_id: "", evidence_ids: [] },
                ])
              }
            >
              <Plus size={14} />
              增加拆分目标
            </Button>
          )}
          <label>
            说明与依据
            <textarea
              required
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={
                operation === "feedback"
                  ? "说明哪里不正确，以及你掌握的正确业务含义"
                  : "说明端点变更及证据依据"
              }
            />
          </label>
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={() => setOperation(null)}>取消</Button>
            <Button
              kind="primary"
              disabled={
                busy ||
                !reason.trim() ||
                (operation !== "feedback" && invalidTargets) ||
                (operation === "merge" && merged.length < 2)
              }
              onClick={() => {
                void submit();
              }}
            >
              {operation === "feedback" ? "提交反馈" : "保存关联"}
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
