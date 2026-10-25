import "./configuration.css";
import { useState } from "react";
import {
  GitMerge,
  GitPullRequestArrow,
  MessageSquareWarning,
  Plus,
  Split,
  X,
} from "lucide-react";
import { api, ApiError, mutation } from "./api";
import { Button, Modal, Notice, RelationState, Tag } from "./components";
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
export function batchReviewPayload(
  analysisId: string,
  relations: Relation[],
  decision: "confirm" | "reject",
  reason: string,
) {
  return {
    analysis_set_id: analysisId,
    relation_ids: relations.map((row) => row.id),
    expected_versions: Object.fromEntries(
      relations.map((row) => [row.id, row.row_version]),
    ),
    decision,
    reason: reason.trim(),
  };
}
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
  const { w, session, notify, refresh } = useWorkspace();
  const canReview = usePermission("links:review"),
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
  function open(next: CorrectionOperation) {
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
      await api(
        "/relations/" + original.id + "/corrections",
        mutation(
          "POST",
          correctionPayload(
            aid,
            operation,
            reason,
            targets,
            originals,
            correctTarget,
          ),
          original.row_version,
        ),
      );
      notify(
        operation === "feedback"
          ? "纠错反馈已记录，原关系保留等待核验"
          : "新的候选关系已创建，原关系与审核依据已保留",
      );
      setOperation(null);
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "关系已被其他人更新。当前意见与目标保留，请关闭前复制需要保留的内容，再刷新核对当前版本。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
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
  if (!canRead || session.human === false || !aid) return null;
  return (
    <div className="relation-operations">
      <div className="row wrap">
        {canReview && writable && (
          <>
            <Button kind="small" onClick={() => open("retarget")}>
              <GitPullRequestArrow size={14} />
              改选目标
            </Button>
            <Button kind="small" onClick={() => open("split")}>
              <Split size={14} />
              拆分
            </Button>
            <Button kind="small" onClick={() => open("merge")}>
              <GitMerge size={14} />
              合并
            </Button>
          </>
        )}
        <Button kind="small" onClick={() => open("feedback")}>
          <MessageSquareWarning size={14} />
          关联不正确
        </Button>
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
              : "改选、拆分和合并会保留原关系并生成候选，需要再次审核；原确认不会迁移到新目标。"}
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
              {operation === "feedback" ? "提交反馈" : "创建候选"}
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

export function BatchRelationReview() {
  const { w, notify, refresh } = useWorkspace();
  const canReview = usePermission("links:review");
  const [open, setOpen] = useState(false),
    [rows, setRows] = useState<Relation[]>([]),
    [type, setType] = useState(""),
    [selected, setSelected] = useState<string[]>([]),
    [decision, setDecision] = useState<"confirm" | "reject">("confirm"),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const eligible = rows.filter((row) => typeOf(row) === type),
    chosen = eligible.filter((row) => selected.includes(row.id));
  const types = [...new Set(rows.map(typeOf))];
  async function submit() {
    if (!w.selected_analysis_set) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ count: number }>(
        "/projects/" + w.project.id + "/relations/batch-review",
        mutation(
          "POST",
          batchReviewPayload(
            w.selected_analysis_set.id,
            chosen,
            decision,
            reason,
          ),
        ),
      );
      setOpen(false);
      notify(
        "已批量" +
          (decision === "confirm" ? "确认 " : "拒绝 ") +
          result.count +
          " 项关系，每项审核记录独立保存",
      );
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "至少一项关系已更新，整批未提交。当前选择和说明保留；请关闭并刷新后重新核对。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  if (
    !canReview ||
    !w.selected_analysis_set ||
    w.selected_analysis_set.state === "frozen"
  )
    return null;
  return (
    <>
      <Button
        kind="small"
        onClick={() => {
          const candidates = w.relations.filter(
            (row) => row.review_status === "candidate",
          );
          setRows(candidates);
          setType(
            typeOf(
              candidates.find((row) =>
                deterministicRelationTypes.has(typeOf(row)),
              ) ||
                candidates[0] ||
                ({ relation_type: "" } as Relation),
            ),
          );
          setSelected([]);
          setReason("");
          setError("");
          setDecision("confirm");
          setOpen(true);
        }}
      >
        批量审核同类关系
      </Button>
      {open && (
        <Modal
          title="同基线、同类型关系批量审核"
          onClose={() => setOpen(false)}
        >
          <Notice>
            每批最多 100
            项。确认仅限确定性结构关系，且每项必须有效并通过证据核验；任一项发生冲突或不合格，整批不写入。每条关系保留独立审核日志。
          </Notice>
          <label>
            关系类型
            <select
              value={type}
              onChange={(e) => {
                setType(e.target.value);
                if (!deterministicRelationTypes.has(e.target.value))
                  setDecision("reject");
                setSelected([]);
              }}
            >
              {types.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label>
            审核操作
            <select
              value={decision}
              onChange={(e) => {
                setDecision(e.target.value as "confirm" | "reject");
                setSelected([]);
              }}
            >
              <option
                value="confirm"
                disabled={!deterministicRelationTypes.has(type)}
              >
                确认确定性关系
              </option>
              <option value="reject">拒绝候选</option>
            </select>
          </label>
          <div className="row between">
            <strong>
              {chosen.length} / {eligible.length} 项已选择
            </strong>
            <Button
              kind="small"
              onClick={() =>
                setSelected(
                  eligible
                    .filter(
                      (row) =>
                        decision === "reject" ||
                        (row.validity_status === "valid" &&
                          row.evidence_ids.length),
                    )
                    .slice(0, 100)
                    .map((row) => row.id),
                )
              }
            >
              选择符合条件的前 100 项
            </Button>
          </div>
          <div className="evidence-checks">
            {eligible.map((row) => (
              <label className="check-line" key={row.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(row.id)}
                  disabled={
                    (!selected.includes(row.id) && selected.length >= 100) ||
                    (decision === "confirm" &&
                      (row.validity_status !== "valid" ||
                        !row.evidence_ids.length))
                  }
                  onChange={(e) =>
                    setSelected((previous) =>
                      e.target.checked
                        ? [...previous, row.id]
                        : previous.filter((id) => id !== row.id),
                    )
                  }
                />
                <span>
                  {row.from_name} → {row.to_name}
                  <small>
                    {row.evidence_ids.length} 项证据 · 关系 v{row.row_version}
                  </small>
                  <Tag value={row.validity_status} />
                </span>
              </label>
            ))}
          </div>
          <label>
            本批审核依据
            <textarea
              required
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="说明同类规则及逐项核对结果"
            />
          </label>
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={
                busy ||
                !chosen.length ||
                !reason.trim() ||
                (decision === "confirm" &&
                  !deterministicRelationTypes.has(type))
              }
              onClick={() => {
                void submit();
              }}
            >
              确认处理 {chosen.length} 项
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}
