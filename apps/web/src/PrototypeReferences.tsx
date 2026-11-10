import { useEffect, useState } from "react";
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
    return () => controller.abort();
  }, [scope, url]);
  if (!items.length && !error) return null;
  return (
    <section aria-label="关联的原型动作">
      <h3>关联的原型动作</h3>
      {error && <p role="status">原型关联暂时无法读取。</p>}
      {items.map((row) => (
        <Button
          key={`${row.page_id}:${row.state_key}:${row.action_id}`}
          onClick={() => {
            navigateExplanation({
              pageId: row.page_id,
              stateKey: row.state_key,
              triggerStateKey: row.state_key,
              actionId: row.action_id,
              followTarget: false,
            });
            navigate("workbench");
          }}
        >
          {row.page_name} · {row.state_name} · {row.action_name}
        </Button>
      ))}
      {cursor && (
        <Button
          onClick={() => {
            const request = new AbortController();
            void read(request.signal, cursor).then(
              (data) => {
                setItems((rows) => [...rows, ...data.references]);
                setCursor(data.next_cursor);
              },
              () => setError(true),
            );
          }}
        >
          更多关联
        </Button>
      )}
    </section>
  );
}
