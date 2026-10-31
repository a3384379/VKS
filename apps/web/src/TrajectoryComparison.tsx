import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Button, Notice, Panel, Tag } from "./components";
import { useWorkspace } from "./context";
import { ObservedStateSurface } from "./CaptureResults";
type Option = { key: string; title: string; kind: string };
type State = {
  key: string;
  label: string;
  evidence_origin: string;
  capture_id?: string;
  snapshot_index?: number;
};
type Side = {
  title: string;
  role: string;
  preconditions: string[];
  expected_results: string[];
  states: State[];
  coverage: string;
  state_count: number;
};
type Result = {
  left: Side;
  right: Side;
  differences: {
    role_changed: boolean;
    preconditions_changed: boolean;
    shared_declared_actions: string[];
  };
  comparison_sha256: string;
  unknowns: string[];
};
export function TrajectoryComparison() {
  const { w } = useWorkspace();
  const key = `${w.project.id}:${w.selected_analysis_set?.id}:${w.selected_analysis_set?.selection_version}:${w.selected_release?.id}`;
  const requestSequence = useRef(0);
  const [options, setOptions] = useState<Option[]>([]),
    [left, setLeft] = useState(""),
    [right, setRight] = useState(""),
    [error, setError] = useState(""),
    [report, setReport] = useState<{ key: string; value: Result } | null>(null),
    [indices, setIndices] = useState([0, 0]),
    [busy, setBusy] = useState(false);
  const pairKey = `${key}:${left}:${right}`;
  const current = useRef(pairKey);
  current.current = pairKey;
  const scope = new URLSearchParams(
    w.selected_release
      ? { release_id: w.selected_release.id }
      : w.selected_analysis_set
        ? { analysis_set_id: w.selected_analysis_set.id }
        : {},
  );
  useEffect(() => {
    ++requestSequence.current;
    const controller = new AbortController();
    setOptions([]);
    setReport(null);
    setError("");
    setBusy(false);
    void api<{ items: Option[] }>(
      `/api/v2/projects/${w.project.id}/trajectory-options?${scope}`,
      { signal: controller.signal },
    )
      .then((r) => {
        if (!controller.signal.aborted) {
          setOptions(r.items);
          setLeft(r.items[0]?.key || "");
          setRight(r.items[1]?.key || r.items[0]?.key || "");
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [key]);
  async function compare() {
    const requested = pairKey;
    const sequence = ++requestSequence.current;
    setBusy(true);
    setError("");
    try {
      const q = new URLSearchParams(scope);
      q.set("left", left);
      q.set("right", right);
      const value = await api<Result>(
        `/api/v2/projects/${w.project.id}/trajectory-comparison?${q}`,
      );
      if (
        current.current === requested &&
        requestSequence.current === sequence
      ) {
        setReport({ key: requested, value });
        setIndices([0, 0]);
      }
    } catch (e) {
      if (current.current === requested && requestSequence.current === sequence)
        setError((e as Error).message);
    } finally {
      if (current.current === requested && requestSequence.current === sequence)
        setBusy(false);
    }
  }
  if (!w.scenarios.length) return null;
  const result = report?.key === pairKey ? report.value : null;
  return (
    <Panel
      title="角色、前置条件与双轨迹对比"
      action={<Tag>已保存轨迹 · 被动比较</Tag>}
    >
      {error && <Notice tone="red">{error}</Notice>}
      <div className="row wrap">
        {(
          [
            ["左侧轨迹", left, setLeft],
            ["右侧轨迹", right, setRight],
          ] as const
        ).map(([title, value, set]) => (
          <label key={title}>
            {title}
            <select
              aria-label={title}
              value={value}
              onChange={(e) => {
                ++requestSequence.current;
                set(e.target.value);
                setReport(null);
                setBusy(false);
                setError("");
              }}
            >
              {options.map((option) => (
                <option key={option.key} value={option.key}>
                  {option.title} ·{" "}
                  {option.kind === "capture" ? "运行观察" : "声明状态图"}
                </option>
              ))}
            </select>
          </label>
        ))}
        <Button
          disabled={busy || !left || !right}
          onClick={() => void compare()}
        >
          {busy ? "正在比较…" : "比较已保存轨迹"}
        </Button>
      </div>
      {result && (
        <>
          <div className="v2-state-compare comparing">
            {[result.left, result.right].map((side, i) => {
              const state = side.states[indices[i]];
              return (
                <section key={i} className="v2-replay-state">
                  <h3>{side.title}</h3>
                  <p>角色：{side.role}</p>
                  <p>前置条件：{side.preconditions.join("；") || "未声明"}</p>
                  <p>
                    预期结果：{side.expected_results.join("；") || "未声明"}
                  </p>
                  <small>
                    已保存 {side.state_count} 个状态 ·{" "}
                    {side.coverage === "finite_observed_trajectory"
                      ? "有限运行观察"
                      : "场景声明"}
                  </small>
                  <label>
                    查看状态
                    <select
                      aria-label={`${i ? "右" : "左"}侧对比快照`}
                      value={indices[i]}
                      onChange={(e) =>
                        setIndices((rows) =>
                          rows.map((value, j) =>
                            j === i ? Number(e.target.value) : value,
                          ),
                        )
                      }
                    >
                      {side.states.map((s, index) => (
                        <option key={s.key} value={index}>
                          {s.label} · {s.evidence_origin}
                        </option>
                      ))}
                    </select>
                  </label>
                  {state?.capture_id ? (
                    <ObservedStateSurface
                      ariaLabel={`${i ? "右" : "左"}侧轨迹观察快照`}
                      projectId={w.project.id}
                      captureId={state.capture_id}
                      index={state.snapshot_index || 0}
                      analysisId={w.selected_analysis_set?.id}
                      releaseId={w.selected_release?.id}
                    />
                  ) : (
                    <p>
                      {state?.label || "没有已保存状态"} ·{" "}
                      {state?.evidence_origin || "unknown"}
                    </p>
                  )}
                </section>
              );
            })}
          </div>
          <p>
            角色声明{result.differences.role_changed ? "不同" : "相同"}
            ；前置条件
            {result.differences.preconditions_changed ? "不同" : "相同"}
            ；共同声明动作 {
              result.differences.shared_declared_actions.length
            }{" "}
            个。
          </p>
          <Notice>{result.unknowns.join("；")}</Notice>
          <small className="v2-hash">
            固定比较摘要：{result.comparison_sha256}
          </small>
        </>
      )}
      <p className="muted">
        录制检索最近 100
        条匹配来源的窗口。没有覆盖的轨迹保留未知，不执行任何业务动作。
      </p>
    </Panel>
  );
}
