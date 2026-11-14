"""A content-free, locally editable handbook, distinct from readonly exports."""

import base64
import gzip
import json
from pathlib import Path

from .readonly_export import html_library

SCHEMA = "vks-handbook-authoring-1"


def template_html():
    assets = Path(__file__).with_name("reader_assets")
    payload = {
        "schema_version": 1,
        "authoring": SCHEMA,
        "reader_mode": "handbook",
        "project": {"id": "vks-handbook-template", "name": "本地知识手册"},
        "snapshot": {"id": "authoring", "name": "可编辑模板"},
        "prototypes": [],
        "documents": [],
        "navigation": {"packages": []},
        "bindings": [],
        "native_documents": [],
        "rag": {"chunks": []},
        "warnings": [],
        "ai": {"base_url": "https://api.openai.com/v1", "protocol": "responses", "model": ""},
    }
    # The immutable source skeleton is captured before any personal input or
    # rendered DOM exists. Browser saves rebuild from it, never live outerHTML.
    source = html_library(payload).decode()
    packed = base64.b64encode(gzip.compress(source.encode(), compresslevel=9, mtime=0)).decode()
    envelope = {"schema": SCHEMA, "template_id": "handbook", "template_version": "1.0.0", "documents": [], "images": []}
    css = (assets / "authoring.css").read_text().replace("</style", "<\\/style")
    script = (assets / "authoring.js").read_text().replace("</script", "<\\/script")
    additions = (
        '<script id="vks-template-data" type="application/json">'
        + json.dumps(envelope, ensure_ascii=False).replace("<", "\\u003c")
        + '</script><script id="vks-authoring-source" type="application/octet-stream" data-encoding="gzip-base64">'
        + packed
        + '</script><script id="vks-authoring-runtime">'
        + script
        + "</script>"
    )
    head = source.index("</head>")
    source = source[:head] + '<style id="vks-authoring-style">' + css + "</style>" + source[head:]
    body = source.rfind("</body>")
    return (source[:body] + additions + source[body:]).encode()
