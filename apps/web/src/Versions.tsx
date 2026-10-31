import { useEffect, useRef, useState } from "react";
import { api, date, mutation } from "./api";
import { Button, Empty, Notice, Panel, Tag } from "./components";
import {
  ReadScope,
  usePermission,
  useReadScope,
  useWorkspace,
} from "./context";
import { NewDraftButton } from "./DraftActions";
import { ImpactPaths } from "./ImpactPaths";
import type { Dict } from "./types";

export function Versions() {
  const { w, run, busy } = useWorkspace();
  const canSave = usePermission("knowledge:edit");
  const canSetDefault = usePermission("project:manage");
  const canExport = usePermission("knowledge:export");
  const scope = useReadScope();
  const [from, setFrom] = useState(w.releases[0]?.id || "");
  const [to, setTo] = useState(w.selected_analysis_set?.id || "");
  const [result, setResult] = useState<{ key: string; value: Dict } | null>(
    null,
  );
  const [error, setError] = useState("");
  const [comparing, setComparing] = useState(false);
  const [note, setNote] = useState("");
  const request = useRef<AbortController | null>(null);
  const key = JSON.stringify([scope, from, to]);
  const current = useRef(key);
  current.current = key;
  useEffect(() => {
    request.current?.abort();
    setResult(null);
    setError("");
    setComparing(false);
    return () => request.current?.abort();
  }, [key]);
  useEffect(() => {
    setTo((previous) =>
      w.analysis_sets.some((row) => row.id === previous)
        ? previous
        : w.selected_analysis_set?.id || "",
    );
    setFrom((previous) =>
      w.releases.some((row) => row.id === previous)
        ? previous
        : w.releases[0]?.id || "",
    );
  }, [w.analysis_sets, w.releases, w.selected_analysis_set?.id]);
  async function compare() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const requested = key;
    setComparing(true);
    setError("");
    try {
      const value = await api<Dict>(
        `/projects/${w.project.id}/releases/${from}/diff?to_analysis_set_id=${to}`,
        { signal: controller.signal },
      );
      if (!controller.signal.aborted && current.current === requested)
        setResult({ key: requested, value });
    } catch (e) {
      if (!controller.signal.aborted && current.current === requested)
        setError((e as Error).message);
    } finally {
      if (!controller.signal.aborted && current.current === requested)
        setComparing(false);
    }
  }
  const report = result?.key === key ? result.value : null;
  const changes = (report?.changes || []) as Dict[];
  return (
    <div className="configuration-stack">
      <Panel title="知识版本" action={<NewDraftButton />}>
        <Notice>
          保存当前知识为固定版本，后续可比较或回退查看。编辑固定版本前创建草稿。
        </Notice>
        {canSave && w.selected_analysis_set?.state !== "frozen" && (
          <div className="configuration-stack">
            <label>
              版本说明
              <input
                value={note}
                maxLength={2000}
                onChange={(e) => setNote(e.target.value)}
                placeholder="可选，记录本次变化"
              />
            </label>
            <Button
              kind="primary"
              disabled={busy || !w.selected_analysis_set}
              onClick={() =>
                void run(
                  () =>
                    api(
                      `/api/v2/projects/${w.project.id}/snapshots`,
                      mutation("POST", {
                        analysis_set_id: w.selected_analysis_set!.id,
                        change_note: note || "保存知识版本",
                      }),
                    ),
                  "知识版本已保存",
                )
              }
            >
              保存知识版本
            </Button>
          </div>
        )}
        {w.releases.map((row) => (
          <article key={row.id} className="proposal-card">
            <div className="row between">
              <h3>{row.name || row.release_no}</h3>
              <Tag>固定版本</Tag>
            </div>
            <p>{date(row.published_at)}</p>
            <code>{row.manifest_sha256}</code>
            <div className="configuration-actions">
              {canSetDefault && (
                <Button
                  kind="small"
                  onClick={() =>
                    void run(
                      () =>
                        api(
                          `/projects/${w.project.id}`,
                          mutation(
                            "PATCH",
                            { current_release_id: row.id },
                            w.project.row_version,
                          ),
                        ),
                      "默认版本已切换",
                    )
                  }
                >
                  设为默认版本
                </Button>
              )}
              {canExport &&
                ["markdown_bundle", "pdf", "vksbundle"].map((format) => (
                  <Button
                    key={format}
                    kind="small"
                    onClick={() =>
                      void run(
                        () =>
                          api(
                            `/projects/${w.project.id}/exports`,
                            mutation("POST", {
                              release_id: row.id,
                              format,
                              include_source: true,
                            }),
                          ),
                        "导出任务已创建",
                      )
                    }
                  >
                    导出{" "}
                    {format === "markdown_bundle"
                      ? "Markdown"
                      : format === "pdf"
                        ? "PDF"
                        : "可回导包"}
                  </Button>
                ))}
            </div>
          </article>
        ))}
        {!w.releases.length && (
          <Empty
            title="尚未保存固定版本"
            description="当前草稿中的知识可直接阅读和修改。"
          />
        )}
      </Panel>
      <Panel title="比较版本">
        <div className="version-summary">
          <div className="version-compare">
            <label>
              原版本
              <select
                aria-label="原版本"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
              >
                <option value="">选择固定版本</option>
                {w.releases.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name || row.release_no}
                  </option>
                ))}
              </select>
            </label>
            <label>
              目标基线
              <select
                aria-label="目标基线"
                value={to}
                onChange={(e) => setTo(e.target.value)}
              >
                {w.analysis_sets.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name}
                  </option>
                ))}
              </select>
            </label>
            <Button
              disabled={!from || !to || comparing}
              onClick={() => void compare()}
            >
              {comparing ? "正在比较版本…" : "比较版本"}
            </Button>
          </div>
        </div>
        {error && <Notice tone="red">{error}</Notice>}
        {report && (
          <>
            <div role="status" aria-label="本次版本对比">
              对比：
              {w.releases.find((row) => row.id === from)?.release_no ||
                from} →{" "}
              {w.analysis_sets.find((row) => row.id === to)?.name || to}
            </div>
            <p>{changes.length} 个对象变化</p>
            <div className="diff-changes">
              {changes.map((change, index) => (
                <div key={index} className="proposal-card">
                  <Tag>{String(change.change_kind)}</Tag>
                  <code>{String(change.entity_id)}</code>
                  <p>
                    原修订 {String(change.old_revision_id || "无")} → 新修订{" "}
                    {String(change.new_revision_id || "无")}
                  </p>
                </div>
              ))}
            </div>
            <ReadScope>
              <ImpactPaths
                report={{
                  ...report,
                  from_release_id: from,
                  to_analysis_set_id: to,
                }}
              />
            </ReadScope>
          </>
        )}
      </Panel>
    </div>
  );
}
