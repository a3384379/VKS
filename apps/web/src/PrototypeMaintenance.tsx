import { useEffect, useRef, useState } from "react";
import { ApiError, api, mutation, sha256 } from "./api";
import { Button, Notice } from "./components";
import { usePermission, useWorkspace } from "./context";
import {
  prototypeApi,
  type PrototypeAnchor,
  type PrototypeManifest,
  type PrototypeState,
} from "./prototype";
import type { Entity } from "./types";

export type EditableAnchor = PrototypeAnchor & { label: string };
type Rect = PrototypeAnchor["rect"];
type Viewport = NonNullable<PrototypeState["viewport"]>;
export type StatePreview = {
  anchors: EditableAnchor[];
  image?: string;
  viewport: Viewport;
  creating: boolean;
};

function usePrototypeSave() {
  const { refresh, notify } = useWorkspace();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const attempt = useRef<{ signature: string; key: string } | null>(null);
  function request(
    path: string,
    method: string,
    payload: unknown,
    version?: number,
  ) {
    const signature = JSON.stringify([path, method, version, payload]);
    if (attempt.current?.signature !== signature)
      attempt.current = { signature, key: crypto.randomUUID() };
    const init = mutation(method, payload, version);
    const headers = new Headers(init.headers);
    headers.set("Idempotency-Key", attempt.current.key);
    return api(path, { ...init, headers });
  }
  async function save(
    operation: () => Promise<unknown>,
    message: string,
    onSaved: () => void,
  ) {
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      await operation();
      attempt.current = null;
      onSaved();
      notify(message);
      try {
        await refresh();
      } catch (failure) {
        setError(`已保存，暂时无法刷新。${(failure as Error).message}`);
      }
    } catch (failure) {
      setError(
        failure instanceof ApiError && [409, 412].includes(failure.status)
          ? `保存冲突：${failure.message}。本地输入已保留，可复制后重新载入当前修订。`
          : (failure as Error).message,
      );
    } finally {
      setSaving(false);
    }
  }
  return { saving, error, setError, request, save };
}

export function PrototypeStateEditor({
  page,
  state,
  manifest,
  actions,
  drawing,
  setDrawing,
  drawn,
  dirty,
  setDirty,
  onSaved,
  onPreview,
  selectedAnchor,
  setSelectedAnchor,
}: {
  page: Entity;
  state: PrototypeState;
  manifest: PrototypeManifest;
  actions: Map<string, Entity>;
  drawing: boolean;
  setDrawing: (active: boolean) => void;
  drawn: Rect | null;
  dirty: boolean;
  setDirty: (dirty: boolean) => void;
  onSaved: (stateKey: string) => void;
  onPreview: (preview: StatePreview) => void;
  selectedAnchor: string;
  setSelectedAnchor: (key: string) => void;
}) {
  const { w, refresh } = useWorkspace();
  const canUpload = usePermission("source:import");
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState(state.stateKey);
  const [name, setName] = useState(state.name);
  const [image, setImage] = useState<File | null>(null);
  const [previewImage, setPreviewImage] = useState("");
  const initialViewport = state.viewport || {
    width: 1440,
    height: 900,
    devicePixelRatio: 1,
  };
  const [viewport, setViewport] = useState(initialViewport);
  const [inputVersion, setInputVersion] = useState(0);
  const loadedActions = useRef(new Set(actions.keys()));
  const editableAnchors = () =>
    state.anchors.flatMap((anchor) => {
      const action = actions.get(anchor.actionKey);
      return action ? [{ ...anchor, label: action.name }] : [];
    });
  const [anchors, setAnchors] = useState<EditableAnchor[]>(editableAnchors);
  const draft = w.selected_analysis_set;
  const current = {
    revision: page.entity_revision_id,
    version: draft?.selection_version,
  };
  const loaded = useRef(current);
  const changed =
    dirty &&
    (current.revision !== loaded.current.revision ||
      current.version !== loaded.current.version);
  const upload = useRef<{ file: File; id: string } | null>(null);
  const { saving, error, setError, request, save } = usePrototypeSave();
  const visibleAnchors = anchors.filter(
    (anchor) =>
      actions.has(anchor.actionKey) ||
      !loadedActions.current.has(anchor.actionKey),
  );
  function reset() {
    loaded.current = current;
    loadedActions.current = new Set(actions.keys());
    setCreating(false);
    setKey(state.stateKey);
    setName(state.name);
    setAnchors(editableAnchors());
    setImage(null);
    setViewport(initialViewport);
    setInputVersion((value) => value + 1);
    setDrawing(false);
    setDirty(false);
    setError("");
  }
  useEffect(() => {
    if (
      !dirty &&
      !saving &&
      (current.revision !== loaded.current.revision ||
        current.version !== loaded.current.version)
    )
      reset();
  }, [current.revision, current.version, dirty, saving]);
  useEffect(() => {
    setPreviewImage("");
    if (!image) return;
    let active = true;
    let url: string | undefined;
    void createImageBitmap(image).then(
      (bitmap) => {
        if (active) {
          setViewport({
            width: bitmap.width,
            height: bitmap.height,
            devicePixelRatio: 1,
          });
          url = URL.createObjectURL(image);
          setPreviewImage(url);
        }
        bitmap.close();
      },
      () => {
        if (active) setError("无法读取所选 PNG，请重新选择画面。");
      },
    );
    return () => {
      active = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [image, setError]);
  useEffect(() => {
    onPreview({
      anchors: visibleAnchors,
      image: previewImage || undefined,
      viewport,
      creating,
    });
  }, [
    anchors,
    current.revision,
    current.version,
    previewImage,
    viewport,
    creating,
    onPreview,
  ]);
  function add(rect: Rect) {
    const key = "control." + crypto.randomUUID();
    setAnchors((rows) => [
      ...rows,
      { anchorKey: key, actionKey: key, label: "", rect },
    ]);
    setSelectedAnchor(key);
    setDirty(true);
  }
  useEffect(() => {
    if (drawn) {
      add(drawn);
      setDrawing(false);
    }
  }, [drawn]);
  function update(index: number, update: Partial<EditableAnchor>) {
    setDirty(true);
    setAnchors((rows) =>
      rows.map((row, offset) =>
        offset === index ? { ...row, ...update } : row,
      ),
    );
  }
  async function submit() {
    if (!draft || saving) return;
    if (creating && manifest.states.some((row) => row.stateKey === key)) {
      setError("状态标识已存在，请使用新的标识或返回修改已有状态。");
      return;
    }
    if (image && !previewImage) {
      setError("请等待 PNG 预览加载，或重新选择画面。");
      return;
    }
    setDirty(true);
    await save(
      async () => {
        let uploadId: string | undefined;
        if (image) {
          if (upload.current?.file === image) uploadId = upload.current.id;
          else {
            const uploaded = await api<{
              artifact_id: string;
              upload_url: string;
            }>(
              `/projects/${w.project.id}/upload-sessions`,
              mutation("POST", {
                filename: image.name,
                size_bytes: image.size,
                sha256: await sha256(image),
                media_type: "image/png",
              }),
            );
            await api(uploaded.upload_url, {
              method: "PUT",
              body: image,
              headers: { "Content-Type": "application/octet-stream" },
            });
            uploadId = uploaded.artifact_id;
            upload.current = { file: image, id: uploadId };
          }
        }
        return request(
          `${prototypeApi(w.project.id)}/pages/${encodeURIComponent(page.stable_key)}/anchors`,
          "PUT",
          {
            analysis_set_id: draft.id,
            page_revision_id: loaded.current.revision,
            state_key: key,
            name,
            anchors: visibleAnchors,
            ...(uploadId ? { upload_artifact_id: uploadId, viewport } : {}),
          },
          loaded.current.version,
        );
      },
      "已保存状态和热区",
      () => {
        setDrawing(false);
        setDirty(false);
        onSaved(key);
      },
    );
  }
  return (
    <form
      className="prototype-v3-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h3>维护状态与热区</h3>
      <fieldset className="prototype-v3-form-fields" disabled={saving}>
        <div className="prototype-v3-tools">
          <Button
            disabled={saving || creating}
            onClick={() => {
              setCreating(true);
              setKey("");
              setName("");
              setAnchors([]);
              setImage(null);
              setInputVersion((value) => value + 1);
              setDrawing(false);
              setDirty(true);
            }}
          >
            新建状态
          </Button>
          <Button
            disabled={saving || !dirty}
            onClick={() => {
              void refresh().then(reset, (error) => setError(error.message));
            }}
          >
            重新载入当前状态
          </Button>
        </div>
        <label>
          状态标识
          <input
            required
            readOnly={!creating}
            value={key}
            onChange={(event) => {
              setKey(event.target.value);
              setDirty(true);
            }}
            pattern={"[a-z][a-z0-9._\\-]{0,95}"}
          />
        </label>
        <label>
          状态名称
          <input
            required
            maxLength={160}
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setDirty(true);
            }}
          />
        </label>
        {canUpload && (
          <>
            <label>
              补录原始 PNG
              <input
                key={inputVersion}
                type="file"
                accept="image/png,.png"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  setImage(file || null);
                  setDirty(true);
                }}
              />
            </label>
            {image && (
              <div className="prototype-v3-tools">
                {(["width", "height", "devicePixelRatio"] as const).map(
                  (axis) => (
                    <label key={axis}>
                      {
                        {
                          width: "视口宽",
                          height: "视口高",
                          devicePixelRatio: "设备像素比",
                        }[axis]
                      }
                      <input
                        required
                        type="number"
                        min={axis === "devicePixelRatio" ? 0.5 : 1}
                        max={axis === "devicePixelRatio" ? 4 : 7680}
                        step={axis === "devicePixelRatio" ? "any" : 1}
                        value={viewport[axis]}
                        onChange={(event) =>
                          setViewport({
                            ...viewport,
                            [axis]: Number(event.target.value),
                          })
                        }
                      />
                    </label>
                  ),
                )}
              </div>
            )}
          </>
        )}
        <div className="prototype-v3-tools">
          <Button onClick={() => add([0.05, 0.05, 0.1, 0.06])}>填写热区</Button>
          {(previewImage || (!creating && state.surface)) && (
            <Button aria-pressed={drawing} onClick={() => setDrawing(!drawing)}>
              在画布绘制
            </Button>
          )}
        </div>
        {anchors.map(
          (anchor, index) =>
            visibleAnchors.includes(anchor) && (
              <fieldset
                key={anchor.anchorKey}
                className={
                  selectedAnchor === anchor.anchorKey
                    ? "prototype-v3-selected-anchor"
                    : undefined
                }
                onFocus={() => setSelectedAnchor(anchor.anchorKey)}
              >
                <legend>热区 {index + 1}</legend>
                <label>
                  动作标识
                  <input readOnly value={anchor.actionKey} />
                </label>
                {!actions.has(anchor.actionKey) && actions.size > 0 && (
                  <label>
                    复用已有动作
                    <select
                      value=""
                      onChange={(event) => {
                        const action = actions.get(event.target.value);
                        if (action)
                          update(index, {
                            actionKey: action.stable_key,
                            label: action.name,
                          });
                      }}
                    >
                      <option value="">保留新动作</option>
                      {[...actions.values()]
                        .filter(
                          (action) =>
                            !anchors.some(
                              (row) => row.actionKey === action.stable_key,
                            ),
                        )
                        .map((action) => (
                          <option key={action.id} value={action.stable_key}>
                            {action.name}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
                <label>
                  动作名称
                  <input
                    required
                    maxLength={160}
                    value={anchor.label}
                    onChange={(event) =>
                      update(index, { label: event.target.value })
                    }
                  />
                </label>
                <div className="prototype-v3-tools">
                  {(["左", "上", "宽", "高"] as const).map((label, offset) => (
                    <label key={label}>
                      {label}
                      <input
                        required
                        type="number"
                        min={0}
                        max={1}
                        step="any"
                        value={anchor.rect[offset]}
                        onChange={(event) => {
                          const next: Rect = [...anchor.rect];
                          next[offset] = Number(event.target.value);
                          update(index, { rect: next });
                        }}
                        onKeyDown={(event) => {
                          if (
                            event.key !== "ArrowUp" &&
                            event.key !== "ArrowDown"
                          )
                            return;
                          event.preventDefault();
                          const delta =
                            (event.key === "ArrowUp" ? 1 : -1) *
                            (event.shiftKey ? 0.01 : 0.001);
                          const next: Rect = [...anchor.rect];
                          next[offset] = Math.max(
                            0,
                            Math.min(
                              1,
                              Math.round((next[offset] + delta) * 10000) /
                                10000,
                            ),
                          );
                          update(index, { rect: next });
                        }}
                      />
                    </label>
                  ))}
                </div>
                <label>
                  目标状态
                  <select
                    value={anchor.targetStateKey || ""}
                    onChange={(event) =>
                      update(index, {
                        targetStateKey: event.target.value || undefined,
                      })
                    }
                  >
                    <option value="">留在当前状态</option>
                    {manifest.states.map((row) => (
                      <option key={row.stateKey} value={row.stateKey}>
                        {row.name}
                      </option>
                    ))}
                  </select>
                </label>
                <Button
                  onClick={() => {
                    setAnchors((rows) =>
                      rows.filter((_, offset) => index !== offset),
                    );
                    setDirty(true);
                  }}
                >
                  移除热区
                </Button>
              </fieldset>
            ),
        )}
        {changed && (
          <Notice>
            当前草稿已更新。保存时会核对原修订，本地输入仍可查看。
          </Notice>
        )}
        {error && <Notice tone="red">{error}</Notice>}
        <Button type="submit" disabled={saving || !dirty}>
          {saving ? "保存中" : "保存状态"}
        </Button>
      </fieldset>
    </form>
  );
}

export function PrototypeBindingEditor({
  page,
  action,
  state,
  manifest,
  dirty,
  setDirty,
}: {
  page: Entity;
  action: Entity;
  state: PrototypeState;
  manifest: PrototypeManifest;
  dirty: boolean;
  setDirty: (dirty: boolean) => void;
}) {
  const { w, refresh } = useWorkspace();
  const [editing, setEditing] = useState("");
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [applicability, setApplicability] = useState("");
  const [outcome, setOutcome] = useState("");
  const [exceptions, setExceptions] = useState("");
  const [search, setSearch] = useState("");
  const [sources, setSources] = useState<string[]>([]);
  const [sourcesChanged, setSourcesChanged] = useState(false);
  const [stateKeys, setStateKeys] = useState([state.stateKey]);
  const [allStates, setAllStates] = useState(false);
  const [priority, setPriority] = useState(0);
  const [reuse, setReuse] = useState(false);
  const { saving, error, setError, request, save } = usePrototypeSave();
  const draft = w.selected_analysis_set;
  const existing = w.entities.find(
    (row) => row.type === "business_point" && row.stable_key === editing,
  );
  function bindingFor(point: Entity | undefined) {
    return w.relations.find(
      (row) =>
        row.relation_type === "has_business_point" &&
        row.from_revision_id === action.entity_revision_id &&
        row.to_revision_id === point?.entity_revision_id,
    );
  }
  const binding = bindingFor(existing);
  const current = {
    pageRevision: page.entity_revision_id,
    actionRevision: action.entity_revision_id,
    version: draft?.selection_version,
    pointRevision: existing?.entity_revision_id,
    bindingVersion: binding?.row_version,
  };
  const loaded = useRef(current);
  const changed = Object.keys(current).some((key) => {
    const field = key as keyof typeof current;
    return current[field] !== loaded.current[field];
  });
  const sectionByRevision = new Map(
    w.entities
      .filter((row) => row.type === "section")
      .map((row) => [row.entity_revision_id, row]),
  );
  const documentNames = new Map(
    w.documents.map((row) => [row.stable_key, row.name]),
  );
  function sourceLabel(row: Entity) {
    const document = documentNames.get(String(row.metadata.document_key));
    const line = Number(row.locator.startLine ?? row.locator.line_start);
    return [
      document,
      row.name,
      Number.isInteger(line) && line > 0 ? `第 ${line} 行` : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  const sectionMatches = [...sectionByRevision.values()]
    .filter((row) =>
      `${sourceLabel(row)} ${row.stable_key}`
        .toLocaleLowerCase()
        .includes(search.toLocaleLowerCase()),
    )
    .slice(0, 100);
  const applicableStates = manifest.states.filter((row) =>
    row.anchors.some((anchor) => anchor.actionKey === action.stable_key),
  );
  function load(pointKey: string, newPoint = false) {
    const row = w.entities.find(
      (row) => row.type === "business_point" && row.stable_key === pointKey,
    );
    const edge = bindingFor(row);
    loaded.current = {
      ...current,
      pointRevision: row?.entity_revision_id,
      bindingVersion: edge?.row_version,
    };
    setEditing(pointKey);
    setKey(pointKey || (newPoint ? "point." + crypto.randomUUID() : ""));
    setTitle(row?.name || "");
    setSummary(row?.content || "");
    setApplicability(String(row?.metadata.applicability || ""));
    setOutcome(String(row?.metadata.outcome || ""));
    setExceptions(String(row?.metadata.exceptions || ""));
    setSources(
      w.relations
        .filter(
          (link) =>
            !link.excluded &&
            link.relation_type === "supported_by_section" &&
            link.from_revision_id === row?.entity_revision_id &&
            sectionByRevision.has(link.to_revision_id),
        )
        .map((link) => link.to_revision_id),
    );
    setSourcesChanged(false);
    setReuse(!!row);
    setPriority(edge?.prototype_scope?.priority || 0);
    const states =
      edge?.prototype_scope?.states ?? (edge ? [] : [state.stateKey]);
    setStateKeys(states);
    setAllStates(states.length === 0);
    setSearch("");
    setDirty(newPoint);
    setError("");
  }
  useEffect(() => {
    if (editing && !existing && !saving) load("");
    else if (!dirty && !saving && changed) load(editing);
  }, [changed, dirty, existing, saving]);
  function toggleSource(id: string, checked: boolean) {
    setSources((rows) =>
      checked ? [...rows, id] : rows.filter((row) => row !== id),
    );
    setSourcesChanged(true);
    setDirty(true);
  }
  async function submit(excluded = false) {
    if (!draft || saving) return;
    const validStateKeys = stateKeys.filter((key) =>
      applicableStates.some((candidate) => candidate.stateKey === key),
    );
    if (!excluded && !allStates && validStateKeys.length !== stateKeys.length) {
      setError("适用状态包含已移除的热区，请重新选择适用状态。");
      return;
    }
    setDirty(true);
    await save(
      () =>
        request(
          `${prototypeApi(w.project.id)}/actions/${encodeURIComponent(action.stable_key)}/business-points`,
          "POST",
          {
            analysis_set_id: draft.id,
            page_revision_id: loaded.current.pageRevision,
            action_revision_id: loaded.current.actionRevision,
            point_key: key,
            point_revision_id: loaded.current.pointRevision,
            binding_version: loaded.current.bindingVersion,
            state_keys: allStates || excluded ? [] : validStateKeys,
            priority,
            excluded,
            ...(!excluded && !reuse
              ? {
                  point: {
                    title,
                    summary,
                    applicability,
                    outcome,
                    exceptions,
                  },
                  ...(!existing || sourcesChanged
                    ? { section_revision_ids: sources }
                    : {}),
                }
              : {}),
          },
          loaded.current.version,
        ),
      excluded ? "已移除业务点关联" : "已保存业务点关联",
      () => {
        setEditing(key);
        setDirty(false);
      },
    );
  }
  return (
    <form
      className="prototype-v3-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <h3>维护业务点</h3>
      <fieldset className="prototype-v3-form-fields" disabled={saving}>
        <label>
          选择业务点
          <select
            value={editing}
            disabled={dirty || saving}
            onChange={(event) => load(event.target.value)}
          >
            <option value="">新建业务点</option>
            {w.entities
              .filter((row) => row.type === "business_point")
              .map((row) => (
                <option key={row.id} value={row.stable_key}>
                  {row.name}
                </option>
              ))}
          </select>
        </label>
        {!key && (
          <Button disabled={saving} onClick={() => load("", true)}>
            开始新建
          </Button>
        )}
        {key && (!editing || existing) && (
          <>
            {existing && (
              <label>
                <input
                  type="checkbox"
                  checked={reuse}
                  onChange={(event) => {
                    setReuse(event.target.checked);
                    setDirty(true);
                  }}
                />
                复用当前说明和出处
              </label>
            )}
            {!reuse && (
              <>
                <label>
                  业务点标题
                  <input
                    required
                    maxLength={160}
                    value={title}
                    onChange={(event) => {
                      setTitle(event.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
                <label>
                  业务说明
                  <textarea
                    aria-label="业务说明"
                    required
                    maxLength={8000}
                    value={summary}
                    onChange={(event) => {
                      setSummary(event.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
                <label>
                  适用条件
                  <textarea
                    maxLength={2000}
                    value={applicability}
                    onChange={(event) => {
                      setApplicability(event.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
                <label>
                  结果与影响
                  <textarea
                    maxLength={2000}
                    value={outcome}
                    onChange={(event) => {
                      setOutcome(event.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
                <label>
                  例外
                  <textarea
                    maxLength={2000}
                    value={exceptions}
                    onChange={(event) => {
                      setExceptions(event.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
                <label>
                  搜索原文章节
                  <input
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                  />
                </label>
                {sources.length > 0 && (
                  <fieldset>
                    <legend>已选原文章节</legend>
                    {sources.map((id) => {
                      const section = sectionByRevision.get(id);
                      return (
                        <label key={id}>
                          <input
                            type="checkbox"
                            checked
                            onChange={() => toggleSource(id, false)}
                          />
                          {section ? sourceLabel(section) : "章节修订已不可用"}
                        </label>
                      );
                    })}
                    {existing && (
                      <Button
                        onClick={() => {
                          setSourcesChanged(true);
                          setDirty(true);
                        }}
                      >
                        重新确认已选出处
                      </Button>
                    )}
                  </fieldset>
                )}
                <fieldset className="prototype-v3-section-matches">
                  <legend>选择原文章节</legend>
                  {sectionMatches
                    .filter((row) => !sources.includes(row.entity_revision_id))
                    .map((row) => (
                      <label key={row.id}>
                        <input
                          type="checkbox"
                          checked={false}
                          disabled={sources.length >= 20}
                          onChange={() =>
                            toggleSource(row.entity_revision_id, true)
                          }
                        />
                        {sourceLabel(row)}
                      </label>
                    ))}
                </fieldset>
                {!sources.length && <Notice>这条说明将标为维护说明。</Notice>}
              </>
            )}
            <fieldset>
              <legend>适用状态</legend>
              <label>
                <input
                  type="checkbox"
                  checked={allStates}
                  onChange={(event) => {
                    setAllStates(event.target.checked);
                    if (!event.target.checked && !stateKeys.length)
                      setStateKeys([state.stateKey]);
                    setDirty(true);
                  }}
                />
                所有触发状态
              </label>
              {!allStates &&
                applicableStates.map((row) => (
                  <label key={row.stateKey}>
                    <input
                      type="checkbox"
                      checked={stateKeys.includes(row.stateKey)}
                      onChange={(event) => {
                        setStateKeys((keys) =>
                          event.target.checked
                            ? [...keys, row.stateKey]
                            : keys.filter((key) => key !== row.stateKey),
                        );
                        setDirty(true);
                      }}
                    />
                    {row.name}
                  </label>
                ))}
              {!allStates &&
                stateKeys
                  .filter(
                    (key) =>
                      !applicableStates.some(
                        (candidate) => candidate.stateKey === key,
                      ),
                  )
                  .map((key) => (
                    <label key={key}>
                      <input
                        type="checkbox"
                        checked
                        onChange={() => {
                          setStateKeys((keys) =>
                            keys.filter((stateKey) => stateKey !== key),
                          );
                          setDirty(true);
                        }}
                      />
                      已移除热区 ·{" "}
                      {manifest.states.find((row) => row.stateKey === key)
                        ?.name || key}
                    </label>
                  ))}
            </fieldset>
            <label>
              显示顺序
              <input
                type="number"
                min={0}
                max={1000}
                value={priority}
                onChange={(event) => {
                  setPriority(Number(event.target.value));
                  setDirty(true);
                }}
              />
            </label>
            <div className="prototype-v3-tools">
              <Button
                type="submit"
                disabled={saving || (!allStates && !stateKeys.length)}
              >
                {binding?.excluded ? "恢复关联" : "保存业务点"}
              </Button>
              {binding && !binding.excluded && (
                <Button disabled={saving} onClick={() => void submit(true)}>
                  移除关联
                </Button>
              )}
              {dirty && (
                <Button
                  disabled={saving}
                  onClick={() => {
                    void refresh().then(
                      () => load(editing),
                      (error) => setError(error.message),
                    );
                  }}
                >
                  重新载入业务点
                </Button>
              )}
            </div>
            {!reuse && title.trim() && summary.trim() && (
              <details>
                <summary>预览业务说明</summary>
                <strong>{title}</strong>
                <p>{summary}</p>
                {applicability && <p>适用条件　{applicability}</p>}
                {outcome && <p>结果与影响　{outcome}</p>}
                {exceptions && <p>例外　{exceptions}</p>}
              </details>
            )}
          </>
        )}
        {dirty && changed && (
          <Notice>
            当前草稿已更新。保存时会核对原修订，本地输入仍可查看。
          </Notice>
        )}
        {error && <Notice tone="red">{error}</Notice>}
      </fieldset>
    </form>
  );
}
