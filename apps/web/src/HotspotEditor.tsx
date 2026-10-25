import "./hotspot-editor.css";
import { useRef, useState, type PointerEvent, type ReactNode } from "react";
import { Pencil, Plus, Save, Trash2 } from "lucide-react";
import { api, ApiError, mutation } from "./api";
import {
  Button,
  Modal,
  Notice,
  PreviewBoundary,
  SafePrototype,
  Tag,
} from "./components";
import { ArtifactImage } from "./ArtifactImage";
import { NewDraftButton } from "./DraftActions";
import { usePermission, useWorkspace } from "./context";
import {
  normalizedRect,
  parsePassiveSelector,
  validRect,
  type HotspotRect,
} from "./hotspots";
import type { Entity } from "./types";

const initialRect = { x: 0.1, y: 0.1, width: 0.25, height: 0.12 };
function screenshot(page: Entity) {
  return page.metadata?.artifact_kind === "screenshot";
}
export function HotspotSurface({
  page,
  actions,
  selected,
  onSelect,
  children,
}: {
  page: Entity;
  actions: Entity[];
  selected?: string;
  onSelect: (id: string) => void;
  children: ReactNode;
}) {
  return (
    <div className="hotspot-surface">
      {children}
      <div className="authored-hotspots">
        {actions
          .filter(
            (action) =>
              action.metadata?.status !== "removed" &&
              validRect(action.metadata?.rect) &&
              (screenshot(page) || !action.metadata?.selector),
          )
          .map((action) => {
            const rect = action.metadata.rect as unknown as HotspotRect;
            return (
              <button
                type="button"
                className={
                  "authored-hotspot " + (action.id === selected ? "active" : "")
                }
                key={action.id}
                style={{
                  left: rect.x * 100 + "%",
                  top: rect.y * 100 + "%",
                  width: rect.width * 100 + "%",
                  height: rect.height * 100 + "%",
                }}
                title={"选择动作：" + action.name}
                aria-label={"选择动作：" + action.name}
                onClick={() => onSelect(action.id)}
              >
                <span>{action.name}</span>
              </button>
            );
          })}
      </div>
    </div>
  );
}
export function HotspotEditor({
  page,
  actions,
}: {
  page: Entity;
  actions: Entity[];
}) {
  const { w, notify, refresh, setSelected } = useWorkspace();
  const canEdit = usePermission("knowledge:edit");
  const [baseAnalysisId, setBaseAnalysisId] = useState(
    w.selected_analysis_set?.id || "",
  );
  const [open, setOpen] = useState(false),
    [basePage, setBasePage] = useState(page),
    [actionId, setActionId] = useState(""),
    [actionKey, setActionKey] = useState(""),
    [label, setLabel] = useState(""),
    [rect, setRect] = useState<HotspotRect>(initialRect),
    [selector, setSelector] = useState(""),
    [placement, setPlacement] = useState<"area" | "selector">("area"),
    [targetState, setTargetState] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [deleting, setDeleting] = useState(false);
  const canvas = useRef<HTMLDivElement>(null),
    dragStart = useRef<{ x: number; y: number } | null>(null);
  const activeActions = actions.filter(
    (action) => action.metadata?.status !== "removed",
  );
  function choose(id: string) {
    const action = activeActions.find((row) => row.id === id);
    setActionId(id);
    setActionKey(action?.stable_key || "");
    setLabel(action?.name || "");
    setRect(
      validRect(action?.metadata?.rect) ? action.metadata.rect : initialRect,
    );
    setSelector(String(action?.metadata?.selector || ""));
    setPlacement(action?.metadata?.selector ? "selector" : "area");
    setTargetState(String(action?.metadata?.target_state_key || ""));
    setDeleting(false);
    setError("");
  }
  function point(event: PointerEvent) {
    const bounds = canvas.current!.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
      y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)),
    };
  }
  function start(event: PointerEvent<HTMLDivElement>) {
    if (placement !== "area" || event.button !== 0) return;
    event.preventDefault();
    dragStart.current = point(event);
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    if (dragStart.current)
      setRect(normalizedRect(dragStart.current, point(event)));
  }
  function finish(event: PointerEvent<HTMLDivElement>) {
    if (!dragStart.current) return;
    const next = normalizedRect(dragStart.current, point(event));
    dragStart.current = null;
    setRect(
      next.width < 0.005 || next.height < 0.005
        ? {
            x: Math.min(next.x, 0.8),
            y: Math.min(next.y, 0.9),
            width: 0.2,
            height: 0.1,
          }
        : next,
    );
    event.currentTarget.releasePointerCapture(event.pointerId);
  }
  async function save(remove = false) {
    if (!baseAnalysisId) return;
    setBusy(true);
    setError("");
    try {
      const response = await api<{ action: Entity }>(
        "/projects/" +
          w.project.id +
          "/pages/" +
          basePage.id +
          "/hotspots" +
          (remove ? "/" + actionId : ""),
        mutation(
          remove ? "DELETE" : "POST",
          {
            analysis_set_id: baseAnalysisId,
            base_page_revision_id: basePage.entity_revision_id,
            ...(remove
              ? {}
              : {
                  ...(actionId ? { action_id: actionId } : {}),
                  action_key: actionKey.trim(),
                  label: label.trim(),
                  rect,
                  ...(placement === "selector" && selector.trim()
                    ? { selector: selector.trim() }
                    : {}),
                  ...(targetState.trim()
                    ? { target_state_key: targetState.trim() }
                    : {}),
                }),
          },
          basePage.row_version || Number(basePage.metadata?.row_version) || 1,
        ),
      );
      notify(
        remove
          ? "热点已在新修订中移除，历史版本与关联仍可追溯"
          : "热点已保存为新修订，后续关联仍需审核",
      );
      setOpen(false);
      if (!remove && response.action) setSelected(response.action.id);
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 412
          ? "页面修订已更新，当前输入保留。请复制需要的变更，关闭后刷新并重新选择当前页面再保存。"
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  }
  if (!canEdit) return null;
  if (w.selected_analysis_set?.state === "frozen")
    return <NewDraftButton small />;
  return (
    <>
      <Button
        kind="small"
        onClick={() => {
          setBasePage(page);
          setBaseAnalysisId(w.selected_analysis_set?.id || "");
          choose("");
          setOpen(true);
        }}
      >
        <Pencil size={14} />
        编辑热点
      </Button>
      {open && (
        <Modal
          className="hotspot-editor-dialog"
          title={"编辑页面热点 · " + basePage.name}
          onClose={() => setOpen(false)}
        >
          <Notice>
            拖画区域，或指定安全 DOM
            选择器。每次保存创建独立页面与动作修订；不执行原型脚本，也不将人工热点视为运行证据。
          </Notice>
          <div className="hotspot-editor-layout">
            <div>
              <div
                ref={canvas}
                className={
                  "hotspot-draw-area " + (placement === "area" ? "drawing" : "")
                }
                onPointerDown={start}
                onPointerMove={move}
                onPointerUp={finish}
                onPointerCancel={() => {
                  dragStart.current = null;
                }}
              >
                {screenshot(basePage) && basePage.artifact_id ? (
                  <ArtifactImage
                    projectId={w.project.id}
                    artifactId={basePage.artifact_id}
                    name={basePage.name}
                    bare
                  />
                ) : (
                  <PreviewBoundary>
                    <SafePrototype
                      html={basePage.content || ""}
                      actions={[]}
                      onSelect={() => {}}
                    />
                  </PreviewBoundary>
                )}
                {validRect(rect) && placement === "area" && (
                  <div
                    className="hotspot-draft-rect"
                    style={{
                      left: rect.x * 100 + "%",
                      top: rect.y * 100 + "%",
                      width: rect.width * 100 + "%",
                      height: rect.height * 100 + "%",
                    }}
                  >
                    <span>{label || "新热点"}</span>
                  </div>
                )}
              </div>
              <p className="section-description">
                区域按静态预览的相对比例保存。布局或截图版本变化后需要复核；单击可放置默认区域，再微调尺寸。
              </p>
            </div>
            <div className="hotspot-editor-fields">
              <label>
                编辑动作
                <select
                  value={actionId}
                  onChange={(e) => choose(e.target.value)}
                >
                  <option value="">＋ 创建新动作</option>
                  {activeActions.map((action) => (
                    <option key={action.id} value={action.id}>
                      {action.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                稳定动作标识
                <input
                  required
                  readOnly={!!actionId}
                  pattern="[A-Za-z][A-Za-z0-9._-]*"
                  maxLength={160}
                  value={actionKey}
                  onChange={(e) => setActionKey(e.target.value)}
                  placeholder="ui.invoice.submit"
                />
              </label>
              <label>
                动作名称
                <input
                  required
                  maxLength={160}
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder="例如：确认开票"
                />
              </label>
              <label>
                定位方式
                <select
                  value={placement}
                  onChange={(e) =>
                    setPlacement(e.target.value as "area" | "selector")
                  }
                >
                  <option value="area">区域坐标</option>
                  <option value="selector" disabled={screenshot(basePage)}>
                    安全 DOM 选择器
                  </option>
                </select>
              </label>
              {placement === "selector" ? (
                <label>
                  DOM 选择器
                  <input
                    value={selector}
                    onChange={(e) => setSelector(e.target.value)}
                    placeholder={'button[data-testid="submit"]'}
                  />
                  <small>
                    支持单个 tag、#id、.class、[attr="值"]
                    及按此顺序组合；需唯一匹配可见元素。
                  </small>
                </label>
              ) : (
                <div className="hotspot-coordinates">
                  {(["x", "y", "width", "height"] as const).map((key) => (
                    <label key={key}>
                      {{ x: "X", y: "Y", width: "宽", height: "高" }[key]}（%）
                      <input
                        type="number"
                        min={key === "width" || key === "height" ? 0.1 : 0}
                        max={100}
                        step={0.1}
                        value={Number((rect[key] * 100).toFixed(2))}
                        onChange={(e) =>
                          setRect((previous) => ({
                            ...previous,
                            [key]: Number(e.target.value) / 100,
                          }))
                        }
                      />
                    </label>
                  ))}
                </div>
              )}
              <label>
                目标讲解状态（可选）
                <input
                  value={targetState}
                  maxLength={160}
                  onChange={(e) => setTargetState(e.target.value)}
                  placeholder="当前场景中已有的 stateKey"
                />
              </label>
              {actionId && (
                <Button
                  kind="small"
                  disabled={busy}
                  onClick={() => setDeleting(true)}
                >
                  <Trash2 size={14} />
                  移除当前热点
                </Button>
              )}
            </div>
          </div>
          {error && <Notice tone="red">{error}</Notice>}
          {deleting && (
            <Notice tone="amber">
              将从新页面修订中移除当前热点，旧版本继续保留。
              <Button
                kind="small"
                disabled={busy}
                onClick={() => {
                  void save(true);
                }}
              >
                确认移除热点
              </Button>
            </Notice>
          )}
          <div className="modal-actions">
            <Tag value="manual">人工热点</Tag>
            <Button onClick={() => setOpen(false)}>取消</Button>
            <Button
              kind="primary"
              disabled={
                busy ||
                !actionKey.trim() ||
                !label.trim() ||
                !validRect(rect) ||
                (placement === "selector" &&
                  !parsePassiveSelector(selector.trim()))
              }
              onClick={() => {
                void save();
              }}
            >
              <Save size={15} />
              保存新修订
            </Button>
            <Button kind="small" disabled={busy} onClick={() => choose("")}>
              <Plus size={14} />
              另建动作
            </Button>
          </div>
        </Modal>
      )}
    </>
  );
}
