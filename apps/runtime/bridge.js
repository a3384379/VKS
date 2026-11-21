/* A static, credential-free runtime. Source HTML never runs in Studio's origin. */
(() => {
  "use strict";
  const query = new URLSearchParams(location.search);
  const instance = query.get("instance");
  let config;
  try {
    config = JSON.parse(
      document.querySelector("#vks-runtime-config").textContent,
    );
  } catch {
    return;
  }
  const host = config.host;
  const documentMode = config.mode === "document";
  if (!instance || !host || !/^https?:\/\//.test(host)) return;
  const frame = document.querySelector("#material");
  let port,
    picking = false,
    sequence = 0,
    epoch = 1,
    loaded = false,
    navigation = 0,
    documentURL = "";
  const registered = new WeakMap();
  const windows = new Map();
  const ambiguousFrames = new Set();
  const histories = new WeakMap();
  const sourcePaths = new WeakMap();
  let documentNavigating = false,
    lastDocument = null,
    inventoryGeneration = 0,
    inventorySpec = null,
    materialWrapper = null;
  const send = (type, payload = {}, request_id = "") =>
    port?.postMessage({
      protocol_version: 1,
      runtime_instance_id: instance,
      epoch,
      sequence: ++sequence,
      request_id,
      type,
      payload,
    });
  function visible(el) {
    if (!el?.isConnected || !el.getClientRects().length ||
        getComputedStyle(el).visibility !== "visible") return false;
    if (typeof el.checkVisibility === "function")
      return el.checkVisibility({ opacityProperty: true, checkOpacity: true });
    for (let node = el; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (Number(style.opacity) === 0 || style.contentVisibility === "hidden")
        return false;
    }
    return true;
  }
  const labelText = (child) =>
    child.nodeType === 3
      ? child.nodeValue
      : child.nodeType === 1 &&
          child.matches("input,select,textarea,script,style,[contenteditable]")
        ? ""
        : Array.from(child.childNodes).map(labelText).join("");
  const fieldLabel = (el) => {
    const node = el.labels?.[0];
    if (!node) return "";
    return labelText(node).trim();
  };
  const label = (el) =>
    (
      el.getAttribute("aria-label") ||
      el.getAttribute("title") ||
      (el.matches("input,select,textarea") ? fieldLabel(el) : "") ||
      el.getAttribute("placeholder") ||
      (!el.matches("input,select,textarea") ? labelText(el).trim() : "") ||
      (el.matches(
        'input[type="submit"],input[type="button"],input[type="reset"]',
      )
        ? el.value
        : "") ||
      (el.matches("input,select,textarea") ? el.name : "") ||
      (el.matches(".el-switch,.el-radio,.el-checkbox")
        ? el.parentElement?.textContent?.trim()
        : "") ||
      el.tagName
    )
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 160);
  function selector(el) {
    const doc = el.ownerDocument;
    if (el.id && doc.querySelectorAll("#" + CSS.escape(el.id)).length === 1)
      return "#" + CSS.escape(el.id);
    const parts = [];
    for (
      let node = el;
      node && node.nodeType === 1 && node !== doc.documentElement;
      node = node.parentElement
    ) {
      if (
        node.id &&
        doc.querySelectorAll("#" + CSS.escape(node.id)).length === 1
      ) {
        parts.unshift("#" + CSS.escape(node.id));
        break;
      }
      parts.unshift(
        node.localName +
          ":nth-of-type(" +
          (Array.from(node.parentElement?.children || [])
            .filter((n) => n.localName === node.localName)
            .indexOf(node) +
            1) +
          ")",
      );
    }
    return parts.join(" > ");
  }
  const dialogs =
    'dialog[open],[role="dialog"],.el-dialog,.el-drawer,.el-message-box,.ant-modal,.modal,.drawer,.offline-modal,.demo-modal,.prototype-modal,.vks-modal';
  const observableState = (doc) => {
    const candidates = Array.from(doc.querySelectorAll(dialogs));
    return candidates.filter(visible).map(selector).join(" | ");
  };
  const stateOf = (doc) => observableState(doc).slice(0, 500);
  function target(el, win, path, route, state = stateOf(win.document)) {
    return {
      route,
      frame_path: path,
      selector: selector(el),
      label: label(el),
      state,
      open_steps: histories.get(win) || [],
    };
  }
  const highlights = new WeakMap();
  function glow(el) {
    const highlight = highlights.get(el) || {
      outline: el.style.outline,
      outlineOffset: el.style.outlineOffset,
    };
    clearTimeout(highlight.timer);
    el.style.outline = "3px solid #13a58d";
    el.style.outlineOffset = "3px";
    el.scrollIntoView({ block: "nearest", inline: "nearest" });
    highlight.timer = setTimeout(() => {
      el.style.outline = highlight.outline;
      el.style.outlineOffset = highlight.outlineOffset;
      highlights.delete(el);
    }, 2200);
    highlights.set(el, highlight);
  }
  const actionable =
    'button,a,input,select,textarea,[role="button"],[role="menuitem"],[role="tab"],[onclick],[tabindex]:not([tabindex="-1"]),label,.el-switch,.el-checkbox,.el-radio,.el-menu-item,.el-submenu__title';
  let pointed = null,
    currentModule = null;
  const editable = (el) =>
    el?.matches?.('input,textarea,select,[contenteditable="true"]') ||
    el?.isContentEditable;
  function context(el, doc) {
    const container =
      el.closest('[role="dialog"],.el-dialog,.el-drawer,.modal,.drawer') ||
      doc.body;
    const heading = Array.from(
      container.querySelectorAll(
        '.el-dialog__title,[role="heading"],h1,h2,h3,.page-title,.title',
      ),
    ).find(visible);
    return {
      page_title: (doc.title || "").trim().slice(0, 200),
      section_title: (heading?.textContent || "")
        .trim()
        .replace(/\s+/g, " ")
        .slice(0, 200),
      nearby_labels: [
        ...new Set(
          Array.from(container.querySelectorAll('button,[role="button"]'))
            .filter(visible)
            .map(label),
        ),
      ]
        .slice(0, 8)
        .map((s) => s.slice(0, 80)),
    };
  }
  function selected(el, win, path, route) {
    const current = win.__prototypePackPage?.path
      ? "/" + win.__prototypePackPage.path.replace(/^\//, "")
      : route;
    return {
      target: target(el, win, path, current),
      context: context(el, win.document),
    };
  }
  function moduleTarget(win, path, route) {
    const doc = win.document;
    const name = (
      doc.title ||
      doc.querySelector("h1,h2,.page-title")?.textContent ||
      route
        .split("/")
        .pop()
        ?.replace(/\.html?$/i, "") ||
      "当前模块"
    )
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 160);
    return {
      route,
      frame_path: path,
      selector: "body",
      label: name,
      state: "",
      open_steps: [],
    };
  }
  function moduleContext(win, path, route) {
    const doc = win.document;
    return {
      target: moduleTarget(win, path, route),
      context: { ...context(doc.body, doc), scope: "module" },
    };
  }
  function coordinates(win, x, y) {
    for (let depth = 0; win !== window && depth < 10; depth++) {
      const el = win.frameElement;
      if (!el) break;
      const rect = el.getBoundingClientRect();
      x =
        rect.left +
        el.clientLeft +
        x * (rect.width / (el.offsetWidth || rect.width));
      y =
        rect.top +
        el.clientTop +
        y * (rect.height / (el.offsetHeight || rect.height));
      win = win.parent;
    }
    return { x: x / innerWidth, y: y / innerHeight };
  }
  function install(win, path, route) {
    let doc;
    try {
      doc = win.document;
      if (
        !doc?.body ||
        (!doc.body.childElementCount && !doc.body.textContent?.trim()) ||
        doc.readyState === "loading"
      )
        return;
    } catch {
      return;
    }
    const own = win.__prototypePackPage;
    route = own?.path ? "/" + own.path.replace(/^\//, "") : route;
    const windowKey = JSON.stringify(path);
    if (windows.has(windowKey) && windows.get(windowKey).win !== win)
      ambiguousFrames.add(windowKey);
    windows.set(windowKey, { win, doc, route, path });
    if (documentMode) {
      installDocument(win, route);
      for (const child of doc.querySelectorAll("iframe")) {
        if (child.contentWindow)
          install(
            child.contentWindow,
            [...path, child.id || child.title || "frame"],
            route,
          );
      }
      return;
    }
    if (registered.get(doc) !== doc.documentElement) {
      registered.set(doc, doc.documentElement);
      doc.addEventListener("load", scan, true);
      doc.addEventListener(
        "pointerdown",
        (event) => {
          if (event.button !== 2) send("context.dismiss");
        },
        true,
      );
      doc.addEventListener(
        "focusin",
        (event) => {
          const el = event.target?.closest?.(actionable);
          if (el && visible(el)) pointed = { el, win, path, route };
        },
        true,
      );
      doc.addEventListener(
        "pointerover",
        (event) => {
          const el = event.target?.closest?.(actionable);
          pointed = el && visible(el) ? { el, win, path, route } : null;
        },
        true,
      );
      doc.addEventListener(
        "contextmenu",
        (event) => {
          if (event.shiftKey || picking) return;
          const el = event.target?.closest?.(actionable);
          if (el && !visible(el)) return;
          event.preventDefault();
          event.stopImmediatePropagation();
          pointed = el ? { el, win, path, route } : null;
          send("context.menu", {
            ...(el
              ? selected(el, win, path, route)
              : currentModule || moduleContext(win, path, route)),
            ...coordinates(win, event.clientX, event.clientY),
          });
        },
        true,
      );
      doc.addEventListener("scroll", () => send("context.dismiss"), true);
      doc.addEventListener(
        "click",
        (event) => {
          const el =
            event.target?.closest?.(
              'button,a,input,select,textarea,[role="button"],[role="menuitem"],[role="tab"],[onclick],[tabindex]:not([tabindex="-1"]),label,.el-switch,.el-checkbox,.el-radio,.el-menu-item,.el-submenu__title',
            ) || (picking ? event.target : null);
          const currentRoute = win.__prototypePackPage?.path
            ? "/" + win.__prototypePackPage.path.replace(/^\//, "")
            : route;
          const portal =
            !win.__prototypePackPage?.path &&
            (win.__prototypePack || doc.getElementById("prototypePackPayload"));
          if (
            !picking &&
            portal &&
            (!el ||
              el.matches(
                '[role="menuitem"],[role="tab"],[data-page],[data-tab],a[data-vks-page]',
              ))
          ) {
            pointed = null;
            // Same-route navigation can be a no-op in the original portal.
            // Resolve after its handler without reloading or selecting the menu.
            setTimeout(() => scan(true), 0);
            return;
          }
          if (!el) {
            pointed = null;
            // Blank list/page clicks return to the visible business module.
            // The surrounding portal/menu must not replace its active iframe.
            const module =
              path.length === 0 && currentModule
                ? currentModule
                : moduleContext(win, path, currentRoute);
            currentModule = module;
            send("control.activated", module);
            return;
          }
          if (!visible(el)) return;
          const selected = target(el, win, path, currentRoute);
          const selectedControl = {
            target: selected,
            context: context(el, doc),
          };
          pointed = { el, win, path, route };
          if (picking) {
            event.preventDefault();
            event.stopImmediatePropagation();
            picking = false;
            glow(el);
            send("target.selected", selected);
            return;
          }
          send("action.activated", selected);
          if (!editable(el)) send("control.activated", selectedControl);
          const old = stateOf(doc);
          setTimeout(() => {
            if (win.document !== doc) return;
            const now = stateOf(doc);
            if (!now) histories.set(win, []);
            else if (now !== old)
              histories.set(
                win,
                [...(histories.get(win) || []), selected.selector].slice(-8),
              );
          }, 180);
        },
        true,
      );
      doc.addEventListener(
        "keydown",
        (event) => {
          if (event.key === "Escape") send("context.dismiss");
          if (event.key === "Escape" && picking) {
            picking = false;
            event.preventDefault();
            event.stopImmediatePropagation();
            send("picker.cancelled");
          }
        },
        true,
      );
      if (path.includes("actualPage") || path.length === 0) {
        const module =
          (!win.__prototypePack &&
            !doc.getElementById("prototypePackPayload")) ||
          own?.path
            ? moduleContext(win, path, route)
            : null;
        if (module) currentModule = module;
        send("context.changed", { route, frame_path: path, module });
      }
    }
    for (const child of doc.querySelectorAll("iframe")) {
      if (child.contentWindow)
        install(
          child.contentWindow,
          [...path, child.id || child.title || "frame"],
          route,
        );
    }
  }
  function handbookSources(doc) {
    if (sourcePaths.has(doc)) return sourcePaths.get(doc);
    const source = doc.getElementById("source-data");
    if (!source) return null;
    let paths = null;
    try {
      const entries = JSON.parse(source.textContent);
      paths = Object.fromEntries(
        Object.entries(entries)
          .filter(([id, value]) => id && typeof value?.path === "string")
          .map(([id, value]) => [id, value.path.replace(/^handbook\//, "")]),
      );
    } catch {}
    sourcePaths.set(doc, paths);
    return paths;
  }
  const decodeAnchor = (hash) => {
    try {
      return decodeURIComponent(hash.replace(/^#/, ""));
    } catch {
      return hash.replace(/^#/, "");
    }
  };
  const normalizedPath = (path) => (path || "").replace(/^\//, "");
  function documentPosition(win, route) {
    const anchor = decodeAnchor(win.location.hash);
    const sources = handbookSources(win.document);
    if (sources) {
      const ids = Object.keys(sources).filter(
        (id) => anchor === id || anchor.startsWith(id + "--"),
      );
      if (ids.length !== 1) return null;
      return { path: "handbook/" + sources[ids[0]], anchor, articleId: ids[0] };
    }
    if (win.__prototypePack || win.document.getElementById("fh-pack-data"))
      return null;
    return { path: normalizedPath(route === "/" ? "" : route), anchor };
  }
  function installDocument(win, route) {
    const doc = win.document;
    const position = documentPosition(win, route);
    if (registered.get(doc) === doc.documentElement) return;
    registered.set(doc, doc.documentElement);
    // Native handbook viewers refit when their viewport is resized. Automatic
    // scrollbars can alternately consume/release space at fractional scales,
    // feeding that observer and painting two alternating diagram positions.
    // Reserve both scroll axes; do not replace the original SVG or listeners.
    const diagramStyle = doc.createElement("style");
    diagramStyle.dataset.vksDiagramViewport = "stable";
    diagramStyle.textContent =
      "#diagram-dialog #diagram-viewport{overflow:scroll!important;scrollbar-gutter:stable!important}#fh-ai-root,#fh-ai-top{display:none!important}";
    (doc.head || doc.documentElement).append(diagramStyle);
    const changed = () => {
      // Only position observation: original navigation, clicks and context menus
      // retain all of their own listeners and browser default behavior.
      const next = documentPosition(win, route);
      if (!next) return;
      const previous = lastDocument;
      lastDocument = next;
      if (
        !documentNavigating &&
        JSON.stringify(previous) !== JSON.stringify(next)
      )
        send("document.changed", next);
    };
    win.addEventListener("hashchange", changed);
    let selectionMenuOpen = false;
    const dismissSelectionMenu = () => {
      if (!selectionMenuOpen) return;
      selectionMenuOpen = false;
      send("document.selection_dismiss");
    };
    doc.addEventListener("pointerdown", dismissSelectionMenu, true);
    doc.addEventListener("scroll", dismissSelectionMenu, true);
    doc.addEventListener(
      "keydown",
      (event) => {
        if (event.key === "Escape") dismissSelectionMenu();
      },
      true,
    );
    doc.addEventListener(
      "contextmenu",
      (event) => {
        dismissSelectionMenu();
        if (
          event.target?.closest?.(
            "#fh-ai-root,input,textarea,[contenteditable]",
          )
        )
          return;
        const selection = win.getSelection();
        if (
          selection?.anchorNode?.parentElement?.closest(
            "#fh-ai-root,input,textarea,[contenteditable]",
          )
        )
          return;
        const text = selection?.toString().trim().slice(0, 4000);
        if (!text) return;
        event.preventDefault();
        const anchor = selection.rangeCount
          ? selection.getRangeAt(0).getBoundingClientRect()
          : null;
        let x = event.clientX,
          y = event.clientY,
          current = win;
        if (
          !Number.isFinite(x) ||
          !Number.isFinite(y) ||
          (x === 0 && y === 0)
        ) {
          x = anchor?.left || 8;
          y = anchor?.bottom || 8;
        }
        while (current !== window && current.frameElement) {
          const rect = current.frameElement.getBoundingClientRect();
          x += rect.left;
          y += rect.top;
          current = current.parent;
        }
        selectionMenuOpen = true;
        send("document.selection_menu", { text, x, y });
      },
      true,
    );
    doc.addEventListener("load", scan, true);
    if (position) {
      const previous = lastDocument;
      lastDocument = position;
      if (
        !documentNavigating &&
        ((previous &&
          win.__prototypePackPage &&
          previous.path !== position.path) ||
          (position.anchor &&
            JSON.stringify(previous) !== JSON.stringify(position)))
      )
        send("document.changed", position);
    }
  }
  function validDocumentLocation(value) {
    if (!value || typeof value !== "object") return false;
    for (const key of [
      "path",
      "anchor",
      "articleId",
      "document_anchor",
      "selector",
      "heading",
    ])
      if (
        value[key] !== undefined &&
        (typeof value[key] !== "string" || value[key].length > 2000)
      )
        return false;
    if (
      value.heading_ordinal !== undefined &&
      (!Number.isSafeInteger(value.heading_ordinal) ||
        value.heading_ordinal < 0 ||
        value.heading_ordinal > 100000)
    )
      return false;
    if (
      value.heading_level !== undefined &&
      (!Number.isInteger(value.heading_level) ||
        value.heading_level < 1 ||
        value.heading_level > 6)
    )
      return false;
    return true;
  }
  async function revealDocument(location, request) {
    if (!validDocumentLocation(location)) {
      send(
        "command.result",
        { ok: false, message: "文档定位信息无效" },
        request,
      );
      return;
    }
    const generation = ++navigation;
    documentNavigating = true;
    const path = normalizedPath(location.path);
    let current,
      articleId = location.articleId || location.document_anchor || "";
    const anchor = decodeAnchor(location.anchor || "");
    try {
      for (let attempt = 0; attempt < 200 && !current; attempt++) {
        if (generation !== navigation) return;
        scan();
        const entries = Array.from(windows.values());
        for (const candidate of entries) {
          const sources = handbookSources(candidate.win.document);
          if (!sources) continue;
          const matches = Object.keys(sources).filter(
            (id) =>
              (!path ||
                normalizedPath(sources[id]) ===
                  path.replace(/^handbook\//, "")) &&
              (!articleId || id === articleId) &&
              (!anchor || anchor === id || anchor.startsWith(id + "--")),
          );
          if (matches.length === 1) {
            current = candidate;
            articleId = matches[0];
            break;
          }
        }
        if (!current && !articleId) {
          const pack = entries.find(({ win }) => win.__prototypePack?.show);
          if (pack) {
            const candidates = entries.filter(
              ({ win }) =>
                win.__prototypePackPage?.path &&
                normalizedPath(win.__prototypePackPage.path) === path,
            );
            if (candidates.length === 1) current = candidates[0];
            else if (attempt === 0 || attempt === 10)
              pack.win.__prototypePack.show(path);
          } else {
            const candidates = entries.filter(
              ({ win }) =>
                !win.__prototypePack &&
                !win.document.getElementById("fh-pack-data"),
            );
            const root = candidates.find(({ path }) => path.length === 0);
            if (root) current = root;
            else if (candidates.length === 1) current = candidates[0];
          }
        }
        if (!current) await new Promise((resolve) => setTimeout(resolve, 100));
      }
      if (!current) throw new Error("未找到准确的原文档页面");
      const win = current.win,
        doc = win.document;
      const targetAnchor = anchor || articleId;
      if (articleId) win.fhBookPages?.ensure(targetAnchor);
      if (targetAnchor) {
        const before = win.location.hash;
        win.location.hash = targetAnchor;
        if (win.location.hash === before)
          win.dispatchEvent(new win.HashChangeEvent("hashchange"));
        await new Promise((resolve) =>
          win.requestAnimationFrame(() => win.requestAnimationFrame(resolve)),
        );
      }
      if (generation !== navigation) return;
      const article = articleId ? doc.getElementById(articleId) : doc.body;
      if (!article) throw new Error("原文档中没有此文章");
      let target = anchor ? doc.getElementById(anchor) : null;
      if (anchor && !target) throw new Error("原文档中没有此准确锚点");
      if (location.selector) {
        const matches = Array.from(doc.querySelectorAll(location.selector));
        if (matches.length !== 1 || (target && target !== matches[0]))
          throw new Error("原文档位置不唯一或已变化");
        target = matches[0];
      }
      if (location.heading_ordinal !== undefined) {
        const headings = article.querySelectorAll("h1,h2,h3,h4,h5,h6");
        const heading = headings[location.heading_ordinal];
        const clean = (value) => (value || "").trim().replace(/\s+/g, " ");
        if (
          !heading ||
          !location.heading ||
          clean(heading.textContent) !== clean(location.heading) ||
          (location.heading_level &&
            heading.tagName !== "H" + location.heading_level) ||
          (target && target !== heading)
        )
          throw new Error("原文档章节位置与索引不一致");
        target = heading;
      } else if (location.heading && !target)
        throw new Error("缺少准确的原文档章节位置");
      target ||= article;
      if (articleId && !article.contains(target) && article !== target)
        throw new Error("章节不属于此原文档文章");
      for (
        let element = target.parentElement;
        element;
        element = element.parentElement
      )
        if (element.tagName === "DETAILS") element.open = true;
      if (!visible(target))
        throw new Error("准确章节当前不可见，请使用原文档目录打开");
      target.scrollIntoView({ block: "start", inline: "nearest" });
      lastDocument = documentPosition(win, current.route);
      send("command.result", { ok: true, message: "已定位到原文档" }, request);
    } catch (error) {
      if (generation === navigation)
        send(
          "command.result",
          { ok: false, message: error.message || "原文档定位失败" },
          request,
        );
    } finally {
      if (generation === navigation) documentNavigating = false;
    }
  }
  function shownWindow(win) {
    for (let child = win; child !== frame.contentWindow; child = child.parent) {
      if (!visible(child.frameElement)) return false;
    }
    return visible(frame);
  }
  function prototypePage(t) {
    const key = JSON.stringify(t.frame_path);
    const current = windows.get(key);
    if (!current || current.route !== t.route || ambiguousFrames.has(key)) return null;
    try {
      return current.win.document === current.doc && shownWindow(current.win) ? current : null;
    } catch {
      return null;
    }
  }
  function matchingControl(t, current) {
    const matches = Array.from(current.doc.querySelectorAll(t.selector)).filter(visible);
    const el = matches.length === 1 ? matches[0] : null;
    const matchedLabel = el && (t.selector === "body"
      ? moduleTarget(current.win, current.path, current.route).label : label(el));
    return matchedLabel === t.label ? el : null;
  }
  const documentIds = new WeakMap();
  let nextDocumentId = 0;
  function observeTarget(t, request, pageOnly) {
    try {
      scan();
      const current = prototypePage(t);
      if (current) {
        const { doc, route, path } = current;
        const control = pageOnly ? null : matchingControl(t, current);
        const observed = observableState(doc);
        const complete = observed.length < 500;
        const verified = !!control && !!t.state && complete && observed === t.state;
        if (!documentIds.has(doc)) documentIds.set(doc, ++nextDocumentId);
        send("command.result", {
          ok: pageOnly || !!control,
          navigation_level: control ? (verified ? "state" : "control") : "page",
          document_id: documentIds.get(doc),
          route,
          frame_path: path,
          state_verified: verified,
          state_complete: complete,
          observed_state: observed.slice(0, 500),
          target: control ? { selector: t.selector, label: t.label } : undefined,
        }, request);
        return;
      }
    } catch {
      // A detached, inaccessible, or invalid target cannot be verified.
    }
    send("command.result", { ok: false }, request);
  }
  function showPrototypePage(pack, route) {
    const source = frame.contentWindow;
    const page = source.document.querySelector("iframe#actualPage");
    const src = Object.getOwnPropertyDescriptor(source.HTMLIFrameElement.prototype, "src");
    if (materialWrapper !== "static-site" || !page?.contentWindow ||
        !src?.get || !src.set || Object.hasOwn(page, "src")) {
      pack.show(route.slice(1));
      return;
    }
    // Older immutable site wrappers set iframe.src synchronously. Replace
    // that internal history entry when Studio is following a host bookmark.
    Object.defineProperty(page, "src", {
      configurable: true,
      get() { return src.get.call(page); },
      set(value) {
        if (typeof value === "string" && value.startsWith("blob:" + location.origin + "/"))
          page.contentWindow.location.replace(value);
        else src.set.call(page, value);
      },
    });
    try {
      pack.show(route.slice(1));
    } finally {
      delete page.src;
    }
  }
  let openingPage = null;
  async function reveal(t, request, pageOnly, highlight) {
    picking = false;
    const generation = ++navigation;
    const get = () => {
      if (openingPage) {
        if (windows.get(openingPage.key)?.route !== openingPage.route) return null;
        openingPage = null;
      }
      return prototypePage(t);
    };
    const result = (message, level, observed, selectedTarget) => {
      if (generation !== navigation) return;
      send("command.result", {
        ok: level === "control" || level === "state" || (pageOnly && level === "page"),
        message,
        navigation_level: level,
        route: level ? t.route : undefined,
        frame_path: level ? t.frame_path : undefined,
        target: selectedTarget,
        state_verified: level === "state",
        observed_state: observed?.slice(0, 500),
      }, request);
    };
    try {
      let current = get();
      let opened = false;
      // Keep the existing form when the requested page is already open.
      for (let i = 0; i < 50 && !current; i++) {
        const pack = frame.contentWindow.__prototypePack;
        if (!opened && !openingPage && materialWrapper && typeof pack?.show === "function") {
          // A previous request may have started a frame navigation. Wait for
          // its document to commit before verifying the next destination.
          const frameIndex = t.frame_path.indexOf("actualPage");
          if (frameIndex >= 0)
            openingPage = { route: t.route, key: JSON.stringify(t.frame_path.slice(0, frameIndex + 1)) };
          opened = true;
          showPrototypePage(pack, t.route);
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
        if (generation !== navigation) return;
        scan();
        current = get();
      }
      if (!current) {
        openingPage = null;
        result("页面尚未加载，请在原型中打开对应页面");
        return;
      }
      const { doc } = current;
      const exactPage = () => get()?.doc === doc;
      const find = () => exactPage() ? matchingControl(t, current) : null;
      if (pageOnly) {
        result("已打开原型页面", "page", observableState(doc));
        return;
      }
      const stateMatches = () => {
        const observed = observableState(doc);
        return observed === t.state && observed.length < 500;
      };
      for (const step of t.open_steps || []) {
        if (generation !== navigation) return;
        if (!exactPage()) throw new Error("原型页面已切换");
        if (find() && stateMatches()) break;
        const candidates = Array.from(doc.querySelectorAll(step)).filter(visible);
        if (candidates.length !== 1) break;
        candidates[0].click();
        await new Promise((resolve) => setTimeout(resolve, 280));
        scan();
      }
      let el;
      for (let attempt = 0; attempt < 25; attempt++) {
        if (generation !== navigation) return;
        el = find();
        if (el && (!t.state || stateMatches())) break;
        if (attempt < 24) await new Promise((resolve) => setTimeout(resolve, 50));
        scan();
      }
      if (!exactPage()) {
        result("原型页面已切换，无法确认位置");
        return;
      }
      if (!el) {
        result("已打开原型页面，当前无法定位指定控件", "page", observableState(doc));
        return;
      }
      if (highlight) {
        glow(el);
        el.focus({ preventScroll: true });
      } else el.scrollIntoView({ block: "nearest", inline: "nearest" });
      if (find() !== el) {
        result("目标在定位过程中发生变化，仅确认页面已打开", "page", observableState(doc));
        return;
      }
      const observed = observableState(doc);
      const verified = !!t.state && observed === t.state && observed.length < 500;
      result(verified ? "已核验指定状态并定位到 " + t.label
        : t.state || observed ? "控件可见，无法核验指定状态"
        : "已定位到 " + t.label,
        verified ? "state" : "control", observed, {
          ...target(el, current.win, current.path, current.route),
          selector: t.selector,
          label: t.label,
        });
    } catch {
      result(get() ? "已打开原型页面，当前无法定位指定控件" : "原型页面无法打开",
        get() ? "page" : undefined);
    }
  }
  let lastPosition = "";
  function scan(activateModule = false) {
    if (loaded) {
      windows.clear();
      ambiguousFrames.clear();
      install(frame.contentWindow, [], "/");
      if (!documentMode) {
        let active = null;
        for (const { win, path, route } of windows.values()) {
          if (
            !ambiguousFrames.has(JSON.stringify(path)) && shownWindow(win) &&
            ((!path.length && !materialWrapper && !win.__prototypePack) ||
              (path[path.length - 1] === "actualPage" && win.__prototypePackPage?.path))
          ) active = { route, frame_path: path, state: stateOf(win.document) };
        }
        const position = JSON.stringify(active);
        if (position !== lastPosition) {
          lastPosition = position;
          send("prototype.position", active);
        }
      }
      if (activateModule === true) {
        let active = null;
        for (const { win, path, route } of windows.values()) {
          let shown = true;
          for (
            let child = win;
            child !== frame.contentWindow && shown;
            child = child.parent
          )
            shown = !!child.frameElement && visible(child.frameElement);
          if (
            shown &&
            (path.length === 0 || path.includes("actualPage")) &&
            ((!win.__prototypePack &&
              !win.document.getElementById("prototypePackPayload")) ||
              win.__prototypePackPage?.path)
          )
            active = moduleContext(win, path, route);
        }
        if (active) {
          currentModule = active;
          send("control.activated", active);
        }
      }
    }
  }
  async function inventory(payload, request) {
    const route = payload?.route;
    const path = payload?.frame_path;
    const scanId = payload?.scan_id;
    if (
      documentMode ||
      !loaded ||
      typeof scanId !== "string" ||
      scanId.length > 100 ||
      typeof route !== "string" ||
      !route.startsWith("/") ||
      route.length > 1000 ||
      !Array.isArray(path) ||
      path.length > 8 ||
      path.some(
        (part) => typeof part !== "string" || !part || part.length > 160,
      ) ||
      !inventorySpec?.pages.some(
        (page) =>
          page.route === route &&
          JSON.stringify(page.frame_path) === JSON.stringify(path),
      )
    )
      return;
    const generation = ++inventoryGeneration;
    ++navigation;
    picking = false;
    const started = performance.now();
    let observed = null,
      changedAt = started,
      observing = [],
      dirty = true,
      captured = null,
      lastFingerprint = "";
    const invalidate = () => {
      dirty = true;
    };
    const samePrefix = (actual) =>
      path.every((part, index) => part === actual[index]);
    const activeWindow = (entry) => {
      try {
        if (
          entry.route !== route ||
          !samePrefix(entry.path) ||
          entry.path.some((_, index) =>
            ambiguousFrames.has(JSON.stringify(entry.path.slice(0, index + 1))),
          ) ||
          !entry.win.document.body ||
          entry.win.document.readyState === "loading"
        )
          return false;
        for (let win = entry.win; win !== window; win = win.parent)
          if (!win.frameElement || !visible(win.frameElement)) return false;
        return true;
      } catch {
        return false;
      }
    };
    const candidates = () => Array.from(windows.values()).filter(activeWindow);
    const shown = (node) => {
      if (
        !visible(node) ||
        !Array.from(node.getClientRects()).some(
          (rect) => rect.width > 0 && rect.height > 0,
        )
      )
        return false;
      // Vue keeps a leaving loading mask in the DOM while its opacity is zero.
      // Such a completed transition is not an active loading state.
      for (let parent = node; parent; parent = parent.parentElement)
        if (Number(getComputedStyle(parent).opacity) === 0) return false;
      return true;
    };
    const mountPending = (doc) => {
      if (doc.readyState !== "complete") return true;
      if (
        Array.from(
          doc.querySelectorAll(
            '[aria-busy="true"],.el-loading-mask,.ant-spin-spinning,.ant-skeleton,.skeleton,[data-loading="true"]',
          ),
        ).some(shown)
      )
        return true;
      for (const root of doc.querySelectorAll("#app,#root,[data-v-app]")) {
        if (!visible(root)) continue;
        const text = labelText(root).trim();
        if (
          /^(loading[.\s…]*|正在加载[.。\s…]*|加载中[.。\s…]*|请稍候[.。\s…]*)$/i.test(
            text,
          )
        )
          return true;
        if (
          !text &&
          !root.querySelector(
            "button,a,input,select,textarea,img,svg,canvas,table,article,h1,h2,h3,p,ul,ol",
          )
        ) {
          // Vue/React dialogs can render through a portal outside their empty
          // mount root. Observe that rendered UI rather than treating the
          // unused placeholder as a permanently loading application. A blank
          // root with only a surrounding toolbar still remains pending.
          const portalReady = Array.from(doc.querySelectorAll(dialogs)).some(
            (dialog) =>
              !root.contains(dialog) &&
              shown(dialog) &&
              Array.from(dialog.querySelectorAll(actionable)).some(shown),
          );
          if (!portalReady) return true;
        }
      }
      return false;
    };
    const result = (status, targets, count, unverified, message = "") =>
      send(
        "inventory.result",
        {
          scan_id: scanId,
          route,
          frame_path: path,
          status,
          targets,
          observed: count,
          truncated: Math.max(0, count - targets.length),
          unverified_frames: unverified,
          message,
        },
        request,
      );
    try {
      scan();
      // Current pages are observed directly. Cross-page navigation is limited
      // to a declared page in a recognized wrapper; arbitrary source globals
      // with the same function name must never become navigation commands.
      let navigated = false;
      while (
        generation === inventoryGeneration &&
        performance.now() - started < 10000
      ) {
        await new Promise((resolve) => setTimeout(resolve, 100));
        if (generation !== inventoryGeneration) return;
        scan();
        const entries = candidates();
        if (!entries.length && !navigated) {
          const pack = Array.from(windows.values()).find((entry) => {
            const doc = entry.win.document;
            return (
              entry.path.length === 0 &&
              entry.win === frame.contentWindow &&
              materialWrapper === inventorySpec.format &&
              doc.querySelector("iframe#actualPage") &&
              typeof entry.win.__prototypePack?.show === "function"
            );
          });
          if (pack) {
            navigated = true;
            pack.win.__prototypePack.show(route.replace(/^\//, ""));
            continue;
          }
        }
        if (!entries.length) continue;
        const docs = entries.map((entry) => entry.win.document);
        if (
          !observed ||
          docs.length !== observed.length ||
          docs.some((doc, index) => doc !== observed[index])
        ) {
          observing.forEach((observer) => observer.disconnect());
          observed = docs;
          changedAt = performance.now();
          dirty = true;
          captured = null;
          observing = docs.map((doc) => {
            const observer = new MutationObserver(invalidate);
            // Head changes include a late title or stylesheet. A mutation marks
            // observations dirty; only changed targets/readiness reset quiet
            // time, so an unrelated ticking clock cannot starve the scan.
            observer.observe(doc.documentElement, {
              childList: true,
              subtree: true,
              characterData: true,
              attributes: true,
            });
            const events = [
              "load",
              "resize",
              "transitionrun",
              "transitionend",
              "animationstart",
              "animationend",
            ];
            for (const event of events)
              doc.addEventListener(event, invalidate, true);
            doc.defaultView?.addEventListener("resize", invalidate);
            return {
              disconnect() {
                observer.disconnect();
                for (const event of events)
                  doc.removeEventListener(event, invalidate, true);
                doc.defaultView?.removeEventListener("resize", invalidate);
              },
            };
          });
        }
        // Stable pages keep their last extraction. Recheck layout/readiness at
        // the final quiet boundary, including CSS-only visibility changes.
        const minimum = captured?.items.some((item) => item.selector !== "body")
          ? 700
          : 2000;
        const finishing =
          captured &&
          performance.now() - started >= minimum &&
          performance.now() - changedAt >= 500;
        if (!dirty && captured && !finishing) continue;
        dirty = false;
        if (docs.some(mountPending)) {
          changedAt = performance.now();
          dirty = true;
          continue;
        }
        const items = [],
          seen = new Set();
        let unverified = 0;
        for (const entry of entries) {
          const doc = entry.win.document;
          const module = moduleTarget(entry.win, entry.path, route);
          items.push(module);
          const state = stateOf(doc);
          for (const node of doc.querySelectorAll(actionable)) {
            if (
              !shown(node) ||
              node.matches("label") ||
              (editable(node) && node.isContentEditable)
            )
              continue;
            const item = target(node, entry.win, entry.path, route, state);
            item.open_steps = [];
            const key = JSON.stringify([
              item.frame_path,
              item.selector,
              item.state,
            ]);
            if (!seen.has(key) && item.selector.length <= 2000) {
              seen.add(key);
              items.push(item);
            }
          }
          for (const child of doc.querySelectorAll("iframe")) {
            if (!visible(child)) continue;
            if (
              ambiguousFrames.has(
                JSON.stringify([
                  ...entry.path,
                  child.id || child.title || "frame",
                ]),
              )
            ) {
              unverified++;
              continue;
            }
            try {
              if (!child.contentWindow?.document?.body) unverified++;
            } catch {
              unverified++;
            }
          }
        }
        const fingerprint = JSON.stringify([items, unverified]);
        captured = { items, unverified };
        if (fingerprint !== lastFingerprint) {
          changedAt = performance.now();
          lastFingerprint = fingerprint;
        }
        // Empty app mounts need a longer quiet interval than populated pages.
        const observedMinimum = items.some((item) => item.selector !== "body")
          ? 700
          : 2000;
        if (
          performance.now() - started < observedMinimum ||
          performance.now() - changedAt < 500
        )
          continue;
        if (
          !items.some(
            (item) =>
              item.selector === "body" &&
              JSON.stringify(item.frame_path) === JSON.stringify(path),
          )
        ) {
          result(
            "failed",
            [],
            0,
            unverified,
            "声明模块的实际 frame 未能准确加载",
          );
          return;
        }
        result("captured", items.slice(0, 1500), items.length, unverified);
        return;
      }
      if (generation === inventoryGeneration)
        result("failed", [], 0, 0, "页面未在时限内稳定或声明页面尚未加载");
    } catch (error) {
      if (generation === inventoryGeneration)
        result("failed", [], 0, 0, error.message || "页面扫描失败");
    } finally {
      observing.forEach((observer) => observer.disconnect());
    }
  }
  window.addEventListener("message", (event) => {
    if (
      event.source !== parent ||
      event.origin !== host ||
      event.data?.type !== "vks.connect" ||
      event.data?.instance !== instance ||
      !event.ports[0] ||
      port
    )
      return;
    port = event.ports[0];
    port.onmessage = async ({ data }) => {
      if (
        data?.runtime_instance_id !== instance ||
        data?.protocol_version !== 1
      )
        return;
      if (
        data.type === "material.load" &&
        data.payload?.bytes instanceof ArrayBuffer &&
        data.payload.bytes.byteLength <= 80 * 1024 * 1024
      ) {
        ++inventoryGeneration;
        inventorySpec = null;
        materialWrapper = null;
        openingPage = null;
        if (!documentMode) {
          // Bind the wrapper fact to this verified material transfer. Charge
          // startup removes its inert payload node, so live DOM is insufficient.
          const parsed = new DOMParser().parseFromString(
            new TextDecoder().decode(data.payload.bytes),
            "text/html",
          );
          if (
            parsed.querySelector('script#vks-site[type="application/json"]') &&
            parsed.querySelector("iframe#actualPage")
          )
            materialWrapper = "static-site";
          else if (
            parsed.querySelector(
              'script#prototypePackPayload[type="application/octet-stream"]',
            ) &&
            parsed.querySelector("iframe#actualPage")
          )
            materialWrapper = "charge-prototype";
        }
        if (documentMode) {
          // Native submit listeners must run; CSP form-action 'none' still
          // prevents a form from navigating or sending its fields directly.
          frame.sandbox.add("allow-forms");
          if (documentURL) URL.revokeObjectURL(documentURL);
          documentURL = URL.createObjectURL(
            new Blob([data.payload.bytes], { type: "text/html" }),
          );
          frame.src = documentURL;
        } else frame.srcdoc = new TextDecoder().decode(data.payload.bytes);
        frame.hidden = false;
        document.querySelector("#status").hidden = true;
        loaded = true;
        send("material.loaded");
      } else if (data.type === "inventory.configure") {
        const p = data.payload;
        if (
          !documentMode &&
          p &&
          ["html", "static-site", "charge-prototype"].includes(p.format) &&
          Array.isArray(p.pages) &&
          p.pages.length <= 10000 &&
          p.pages.every(
            (page) =>
              typeof page.route === "string" &&
              page.route.startsWith("/") &&
              page.route.length <= 1000 &&
              Array.isArray(page.frame_path) &&
              page.frame_path.length <= 8 &&
              page.frame_path.every(
                (part) =>
                  typeof part === "string" &&
                  part.length > 0 &&
                  part.length <= 160,
              ),
          )
        ) {
          ++inventoryGeneration;
          inventorySpec = p;
        }
      } else if (data.type === "inventory.scan") {
        await inventory(data.payload, data.request_id);
      } else if (data.type === "inventory.cancel") {
        ++inventoryGeneration;
      } else if (data.type === "knowledge.request") {
        if (documentMode) return;
        const c =
          data.payload?.scope === "module"
            ? currentModule
            : pointed && visible(pointed.el)
              ? selected(pointed.el, pointed.win, pointed.path, pointed.route)
              : currentModule;
        if (c)
          send("knowledge.request", {
            ...c,
            mode: data.payload?.mode === "documents" ? "documents" : "explain",
          });
      } else if (data.type === "picker.start") {
        if (documentMode) return;
        picking = true;
        send("picker.started");
      } else if (data.type === "picker.cancel") {
        if (documentMode) return;
        picking = false;
        send("picker.cancelled");
      } else if (data.type === "document.reveal" && documentMode)
        await revealDocument(data.payload?.location, data.request_id);
      else if (data.type === "document.cancel" && documentMode) {
        navigation++;
        documentNavigating = false;
      }
      else if (data.type === "target.cancel" && !documentMode)
        navigation++;
      else if (
        data.type === "target.reveal" &&
        !documentMode &&
        data.payload?.target
      )
        await reveal(data.payload.target, data.request_id,
          data.payload.page_only === true, data.payload.highlight !== false);
      else if (data.type === "target.observe" && !documentMode && data.payload?.target)
        observeTarget(data.payload.target, data.request_id, data.payload.page_only === true);
    };
    port.start();
    send("runtime.ready");
  });
  frame.addEventListener("load", scan);
  setInterval(scan, 200);
  parent.postMessage({ type: "vks.runtime.ready", instance }, host);
})();
