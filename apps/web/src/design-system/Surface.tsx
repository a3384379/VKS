import type { ReactNode } from "react";
import { Panel } from "../components";

export function Surface({
  children,
  title,
  action,
  className = "",
}: {
  children: ReactNode;
  title?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <Panel title={title} action={action} className={`v2-panel ${className}`}>
      <div className="v2-panel-body">{children}</div>
    </Panel>
  );
}
