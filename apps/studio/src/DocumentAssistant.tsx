import { showSelectionMenu } from "../../../packages/markdown-runtime/src/selectionMenu";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, apiResponse, mutation } from "../../web/src/api";
import { conversationAnswer } from "./chatStream";
import type { MarkdownReference } from "./RichMarkdown";
import { saveDisplaySpeed, useDisplaySpeed } from "./chatPreferences";
import { useFloatingWindow, type ResizeDirection } from "./useFloatingWindow";
import {
  DetachedAssistantWindow,
  DocumentAssistantFrame,
} from "../../../packages/assistant-runtime/src/detached";
import { startFloatingGesture } from "../../../packages/assistant-runtime/src/floating";

type Operation = {
  kind: string;
  title: string;
  document_id?: string;
  revision_id?: string;
  job_id?: string;
};
type Answer = {
  explanation: string;
  sections: MarkdownReference[];
  unknowns: string[];
  operations?: Operation[];
};
type Question = {
  id: string;
  question: string;
  history: { role: string; content: string }[];
  scope: string;
  mode: string;
  selection: string;
  document_id: string;
};

// The Sites shell is a versioned application asset without any project data.
const hostedShell = typeof document !== "undefined" && document.querySelector<HTMLMetaElement>(
  'meta[name="vks-document-assistant"]',
)?.content;
let cachedShell: Promise<string> | undefined;

function assistantSource(signal?: AbortSignal) {
  if (!hostedShell || typeof DecompressionStream !== "function")
    return apiResponse("/api/v3/document-templates/handbook/assistant", { signal })
      .then((response) => response.text());
  return cachedShell ??= fetch(hostedShell, {
    credentials: "same-origin",
    redirect: "error",
  }).then(async (response) => {
    if (!response.ok) throw new Error("文档助手资源加载失败。");
    const bytes = new Uint8Array(await response.arrayBuffer());
    // If the serving layer already applied Content-Encoding, fetch has decoded it.
    if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
      return new Response(stream).text();
    }
    return new TextDecoder().decode(bytes);
  }).catch((error) => {
    cachedShell = undefined;
    throw error;
  });
}

/** Original handbook UI; the host alone owns authenticated API requests. */
export function DocumentAssistant(props: {
  visible: boolean;
  reading: boolean;
  scopeKey: string;
  prefix: string;
  baseline: { analysis_set_id?: string; release_id?: string };
  document?: { revision_id: string; name: string };
  documents: number;
  detachedWindow: DetachedAssistantWindow;
  detachedOpen: boolean;
  onDetach: () => void;
  onClose: () => void;
  onAskSelection: () => void;
  onSettings: () => void;
  onIndex: () => void;
  onCitation: (reference: MarkdownReference) => void;
  onUpdated: (operations: Operation[]) => Promise<void>;
}) {
  const windowKey =
    "vks.document-window:" + props.prefix + JSON.stringify(props.baseline);
  const [mode, setMode] = useState<"floating" | "docked">(() => {
    try {
      return localStorage.getItem(windowKey + ":mode") === "floating"
        ? "floating"
        : "docked";
    } catch {
      return "docked";
    }
  });
  const [dockWidth, setDockWidth] = useState(() => {
    try {
      return Number(localStorage.getItem(windowKey + ":width")) || 560;
    } catch {
      return 560;
    }
  });
  const floating = useFloatingWindow(mode === "docked", windowKey + ":box", {
    left: Math.max(12, innerWidth - 584),
    top: 80,
    width: 560,
    height: Math.min(720, innerHeight - 104),
  });
  const sideGesture = useRef<(() => void) | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const speed = useDisplaySpeed();
  const inlineHost = useRef<HTMLDivElement>(null);
  const view = useRef<DocumentAssistantFrame | null>(null);
  const receiveRef = useRef<
    (type: string, payload: Record<string, any>) => void
  >(() => {});
  const channel = useRef(crypto.randomUUID());
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [ready, setReady] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [selectionToExplain, setSelectionToExplain] = useState("");
  const active = useRef<{ id: string; controller: AbortController } | null>(
    null,
  );
  const citations = useRef(new Map<string, MarkdownReference>());
  const operations = useRef(new Map<string, Operation>());
  const width = Math.max(
    Math.min(360, innerWidth - 24),
    Math.min(dockWidth, 1120, innerWidth - 24),
  );
  useEffect(() => {
    const studio = document.querySelector<HTMLElement>(".studio");
    if (!studio) return;
    studio.classList.toggle(
      "document-ai-docked",
      props.visible &&
        !props.detachedOpen &&
        mode === "docked" &&
        !expanded &&
        innerWidth >= 1100,
    );
    studio.style.setProperty("--document-ai-dock-width", width + 24 + "px");
    return () => studio.classList.remove("document-ai-docked");
  }, [props.visible, props.detachedOpen, mode, expanded, width, innerWidth]);
  useEffect(() => () => sideGesture.current?.(), []);
  function rememberWidth(value: number) {
    setDockWidth(value);
    try {
      localStorage.setItem(windowKey + ":width", String(value));
    } catch {
      /* optional */
    }
  }
  const sourceScope = useRef(props.scopeKey),
    ownRevision = useRef(false);
  useLayoutEffect(() => {
    if (sourceScope.current === props.scopeKey) return;
    sourceScope.current = props.scopeKey;
    if (ownRevision.current) {
      ownRevision.current = false;
      return;
    }
    active.current?.controller.abort();
    active.current = null;
    citations.current.clear();
    operations.current.clear();
    channel.current = crypto.randomUUID();
    setSource("");
    setReady(false);
    setError("");
    document.dispatchEvent(new Event("vks-close-selection-menu"));
    setSelectionToExplain("");
  }, [props.scopeKey]);
  useEffect(() => {
    document.dispatchEvent(new Event("vks-close-selection-menu"));
  }, [props.document?.revision_id, props.reading]);
  function post(type: string, payload = {}) {
    view.current?.post(type, payload);
  }
  useEffect(() => {
    if (!source || !inlineHost.current) return;
    const transport = new DocumentAssistantFrame(
      source,
      channel.current,
      inlineHost.current,
      (type, payload) => receiveRef.current(type, payload),
    );
    view.current = transport;
    const leaving = () => transport.disconnect();
    window.addEventListener("pagehide", leaving);
    const back = props.detachedWindow.beforeReturn(() => {
      if (inlineHost.current) transport.moveTo(inlineHost.current);
    });
    if (props.detachedOpen && props.detachedWindow.documentMount)
      transport.moveTo(props.detachedWindow.documentMount, true);
    return () => {
      back();
      window.removeEventListener("pagehide", leaving);
      transport.dispose();
      if (view.current === transport) view.current = null;
    };
  }, [source, props.detachedWindow]);
  useEffect(() => {
    const host = props.detachedOpen
      ? props.detachedWindow.documentMount
      : inlineHost.current;
    if (host) view.current?.moveTo(host, props.detachedOpen);
  }, [source, props.detachedOpen, props.detachedWindow]);
  useEffect(() => {
    if (!props.reading || !hostedShell || typeof DecompressionStream !== "function") return;
    const preload = window.setTimeout(() => void assistantSource().catch(() => {}), 400);
    return () => window.clearTimeout(preload);
  }, [props.reading]);
  useEffect(() => {
    if (!props.visible || source) return;
    const controller = new AbortController();
    setError("");
    void assistantSource(controller.signal)
      .then((html) => {
        if (!controller.signal.aborted && sourceScope.current === props.scopeKey)
          setSource(
            html
              .replaceAll("HOST_CHANNEL", channel.current)
              .replaceAll("HOST_ASSET_ORIGIN", location.origin)
              .replaceAll(
                "HOST_SCOPE",
                encodeURIComponent(props.scopeKey).replaceAll("%", "_"),
              ),
          );
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(String(error));
      });
    return () => controller.abort();
  }, [props.visible, source, props.scopeKey, loadAttempt]);
  useEffect(() => {
    if (ready)
      post("context", {
        document_id: props.document?.revision_id || "",
        document_name: props.document?.name || "",
        documents: props.documents,
        retrieval: "使用平台向量 + 关键词索引",
        connection: "使用项目统一 AI 设置",
        display_mode: speed,
        window_mode: mode,
      });
  }, [
    ready,
    props.visible,
    props.document?.revision_id,
    props.document?.name,
    props.documents,
    speed,
    mode,
  ]);
  useEffect(() => {
    let close = () => {};
    const offer = (text: string, x: number, y: number) => {
      close();
      if (!latest.current.reading) return;
      close = showSelectionMenu(text, x, y, (quote) => {
        setSelectionToExplain(quote);
        latest.current.onAskSelection();
      });
    };
    const capture = (event: MouseEvent) => {
      const selected = window.getSelection();
      const target = event.target as Element;
      if (
        !latest.current.reading ||
        !target.closest?.(
          ".document-reader,.handbook-article,.document-content,.document-view",
        ) ||
        target.closest("input,textarea,[contenteditable]")
      )
        return;
      const text = selected?.toString().trim() || "";
      if (
        !text ||
        !selected?.anchorNode?.parentElement?.closest(
          ".document-reader,.handbook-article,.document-content,.document-view",
        )
      )
        return;
      event.preventDefault();
      offer(text, event.clientX, event.clientY);
    };
    const native = (event: Event) => {
      const data = (event as CustomEvent).detail;
      if (
        typeof data?.text === "string" &&
        Number.isFinite(data.x) &&
        Number.isFinite(data.y)
      )
        offer(data.text, data.x, data.y);
    };
    document.addEventListener("contextmenu", capture);
    window.addEventListener("vks-document-selection-menu", native);
    return () => {
      close();
      document.removeEventListener("contextmenu", capture);
      window.removeEventListener("vks-document-selection-menu", native);
    };
  }, []);
  useEffect(() => {
    if (ready && props.visible && selectionToExplain) {
      post("selection", { text: selectionToExplain, explain: true });
      setSelectionToExplain("");
    }
  }, [ready, props.visible, selectionToExplain]);
  useEffect(() => {
    async function receive(type: string, payload: Record<string, any>) {
      const current = latest.current;
      if (type === "ready") {
        setReady(true);
        return;
      }
      if (type === "detach") return current.onDetach();
      if (type === "close") return current.onClose();
      if (type === "settings") return current.onSettings();
      if (type === "index") return current.onIndex();
      if (type === "layout") {
        setExpanded(!!payload.expanded);
        return;
      }
      if (
        type === "window_mode" &&
        ["floating", "docked"].includes(payload.mode)
      ) {
        setMode(payload.mode);
        setExpanded(false);
        try {
          localStorage.setItem(windowKey + ":mode", payload.mode);
        } catch {
          /* optional */
        }
        return;
      }
      if (type === "window_reset") {
        floating.reset();
        rememberWidth(560);
        setExpanded(false);
        return;
      }
      if (type === "operation") {
        const operation = operations.current.get(payload.id);
        if (operation?.revision_id)
          current.onCitation({
            revision_id: "",
            document_revision_id: operation.revision_id,
            document_name: operation.title,
            name: operation.title,
            content: "",
          });
        if (operation?.job_id) {
          try {
            const project = current.prefix.split("/")[4];
            const job = await api<{
              state: string;
              result?: { download_url?: string; filename?: string };
              error?: { message?: string };
            }>(`/jobs/${operation.job_id}?project_id=${project}`);
            const expected = `/api/v1/projects/${project}/exports/${operation.job_id}/download`;
            if (job.state !== "succeeded")
              throw new Error(
                job.error?.message || "导出仍在进行，请稍后再次查看导出结果。",
              );
            if (job.result?.download_url !== expected)
              throw new Error("导出结果不属于当前项目。");
            const response = await apiResponse(expected),
              url = URL.createObjectURL(await response.blob());
            const a = document.createElement("a");
            a.href = url;
            a.download = job.result.filename || "项目导出";
            a.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          } catch (error) {
            setError(error instanceof Error ? error.message : String(error));
          }
        }
        return;
      }
      if (
        type === "display" &&
        ["normal", "fast", "instant"].includes(payload.mode)
      ) {
        saveDisplaySpeed(payload.mode);
        return;
      }
      if (type === "citation") {
        const ref =
          citations.current.get(payload.revision_id) ||
          (typeof payload.document_revision_id === "string" &&
          typeof payload.revision_id === "string"
            ? {
                revision_id: payload.revision_id,
                document_revision_id: payload.document_revision_id,
                document_name: "",
                name: "",
                content: "",
              }
            : undefined);
        if (ref) current.onCitation(ref);
        return;
      }
      if (type === "cancel") {
        if (active.current && active.current.id === payload.id)
          active.current.controller.abort();
        return;
      }
      if (type !== "ask" || active.current) return;
      if (!current.visible) {
        post("error", {
          id: payload.id,
          message: "请在主页面打开当前文档后继续提问。",
        });
        return;
      }
      const question = payload as Question,
        controller = new AbortController();
      if (question.document_id !== (current.document?.revision_id || "")) {
        post("error", {
          id: question.id,
          message: "文档已切换，请在当前文章重新提问。",
        });
        return;
      }
      active.current = { id: question.id, controller };
      try {
        const request = mutation("POST", {
          ...current.baseline,
          package_id: null,
          document_revision_id: current.document?.revision_id,
          document_scope: question.scope === "current" ? "current" : "all",
          target: {
            route: "/documents",
            frame_path: [],
            selector: "body",
            label: current.document?.name.slice(0, 160) || "项目知识文档",
            state: "",
            open_steps: [],
          },
          context: {
            scope: "module",
            page_title: current.document?.name.slice(0, 200) || "项目知识文档",
            section_title: "",
            nearby_labels: [],
          },
          question: question.question,
          history: question.history,
          selected_text: question.selection,
          retrieval_mode:
            question.mode === "local"
              ? "local"
              : question.mode === "keyword-ai"
                ? "keyword"
                : "model",
        });
        const response = await apiResponse(`${current.prefix}/conversation`, {
          ...request,
          headers: {
            ...request.headers,
            Accept: "text/event-stream",
            "Idempotency-Key": question.id,
          },
          signal: controller.signal,
        });
        const answer = await conversationAnswer<Answer>(
          response,
          (text) => post("delta", { id: question.id, text }),
          controller.signal,
        );
        for (const ref of answer.sections)
          citations.current.set(ref.revision_id, ref);
        for (const operation of answer.operations || []) {
          const id = operation.job_id || operation.revision_id;
          if (id) operations.current.set(id, operation);
        }
        if (
          answer.operations?.some((operation) => operation.kind === "document")
        )
          ownRevision.current = true;
        if (answer.operations?.length)
          await current.onUpdated(answer.operations);
        if (!controller.signal.aborted)
          post("complete", { id: question.id, answer });
      } catch (error) {
        if (!controller.signal.aborted)
          post("error", {
            id: question.id,
            message: error instanceof Error ? error.message : String(error),
          });
      } finally {
        if (active.current?.id === question.id) active.current = null;
      }
    }
    receiveRef.current = receive;
    return () => {
      receiveRef.current = () => {};
      active.current?.controller.abort();
    };
  }, []);
  return (
    <>
      {(props.visible || source || error) && (
        <aside
          ref={floating.ref}
          className={
            "document-ai-window " + mode + (expanded ? " expanded" : "")
          }
          data-window-mode={mode}
          style={
            expanded
              ? {
                  left: 12,
                  right: 12,
                  top: 72,
                  bottom: 12,
                  width: "auto",
                  height: "auto",
                }
              : mode === "floating"
                ? floating.style
                : { width }
          }
          aria-label="文档 AI 助手"
          aria-busy={!ready && !error}
          hidden={!props.visible || props.detachedOpen}
        >
          {error && (
            <div className="document-ai-load-state" role="alert">
              <p>{error}</p>
              <button type="button" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>
                重试
              </button>
              <button type="button" onClick={props.onClose}>收起助手</button>
            </div>
          )}
          {!ready && !error && (
            <div className="document-ai-load-state">
              <p role="status">正在打开文档助手…</p>
              <button type="button" onClick={props.onClose}>收起助手</button>
            </div>
          )}
          <div ref={inlineHost} style={{ width: "100%", height: "100%" }} />
          {mode === "floating" && !expanded && (
            <div
              className="document-ai-drag"
              role="button"
              tabIndex={0}
              aria-label="移动文档助手"
              onPointerDown={(event) => floating.begin(event, "move")}
              onKeyDown={(event) => {
                const deltas: Record<string, [number, number]> = {
                  ArrowLeft: [-20, 0],
                  ArrowRight: [20, 0],
                  ArrowUp: [0, -20],
                  ArrowDown: [0, 20],
                };
                if (deltas[event.key]) {
                  event.preventDefault();
                  floating.keyboardMove(...deltas[event.key]);
                }
              }}
            />
          )}
          {!expanded &&
            (["n", "s", "e", "w", "ne", "nw", "se", "sw"] as ResizeDirection[])
              .filter((direction) => mode === "floating" || direction === "w")
              .map((direction) => (
                <div
                  key={direction}
                  className={"document-ai-resize resize-" + direction}
                  role="separator"
                  tabIndex={0}
                  aria-label={"调整文档助手大小 " + direction}
                  onPointerDown={(event) => {
                    if (mode === "floating") {
                      floating.begin(event, direction);
                      return;
                    }
                    if (!floating.ref.current) return;
                    sideGesture.current?.();
                    sideGesture.current = startFloatingGesture(event, {
                      node: floating.ref.current,
                      handle: event.currentTarget,
                      direction: "w",
                      onCommit: (box) => {
                        const node = floating.ref.current;
                        if (node)
                          Object.assign(node.style, {
                            left: "",
                            top: "",
                            right: "",
                            bottom: "",
                            height: "",
                          });
                        rememberWidth(box.width);
                      },
                    });
                  }}
                  onKeyDown={(event) => {
                    const delta: Record<string, [number, number]> = {
                      ArrowLeft: [24, 0],
                      ArrowRight: [-24, 0],
                      ArrowUp: [0, -24],
                      ArrowDown: [0, 24],
                    };
                    if (!delta[event.key]) return;
                    event.preventDefault();
                    if (mode === "floating")
                      floating.keyboardResize(...delta[event.key]);
                    else rememberWidth(width + delta[event.key][0]);
                  }}
                />
              ))}
        </aside>
      )}
    </>
  );
}
