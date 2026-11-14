"""人格与聊天规则的读写 API（M1-10 · 面板 06 §4 人格编辑器的后端）。

    GET  /api/persona                 四个用户文件：persona + global/private/group 规则
    POST /api/persona/{name}          写其中一个；带 base_version，被别处改过回 409
    GET  /api/persona/frame           框架文件 base.md / chat.md（只读）+ 内置人设（"恢复内置"用）
    GET  /api/persona/preview?kind=   私聊 / 群聊时她真正读到的 system，按注入顺序分段

**冲突检测用内容哈希，不用 mtime**（偏离 04 §5 的 base_mtime）：mtime 有粒度
问题（同一秒内两次写、编辑器保留 mtime），而 ns 精度的 mtime 超过 JS 安全整数
范围、传到前端会被截断。sha256 前 16 位作版本号，两种问题都没有。
文件不存在时版本号是 null——带 null 来写 = "我以为它还不存在"。

**四个文件共用一个写口**（偏离 04 §5 的 /api/persona + /api/rules/{scope} 两套）：
语义完全相同（整份覆盖 + 版本检查），分两套只会让两边漂移。

**只开四个文件**：名字查表映射到固定路径，请求里拿不到任意路径（防穿越）；
base.md / chat.md 是框架契约，这里**没有写口**（改了 `<msg>` 就解析不了）。

写入原样落字节（不 strip、不规范化）：Persona 每回合现拼，"内容不变 → 字节
不变"是缓存命中的前提；保存成功下一句对话即生效（热重载，PR3）。
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.config.api import _bad, _guard_post_json
from core.fsutil import replace_with_retry
from core.llm.persona_llm.base import (
    BUILTIN_PERSONA_PATH,
    PROMPTS_DIR,
    RULES_DIR,
    USER_PERSONA_PATH,
    Persona,
    _has_real_content,
    pick_persona_source,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 单个文件的字数上限。人设/规则是给模型读的静态块，几千字已经很长；
#: 上限只为挡住误粘贴的大文件，不是给内容定额。
MAX_CHARS = 100_000

#: 预览的会话类型
_KINDS = ("private", "group")

# 同一进程内"比版本 + 写"必须原子：两个标签页几乎同时保存时，
# 后到的那个要看到前一个写完的版本，而不是两个都过检查。
_write_lock = threading.Lock()


def _version(raw: bytes | None) -> str | None:
    return None if raw is None else hashlib.sha256(raw).hexdigest()[:16]


def _read_raw(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _file_payload(path: Path, raw: bytes | None) -> dict:
    """一个文件的读视图。content 统一成 \\n 换行（编辑器与 diff 好处理），
    version 按磁盘原字节算——前端拿它回来写，比对的是真实文件。"""
    if raw is None:
        return {"exists": False, "content": "", "version": None, "mtime": None, "chars": 0}
    try:
        content = raw.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        # 坏字节不硬解：给空内容 + 错误，避免前端拿着乱码一保存就把原文件毁掉
        return {"exists": True, "content": "", "version": _version(raw), "mtime": _mtime(path),
                "chars": 0, "error": "文件不是 UTF-8 编码，请用文本编辑器修复后再在这里改"}
    return {"exists": True, "content": content, "version": _version(raw),
            "mtime": _mtime(path), "chars": len(content)}


def _atomic_write(path: Path, data: bytes) -> None:
    """temp + 替换：写到一半崩溃也不会留下半个文件（同 Config.save；Windows 共享冲突会退避重试）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_persona_routes(
    persona_path: Path | None = None,
    rules_dir: Path | None = None,
) -> Callable[[FastAPI], None]:
    """造一个路由注册器，交给 WebAdapter 的 `extra_routes`。

    两个路径参数只在测试里用（指向 tmp），生产不传 = data/config/ 下的那几份。
    """
    persona_file = Path(persona_path) if persona_path is not None else USER_PERSONA_PATH
    rules = Path(rules_dir) if rules_dir is not None else RULES_DIR
    #: 名字 → 文件。写口只认这四个名字
    files: dict[str, Path] = {
        "persona": persona_file,
        "global": rules / "global.md",
        "private": rules / "private.md",
        "group": rules / "group.md",
    }

    def _persona() -> Persona:
        return Persona(persona_path=persona_file, rules_dir=rules)

    def _persona_source() -> str | None:
        # 与对话现拼用同一个判据（pick_persona_source），不在这里另抄一份。
        # 文件不是 UTF-8 时对话那边也拼不出来（沿用上次好的），这里如实给 null
        try:
            return "user" if pick_persona_source(persona_file, BUILTIN_PERSONA_PATH) == persona_file else "builtin"
        except UnicodeDecodeError:
            return None

    def _read_all() -> dict:
        out = {}
        for name, path in files.items():
            payload = _file_payload(path, _read_raw(path))
            if name == "persona":
                # 用户那份只剩骨架时，她实际用的是内置人设——编辑器要说清楚
                payload["source"] = _persona_source()
            else:
                # 规则没有内置默认轨：判空 = 不注入
                payload["injected"] = _has_real_content(payload["content"])
            out[name] = payload
        return out

    def _write(name: str, content: str, base_version: str | None) -> tuple[int, dict]:
        path = files[name]
        with _write_lock:
            current = _read_raw(path)
            if _version(current) != base_version:
                # 被别处改过：不写，把磁盘上的现状给前端（编辑器里的内容前端自己留着）
                return 409, {"ok": False, "error": f"{path.name} 已在别处被改过，请先看改动再决定",
                             "current": _file_payload(path, current)}
            data = content.encode("utf-8")
            _atomic_write(path, data)
        logger.info("面板保存了 %s（%d 字）", path, len(content))
        return 200, {"ok": True, "name": name, **_file_payload(path, data),
                     "note": "已保存，下一句对话即生效"}

    def _frame() -> dict:
        return {
            "ok": True,
            "files": {
                stem: {"content": (PROMPTS_DIR / f"{stem}.md").read_text(encoding="utf-8")}
                for stem in ("base", "chat")
            },
            "builtin_persona": {"content": BUILTIN_PERSONA_PATH.read_text(encoding="utf-8")},
        }

    def _preview(kind: str) -> dict:
        # 与对话走同一个 Persona.system_parts——预览就是她真正读到的
        persona = _persona()
        edit_names = {path: name for name, path in files.items()}
        blocks = []
        for block, path, text in persona.system_parts(kind):
            item = {
                "name": block,
                "file": path.name,
                # 去 /api/persona 里改哪一份。人设块固定指向用户那份 persona：
                # 生效的是内置人设时，想换掉它也正是去写用户的 persona.md。框架为 null
                "edit_as": "persona" if block == "persona" else edit_names.get(path),
                "content": text,
                "tokens": len(text),  # 与 context._estimate_tokens 同口径：1 字 ≈ 1 token
            }
            if block == "persona":
                item["source"] = "user" if path == persona_file else "builtin"
            blocks.append(item)
        text = "\n\n".join(b["content"] for b in blocks)
        return {"ok": True, "kind": kind, "blocks": blocks, "text": text, "tokens": len(text)}

    def register(app: FastAPI) -> None:
        @app.get("/api/persona")
        async def read_files() -> JSONResponse:
            try:
                return JSONResponse({"ok": True, "files": await asyncio.to_thread(_read_all)})
            except Exception:
                logger.exception("读取人格/规则文件失败")
                return JSONResponse({"ok": False, "error": "读取人格/规则文件失败"})

        @app.get("/api/persona/frame")
        async def frame() -> JSONResponse:
            try:
                return JSONResponse(await asyncio.to_thread(_frame))
            except Exception:
                logger.exception("读取框架提示词失败")
                return JSONResponse({"ok": False, "error": "读取框架提示词失败"})

        @app.get("/api/persona/preview")
        async def preview(kind: str = "private") -> JSONResponse:
            if kind not in _KINDS:
                return _bad(f"kind 只能是 {list(_KINDS)}")
            try:
                return JSONResponse(await asyncio.to_thread(_preview, kind))
            except Exception:
                logger.exception("拼接预览失败")
                return JSONResponse({"ok": False, "error": "拼接预览失败（某个文件读不出来）"})

        @app.post("/api/persona/{name}")
        async def write_file(name: str, request: Request) -> JSONResponse:
            body, err = await _guard_post_json(request)
            if err is not None:
                return err
            if name not in files:
                return _bad(f"只能写这几个文件：{list(files)}", code=404)
            content = body.get("content")
            if not isinstance(content, str):
                return _bad("content 必须是字符串")
            if len(content) > MAX_CHARS:
                return _bad(f"内容太长（{len(content)} 字，上限 {MAX_CHARS}）", code=413)
            # 必须显式带 base_version：不带就写 = 盲写，正是要防的覆盖别处改动
            if "base_version" not in body:
                return _bad("缺少 base_version（先 GET /api/persona 拿到版本号）")
            base_version = body["base_version"]
            if base_version is not None and not isinstance(base_version, str):
                return _bad("base_version 必须是字符串或 null")
            try:
                code, payload = await asyncio.to_thread(_write, name, content, base_version)
            except OSError as exc:
                return _bad(f"写入失败：{exc}", code=500)
            return JSONResponse(payload, status_code=code)

    return register
