import { useEffect, useId, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FileText,
  Search,
} from "lucide-react";
import type { BindingModuleNode } from "./bindingModuleTree";

export function BindingModulePicker({
  roots,
  selected,
  onSelect,
  onOpen,
}: {
  roots: BindingModuleNode[];
  selected?: BindingModuleNode;
  onSelect: (id: string) => void;
  onOpen: () => void;
}) {
  const [open, setOpen] = useState(false),
    [query, setQuery] = useState("");
  const [focused, setFocused] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const wrapper = useRef<HTMLDivElement>(null),
    trigger = useRef<HTMLButtonElement>(null),
    input = useRef<HTMLInputElement>(null),
    tree = useRef<HTMLDivElement>(null);
  const treeId = useId();
  const label = selected?.path.join(" / ") || "全部模块";
  useEffect(() => {
    if (!open) return;
    input.current?.focus();
    const outside = (event: PointerEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  function select(id: string) {
    onSelect(id);
    setOpen(false);
    setQuery("");
    trigger.current?.focus();
  }
  function toggleOpen() {
    if (!open) {
      onOpen();
      setQuery("");
      if (selected)
        setExpanded((previous) => {
          const next = new Set(previous);
          function find(rows: BindingModuleNode[]): boolean {
            for (const row of rows)
              if (row.id === selected!.id || find(row.children)) {
                if (row.children.length) next.add(row.id);
                return true;
              }
            return false;
          }
          find(roots);
          return next;
        });
    }
    setOpen(!open);
  }
  const needle = query.trim().toLocaleLowerCase();
  function matching(
    rows: BindingModuleNode[],
    parentMatches = false,
  ): BindingModuleNode[] {
    if (!needle) return rows;
    return rows.flatMap((node) => {
      const matches =
        parentMatches || node.label.toLocaleLowerCase().includes(needle);
      const children = matching(node.children, matches);
      return matches || children.length ? [{ ...node, children }] : [];
    });
  }
  function toggle(id: string) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function items() {
    return Array.from(
      tree.current?.querySelectorAll<HTMLElement>('[role="treeitem"]') || [],
    );
  }
  function keydown(event: React.KeyboardEvent<HTMLDivElement>) {
    if ((event.target as HTMLElement).closest("button")) return;
    const rows = items(),
      current = (event.target as HTMLElement).closest<HTMLElement>(
        '[role="treeitem"]',
      );
    if (!current) return;
    const index = rows.indexOf(current),
      key = current.dataset.nodeId!,
      hasChildren = current.hasAttribute("aria-expanded"),
      isExpanded = current.getAttribute("aria-expanded") === "true";
    if (
      [
        "ArrowDown",
        "ArrowUp",
        "ArrowRight",
        "ArrowLeft",
        "Home",
        "End",
        "Enter",
        " ",
      ].includes(event.key)
    )
      event.preventDefault();
    if (event.key === "ArrowDown")
      rows[Math.min(index + 1, rows.length - 1)]?.focus();
    if (event.key === "ArrowUp") rows[Math.max(0, index - 1)]?.focus();
    if (event.key === "Home") rows[0]?.focus();
    if (event.key === "End") rows.at(-1)?.focus();
    if (event.key === "Enter" || event.key === " ") select(key);
    if (event.key === "ArrowRight" && hasChildren) {
      if (!isExpanded && !needle) toggle(key);
      else rows[index + 1]?.focus();
    }
    if (event.key === "ArrowLeft") {
      if (hasChildren && isExpanded && !needle) toggle(key);
      else
        current.parentElement?.parentElement
          ?.closest<HTMLElement>('[role="treeitem"]')
          ?.focus();
    }
  }
  function branch(rows: BindingModuleNode[], depth = 1) {
    return (
      <ul role="group">
        {rows.map((node) => {
          const folder = !!node.children.length,
            isExpanded = !!needle || expanded.has(node.id);
          return (
            <li
              role="treeitem"
              key={node.id}
              aria-label={node.label}
              aria-level={depth}
              aria-selected={selected?.id === node.id}
              aria-expanded={folder ? isExpanded : undefined}
              tabIndex={node.id === tabStop ? 0 : -1}
              data-node-id={node.id}
              onFocus={(event) => {
                if (event.target === event.currentTarget) setFocused(node.id);
              }}
            >
              <div
                className="binding-module-node"
                title={node.path.join(" / ")}
                onClick={(event) => {
                  event.stopPropagation();
                  select(node.id);
                }}
              >
                {folder ? (
                  <button
                    type="button"
                    tabIndex={-1}
                    className="binding-module-toggle"
                    aria-label={`${isExpanded ? "收起" : "展开"}${node.label}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      toggle(node.id);
                    }}
                  >
                    {isExpanded ? (
                      <ChevronDown size={14} />
                    ) : (
                      <ChevronRight size={14} />
                    )}
                  </button>
                ) : (
                  <span className="binding-module-spacer" />
                )}
                {folder ? <Folder size={15} /> : <FileText size={15} />}
                <span className="binding-module-name">{node.label}</span>
                <small aria-hidden="true">{node.bindingIds.size}</small>
              </div>
              {folder && isExpanded && branch(node.children, depth + 1)}
            </li>
          );
        })}
      </ul>
    );
  }
  const filtered = matching(roots);
  const visibleIds: string[] = [];
  function collect(rows: BindingModuleNode[]) {
    for (const node of rows) {
      visibleIds.push(node.id);
      if (needle || expanded.has(node.id)) collect(node.children);
    }
  }
  collect(filtered);
  const tabStop = visibleIds.includes(focused)
    ? focused
    : selected && visibleIds.includes(selected.id)
      ? selected.id
      : visibleIds[0];
  return (
    <div
      className="binding-module-picker"
      ref={wrapper}
      onKeyDown={(event) => {
        if (event.key === "Escape" && open) {
          event.stopPropagation();
          setOpen(false);
          trigger.current?.focus();
        }
      }}
    >
      <button
        type="button"
        ref={trigger}
        className="binding-module-trigger"
        aria-label="筛选关联模块"
        aria-haspopup="tree"
        aria-expanded={open}
        aria-controls={treeId}
        title={label}
        onClick={toggleOpen}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" && !open) {
            event.preventDefault();
            toggleOpen();
          }
        }}
      >
        <span>{label}</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="binding-module-popover">
          <label className="binding-module-search">
            <Search size={14} />
            <input
              ref={input}
              aria-label="搜索模块或功能"
              placeholder="搜索模块或功能…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  items()[0]?.focus();
                }
              }}
            />
          </label>
          <button
            type="button"
            className="binding-module-all"
            onClick={() => select("")}
          >
            全部模块
          </button>
          <div
            ref={tree}
            role="tree"
            aria-label="关联模块与功能"
            id={treeId}
            onKeyDown={keydown}
          >
            {branch(filtered)}
          </div>
          {!filtered.length && (
            <p className="binding-module-empty">没有匹配的模块或功能</p>
          )}
        </div>
      )}
    </div>
  );
}
