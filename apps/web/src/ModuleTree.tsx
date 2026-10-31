import "./configuration.css";
import { useCallback, useEffect, useRef, useState } from "react";
import { FolderTree, Plus, Settings2, Trash2 } from "lucide-react";
import { api, ApiError, mutation } from "./api";
import {
  Button,
  Empty,
  Modal,
  Notice,
  Panel,
  Spinner,
  Tag,
} from "./components";
import { useReadScope, usePermission, useWorkspace } from "./context";
import type { Entity, SourceConnector } from "./types";
import { ProductEntityLink } from "./ProductEntityLink";

export interface ModuleNode {
  id: string;
  parent_id: string | null;
  key: string;
  name: string;
  description: string;
  sort_order: number;
  row_version: number;
  entity_count: number;
  children: ModuleNode[];
}
interface Members {
  module_id: string;
  row_version: number;
  entity_ids: string[];
  source_snapshot_ids: string[];
  connector_ids: string[];
  entities: Entity[];
}
export function flattenModules(
  nodes: ModuleNode[],
  depth = 0,
): (ModuleNode & { depth: number })[] {
  return nodes.flatMap((node) => [
    { ...node, depth },
    ...flattenModules(node.children || [], depth + 1),
  ]);
}
export function moduleBaseline(analysisId?: string, releaseId?: string) {
  const query = new URLSearchParams();
  if (releaseId) query.set("release_id", releaseId);
  else if (analysisId) query.set("analysis_set_id", analysisId);
  return query.toString();
}
export function ModuleTree() {
  const { w, session } = useWorkspace();
  // Drafts belong to an actual baseline and authority. Content polling only
  // invalidates the read projections inside the body, not the editable values.
  const authority = JSON.stringify([
    w.project.id,
    w.selected_analysis_set?.id,
    w.selected_release?.id,
    session.actor_id,
    [
      ...(session.project_permissions?.[w.project.id] ?? session.scopes ?? []),
    ].sort(),
  ]);
  return <ModuleTreeBody key={authority} />;
}
function ModuleTreeBody() {
  const { w, notify } = useWorkspace();
  const canEdit = usePermission("knowledge:edit");
  const canManage = usePermission("project:manage");
  const scope = useReadScope();
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const treeRequest = useRef<AbortController | null>(null);
  const [treeData, setTreeData] = useState<{
      scope: string;
      rows: ModuleNode[];
    } | null>(null),
    [selectedId, setSelectedId] = useState(""),
    [memberData, setMemberData] = useState<{
      scope: string;
      row: Members;
    } | null>(null),
    [connectorData, setConnectorData] = useState<{
      scope: string;
      rows: SourceConnector[];
    } | null>(null),
    [loading, setLoading] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [editing, setEditing] = useState<ModuleNode | null>(null),
    [metadataOpen, setMetadataOpen] = useState(false),
    [bindingOpen, setBindingOpen] = useState(false),
    [deleting, setDeleting] = useState<ModuleNode | null>(null),
    [key, setKey] = useState(""),
    [name, setName] = useState(""),
    [description, setDescription] = useState(""),
    [parentId, setParentId] = useState(""),
    [order, setOrder] = useState(0),
    [entityIds, setEntityIds] = useState<string[]>([]),
    [sourceIds, setSourceIds] = useState<string[]>([]),
    [connectorIds, setConnectorIds] = useState<string[]>([]),
    [filter, setFilter] = useState(""),
    [bindingVersion, setBindingVersion] = useState(1);
  const tree = treeData?.scope === scope ? treeData.rows : [];
  const members =
    memberData?.scope === scope && memberData.row.module_id === selectedId
      ? memberData.row
      : null;
  const connectors =
    canManage && connectorData?.scope === scope ? connectorData.rows : [];
  const editingRef = useRef(editing);
  editingRef.current = editing;
  const query = moduleBaseline(
      w.selected_analysis_set?.id,
      w.selected_release?.id,
    ),
    flat = flattenModules(tree),
    selected = flat.find((row) => row.id === selectedId);
  const base = "/projects/" + w.project.id + "/modules";
  const reload = useCallback(async () => {
    treeRequest.current?.abort();
    const controller = new AbortController();
    treeRequest.current = controller;
    const isCurrent = () =>
      !controller.signal.aborted && currentScope.current === scope;
    setLoading(true);
    setError("");
    try {
      const rows = await api<ModuleNode[]>(base + "?" + query, {
        signal: controller.signal,
      });
      if (!isCurrent()) return;
      setTreeData({ scope, rows });
      if (
        editingRef.current &&
        !flattenModules(rows).some((row) => row.id === editingRef.current?.id)
      ) {
        setMetadataOpen(false);
        setEditing(null);
        setKey("");
        setName("");
        setDescription("");
        setDeleting(null);
      }
      setSelectedId((previous) =>
        flattenModules(rows).some((row) => row.id === previous)
          ? previous
          : rows[0]?.id || "",
      );
    } catch (e) {
      if (isCurrent()) setError((e as Error).message);
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [base, query, scope]);
  useEffect(() => {
    void reload();
    return () => treeRequest.current?.abort();
  }, [reload]);
  useEffect(() => {
    setMemberData(null);
    if (!selectedId) return;
    const controller = new AbortController();
    void api<Members>(base + "/" + selectedId + "/members?" + query, {
      signal: controller.signal,
    })
      .then((row) => {
        if (!controller.signal.aborted && currentScope.current === scope)
          setMemberData({ scope, row });
      })
      .catch((e) => {
        if (!controller.signal.aborted && currentScope.current === scope)
          setError(e.message);
      });
    return () => controller.abort();
  }, [base, query, scope, selectedId, selected?.row_version]);
  useEffect(() => {
    if (!canManage) return;
    const controller = new AbortController();
    void api<SourceConnector[]>(`/projects/${w.project.id}/source-connectors`, {
      signal: controller.signal,
    })
      .then((rows) => {
        if (!controller.signal.aborted && currentScope.current === scope)
          setConnectorData({ scope, rows });
      })
      .catch((error) => {
        if (!controller.signal.aborted && currentScope.current === scope)
          setError(error.message);
      });
    return () => controller.abort();
  }, [w.project.id, canManage, scope]);
  function openMetadata(node?: ModuleNode) {
    setEditing(node || null);
    setKey(node?.key || "");
    setName(node?.name || "");
    setDescription(node?.description || "");
    setParentId(node ? node.parent_id || "" : selectedId);
    setOrder(node?.sort_order || 0);
    setError("");
    setMetadataOpen(true);
  }
  async function perform(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    setError("");
    try {
      await action();
      setMetadataOpen(false);
      setBindingOpen(false);
      setDeleting(null);
      notify(message);
      await reload();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "模块已被其他人更新，当前输入保留。关闭前请复制变更，再重新载入并合并。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  const unavailableParents = editing
    ? new Set(flattenModules([editing]).map((node) => node.id))
    : new Set<string>();
  return (
    <div className="configuration-stack">
      <Panel
        title="模块结构与知识归属"
        action={
          canEdit && (
            <Button kind="primary small" onClick={() => openMetadata()}>
              <Plus size={15} />
              创建模块
            </Button>
          )
        }
      >
        <Notice>
          模块组织业务范围，同一知识实体可属于多个模块。调整模块结构与归属会保留实体身份、修订和已发布版本。
        </Notice>
        {error && <Notice tone="red">{error}</Notice>}
        {loading ? (
          <Spinner />
        ) : (
          <div className="module-layout">
            <nav className="module-tree" aria-label="项目模块树">
              {flat.map((node) => (
                <button
                  className={node.id === selectedId ? "selected" : ""}
                  key={node.id}
                  style={{ paddingLeft: 12 + node.depth * 18 }}
                  onClick={() => setSelectedId(node.id)}
                >
                  <FolderTree size={16} />
                  <span>{node.name}</span>
                  <Tag>{node.entity_count}</Tag>
                </button>
              ))}
            </nav>
            {selected ? (
              <section className="module-detail">
                <div className="row between">
                  <div>
                    <h3>{selected.name}</h3>
                    <code>{selected.key}</code>
                  </div>
                  {canEdit && (
                    <div className="row">
                      <Button
                        kind="small"
                        onClick={() => openMetadata(selected)}
                      >
                        <Settings2 size={14} />
                        编辑
                      </Button>
                      <Button
                        kind="small"
                        disabled={!!selected.children.length}
                        onClick={() => {
                          setError("");
                          setDeleting(selected);
                        }}
                      >
                        <Trash2 size={14} />
                        删除
                      </Button>
                    </div>
                  )}
                </div>
                <p className="section-description">
                  {selected.description || "可在此模块下组织相关业务知识。"}
                </p>
                <div className="row between">
                  <strong>{selected.entity_count} 个当前可见实体</strong>
                  {canEdit && (
                    <Button
                      kind="small"
                      disabled={!members}
                      onClick={() => {
                        if (!members) return;
                        setEntityIds([...members.entity_ids]);
                        setSourceIds([...members.source_snapshot_ids]);
                        setConnectorIds([...members.connector_ids]);
                        setBindingVersion(members.row_version);
                        setFilter("");
                        setError("");
                        setBindingOpen(true);
                      }}
                    >
                      调整实体归属
                    </Button>
                  )}
                </div>
                <div className="module-entities">
                  {members?.entities.map((entity) => (
                    <div key={entity.id}>
                      <Tag>{entity.type}</Tag>
                      <ProductEntityLink entity={entity} />
                      <small>{entity.stable_key}</small>
                    </div>
                  ))}
                  {members && !members.entities.length && (
                    <Empty
                      title="当前基线内没有可见实体"
                      description="为模块选择当前知识，或切换到含有对应实体的资料基线。"
                    />
                  )}
                </div>
              </section>
            ) : (
              <Empty
                title="还没有项目模块"
                description="导入前即可建立模块树，再将知识实体归入业务范围。"
              />
            )}
          </div>
        )}
      </Panel>
      {metadataOpen &&
        canEdit &&
        treeData?.scope === scope &&
        (!editing || flat.some((node) => node.id === editing.id)) && (
          <Modal
            title={editing ? "编辑模块" : "创建模块"}
            onClose={() => setMetadataOpen(false)}
          >
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void perform(
                  () =>
                    api(
                      editing ? base + "/" + editing.id : base,
                      mutation(
                        editing ? "PATCH" : "POST",
                        {
                          ...(editing ? {} : { key: key.trim() }),
                          name: name.trim(),
                          description: description.trim(),
                          parent_id: parentId || null,
                          sort_order: order,
                        },
                        editing?.row_version,
                      ),
                    ),
                  editing ? "模块资料已更新，实体身份保留" : "模块已创建",
                );
              }}
            >
              <label>
                模块标识
                <input
                  required
                  readOnly={!!editing}
                  pattern="[a-zA-Z0-9._-]+"
                  maxLength={96}
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  placeholder="例如 billing.invoice"
                />
              </label>
              <label>
                模块名称
                <input
                  required
                  maxLength={160}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <label>
                父模块
                <select
                  value={parentId}
                  onChange={(e) => setParentId(e.target.value)}
                >
                  <option value="">项目根层级</option>
                  {flat
                    .filter((node) => !unavailableParents.has(node.id))
                    .map((node) => (
                      <option key={node.id} value={node.id}>
                        {"　".repeat(node.depth) + node.name}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                排序
                <input
                  type="number"
                  min={-100000}
                  max={100000}
                  required
                  value={order}
                  onChange={(e) => setOrder(Number(e.target.value))}
                />
              </label>
              <label>
                说明
                <textarea
                  maxLength={2000}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </label>
              {error && <Notice tone="red">{error}</Notice>}
              <div className="modal-actions">
                <Button onClick={() => setMetadataOpen(false)}>取消</Button>
                <Button
                  kind="primary"
                  type="submit"
                  disabled={busy || !name.trim() || !key.trim()}
                >
                  保存模块
                </Button>
              </div>
            </form>
          </Modal>
        )}
      {bindingOpen && canEdit && selected && members && (
        <Modal title="调整模块的实体归属" onClose={() => setBindingOpen(false)}>
          <Notice>
            实体、固定来源快照与来源连接均可归属多个模块。快照固定一个来源版本；连接按准确来源标识涵盖其快照。当前不可见或基线之外的已有归属由服务端保留。
          </Notice>
          <label>
            筛选当前基线实体
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="名称或稳定标识"
            />
          </label>
          <div className="evidence-checks">
            {w.entities
              .filter(
                (entity) =>
                  !filter ||
                  (entity.name + " " + entity.stable_key).includes(filter),
              )
              .map((entity) => (
                <label className="check-line" key={entity.id}>
                  <input
                    type="checkbox"
                    checked={entityIds.includes(entity.entity_id || entity.id)}
                    onChange={(e) => {
                      const id = entity.entity_id || entity.id;
                      setEntityIds((previous) =>
                        e.target.checked
                          ? [...new Set([...previous, id])]
                          : previous.filter((value) => value !== id),
                      );
                    }}
                  />
                  <span>
                    {entity.name}
                    <small>
                      {entity.type} · {entity.stable_key}
                    </small>
                  </span>
                </label>
              ))}
          </div>
          <fieldset>
            <legend>来源快照</legend>
            <p className="muted">选择当前基线内有权读取的固定来源版本。</p>
            <div className="evidence-checks">
              {w.sources.map((source) => {
                const id = source.source_snapshot_id;
                if (!id) return null;
                return (
                  <label className="check-line" key={id}>
                    <input
                      type="checkbox"
                      value={id}
                      checked={sourceIds.includes(id)}
                      onChange={(event) =>
                        setSourceIds((previous) =>
                          event.target.checked
                            ? [...new Set([...previous, id])]
                            : previous.filter((value) => value !== id),
                        )
                      }
                    />
                    <span>
                      {source.source_id || source.role}
                      <small>
                        {source.role} · {source.provider} ·{" "}
                        {source.provider_revision || "固定版本"} · {id}
                      </small>
                    </span>
                  </label>
                );
              })}
              {!w.sources.length && (
                <p className="muted">当前基线没有可选择的来源快照。</p>
              )}
            </div>
          </fieldset>
          {canManage && (
            <fieldset>
              <legend>来源连接</legend>
              <p className="muted">
                连接归属独立于快照归属；同名连接以唯一来源标识区分。
              </p>
              <div className="evidence-checks">
                {connectors.map((connector) => (
                  <label className="check-line" key={connector.id}>
                    <input
                      type="checkbox"
                      value={connector.id}
                      checked={connectorIds.includes(connector.id)}
                      onChange={(event) =>
                        setConnectorIds((previous) =>
                          event.target.checked
                            ? [...new Set([...previous, connector.id])]
                            : previous.filter(
                                (value) => value !== connector.id,
                              ),
                        )
                      }
                    />
                    <span>
                      {connector.name}
                      <small>
                        {connector.provider} · {connector.key} · {connector.id}
                      </small>
                    </span>
                  </label>
                ))}
                {connectorData?.scope !== scope ? (
                  <Spinner />
                ) : (
                  !connectors.length && (
                    <p className="muted">
                      尚未登记来源连接，可在来源连接设置中登记。
                    </p>
                  )
                )}
              </div>
            </fieldset>
          )}
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={() => setBindingOpen(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={busy || (canManage && connectorData?.scope !== scope)}
              onClick={() => {
                void perform(
                  () =>
                    api(
                      base + "/" + selected.id + "/members",
                      mutation(
                        "PUT",
                        {
                          entity_ids: entityIds.filter((id) =>
                            w.entities.some(
                              (entity) => entity.entity_id === id,
                            ),
                          ),
                          source_snapshot_ids: sourceIds.filter((id) =>
                            w.sources.some(
                              (source) => source.source_snapshot_id === id,
                            ),
                          ),
                          connector_ids: connectorIds.filter((id) =>
                            connectors.some((connector) => connector.id === id),
                          ),
                          ...(w.selected_release
                            ? { release_id: w.selected_release.id }
                            : w.selected_analysis_set
                              ? { analysis_set_id: w.selected_analysis_set.id }
                              : {}),
                        },
                        bindingVersion,
                      ),
                    ),
                  "模块实体归属已更新",
                );
              }}
            >
              保存归属
            </Button>
          </div>
        </Modal>
      )}
      {deleting && canEdit && flat.some((node) => node.id === deleting.id) && (
        <Modal title="删除空模块层级" onClose={() => setDeleting(null)}>
          <Notice tone="amber">
            将删除模块“{deleting.name}
            ”及其归属映射。知识实体、来源和不可变修订继续保留。包含子模块时需要先调整子层级。
          </Notice>
          {error && <Notice tone="red">{error}</Notice>}
          <div className="modal-actions">
            <Button onClick={() => setDeleting(null)}>取消</Button>
            <Button
              kind="primary"
              disabled={busy}
              onClick={() => {
                void perform(
                  () =>
                    api(
                      base + "/" + deleting.id,
                      mutation("DELETE", {}, deleting.row_version),
                    ),
                  "模块已删除，知识实体保留",
                );
              }}
            >
              确认删除模块
            </Button>
          </div>
        </Modal>
      )}
    </div>
  );
}
