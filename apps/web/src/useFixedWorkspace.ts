import { useEffect, useState } from "react";
import { api } from "./api";
import { useReadScope, useWorkspace } from "./context";
import type { Workspace } from "./types";

export function useFixedWorkspace(baseline: string, identity = "") {
  const { w } = useWorkspace();
  const scope = useReadScope();
  const key = `${scope}:${baseline}:${identity}`;
  const [result, setResult] = useState<{
      key: string;
      value: Workspace;
    } | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    if (!baseline) return;
    const controller = new AbortController();
    setResult(null);
    setError("");
    const query = new URLSearchParams(
      baseline.startsWith("release:")
        ? { release_id: baseline.slice(8) }
        : { analysis_set_id: baseline.slice(6) },
    );
    void api<Workspace>(`/projects/${w.project.id}/workspace?${query}`, {
      signal: controller.signal,
    })
      .then((value) => {
        if (!controller.signal.aborted) setResult({ key, value });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [w.project.id, key, baseline]);
  return { workspace: result?.key === key ? result.value : null, error };
}
