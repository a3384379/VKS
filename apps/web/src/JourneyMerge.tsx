import { useState } from "react";
import { api, mutation } from "./api";
import { Button, Modal, Notice } from "./components";
import { useWorkspace } from "./context";
import type { JourneyDSL, JourneyRevision } from "./journeys";
import { mergeJourney } from "./journey-editing";

export function JourneyMerge({
  active,
  base,
  local,
  onApply,
}: {
  active: JourneyRevision;
  base: JourneyDSL;
  local: JourneyDSL;
  onApply: (dsl: JourneyDSL, remote: JourneyRevision) => void;
}) {
  const { w } = useWorkspace();
  const [remote, setRemote] = useState<JourneyRevision | null>(null),
    [choices, setChoices] = useState<Record<string, "local" | "remote">>({}),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function load() {
    setError("");
    setBusy(true);
    try {
      const rows = await api<JourneyRevision[]>(
        `/api/v2/projects/${w.project.id}/journeys?analysis_set_id=${w.selected_analysis_set?.id}`,
      );
      const row = rows.find((r) => r.journey_id === active.journey_id);
      if (!row) throw new Error("当前版本已不可见，请核对权限与基线");
      mergeJourney(base, local, row.dsl);
      setRemote(row);
      setChoices({});
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const result = remote ? mergeJourney(base, local, remote.dsl, choices) : null;
  async function apply() {
    if (!result || !remote) return;
    setBusy(true);
    setError("");
    try {
      const normalized = await api<{ dsl: JourneyDSL }>(
        `/api/v2/projects/${w.project.id}/journeys/validate`,
        mutation("POST", {
          analysis_set_id: w.selected_analysis_set?.id,
          dsl: result.merged,
        }),
      );
      onApply(normalized.dsl, remote);
      setRemote(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Button disabled={busy} onClick={() => void load()}>
        比较并合并更新
      </Button>
      {error && !remote && <Notice tone="red">{error}</Notice>}
      {remote && result && (
        <Modal title="合并流程更新" onClose={() => setRemote(null)}>
          <p>
            互不冲突的编辑已合并。每处冲突由你选择，绑定及回放引用整体保留；校验后成为本地待保存草稿。
          </p>
          {error && <Notice tone="red">{error}</Notice>}
          {result.conflicts.length ? (
            result.conflicts.map((c) => (
              <fieldset key={c.path}>
                <legend>{c.path}</legend>
                <p>共同版本：{JSON.stringify(c.base) ?? "无"}</p>
                <p>本地：{JSON.stringify(c.local) ?? "删除"}</p>
                <p>远端：{JSON.stringify(c.remote) ?? "删除"}</p>
                <label>
                  采用编辑
                  <select
                    aria-label={`合并选择 ${c.path}`}
                    value={choices[c.path] || ""}
                    onChange={(e) =>
                      setChoices((rows) => ({
                        ...rows,
                        [c.path]: e.target.value as "local" | "remote",
                      }))
                    }
                  >
                    <option value="">请选择</option>
                    <option value="local">保留本地</option>
                    <option value="remote">采用远端</option>
                  </select>
                </label>
              </fieldset>
            ))
          ) : (
            <Notice>没有冲突，可以保留两边的编辑。</Notice>
          )}
          <Button
            kind="primary"
            disabled={busy || result.conflicts.some((c) => !choices[c.path])}
            onClick={() => void apply()}
          >
            校验并应用为待保存草稿
          </Button>
        </Modal>
      )}
    </>
  );
}
