"""Check the pixels of an actual generated SVG, including narrow readers."""

from math import log

from playwright.sync_api import expect


def assert_clear_mermaid_zoom(diagram):
    image = diagram.locator(".diagram-image img")
    expect(image).to_be_visible(timeout=20000)
    expect(image).to_have_js_property("complete", True)
    # A narrow reader exposes the general Markdown image rule that used to
    # clamp the diagram even though the zoom indicator kept increasing.
    assert image.bounding_box()["height"] <= 481
    assert image.bounding_box()["width"] <= image.evaluate("n=>n.parentElement.clientWidth")
    assert image.bounding_box()["width"] <= image.evaluate("n=>n.naturalWidth") + 1
    image.click()
    dialog = diagram.page.locator(".md-image-dialog")
    expect(dialog).to_be_visible()
    image = dialog.locator("img")
    canvas = dialog.locator(".md-image-canvas")
    first_width = None
    image_url = image.get_attribute("src")
    observations = []
    try:
        for percent in (100, 200, 300, 100, 300):
            dialog.get_by_role("button", name="图片缩放比例", exact=True).click()
            delta = log(percent / 100) / 0.0025
            rect = canvas.bounding_box()
            while delta > 0:
                step = min(150, delta)
                canvas.dispatch_event("wheel", {
                    "deltaY": -step, "clientX": rect["x"] + rect["width"]/2,
                    "clientY": rect["y"] + rect["height"]/2,
                })
                delta -= step
            expect(dialog.get_by_role("button", name="图片缩放比例", exact=True)).to_have_text(f"{percent}%")
            expect(image).to_have_attribute("src", image_url)
            width = image.bounding_box()["width"]
            if first_width is None:
                first_width = width
            assert abs(width - first_width * percent / 100) < 2
            pixels = image.evaluate(
                """async image => {
                  await image.decode();
                  const source = await (await fetch(image.src)).text();
                  const svg = new DOMParser().parseFromString(source, 'image/svg+xml').documentElement;
                  const probe = document.importNode(svg, true);
                  probe.style.cssText += ';position:fixed;left:-10000px;top:0';
                  document.body.append(probe);
                  try {
                    const shape = probe.querySelector('.node rect');
                    const node = shape.getBoundingClientRect(), whole = probe.getBoundingClientRect();
                    const width = image.getBoundingClientRect().width, scale = width / whole.width;
                    const bitmap = document.createElement('canvas');
                    bitmap.width = Math.ceil(width); bitmap.height = Math.ceil(whole.height * scale);
                    const context = bitmap.getContext('2d');
                    context.fillStyle = 'white'; context.fillRect(0, 0, bitmap.width, bitmap.height);
                    context.drawImage(image, 0, 0, width, whole.height * scale);
                    // Outside the node stroke and away from the arrow: the old
                    // offset shadow paints gray here, more visibly when zoomed.
                    const x = Math.round((node.right - whole.left + 3) * scale);
                    const y = Math.round((node.top - whole.top + node.height / 4) * scale);
                    return {pixel: Array.from(context.getImageData(x, y, 1, 1).data),
                      filter: getComputedStyle(shape).filter};
                  } finally { probe.remove(); }
                }"""
            )
            assert pixels["filter"] == "none", pixels
            assert min(pixels["pixel"][:3]) >= 253, pixels
            observations.append((percent, width, pixels))
        assert image.count() == 1
    finally:
        dialog.get_by_role("button", name="按宽度", exact=True).click()
        expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="适应窗口", exact=True).click()
        dialog.get_by_role("button", name="关闭大图", exact=True).click()
        expect(dialog).to_have_count(0)
    return observations
