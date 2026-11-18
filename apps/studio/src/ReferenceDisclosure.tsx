import { useState, type ReactNode } from "react";

export function ReferenceDisclosure({
  count,
  children,
}: {
  count: number;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!count) return null;
  return (
    <details
      className="source-disclosure"
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        相关原文 <span>{count}</span>
      </summary>
      {expanded && children}
    </details>
  );
}
