type CitationNode = {
  type: string;
  value?: string;
  tagName?: string;
  children?: CitationNode[];
};

const revisionLabel = String.raw`(?:revision[ _]id|修订\s*id)\s*[:：]\s*`;
const revisionUuid = "[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}";
const inlineRevisionLabel = new RegExp(
  String.raw`[ \t]*[（(]\s*${revisionLabel}$`,
  "i",
);
const inlineRevisionId = new RegExp(`^${revisionUuid}$`, "i");
const revisionSuffix = new RegExp(
  String.raw`[ \t]*[（(]\s*${revisionLabel}${revisionUuid}\s*[）)]`,
  "gi",
);

export function stripCitationRevisionIds<T extends CitationNode>(
  children: T[],
) {
  return children
    .filter((child, index) => {
      const previous = children[index - 1],
        next = children[index + 1];
      if (
        child.tagName !== "code" ||
        !inlineRevisionId.test(child.children?.[0]?.value ?? "") ||
        previous?.type !== "text" ||
        !inlineRevisionLabel.test(previous.value ?? "") ||
        next?.type !== "text" ||
        !/^\s*[）)]/.test(next.value ?? "")
      )
        return true;
      previous.value = previous.value!.replace(inlineRevisionLabel, "");
      next.value = next.value!.replace(/^\s*[）)]/, "");
      return false;
    })
    .map((child) =>
      child.type === "text" && child.value
        ? { ...child, value: child.value.replace(revisionSuffix, "") }
        : child,
    );
}
