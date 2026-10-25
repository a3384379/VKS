export type ImportKind = "bundle" | "documents" | "prototype";
export function importDetails(filename: string, kind: ImportKind) {
  const extension = filename.toLowerCase().split(".").pop() || "";
  if (kind === "bundle")
    return extension === "zip"
      ? { media_type: "application/zip", prototype_mode: undefined }
      : null;
  if (kind === "documents") {
    const types: Record<string, string> = {
      md: "text/markdown",
      markdown: "text/markdown",
      json: "application/json",
      yaml: "application/yaml",
      yml: "application/yaml",
    };
    return types[extension]
      ? { media_type: types[extension], prototype_mode: undefined }
      : null;
  }
  const images: Record<string, string> = {
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    webp: "image/webp",
  };
  if (images[extension])
    return {
      media_type: images[extension],
      prototype_mode: "screenshot_hotspots",
    };
  if (["html", "htm"].includes(extension))
    return { media_type: "text/html", prototype_mode: "axure_html" };
  if (extension === "zip")
    return { media_type: "application/zip", prototype_mode: "preview_bundle" };
  return null;
}
export function importPayload(
  kind: ImportKind,
  artifactId: string | string[],
  analysisId?: string,
  prototypeMode?: string,
) {
  return {
    kind,
    artifact_ids: Array.isArray(artifactId) ? artifactId : [artifactId],
    ...(kind === "bundle" ? {} : { analysis_set_id: analysisId }),
    ...(kind === "prototype" ? { prototype_mode: prototypeMode } : {}),
  };
}
