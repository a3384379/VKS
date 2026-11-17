"""A package portal whose active menu/tab does not reload its business iframe."""

import json


def portal_source():
    pages = {
        name: (
            f"<!doctype html><html><head><title>{title}</title></head><body>"
            f"<h1>{title}</h1><input aria-label='未保存内容'>"
            "<button id='query' onclick='window.queryClicks=(window.queryClicks||0)+1'>查询收费项</button>"
            "<div id='blank' style='height:90px;width:200px'></div></body></html>"
        )
        for name, title in (("items.html", "收费项目管理"), ("standards.html", "收费标准管理"))
    }
    return (
        "<!doctype html><html><head><title>云收费门户</title></head><body>"
        '<button role="menuitem" data-page="items.html">收费项管理</button>'
        '<button role="menuitem" data-page="standards.html">收费标准管理</button>'
        '<button role="tab" data-tab="items.html">收费项管理</button>'
        '<div id="portal-blank" style="height:30px;width:200px"></div>'
        '<iframe id="actualPage" style="height:350px;width:650px"></iframe><script>'
        f"const pages={json.dumps(pages, ensure_ascii=False)};"
        "window.pageLoads=0;let current='';window.__prototypePack={show(path){"
        "if(current===path)return;current=path;window.pageLoads++;"
        "const doc=new DOMParser().parseFromString(pages[path],'text/html');"
        "const boot=doc.createElement('script');"
        "boot.textContent='window.__prototypePackPage='+JSON.stringify({path})+';';"
        "doc.head.prepend(boot);document.querySelector('#actualPage').srcdoc=doc.documentElement.outerHTML;}};"
        "document.addEventListener('click',e=>{const nav=e.target.closest('[data-page],[data-tab]');"
        "if(nav)__prototypePack.show(nav.dataset.page||nav.dataset.tab)});"
        "__prototypePack.show('standards.html');</script></body></html>"
    )
