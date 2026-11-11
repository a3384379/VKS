/* A static, credential-free runtime. Source HTML never runs in Studio's origin. */
(() => {
  "use strict";
  const query = new URLSearchParams(location.search);
  const instance = query.get("instance");
  const host = query.get("host");
  if (!instance || !host || !/^https?:\/\//.test(host)) return;
  const frame = document.querySelector("#material");
  let port,
    picking = false,
    sequence = 0,
    epoch = 1,
    loaded = false,
    navigation = 0;
  const registered = new WeakMap();
  const windows = new Map();
  const histories = new WeakMap();
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
  const visible = (el) =>
    !!(
      el?.isConnected &&
      el.getClientRects().length &&
      getComputedStyle(el).visibility !== "hidden"
    );
  const label = (el) =>
    (
      el.getAttribute("aria-label") ||
      el.getAttribute("title") ||
      el.getAttribute("placeholder") ||
      el.textContent?.trim() ||
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
  const stateOf = (doc) => {
    const candidates = Array.from(
      doc.querySelectorAll(
        '[role="dialog"],.el-dialog,.el-drawer,.el-message-box,.ant-modal,.modal,.drawer,.offline-modal,.demo-modal,.prototype-modal,.vks-modal',
      ),
    );
    return candidates.filter(visible).map(selector).join(" | ").slice(0, 500);
  };
  function target(el, win, path, route) {
    return {
      route,
      frame_path: path,
      selector: selector(el),
      label: label(el),
      state: stateOf(win.document),
      open_steps: histories.get(win) || [],
    };
  }
  function glow(el) {
    const prior = el.style.outline,
      offset = el.style.outlineOffset;
    el.style.outline = "3px solid #13a58d";
    el.style.outlineOffset = "3px";
    el.scrollIntoView({ block: "nearest", inline: "nearest" });
    setTimeout(() => {
      el.style.outline = prior;
      el.style.outlineOffset = offset;
    }, 2200);
  }
  function install(win, path, route) {
    let doc;
    try {
      doc = win.document;
      if (!doc?.body || doc.readyState === "loading") return;
    } catch {
      return;
    }
    const own = win.__prototypePackPage;
    route = own?.path ? "/" + own.path.replace(/^\//, "") : route;
    windows.set(path.join("/"), { win, route, path });
    if (registered.get(doc) !== doc.documentElement) {
      registered.set(doc, doc.documentElement);
      doc.addEventListener(
        "click",
        (event) => {
          const el =
            event.target?.closest?.(
              'button,a,input,select,textarea,[role="button"],label,.el-switch,.el-checkbox,.el-radio',
            ) || (picking ? event.target : null);
          if (!el || !visible(el)) return;
          const currentRoute = win.__prototypePackPage?.path
            ? "/" + win.__prototypePackPage.path.replace(/^\//, "")
            : route;
          const selected = target(el, win, path, currentRoute);
          if (picking) {
            event.preventDefault();
            event.stopImmediatePropagation();
            picking = false;
            glow(el);
            send("target.selected", selected);
            return;
          }
          send("action.activated", selected);
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
          if (event.key === "Escape" && picking) {
            picking = false;
            event.preventDefault();
            event.stopImmediatePropagation();
            send("picker.cancelled");
          }
        },
        true,
      );
      if (path.includes("actualPage") || path.length === 0)
        send("context.changed", { route, frame_path: path });
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
  async function reveal(t, request) {
    picking = false;
    const generation = ++navigation;
    let root = frame.contentWindow;
    const get = () =>
      Array.from(windows.values()).find(
        (w) =>
          w.route === t.route &&
          JSON.stringify(w.path) === JSON.stringify(t.frame_path),
      );
    let current = get();
    // Preserve the existing form when the requested page is already open.
    if (!current && root.__prototypePack?.show)
      root.__prototypePack.show(t.route.replace(/^\//, ""));
    for (let i = 0; i < 50 && !current; i++) {
      await new Promise((r) => setTimeout(r, 100));
      if (generation !== navigation) return;
      scan();
      current = get();
    }
    if (!current) {
      send(
        "command.result",
        { ok: false, message: "页面尚未加载，请在原型中打开对应页面" },
        request,
      );
      return;
    }
    let el;
    try {
      const find = () =>
        Array.from(current.win.document.querySelectorAll(t.selector))
          .filter(visible)
          .filter((n) => label(n) === t.label);
      for (const step of t.open_steps || []) {
        if (generation !== navigation) return;
        if (find().length) break;
        const candidates = Array.from(
          current.win.document.querySelectorAll(step),
        ).filter(visible);
        if (candidates.length !== 1) break;
        candidates[0].click();
        await new Promise((r) => setTimeout(r, 280));
      }
      const matches = find();
      if (matches.length === 1) el = matches[0];
    } catch {}
    if (el) {
      glow(el);
      el.focus({ preventScroll: true });
      send(
        "command.result",
        { ok: true, message: "已定位到 " + t.label },
        request,
      );
    } else
      send(
        "command.result",
        {
          ok: false,
          message: "已打开页面，请打开目标弹窗后重试定位；多个候选时请重新选择",
        },
        request,
      );
  }
  function scan() {
    if (loaded) {
      windows.clear();
      install(frame.contentWindow, [], "/");
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
        frame.srcdoc = new TextDecoder().decode(data.payload.bytes);
        frame.hidden = false;
        document.querySelector("#status").hidden = true;
        loaded = true;
        send("material.loaded");
      } else if (data.type === "picker.start") {
        picking = true;
        send("picker.started");
      } else if (data.type === "picker.cancel") {
        picking = false;
        send("picker.cancelled");
      } else if (data.type === "target.reveal" && data.payload?.target)
        await reveal(data.payload.target, data.request_id);
    };
    port.start();
    send("runtime.ready");
  });
  setInterval(scan, 200);
  parent.postMessage({ type: "vks.runtime.ready", instance }, host);
})();
