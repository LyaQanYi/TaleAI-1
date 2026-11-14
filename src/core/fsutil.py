"""文件小工具：原子替换（带 Windows 共享冲突重试）。

`os.replace` 在 Windows 上，目标文件正被别的进程打开、且对方没给
FILE_SHARE_DELETE 时会抛 PermissionError——记事本 / VS Code 开着、杀毒软件实时
扫描、或者 Persona 每回合现读恰好撞上，都会这样。这类冲突是瞬时的，退避几次
基本就过；真打不开（只读目录、权限不够）重试几次后照常抛出。
"""

import os
import time

#: 退避间隔（秒）：第 1、2 次重试前各等这么久，总共最多试 3 次
REPLACE_BACKOFF = (0.05, 0.1)


def replace_with_retry(src: str | os.PathLike, dst: str | os.PathLike,
                       backoff: tuple[float, ...] = REPLACE_BACKOFF) -> None:
    """os.replace(src, dst)；遇到 PermissionError 按 backoff 退避重试。"""
    for delay in backoff:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(src, dst)
