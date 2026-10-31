import { useEffect, useState } from "react";
import { api } from "./api";
import { Button, Notice, Tag } from "./components";
import { useReadScope, useWorkspace } from "./context";
import type { Entity } from "./types";
import type { JourneyRevision } from "./journeys";
type References = {
  actions: Entity[];
  journeys: JourneyRevision[];
  is_truncated: boolean;
  unknowns?: string[];
};
export function BusinessReferences({ entityId }: { entityId: string }) {
  const { w, navigate, navigateExplanation, navigateJourney, journey } =
    useWorkspace();
  const readScope = `${useReadScope()}:${entityId}`;
  const [result, setData] = useState<{
      scope: string;
      value: References;
    } | null>(null),
    [error, setError] = useState("");
  const data = result?.scope === readScope ? result.value : null;
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError("");
    const q = new URLSearchParams({ project_id: w.project.id });
    if (w.selected_release) q.set("release_id", w.selected_release.id);
    else if (w.selected_analysis_set)
      q.set("analysis_set_id", w.selected_analysis_set.id);
    void api<References>(
      `/api/v2/entities/${entityId}/business-references?${q}`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted) setData({ scope: readScope, value });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [readScope]);
  return (
    <div className="v2-business-references">
      <h3>关联业务上下文</h3>
      <Tag>同版可见资料 · 动作关系为有效</Tag>
      {error && <Notice tone="red">{error}</Notice>}
      {data?.actions.map((a) => (
        <Button
          key={a.id}
          onClick={() => {
            navigateExplanation({ actionId: a.id, followTarget: false });
            navigate("workbench");
          }}
        >
          {a.name} →
        </Button>
      ))}
      {data?.journeys
        ?.filter((row) =>
          w.entities.some(
            (entity) =>
              entity.entity_id === row.journey_id &&
              entity.entity_revision_id === row.revision_id,
          ),
        )
        .map((row) => (
          <Button
            key={row.journey_id}
            onClick={() =>
              navigateJourney(
                journey.journeyId === row.journey_id &&
                  journey.revisionId === row.revision_id
                  ? journey
                  : {
                      journeyId: row.journey_id,
                      revisionId: row.revision_id,
                      stepKey:
                        row.dsl.nodes.find(
                          (node) => node.binding?.entity_id === entityId,
                        )?.key ||
                        row.dsl.nodes.find((node) => node.kind === "step")
                          ?.key ||
                        row.dsl.entry,
                      choices: {},
                    },
              )
            }
          >
            打开导览 · {row.dsl.title} →
          </Button>
        ))}
      {data && !data.actions.length && !data.journeys?.length && (
        <p className="muted">
          当前范围没有已关联业务动作或准确引用此资料的导览，不能凭名称推断。
        </p>
      )}
      {data?.is_truncated && (
        <Notice>关联范围已截断，可缩小范围后继续查询。</Notice>
      )}
      {data?.unknowns?.map((text, index) => (
        <p className="muted" key={index}>
          {text}
        </p>
      ))}
    </div>
  );
}
