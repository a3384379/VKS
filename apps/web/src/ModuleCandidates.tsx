import { useEffect, useState } from "react";
import { api, mutation } from "./api";
import { Button, Notice, Tag } from "./components";
import { useWorkspace } from "./context";
export type ModuleCandidate = {
  key: string;
  title: string;
  reason: string;
  entity_ids: string[];
};
export function ModuleCandidates({
  jobId,
  candidates,
  decisions,
  reload,
}: {
  jobId: string;
  candidates: ModuleCandidate[];
  decisions: Record<string, string>;
  reload: () => Promise<void>;
}) {
  const { run } = useWorkspace();
  const [selected, setSelected] = useState<string[]>([]),
    [reason, setReason] = useState("");
  const pendingSelection = selected.filter(
    (key) =>
      candidates.some((candidate) => candidate.key === key) && !decisions[key],
  );
  useEffect(() => {
    setSelected([]);
    setReason("");
  }, [jobId]);
  useEffect(() => {
    setSelected((rows) => {
      const pending = rows.filter(
        (key) =>
          candidates.some((candidate) => candidate.key === key) &&
          !decisions[key],
      );
      return pending.length === rows.length ? rows : pending;
    });
  }, [candidates, decisions]);
  return (
    <div>
      <Notice>
        候选可重叠。采用后可在项目模块管理调整成员与层级；拒绝决定绑定本次报告，原始材料保持不变。
      </Notice>
      <div className="v2-domain-grid">
        {candidates.map((candidate) => (
          <div className="v2-domain" key={candidate.key}>
            <h3>{candidate.title}</h3>
            <p>{candidate.reason}</p>
            <small>{candidate.entity_ids.length} 个准确实体引用</small>
            {decisions[candidate.key] ? (
              <Tag>
                {decisions[candidate.key] === "accepted" ? "已采用" : "已拒绝"}
              </Tag>
            ) : (
              <>
                <label>
                  <input
                    type="checkbox"
                    checked={selected.includes(candidate.key)}
                    onChange={(e) =>
                      setSelected((rows) =>
                        e.target.checked
                          ? [...rows, candidate.key]
                          : rows.filter((k) => k !== candidate.key),
                      )
                    }
                  />
                  选择此候选
                </label>
                <Button
                  onClick={() =>
                    void run(async () => {
                      await api(
                        `/api/v2/knowledge-tasks/${jobId}/module-proposals/${candidate.key}/accept`,
                        mutation("POST", {}),
                      );
                      setSelected((rows) =>
                        rows.filter((key) => key !== candidate.key),
                      );
                      await reload();
                    }, "已按人员决定创建模块分组")
                  }
                >
                  采用为模块分组
                </Button>
              </>
            )}
          </div>
        ))}
      </div>
      <label>
        批量拒绝原因
        <input
          value={reason}
          maxLength={2000}
          onChange={(e) => setReason(e.target.value)}
        />
      </label>
      <Button
        disabled={!pendingSelection.length || !reason.trim()}
        onClick={() =>
          void run(async () => {
            await api(
              `/api/v2/knowledge-tasks/${jobId}/module-proposals/reject`,
              mutation("POST", { candidate_keys: pendingSelection, reason }),
            );
            setSelected([]);
            await reload();
          }, "已记录候选拒绝决定")
        }
      >
        拒绝所选 {pendingSelection.length} 项
      </Button>
    </div>
  );
}
