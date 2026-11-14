import { useEffect, useRef, useState } from "react";
import { BookOpen, LoaderCircle } from "lucide-react";
import { api, mutation } from "../../web/src/api";

export type Presentation = {
  mode: "auto" | "standard" | "handbook" | "original";
  template_id: "handbook";
  template_version: "1.0.0";
};
export function DocumentAppearance({
  project,
  analysis,
  settings,
  version,
  editable,
  onSaved,
}: {
  project: string;
  analysis: string;
  settings: Presentation;
  version: number;
  editable: boolean;
  onSaved: (settings: Presentation, selectionVersion: number) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const epoch = useRef(0),
    pending = useRef(false);
  useEffect(() => {
    epoch.current++;
    pending.current = false;
    setBusy(false);
    setError("");
    return () => {
      epoch.current++;
    };
  }, [project, analysis]);
  return (
    <div className="document-appearance">
      <BookOpen size={15} />
      <select
        aria-label="文档阅读方式"
        value={settings.mode}
        disabled={!editable || busy}
        onChange={async (e) => {
          if (pending.current || !editable) return;
          const n = epoch.current;
          pending.current = true;
          const mode = e.target.value as Presentation["mode"];
          setBusy(true);
          setError("");
          try {
            const result = await api<{
              settings: Presentation;
              selection_version: number;
            }>(
              `/api/v3/projects/${project}/document-presentation`,
              mutation(
                "PUT",
                {
                  ...settings,
                  mode,
                  analysis_set_id: analysis,
                },
                version,
              ),
            );
            if (n === epoch.current)
              await onSaved(result.settings, result.selection_version);
          } catch (err) {
            if (n === epoch.current)
              setError(err instanceof Error ? err.message : String(err));
          } finally {
            if (n === epoch.current) {
              pending.current = false;
              setBusy(false);
            }
          }
        }}
      >
        <option value="auto">跟随文档原样</option>
        <option value="handbook">知识手册模板</option>
        <option value="standard">标准阅读</option>
        <option value="original">原样文档</option>
      </select>
      {busy && <LoaderCircle size={14} className="spinning" />}
      {error && <span role="alert">{error}</span>}
    </div>
  );
}
