import { useRef, useState } from "react";
import { api } from "./api";
import { Button, Notice } from "./components";
import type { Dict } from "./types";

export function TaskStages({
  jobId,
  artifacts,
}: {
  jobId: string;
  artifacts: { stage: string; sha256: string; size_bytes: number }[];
}) {
  const [selected, setSelected] = useState<Dict | null>(null),
    [error, setError] = useState("");
  const request = useRef(0);
  return (
    <details className="v2-task-stages">
      <summary>已保存阶段产物 · {artifacts.length} 项</summary>
      <p className="muted">
        按准确输入和源码版本保存；读取时重新核对权限与摘要。工具步骤中断后可定位已完成产物。
      </p>
      <div className="row wrap">
        {artifacts.map((item) => (
          <Button
            key={item.sha256}
            onClick={() => {
              setSelected(null);
              setError("");
              const current = ++request.current;
              void api<Dict>(
                `/api/v2/knowledge-tasks/${jobId}/stage-artifacts/${item.sha256}`,
              )
                .then((value) => {
                  if (request.current === current) setSelected(value);
                })
                .catch((e) => {
                  if (request.current === current) setError(e.message);
                });
            }}
          >
            {item.stage} · {Math.ceil(item.size_bytes / 1024)} KB
          </Button>
        ))}
      </div>
      {error && <Notice tone="red">{error}</Notice>}
      {selected && (
        <pre className="json-preview">{JSON.stringify(selected, null, 2)}</pre>
      )}
    </details>
  );
}
