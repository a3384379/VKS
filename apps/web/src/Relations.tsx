import "./relations.css";
import { useEffect, useState } from "react";
import { api, mutation } from "./api";
import {
  Button,
  Empty,
  EvidenceList,
  Notice,
  Panel,
  RelationState,
} from "./components";
import { RelationOperations } from "./RelationOperations";
import { usePermission, useWorkspace } from "./context";

export function Relations() {
  const { w, selected, setSelected, run, busy } = useWorkspace();
  const canEdit =
    usePermission("knowledge:edit") &&
    w.selected_analysis_set?.state !== "frozen";
  const [showExcluded, setShowExcluded] = useState(false);
  useEffect(() => {
    const target = w.relations.find((row) => row.id === selected);
    if (target) setShowExcluded(target.excluded);
  }, [selected, w.relations]);
  const rows = w.relations.filter((row) => row.excluded === showExcluded);
  const relation = rows.find((row) => row.id === selected) || rows[0];
  return (
    <div className="configuration-stack">
      <Panel title="知识关联">
        <Notice>
          关联直接参与知识阅读。错误关联可移除，已移除内容可恢复；来源变化导致的失效单独显示。
        </Notice>
        <div className="segmented">
          <button
            className={!showExcluded ? "active" : ""}
            onClick={() => setShowExcluded(false)}
          >
            当前关联
          </button>
          <button
            className={showExcluded ? "active" : ""}
            onClick={() => setShowExcluded(true)}
          >
            已移除
          </button>
        </div>
        <div className="knowledge-relations-layout">
          <div className="relation-list" aria-label="知识关联列表">
            {rows.map((row) => (
              <button
                data-relation-id={row.id}
                key={row.id}
                className={row.id === relation?.id ? "selected" : ""}
                onClick={() => setSelected(row.id)}
              >
                {row.from_name} → {row.to_name}
                <small>{row.relation_type}</small>
              </button>
            ))}
          </div>
          {relation && (
            <div className="configuration-stack relation-detail">
              <h3>
                {relation.from_name} → {relation.to_name}
              </h3>
              <RelationState relation={relation} />
              <p>{relation.reason}</p>
              <EvidenceList
                evidence={w.evidence.filter((row) =>
                  relation.evidence_ids.includes(row.id),
                )}
              />
              {canEdit && (
                <>
                  <RelationOperations relation={relation} />
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () =>
                          api(
                            `/relations/${relation.id}`,
                            mutation(
                              "PATCH",
                              { excluded: !relation.excluded },
                              relation.row_version,
                            ),
                          ),
                        relation.excluded ? "关联已恢复" : "关联已移除",
                      )
                    }
                  >
                    {relation.excluded ? "恢复关联" : "移除关联"}
                  </Button>
                </>
              )}
            </div>
          )}
        </div>
        {!rows.length && (
          <Empty
            title={showExcluded ? "暂无已移除关联" : "当前没有关联"}
            description="导入资料或从业务动作中建立关联。"
          />
        )}
      </Panel>
    </div>
  );
}
