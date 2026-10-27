import { useState } from "react";
import { api, mutation } from "./api";
import { Button, Notice } from "./components";
import { usePermission, useWorkspace } from "./context";
import type { Capture } from "./CaptureResults";
import type { JourneyRevision } from "./journeys";

export function CaptureJourneyBuilder({
  scenario,
  captures,
  hasStates,
}: {
  scenario: { scenario_id: string; entity_revision_id: string };
  captures: Capture[];
  hasStates: boolean;
}) {
  const { w, run, navigate, setSelected } = useWorkspace();
  const trace = usePermission("trace:read");
  const [selected, setChecked] = useState<string[]>([]),
    [error, setError] = useState("");
  const options = captures.filter(
    (c) => c.state === "succeeded" && c.scenario_id === scenario.scenario_id,
  );
  async function propose(ids: string[]) {
    setError("");
    const result = await run(async () => {
      try {
        return await api<JourneyRevision>(
          `/api/v2/projects/${w.project.id}/journey-proposals`,
          mutation(
            "POST",
            {
              analysis_set_id: w.selected_analysis_set?.id,
              scenario_id: scenario.scenario_id,
              scenario_revision_id: scenario.entity_revision_id,
              capture_ids: ids,
            },
            w.selected_analysis_set?.selection_version,
          ),
        );
      } catch (e) {
        setError((e as Error).message);
        throw e;
      }
    }, "已生成独立流程候选，原场景和采集保持不变");
    if (result) {
      setSelected(result.journey_id);
      navigate("journeys");
    }
  }
  return (
    <details className="v2-capture-builder">
      <summary>从已保存状态构建流程候选</summary>
      <p className="muted">
        录制只提供观察快照，动作顺序和分支条件仍需人员核对。生成操作不会执行任何录制步骤。
      </p>
      {error && <Notice tone="red">{error}</Notice>}
      {trace && options.length > 0 && (
        <fieldset>
          <legend>选择有限录制轨迹（最多 10 条）</legend>
          {options.map((capture, i) => (
            <label key={capture.id}>
              <input
                type="checkbox"
                aria-label={`选择录制轨迹 ${i + 1}`}
                checked={selected.includes(capture.id)}
                onChange={(e) =>
                  setChecked((ids) =>
                    e.target.checked
                      ? [...ids, capture.id]
                      : ids.filter((id) => id !== capture.id),
                  )
                }
              />
              轨迹 {i + 1} · {capture.result.snapshots?.length || 0} 个脱敏状态
              · {capture.source_alignment}
            </label>
          ))}
        </fieldset>
      )}
      <div className="row wrap">
        <Button disabled={!hasStates} onClick={() => void propose([])}>
          从状态图生成流程
        </Button>
        <Button
          kind="primary"
          disabled={!trace || !selected.length || selected.length > 10}
          onClick={() => void propose(selected)}
        >
          从选中录制生成流程
        </Button>
      </div>
    </details>
  );
}
