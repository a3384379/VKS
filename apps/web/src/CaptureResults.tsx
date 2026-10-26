import { useEffect, useState } from "react";
import { Button, Modal, Notice, SafePrototype, Tag } from "./components";
import { api } from "./api";

export type Capture = {
  id: string;
  scenario_id?: string;
  scenario_revision_id: string;
  analysis_set_id?: string;
  state: string;
  source_alignment: string;
  redaction_version?: string;
  result: {
    snapshots?: Array<{
      name: string;
      step_key?: string;
      dom_artifact_id: string;
      png_artifact_id: string;
    }>;
    requests?: Array<{
      request_id: string;
      method: string;
      path: string;
      status: number;
    }>;
    blocked?: unknown[];
  };
};

export function ObservedStateSurface({
  projectId,
  captureId,
  index,
  analysisId,
  releaseId,
}: {
  projectId: string;
  captureId: string;
  index: number;
  analysisId?: string;
  releaseId?: string;
}) {
  const query = new URLSearchParams(
    releaseId
      ? { release_id: releaseId }
      : analysisId
        ? { analysis_set_id: analysisId }
        : {},
  );
  const key = `${projectId}/${captureId}/${query}`;
  const [loaded, setLoaded] = useState<{ key: string; value: Capture } | null>(
    null,
  );
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void api<Capture>(`/projects/${projectId}/captures/${captureId}?${query}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setLoaded({ key, value });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [key]);
  const snapshot =
    loaded?.key === key ? loaded.value.result.snapshots?.[index] : null;
  return (
    <div aria-label="当前观察状态快照">
      <Notice>
        运行观察 · 已脱敏 · 页面和动作关联为人工声明，未核实后端因果。
      </Notice>
      {error ? (
        <Notice tone="red">{error}</Notice>
      ) : snapshot ? (
        <figure className="scenario-capture">
          <img
            src={`/api/v1/captures/${captureId}/artifacts/${snapshot.png_artifact_id}`}
            alt={snapshot.name + " · 当前观察状态"}
          />
        </figure>
      ) : (
        <p>正在读取获准的观察快照…</p>
      )}
    </div>
  );
}

export function CaptureResults({
  capture,
  onClose,
  onBind,
}: {
  capture: Capture;
  onClose: () => void;
  onBind?: (action: boolean, index: number) => void;
}) {
  const [index, setIndex] = useState(0);
  const snapshot = capture.result.snapshots?.[index];
  const source = snapshot
    ? `/api/v1/captures/${capture.id}/artifacts/${snapshot.dom_artifact_id}`
    : "";
  const [dom, setDom] = useState<{ source: string; content: string } | null>(
    null,
  );
  const [error, setError] = useState("");
  useEffect(() => {
    if (!source) return;
    const controller = new AbortController();
    setError("");
    void fetch(source, {
      credentials: "same-origin",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("采集制品不可用或已失去访问权限");
        const content = await response.text();
        if (!controller.signal.aborted) setDom({ source, content });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [source]);
  return (
    <Modal title="采集结果" onClose={onClose} className="scenario-modal">
      <Notice>
        这是已脱敏的测试环境观察。页面只被动展示；源码对齐：
        {capture.source_alignment === "unknown"
          ? "未核对"
          : capture.source_alignment}
        。不代表后端因果已核实。
      </Notice>
      <div className="row wrap">
        <Tag value={capture.state} />
        <span>{capture.redaction_version || "已脱敏"}</span>
      </div>
      {error && <Notice tone="red">{error}</Notice>}
      {snapshot && (
        <>
          <label>
            采集快照
            <select
              aria-label="采集快照"
              value={index}
              onChange={(e) => setIndex(Number(e.target.value))}
            >
              {capture.result.snapshots!.map((item, i) => (
                <option value={i} key={i}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <figure className="scenario-capture">
            <img
              src={`/api/v1/captures/${capture.id}/artifacts/${snapshot.png_artifact_id}`}
              alt={snapshot.name + " · 已脱敏采集截图"}
            />
          </figure>
          <details>
            <summary>查看安全 DOM 快照</summary>
            {dom?.source === source && (
              <SafePrototype
                html={dom.content}
                actions={[]}
                onSelect={() => {}}
              />
            )}
          </details>
        </>
      )}
      <details>
        <summary>请求摘要（不含认证头和报文正文）</summary>
        <table>
          <thead>
            <tr>
              <th>方法</th>
              <th>路径</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {capture.result.requests?.map((request) => (
              <tr key={request.request_id}>
                <td>{request.method}</td>
                <td>{request.path}</td>
                <td>{request.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p>策略阻断：{capture.result.blocked?.length || 0}</p>
      </details>
      {onBind && snapshot && (
        <div className="row">
          <Button onClick={() => onBind(false, index)}>加入讲解状态</Button>
          <Button onClick={() => onBind(true, index)}>关联已有动作</Button>
        </div>
      )}
    </Modal>
  );
}
