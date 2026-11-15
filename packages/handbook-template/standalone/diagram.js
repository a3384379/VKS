/* Responsive diagram previews and an accessible, touch-capable detail viewer. */
(() => {
  'use strict';
  function install() {
    if (window.fhDiagramReader) { window.fhDiagramReader.refresh(); return; }
    const dialog = document.getElementById('diagram-dialog');
    const viewport = document.getElementById('diagram-viewport');
    const content = document.getElementById('diagram-zoom-content');
    const actions = dialog?.querySelector('.diagram-actions');
    if (!dialog || !viewport || !content || !actions) return;
    const mobile = () => matchMedia('(max-width:760px)').matches;
    const state = {svg:null, width:0, height:0, scale:1, mode:'window', serial:0};
    const observed = new WeakSet();
    const pointers = new Map();
    let gesture = null, pendingFrame = 0;
    const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
    const dimensions = svg => {
      const box = svg.viewBox?.baseVal;
      if (box?.width > 0 && box?.height > 0) return {width:box.width, height:box.height};
      const width = Number(svg.getAttribute('width'));
      const height = Number(svg.getAttribute('height'));
      return width > 0 && height > 0 ? {width,height} : null;
    };
    const innerSize = element => {
      const style = getComputedStyle(element);
      return {
        width:Math.max(1, element.clientWidth - parseFloat(style.paddingLeft || 0) - parseFloat(style.paddingRight || 0)),
        height:Math.max(1, element.clientHeight - parseFloat(style.paddingTop || 0) - parseFloat(style.paddingBottom || 0))
      };
    };
    function fitPreview(canvas) {
      const svg = canvas.querySelector(':scope > svg');
      if (!svg) return;
      const natural = dimensions(svg);
      if (!natural) return;
      canvas.classList.add('fh-diagram-preview');
      if (!observed.has(canvas)) {
        observed.add(canvas);
        previewObserver.observe(canvas);
        canvas.setAttribute('role', 'button');
        canvas.setAttribute('tabindex', '0');
        canvas.setAttribute('aria-label', '放大查看完整图表');
        canvas.setAttribute('aria-haspopup', 'dialog');
      }
      const available = innerSize(canvas).width;
      if (available < 40 || canvas.getClientRects().length === 0) return;
      const limit = mobile() ? clamp(innerHeight * .34, 140, 240) : clamp(innerHeight * .42, 220, 400);
      const scale = Math.min(1, available / natural.width, limit / natural.height);
      const width = Math.max(1, natural.width * scale);
      const height = Math.max(1, natural.height * scale);
      const signature = `${width.toFixed(2)}:${height.toFixed(2)}`;
      if (canvas.dataset.fhPreviewSize !== signature) {
        canvas.style.setProperty('--fh-preview-width', `${width.toFixed(2)}px`);
        canvas.style.setProperty('--fh-preview-height', `${height.toFixed(2)}px`);
        canvas.dataset.fhPreviewSize = signature;
        canvas.dataset.fhPreviewScale = String(scale);
      }
      let note = canvas.nextElementSibling;
      if (!note?.classList.contains('fh-diagram-preview-note')) {
        note = document.createElement('p');
        note.className = 'fh-diagram-preview-note';
        canvas.after(note);
      }
      const label = mobile() ? '点图放大 · 双指缩放，单指拖动' : '点图放大 · 适应窗口或按宽度查看细节';
      if (note.textContent !== label) note.textContent = label;
    }
    const previewObserver = new ResizeObserver(entries => entries.forEach(entry => fitPreview(entry.target)));
    function scan(root) {
      if (!(root instanceof Element || root instanceof Document)) return;
      if (root instanceof Element) {
        if (root.matches('.diagram-canvas')) fitPreview(root);
        const parent = root.closest('.diagram-canvas');
        if (parent) fitPreview(parent);
      }
      root.querySelectorAll('.diagram-canvas').forEach(fitPreview);
    }
    function refresh() {
      if (pendingFrame) return;
      pendingFrame = requestAnimationFrame(() => {
        pendingFrame = 0;
        scan(document);
        if (dialog.open && state.svg) {
          if (state.mode === 'window') fitDetail('window');
          else if (state.mode === 'width') fitDetail('width');
          else renderDetail();
        }
      });
    }
    for (const [id,label] of [['diagram-fit-window','适应窗口'],['diagram-fit-width','按宽度']]) {
      if (!document.getElementById(id)) {
        const button = document.createElement('button');
        button.type = 'button'; button.id = id; button.className = 'diagram-fit-button';
        button.textContent = label; button.setAttribute('aria-pressed', 'false');
        actions.insertBefore(button, document.getElementById('zoom-out'));
      }
    }
    dialog.classList.add('fh-diagram-detail');
    const percentLabel = value => {
      const percent = value * 100;
      return `${percent >= 10 ? Math.round(percent) : Number(percent.toFixed(percent >= 1 ? 1 : 2))}%`;
    };
    function renderDetail() {
      if (!state.svg) return;
      const available = innerSize(viewport);
      const width = Math.max(1, state.width * state.scale);
      const height = Math.max(1, state.height * state.scale);
      state.svg.style.setProperty('width', `${width}px`, 'important');
      state.svg.style.setProperty('height', `${height}px`, 'important');
      state.svg.style.setProperty('max-width', 'none', 'important');
      state.svg.style.setProperty('max-height', 'none', 'important');
      state.svg.style.setProperty('min-width', '0', 'important');
      state.svg.setAttribute('width', String(width));
      state.svg.setAttribute('height', String(height));
      content.style.width = `${Math.max(width, available.width)}px`;
      content.style.height = `${Math.max(height, available.height)}px`;
      const reset = document.getElementById('zoom-reset');
      reset.textContent = percentLabel(state.scale);
      reset.title = '恢复原始大小（100%）';
      reset.setAttribute('aria-label', `当前缩放${percentLabel(state.scale)}，点击恢复100%`);
      dialog.dataset.diagramScale = String(state.scale);
      dialog.dataset.diagramMode = state.mode;
      document.getElementById('diagram-fit-window').setAttribute('aria-pressed', String(state.mode === 'window'));
      document.getElementById('diagram-fit-width').setAttribute('aria-pressed', String(state.mode === 'width'));
    }
    function fitDetail(mode) {
      if (!state.svg) return;
      const available = innerSize(viewport);
      state.mode = mode;
      state.scale = mode === 'width' ? clamp(available.width / state.width, .001, 4)
        : Math.max(.001, Math.min(1, available.width / state.width, available.height / state.height));
      renderDetail();
      viewport.scrollTo(0, 0);
    }
    function setScale(next, clientX, clientY) {
      if (!state.svg) return;
      next = clamp(next, .001, 4);
      if (Math.abs(next - state.scale) < 1e-7) return;
      const rect = viewport.getBoundingClientRect();
      clientX ??= rect.left + viewport.clientWidth / 2;
      clientY ??= rect.top + viewport.clientHeight / 2;
      const before = state.svg.getBoundingClientRect();
      const x = clamp((clientX - before.left) / before.width, 0, 1);
      const y = clamp((clientY - before.top) / before.height, 0, 1);
      state.mode = 'manual'; state.scale = next; renderDetail();
      const after = state.svg.getBoundingClientRect();
      viewport.scrollLeft += after.left + x * after.width - clientX;
      viewport.scrollTop += after.top + y * after.height - clientY;
    }
    function clearPointers() {
      const ids = [...pointers.keys()];
      pointers.clear(); gesture = null; viewport.classList.remove('is-dragging');
      for (const id of ids) {
        try { if (viewport.hasPointerCapture(id)) viewport.releasePointerCapture(id); } catch (_) {}
      }
    }
    function openDetail(canvas) {
      const original = canvas.querySelector(':scope > svg');
      const natural = original && dimensions(original);
      if (!natural) return;
      clearPointers();
      const clone = original.cloneNode(true);
      const ids = [...new Set([clone, ...clone.querySelectorAll('[id]')].map(node => node.id).filter(Boolean))].sort((a,b) => b.length - a.length);
      const prefix = `detail-${++state.serial}-`;
      let markup = clone.outerHTML;
      for (const id of ids) markup = markup.split(id).join(prefix + id);
      content.innerHTML = markup;
      state.svg = content.querySelector('svg');
      state.width = natural.width; state.height = natural.height;
      document.getElementById('diagram-title').textContent = canvas.closest('figure')?.querySelector('figcaption span')?.textContent || '图表详情';
      if (!dialog.open) dialog.showModal();
      fitDetail('window');
      requestAnimationFrame(() => { if (dialog.open && state.mode === 'window') fitDetail('window'); });
    }
    function stop(event) { event.preventDefault(); event.stopImmediatePropagation(); }
    document.addEventListener('click', event => {
      const target = event.target instanceof Element ? event.target : event.target.parentElement;
      const button = target?.closest('button');
      const action = button?.id;
      if (dialog.open && ['diagram-fit-window','diagram-fit-width','zoom-in','zoom-out','zoom-reset'].includes(action)) {
        stop(event);
        if (action === 'diagram-fit-window') fitDetail('window');
        else if (action === 'diagram-fit-width') fitDetail('width');
        else setScale(action === 'zoom-reset' ? 1 : state.scale * (action === 'zoom-in' ? 1.25 : .8));
        return;
      }
      const zoom = target?.closest('.zoom-diagram');
      const canvas = zoom?.closest('figure')?.querySelector('.diagram-canvas') || target?.closest('.diagram-canvas.fh-diagram-preview');
      if (canvas?.querySelector(':scope > svg')) { stop(event); openDetail(canvas); }
    }, true);
    document.addEventListener('keydown', event => {
      const canvas = event.target instanceof Element && event.target.closest('.diagram-canvas.fh-diagram-preview');
      if (canvas && ['Enter',' '].includes(event.key)) { stop(event); openDetail(canvas); return; }
      if (!dialog.open || event.altKey || event.ctrlKey || event.metaKey) return;
      if (['+','=','-','0','f','w'].includes(event.key)) {
        stop(event);
        if (event.key === 'f') fitDetail('window');
        else if (event.key === 'w') fitDetail('width');
        else setScale(event.key === '0' ? 1 : state.scale * (event.key === '-' ? .8 : 1.25));
      }
    }, true);
    viewport.addEventListener('wheel', event => {
      if (!dialog.open || !state.svg) return;
      stop(event);
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1);
      setScale(state.scale * Math.exp(-clamp(delta, -240, 240) * .0015), event.clientX, event.clientY);
    }, {capture:true, passive:false});
    const describeGesture = () => {
      const points = [...pointers.values()];
      if (points.length >= 2) {
        const [a,b] = points;
        return {kind:'pinch', distance:Math.max(1, Math.hypot(a.x-b.x,a.y-b.y)), x:(a.x+b.x)/2, y:(a.y+b.y)/2};
      }
      return points.length ? {kind:'drag', x:points[0].x, y:points[0].y} : null;
    };
    viewport.addEventListener('pointerdown', event => {
      if (!dialog.open || !state.svg || (event.pointerType !== 'touch' && event.button !== 0)) return;
      const rect = viewport.getBoundingClientRect();
      if (event.clientX - rect.left >= viewport.clientWidth || event.clientY - rect.top >= viewport.clientHeight) return;
      stop(event); pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
      try { viewport.setPointerCapture(event.pointerId); } catch (_) {}
      gesture = describeGesture(); viewport.classList.add('is-dragging');
    }, true);
    viewport.addEventListener('pointermove', event => {
      if (!pointers.has(event.pointerId)) return;
      stop(event); pointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
      const next = describeGesture();
      if (gesture && next) {
        viewport.scrollLeft += gesture.x - next.x;
        viewport.scrollTop += gesture.y - next.y;
        if (gesture.kind === 'pinch' && next.kind === 'pinch') setScale(state.scale * next.distance / gesture.distance, next.x, next.y);
      }
      gesture = next;
    }, true);
    for (const name of ['pointerup','pointercancel','lostpointercapture']) {
      viewport.addEventListener(name, event => {
        if (!pointers.has(event.pointerId)) return;
        stop(event); pointers.delete(event.pointerId);
        try { if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId); } catch (_) {}
        gesture = describeGesture();
        if (!gesture) viewport.classList.remove('is-dragging');
      }, true);
    }
    viewport.addEventListener('dblclick', event => { if (dialog.open) { stop(event); fitDetail(state.mode === 'window' ? 'width' : 'window'); } }, true);
    dialog.addEventListener('close', () => { clearPointers(); state.svg = null; });
    window.addEventListener('blur', clearPointers);
    window.addEventListener('resize', refresh);
    window.addEventListener('orientationchange', refresh);
    new ResizeObserver(() => { if (dialog.open && state.svg) refresh(); }).observe(viewport);
    new MutationObserver(records => {
      for (const record of records) for (const node of record.addedNodes) if (node instanceof Element && !dialog.contains(node)) scan(node);
    }).observe(document.body,{childList:true,subtree:true});
    window.fhDiagramReader = {version:1, refresh};
    scan(document);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once:true});
  else install();
})();
