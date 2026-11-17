import { useMemo, useState } from "react";
import { RichMarkdown, type MarkdownReference } from "./RichMarkdown";
import { associationDiff, comparisonGroups } from "./associationDiff";

export type AssociationBaseline = {
  title: string;
  explanation: string;
  sections?: MarkdownReference[];
  binding_version: number;
  point_revision_id: string;
};

export function AssociationComparison({
  title,
  explanation,
  sections,
  baseline,
  onReference,
}: {
  title: string;
  explanation: string;
  sections: MarkdownReference[];
  baseline?: AssociationBaseline | null;
  onReference: (reference: MarkdownReference) => void;
}) {
  const [view, setView] = useState<"diff" | "markdown">(
    baseline === undefined ? "markdown" : "diff",
  );
  const [expanded, setExpanded] = useState(new Set<number>());
  const [limit, setLimit] = useState(500);
  const result = useMemo(
    () =>
      associationDiff(
        baseline ? `# ${baseline.title}\n\n${baseline.explanation}` : "",
        `# ${title}\n\n${explanation}`,
      ),
    [baseline, title, explanation],
  );
  const groups = useMemo(() => comparisonGroups(result.lines), [result]);
  const displayed = useMemo(
    () =>
      groups.flatMap((group) =>
        group.collapsed && !expanded.has(group.start)
          ? [{ ...group, hidden: true }]
          : Array.from({ length: group.end - group.start }, (_, offset) => ({
              start: group.start + offset,
              end: group.start + offset + 1,
              hidden: false,
            })),
      ),
    [groups, expanded],
  );
  return (
    <section className="association-comparison" aria-label="关联文档新旧对比">
      <div className="association-comparison-toolbar">
        <div role="tablist" aria-label="关联预览显示方式">
          <button
            role="tab"
            disabled={baseline === undefined}
            aria-selected={view === "diff"}
            onClick={() => setView("diff")}
          >
            新旧对比
          </button>
          <button
            role="tab"
            aria-selected={view === "markdown"}
            onClick={() => setView("markdown")}
          >
            Markdown 预览
          </button>
        </div>
        <span className="association-diff-added">+{result.added} 行</span>
        <span className="association-diff-removed">−{result.removed} 行</span>
      </div>
      {baseline === undefined && (
        <p className="muted">此预览未提供已保存版本。</p>
      )}
      {baseline === null && (
        <p className="muted">当前没有已保存关联，本次内容为新增。</p>
      )}
      {view === "diff" ? (
        <div role="tabpanel" aria-label="新旧对比">
          <div className="association-diff-head">
            <strong>
              已保存版本{baseline ? ` · v${baseline.binding_version}` : ""}
            </strong>
            <strong>本次生成 · 尚未保存</strong>
          </div>
          {baseline !== undefined && !result.added && !result.removed && (
            <p>正文与已保存版本一致。</p>
          )}
          {result.coarse && (
            <p className="muted">改动较大，按完整替换段落显示。</p>
          )}
          <div className="association-diff-lines">
            {displayed.slice(0, limit).map((group) =>
              group.hidden ? (
                <button
                  className="association-diff-fold"
                  key={`fold-${group.start}`}
                  onClick={() =>
                    setExpanded((old) => new Set([...old, group.start]))
                  }
                >
                  展开 {group.end - group.start} 行未变化内容
                </button>
              ) : (
                (() => {
                  const row = result.lines[group.start];
                  return (
                    <div
                      className="association-diff-row"
                      key={`line-${group.start}`}
                    >
                      <div
                        className={
                          row.changed && row.before !== undefined
                            ? "association-diff-deleted"
                            : ""
                        }
                      >
                        <span
                          className="association-diff-number"
                          aria-hidden="true"
                        >
                          {row.oldLine}
                        </span>
                        <span
                          className="association-diff-sign"
                          aria-hidden="true"
                        >
                          {row.changed && row.before !== undefined ? "−" : ""}
                        </span>
                        <code>{row.before}</code>
                      </div>
                      <div
                        className={
                          row.changed && row.after !== undefined
                            ? "association-diff-inserted"
                            : ""
                        }
                      >
                        <span
                          className="association-diff-number"
                          aria-hidden="true"
                        >
                          {row.newLine}
                        </span>
                        <span
                          className="association-diff-sign"
                          aria-hidden="true"
                        >
                          {row.changed && row.after !== undefined ? "+" : ""}
                        </span>
                        <code>{row.after}</code>
                      </div>
                    </div>
                  );
                })()
              ),
            )}
          </div>
          {displayed.length > limit && (
            <button
              className="association-diff-fold"
              onClick={() => setLimit((n) => n + 500)}
            >
              继续显示后续差异（剩余 {displayed.length - limit} 行）
            </button>
          )}
        </div>
      ) : (
        <div role="tabpanel" aria-label="Markdown 预览">
          <RichMarkdown references={sections} onReference={onReference}>
            {explanation}
          </RichMarkdown>
        </div>
      )}
      {!!baseline?.sections?.length && (
        <p className="association-comparison-reference-count">
          引用章节：已保存 {baseline.sections.length} → 本次 {sections.length}
          。行数和引用数量用于辅助核对，不代表内容质量评分。
        </p>
      )}
    </section>
  );
}
