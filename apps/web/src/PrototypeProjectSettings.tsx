import { useState } from "react";
import { api, date, mutation } from "./api";
import { Button, Notice, Panel } from "./components";
import { usePermission, useWorkspace } from "./context";
import { prototypeApi, prototypeManifest, prototypeScope } from "./prototype";

export function PrototypeProjectSettings() {
  const { w } = useWorkspace();
  return (
    <PrototypePreferencesForm
      key={
        w.project.id +
        ":" +
        (w.prototype_settings?.row_version ?? 0) +
        ":" +
        (w.selected_release?.id || w.selected_analysis_set?.id || "")
      }
    />
  );
}

function PrototypePreferencesForm() {
  const { w, run, busy } = useWorkspace();
  const allowed = usePermission("project:manage");
  const pages = w.pages.filter((row) => prototypeManifest(row));
  const [entry, setEntry] = useState(
    pages.find((page) => page.id === w.prototype_settings?.entry_page_id)
      ?.stable_key || "",
  );
  const [order, setOrder] = useState(
    w.prototype_settings?.menu_order?.join("\n") || "",
  );
  if (!allowed || !pages.length) return null;
  return (
    <Panel title="原型导览设置">
      <form
        className="prototype-v3-editor"
        onSubmit={(event) => {
          event.preventDefault();
          const scope = prototypeScope(w);
          void run(
            () =>
              api(
                `${prototypeApi(w.project.id)}/settings`,
                mutation(
                  "PUT",
                  {
                    ...Object.fromEntries(scope),
                    entry_page_key: entry || null,
                    menu_order: order
                      .split("\n")
                      .map((row) => row.trim())
                      .filter(Boolean),
                  },
                  w.prototype_settings?.row_version ?? 0,
                ),
              ),
            "已保存原型导览设置",
          );
        }}
      >
        <label>
          默认入口
          <select
            value={entry}
            onChange={(event) => setEntry(event.target.value)}
          >
            <option value="">打开项目空间</option>
            {pages
              .filter((page) =>
                prototypeManifest(page)?.states.some(
                  (state) =>
                    state.stateKey ===
                      prototypeManifest(page)?.defaultStateKey && state.surface,
                ),
              )
              .map((page) => (
                <option key={page.id} value={page.stable_key}>
                  {page.name}
                </option>
              ))}
          </select>
        </label>
        <label>
          快捷菜单排序
          <textarea
            placeholder="按顺序填写页面标识，每行一个"
            value={order}
            onChange={(event) => setOrder(event.target.value)}
          />
        </label>
        <details>
          <summary>查看当前可用页面标识</summary>
          <ul>
            {pages.map((page) => (
              <li key={page.id}>
                {page.name} · <code>{page.stable_key}</code>
              </li>
            ))}
          </ul>
        </details>
        <Notice>入口和菜单随当前知识版本的可读页面显示。</Notice>
        <Button type="submit" disabled={busy}>
          保存导览设置
        </Button>
      </form>
    </Panel>
  );
}

export function InstanceBackupStatus() {
  const { w } = useWorkspace();
  if (!w.backup_status) return null;
  const { backup, restore } = w.backup_status;
  return (
    <Panel title="备份与恢复">
      {backup ? (
        <p>
          最近备份 {date(backup.timestamp)} ·{" "}
          {backup.database_kind === "postgres" ? "PostgreSQL" : "SQLite"} ·
          已记录 SHA-256
          <code> {backup.sha256}</code>
        </p>
      ) : (
        <p>暂无本机备份记录。</p>
      )}
      {restore && (
        <p>
          最近恢复 {date(restore.timestamp)} · 数据库和对象已校验 · 归档 SHA-256
          <code> {restore.sha256}</code>
        </p>
      )}
    </Panel>
  );
}
