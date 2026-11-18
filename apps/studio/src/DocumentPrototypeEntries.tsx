import { useEffect, useRef } from "react";
import { ArrowUpRight, X } from "lucide-react";
import {
  bindingModuleTree,
  bindingPageKey,
  type ModuleBinding,
} from "./bindingModuleTree";
import type { MarkdownSection } from "./RichMarkdown";
import type { Target } from "./runtime";

type EntryBinding = ModuleBinding & {
  title: string;
  target: Target;
  excluded: boolean;
  validity_status: string;
  sections: { revision_id: string; document_revision_id: string }[];
};
type PrototypePage<T> = {
  key: string;
  path: string;
  packageName: string;
  label: string;
  entry: T;
  pageOnly: boolean;
  direct: boolean;
  controls: T[];
};
export type ChapterPrototypeEntries<T> = {
  pages: PrototypePage<T>[];
  other: PrototypePage<T>[];
};

const titleKey = (name: string) =>
  name
    .split(" · ")[0]
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/g, "");
const controlKey = (target: Target) =>
  JSON.stringify([
    target.frame_path,
    target.selector,
    target.state || "",
    target.label,
  ]);

export function documentPrototypeEntries<T extends EntryBinding>(
  revision: string,
  chapters: (MarkdownSection & { heading_level?: number })[],
  bindings: T[],
  packages: { id: string; name: string; kind: string }[],
) {
  const names = new Map(
    packages
      .filter((pack) => pack.kind === "prototype")
      .map((pack) => [pack.id, pack.name]),
  );
  const valid = bindings.filter(
    (binding) =>
      !binding.excluded &&
      binding.validity_status === "valid" &&
      names.has(binding.package_id),
  );
  const tree = bindingModuleTree(valid, packages);
  const modules = new Map<string, T>();
  const citations = new Map<string, Map<string, T[]>>();
  for (const binding of valid) {
    const key = bindingPageKey(binding);
    if (binding.target.selector === "body") {
      const previous = modules.get(key);
      if (
        !previous ||
        binding.target.frame_path.length < previous.target.frame_path.length
      )
        modules.set(key, binding);
    }
    for (const ref of binding.sections) {
      if (ref.document_revision_id !== revision) continue;
      const pages = citations.get(ref.revision_id) ?? new Map<string, T[]>();
      const group = pages.get(key) ?? [];
      group.push(binding);
      pages.set(key, group);
      citations.set(ref.revision_id, pages);
    }
  }
  const result = new Map<string, ChapterPrototypeEntries<T>>();
  const ancestors: { title: string; level: number }[] = [];
  for (const chapter of chapters) {
    const level =
      chapter.heading_level ??
      /^ {0,3}(#{1,6})\s/.exec(chapter.content)?.[1].length ??
      0;
    if (level) {
      while (ancestors.length && ancestors[ancestors.length - 1].level >= level)
        ancestors.pop();
      ancestors.push({ title: titleKey(chapter.name), level });
    }
    const groups = citations.get(chapter.revision_id);
    if (!groups) continue;
    const pages = [...groups]
      .map(([key, entries]) => {
        const direct = entries.find(
          (entry) => entry.target.selector === "body",
        );
        const page = direct ?? modules.get(key);
        const node = tree.byPage.get(key);
        const controls = new Map<string, T>();
        for (const entry of entries)
          if (entry.target.selector !== "body")
            controls.set(controlKey(entry.target), entry);
        return {
          key,
          label: node?.label ?? entries[0].target.route,
          path: node?.path.join(" / ") ?? entries[0].target.route,
          packageName: names.get(entries[0].package_id) || "",
          entry: page ?? entries[0],
          pageOnly: !page,
          direct: !!direct,
          controls: [...controls.values()],
        };
      })
      .sort((a, b) => a.path.localeCompare(b.path, "zh-CN"));
    // An exact chapter or ancestor title distinguishes the business page from
    // other pages that happen to cite the same source chapter.
    const heading = [
      titleKey(chapter.name),
      ...ancestors.map((item) => item.title).reverse(),
    ].find(
      (name) => name && pages.some((page) => titleKey(page.label) === name),
    );
    const primary = heading
      ? pages.filter((page) => titleKey(page.label) === heading)
      : pages.filter((page) => page.direct);
    result.set(chapter.revision_id, {
      pages: primary.length ? primary : pages,
      other: primary.length
        ? pages.filter((page) => !primary.includes(page))
        : [],
    });
  }
  return result;
}

export function DocumentPrototypeEntries<T extends EntryBinding>({
  chapter,
  entries,
  onOpen,
  onClose,
}: {
  chapter: string;
  entries: ChapterPrototypeEntries<T>;
  onOpen: (entry: T, pageOnly: boolean) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => dialog.current?.showModal(), []);

  function pageCards(pages: PrototypePage<T>[]) {
    return pages.map((page) => (
      <article
        className="chapter-prototype-page"
        key={page.key}
        aria-label={page.path}
      >
        <header>
          <div>
            <h3>{page.path}</h3>
            <small>{page.packageName}</small>
          </div>
          <button onClick={() => onOpen(page.entry, page.pageOnly)}>
            <ArrowUpRight size={15} /> 打开原型
          </button>
        </header>
        {!!page.controls.length && (
          <details>
            <summary>关联控件（{page.controls.length}）</summary>
            <ul>
              {page.controls.map((control) => (
                <li key={control.id}>
                  <div>
                    <b>{control.target.label}</b>
                    <small>{control.title}</small>
                    {page.controls.some(
                      (item) =>
                        item.id !== control.id &&
                        item.target.label === control.target.label,
                    ) && (
                      <small>
                        {[
                          ...control.target.frame_path,
                          control.target.state,
                          control.target.selector,
                        ]
                          .filter(Boolean)
                          .join(" / ")}
                      </small>
                    )}
                  </div>
                  <button onClick={() => onOpen(control, false)}>
                    打开原型
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </article>
    ));
  }

  return (
    <dialog
      ref={dialog}
      className="chapter-prototype-dialog"
      aria-label="原型入口"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header>
        <div>
          <h2>原型入口</h2>
          <p>{chapter}</p>
        </div>
        <button aria-label="关闭原型入口" onClick={onClose}>
          <X size={17} />
        </button>
      </header>
      <div className="chapter-prototype-results">
        {pageCards(entries.pages)}
        {!!entries.other.length && (
          <details className="chapter-prototype-other">
            <summary>其他引用本章的页面（{entries.other.length}）</summary>
            {pageCards(entries.other)}
          </details>
        )}
      </div>
    </dialog>
  );
}
