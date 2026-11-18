import { showSelectionMenu } from "./selectionMenu.ts";
import { openImagePreview as imageDialog } from "./imagePreview.ts";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkRehype from "remark-rehype";
import rehypeRaw from "rehype-raw";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkCjkFriendlyGfm from "remark-cjk-friendly-gfm-strikethrough";
import katex from "katex";
import DOMPurify from "dompurify";
import { common, createLowlight } from "lowlight";
import { MAX_SVG_LENGTH, validateDiagramSource } from "./diagram-policy.ts";

const parser = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkMath)
  .use(remarkCjkFriendly)
  .use(remarkCjkFriendlyGfm);
const convert = unified()
  .use(remarkRehype, {
    allowDangerousHtml: true,
    handlers: {
      math(_state, node) {
        return {
          type: "element",
          tagName: "div",
          properties: { dataVksMath: node.value, dataVksDisplay: true },
          children: [],
          position: node.position,
        };
      },
      inlineMath(_state, node) {
        return {
          type: "element",
          tagName: "span",
          properties: { dataVksMath: node.value },
          children: [],
          position: node.position,
        };
      },
    },
  })
  .use(rehypeRaw);
const lowlight = createLowlight(common);
let sequence = 0;
const allowed = new Set(
  "a p div span h1 h2 h3 h4 h5 h6 strong b em i u del s sup sub blockquote ul ol li table caption colgroup col thead tbody tfoot tr th td pre code br hr details summary kbd samp var time input section dl dt dd figure figcaption".split(
    " ",
  ),
);
const active = new Set(
  "script style iframe object embed link meta base form button textarea select video audio source picture svg math canvas".split(
    " ",
  ),
);
const element = (tag, attributes = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes))
    if (value !== undefined && value !== null)
      node.setAttribute(name, String(value));
  for (const child of children.flat(Infinity))
    if (child !== undefined && child !== null)
      node.append(
        typeof child === "string" ? document.createTextNode(child) : child,
      );
  return node;
};

function safeCss(css) {
  const local = css.replace(/url\(\s*["']?#[a-z_][\w:.-]*["']?\s*\)/gi, "");
  return !/url\s*\(|@import|@font-face|expression\s*\(|behavior\s*:|\\/i.test(
    local.replace(/\/\*[\s\S]*?\*\//g, ""),
  );
}
function sanitizedSvg(source) {
  if (source.length > MAX_SVG_LENGTH || /<!DOCTYPE|<!ENTITY/i.test(source))
    throw new Error("图表输出无效或过大");
  const cleaned = DOMPurify.sanitize(source, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: [
      "script",
      "foreignObject",
      "image",
      "use",
      "a",
      "animate",
      "animateMotion",
      "set",
      "iframe",
      "object",
      "embed",
    ],
    FORBID_ATTR: ["href", "xlink:href", "src"],
  });
  const doc = new DOMParser().parseFromString(cleaned, "image/svg+xml");
  const root = doc.documentElement;
  if (
    doc.querySelector("parsererror") ||
    root.localName !== "svg" ||
    root.namespaceURI !== "http://www.w3.org/2000/svg"
  )
    throw new Error("图表格式无效");
  const box = root.getAttribute("viewBox")?.trim().split(/[ ,]+/).map(Number);
  if (
    box &&
    (box.length !== 4 ||
      box.some((n) => !Number.isFinite(n)) ||
      box[2] <= 0 ||
      box[3] <= 0 ||
      box[2] > 8192 ||
      box[3] > 8192)
  )
    throw new Error("图表尺寸超限");
  if (box) {
    root.setAttribute("width", box[2]);
    root.setAttribute("height", box[3]);
    root.setAttribute(
      "style",
      (root.getAttribute("style") || "").replace(/max-width\s*:[^;]+;?/gi, ""),
    );
  }
  for (const node of [root, ...root.querySelectorAll("*")]) {
    if (node.localName === "style" && !safeCss(node.textContent || ""))
      throw new Error("图表包含外部资源");
    for (const attribute of [...node.attributes]) {
      if (/^on/i.test(attribute.name)) node.removeAttribute(attribute.name);
      if (!safeCss(attribute.value)) throw new Error("图表包含外部资源");
    }
  }
  root.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  return new XMLSerializer().serializeToString(root);
}

let engine;
async function diagramEngine() {
  if (!engine)
    engine = (async () => {
      const compressed = Uint8Array.from(atob(__VKS_MERMAID_GZIP__), (c) =>
        c.charCodeAt(0),
      );
      return new TextDecoder().decode(
        await new Response(
          new Blob([compressed])
            .stream()
            .pipeThrough(new DecompressionStream("gzip")),
        ).arrayBuffer(),
      );
    })();
  return engine;
}
let activeDiagrams = 0;
const diagramQueue = [];
function nextDiagram() {
  while (activeDiagrams < 2 && diagramQueue.length) diagramQueue.shift()();
}
function renderDiagram(source, figure) {
  return new Promise((resolve, reject) => {
    diagramQueue.push(async () => {
      if (!figure.isConnected) {
        reject(new Error("已取消"));
        nextDiagram();
        return;
      }
      activeDiagrams++;
      const nonce = crypto.randomUUID(),
        frame = element("iframe", {
          title: "本地图表渲染器",
          sandbox: "allow-scripts",
          "aria-hidden": "true",
          tabindex: "-1",
        });
      frame.style.cssText =
        "position:fixed;left:-20000px;top:0;width:1400px;height:1200px;border:0;visibility:hidden;pointer-events:none";
      let finished = false,
        timer,
        check;
      const finish = (error, svg) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        clearInterval(check);
        window.removeEventListener("message", receive);
        frame.remove();
        activeDiagrams--;
        nextDiagram();
        error ? reject(error) : resolve(svg);
      };
      const receive = async (event) => {
        if (
          event.source !== frame.contentWindow ||
          event.origin !== "null" ||
          event.data?.nonce !== nonce
        )
          return;
        if (event.data.type === "ready") {
          try {
            const runtime = await diagramEngine();
            if (!finished)
              frame.contentWindow.postMessage({ nonce, runtime, source }, "*");
          } catch {
            finish(new Error("本地图表引擎未能打开"));
          }
        } else if (event.data.type === "result") {
          if (typeof event.data.svg === "string") {
            try {
              finish(null, sanitizedSvg(event.data.svg));
            } catch {
              finish(new Error("图表输出无法安全显示"));
            }
          } else finish(new Error("图表未能渲染，请检查语法或查看源码"));
        }
      };
      window.addEventListener("message", receive);
      const boot = `const nonce=${JSON.stringify(nonce)};let accepted=false;window.addEventListener('message',async event=>{if(accepted||event.source!==parent||event.data?.nonce!==nonce||typeof event.data.runtime!=='string')return;accepted=true;try{const script=document.createElement('script');script.textContent=event.data.runtime;document.head.append(script);const svg=await window.VksReaderDiagram(event.data.source);parent.postMessage({nonce,type:'result',svg},'*')}catch{parent.postMessage({nonce,type:'result',error:true},'*')}});parent.postMessage({nonce,type:'ready'},'*');`;
      frame.srcdoc =
        "<!doctype html><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none';script-src 'unsafe-inline';style-src 'unsafe-inline';img-src data: blob:;font-src data:;connect-src 'none';object-src 'none';base-uri 'none';frame-src 'none'\"><body><script>" +
        boot +
        "<" +
        "/script></body>";
      timer = setTimeout(
        () => finish(new Error("图表渲染超时，可展开源码查看")),
        15000,
      );
      check = setInterval(() => {
        if (!figure.isConnected) finish(new Error("已取消"));
      }, 150);
      document.body.append(frame);
    });
    nextDiagram();
  });
}
function diagram(source) {
  const figure = element("figure", {
    class: "md-diagram",
    "aria-label": "Mermaid 图表",
  });
  const status = element(
    "p",
    { class: "md-diagram-status", role: "status" },
    "正在本地生成图表…",
  );
  const details = element(
    "details",
    {},
    element("summary", {}, "查看图表源码"),
    element("pre", {}, element("code", {}, source)),
  );
  figure.append(
    element("figcaption", { class: "md-diagram-head" }, "流程与关系图"),
    status,
    details,
  );
  const problem = validateDiagramSource("mermaid", source);
  if (problem) {
    status.textContent = problem;
    details.open = true;
    return figure;
  }
  const begin = async () => {
    try {
      const svg = await renderDiagram(source, figure);
      if (!figure.isConnected) return;
      const raw = new TextEncoder().encode(svg);
      let encoded = "";
      for (let index = 0; index < raw.length; index += 32768)
        encoded += String.fromCharCode(...raw.subarray(index, index + 32768));
      const uri = "data:image/svg+xml;base64," + btoa(encoded);
      const image = element("img", {
        src: uri,
        alt: "Mermaid 图表",
        loading: "lazy",
      });
      const show = () =>
        imageDialog(uri, "Mermaid 图表", image.ownerDocument, image);
      image.tabIndex = 0;
      image.title = "点击查看大图";
      image.setAttribute("aria-haspopup", "dialog");
      image.addEventListener("click", show);
      image.addEventListener("keydown", (event) => {
        if (["Enter", " "].includes(event.key)) {
          event.preventDefault();
          show();
        }
      });
      const enlarge = element(
        "button",
        { type: "button", "aria-label": "查看图表大图" },
        "放大查看 ↗",
      );
      enlarge.addEventListener("click", show);
      figure.querySelector("figcaption").append(enlarge);
      status.replaceWith(
        element(
          "div",
          { class: "md-diagram-image" },
          image,
          element(
            "small",
            { class: "md-media-hint" },
            "点图放大 · 适应窗口或按宽度查看细节",
          ),
        ),
      );
    } catch (error) {
      if (figure.isConnected) {
        status.textContent = error.message;
        details.open = true;
      }
    }
  };
  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        begin();
      }
    },
    { rootMargin: "350px" },
  );
  // Observe after the host has attached the returned fragment.
  setTimeout(() => {
    if (figure.isConnected) observer.observe(figure);
    else observer.disconnect();
  }, 0);
  return figure;
}

function math(source, display) {
  const node = element(display ? "div" : "span", {
    class: display ? "math-display" : "math-inline",
  });
  try {
    if (source.length > 8000) throw new Error();
    katex.render(source, node, {
      displayMode: display,
      trust: false,
      strict: "ignore",
      throwOnError: true,
      maxExpand: 1000,
      maxSize: 20,
      output: "htmlAndMathml",
    });
  } catch {
    node.classList.add("math-error");
    node.textContent = source;
    node.setAttribute("title", "公式未能渲染，保留原始表达式");
  }
  return node;
}

const definitionCache = new Map();
function definitionsFrom(source) {
  if (definitionCache.has(source)) return definitionCache.get(source);
  const result = parser
    .parse(source)
    .children.filter((node) =>
      ["definition", "footnoteDefinition"].includes(node.type),
    )
    .map((node) => ({
      key: node.type + ":" + node.identifier,
      signature: JSON.stringify(node, (name, value) =>
        name === "position" ? undefined : value,
      ),
      raw: source.slice(node.position.start.offset, node.position.end.offset),
    }));
  definitionCache.set(source, result);
  if (definitionCache.size > 16)
    definitionCache.delete(definitionCache.keys().next().value);
  return result;
}
function parseWithReferences(source, sources = []) {
  const original = parser.parse(source);
  const isDefinition = (node) =>
    ["definition", "footnoteDefinition"].includes(node.type);
  const known = new Set(
    original.children
      .filter(isDefinition)
      .map((node) => node.type + ":" + node.identifier),
  );
  const extras = new Map();
  for (const reference of sources.filter(Boolean)) {
    if (reference === source) continue;
    for (const entry of definitionsFrom(reference)) {
      if (known.has(entry.key)) continue;
      const previous = extras.get(entry.key);
      if (!previous) extras.set(entry.key, entry);
      else if (previous.signature !== entry.signature)
        extras.set(entry.key, { ...previous, ambiguous: true });
    }
  }
  const candidates = [...extras.values()].filter((entry) => !entry.ambiguous);
  if (!candidates.length) return original;
  const parsed = parser.parse(
    source + "\n\n" + candidates.map((entry) => entry.raw).join("\n\n"),
  );
  const found = new Set(
    parsed.children
      .filter(isDefinition)
      .map((node) => node.type + ":" + node.identifier),
  );
  // An unfinished streamed code fence or HTML block must never swallow source
  // definitions and turn them into unrelated body text in the answer.
  return candidates.every((entry) => found.has(entry.key)) ? parsed : original;
}

function render(source, options = {}) {
  const prefix = "vks-md-" + ++sequence + "-";
  const referenceSources =
    options.referenceSources ||
    (options.referenceSource ? [options.referenceSource] : []);
  const ast = parseWithReferences(String(source || ""), referenceSources);
  const tree = convert.runSync(ast),
    root = element("div", { class: "rich-markdown" });
  const ids = new Map();
  const collect = (node) => {
    if (node.properties?.id)
      ids.set(
        String(node.properties.id),
        prefix + String(node.properties.id).replace(/[^a-z0-9_-]/gi, "-"),
      );
    for (const child of node.children || []) collect(child);
  };
  collect(tree);
  const renderText = (value) => {
    if (
      !options.openSection ||
      (!options.citations?.length && !options.markMissingCitations)
    )
      return document.createTextNode(value);
    const result = document.createDocumentFragment();
    let index = 0;
    for (const match of value.matchAll(/\[(\d+)\]/g)) {
      result.append(document.createTextNode(value.slice(index, match.index)));
      const number = Number(match[1]);
      const reference = options.citations?.[number - 1];
      if (reference) {
        const link = element(
          "button",
          { type: "button", class: "citation-inline" },
          match[0],
        );
        link.addEventListener("click", () => options.openSection(reference.id));
        result.append(link);
      } else if (options.markMissingCitations && number > 0) {
        result.append(
          element(
            "span",
            { class: "citation-missing" },
            match[0] + "（原文缺失）",
          ),
        );
      } else result.append(document.createTextNode(match[0]));
      index = match.index + match[0].length;
    }
    result.append(document.createTextNode(value.slice(index)));
    return result;
  };
  const renderChildren = (node, plain = false) =>
    (node.children || []).map((child) => nodeToDom(child, plain));
  const nodeToDom = (node, plain = false) => {
    if (node.type === "text")
      return plain
        ? document.createTextNode(node.value)
        : renderText(node.value);
    if (node.type !== "element") return document.createDocumentFragment();
    const tag = node.tagName.toLowerCase(),
      props = node.properties || {};
    if (props.dataVksMath !== undefined)
      return math(
        String(props.dataVksMath),
        props.dataVksDisplay !== undefined,
      );
    if (tag === "img") {
      const alt = String(props.alt || "文档图片"),
        uri = options.resolveImage?.(String(props.src || ""));
      if (!uri)
        return element(
          "span",
          { class: "md-image-note", role: "status" },
          `${alt}（图片未包含在当前资料中）`,
        );
      const image = element("img", {
        src: uri,
        alt,
        loading: "lazy",
        class: "md-image",
        referrerpolicy: "no-referrer",
      });
      if (props.title) image.title = String(props.title);
      const show = () => imageDialog(uri, alt, image.ownerDocument, image);
      image.tabIndex = 0;
      image.setAttribute("aria-haspopup", "dialog");
      image.addEventListener("click", show);
      image.addEventListener("keydown", (event) => {
        if (["Enter", " "].includes(event.key)) {
          event.preventDefault();
          show();
        }
      });
      image.addEventListener(
        "error",
        () =>
          image.replaceWith(
            element(
              "span",
              { class: "md-image-note" },
              `${alt}（图片未能打开）`,
            ),
          ),
        { once: true },
      );
      return image;
    }
    if (tag === "pre" && node.children?.[0]?.tagName === "code") {
      const code = node.children[0],
        language = String(
          code.properties?.className?.find((c) => c.startsWith("language-")) ||
            "",
        )
          .slice(9)
          .toLowerCase();
      const value =
        code.children
          ?.map((n) => n.value || "")
          .join("")
          .replace(/\n$/, "") || "";
      if (language === "mermaid") {
        if (options.deferDiagrams)
          return element(
            "figure",
            { class: "md-diagram md-diagram-deferred" },
            element(
              "figcaption",
              { class: "md-diagram-head" },
              "Mermaid 图表 · 回答结束后绘制",
            ),
            element("pre", {}, element("code", {}, value)),
          );
        return diagram(value);
      }
      if (["math", "latex", "tex"].includes(language)) return math(value, true);
      const result = element("code", {
        class: language ? "language-" + language : undefined,
      });
      if (language && lowlight.registered(language) && value.length < 100000) {
        try {
          const highlighted = lowlight.highlight(language, value);
          for (const child of highlighted.children)
            result.append(nodeToDom(child, true));
        } catch {
          result.textContent = value;
        }
      } else result.textContent = value;
      return element(
        "pre",
        {},
        language
          ? element("span", { class: "md-code-language" }, language)
          : null,
        result,
      );
    }
    if (!allowed.has(tag) || active.has(tag)) {
      const raw =
        node.position?.start?.offset !== undefined
          ? String(source).slice(
              node.position.start.offset,
              node.position.end.offset,
            )
          : "";
      return element(
        "code",
        { class: "md-unsupported-html" },
        raw || (node.children || []).map((n) => n.value || "").join(""),
      );
    }
    if (tag === "input") {
      if (props.type !== "checkbox") return document.createDocumentFragment();
      const input = element("input", {
        type: "checkbox",
        disabled: "",
        "aria-label": props.checked ? "已完成" : "未完成",
      });
      input.checked = !!props.checked;
      return input;
    }
    if (/^h[1-6]$/.test(tag) && options.title && tag === "h1") {
      const label = (node.children || []).map((n) => n.value || "").join("");
      if (label.trim() === options.title.trim())
        return document.createDocumentFragment();
    }
    const attributes = {};
    if (props.id && ids.has(String(props.id)))
      attributes.id = ids.get(String(props.id));
    if (props.title) attributes.title = String(props.title);
    if (tag === "ol" && Number.isInteger(props.start) && props.start > 0)
      attributes.start = props.start;
    if (tag === "li" && props.className?.includes("task-list-item"))
      attributes.class = "task-list-item";
    if (tag === "section" && props.dataFootnotes !== undefined)
      attributes.class = "footnotes";
    if (
      tag === "span" &&
      props.className?.every((c) => /^hljs-[a-z-]+$/.test(c))
    )
      attributes.class = props.className.join(" ");
    if (["th", "td"].includes(tag)) {
      for (const field of ["colSpan", "rowSpan"])
        if (
          Number.isInteger(props[field]) &&
          props[field] > 0 &&
          props[field] < 100
        )
          attributes[field.toLowerCase()] = props[field];
    }
    const result = element(
      tag === "h1" && options.title ? "h2" : tag,
      attributes,
    );
    if (
      ["th", "td"].includes(tag) &&
      ["left", "center", "right"].includes(props.align)
    )
      result.style.textAlign = props.align;
    if (tag === "details") result.open = !!props.open;
    if (tag === "a") {
      const href = String(props.href || "");
      if (href.startsWith("#") && ids.has(href.slice(1))) {
        result.href = "#" + ids.get(href.slice(1));
        result.addEventListener("click", (event) => {
          event.preventDefault();
          document
            .getElementById(ids.get(href.slice(1)))
            ?.scrollIntoView({ block: "center" });
        });
      } else if (options.link) {
        const target = options.link(href);
        if (target?.section) {
          result.href = "#" + new URLSearchParams({ section: target.section });
          result.addEventListener("click", (event) => {
            event.preventDefault();
            options.openSection?.(target.section);
          });
        } else if (target?.href) {
          result.href = target.href;
          result.target = "_blank";
          result.rel = "noopener noreferrer";
        }
      }
    }
    for (const child of renderChildren(
      node,
      plain || ["code", "pre", "a"].includes(tag),
    ))
      result.append(child);
    return tag === "table"
      ? element("div", { class: "md-table-scroll" }, result)
      : result;
  };
  if (options.sections?.length) {
    let current, currentId;
    const choices = options.sections;
    const groups = new Map();
    for (const node of tree.children) {
      if (node.properties?.dataFootnotes !== undefined) {
        root.append(nodeToDom(node));
        continue;
      }
      const line = node.position?.start?.line || 0;
      const section =
        [...choices]
          .reverse()
          .find((section) => (section.start_line || 1) <= line) || choices[0];
      if (currentId !== section.id) {
        currentId = section.id;
        current =
          groups.get(currentId) ||
          element("section", {
            id: "section-" + section.id,
            "data-section-id": section.id,
          });
        if (!groups.has(currentId)) {
          groups.set(currentId, current);
          root.append(current);
        }
      }
      current.append(nodeToDom(node));
    }
    for (const section of choices) {
      const block = [...root.children].find(
        (node) => node.dataset.sectionId === section.id,
      );
      if (block) options.sectionLinks?.(section, block);
    }
  } else for (const child of tree.children) root.append(nodeToDom(child));
  return root;
}

const referenceCache = new Map();
function imageReferences(source, referenceSources = []) {
  if (typeof referenceSources === "string")
    referenceSources = [referenceSources];
  const key = source + "\0" + referenceSources.join("\0");
  if (referenceCache.has(key)) return referenceCache.get(key);
  const tree = convert.runSync(
    parseWithReferences(String(source || ""), referenceSources),
  );
  const references = new Set();
  const visit = (node) => {
    if (node.tagName === "img" && typeof node.properties?.src === "string")
      references.add(node.properties.src);
    for (const child of node.children || []) visit(child);
  };
  visit(tree);
  referenceCache.set(key, references);
  if (referenceCache.size > 16)
    referenceCache.delete(referenceCache.keys().next().value);
  return references;
}
window.VksReaderMarkdown = {
  render,
  sanitizedSvg,
  imageReferences,
  showSelectionMenu,
};
