(() => {
  const openUpdate = () => {
    document.getElementById('fh-ai-launch')?.click();
    document.querySelector('[data-ai-tab="knowledge"]')?.click();
    document.getElementById('fh-update-pick')?.click();
  };
  const actions = document.querySelector('.top-actions');
  let update = document.getElementById('template-document-update');
  if (!update) {
    update = document.createElement('button');
    update.id = 'template-document-update';
    update.textContent = '文档更新';
    actions.prepend(update);
  }
  update.addEventListener('click', openUpdate);
  const start = document.getElementById('template-start');
  start?.addEventListener('click', (event) => {
    const documents = JSON.parse(document.getElementById('source-data').textContent);
    if (!Object.keys(documents).length) {
      event.preventDefault();
      event.stopPropagation();
      openUpdate();
    }
  });
  document.documentElement.dataset.handbookTemplate = 'standalone-original-2';
})();
