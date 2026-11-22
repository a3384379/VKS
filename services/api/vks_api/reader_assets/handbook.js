/* Content-free handbook shell. The host retains revision, rendering and AI ownership. */
(() => {
  "use strict";
  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  };
  const action = (label, callback, className, accessibleName) => {
    const node = element("button", className, label);
    node.type = "button";
    if (accessibleName) node.setAttribute("aria-label", accessibleName);
    node.addEventListener("click", callback);
    return node;
  };
  const countDocuments = (category) => {
    const ids = new Set();
    const visit = (nodes) => {
      for (const node of nodes || [])
        if (node.kind === "document") ids.add(node.id);
        else if (node.kind === "folder") visit(node.children);
    };
    visit(category?.children);
    return ids.size;
  };
  function mount(options) {
    const {
      root,
      project,
      documents: initialDocuments,
      navigation: initialNavigation,
      packageSelect,
      categories,
      docFilter,
      docList,
      docScroll,
      showBrand,
      getState,
      onOpen,
      onOverview,
      onSearch,
      onAskAI,
      onPrint,
    } = options;
    let documents = initialDocuments,
      documentMap = new Map(documents.map((doc) => [doc.id, doc]));
    const projectName = String(project?.name || "项目知识手册");
    let packList = Array.isArray(initialNavigation)
      ? initialNavigation
      : initialNavigation?.packages || [];
    const currentPackage = () =>
      packList.find((pack) => pack.id === getState().libraryPackage) ||
      packList[0];
    const currentCategory = () =>
      currentPackage()?.categories.find(
        (category) => category.id === getState().category,
      ) || currentPackage()?.categories[0];
    root.classList.add("vks-handbook", "hb-portable");
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", "知识手册阅读模板");
    root.dataset.templateId = "handbook";
    root.dataset.templateVersion = "1.0.0";

    const sidebar = element("aside", "hb-sidebar");
    sidebar.setAttribute("aria-label", "手册目录");
    if (showBrand) {
      const brand = action("", onOverview, "hb-brand", "手册总览");
      brand.append(element("span", "hb-brand-mark", [...projectName][0] || "知"));
      const brandTitle = element("span", "", projectName);
      brandTitle.append(element("small", "", "PROJECT KNOWLEDGE BASE"));
      brand.append(brandTitle);
      sidebar.append(brand);
    }
    const search = action(
      "⌕　搜索全文",
      onSearch,
      "hb-search-launch",
      "搜索全文",
    );
    search.append(element("kbd", "", "Ctrl K"));
    const sourcePicker = element("label", "hb-package-picker", "文档资料包");
    sourcePicker.append(packageSelect);
    const categoryName = element("div", "hb-category-name");
    const categoryTitle = element("strong");
    const categoryCount = element("span");
    categoryName.append(
      element("small", "", "当前模块"),
      categoryTitle,
      categoryCount,
    );
    const tocLabel = element("div", "hb-toc-label", "文章目录");
    docFilter.setAttribute("placeholder", "筛选当前分类…");
    docFilter.classList.add("hb-local-filter");
    const toc = element("nav", "hb-toc");
    toc.setAttribute("aria-label", "手册文章目录");
    toc.append(docFilter, docList);
    const sidebarFoot = element("div", "hb-sidebar-foot");
    sidebarFoot.append(
      element("span", "hb-status-dot"),
      element("span", "", "只读手册"),
      element("span", "", `v1.0.0 · ${documents.length} 篇`),
    );
    sidebar.append(
      search,
      sourcePicker,
      categoryName,
      tocLabel,
      toc,
      sidebarFoot,
    );

    const workspace = element("div", "hb-workspace");
    const topbar = element("header", "hb-topbar");
    const mobileToggle = action(
      "☰",
      () => setMobile(!root.classList.contains("hb-mobile-open")),
      "hb-mobile-toggle",
      "打开手册目录",
    );
    mobileToggle.setAttribute("aria-expanded", "false");
    const breadcrumb = element("div", "hb-breadcrumb");
    breadcrumb.setAttribute("aria-label", "当前位置");
    const actions = element("div", "hb-actions");
    actions.append(
      action("✧", onAskAI, "", "向手册 AI 提问"),
      action("总览", onOverview, "", "手册总览"),
      action("搜索", onSearch, "", "搜索手册"),
      action("打印 / PDF", onPrint || (() => window.print()), "", "打印 / PDF"),
    );
    topbar.append(mobileToggle, breadcrumb, actions);
    categories.classList.remove("document-categories");
    categories.classList.add("hb-categories");
    docScroll.classList.add("hb-main");
    workspace.append(topbar, categories, docScroll);
    const dismiss = action(
      "",
      () => setMobile(false),
      "hb-sidebar-dismiss",
      "关闭手册目录",
    );
    dismiss.hidden = true;
    root.replaceChildren(sidebar, dismiss, workspace);

    function setMobile(open) {
      root.classList.toggle("hb-mobile-open", open);
      dismiss.hidden = !open;
      mobileToggle.setAttribute("aria-expanded", String(open));
      mobileToggle.setAttribute(
        "aria-label",
        open ? "收起手册目录" : "打开手册目录",
      );
    }
    root.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && root.classList.contains("hb-mobile-open")) {
        setMobile(false);
        mobileToggle.focus();
      }
    });
    docList.addEventListener("click", (event) => {
      if (event.target.closest(".document-link,.chapter-link"))
        setMobile(false);
    });
    categories.addEventListener("click", () => setMobile(false));
    packageSelect.addEventListener("change", () => setMobile(false));

    function update() {
      const pack = currentPackage();
      const category = currentCategory();
      const doc = documentMap.get(getState().doc);
      sourcePicker.hidden = packList.length < 2;
      categoryTitle.textContent = category?.name || "项目文档";
      categoryCount.textContent = `${countDocuments(category)} 篇`;
      breadcrumb.textContent = [
        projectName,
        category?.name,
        doc?.title || "阅读总览",
      ]
        .filter(Boolean)
        .join(" / ");
      for (const tab of categories.children) {
        const source = pack?.categories.find(
          (item) => item.id === tab.dataset.categoryId,
        );
        if (!source) continue;
        tab.textContent = source.name;
        tab.append(element("span", "", countDocuments(source)));
      }
      const classes = {
        ".doc-articles": "hb-tree",
        ".folder-toggle": "hb-folder",
        ".doc-row": "hb-document-row",
        ".document-link": "hb-document-link",
        ".doc-chapters": "hb-headings",
        ".doc-expand,.chapter-expand": "hb-heading-toggle",
      };
      for (const [selector, className] of Object.entries(classes))
        for (const node of docList.querySelectorAll(selector))
          node.classList.add(className);
      for (const node of docList.querySelectorAll(".doc-entry"))
        node.classList.toggle(
          "hb-current",
          !!node.querySelector(
            ':scope > .doc-row .document-link[aria-current="page"]',
          ),
        );
    }
    function footer() {
      const node = element(
        "footer",
        "hb-book-footer",
        `${projectName} · ${documents.length} 篇原文`,
      );
      node.append(
        document.createElement("br"),
        element("span", "", "本地只读 · 知识手册模板 1.0.0"),
      );
      return node;
    }
    function renderCover() {
      setMobile(false);
      const cover = element("section", "hb-cover");
      const eyebrow = element("p", "hb-eyebrow");
      eyebrow.append(
        element("span"),
        document.createTextNode("PROJECT KNOWLEDGE BASE"),
      );
      const heading = element("h1", "", projectName);
      heading.append(element("em", "", "项目知识手册"));
      const stats = element("div", "hb-stats");
      const categoriesCount = packList.reduce(
        (total, pack) => total + pack.categories.length,
        0,
      );
      for (const [value, label] of [
        [documents.length, "篇完整文档"],
        [categoriesCount, "个专题分类"],
        [packList.length, "个资料包"],
      ]) {
        const stat = element("div");
        stat.append(element("strong", "", value), element("span", "", label));
        stats.append(stat);
      }
      const note = element("aside", "hb-reading-note");
      note.append(
        element("strong", "", "阅读方式"),
        element(
          "p",
          "",
          documents.length
            ? "顶部切换分类，在左侧找到文章，中间阅读所选文章。全文搜索支持跨文档查找；AI 提问使用你自己的模型连接。"
            : "这个手册尚未包含文档。",
        ),
      );
      cover.append(
        eyebrow,
        heading,
        element(
          "p",
          "hb-cover-lead",
          "选择上方分类，在左侧找到文章。\n一次专注一篇，随时在业务、架构与技术专题之间切换。",
        ),
        stats,
        note,
      );
      const firstId = currentCategory()?.documentIds?.[0] || documents[0]?.id;
      if (firstId)
        cover.append(
          action(
            "开始阅读　 →",
            () => onOpen(firstId),
            "hb-start-reading",
            "开始阅读",
          ),
        );
      docScroll.replaceChildren(cover, footer());
      docScroll.scrollTop = 0;
      update();
    }
    function renderArticle() {
      const article = docScroll.querySelector(":scope > article.document");
      if (!article) return;
      const doc = documentMap.get(getState().doc);
      const wrapper = element("div", "hb-article");
      article.before(
        element("p", "hb-article-path", doc?.path || doc?.title || ""),
        wrapper,
      );
      article.classList.add("document-reader");
      article.querySelector(".rich-markdown")?.classList.add("markdown");
      wrapper.append(article);
      docScroll
        .querySelector(":scope > .document-pagination")
        ?.classList.add("hb-pagination");
      docScroll.append(footer());
      update();
    }
    update();
    return Object.freeze({
      update,
      renderCover,
      renderArticle,
      setData(nextDocuments, nextNavigation) {
        documents = nextDocuments;
        documentMap = new Map(documents.map((doc) => [doc.id, doc]));
        packList = Array.isArray(nextNavigation)
          ? nextNavigation
          : nextNavigation?.packages || [];
        sidebarFoot.lastElementChild.textContent = `v1.0.0 · ${documents.length} 篇`;
      },
    });
  }
  window.VksHandbook = Object.freeze({ mount });
})();
