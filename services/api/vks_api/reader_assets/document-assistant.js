// Installed inside the original handbook's application closure, before its
// history wrapper. UI/history/copy/followups remain the original implementation.
const vksDocumentHost = (() => {
  const channel = "HOST_CHANNEL";
  let current = {},
    active = null,
    connected = true;
  const owner = (() => {
    try {
      return parent.VKSAssistantOwner || parent;
    } catch {
      return parent;
    }
  })();
  const bridge = () => {
    try {
      return owner.VKSAssistantBridges?.get(channel);
    } catch {
      return null;
    }
  };
  const send = (type, payload = {}) => {
    const host = bridge();
    if (host) host.message(window, type, payload);
    else parent.postMessage({ vks_document_ai: channel, type, payload }, "*");
  };
  const cacheView = () =>
    bridge()?.snapshot(window, window.VKSAssistantView?.snapshot());
  Fc = async () => {
    Me ||= [];
    return true;
  };
  cT = () => {}; // Never restore the standalone key or another provider's index.
  uT = () => current.document_id || "";
  $A = (source) =>
    send("citation", {
      revision_id: source.anchor,
      document_revision_id: source.docId,
    });
  const previousClose = Yh;
  Yh = () => {
    send("close");
    previousClose();
  };
  const previousTab = qc;
  qc = (tab) => previousTab(tab);
  // Keep source IDs in the stored answer; present one set of numeric citations.
  function linkReferences(root, sources) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (
        node.parentElement?.closest(
          "pre,code,a,button,svg,math,.diagram-source",
        )
      )
        continue;
      const parts = document.createDocumentFragment();
      let end = 0;
      for (const match of node.data.matchAll(/\[(\d{1,2})\]/g)) {
        const source = sources.find(
          (item) => item.sourceId === "S" + Number(match[1]),
        );
        if (!source) continue;
        parts.append(
          document.createTextNode(node.data.slice(end, match.index)),
        );
        const cite = Re("button", "fh-ai-cite fh-ai-inline-cite", match[0]);
        cite.type = "button";
        cite.title = source.docTitle + " / " + source.title;
        cite.dataset.sourceAnchor = source.anchor;
        cite.onclick = () => $A(source);
        parts.append(cite);
        end = match.index + match[0].length;
      }
      if (end) {
        parts.append(document.createTextNode(node.data.slice(end)));
        node.replaceWith(parts);
      }
    }
    return root;
  }
  zA = (bubble, sources, filter) => {
    const rows = filter
      ? sources.filter((source) => filter.has(source.sourceId))
      : sources;
    const details = Re("details", "fh-ai-sources");
    const summary = Re("summary", "", `查看 ${rows.length} 处原文依据`);
    summary.addEventListener("click", () => {
      const view = fhReadingUpgrade.captureView();
      view.following = false;
      fhReadingUpgrade.restoreView(view);
    });
    details.append(summary);
    for (const source of rows) {
      const entry = Re("article", "fh-ai-source-entry");
      const open = Re("button", "fh-ai-source");
      open.type = "button";
      open.dataset.sourceAnchor = source.anchor;
      const number = source.sourceId.replace(/^S/, "");
      open.append(
        Re("strong", "", `[${number}] ${source.title}`),
        Re("small", "", source.docTitle),
      );
      open.onclick = () => $A(source);
      entry.append(open);
      details.append(entry);
    }
    bubble.append(details);
    return details;
  };
  const previousAnswer = _T;
  _T = (...args) => {
    const result = previousAnswer(...args);
    P("fh-ai-messages")
      .lastElementChild?.querySelectorAll(".fh-ai-answer-block > .fh-ai-cite")
      .forEach((cite) => cite.remove());
    return result;
  };
  SA = (answer) =>
    answer.blocks.map((block) => block.text).join("\n\n") +
    (answer.missingInformation.length
      ? "\n\n待补充：\n" + answer.missingInformation.join("\n")
      : "");
  function paintContext() {
    P("fh-ai-panel").hidden = false;
    document.body.classList.add("fh-ai-open");
    if (!connected) {
      P("fh-ai-send").disabled = true;
      P("fh-ai-question").disabled = true;
      La("主页面已断开，已显示内容保留。");
    }
    const hostStatus = P("vks-host-index-status");
    if (hostStatus)
      hostStatus.textContent = current.document_name
        ? "正在阅读 · " + current.document_name
        : "项目知识问答";
    P("fh-ai-index-status").textContent =
      current.retrieval || "使用平台知识索引";
    P("fh-ai-doc-count").textContent = current.documents || 0;
    P("fh-ai-chunk-count").textContent = current.sections || 0;
    P("fh-ai-index-detail").textContent =
      "与原型助手共用平台向量与关键词检索；此窗口不单独建库。";
    P("fh-ai-build-detail").textContent = current.document_name
      ? "正在阅读：" + current.document_name
      : "全部知识文档";
    P("fh-ai-key-state").textContent = "与原型助手共用连接配置";
    P("fh-ai-usage").textContent = current.connection || "使用统一 AI 设置";
    const notice = P("fh-ai-welcome").querySelector("p");
    if (notice)
      notice.textContent =
        "查流程、理解规则、定位问题。回答附上文档原文，关键结论可以追溯。";
    const hint = P("fh-ai-welcome").querySelector(":scope > .fh-ai-small");
    if (hint)
      hint.textContent =
        "使用统一 AI 连接及平台知识索引；也可以切换“仅查找原文”。";
    const popout = document.querySelector(".fh-ai-host-popout");
    if (popout) popout.hidden = !!current.detached;
    P("fh-ai-layout-toggle").hidden =
      !!current.detached || parent.innerWidth <= 700;
    const speed = P("fh-ai-reading-speed");
    for (const button of document.querySelectorAll("[data-vks-window-mode]")) {
      const selected =
        button.dataset.vksWindowMode === (current.window_mode || "docked");
      button.setAttribute("aria-pressed", String(selected));
      button.classList.toggle("is-active", selected);
    }
    if (speed && current.display_mode && speed.value !== current.display_mode) {
      speed.value = current.display_mode;
      speed.dispatchEvent(new Event("change"));
    }
  }
  MA = async (event) => {
    event?.preventDefault();
    if (At || !connected) return;
    const question = P("fh-ai-question").value.trim();
    if (!question || question.length > 2000) return;
    const scope = P("fh-ai-scope").value;
    if (scope === "current" && !uT()) return re("请先打开一篇文档。");
    const id = crypto.randomUUID(),
      selection = $a?.text || "",
      documentId = uT(),
      mode = P("fh-ai-mode").value;
    const history = za.slice(-3).flatMap((turn) => [
      { role: "user", content: turn.question.slice(0, 2000) },
      { role: "assistant", content: turn.answer.slice(0, 8000) },
    ]);
    beginQuestion({
      id,
      question,
      history,
      selection,
      documentId,
      scope,
      mode,
    });
  };
  function beginQuestion(
    {
      id,
      question,
      history = [],
      selection,
      documentId,
      scope,
      mode,
      partial: initial = "",
    },
    restoring = false,
  ) {
    FA = question;
    xe = new AbortController();
    const signal = xe.signal;
    kt(true, "answer");
    if (!restoring) Lc("user", question);
    P("fh-ai-question").value = "";
    // Preview never enters saved history; only validated completion does.
    const preview = Re("article", "fh-ai-message assistant");
    const bubble = Re("div", "fh-ai-bubble fh-ai-markdown");
    preview.append(Re("div", "fh-ai-message-label", "AI · 正在生成"), bubble);
    P("fh-ai-messages").append(preview);
    let partial = initial;
    let paint = 0,
      displayed = 0;
    function reveal() {
      paint = 0;
      const chars = Array.from(partial),
        mode = P("fh-ai-reading-speed")?.value || "normal";
      displayed =
        mode === "instant"
          ? chars.length
          : Math.min(chars.length, displayed + (mode === "fast" ? 24 : 8));
      bubble.textContent = chars.slice(0, displayed).join("");
      fhReadingUpgrade.messageAdded(preview, "assistant");
      if (displayed < chars.length) paint = requestAnimationFrame(reveal);
    }
    active = {
      id,
      snapshot: () => ({
        id,
        question,
        history,
        selection,
        documentId,
        scope,
        mode,
        partial,
      }),
      resume(text) {
        partial = text;
        displayed = 0;
        if (!paint) paint = requestAnimationFrame(reveal);
      },
      delta(text) {
        partial += text;
        if (!paint) paint = requestAnimationFrame(reveal);
      },
      async finish(result, error) {
        cancelAnimationFrame(paint);
        preview.remove();
        active = null;
        if (error) {
          Lc("error", (partial ? partial + "\n\n未完成：" : "") + error);
          P("fh-ai-question").value = question;
        } else {
          const sources = (result.sections || []).map((ref, i) => ({
            id: ref.revision_id,
            sourceId: "S" + (i + 1),
            anchor: ref.revision_id,
            docId: ref.document_revision_id,
            docTitle: ref.document_name,
            title: ref.name,
            context: ref.name,
            text: ref.content,
            path: "",
            part: 1,
            parts: 1,
          }));
          const answer = {
            status: sources.length ? "answered" : "insufficient",
            blocks: [
              {
                kind: sources.length ? "fact" : "suggestion",
                text: result.explanation,
                sourceIds: sources.map((ref) => ref.sourceId),
              },
            ],
            missingInformation: result.unknowns || [],
            followUpQuestions: [],
            suggestedQuestions: result.suggested_questions || [],
          };
          if (result.operations?.length) Lc("assistant", result.explanation);
          else
            await _T(
              answer,
              sources,
              mode === "hybrid" ? "hybrid" : "keyword",
              0,
              scope === "current" ? documentId : "",
            );
          const last = P("fh-ai-messages").querySelector(
            ".fh-ai-message:last-child .fh-ai-bubble",
          );
          for (const operation of result.operations || []) {
            const action = Re(
              "button",
              "fh-ai-primary",
              operation.job_id ? "查看导出结果" : operation.title,
            );
            action.onclick = () =>
              send("operation", {
                id: operation.job_id || operation.revision_id,
              });
            last?.append(action);
          }
        }
        kt(false);
        La("");
        paintContext();
        send("settled", { id });
        cacheView();
      },
    };
    signal.addEventListener(
      "abort",
      () => {
        if (active?.id !== id) return;
        send("cancel", { id });
        active.finish(null, "已停止，本轮内容未保存为完整回答。");
      },
      { once: true },
    );
    cacheView();
    La("正在检索文档并生成回答…");
    if (initial) reveal();
    if (!restoring)
      send("ask", {
        id,
        question,
        history,
        selection,
        scope,
        document_id: documentId,
        mode,
      });
  }
  function disconnect() {
    if (!connected) return;
    connected = false;
    if (active)
      void active.finish(null, "主页面已关闭或刷新，请重新打开主页面继续。");
    fhChatUpgrade.seal();
    paintContext();
  }
  window.addEventListener("message", (event) => {
    if (
      (event.source !== parent && event.source !== owner) ||
      event.data?.vks_document_ai !== channel
    )
      return;
    const { type, payload = {} } = event.data;
    if (type === "hydrate") {
      void (async () => {
        await fhChatUpgrade.restore(payload.history);
        P("fh-ai-scope").value = payload.scope || "all";
        P("fh-ai-mode").value = payload.mode || "hybrid";
        qc(payload.tab || "chat");
        fhLayoutUpgrade.setToolsOpen(!!payload.tools);
        $a = payload.selection || null;
        if (typeof payload.draft === "string")
          P("fh-ai-question").value = payload.draft;
        if (payload.pending) beginQuestion(payload.pending, true);
        P("fh-ai-messages").scrollTop = payload.scroll || 0;
        send("hydrated");
        cacheView();
      })();
      return;
    }
    if (type === "resume") {
      if (payload.id === active?.id) active.resume(String(payload.text || ""));
      return;
    }
    if (type === "disconnected") {
      disconnect();
      return;
    }
    if (type === "context") {
      current = payload;
      paintContext();
    }
    if (type === "selection") {
      $a = {
        text: String(payload.text || "").slice(0, 4000),
        docTitle: current.document_name || "",
      };
      P("fh-ai-selection-text").textContent = $a.text;
      P("fh-ai-selection").hidden = !$a.text;
      if (payload.explain) {
        P("fh-ai-question").value =
          "请解释这段内容的业务含义和需要注意的边界。";
        P("fh-ai-question").dispatchEvent(
          new Event("input", { bubbles: true }),
        );
        P("fh-ai-question").focus();
      }
      cacheView();
    }
    if (payload.id !== active?.id) return;
    if (type === "delta") active.delta(String(payload.text || ""));
    if (type === "complete") active.finish(payload.answer);
    if (type === "error")
      active.finish(null, String(payload.message || "回答未完成"));
  });
  window.addEventListener("DOMContentLoaded", async () => {
    P("fh-ai-form").classList.add("vks-ai-composer");
    P("fh-ai-form")
      .querySelector(".fh-ai-compose-actions")
      .classList.add("vks-ai-composer-actions");
    P("fh-ai-form")
      .querySelector(".fh-ai-spacer")
      .classList.add("vks-ai-composer-spacer");
    P("fh-ai-send").classList.add("vks-ai-composer-send");
    P("fh-ai-stop").classList.add("vks-ai-composer-send");
    P("fh-ai-question").title = "Enter 发送 · Shift+Enter 换行";
    P("fh-ai-send").title = "发送（Enter）";
    window.VKSAssistantView = {
      disconnect,
      snapshot: () => ({
        history: fhChatUpgrade.snapshot(),
        pending: active?.snapshot(),
        scope: P("fh-ai-scope").value,
        mode: P("fh-ai-mode").value,
        tab:
          document.querySelector('.fh-ai-tabs [aria-selected="true"]')?.dataset
            .aiTab || (P("fh-ai-tab-settings").hidden ? "chat" : "settings"),
        tools: P("fh-ai-tools-toggle").getAttribute("aria-expanded") === "true",
        selection: $a,
        scroll: P("fh-ai-messages").scrollTop,
      }),
    };
    const popout = Re(
      "button",
      "fh-ai-host-popout fh-ai-icon assistant-detach-open",
      "↗",
    );
    popout.type = "button";
    popout.setAttribute("aria-label", "弹出独立窗口");
    popout.title = "弹出独立窗口";
    popout.onclick = () => send("detach");
    P("fh-ai-panel")
      .querySelector(".fh-ai-head-actions")
      .insertBefore(popout, P("fh-ai-close"));
    document.addEventListener("input", (event) => {
      if (event.target === P("fh-ai-question"))
        bridge()?.snapshot(window, null, P("fh-ai-question").value);
    });
    document.addEventListener("change", (event) => {
      if (event.target === P("fh-ai-history-select") && !$a)
        send("selection-cleared");
      cacheView();
    });
    document.addEventListener(
      "click",
      (event) => {
        const button = event.target?.closest?.("#fh-ai-new");
        if (!button || button.disabled) return;
        // The history controller handles this button in target capture and stops
        // propagation. Update the host cache after its new-session state settles.
        queueMicrotask(() => {
          $a = null;
          P("fh-ai-selection").hidden = true;
          P("fh-ai-selection-text").textContent = "";
          send("selection-cleared");
          cacheView();
        });
      },
      true,
    );
    P("fh-ai-selection-clear").addEventListener("click", () => {
      send("selection-cleared");
      cacheView();
    });
    document.addEventListener("click", cacheView);
    // The embedded host forbids native form navigation. Route button clicks
    // directly through the same current ask wrapper as the Enter shortcut.
    P("fh-ai-send").addEventListener("click", (event) => {
      event.preventDefault();
      void MA(event);
    });
    for (const tab of ["knowledge", "settings"]) {
      const section = P("fh-ai-tab-" + tab);
      for (const child of [...section.children])
        if (
          !child.classList.contains("fh-ai-reading-settings") &&
          !child.classList.contains("fh-ai-history-card")
        )
          child.hidden = true;
      if (tab === "settings") {
        const action = Re(
          "button",
          "fh-ai-host-settings-link",
          "模型连接设置 ↗",
        );
        action.type = "button";
        action.setAttribute("aria-label", "模型连接设置");
        action.onclick = () => send("settings");
        section.classList.add("vks-assistant-settings");
        section.setAttribute("aria-label", "助手设置");
        const tabButton = document.querySelector('[data-ai-tab="settings"]');
        tabButton.textContent = "助手设置";
        const intro = Re("div", "fh-ai-settings-intro");
        intro.append(Re("span", "", "偏好与记录"), action);
        const layout = Re("div", "fh-ai-window-options fh-ai-preference-group");
        layout.append(Re("h4", "", "窗口布局"));
        const layoutRow = Re("div", "fh-ai-setting-row");
        layoutRow.append(Re("span", "fh-ai-setting-label", "展开方式"));
        const choices = Re("div", "fh-ai-window-choices");
        choices.setAttribute("role", "group");
        choices.setAttribute("aria-label", "文档助手展开方式");
        function collapseReading() {
          const button = P("fh-ai-layout-toggle");
          if (button?.getAttribute("aria-pressed") === "true") button.click();
        }
        for (const [mode, title] of [
          ["floating", "浮动小窗"],
          ["docked", "侧边展开"],
        ]) {
          const button = Re("button", "", title);
          button.type = "button";
          button.dataset.vksWindowMode = mode;
          button.onclick = () => {
            collapseReading();
            send("window_mode", { mode });
          };
          choices.append(button);
        }
        const reset = Re("button", "fh-ai-window-reset", "恢复默认");
        reset.type = "button";
        reset.title = "恢复默认位置与大小";
        reset.setAttribute("aria-label", "恢复默认位置与大小");
        reset.onclick = () => {
          collapseReading();
          send("window_reset");
        };
        layoutRow.append(choices, reset);
        layout.append(layoutRow);
        const reading = section.querySelector(".fh-ai-reading-settings");
        reading?.classList.add("fh-ai-preference-group");
        const history = section.querySelector(".fh-ai-history-card");
        section.append(intro, layout);
        if (reading) section.append(reading);
        if (history) section.append(history);
      }
    }
    document.querySelector('[data-ai-tab="knowledge"]').hidden = true;
    P("fh-ai-reading-speed").addEventListener("change", () =>
      send("display", { mode: P("fh-ai-reading-speed").value }),
    );
    const readingNote = document.querySelector(
      ".fh-ai-reading-settings .fh-ai-small",
    );
    if (readingNote)
      readingNote.textContent =
        "边生成边显示，向上翻阅时暂停跟随。表格和图表保持完整。";
    P("fh-ai-panel").hidden = false;
    const hostStatus = Re("div", "vks-host-index-status");
    hostStatus.id = "vks-host-index-status";
    P("fh-ai-panel").querySelector(".fh-ai-tabs").after(hostStatus);
    const expand = P("fh-ai-layout-toggle");
    if (expand) {
      const fit = () => {
        expand.hidden = !!current.detached || parent.innerWidth <= 700;
      };
      fit();
      parent.addEventListener("resize", fit);
      window.addEventListener(
        "pagehide",
        () => parent.removeEventListener("resize", fit),
        { once: true },
      );
    }
    if (expand)
      new MutationObserver(() =>
        send("layout", {
          expanded: expand.getAttribute("aria-pressed") === "true",
        }),
      ).observe(expand, {
        attributes: true,
        attributeFilter: ["aria-pressed"],
      });
    document.body.classList.add("fh-ai-open");
    P("fh-ai-panel").querySelector("h2").firstChild.textContent = "知识助手 ";
    document.querySelectorAll("[data-ai-example]").forEach((button, i) => {
      const questions = [
        "这份资料主要说明什么？",
        "关键流程有哪些步骤？",
        "有哪些常见问题和注意事项？",
      ];
      button.dataset.aiExample = questions[i % 3];
      button.textContent = questions[i % 3] + " ↗";
    });
    // History-directory persistence remains owned by the original UI. Document
    // and vector mutation controls stay hidden in the host-owned index panel.
    paintContext();
    await fhChatUpgrade.ready;
    send("ready");
    cacheView();
  });
  return {
    send,
    installPresentation(presentation) {
      const markdown = presentation.renderMarkdown;
      presentation.renderMarkdown = (text, ids, sources = []) =>
        linkReferences(markdown(text, ids, sources), sources);
    },
  };
})();
