import { createElement, Fragment, type ReactNode } from "react";
import { common, createLowlight } from "lowlight";
import type { RootContent } from "hast";
import "./markdown-extras.css";

const highlighter = createLowlight(common);
function renderToken(token: RootContent, index: number): ReactNode {
  if (token.type === "text") return token.value;
  if (token.type !== "element") return null;
  const children = token.children.map(renderToken);
  if (token.tagName !== "span")
    return createElement(Fragment, { key: index }, children);
  const classes = Array.isArray(token.properties.className)
    ? token.properties.className
        .filter(
          (value): value is string =>
            typeof value === "string" &&
            /^(?:hljs-|language-)[a-z0-9_-]+$/.test(value),
        )
        .join(" ")
    : "";
  return createElement(
    "span",
    { key: index, className: classes || undefined },
    children,
  );
}
export default function CodeHighlight({
  language,
  value,
}: {
  language: string;
  value: string;
}) {
  const supported = highlighter.registered(language) && value.length <= 100000;
  try {
    return (
      <code className={"hljs language-" + language}>
        {supported
          ? highlighter.highlight(language, value).children.map(renderToken)
          : value}
      </code>
    );
  } catch {
    return <code>{value}</code>;
  }
}
