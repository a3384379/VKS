# Shared Markdown reader

The handbook interaction is the baseline: prose shows a complete thumbnail; clicking an image or rendered diagram opens the same detail viewer with fit-to-window, fit-to-width, zoom, 100% reset and close controls. Small images retain their natural size in prose. Large or tall images fit both the reading column and the thumbnail height. The detail viewer supports wheel zoom, pointer panning and keyboard controls in its actual owner window.

`src/RichMarkdown.tsx` is the React reading adapter used by Studio documents, knowledge panels, answers and citation previews. The Web compatibility entry points re-export shared media components, diagram policy, isolated renderer and math plugins. Application adapters continue to own authorization, exact revision selection, section identity and navigation.

`src/reader.mjs` is the portable DOM adapter bundled into standalone readers and the embedded handbook assistant. It uses the same diagram policy, thumbnail styles, image viewer and explicit selection context menu. `scripts/reader_markdown.mjs` is only its build entry point. Generated reader assets are produced by `npm run build:reader`.

Imported HTML remains an immutable source document with its own original viewer. It is not rewritten to force the platform renderer into the source. Platform Markdown renders sanitized, inert images; diagram code still runs in the isolated engine frame with source and size limits. Viewing a larger image never executes source HTML, relaxes attachment authorization or sends content to a model.
