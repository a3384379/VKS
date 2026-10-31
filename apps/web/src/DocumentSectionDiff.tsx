import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Button, Notice, Tag } from "./components";
import { useWorkspace } from "./context";
import { ProductEntityLink } from "./ProductEntityLink";
type Report = {
  before: { revision_id: string; name: string } | null;
  after: { revision_id: string; name: string } | null;
  sections: {
    section_id: string;
    section_key: string;
    change_kind: string;
    locked_before: boolean;
    locked_after: boolean;
    before: { revision_id: string; name: string } | null;
    after: { revision_id: string; name: string } | null;
    patch: { text: string; is_truncated: boolean };
  }[];
  denominator: {
    before_sections: number;
    after_sections: number;
    changed_sections: number;
    unchanged_sections: number;
  };
  is_truncated: boolean;
  report_sha256: string;
};
export function DocumentSectionDiff({
  documentId,
  fromRelease,
  expectedRevision,
}: {
  documentId: string;
  fromRelease: string;
  expectedRevision?: string;
}) {
  const { w } = useWorkspace();
  const key = `${w.project.id}:${w.selected_analysis_set?.id}:${w.selected_analysis_set?.selection_version}:${documentId}:${fromRelease}:${expectedRevision}`;
  const current = useRef(key);
  current.current = key;
  const [report, setReport] = useState<Report | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    setReport(null);
    setError("");
  }, [key]);
  async function load() {
    const requested = key;
    setBusy(true);
    setError("");
    const q = new URLSearchParams({
      from_release_id: fromRelease,
      to_analysis_set_id: w.selected_analysis_set!.id,
      expected_revision_id: expectedRevision || "-",
    });
    try {
      const row = await api<Report>(
        `/api/v2/projects/${w.project.id}/documents/${documentId}/section-diff?${q}`,
      );
      if (current.current === requested) setReport(row);
    } catch (e) {
      if (current.current === requested) setError((e as Error).message);
    } finally {
      if (current.current === requested) setBusy(false);
    }
  }
  return (
    <div>
      <Button kind="small" disabled={busy} onClick={() => void load()}>
        {busy ? "正在核对章节…" : "查看固定章节差异"}
      </Button>
      {error && <Notice tone="red">{error}</Notice>}
      {report && (
        <>
          <p>
            旧版 {report.denominator.before_sections} 节，新版{" "}
            {report.denominator.after_sections} 节；
            {report.denominator.changed_sections} 节有变化，
            {report.denominator.unchanged_sections} 节保持原修订与顺序。
          </p>
          <Notice>
            差异展示基于准确修订；此处不覆盖正文、人工锁或历史修订。
          </Notice>
          {report.sections.map((row) => (
            <details key={row.section_id}>
              <summary>
                {row.after?.name || row.before?.name} ·{" "}
                {(
                  {
                    added: "新增",
                    removed: "删除",
                    modified: "正文改变",
                    renamed: "标题改变",
                    moved: "位置改变",
                    metadata_only: "元数据改变",
                  } as Record<string, string>
                )[row.change_kind] || row.change_kind}{" "}
                {(row.locked_before || row.locked_after) && <Tag>含人工锁</Tag>}
              </summary>
              {w.entities.find(
                (e) =>
                  e.entity_id === row.section_id &&
                  e.type === "section" &&
                  e.entity_revision_id === row.after?.revision_id,
              ) && (
                <ProductEntityLink
                  entity={
                    w.entities.find(
                      (e) =>
                        e.entity_id === row.section_id &&
                        e.type === "section" &&
                        e.entity_revision_id === row.after?.revision_id,
                    )!
                  }
                >
                  打开同版章节
                </ProductEntityLink>
              )}
              <pre className="v2-json">{row.patch.text || "正文未变化"}</pre>
              {row.patch.is_truncated && (
                <Notice>差异显示已截断，请核对完整原文。</Notice>
              )}
            </details>
          ))}
          {report.is_truncated && (
            <Notice>当前显示前 200 项变化，完整分母见上方。</Notice>
          )}
          <small className="v2-hash">固定报告：{report.report_sha256}</small>
        </>
      )}
    </div>
  );
}
