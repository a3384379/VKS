import "./product-projections.css";
import { useEffect, useState } from "react";
import { api } from "./api";
import { Button, Empty, Notice, Panel, Spinner, Tag } from "./components";
import { useReadScope, useWorkspace } from "./context";
import { flattenModules, moduleBaseline, type ModuleNode } from "./ModuleTree";
import { ProductEntityLink } from "./ProductEntityLink";
import { materialKind } from "./product-projections";
import { journeyKinds } from "./journey-reading";
import type { Entity } from "./types";

export function BusinessModuleExplorer({
  compact = false,
}: {
  compact?: boolean;
}) {
  const { w, hasUnsavedChanges, notify } = useWorkspace();
  const scope = useReadScope();
  const [tree, setTree] = useState<ModuleNode[]>([]),
    [members, setMembers] = useState<Entity[] | null>(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(
    () => new URLSearchParams(location.hash.slice(1)).get("module") || "",
  );
  const query = moduleBaseline(
    w.selected_analysis_set?.id,
    w.selected_release?.id,
  );
  const base = `/projects/${w.project.id}/modules`;
  const flat = flattenModules(tree),
    module = flat.find((row) => row.id === selected);
  useEffect(() => {
    const sync = () =>
      setSelected(
        new URLSearchParams(location.hash.slice(1)).get("module") || "",
      );
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setTree([]);
    setMembers(null);
    setError("");
    setLoading(true);
    void api<ModuleNode[]>(`${base}?${query}`, { signal: controller.signal })
      .then((rows) => {
        if (!controller.signal.aborted) setTree(rows);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [base, query, scope]);
  useEffect(() => {
    setMembers(null);
    if (compact || !module) return;
    const controller = new AbortController();
    setError("");
    void api<{ entities: Entity[] }>(`${base}/${module.id}/members?${query}`, {
      signal: controller.signal,
    })
      .then((row) => {
        if (!controller.signal.aborted) setMembers(row.entities);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [base, query, module?.id, module?.row_version, compact, scope]);
  function select(id: string) {
    if (hasUnsavedChanges) {
      notify("请先保存或还原当前草稿", "error");
      return;
    }
    const route = new URLSearchParams(location.hash.slice(1));
    route.set("view", "workbench");
    route.set("project", w.project.id);
    route.set(
      "baseline",
      w.selected_release
        ? `release:${w.selected_release.id}`
        : `draft:${w.selected_analysis_set?.id}`,
    );
    if (id) route.set("module", id);
    else route.delete("module");
    for (const key of [
      "entity",
      "action",
      "ui_page",
      "scenario",
      "state",
      "journey",
      "journey_revision",
      "journey_step",
      "journey_choices",
    ])
      route.delete(key);
    location.hash = route.toString();
    setSelected(id);
  }
  const groups = members
    ? [
        {
          name: "业务场景",
          items: members.filter(
            (e) =>
              e.type === "journey" && materialKind(e) === "business_process",
          ),
        },
        {
          name: "导览与其他流程材料",
          items: members.filter(
            (e) =>
              e.type === "journey" && materialKind(e) !== "business_process",
          ),
        },
        {
          name: "页面与动作",
          items: members.filter((e) => ["page", "action"].includes(e.type)),
        },
        {
          name: "模块资料",
          items: members.filter(
            (e) => !["journey", "page", "action"].includes(e.type),
          ),
        },
      ]
    : [];
  return (
    <Panel
      title="业务模块"
      className="business-modules"
      action={
        !compact && selected ? (
          <Button kind="small" onClick={() => select("")}>
            全部模块
          </Button>
        ) : undefined
      }
    >
      <p className="muted">
        已建立的模块范围 · 当前身份与固定版本；模块归属不代表业务知识已经核验。
      </p>
      {error && <Notice tone="red">{error}</Notice>}
      {loading ? (
        <Spinner />
      ) : flat.length ? (
        <div className="product-module-cards">
          {flat.map((node) => (
            <Button
              key={node.id}
              kind={selected === node.id ? "primary" : ""}
              onClick={() => select(node.id)}
            >
              <span>
                {"　".repeat(node.depth)}
                {node.name}
              </span>
              <Tag>{node.entity_count} 项</Tag>
            </Button>
          ))}
        </div>
      ) : (
        <Empty
          title="尚未建立业务模块"
          description="可从知识构建查看模块候选、核对范围并采用；页面目录不会自动变成业务域。"
        />
      )}
      {!compact && selected && !loading && !module && (
        <Notice tone="amber">
          目标模块不在当前可见版本中，请重新选择可见模块。
        </Notice>
      )}
      {!compact && module && (
        <section aria-label={`${module.name}模块范围`}>
          <h3>{module.name}</h3>
          <p>
            {module.description ||
              "按明确归属查看模块内的业务场景、动作与资料。"}
          </p>
          {!members && !error ? (
            <Spinner />
          ) : (
            groups.map((group) => (
              <section className="product-member-group" key={group.name}>
                <h4>
                  {group.name} · {group.items.length}
                </h4>
                <div className="product-module-cards">
                  {group.items.map((entity) => (
                    <article key={entity.id} data-module-entity={entity.id}>
                      <Tag>
                        {entity.type === "journey"
                          ? journeyKinds[materialKind(entity)].label
                          : entity.type}
                      </Tag>
                      <ProductEntityLink entity={entity} />
                      <small>{entity.stable_key}</small>
                    </article>
                  ))}
                </div>
                {!group.items.length && (
                  <p className="muted">当前模块没有可见的此类材料。</p>
                )}
              </section>
            ))
          )}
        </section>
      )}
    </Panel>
  );
}
