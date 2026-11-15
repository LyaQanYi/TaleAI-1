"""管理面板（面板设计 06 · P0）：页面契约。

跟 test_ux_shell.py 同一条纪律——页面是静态文件，只验契约（路由、文件、
关键钩子、安全纪律），不验浏览器里的运行时行为。
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

from core.adapter.web.adapter import WEBUI_DIR, WebAdapter

MONACO_DIR = WEBUI_DIR / "vendor" / "monaco"


def _read(name: str) -> str:
    path = WEBUI_DIR / name
    assert path.exists(), f"webui/{name} 不存在"
    return path.read_text(encoding="utf-8")


# ---------- 路由 ----------


def test_panel_redirects_to_page():
    r = TestClient(WebAdapter().app, follow_redirects=False).get("/panel")
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/static/panel.html"


def test_settings_goes_to_panel_model_page():
    r = TestClient(WebAdapter().app, follow_redirects=False).get("/settings")
    assert r.headers["location"] == "/static/panel.html#/model"


def test_panel_files_served():
    c = TestClient(WebAdapter().app)
    for name, ctype in (("panel.html", "text/html"), ("panel.js", "javascript"), ("panel.css", "text/css")):
        r = c.get(f"/static/{name}")
        assert r.status_code == 200, name
        assert ctype in r.headers["content-type"], name


def test_old_settings_page_still_reachable():
    """旧设置页先保留一个版本（面板 04 Q6）。"""
    assert TestClient(WebAdapter().app).get("/static/settings.html").status_code == 200


# ---------- 页面纪律 ----------


def test_panel_builds_dom_without_innerhtml():
    src = _read("panel.js") + _read("panel.html")
    assert "innerHTML" not in src and "outerHTML" not in src and "insertAdjacentHTML" not in src, \
        "面板不该把字符串当 HTML 塞进 DOM（XSS 纪律）"
    assert "textContent" in _read("panel.js")
    assert "eval(" not in src and "new Function" not in src


def test_panel_makes_no_cdn_calls():
    low = (_read("panel.js") + _read("panel.html") + _read("panel.css")).lower()
    for m in ("cdn.", "unpkg.com", "jsdelivr", "googleapis", "http://", "https://"):
        assert m not in low, f"面板引了外部资源：{m!r}"


def test_panel_uses_vendored_monaco_with_fallback():
    js = _read("panel.js")
    assert '"/static/vendor/monaco/vs"' in js
    assert "mountTextarea" in js, "Monaco 载不进来时要退回普通文本框"
    for rel in ("vs/loader.js", "vs/editor/editor.main.js", "vs/editor/editor.main.css",
                "vs/base/worker/workerMain.js", "vs/basic-languages/markdown/markdown.js",
                "vs/nls.messages.zh-cn.js", "LICENSE.txt"):
        assert (MONACO_DIR / rel).is_file(), f"vendor/monaco/{rel} 缺了"


def test_vendored_monaco_is_trimmed():
    """只带编辑 Markdown 用得到的部分，不把 language/（几 MB 的语言服务）也搬进来。"""
    assert not (MONACO_DIR / "vs" / "language").exists()
    total = sum(p.stat().st_size for p in MONACO_DIR.rglob("*") if p.is_file())
    assert total < 6 * 1024 * 1024, f"vendor/monaco 太大了：{total} 字节"


def test_monaco_served_from_static():
    r = TestClient(WebAdapter().app).get("/static/vendor/monaco/vs/loader.js")
    assert r.status_code == 200


# ---------- 接口契约 ----------


def test_panel_persona_editor_uses_versioned_writes():
    js = _read("panel.js")
    assert "/api/persona" in js and "/api/persona/frame" in js and "/api/persona/preview" in js
    assert "base_version" in js, "写人格文件必须带 base_version（防覆盖别处的改动）"
    assert re.search(r"status\s*===\s*409", js), "没处理 409（文件在别处被改过）"


def test_panel_settings_pages_are_field_driven():
    js = _read("panel.js")
    assert "/api/settings/fields" in js and "/api/settings/values" in js
    assert "/api/platforms/qq" in js


def test_panel_nav_covers_p0_pages_and_old_pages():
    html = _read("panel.html")
    for p in ("persona", "model", "platforms", "settings"):
        assert f'href="#/{p}"' in html, f"侧栏缺 {p}"
    assert 'href="/logs"' in html and 'href="/"' in html


def test_dirty_bar_class_not_reused_for_row_state():
    # .dirty 是底部浮条（position:fixed + visibility:hidden）；字段行若也挂 .dirty，
    # 输入第一个字就会被藏起来、丢焦点（v0.1.0 的回归）。行和标签的“已改”用 .is-dirty。
    css = _read("panel.css")
    js = _read("panel.js")
    assert not re.search(r"\.\w[\w-]*\.dirty\b|\.dirty\.(?!on\b)", css)
    assert 'classList.toggle("dirty"' not in js
    assert '"tag dirty"' not in js
    assert ".f.is-dirty" in css and 'classList.toggle("is-dirty"' in js


def test_chat_thinking_placeholder_removes_whole_row():
    """撤"在想…"占位要连外层 .row 一起删，否则留下空行、多出一截间距。"""
    html = _read("index.html")
    assert "function dropPending()" in html and 'closest(".row")' in html
    assert "pending.remove()" not in html


def test_toast_does_not_cover_sidebar():
    """侧栏展开时提示条从内容区起算，不压住侧栏底部的链接；收起 / 窄屏再贴左。"""
    css = _read("panel.css")
    assert "left: calc(224px + 40px)" in css
    assert "body.nav-closed .toast" in css
    # 窄屏规则必须写在基础规则之后，否则被同权重的后写规则盖掉
    assert css.index("@media (max-width: 1100px) { .toast") > css.index(".toast {")
