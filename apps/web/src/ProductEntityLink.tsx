import { api } from "./api";
import { useEffect, useRef, type ReactNode } from "react";
import { moduleWorkspace } from "./module-context";
import { Button } from "./components";
import { useReadScope, useWorkspace } from "./context";
import type { Entity, Workspace } from "./types";

let latestEntityIntent = 0;

/** Every target is resolved against a permission-filtered, fixed workspace first. */
export function ProductEntityLink({
  entity,
  baseline,
  stepKey = "",
  children,
}: {
  entity: Entity;
  baseline?: string;
  stepKey?: string;
  children?: ReactNode;
}) {
  const {
    w,
    navigateReadEntity,
    hasUnsavedChanges,
    notify,
    navigationEpoch,
    moduleId,
    moduleEntities,
  } = useWorkspace();
  const scope = useReadScope();
  const state = useRef({
    mounted: true,
    scope,
    epoch: navigationEpoch,
    moduleId,
    moduleEntities,
  });
  state.current.scope = scope;
  state.current.epoch = navigationEpoch;
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    state.current.mounted = true;
    return () => {
      state.current.mounted = false;
    };
  }, []);
  useEffect(() => {
    request.current?.abort();
  }, [scope]);
  async function open() {
    if (hasUnsavedChanges) {
      notify("请先保存或还原当前草稿", "error");
      return;
    }
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const intent = ++latestEntityIntent;
    const route = location.hash;
    const epoch = navigationEpoch;
    const isCurrent = () =>
      state.current.mounted &&
      !controller.signal.aborted &&
      latestEntityIntent === intent &&
      state.current.scope === scope &&
      state.current.epoch === epoch &&
      location.hash === route;
    const currentBaseline = w.selected_release
      ? `release:${w.selected_release.id}`
      : `draft:${w.selected_analysis_set?.id}`;
    const targetBaseline = baseline || currentBaseline;
    let live: Workspace;
    try {
      const query = new URLSearchParams(
        targetBaseline.startsWith("release:")
          ? { release_id: targetBaseline.slice(8) }
          : { analysis_set_id: targetBaseline.slice(6) },
      );
      live = await api<Workspace>(
        `/projects/${w.project.id}/workspace?${query}`,
        { signal: controller.signal },
      );
      if (!isCurrent()) return;
      const available =
        entity.type === "relation"
          ? live.relations.some((row) => row.id === entity.id)
          : live.entities.some(
              (row) =>
                row.entity_id === entity.entity_id &&
                row.entity_revision_id === entity.entity_revision_id,
            );
      if (!available) {
        notify(
          "目标的准确修订已不在该版本的当前可见范围中，请重新载入报告或模块",
          "error",
        );
        return;
      }
    } catch (error) {
      if (isCurrent()) notify((error as Error).message, "error");
      return;
    }
    const scoped = moduleEntities ? moduleWorkspace(w, moduleEntities) : null;
    const outsideModule =
      !!moduleId &&
      !!scoped &&
      (entity.type === "action"
        ? !scoped.actions.some(
            (member) => member.entity_id === entity.entity_id,
          )
        : entity.type === "page"
          ? !scoped.pages.some(
              (member) => member.entity_id === entity.entity_id,
            )
          : false);
    if (!isCurrent()) return;
    if (navigateReadEntity) {
      navigateReadEntity(live, entity, targetBaseline, stepKey);
      return;
    }
    const view =
      entity.type === "relation"
        ? "relations"
        : entity.type === "journey"
          ? "journeys"
          : ["document", "section"].includes(entity.type)
            ? "documents"
            : ["action", "page"].includes(entity.type)
              ? "workbench"
              : "graph";
    const query = new URLSearchParams(route.slice(1));
    query.set("project", w.project.id);
    query.set("baseline", targetBaseline);
    query.set("view", view);
    if (outsideModule) query.delete("module");
    query.set("entity", entity.id);
    // The selected target must never inherit a different version's action/state.
    for (const key of [
      "action",
      "ui_page",
      "scenario",
      "state",
      "journey",
      "journey_revision",
      "journey_step",
      "journey_choices",
    ])
      query.delete(key);
    if (entity.type === "action") query.set("action", entity.id);
    if (entity.type === "page") query.set("ui_page", entity.id);
    if (entity.type === "journey") {
      query.set("journey", entity.entity_id);
      query.set("journey_revision", entity.entity_revision_id);
      if (stepKey) query.set("journey_step", stepKey);
    }
    location.hash = query.toString();
  }
  return (
    <Button kind="small" onClick={() => void open()}>
      {children || entity.name}
    </Button>
  );
}
