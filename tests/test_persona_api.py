"""M1-10：人格 / 规则读写 API（面板 06 §4 人格编辑器的后端）。

契约要点：
- GET 一次给齐四个用户文件 + 各自的版本号；persona 标明实际用的是 user 还是 builtin
- 写必须带 base_version；磁盘版本对不上 → 409，不写，回磁盘现状
- 只认四个名字，框架文件（base/chat）没有写口
- 写入原样落字节；写完 Persona 下一次现拼就是新内容（热重载）
- 预览与 Persona.build_system_prompt 逐字节一致
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core.adapter.web.adapter import WebAdapter
from core.llm.persona_llm.base import Persona, ensure_user_persona, ensure_user_rules
from core.persona_api import MAX_CHARS, build_persona_routes


@pytest.fixture
def paths(tmp_path):
    persona = tmp_path / "persona.md"
    rules = tmp_path / "rules"
    ensure_user_persona(persona)
    ensure_user_rules(rules)
    return persona, rules


@pytest.fixture
def client(paths):
    persona, rules = paths
    a = WebAdapter(extra_routes=build_persona_routes(persona_path=persona, rules_dir=rules))
    return TestClient(a.app)


def _files(client):
    r = client.get("/api/persona")
    assert r.status_code == 200 and r.json()["ok"] is True
    return r.json()["files"]


def _save(client, name, content, base_version, **kw):
    return client.post(f"/api/persona/{name}",
                       json={"content": content, "base_version": base_version}, **kw)


# ---------- 读 ----------


def test_read_returns_four_files_with_versions(client):
    files = _files(client)
    assert set(files) == {"persona", "global", "private", "group"}
    for f in files.values():
        assert f["exists"] is True
        assert isinstance(f["version"], str) and len(f["version"]) == 16
        assert f["chars"] == len(f["content"])


def test_skeleton_persona_reports_builtin_source(client):
    """只剩骨架 → 她实际用内置人设；规则骨架 → 不注入。"""
    files = _files(client)
    assert files["persona"]["source"] == "builtin"
    assert files["global"]["injected"] is False


def test_missing_file_has_null_version(tmp_path):
    a = WebAdapter(extra_routes=build_persona_routes(
        persona_path=tmp_path / "none.md", rules_dir=tmp_path / "no-rules"))
    files = _files(TestClient(a.app))
    assert files["persona"] == {"exists": False, "content": "", "version": None,
                                "mtime": None, "chars": 0, "source": "builtin"}


def test_crlf_file_is_served_with_lf(client, paths):
    persona, _ = paths
    persona.write_bytes("# 林星遥\r\n\r\n她喜欢下雨天。\r\n".encode())
    f = _files(client)["persona"]
    assert f["content"] == "# 林星遥\n\n她喜欢下雨天。\n"
    assert f["source"] == "user"


def test_non_utf8_file_is_not_served_as_garbage(client, paths):
    """坏字节不硬解成乱码——否则前端一保存就把原文件毁了。"""
    persona, _ = paths
    persona.write_bytes(b"\xff\xfe bad")
    f = _files(client)["persona"]
    assert f["content"] == "" and "UTF-8" in f["error"]


# ---------- 写 ----------


def test_save_writes_exact_bytes_and_takes_effect(client, paths):
    persona, rules = paths
    version = _files(client)["persona"]["version"]
    text = "# 林星遥\n\n她说话慢，喜欢下雨天。  \n"
    r = _save(client, "persona", text, version)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["version"] != version
    # 原样落字节：不 strip、不转 CRLF
    assert persona.read_bytes() == text.encode("utf-8")
    # 热重载：Persona 下一次现拼就是新内容
    assert "她说话慢，喜欢下雨天。" in Persona(persona_path=persona, rules_dir=rules).build_system_prompt()
    assert _files(client)["persona"]["source"] == "user"


def test_save_rule_then_injected(client, paths):
    _, rules = paths
    version = _files(client)["group"]["version"]
    assert _save(client, "group", "群里少说话。\n", version).status_code == 200
    assert _files(client)["group"]["injected"] is True
    assert (rules / "group.md").read_text(encoding="utf-8") == "群里少说话。\n"


def test_empty_rule_is_allowed(client):
    """空字符串 = 不注入，但文件保留。"""
    version = _files(client)["global"]["version"]
    r = _save(client, "global", "", version)
    assert r.status_code == 200 and r.json()["exists"] is True
    assert _files(client)["global"]["injected"] is False


def test_stale_version_conflicts_and_does_not_write(client, paths):
    """别处（另一个标签页 / 文本编辑器）改过 → 409，回磁盘现状，不覆盖。"""
    persona, _ = paths
    old = _files(client)["persona"]["version"]
    persona.write_text("# 别处改的\n\n正文\n", encoding="utf-8")
    r = _save(client, "persona", "面板里的版本", old)
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["current"]["content"] == "# 别处改的\n\n正文\n"
    assert persona.read_text(encoding="utf-8") == "# 别处改的\n\n正文\n"


def test_second_save_with_same_base_conflicts(client):
    """两个标签页拿同一个版本先后保存：后到的 409。"""
    v = _files(client)["private"]["version"]
    assert _save(client, "private", "A", v).status_code == 200
    assert _save(client, "private", "B", v).status_code == 409


def test_null_version_creates_missing_file(tmp_path):
    rules = tmp_path / "rules"
    a = WebAdapter(extra_routes=build_persona_routes(persona_path=tmp_path / "p.md", rules_dir=rules))
    c = TestClient(a.app)
    assert _save(c, "global", "新规则\n", None).status_code == 200
    assert (rules / "global.md").read_text(encoding="utf-8") == "新规则\n"
    # 已存在时再带 null = 以为它不存在 → 冲突
    assert _save(c, "global", "x", None).status_code == 409


def test_missing_base_version_is_rejected(client):
    r = client.post("/api/persona/persona", json={"content": "x"})
    assert r.status_code == 400


def test_unknown_name_is_rejected(client, paths):
    """只认四个名字：框架文件没有写口，也拿不到任意路径。"""
    for name in ("base", "chat", "..", "secrets"):
        r = _save(client, name, "x", None)
        assert r.status_code == 404, name


def test_non_string_content_is_rejected(client):
    v = _files(client)["persona"]["version"]
    assert _save(client, "persona", 123, v).status_code == 400


def test_too_long_is_rejected(client, paths):
    persona, _ = paths
    before = persona.read_bytes()
    v = _files(client)["persona"]["version"]
    assert _save(client, "persona", "字" * (MAX_CHARS + 1), v).status_code == 413
    assert persona.read_bytes() == before


def test_cross_origin_write_is_rejected(client, paths):
    """写口走 _guard_post_json：跨站 Origin → 403。"""
    persona, _ = paths
    before = persona.read_bytes()
    v = _files(client)["persona"]["version"]
    r = _save(client, "persona", "x", v, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert persona.read_bytes() == before


def test_write_requires_json_content_type(client):
    r = client.post("/api/persona/persona", content=b"content=x",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


# ---------- 框架（只读） ----------


def test_frame_returns_base_chat_and_builtin(client):
    r = client.get("/api/persona/frame")
    body = r.json()
    assert body["ok"] is True
    assert set(body["files"]) == {"base", "chat"}
    assert body["files"]["chat"]["content"].strip()
    assert body["builtin_persona"]["content"].strip()


def test_frame_has_no_write_route(client):
    assert client.post("/api/persona/frame", json={"content": "x", "base_version": None}).status_code == 404


# ---------- 拼接预览 ----------


@pytest.mark.parametrize("kind", ["private", "group"])
def test_preview_matches_real_system_prompt(client, paths, kind):
    """预览与对话共用 Persona.system_parts——逐字节一致。"""
    persona, rules = paths
    (rules / "global.md").write_text("全局：别太长。\n", encoding="utf-8")
    (rules / f"{kind}.md").write_text(f"{kind} 专属规则。\n", encoding="utf-8")
    body = client.get(f"/api/persona/preview?kind={kind}").json()
    assert body["ok"] is True
    real = Persona(persona_path=persona, rules_dir=rules).build_system_prompt(kind)
    assert body["text"] == real
    assert body["tokens"] == len(real)
    names = [b["name"] for b in body["blocks"]]
    assert names == ["base", "chat", "persona", "rules.global", f"rules.{kind}"]


def test_preview_marks_editable_blocks(client, paths):
    persona, rules = paths
    (rules / "private.md").write_text("私聊规则。\n", encoding="utf-8")
    blocks = {b["name"]: b for b in client.get("/api/persona/preview?kind=private").json()["blocks"]}
    assert blocks["base"]["edit_as"] is None
    # 人设只剩骨架 → 用的是内置那份，不可在这里直接改
    assert blocks["persona"]["edit_as"] is None
    assert blocks["rules.private"]["edit_as"] == "private"
    persona.write_text("# 林星遥\n\n正文\n", encoding="utf-8")
    blocks = {b["name"]: b for b in client.get("/api/persona/preview?kind=private").json()["blocks"]}
    assert blocks["persona"]["edit_as"] == "persona"


def test_preview_skips_empty_rules(client):
    """骨架规则不注入，预览里也不出现。"""
    names = [b["name"] for b in client.get("/api/persona/preview?kind=group").json()["blocks"]]
    assert names == ["base", "chat", "persona"]


def test_preview_rejects_unknown_kind(client):
    assert client.get("/api/persona/preview?kind=admin").status_code == 400
