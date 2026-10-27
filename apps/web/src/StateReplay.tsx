import { Surface as Panel } from "./design-system/Surface";
import { useEffect, useState } from "react";
import { api } from "./api";
import { Notice, Tag } from "./components";
import { useWorkspace } from "./context";
import { ObservedStateSurface } from "./CaptureResults";
import { TrajectoryComparison } from "./TrajectoryComparison";
type State = {
  stateKey: string;
  label: string;
  evidenceOrigin: string;
  pageKey?: string;
  actionKey?: string;
  captureRef?: string;
  snapshotIndex?: number;
};
type Scenario = {
  entity_id: string;
  revision_id: string;
  name: string;
  states: State[];
  transitions: {
    fromState: string;
    toState: string;
    conditionLabel?: string;
  }[];
};
export function StateReplay() {
  const { w, baseline, explanation, navigateExplanation } = useWorkspace();
  const [scenarios, setScenarios] = useState<Scenario[]>([]),
    [compare, setCompare] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setScenarios([]);
    setCompare("");
    setError("");
    const q = new URLSearchParams();
    if (w.selected_release) q.set("release_id", w.selected_release.id);
    else if (w.selected_analysis_set)
      q.set("analysis_set_id", w.selected_analysis_set.id);
    void api<{ scenarios: Scenario[] }>(
      `/api/v2/projects/${w.project.id}/state-replay?${q}`,
      { signal: controller.signal },
    )
      .then((r) => setScenarios(r.scenarios))
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [w.project.id, baseline, w.selected_analysis_set?.selection_version]);
  const scenario =
    scenarios.find((s) => s.entity_id === explanation.scenarioId) ||
    scenarios.find((s) => s.states.length);
  if (!scenario?.states.length && !error) return <TrajectoryComparison />;
  const selected =
    scenario?.states.find((s) => s.stateKey === explanation.stateKey) ||
    scenario?.states[0];
  const other = scenario?.states.find((s) => s.stateKey === compare);
  function surface(state: State) {
    return (
      <div className="v2-replay-state">
        <h3>{state.label}</h3>
        <Tag>{state.evidenceOrigin}</Tag>
        {state.evidenceOrigin === "observed" && state.captureRef ? (
          <ObservedStateSurface
            ariaLabel={`对比状态快照：${state.label}`}
            projectId={w.project.id}
            captureId={state.captureRef}
            index={state.snapshotIndex || 0}
            analysisId={w.selected_analysis_set?.id}
            releaseId={w.selected_release?.id}
          />
        ) : (
          <p className="muted">
            {state.evidenceOrigin === "simulated" ? "模拟状态" : "声明状态"} ·{" "}
            {w.pages.find((p) => p.stable_key === state.pageKey)?.name ||
              "未绑定页面快照"}
            。没有采集画面时保留缺口。
          </p>
        )}
      </div>
    );
  }
  return (
    <>
      <Panel title="被动状态与分支时间轴" action={<Tag>讲解 / 已保存状态</Tag>}>
        {error && <Notice tone="red">{error}</Notice>}
        {scenario && (
          <>
            <div className="row wrap">
              <label>
                场景
                <select
                  aria-label="回放场景"
                  value={scenario.entity_id}
                  onChange={(e) => {
                    setCompare("");
                    const target = scenarios.find(
                      (s) => s.entity_id === e.target.value,
                    );
                    navigateExplanation({
                      scenarioId: e.target.value,
                      stateKey: target?.states[0]?.stateKey || "",
                      followTarget: false,
                    });
                  }}
                >
                  {scenarios
                    .filter((s) => s.states.length)
                    .map((s) => (
                      <option value={s.entity_id} key={s.entity_id}>
                        {s.name}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                对比状态
                <select
                  aria-label="对比状态"
                  value={compare}
                  onChange={(e) => setCompare(e.target.value)}
                >
                  <option value="">不对比</option>
                  {scenario.states.map((s) => (
                    <option value={s.stateKey} key={s.stateKey}>
                      {s.label} · {s.evidenceOrigin}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="v2-state-timeline">
              {scenario.states.map((state) => (
                <button
                  key={state.stateKey}
                  aria-pressed={selected?.stateKey === state.stateKey}
                  onClick={() => {
                    const action = w.actions.find(
                      (a) => a.stable_key === state.actionKey,
                    );
                    const page = w.pages.find(
                      (p) => p.stable_key === state.pageKey,
                    );
                    navigateExplanation({
                      scenarioId: scenario.entity_id,
                      stateKey: state.stateKey,
                      ...(action ? { actionId: action.id } : {}),
                      ...(page ? { pageId: page.id } : {}),
                      followTarget: false,
                    });
                  }}
                >
                  <strong>{state.label}</strong>
                  <small>{state.evidenceOrigin}</small>
                </button>
              ))}
            </div>
            <div className={`v2-state-compare ${other ? "comparing" : ""}`}>
              {selected && surface(selected)}
              {other && surface(other)}
            </div>
            <p className="muted">
              切换只读取保存状态；未请求业务系统。运行观察和人工绑定分别保留，未知链路不自动补全。
            </p>
          </>
        )}
      </Panel>
      <TrajectoryComparison />
    </>
  );
}
