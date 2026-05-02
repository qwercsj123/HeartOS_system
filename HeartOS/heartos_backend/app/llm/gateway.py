from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException

from ..config import settings


@dataclass(frozen=True)
class ProviderDef:
    key: str
    name: str
    url: str
    default_model: str
    env_key_name: str
    needs_referer: bool = False


PROVIDERS: dict[str, ProviderDef] = {
    "zhipu": ProviderDef(
        key="zhipu",
        name="Zhipu GLM",
        url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        default_model="glm-4-flash",
        env_key_name="APP_LLM_ZHIPU_API_KEY",
    ),
    "openrouter": ProviderDef(
        key="openrouter",
        name="OpenRouter",
        url="https://openrouter.ai/api/v1/chat/completions",
        default_model="deepseek/deepseek-chat-v3-5",
        env_key_name="APP_LLM_OPENROUTER_API_KEY",
        needs_referer=True,
    ),
    "deepseek": ProviderDef(
        key="deepseek",
        name="DeepSeek",
        url="https://api.deepseek.com/v1/chat/completions",
        default_model="deepseek-chat",
        env_key_name="APP_LLM_DEEPSEEK_API_KEY",
    ),
    "qwen": ProviderDef(
        key="qwen",
        name="Qwen",
        url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        default_model="qwen-turbo",
        env_key_name="APP_LLM_QWEN_API_KEY",
    ),
    "groq": ProviderDef(
        key="groq",
        name="Groq",
        url="https://api.groq.com/openai/v1/chat/completions",
        default_model="llama-3.3-70b-versatile",
        env_key_name="APP_LLM_GROQ_API_KEY",
    ),
}


class LLMGateway:
    def __init__(self, public_base_url: str = "", app_title: str = "HeartOS") -> None:
        self.public_base_url = public_base_url
        self.app_title = app_title
        self._provider_defs: dict[str, ProviderDef] = dict(PROVIDERS)
        self._provider_keys: dict[str, str] = {}

    def register_provider(self, provider: ProviderDef, api_key: str = "") -> None:
        self._provider_defs[provider.key] = provider
        if api_key:
            self._provider_keys[provider.key] = api_key.strip()

    def set_provider_key(self, provider_key: str, api_key: str) -> None:
        self._provider_keys[provider_key] = (api_key or "").strip()

    def list_providers(self) -> list[str]:
        return sorted(self._provider_defs.keys())

    async def chat(
        self,
        *,
        provider_key: str,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        user: dict[str, Any] | None = None,
        override_api_key: str = "",
        timeout_seconds: int = 45,
        retries: int = 1,
    ) -> dict[str, Any]:
        provider = self._provider_defs.get(provider_key)
        if not provider:
            raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider_key}")

        api_key = (override_api_key or "").strip() or self._provider_keys.get(provider_key, "")
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail=f"Provider `{provider_key}` API key not configured on server ({provider.env_key_name})",
            )

        payload_messages: list[dict[str, str]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": model or provider.default_model,
            "messages": payload_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if user:
            payload["user"] = {"id": user.get("id"), "username": user.get("username")}

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        if provider.needs_referer:
            headers["HTTP-Referer"] = self.public_base_url or "http://127.0.0.1"
            headers["X-Title"] = self.app_title

        timeout = httpx.Timeout(timeout_seconds)
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)

        last_err: Exception | None = None
        for attempt in range(max(0, retries) + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                    resp = await client.post(provider.url, headers=headers, json=payload)
                if resp.status_code >= 400:
                    detail = _extract_error_message(resp)
                    raise HTTPException(status_code=resp.status_code, detail=detail)
                data = resp.json()
                reply = _extract_reply(data)
                if not reply:
                    raise HTTPException(status_code=502, detail="Empty response from provider")
                return {
                    "reply": reply,
                    "provider": provider.key,
                    "model": payload["model"],
                    "request_id": resp.headers.get("x-request-id") or resp.headers.get("request-id"),
                    "raw": data,
                }
            except HTTPException:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < retries:
                    continue
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {last_err}")


def _extract_reply(data: dict[str, Any]) -> str:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join([p for p in parts if p]).strip()
    return ""


def _extract_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return (resp.text or f"HTTP {resp.status_code}")[:500]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("msg") or f"HTTP {resp.status_code}")
        if isinstance(err, str):
            return err
        if "detail" in data:
            return str(data.get("detail"))
        if "msg" in data:
            return str(data.get("msg"))
    return str(data)[:500]


def build_default_gateway() -> LLMGateway:
    gw = LLMGateway(public_base_url=settings.public_base_url, app_title="HeartOS")
    gw.set_provider_key("zhipu", settings.llm_zhipu_api_key)
    gw.set_provider_key("openrouter", settings.llm_openrouter_api_key)
    gw.set_provider_key("deepseek", settings.llm_deepseek_api_key)
    gw.set_provider_key("qwen", settings.llm_qwen_api_key)
    gw.set_provider_key("groq", settings.llm_groq_api_key)
    return gw
