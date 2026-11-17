"""The original handbook UI, with retrieval/credentials owned by its host.

Only repository-owned assets are executed. Imported HTML and its AI runtime are
never inserted here; the host supplies authorized answers and exact citations.
"""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def assistant_html():
    root = Path(__file__).resolve().parents[3] / "packages/handbook-template/standalone"
    assets = Path(__file__).with_name("reader_assets")
    source = root.joinpath("shell.html").read_text()
    # The empty template hash is not a project knowledge version. Exact source
    # revision IDs come from the host; do not label them with that empty hash.
    source = source.replace(
        '"corpusHash":"4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"', '"corpusHash":""'
    )
    source = source.replace("__TEMPLATE_LOADER_JSON__", '""')
    runtime = root.joinpath("assistant.js").read_text()
    marker = "const fhChatUpgrade = installChatUpgrade();"
    assert runtime.count(marker) == 1
    runtime = runtime.replace(marker, assets.joinpath("document-assistant.js").read_text() + "\n" + marker)
    presentation = "const fhChatPresentation = installChatPresentation();"
    assert runtime.count(presentation) == 1
    runtime = runtime.replace(presentation, presentation + "\nvksDocumentHost.installPresentation(fhChatPresentation);")
    # The embedded panel is narrow even on a desktop. Responsive expand/collapse
    # follows the host viewport rather than mistaking that panel for a phone.
    runtime = runtime.replace(
        "const narrowScreen = matchMedia('(max-width: 700px)');",
        "const narrowScreen = parent.matchMedia('(max-width: 700px)');",
    )
    # Project/snapshot namespace is supplied by the host before srcdoc is set.
    runtime = runtime.replace("standalone-handbook-2", "vks-document-ai-HOST_SCOPE")
    for name in ("book", "codec", "assistant", "mermaid", "diagram", "integration"):
        code = runtime if name == "assistant" else root.joinpath(name + ".js").read_text()
        if name == "integration":
            code = ""
        source = source.replace("__TEMPLATE_" + name.upper() + "__", code.replace("</script", "<\\/script"))
    style = """<style id="vks-document-assistant-host-style">
html,body{margin:0!important;width:100%;height:100%;overflow:hidden!important}
.appbar,.workspace,.topbar,.floating-top,#back-to-top,#fh-ai-launch,#fh-ai-top,
#fh-ai-selection-ask,
.fh-ai-portable-reminder{display:none!important}
#fh-ai-panel .fh-ai-indicator,#fh-ai-welcome>.fh-ai-small{display:none!important}
.vks-host-index-status{padding:10px 20px;font-size:12px;color:#526b60;background:#f2f7f3;border-bottom:1px solid #dfe9e2}
.fh-ai-host-settings-link{margin:0 0 14px;padding:7px 11px;background:transparent;color:#276450;border:1px solid #d7e2da;border-radius:7px;font-size:12px}
#fh-ai-panel{inset:0!important;position:fixed!important;width:100%!important;height:100%!important;
max-width:none!important;max-height:none!important;border:0!important;box-shadow:none!important}
#fh-ai-panel .fh-ai-head{cursor:default!important}
#fh-ai-panel .fh-ai-version{display:none}
#fh-ai-layout-toggle:not([hidden]){display:inline-flex!important}
#fh-ai-panel .fh-ai-historybar{padding:8px 18px}
#fh-ai-panel .fh-ai-message{overflow-wrap:anywhere}
.vks-host-index-status{padding:6px 14px;font-size:11px;line-height:1.4}
#fh-ai-panel .fh-ai-tools-toggle{min-height:30px;padding:4px 14px}
#fh-ai-panel .fh-ai-tools-label{font-size:11px}
#fh-ai-panel .fh-ai-tools-summary{font-size:11px}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-options{display:flex;gap:12px;padding:8px 14px 4px;align-items:center}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-options>label{display:flex;align-items:center;gap:6px;flex:1;min-width:0;margin:0;font-size:11px;white-space:nowrap}
#fh-ai-panel .fh-ai-chat-tools select{height:30px!important;min-height:30px!important;padding:4px 7px!important;font-size:12px!important;line-height:1.3}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-options select{flex:1;width:0;min-width:0}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-historybar{display:flex;align-items:center;gap:7px;padding:4px 14px}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-historybar>label{margin:0;font-size:11px;white-space:nowrap}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-historybar select{flex:1;width:0;min-width:0}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-historybar>button{height:30px;min-height:30px;padding:4px 8px;font-size:11px}
#fh-ai-panel .fh-ai-chat-tools .fh-ai-history-status{margin:0;padding:2px 14px 7px;font-size:10px;line-height:1.4}
#fh-ai-panel #fh-ai-tab-settings{padding:12px 14px}
#fh-ai-panel #fh-ai-tab-settings .fh-ai-card{padding:12px;margin-bottom:10px;border-radius:9px}
#fh-ai-panel #fh-ai-tab-settings .fh-ai-card h4{font-size:13px;margin:0 0 8px}
#fh-ai-panel #fh-ai-tab-settings .fh-ai-card p{margin:6px 0;font-size:11px;line-height:1.5}
#fh-ai-panel .fh-ai-reading-settings .fh-ai-field{display:grid;grid-template-columns:70px minmax(0,1fr);gap:8px;align-items:center;font-size:12px;margin:0}
#fh-ai-panel .fh-ai-reading-settings select{height:32px!important;min-height:32px!important;padding:4px 8px!important;font-size:12px!important}
#fh-ai-panel #fh-ai-tab-settings .fh-ai-history-card input{padding:6px 8px;min-height:30px;font-size:11px}
#fh-ai-panel #fh-ai-tab-settings .fh-ai-history-card button{padding:5px 8px;min-height:30px;font-size:11px}
#fh-ai-panel .fh-ai-welcome{display:grid;grid-template-columns:36px minmax(0,1fr);column-gap:10px;row-gap:12px;align-items:center;padding:20px 16px 14px}
#fh-ai-panel .fh-ai-welcome .fh-ai-orbit{grid-column:1;grid-row:1;width:36px;height:36px;margin:0;border-radius:10px;display:flex;align-items:center;justify-content:center}
#fh-ai-panel .fh-ai-welcome .fh-ai-orbit svg{width:22px;height:22px}
#fh-ai-panel .fh-ai-welcome h3{grid-column:2;grid-row:1;margin:0;font-size:18px;line-height:1.4}
#fh-ai-panel .fh-ai-welcome>p,#fh-ai-panel .fh-ai-welcome>.fh-ai-examples{grid-column:1/-1;margin:0}
#fh-ai-panel .fh-ai-welcome .fh-ai-examples{gap:8px}
@media(max-width:420px){#fh-ai-panel .fh-ai-chat-tools .fh-ai-options{gap:7px}#fh-ai-panel .fh-ai-chat-tools .fh-ai-options>label{gap:4px;font-size:10px}#fh-ai-panel .fh-ai-chat-tools select{font-size:11px!important}}
#fh-ai-panel .fh-ai-resize-handle,#fh-ai-panel .fh-ai-resize-hint{display:none!important}
.fh-ai-window-options{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 12px;font-size:12px}
.fh-ai-window-choices{display:flex;gap:3px;background:#edf3ef;padding:3px;border-radius:7px}
.fh-ai-window-choices button,.fh-ai-window-reset{border:0;background:transparent;padding:5px 8px;font-size:12px;border-radius:5px;min-height:28px}
.fh-ai-window-choices button.is-active{background:white;color:#205c46;box-shadow:0 1px 4px #254d3c20}
.fh-ai-window-reset{margin-left:auto;color:#526b60}
.fh-ai-source-entry{min-width:0;margin:8px 0;padding:8px;border:1px solid #e0e5ec;border-radius:8px}
.fh-ai-source-entry>.fh-ai-source{width:100%;margin:0}
.fh-ai-source-excerpt{font-size:12px}
#fh-ai-panel .fh-ai-inline-cite{display:inline;padding:0 3px;margin:0 1px;font-size:inherit;line-height:inherit;vertical-align:baseline}
#fh-ai-panel{container-type:size}
#fh-ai-panel .fh-ai-foot>span:last-child{display:none}
/* Host preferences use compact groups; source connection controls stay mounted and hidden. */
#fh-ai-panel .fh-ai-head{height:47px;min-height:47px;padding:0 12px;gap:8px;box-sizing:border-box;background:#f7faf8}
#fh-ai-panel .fh-ai-head h2{margin:0;font-size:14px;line-height:1.4;color:#2b5542;gap:7px}
#fh-ai-panel .fh-ai-head h2 svg{width:18px;height:18px}
#fh-ai-panel .fh-ai-head-actions{gap:5px}
#fh-ai-panel .fh-ai-head-actions .fh-ai-icon{width:30px;height:30px;min-height:30px;padding:3px;font-size:18px}
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings{padding:12px 16px 20px;min-width:0;overflow-x:hidden}
#fh-ai-panel .fh-ai-settings-intro{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px;font-size:11px;color:#71877a}
#fh-ai-panel .fh-ai-host-settings-link{margin:0;padding:4px 0;border:0;background:transparent;font-size:12px;min-height:28px;white-space:nowrap}
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings .fh-ai-preference-group,
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings .fh-ai-history-card{display:block;padding:12px 0;margin:0;border:0;border-radius:0;border-top:1px solid #e0e9e2;background:transparent;box-shadow:none}
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings h4{margin:0 0 10px;font-size:12px;line-height:1.4;color:#345443}
#fh-ai-panel .fh-ai-window-options{font-size:12px}
#fh-ai-panel .fh-ai-setting-row{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
#fh-ai-panel .fh-ai-setting-label{font-size:12px;flex:0 0 62px}
#fh-ai-panel .fh-ai-window-choices{gap:2px;padding:3px;border-radius:7px}
#fh-ai-panel .fh-ai-window-choices button{padding:5px 9px;font-size:12px;min-height:30px;white-space:nowrap}
#fh-ai-panel .fh-ai-window-reset{margin-left:auto;padding:5px 0;font-size:11px;min-height:30px;white-space:nowrap}
#fh-ai-panel .fh-ai-reading-settings .fh-ai-field{grid-template-columns:62px minmax(0,1fr);font-size:12px}
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings .fh-ai-reading-settings p{margin:7px 0 0;font-size:11px;color:#7b8c81}
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings .fh-ai-history-card>p{margin:0 0 10px;font-size:12px;color:#617a6a;overflow-wrap:anywhere}
#fh-ai-panel .fh-ai-history-backup{border:1px solid #e0e8e2;border-radius:7px;margin:0;background:#f8faf8;overflow:hidden}
#fh-ai-panel .fh-ai-history-backup>summary{font-size:12px;padding:9px 11px;cursor:pointer;line-height:1.4;color:#3e6751}
#fh-ai-panel .fh-ai-history-backup-content{padding:0 11px 10px}
#fh-ai-panel .fh-ai-history-backup .fh-ai-field{display:block;margin:8px 0;font-size:11px}
#fh-ai-panel .fh-ai-history-backup input[readonly]{box-sizing:border-box;display:block;width:100%;margin-top:5px;min-width:0;color:#607568;background:#fff}
#fh-ai-panel .fh-ai-history-backup-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:9px 0}
#fh-ai-panel .fh-ai-history-backup button{margin:0;white-space:normal;font-size:11px;line-height:1.4;min-height:32px}
#fh-ai-panel .fh-ai-history-backup .fh-ai-small{overflow-wrap:anywhere}
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings>.fh-ai-settings-intro,
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings>.fh-ai-preference-group,
#fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings>.fh-ai-history-card{max-width:680px;margin-left:auto;margin-right:auto}
@media(max-width:420px){
 #fh-ai-panel .fh-ai-head-actions .fh-ai-icon{width:34px;min-height:34px;height:34px}
 #fh-ai-panel #fh-ai-tab-settings.vks-assistant-settings{padding-left:12px;padding-right:12px}
 #fh-ai-panel .fh-ai-setting-label{flex-basis:54px}
 #fh-ai-panel .fh-ai-reading-settings .fh-ai-field{grid-template-columns:54px minmax(0,1fr)}
 #fh-ai-panel .fh-ai-window-choices button{padding-left:7px;padding-right:7px}
 #fh-ai-panel .fh-ai-window-reset{font-size:10px}
}
</style>"""
    style += "<style>" + assets.joinpath("assistant-composer.css").read_text() + "</style>"
    source = source.replace("</head>", style + "</head>", 1)
    # The framework's network access is replaced by a nonce-bound host channel.
    csp = "<meta http-equiv=\"Content-Security-Policy\" content=\"connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'\">"
    return source.replace("<head>", "<head>" + csp, 1)


def embedded_assistant():
    """Inert, compact source for a file:// export; contains no project data/key."""
    import base64
    import gzip

    return json.dumps(base64.b64encode(gzip.compress(assistant_html().encode(), mtime=0)).decode())
