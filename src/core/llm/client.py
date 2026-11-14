"""模型客户端的构造（ChatLLM 与 MemoryLLM 共用）。"""

from openai import AsyncOpenAI


def make_client(base_url: str, model: str, api_key: str) -> "AsyncOpenAI | None":
    """配齐了才建客户端；没配（网关或模型名为空）返回 None。

    密钥留空时传一个占位值：本机网关（Ollama 之类）不校验密钥，但 openai SDK
    在密钥为空时直接拒绝构造。
    """
    if not base_url or not model:
        return None
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "not-set")
