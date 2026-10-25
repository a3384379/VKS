export interface HotspotRect {
  x: number;
  y: number;
  width: number;
  height: number;
}
export function normalizedRect(
  start: { x: number; y: number },
  end: { x: number; y: number },
): HotspotRect {
  const x = Math.max(0, Math.min(1, Math.min(start.x, end.x))),
    y = Math.max(0, Math.min(1, Math.min(start.y, end.y)));
  return {
    x,
    y,
    width: Math.min(1 - x, Math.abs(end.x - start.x)),
    height: Math.min(1 - y, Math.abs(end.y - start.y)),
  };
}
export function validRect(value: unknown): value is HotspotRect {
  if (!value || typeof value !== "object") return false;
  const rect = value as HotspotRect;
  return (
    [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) &&
    rect.x >= 0 &&
    rect.y >= 0 &&
    rect.width > 0 &&
    rect.height > 0 &&
    rect.x + rect.width <= 1.000001 &&
    rect.y + rect.height <= 1.000001
  );
}
export function parsePassiveSelector(value: string) {
  const match =
    /^(?<tag>[A-Za-z][A-Za-z0-9-]*)?(?:#(?<id>[A-Za-z_][A-Za-z0-9_-]*))?(?:\.(?<className>[A-Za-z_][A-Za-z0-9_-]*))?(?:\[(?<attribute>[A-Za-z_][A-Za-z0-9_.:-]*)=(?:"(?<doubleValue>[^"'\\\]\r\n]{1,256})"|'(?<singleValue>[^"'\\\]\r\n]{1,256})')\])?$/.exec(
      value,
    );
  if (!match?.groups || !value) return null;
  const { tag, id, className, attribute, doubleValue, singleValue } =
    match.groups;
  return {
    tag: tag?.toLowerCase(),
    id,
    className,
    attribute,
    value: doubleValue ?? singleValue,
  };
}
export function matchesPassiveSelector(
  selector: string,
  tag: string,
  attributes: Record<string, string>,
) {
  const parsed = parsePassiveSelector(selector);
  return (
    !!parsed &&
    (!parsed.tag || parsed.tag === tag.toLowerCase()) &&
    (!parsed.id || attributes.id === parsed.id) &&
    (!parsed.className ||
      (attributes.class || "").split(/\s+/).includes(parsed.className)) &&
    (!parsed.attribute ||
      attributes[parsed.attribute.toLowerCase()] === parsed.value)
  );
}
export function hiddenPassiveElement(attributes: Record<string, string>) {
  return (
    "hidden" in attributes ||
    "inert" in attributes ||
    attributes["aria-hidden"]?.toLowerCase() === "true" ||
    /(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))/i.test(
      attributes.style || "",
    )
  );
}
