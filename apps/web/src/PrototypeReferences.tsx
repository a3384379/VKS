import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Button } from "./components";
import { useWorkspace } from "./context";
import { prototypeScope } from "./prototype";
import type { Entity } from "./types";

type Reference = {
  page_id: string;
  page_name: string;
  state_key: string;
  state_name: string;
  action_id: string;
  action_name: string;
};

export function PrototypeReferences({ section }: { section: Entity }) {
  const { w, navigateExplanation, navigate } = useWorkspace();
  const [items, setItems] = useState<Reference[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  const pagination = useRef<AbortController | null>(null);
  const url = `/api/v3/projects/${encodeURIComponent(w.project.id)}/sections/${encodeURIComponent(section.stable_key)}/prototype-references`;
  const scope = prototypeScope(w).toString();
  function read(signal: AbortSignal, after?: string) {
    const query = new URLSearchParams(scope);
    query.set("limit", "20");
    if (after) query.set("after", after);
    return api<{ references: Reference[]; next_cursor: string | null }>(
      `${url}?${query}`,
      { signal },
    );
  }
  useEffect(() => {
    if (!scope) return;
    setError(false);
    const controller = new AbortController();
    read(controller.signal).then(
      (data) => {
        if (controller.signal.aborted) return;
        setItems(data.references);
        setCursor(data.next_cursor);
      },
      () => {
        if (!controller.signal.aborted) setError(true);
      },
    );
    return () => {
      controller.abort();
      pagination.current?.abort();
    };
  }, [scope, url, retry]);
  async function loadMore(after: string) {
    if (pagination.current) return;
    const request = new AbortController();
    pagination.current = request;
    setLoading(true);
    setError(false);
    try {
      const data = await read(request.signal, after);
      if (request.signal.aborted) return;
      setItems((rows) => [...rows, ...data.references]);
      setCursor(data.next_cursor);
    } catch {
      if (!request.signal.aborted) setError(true);
    } finally {
      if (pagination.current === request) pagination.current = null;
      if (!request.signal.aborted) setLoading(false);
    }
  }
  if (!items.length && !error) return null;
  return (
    <section aria-label="关联的原型动作">
      <h3>关联的原型动作</h3>
      {error && (
        <p role="status">
          原型关联暂时无法读取。
          {!cursor && (
            <Button onClick={() => setRetry((count) => count + 1)}>
              重试关联
            </Button>
          )}
        </p>
      )}
      {items.map((row) => (
        <Button
          key={`${row.page_id}:${row.state_key}:${row.action_id}`}
          onClick={() => {
            if (
              navigateExplanation({
                pageId: row.page_id,
                stateKey: row.state_key,
                triggerStateKey: row.state_key,
                actionId: row.action_id,
                followTarget: false,
              })
            )
              navigate("workbench");
          }}
        >
          {row.page_name} · {row.state_name} · {row.action_name}
        </Button>
      ))}
      {cursor && (
        <Button disabled={loading} onClick={() => void loadMore(cursor)}>
          更多关联
        </Button>
      )}
    </section>
  );
}
