from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ProviderName = Literal["openrouter", "deepseek", "zhipu", "qwen", "groq"]


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    provider: ProviderName = "zhipu"
    model: str = ""
    api_key: str = ""
    system: str = ""
    messages: list[ChatMessage]
    max_tokens: int = 1000
    temperature: float = 0.2


class ChatResponse(BaseModel):
    reply: str
    provider: ProviderName
    model: str
    request_id: str | None = None
    raw: dict[str, Any] | None = None
    user_id: str | None = None
    username: str | None = None


class AgentRunRequest(BaseModel):
    agent_id: str
    api_key: str = ""
    provider: ProviderName = "zhipu"
    model: str = ""
    messages: list[ChatMessage]
    max_tokens: int = 1000


class AgentRunResponse(BaseModel):
    agent_id: str
    reply: str
    provider: ProviderName
    model: str


class ECGOmicsAnalyzeRequest(BaseModel):
    inputType: Literal["raw", "xml"]
    ecgData: list[float] | None = None
    ecgSampleRate: int | None = Field(default=None, ge=1)
    xmlData: str | None = None
    zero: float = 0.0
    gain: float = 1.0
    filter: Literal["MeanDecomposition", "BandPass"] = "MeanDecomposition"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    display_name: str | None = None


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    display_name: str
    expires_in: int


class MeResponse(BaseModel):
    user_id: str
    username: str
    display_name: str


class HandEcgSaveRequest(BaseModel):
    user_id: str = ""
    image_base64: str = Field(min_length=1)
    image_mime: str = "image/png"
    image_name: str = ""
    file_name: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class AIEcgDigitizeRequest(BaseModel):
    image_base64: str = Field(min_length=1)
    image_mime: str = "image/png"
    image_name: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class AgentAutoRunRequest(BaseModel):
    message: str = Field(min_length=1)
    provider: ProviderName = "zhipu"
    model: str = ""
    api_key: str = ""
    max_tokens: int = 1000
    temperature: float = 0.2
    context: dict[str, Any] = Field(default_factory=dict)


class AgentAutoRunResponse(BaseModel):
    intent: str
    route: str
    target: str
    reply: str
    action: dict[str, Any] | None = None
