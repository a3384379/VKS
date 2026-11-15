(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const sidebar = $('sidebar');
  const shade = $('sidebar-shade');
  const toggleMenu = open => {
    sidebar.classList.toggle('open', open);
    shade.hidden = !open;
    $('mobile-toggle').setAttribute('aria-expanded', String(open));
  };
  $('mobile-toggle').addEventListener('click', () => toggleMenu(!sidebar.classList.contains('open')));
  shade.addEventListener('click', () => toggleMenu(false));
  $('back-to-top').addEventListener('click', () => window.scrollTo({top:0, behavior:'smooth'}));
  $('print-book').addEventListener('click', () => window.print());
  $('collapse-toc').addEventListener('click', () => {
    document.querySelectorAll('.toc-group:not([hidden]) .toc-headings').forEach(list => list.hidden = true);
    document.querySelectorAll('.toc-group:not([hidden]) .heading-toggle').forEach(button => button.setAttribute('aria-expanded', 'false'));
  });
  document.querySelectorAll('.heading-toggle').forEach(button => {
    button.addEventListener('click', () => {
      const list = button.closest('.toc-doc').querySelector('.toc-headings');
      list.hidden = !list.hidden;
      button.setAttribute('aria-expanded', String(!list.hidden));
    });
  });
  document.querySelectorAll('.prose table').forEach(table => {
    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll';
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', '表格，可横向滚动');
    table.replaceWith(wrapper);
    wrapper.append(table);
  });
  const chapters = [...document.querySelectorAll('.chapter')];
  const articles = [...document.querySelectorAll('article.document')];
  const tocGroups = [...document.querySelectorAll('.toc-group')];
  const moduleTabs = [...document.querySelectorAll('.module-tab')];
  const lastDocument = new Map();
  const scrollPositions = new Map();
  let activeDoc = null, navigationVersion = 0;
  const moduleHeight = new ResizeObserver(() => {
    const top = document.querySelector('.topbar').offsetHeight + $('module-bar').offsetHeight + 20;
    document.documentElement.style.setProperty('--reading-offset', top + 'px');
  });
  moduleHeight.observe($('module-bar'));

  function showDocument(article, chapter) {
    if (activeDoc && activeDoc !== article?.id) scrollPositions.set(activeDoc, window.scrollY);
    activeDoc = article?.id || null;
    window.fhBookPages.activate(activeDoc);
    $('book-start').hidden = Boolean(article);
    chapters.forEach(group => group.hidden = group !== chapter || !article);
    articles.forEach(doc => doc.hidden = doc !== article);
    tocGroups.forEach(group => group.hidden = group.dataset.group !== chapter.id);
    moduleTabs.forEach(tab => {
      const selected = tab.dataset.group === chapter.id;
      tab.classList.toggle('active', selected);
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    $('sidebar-module-name').textContent = chapter.dataset.name;
    const categoryDocs = [...chapter.querySelectorAll('article.document')];
    $('sidebar-module-count').textContent = categoryDocs.length + ' 篇';
    document.querySelectorAll('.toc-doc').forEach(item => {
      item.classList.toggle('active', item.dataset.doc === activeDoc);
      if (item.dataset.doc !== activeDoc) {
        item.querySelector('.toc-headings').hidden = true;
        item.querySelector('.heading-toggle').setAttribute('aria-expanded', 'false');
      }
    });
    if (article) {
      lastDocument.set(chapter.id, article.id);
      $('current-title').textContent = chapter.dataset.name + ' / ' + article.dataset.title;
      const index = categoryDocs.indexOf(article);
      for (const [id, target] of [['previous-doc', categoryDocs[index - 1]], ['next-doc', categoryDocs[index + 1]]]) {
        const link = $(id);
        link.hidden = !target;
        if (target) {
          link.href = '#' + target.id;
          link.querySelector('strong').textContent = target.dataset.title;
        }
      }
      $('document-pagination').hidden = categoryDocs.length < 2;
      const item = document.querySelector(`.toc-doc[data-doc="${article.id}"]`);
      for (let parent = item?.parentElement; parent; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS') parent.open = true;
      }
      item?.scrollIntoView({block:'nearest'});
    } else {
      $('current-title').textContent = '阅读总览';
      $('document-pagination').hidden = true;
    }
  }

  function goToHash() {
    let target;
    try { window.fhBookPages.ensure(decodeURIComponent(location.hash.slice(1))); } catch {}
    try { target = document.getElementById(decodeURIComponent(location.hash.slice(1))); } catch (_) { target = null; }
    if (!target) target = $('book-start');
    const version = ++navigationVersion;
    let article = target.closest('article.document');
    let chapter = target.closest('.chapter') || chapters[0];
    let restoreScroll = false;
    if (target.classList.contains('chapter')) {
      article = document.getElementById(lastDocument.get(chapter.id)) || chapter.querySelector('article.document');
      restoreScroll = true;
      // Store the concrete article in history so refresh/back restores the same page.
      if (article) history.replaceState(null, '', '#' + article.id); else target = $('book-start');
    }
    showDocument(article, chapter);
    for (let parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
    requestAnimationFrame(() => {
      if (version !== navigationVersion) return;
      if (!article || target === article || restoreScroll) {
        window.scrollTo(0, restoreScroll ? (scrollPositions.get(article.id) || 0) : 0);
      } else target.scrollIntoView({block:'start'});
    });
  }
  document.addEventListener('click', event => {
    const anchor = event.target.closest('a[href^="#"]');
    if (anchor && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.button === 0) {
      event.preventDefault();
      toggleMenu(false);
      const hash = new URL(anchor.href).hash;
      if (hash === location.hash) goToHash();
      else location.hash = hash;
    }
  });
  moduleTabs.forEach((tab, index) => tab.addEventListener('keydown', event => {
    const steps = {ArrowLeft: -1, ArrowRight: 1};
    let next = index;
    if (event.key in steps) next = (index + steps[event.key] + moduleTabs.length) % moduleTabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = moduleTabs.length - 1;
    else return;
    event.preventDefault(); moduleTabs[next].focus(); moduleTabs[next].click();
  }));
  window.addEventListener('hashchange', goToHash);
  goToHash();

  const searchDialog = $('search-dialog');
  let searchData;
  const openSearch = () => {
    if (!searchData) searchData = JSON.parse($('search-data').textContent).map(doc => ({...doc,
      lowerTitle: doc.title.toLocaleLowerCase(),
      chunks: doc.chunks.map(chunk => ({...chunk, lower: (chunk.title + '\n' + chunk.text).toLocaleLowerCase()}))}));
    if (!searchDialog.open) searchDialog.showModal();
    $('search-input').focus();
  };
  $('search-launch').addEventListener('click', openSearch);
  $('top-search').addEventListener('click', openSearch);
  document.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', () => $(button.dataset.close).close()));
  for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click', event => {if (event.target === dialog) dialog.close();});
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault(); openSearch();
    }
    if (event.key === 'Escape') toggleMenu(false);
  });
  function marked(text, terms) {
    const fragment = document.createDocumentFragment();
    const lower = text.toLocaleLowerCase();
    let position = 0;
    while (position < text.length) {
      let at = -1, length = 0;
      for (const term of terms) {
        const index = lower.indexOf(term, position);
        if (index >= 0 && (at < 0 || index < at)) { at = index; length = term.length; }
      }
      if (at < 0) { fragment.append(document.createTextNode(text.slice(position))); break; }
      fragment.append(document.createTextNode(text.slice(position, at)));
      const mark = document.createElement('mark'); mark.textContent = text.slice(at, at + length); fragment.append(mark);
      position = at + length;
    }
    return fragment;
  }
  let searchTimer;
  $('search-input').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = window.setTimeout(runSearch, 120);
  });
  function runSearch() {
    const query = $('search-input').value.trim();
    const terms = [...new Set(query.toLocaleLowerCase().split(/\s+/).filter(Boolean))];
    const results = $('search-results'); results.replaceChildren();
    if (!terms.length) { $('search-status').textContent = `搜索 ${searchData.length} 篇文档的完整内容。多个关键词用空格分隔。`; return; }
    const matches = [];
    for (const doc of searchData) {
      const titleHit = terms.every(term => doc.lowerTitle.includes(term));
      const chunks = doc.chunks.filter(chunk => terms.every(term => chunk.lower.includes(term)));
      if (titleHit || chunks.length) matches.push({doc, chunks, score: (titleHit ? 100 : 0) + Math.min(chunks.length, 30)});
    }
    matches.sort((a, b) => b.score - a.score);
    const count = matches.length;
    $('search-status').textContent = count ? `找到 ${count} 篇相关文档${count > 60 ? '，显示前 60 篇' : ''}。点击结果定位到匹配小节。` : '未找到匹配内容，请尝试其他关键词。';
    for (const {doc, chunks} of matches.slice(0, 60)) {
      const chunk = chunks[0];
      const li = document.createElement('li'); const link = document.createElement('a'); link.className = 'search-result';
      link.href = '#' + (chunk?.id || doc.id);
      const path = document.createElement('div'); path.className = 'result-path'; path.textContent = doc.group + ' / ' + (chunk?.title || doc.title);
      const heading = document.createElement('strong'); heading.append(marked(doc.title, terms));
      const snippet = document.createElement('p');
      const text = (chunk?.text || doc.title).replace(/\s+/g, ' ');
      const first = Math.min(...terms.map(term => text.toLocaleLowerCase().indexOf(term)).filter(i => i >= 0));
      const start = Number.isFinite(first) ? Math.max(0, first - 45) : 0;
      snippet.append(marked((start ? '…' : '') + text.slice(start, start + 190) + (text.length > start + 190 ? '…' : ''), terms));
      link.append(path, heading, snippet);
      link.addEventListener('click', () => searchDialog.close());
      li.append(link); results.append(li);
    }
  }

  const diagramDialog = $('diagram-dialog');
  const diagramViewport = $('diagram-viewport');
  let dragState = null;
  let scale = 1, naturalWidth = 1000, naturalHeight = 800;
  function resizeDiagram() {
    const svg = $('diagram-zoom-content').querySelector('svg');
    if (!svg) return;
    svg.style.width = Math.round(naturalWidth * scale) + 'px';
    svg.style.height = Math.round(naturalHeight * scale) + 'px';
    svg.setAttribute('width', String(Math.round(naturalWidth * scale)));
    svg.setAttribute('height', String(Math.round(naturalHeight * scale)));
    $('zoom-reset').textContent = Math.round(scale * 100) + '%';
  }
  function zoomDiagram(nextScale, clientX, clientY) {
    const svg = $('diagram-zoom-content').querySelector('svg');
    if (!svg) return;
    const next = Math.max(.2, Math.min(4, nextScale));
    if (Math.abs(next - scale) < .000001) return;
    const viewportRect = diagramViewport.getBoundingClientRect();
    clientX ??= viewportRect.left + diagramViewport.clientWidth / 2;
    clientY ??= viewportRect.top + diagramViewport.clientHeight / 2;
    const before = svg.getBoundingClientRect();
    const anchorX = (clientX - before.left) / before.width;
    const anchorY = (clientY - before.top) / before.height;
    scale = next;
    resizeDiagram();
    const after = svg.getBoundingClientRect();
    // Keep the same point of the image under the cursor as its dimensions change.
    diagramViewport.scrollLeft += after.left + anchorX * after.width - clientX;
    diagramViewport.scrollTop += after.top + anchorY * after.height - clientY;
    if (dragState) {
      Object.assign(dragState, {x:clientX, y:clientY, left:diagramViewport.scrollLeft, top:diagramViewport.scrollTop});
    }
  }
  function endDiagramDrag() {
    const pointerId = dragState?.pointerId;
    dragState = null;
    diagramViewport.classList.remove('is-dragging');
    if (pointerId !== undefined && diagramViewport.hasPointerCapture(pointerId)) {
      diagramViewport.releasePointerCapture(pointerId);
    }
  }
  diagramViewport.addEventListener('wheel', event => {
    if (!diagramDialog.open) return;
    event.preventDefault();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? diagramViewport.clientHeight : 1);
    const factor = Math.exp(-Math.max(-240, Math.min(240, delta)) * .0015);
    zoomDiagram(scale * factor, event.clientX, event.clientY);
  }, {passive:false});
  diagramViewport.addEventListener('pointerdown', event => {
    if (event.button !== 0 || !event.isPrimary || dragState || !$('diagram-zoom-content').querySelector('svg')) return;
    const rect = diagramViewport.getBoundingClientRect();
    // Let native scrollbars keep their own pointer handling.
    if (event.clientX - rect.left >= diagramViewport.clientWidth || event.clientY - rect.top >= diagramViewport.clientHeight) return;
    event.preventDefault();
    dragState = {pointerId:event.pointerId, x:event.clientX, y:event.clientY, left:diagramViewport.scrollLeft, top:diagramViewport.scrollTop};
    diagramViewport.setPointerCapture(event.pointerId);
    diagramViewport.classList.add('is-dragging');
  });
  diagramViewport.addEventListener('pointermove', event => {
    if (!dragState || event.pointerId !== dragState.pointerId) return;
    diagramViewport.scrollLeft = dragState.left + dragState.x - event.clientX;
    diagramViewport.scrollTop = dragState.top + dragState.y - event.clientY;
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    diagramViewport.addEventListener(type, event => {
      if (dragState?.pointerId === event.pointerId) endDiagramDrag();
    });
  }
  diagramDialog.addEventListener('close', endDiagramDrag);
  window.addEventListener('blur', endDiagramDrag);
  document.addEventListener('click', event => {
    const button = event.target.closest('.zoom-diagram');
    if (!(button instanceof HTMLButtonElement) || button.disabled) return;
    const figure = button.closest('.diagram');
    const original = figure?.querySelector('.diagram-canvas svg');
    if (!original) return;
    const clone = original.cloneNode(true);
    // A dialog clone gets its own identifiers, including CSS selectors and marker references.
    const ids = [...clone.querySelectorAll('[id]'), clone].filter(e => e.id).map(e => e.id).sort((a,b) => b.length-a.length);
    let source = clone.outerHTML;
    ids.forEach(id => {source = source.split(id).join('zoom-' + id);});
    $('diagram-zoom-content').innerHTML = source;
    const svg = $('diagram-zoom-content').querySelector('svg');
    const vb = svg.viewBox.baseVal;
    naturalWidth = vb.width || Number(original.getAttribute('width')) || 1000;
    naturalHeight = vb.height || Number(original.getAttribute('height')) || 800;
    scale = 1;
    $('diagram-title').textContent = figure.querySelector('figcaption span').textContent;
    endDiagramDrag();
    diagramDialog.showModal(); resizeDiagram();
    diagramViewport.scrollTo(0,0);
  });
  $('zoom-in').addEventListener('click', () => zoomDiagram(scale + .2));
  $('zoom-out').addEventListener('click', () => zoomDiagram(scale - .2));
  $('zoom-reset').addEventListener('click', () => zoomDiagram(1));
  let originals, selectedOriginal;
  const originalBytes = source => Uint8Array.from(atob(source.base64), char => char.charCodeAt(0));
  document.addEventListener('click', event => {
    const button=event.target.closest('button.view-original');
    const article=button?.closest('article.document');
    if(!article || article.id!==button.dataset.doc)return;
    if (!originals) originals = JSON.parse($('source-data').textContent);
    if(!Object.prototype.hasOwnProperty.call(originals,button.dataset.doc))return;
    selectedOriginal = originals[button.dataset.doc];
    $('original-path').textContent = selectedOriginal.path;
    $('original-content').textContent = new TextDecoder('utf-8').decode(originalBytes(selectedOriginal));
    $('original-dialog').showModal();
    $('original-content').scrollTop = 0;
  });
  $('download-original').addEventListener('click', () => {
    if (!selectedOriginal) return;
    const url = URL.createObjectURL(new Blob([originalBytes(selectedOriginal)], {type:'text/markdown;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = selectedOriginal.path.split('/').pop(); link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
})();
