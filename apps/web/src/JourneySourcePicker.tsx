import { useState } from "react";
import type { Entity } from "./types";
import type { JourneySemantics } from "./journeys";

type Binding = NonNullable<JourneySemantics["source_refs"]>[number];
const types: Record<string, string> = {
  document: "文档",
  section: "章节",
  page: "页面",
  action: "动作",
  api: "接口",
  symbol: "实现",
  sql: "SQL",
  table: "数据对象",
  scenario: "场景",
};

export function JourneySourcePicker({
  entities,
  value,
  onChange,
}: {
  entities: Entity[];
  value: Binding[];
  onChange: (value: Binding[]) => void;
}) {
  const [search, setSearch] = useState("");
  const materials = entities.filter((entity) => types[entity.type]);
  const matches = materials.filter((entity) =>
    entity.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  );
  const selected = (entity: Entity) =>
    value.some(
      (ref) =>
        ref.entity_id === entity.entity_id &&
        ref.revision_id === entity.entity_revision_id,
    );
  return (
    <div className="journey-source-picker">
      <h3>场景依据资料</h3>
      <p>
        选择支持业务说明的当前版本材料，最多 50
        项。它们支持说明，不会自动证明每一步的执行因果。
      </p>
      <label>
        查找依据资料
        <input
          aria-label="查找依据资料"
          placeholder="搜索文档、页面或接口名称"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </label>
      <div
        className="journey-source-options"
        role="group"
        aria-label="选择场景依据资料"
      >
        {matches.slice(0, 40).map((entity) => (
          <label key={entity.entity_revision_id}>
            <input
              type="checkbox"
              aria-label={`依据资料：${types[entity.type]} · ${entity.name}`}
              checked={selected(entity)}
              disabled={!selected(entity) && value.length >= 50}
              onChange={(event) =>
                onChange(
                  event.target.checked
                    ? [
                        ...value,
                        {
                          entity_id: entity.entity_id,
                          revision_id: entity.entity_revision_id,
                        },
                      ]
                    : value.filter(
                        (ref) =>
                          ref.entity_id !== entity.entity_id ||
                          ref.revision_id !== entity.entity_revision_id,
                      ),
                )
              }
            />
            <span>
              {types[entity.type]} · {entity.name}
            </span>
          </label>
        ))}
      </div>
      {matches.length > 40 && <p>当前显示前 40 项，请输入名称缩小范围。</p>}
      {!matches.length && <p>当前可见版本中没有匹配的资料。</p>}
      {value.length > 0 && (
        <div className="journey-source-selected" aria-label="已选场景依据资料">
          <h4>已选 {value.length} 项</h4>
          {value.map((ref) => {
            const entity = entities.find(
              (item) =>
                item.entity_id === ref.entity_id &&
                item.entity_revision_id === ref.revision_id,
            );
            return (
              <div key={ref.revision_id}>
                <span>{entity?.name || "准确来源当前不可读"}</span>
                <button
                  type="button"
                  onClick={() => onChange(value.filter((item) => item !== ref))}
                >
                  移除
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
