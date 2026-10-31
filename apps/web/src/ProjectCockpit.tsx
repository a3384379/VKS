import { BusinessModuleExplorer } from "./BusinessModuleExplorer";
import { coveredActionRevisions, materialKind } from "./product-projections";
import { journeyKinds } from "./journey-reading";
import { ProductEntityLink } from "./ProductEntityLink";
import { Surface as Panel } from "./design-system/Surface";
import {
  ArrowRight,
  Boxes,
  GitBranch,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { Button, Empty } from "./components";
import { useWorkspace } from "./context";

export function ProjectCockpit() {
  const { w, navigate, navigateExplanation } = useWorkspace();
  const journeys = w.entities.filter((e) => e.type === "journey");
  const business = journeys.filter(
    (e) => materialKind(e) === "business_process",
  );
  const trusted = w.relations.filter(
    (r) => !r.excluded && r.validity_status === "valid",
  );
  const linked = coveredActionRevisions(w.entities, w.relations);
  const covered = w.actions.filter((a) =>
    linked.has(a.entity_revision_id),
  ).length;
  const pending = w.relations.filter((r) => r.excluded).length;
  const stale = w.relations.filter((r) => r.validity_status !== "valid").length;
  const runtimeLinked = new Set(
    w.selected_analysis_set?.runtime_alignment === "matched"
      ? trusted
          .filter((r) => r.causal_basis === "correlated_runtime")
          .flatMap((r) => [r.from_revision_id, r.to_revision_id])
      : [],
  );
  const unverifiedRuntime = w.actions.filter(
    (a) => !runtimeLinked.has(a.entity_revision_id),
  ).length;
  return (
    <>
      <div className="cockpit-grid">
        <div>
          <section className="v2-hero">
            <span className="eyebrow">PROJECT INTELLIGENCE / 知识互联</span>
            <h2>
              让每一个业务操作，
              <br />
              <em>都有据可循。</em>
            </h2>
            <p>
              {w.project.description ||
                "从业务场景进入规则、接口、实现与原始证据，保持每一次探索的版本和上下文。"}
            </p>
            <div className="row wrap">
              <Button kind="primary" onClick={() => navigate("workbench")}>
                <Boxes size={16} />
                探索业务流程 <ArrowRight size={15} />
              </Button>
              <Button onClick={() => navigate("agent")}>
                <Sparkles size={16} />
                构建项目知识
              </Button>
            </div>
          </section>
          <div className="v2-metrics">
            {[
              {
                title: "业务场景",
                value: business.length,
                note: `${journeys.length - business.length} 份其他流程材料 · ${w.scenarios.length} 个采集场景`,
              },
              {
                title: "动作知识覆盖",
                value: `${covered} / ${w.actions.length}`,
                note: "有效知识关联；不计页面包含关系",
              },
              {
                title: "已移除关联",
                value: pending,
                note: `${stale} 条过期或断链`,
              },
            ].map((m) => (
              <div key={m.title}>
                <strong>{m.value}</strong>
                <span>{m.title}</span>
                <small>{m.note}</small>
              </div>
            ))}
          </div>
        </div>
        <Panel title="知识构建建议" className="v2-assistant">
          <Sparkles size={24} />
          <p className="muted">
            当前可见范围：未见已核对运行证据的动作 {unverifiedRuntime} /{" "}
            {w.actions.length}；过期或断链 {stale} / {w.relations.length}
            。分母为零时尚未评估。
          </p>
          <h2>
            {w.sources.length
              ? "从固定来源，建立项目知识。"
              : "从第一份项目材料开始。"}
          </h2>
          <p>接入资料后直接整理和阅读。保留准确来源，发现错误随时修正。</p>
          <dl>
            <dt>可见来源</dt>
            <dd>{w.sources.length}</dd>
            <dt>当前版本</dt>
            <dd>
              {w.selected_release?.release_no ||
                w.selected_analysis_set?.name ||
                "尚未建立"}
            </dd>
            <dt>运行版本对齐</dt>
            <dd>{w.selected_analysis_set?.runtime_alignment || "unknown"}</dd>
          </dl>
          <Button kind="primary" onClick={() => navigate("imports")}>
            打开智能接入向导 <ArrowRight size={15} />
          </Button>
        </Panel>
      </div>
      <BusinessModuleExplorer compact />
      <Panel
        title="页面入口"
        action={
          <Button kind="small" onClick={() => navigate("journeys")}>
            <GitBranch size={14} />
            流程画布
          </Button>
        }
      >
        {w.pages.length ? (
          <div className="v2-domain-grid">
            {w.pages.map((page) => {
              const actions = w.actions.filter(
                (a) =>
                  (a.metadata.page_key || a.metadata.pageKey) ===
                  page.stable_key,
              );
              const count = actions.filter((a) =>
                linked.has(a.entity_revision_id),
              ).length;
              return (
                <button
                  className="v2-domain"
                  key={page.id}
                  onClick={() => {
                    navigateExplanation({
                      pageId: page.id,
                      actionId: actions[0]?.id || "",
                      followTarget: false,
                    });
                    navigate("workbench");
                  }}
                >
                  <Boxes size={22} />
                  <h3>{page.name}</h3>
                  <p>{actions.length} 个业务动作</p>
                  <progress
                    aria-label={`${page.name}动作知识覆盖`}
                    max={Math.max(1, actions.length)}
                    value={count}
                  />
                  <small>
                    已关联 {count} / {actions.length} <ArrowRight size={14} />
                  </small>
                </button>
              );
            })}
          </div>
        ) : (
          <Empty
            title="接入资料，建立业务入口"
            description="上传原型、Markdown 或 OpenAPI；缺少源码时仍可阅读与手工关联。"
            action={
              <Button onClick={() => navigate("imports")}>接入项目</Button>
            }
          />
        )}
      </Panel>
      {Object.entries(journeyKinds).map(([kind, meaning]) => {
        const rows = journeys.filter((j) => materialKind(j) === kind);
        return (
          <Panel
            key={kind}
            title={`${meaning.label} · ${rows.length}`}
            action={
              <Button kind="small" onClick={() => navigate("journeys")}>
                打开流程材料
              </Button>
            }
          >
            <p className="muted">{meaning.description}</p>
            <div className="v2-domain-grid">
              {rows.length ? (
                rows.map((j) => (
                  <article className="v2-domain" key={j.id}>
                    <GitBranch size={20} />
                    <h3>{j.name}</h3>
                    <ProductEntityLink entity={j}>
                      打开{meaning.label}
                    </ProductEntityLink>
                    <small>固定修订 · 随时编辑与保存</small>
                  </article>
                ))
              ) : (
                <p className="muted">当前可见版本尚无此类材料。</p>
              )}
            </div>
          </Panel>
        );
      })}
      <p className="v2-scope">
        <ShieldCheck size={14} />
        指标范围为当前身份、当前版本可见材料；真实业务覆盖率和运行因果需独立验收。
      </p>
    </>
  );
}
