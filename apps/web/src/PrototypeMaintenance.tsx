import { useEffect, useState } from "react";
import { api, mutation, sha256 } from "./api";
import { Button, Notice } from "./components";
import { usePermission, useWorkspace } from "./context";
import {
  prototypeApi,
  type BusinessPoint,
  type PrototypeAnchor,
  type PrototypeManifest,
  type PrototypeState,
} from "./prototype";
import type { Entity } from "./types";

type EditableAnchor = PrototypeAnchor & { label: string };
type Rect = PrototypeAnchor["rect"];

export function PrototypeStateEditor({
  page,
  state,
  manifest,
  actions,
  drawing,
  setDrawing,
  drawn,
}: {
  page: Entity;
  state: PrototypeState;
  manifest: PrototypeManifest;
  actions: Map<string, Entity>;
  drawing: boolean;
  setDrawing: (active: boolean) => void;
  drawn: Rect | null;
}) {
  const { w, run, refresh, navigateExplanation } = useWorkspace();
  const canUpload = usePermission("source:import");
  const [key, setKey] = useState(state.stateKey);
  const [name, setName] = useState(state.name);
  const [image, setImage] = useState<File | null>(null);
  const [viewport, setViewport] = useState(
    state.viewport || { width: 1440, height: 900, devicePixelRatio: 1 },
  );
  const [anchors, setAnchors] = useState<EditableAnchor[]>(
    state.anchors.map((anchor) => ({
      ...anchor,
      label: actions.get(anchor.actionKey)?.name || "",
    })),
  );
  const [saving, setSaving] = useState(false);
  function add(rect: Rect) {
    const key = "control." + crypto.randomUUID();
    setAnchors((rows) => [
      ...rows,
      { anchorKey: key, actionKey: key, label: "", rect },
    ]);
  }
  useEffect(() => {
    if (drawn) add(drawn);
  }, [drawn]);
  function update(index: number, update: Partial<EditableAnchor>) {
    setAnchors((rows) =>
      rows.map((row, offset) =>
        offset === index ? { ...row, ...update } : row,
      ),
    );
  }
  async function save() {
    const draft = w.selected_analysis_set;
    if (!draft || saving) return;
    setSaving(true);
    try {
      const changed = await run(async () => {
        let uploadId: string | undefined;
        if (image) {
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
        }
        return api<{ page_revision_id: string }>(
          `${prototypeApi(w.project.id)}/pages/${encodeURIComponent(page.stable_key)}/anchors`,
          mutation(
            "PUT",
            {
              analysis_set_id: draft.id,
              page_revision_id: page.entity_revision_id,
              state_key: key,
              name,
              anchors,
              ...(uploadId ? { upload_artifact_id: uploadId, viewport } : {}),
            },
            draft.selection_version,
          ),
        );
      }, "已保存状态和热区");
      if (changed) {
        setDrawing(false);
        await refresh();
      }
    } finally {
      setSaving(false);
    }
  }
  return (
    <form
      className="prototype-v3-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>维护状态与热区</h3>
      <div className="prototype-v3-tools">
        <Button
          onClick={() => {
            setKey("");
            setName("");
            setAnchors([]);
            setImage(null);
            setDrawing(false);
          }}
        >
          新建状态
        </Button>
        <Button
          onClick={() =>
            navigateExplanation({
              pageId: page.id,
              stateKey: state.stateKey,
              actionId: "",
            })
          }
        >
          返回当前状态
        </Button>
      </div>
      <label>
        状态标识
        <input
          required
          value={key}
          onChange={(event) => setKey(event.target.value)}
          pattern="[a-z][a-z0-9._\\-]{0,95}"
        />
      </label>
      <label>
        状态名称
        <input
          required
          maxLength={160}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </label>
      {canUpload && (
        <>
          <label>
            补录原始 PNG
            <input
              type="file"
              accept="image/png,.png"
              onChange={(event) => {
                const file = event.target.files?.[0];
                setImage(file || null);
                if (file)
                  void createImageBitmap(file)
                    .then((bitmap) => {
                      setViewport({
                        width: bitmap.width,
                        height: bitmap.height,
                        devicePixelRatio: 1,
                      });
                      bitmap.close();
                    })
                    .catch(() => {});
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
        {state.surface && (
          <Button aria-pressed={drawing} onClick={() => setDrawing(!drawing)}>
            在画布绘制
          </Button>
        )}
      </div>
      {anchors.map((anchor, index) => (
        <fieldset key={anchor.anchorKey}>
          <legend>热区 {index + 1}</legend>
          <label>
            动作标识
            <input
              required
              value={anchor.actionKey}
              onChange={(event) =>
                update(index, { actionKey: event.target.value })
              }
            />
          </label>
          <label>
            动作名称
            <input
              required
              maxLength={160}
              value={anchor.label}
              onChange={(event) => update(index, { label: event.target.value })}
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
            onClick={() =>
              setAnchors((rows) => rows.filter((_, offset) => index !== offset))
            }
          >
            移除热区
          </Button>
        </fieldset>
      ))}
      <Button type="submit" disabled={saving}>
        {saving ? "保存中" : "保存状态"}
      </Button>
    </form>
  );
}

export function PrototypeBindingEditor({
  page,
  action,
  state,
  points,
}: {
  page: Entity;
  action: Entity;
  state: PrototypeState;
  points: BusinessPoint[];
}) {
  const { w, run, refresh } = useWorkspace();
  const [editing, setEditing] = useState("");
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [applicability, setApplicability] = useState("");
  const [outcome, setOutcome] = useState("");
  const [exceptions, setExceptions] = useState("");
  const [search, setSearch] = useState("");
  const [sources, setSources] = useState<string[]>([]);
  const [currentOnly, setCurrentOnly] = useState(true);
  const [priority, setPriority] = useState(0);
  const [reuse, setReuse] = useState(false);
  const [saving, setSaving] = useState(false);
  const existing = w.entities.find(
    (row) => row.type === "business_point" && row.stable_key === editing,
  );
  const binding = w.relations.find(
    (row) =>
      row.relation_type === "has_business_point" &&
      row.from_revision_id === action.entity_revision_id &&
      row.to_revision_id === existing?.entity_revision_id,
  );
  const sections = w.entities.filter(
    (row) =>
      row.type === "section" &&
      (sources.includes(row.entity_revision_id) ||
        `${row.name} ${row.stable_key}`
          .toLocaleLowerCase()
          .includes(search.toLocaleLowerCase())),
  );
  function load(pointKey: string) {
    const row = w.entities.find(
      (row) => row.type === "business_point" && row.stable_key === pointKey,
    );
    const point = points.find((row) => row.point_key === pointKey);
    setEditing(pointKey);
    setKey(pointKey || "point." + crypto.randomUUID());
    setTitle(row?.name || "");
    setSummary(row?.content || "");
    setApplicability(String(row?.metadata.applicability || ""));
    setOutcome(String(row?.metadata.outcome || ""));
    setExceptions(String(row?.metadata.exceptions || ""));
    setSources(
      point?.sources.map((source) => source.section_revision_id) || [],
    );
    setReuse(!!row);
    setPriority(point?.priority || 0);
    setCurrentOnly(point ? point.state_keys.length > 0 : true);
  }
  async function save(excluded = false) {
    const draft = w.selected_analysis_set;
    if (!draft || saving) return;
    setSaving(true);
    try {
      const result = await run(
        () =>
          api<{ point_revision_id: string }>(
            `${prototypeApi(w.project.id)}/actions/${encodeURIComponent(action.stable_key)}/business-points`,
            mutation(
              "POST",
              {
                analysis_set_id: draft.id,
                page_revision_id: page.entity_revision_id,
                action_revision_id: action.entity_revision_id,
                point_key: key,
                point_revision_id: existing?.entity_revision_id,
                binding_version: binding?.row_version,
                state_keys: currentOnly ? [state.stateKey] : [],
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
                      section_revision_ids: sources,
                    }
                  : {}),
              },
              draft.selection_version,
            ),
          ),
        excluded ? "已移除业务点关联" : "已保存业务点关联",
      );
      if (result) await refresh();
    } finally {
      setSaving(false);
    }
  }
  return (
    <form
      className="prototype-v3-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <h3>维护业务点</h3>
      <label>
        选择业务点
        <select value={editing} onChange={(event) => load(event.target.value)}>
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
      {!key && <Button onClick={() => load("")}>开始新建</Button>}
      {key && (
        <>
          {existing && (
            <label>
              <input
                type="checkbox"
                checked={reuse}
                onChange={(event) => setReuse(event.target.checked)}
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
                  onChange={(event) => setTitle(event.target.value)}
                />
              </label>
              <label>
                业务说明
                <textarea
                  required
                  maxLength={8000}
                  value={summary}
                  onChange={(event) => setSummary(event.target.value)}
                />
              </label>
              <label>
                适用条件
                <textarea
                  maxLength={2000}
                  value={applicability}
                  onChange={(event) => setApplicability(event.target.value)}
                />
              </label>
              <label>
                结果与影响
                <textarea
                  maxLength={2000}
                  value={outcome}
                  onChange={(event) => setOutcome(event.target.value)}
                />
              </label>
              <label>
                例外
                <textarea
                  maxLength={2000}
                  value={exceptions}
                  onChange={(event) => setExceptions(event.target.value)}
                />
              </label>
              <label>
                搜索原文章节
                <input
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
              <label>
                原文章节
                <select
                  multiple
                  aria-label="原文章节"
                  value={sources}
                  onChange={(event) =>
                    setSources(
                      [...event.target.selectedOptions].map((row) => row.value),
                    )
                  }
                >
                  {sections.slice(0, 100).map((row) => (
                    <option key={row.id} value={row.entity_revision_id}>
                      {
                        w.documents.find(
                          (doc) => doc.stable_key === row.metadata.document_key,
                        )?.name
                      }{" "}
                      · {row.name}
                    </option>
                  ))}
                </select>
              </label>
              {!sources.length && <Notice>这条说明将标为维护说明。</Notice>}
            </>
          )}
          <label>
            <input
              type="checkbox"
              checked={currentOnly}
              onChange={(event) => setCurrentOnly(event.target.checked)}
            />
            只用于当前触发状态
          </label>
          <label>
            显示顺序
            <input
              type="number"
              min={0}
              max={1000}
              value={priority}
              onChange={(event) => setPriority(Number(event.target.value))}
            />
          </label>
          <div className="prototype-v3-tools">
            <Button type="submit" disabled={saving || sources.length > 20}>
              {binding?.excluded ? "恢复关联" : "保存业务点"}
            </Button>
            {binding && !binding.excluded && (
              <Button disabled={saving} onClick={() => void save(true)}>
                移除关联
              </Button>
            )}
          </div>
        </>
      )}
    </form>
  );
}
