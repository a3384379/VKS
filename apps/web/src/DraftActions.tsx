import { useState } from "react";
import { CopyPlus, Plus } from "lucide-react";
import { api, mutation } from "./api";
import { Button, Modal, Notice } from "./components";
import { useWorkspace, usePermission } from "./context";
import type { AnalysisSet } from "./types";

export function NewDraftButton({
  empty = false,
  small = false,
}: {
  empty?: boolean;
  small?: boolean;
}) {
  const { w, run, busy, setBaseline, hasUnsavedChanges, notify } =
    useWorkspace();
  const canEdit = usePermission(empty ? "analysis:run" : "knowledge:edit");
  const [open, setOpen] = useState(false),
    [name, setName] = useState("");
  if (!canEdit) return null;
  async function create() {
    const source = w.selected_analysis_set;
    const result = await run(
      () =>
        empty || !source
          ? api<AnalysisSet>(
              `/projects/${w.project.id}/analysis-sets`,
              mutation("POST", {
                name: name.trim(),
                sources: [],
                ruleset_version: "combined-v1",
              }),
            )
          : api<AnalysisSet>(
              `/projects/${w.project.id}/analysis-sets/${source.id}/clone`,
              mutation("POST", { name: name.trim() }),
            ),
      "新草稿已创建，原发布版本保留",
    );
    if (result) {
      setOpen(false);
      setBaseline(`draft:${result.id}`);
    }
  }
  return (
    <>
      <Button
        kind={small ? "small" : "primary"}
        disabled={busy}
        onClick={() => {
          if (hasUnsavedChanges) {
            notify("请先保存或还原文档修改，再切换基线", "error");
            return;
          }
          setName(
            empty
              ? "新资料基线"
              : `${w.selected_analysis_set?.name || "知识"} · 新草稿`,
          );
          setOpen(true);
        }}
      >
        {empty ? <Plus size={15} /> : <CopyPlus size={15} />}
        {empty ? "新建空白基线" : "从当前版本创建草稿"}
      </Button>
      {open && (
        <Modal
          title={empty ? "创建空白资料基线" : "创建独立知识草稿"}
          onClose={() => setOpen(false)}
        >
          <Notice>
            {empty
              ? "创建可接收独立文档、接口规范和原型的基线。"
              : "固定原版本的来源与实体修订，复制关联到新草稿；之后的修改不会改变历史发布版。"}
          </Notice>
          <label>
            基线名称
            <input
              autoFocus
              value={name}
              maxLength={160}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </label>
          <div className="modal-actions">
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={busy || !name.trim()}
              onClick={() => {
                void create();
              }}
            >
              创建草稿
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}
