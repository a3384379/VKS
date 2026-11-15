/* Platform-owned local authoring. Saves reconstruct from an inert clean skeleton. */
(() => {
  "use strict";
  const SCHEMA = "vks-handbook-authoring-1",
    MiB = 1024 * 1024;
  const encoder = new TextEncoder();
  const mime = {
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    gif: "image/gif",
    webp: "image/webp",
  };
  const element = (tag, text = "", className = "") => {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, click, label) => {
    const node = element("button", text);
    node.type = "button";
    if (label) node.setAttribute("aria-label", label);
    node.addEventListener("click", click);
    return node;
  };
  const b64bytes = (value) =>
    Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
  const bytes64 = (bytes) => {
    let text = "";
    for (let i = 0; i < bytes.length; i += 32768)
      text += String.fromCharCode(...bytes.subarray(i, i + 32768));
    return btoa(text);
  };
  async function compressed(value) {
    return bytes64(
      new Uint8Array(
        await new Response(
          new Blob([encoder.encode(value)])
            .stream()
            .pipeThrough(new CompressionStream("gzip")),
        ).arrayBuffer(),
      ),
    );
  }
  async function expanded(value) {
    return new TextDecoder().decode(
      await new Response(
        new Blob([b64bytes(value)])
          .stream()
          .pipeThrough(new DecompressionStream("gzip")),
      ).arrayBuffer(),
    );
  }
  const caseKey = (value) => value.toLowerCase().toUpperCase().toLowerCase();
  function path(value) {
    if (
      typeof value !== "string" ||
      !value ||
      value.length > 1000 ||
      value !== value.normalize("NFC") ||
      value.startsWith("/") ||
      /^[a-z]:/i.test(value) ||
      /[\\?#%\u0000-\u001f\u007f]/.test(value) ||
      value
        .split("/")
        .some(
          (s) =>
            !s ||
            s === "." ||
            s === ".." ||
            [".git", "node_modules", ".env", ".env.local", "id_rsa"].includes(
              caseKey(s),
            ),
        )
    )
      throw new Error(
        "文件路径需为普通相对路径，不能包含 ../、反斜杠、?、# 或 %。",
      );
    return value;
  }
  function raster(bytes, type) {
    return type === "image/png"
      ? bytes.length > 8 &&
          [137, 80, 78, 71, 13, 10, 26, 10].every((v, i) => bytes[i] === v)
      : type === "image/jpeg"
        ? bytes.length > 3 &&
          bytes[0] === 255 &&
          bytes[1] === 216 &&
          bytes[2] === 255
        : type === "image/gif"
          ? /^GIF8[79]a/.test(new TextDecoder().decode(bytes.subarray(0, 6)))
          : type === "image/webp" &&
            new TextDecoder().decode(bytes.subarray(0, 4)) === "RIFF" &&
            new TextDecoder().decode(bytes.subarray(8, 12)) === "WEBP";
  }
  function validate(value) {
    if (
      !value ||
      value.schema !== SCHEMA ||
      value.template_id !== "handbook" ||
      value.template_version !== "1.0.0" ||
      Object.keys(value).some(
        (k) =>
          ![
            "schema",
            "template_id",
            "template_version",
            "documents",
            "images",
          ].includes(k),
      ) ||
      !Array.isArray(value.documents) ||
      !Array.isArray(value.images) ||
      value.documents.length > 2000 ||
      value.documents.length + value.images.length > 5000
    )
      throw new Error("模板文档格式或数量不受支持。");
    let total = 0;
    const used = new Set();
    for (const doc of value.documents) {
      path(doc.path);
      if (
        Object.keys(doc).some(
          (k) => !["path", "name", "markdown"].includes(k),
        ) ||
        typeof doc.markdown !== "string" ||
        (doc.name !== undefined &&
          (typeof doc.name !== "string" ||
            !doc.name.trim() ||
            doc.name.length > 160 ||
            /[\u0000-\u001f\u007f]/.test(doc.name))) ||
        used.has(caseKey(doc.path)) ||
        !/(\.md|\.markdown|\.txt)$/i.test(doc.path)
      )
        throw new Error("文档路径、名称或内容不正确，或出现重复路径。");
      used.add(caseKey(doc.path));
      const size = encoder.encode(doc.markdown).length;
      if (size > 2 * MiB) throw new Error("单篇文档不能超过 2 MiB。");
      total += size;
    }
    for (const image of value.images) {
      path(image.path);
      if (
        Object.keys(image).some(
          (k) => !["path", "mime", "base64"].includes(k),
        ) ||
        !Object.values(mime).includes(image.mime) ||
        typeof image.base64 !== "string" ||
        !/^[A-Za-z0-9+/]*={0,2}$/.test(image.base64) ||
        used.has(caseKey(image.path)) ||
        mime[image.path.split(".").at(-1).toLowerCase()] !== image.mime
      )
        throw new Error("图片格式或路径不正确，或出现重复路径。");
      used.add(caseKey(image.path));
      const bytes = b64bytes(image.base64);
      if (bytes.length > 12 * MiB || !raster(bytes, image.mime))
        throw new Error(
          "图片需为有效 PNG、JPEG、GIF 或 WebP，单张不超过 12 MiB。",
        );
      total += bytes.length;
    }
    if (total > 64 * MiB) throw new Error("文档与图片合计不能超过 64 MiB。");
    return value;
  }
  const verifiedImages = new Map();
  async function validateImages(value) {
    const currentPaths = new Set(value.images.map((image) => image.path));
    for (const key of verifiedImages.keys())
      if (!currentPaths.has(key)) verifiedImages.delete(key);
    for (const image of value.images) {
      if (verifiedImages.get(image.path) === image.base64) continue;
      const url = URL.createObjectURL(
        new Blob([b64bytes(image.base64)], { type: image.mime }),
      );
      const picture = new Image();
      try {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => {
            picture.onload = picture.onerror = null;
            reject(new Error("图片读取超时"));
          }, 10000);
          picture.onload = () => {
            clearTimeout(timer);
            resolve();
          };
          picture.onerror = () => {
            clearTimeout(timer);
            reject(new Error("图片无法解码"));
          };
          picture.src = url;
        });
        if (
          !picture.naturalWidth ||
          !picture.naturalHeight ||
          picture.naturalWidth * picture.naturalHeight > 16777216
        )
          throw new Error("图片超过 1677 万像素上限");
        await picture.decode();
        verifiedImages.set(image.path, image.base64);
      } catch (error) {
        throw new Error(
          `${image.path}：${error.message}。请使用有效 PNG、JPEG、GIF 或 WebP 图片。`,
        );
      } finally {
        picture.onload = picture.onerror = null;
        picture.src = "";
        URL.revokeObjectURL(url);
      }
    }
  }
  let book;
  try {
    book = validate(
      JSON.parse(document.getElementById("vks-template-data").textContent),
    );
  } catch (error) {
    const warning = element("p", error.message);
    warning.setAttribute("role", "alert");
    document.body.prepend(warning);
    return;
  }
  const source = document
    .getElementById("vks-authoring-source")
    .textContent.trim();
  const runtime = document.getElementById("vks-authoring-runtime").textContent;
  const css = document.getElementById("vks-authoring-style").textContent;
  let busy = false,
    dirty = false;
  let readerReady;
  const ready = new Promise((resolve, reject) => {
    let tries = 0;
    const check = () => {
      if (window.VksHandbookAuthoring) {
        readerReady = window.VksHandbookAuthoring;
        resolve(readerReady);
      } else if (++tries > 150)
        reject(new Error("阅读框架未能启动，请重新打开模板。"));
      else setTimeout(check, 100);
    };
    check();
  });
  ready.catch((error) => notice(error.message, true));
  document.body.classList.add("vks-authoring");
  const tools = element("section", "", "vks-authoring-tools");
  tools.setAttribute("aria-label", "本地手册编辑工具");
  const title = element("strong", "可编辑知识手册");
  const status = element(
    "p",
    "导入普通 MD 与 PNG、JPEG、GIF、WebP；目录选择会保留内部层级。保存为 HTML 后，可继续编辑或导回平台自动建库。",
  );
  status.setAttribute("role", "status");
  const files = element("input"),
    directory = element("input");
  for (const input of [files, directory]) {
    input.type = "file";
    input.multiple = true;
    input.accept = ".md,.markdown,.txt,.png,.jpg,.jpeg,.gif,.webp";
  }
  files.setAttribute("aria-label", "选择 Markdown 和图片文件");
  directory.setAttribute("aria-label", "选择文档目录");
  directory.setAttribute("webkitdirectory", "");
  const importFiles = button("导入 / 更新文件", () => files.click());
  const importDirectory = button("导入目录", () => directory.click());
  const edit = button("整理文档", () => void operation(openEditor));
  const save = button("保存可编辑 HTML", () => void operation(saveFile));
  tools.append(
    title,
    importFiles,
    importDirectory,
    edit,
    save,
    files,
    directory,
    status,
  );
  document.body.prepend(tools);
  function notice(message, error = false) {
    status.textContent = message;
    status.setAttribute("role", error ? "alert" : "status");
  }
  async function operation(callback) {
    if (busy) return;
    busy = true;
    for (const b of tools.querySelectorAll("button")) b.disabled = true;
    try {
      await callback();
    } catch (error) {
      notice(error.message || String(error), true);
    } finally {
      busy = false;
      for (const b of tools.querySelectorAll("button")) b.disabled = false;
    }
  }
  function label(markdown, fallback) {
    const heading = /^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$/m.exec(markdown)?.[1];
    return (heading || fallback)
      .replace(/[`*_~]/g, "")
      .replace(/\s+/g, " ")
      .slice(0, 160);
  }
  async function docId(value) {
    return (
      "d-" +
      [
        ...new Uint8Array(
          await crypto.subtle.digest("SHA-256", encoder.encode(value)),
        ),
      ]
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("")
    );
  }
  function chapters(markdown, id, title) {
    const lines = markdown.split("\n"),
      marks = [];
    let fence = null;
    for (let i = 0; i < lines.length; i++) {
      const code = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(lines[i]);
      if (code) {
        if (!fence) fence = { character: code[1][0], length: code[1].length };
        else if (
          code[1][0] === fence.character &&
          code[1].length >= fence.length &&
          !code[2].trim()
        )
          fence = null;
        continue;
      }
      if (fence) continue;
      const h = /^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$/.exec(lines[i]);
      if (h)
        marks.push({
          line: i,
          title: label(lines[i], title),
          depth: h[1].length,
        });
      else if (
        i > 0 &&
        lines[i - 1].trim() &&
        /^ {0,3}(=+|-+)\s*$/.test(lines[i]) &&
        !marks.some((m) => m.line === i - 1)
      )
        marks.push({
          line: i - 1,
          title: lines[i - 1].trim(),
          depth: lines[i].trim()[0] === "=" ? 1 : 2,
        });
    }
    if (!marks.length || marks[0].line > 0)
      marks.unshift({
        line: 0,
        title: marks.length ? "正文" : title,
        depth: 0,
      });
    return marks.map((mark, index) => ({
      id: id + "-s" + index,
      title: mark.title,
      heading_level: mark.depth,
      start_line: mark.line + 1,
      content: lines
        .slice(mark.line, marks[index + 1]?.line ?? lines.length)
        .join("\n"),
    }));
  }
  function imagePath(reference, documentPath) {
    const value = decodeURIComponent(reference).normalize("NFC");
    if (
      value.startsWith("/") ||
      /^[a-z][a-z0-9+.-]*:/i.test(value) ||
      /[\\?#\u0000-\u001f\u007f]/.test(value)
    )
      return null;
    const parts = documentPath.split("/").slice(0, -1);
    for (const part of value.split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") {
        if (!parts.length) return null;
        parts.pop();
      } else parts.push(part);
    }
    return parts.join("/");
  }
  async function payload(envelope) {
    const imageMap = new Map(
      envelope.images.map((image) => [
        image.path,
        `data:${image.mime};base64,${image.base64}`,
      ]),
    );
    const documents = await Promise.all(
      envelope.documents.map(async (source) => {
        const id = await docId(source.path),
          title =
            source.name ||
            label(
              source.markdown,
              source.path
                .split("/")
                .at(-1)
                .replace(/\.[^.]+$/, ""),
            );
        const images = {};
        for (const reference of window.VksReaderMarkdown.imageReferences(
          source.markdown,
        )) {
          try {
            const absolute = imagePath(reference, source.path);
            if (imageMap.has(absolute))
              images[reference] = imageMap.get(absolute);
          } catch {}
        }
        return {
          id,
          title,
          path: source.path,
          namespace: "authoring",
          markdown: source.markdown,
          sections: chapters(source.markdown, id, title),
          images,
        };
      }),
    );
    const categories = new Map();
    for (const doc of documents) {
      const parts = doc.path.split("/"),
        categoryKey = parts.length > 1 ? parts.shift() : "",
        categoryId = categoryKey || "root";
      if (!categories.has(categoryId))
        categories.set(categoryId, {
          id: categoryId,
          name: categoryKey || "阅读指南",
          path: categoryKey,
          children: [],
        });
      let children = categories.get(categoryId).children,
        folder = categoryKey;
      for (const segment of parts.slice(0, -1)) {
        folder = folder ? folder + "/" + segment : segment;
        let existing = children.find(
          (node) => node.kind === "folder" && node.path === folder,
        );
        if (!existing) {
          existing = {
            kind: "folder",
            id: "folder-" + folder,
            name: segment,
            path: folder,
            children: [],
          };
          children.push(existing);
        }
        children = existing.children;
      }
      children.push({
        kind: "document",
        id: doc.id,
        name: doc.title,
        path: doc.path,
      });
    }
    return {
      schema_version: 1,
      authoring: SCHEMA,
      reader_mode: "handbook",
      project: { id: "vks-handbook-template", name: "本地知识手册" },
      snapshot: { id: "authoring", name: "可编辑模板" },
      prototypes: [],
      documents,
      navigation: {
        packages: documents.length
          ? [
              {
                id: "authoring",
                name: "本地文档",
                categories: [...categories.values()],
              },
            ]
          : [],
      },
      bindings: [],
      native_documents: [],
      rag: { chunks: [] },
      warnings: [],
      ai: {
        base_url: "https://api.openai.com/v1",
        protocol: "responses",
        model: "",
      },
    };
  }
  async function apply(next, selectedPath) {
    validate(next);
    await validateImages(next);
    const nextPayload = await payload(next);
    const reader = await ready;
    reader.replace(nextPayload);
    book = next;
    dirty = true;
    document.getElementById("vks-template-data").textContent = JSON.stringify(
      book,
    ).replace(/</g, "\\u003c");
    if (selectedPath) {
      const doc = nextPayload.documents.find(
        (doc) => doc.path === selectedPath,
      );
      if (doc) reader.open(doc.id);
    }
    title.textContent = `可编辑知识手册 · ${book.documents.length} 篇 · 有未保存更改`;
    notice(
      `已在本页更新 ${book.documents.length} 篇文档与 ${book.images.length} 张图片。请保存 HTML，原文件不会被自动覆盖。`,
    );
  }
  async function readFiles(selected, folder) {
    if (!selected.length) return;
    const documents = new Map(
        book.documents.map((doc) => [caseKey(doc.path), doc]),
      ),
      images = new Map(
        book.images.map((image) => [caseKey(image.path), image]),
      );
    let skipped = 0,
      read = 0;
    for (const file of selected) {
      const extension = file.name.split(".").at(-1).toLowerCase();
      if (
        !["md", "markdown", "txt", ...Object.keys(mime)].includes(extension)
      ) {
        skipped++;
        continue;
      }
      let filePath =
        folder && file.webkitRelativePath
          ? file.webkitRelativePath.split("/").slice(1).join("/")
          : file.name;
      filePath = path(filePath.normalize("NFC"));
      notice(`正在读取 ${++read} 个文件：${filePath}`);
      if (["md", "markdown", "txt"].includes(extension)) {
        if (file.size > 2 * MiB) throw new Error("单篇文档不能超过 2 MiB。");
        let markdown;
        try {
          markdown = new TextDecoder("utf-8", { fatal: true }).decode(
            await file.arrayBuffer(),
          );
        } catch {
          throw new Error(
            `${filePath} 不是有效 UTF-8，请转换为 UTF-8 后再导入。`,
          );
        }
        documents.set(caseKey(filePath), {
          path: filePath,
          markdown,
        });
        images.delete(caseKey(filePath));
      } else {
        if (file.size > 12 * MiB) throw new Error("单张图片不能超过 12 MiB。");
        const bytes = new Uint8Array(await file.arrayBuffer());
        if (!raster(bytes, mime[extension]))
          throw new Error(`图片内容与扩展名不一致：${filePath}`);
        images.set(caseKey(filePath), {
          path: filePath,
          mime: mime[extension],
          base64: bytes64(bytes),
        });
        documents.delete(caseKey(filePath));
      }
    }
    if (!read)
      throw new Error(
        "请选择 MD、Markdown、TXT 或 PNG、JPEG、GIF、WebP 文件。",
      );
    await apply({
      ...book,
      documents: [...documents.values()],
      images: [...images.values()],
    });
    if (skipped)
      notice(
        `已导入 / 更新 ${read} 个文件；跳过 ${skipped} 个非文档或图片文件。请保存 HTML。`,
      );
  }
  files.addEventListener("change", () => {
    const selected = [...files.files];
    files.value = "";
    void operation(() => readFiles(selected, false));
  });
  directory.addEventListener("change", () => {
    const selected = [...directory.files];
    directory.value = "";
    void operation(() => readFiles(selected, true));
  });
  async function openEditor() {
    await ready;
    const currentId = readerReady.currentDocument();
    let selectedPath = "";
    if (currentId)
      for (const doc of book.documents)
        if ((await docId(doc.path)) === currentId) {
          selectedPath = doc.path;
          break;
        }
    const dialog = element("dialog", "", "vks-authoring-editor");
    dialog.setAttribute("aria-label", "整理本地手册文档");
    const header = element("header");
    header.append(
      element("h2", "整理文档"),
      button("关闭", () => dialog.close()),
    );
    const layout = element("div", "", "authoring-layout"),
      aside = element("aside"),
      main = element("div"),
      list = element("select");
    list.size = 12;
    list.setAttribute("aria-label", "选择要编辑的文档");
    const filePath = element("input"),
      name = element("input"),
      markdown = element("textarea"),
      error = element("p");
    filePath.setAttribute("aria-label", "文档相对路径");
    name.setAttribute("aria-label", "文档显示名称");
    markdown.setAttribute("aria-label", "Markdown 原文");
    error.setAttribute("role", "alert");
    const field = (text, node) => {
      const label = element("label", text);
      label.append(node);
      return label;
    };
    let editing = "";
    const choose = (value) => {
      const doc = book.documents.find((doc) => doc.path === value);
      editing = doc?.path || "";
      filePath.value = doc?.path || "新文档.md";
      name.value = doc?.name || "";
      markdown.value = doc?.markdown || "";
      list.value = editing;
      error.textContent = "";
    };
    const renderList = () => {
      list.replaceChildren(
        ...book.documents.map((doc) => {
          const option = element("option", doc.path);
          option.value = doc.path;
          return option;
        }),
      );
    };
    list.addEventListener("change", () => choose(list.value));
    const create = button("新建 MD", () => {
      choose("");
      let n = 1;
      while (book.documents.some((doc) => doc.path === filePath.value))
        filePath.value = `新文档-${n++}.md`;
      filePath.focus();
    });
    aside.append(create, list);
    main.append(
      field("相对路径（移动文件后需同步调整文中相对引用）", filePath),
      field("显示名称（可选，默认读取 Markdown 标题）", name),
      field("Markdown 原文", markdown),
      error,
    );
    layout.append(aside, main);
    const footer = element("footer");
    footer.append(
      element(
        "small",
        "修改先保存在本页；保存 HTML 后才能长期保留或导回平台。",
      ),
      button(
        "删除文档",
        () =>
          void operation(async () => {
            if (!editing) return;
            if (!confirm(`删除文档 ${editing}？`)) return;
            await apply({
              ...book,
              documents: book.documents.filter((doc) => doc.path !== editing),
            });
            renderList();
            choose(book.documents[0]?.path || "");
          }),
      ),
      button(
        "应用修改",
        () =>
          void operation(async () => {
            try {
              const nextPath = path(filePath.value.trim().normalize("NFC"));
              if (!/\.(md|markdown|txt)$/i.test(nextPath))
                throw new Error("文档路径应以 .md、.markdown 或 .txt 结尾。");
              if (
                nextPath !== editing &&
                book.documents.some((doc) => doc.path === nextPath) &&
                !confirm("同路径已有文档，是否替换？")
              )
                return;
              const doc = {
                path: nextPath,
                markdown: markdown.value,
                ...(name.value.trim() ? { name: name.value.trim() } : {}),
              };
              await apply(
                {
                  ...book,
                  documents: [
                    ...book.documents.filter(
                      (item) => item.path !== editing && item.path !== nextPath,
                    ),
                    doc,
                  ],
                },
                nextPath,
              );
              renderList();
              choose(nextPath);
              dialog.close();
            } catch (e) {
              error.textContent = e.message;
            }
          }),
      ),
    );
    const assets = element("details", "", "authoring-assets"),
      assetList = element("ul");
    assets.append(
      element("summary", `图片资源 · ${book.images.length}`),
      assetList,
    );
    for (const image of book.images) {
      const row = element("li"),
        remove = button(
          "删除",
          () =>
            void operation(async () => {
              if (
                !confirm(
                  `删除图片 ${image.path}？引用这张图的文档需要同步更新。`,
                )
              )
                return;
              await apply({
                ...book,
                images: book.images.filter((item) => item.path !== image.path),
              });
              row.remove();
            }),
          "删除图片 " + image.path,
        );
      row.append(element("span", image.path), remove);
      assetList.append(row);
    }
    dialog.append(header, layout, footer, assets);
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    renderList();
    choose(selectedPath || book.documents[0]?.path || "");
    dialog.showModal();
  }
  async function saveFile() {
    validate(book);
    await validateImages(book);
    notice("正在打包可继续编辑的 HTML…");
    const base = await expanded(source),
      doc = new DOMParser().parseFromString(base, "text/html");
    doc.getElementById("vks-payload").textContent = await compressed(
      JSON.stringify(await payload(book)),
    );
    const style = doc.createElement("style");
    style.id = "vks-authoring-style";
    style.textContent = css.replace(/<\/style/gi, "<\\/style");
    doc.head.append(style);
    for (const [id, type, value] of [
      [
        "vks-template-data",
        "application/json",
        JSON.stringify(book).replace(/</g, "\\u003c"),
      ],
      ["vks-authoring-source", "application/octet-stream", source],
      [
        "vks-authoring-runtime",
        "",
        runtime.replace(/<\/script/gi, "<\\/script"),
      ],
    ]) {
      const script = doc.createElement("script");
      script.id = id;
      if (type) script.type = type;
      script.textContent = value;
      doc.body.append(script);
    }
    const html = "<!doctype html>\n" + doc.documentElement.outerHTML;
    if (encoder.encode(html).length > 80 * MiB)
      throw new Error("最终 HTML 超过 80 MiB，请拆分文档后保存。");
    const url = URL.createObjectURL(
        new Blob([html], { type: "text/html;charset=utf-8" }),
      ),
      link = element("a");
    link.href = url;
    link.download = "知识手册-可继续编辑.html";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    dirty = false;
    title.textContent = `可编辑知识手册 · ${book.documents.length} 篇`;
    notice(
      "已生成可继续编辑的 HTML。你可以重新打开它，或在平台资料管理中导入以自动建库和关联。",
    );
  }
  window.addEventListener("beforeunload", (event) => {
    if (dirty) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  ready.then(() => {
    const pill = document.querySelector(".brand .pill");
    if (pill) pill.textContent = "本地可编辑模板";
    title.textContent = `可编辑知识手册 · ${book.documents.length} 篇`;
  });
})();
