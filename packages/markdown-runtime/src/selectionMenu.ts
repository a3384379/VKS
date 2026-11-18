/** Only an explicit context-menu action transfers a quotation to the assistant. */
export function showSelectionMenu(
  text: string,
  x: number,
  y: number,
  explain: (text: string) => void,
  document: Document = window.document,
) {
  const host = document.defaultView || window;
  document.dispatchEvent(new host.Event("vks-close-selection-menu"));
  const quote = text.trim().slice(0, 4000);
  if (!quote) return () => {};
  const menu = document.createElement("div");
  menu.className = "md-selection-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "文档选择菜单");
  const item = document.createElement("button");
  item.type = "button";
  item.setAttribute("role", "menuitem");
  item.textContent = "解释选中文字";
  const close = () => {
    menu.remove();
    document.removeEventListener("pointerdown", outside, true);
    document.removeEventListener("keydown", key, true);
    document.removeEventListener("contextmenu", outside, true);
    document.removeEventListener("scroll", close, true);
    document.removeEventListener("vks-close-selection-menu", close);
    host.removeEventListener("resize", close);
  };
  const outside = (event: Event) => {
    if (!menu.contains(event.target as Node)) close();
  };
  const key = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      item.focus();
    }
  };
  item.addEventListener("pointerdown", (event) => event.preventDefault());
  item.addEventListener("click", () => {
    close();
    explain(quote);
  });
  menu.append(item);
  document.body.append(menu);
  const rect = menu.getBoundingClientRect();
  const selected = document.getSelection();
  const anchor = selected?.rangeCount
    ? selected.getRangeAt(0).getBoundingClientRect()
    : null;
  if (!Number.isFinite(x) || !Number.isFinite(y) || (x === 0 && y === 0)) {
    x = anchor?.left || 8;
    y = anchor?.bottom || 8;
  }
  menu.style.left =
    Math.max(8, Math.min(x, host.innerWidth - rect.width - 8)) + "px";
  menu.style.top =
    Math.max(8, Math.min(y, host.innerHeight - rect.height - 8)) + "px";
  item.focus({ preventScroll: true });
  document.addEventListener("pointerdown", outside, true);
  document.addEventListener("keydown", key, true);
  document.addEventListener("contextmenu", outside, true);
  document.addEventListener("scroll", close, true);
  document.addEventListener("vks-close-selection-menu", close);
  host.addEventListener("resize", close);
  return close;
}
