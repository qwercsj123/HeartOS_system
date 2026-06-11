from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ProviderName = Literal["openrouter", "deepseek", "zhipu", "qwen", "groq"]


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[dict[str, Any]]

class ChatRequest(BaseModel):
    provider: ProviderName = "zhipu"
    model: str = ""
    api_key: str = ""
    system: str = ""
    messages: list[ChatMessage]
    max_tokens: int = 1000
    temperature: float = 0.2
    thinking: dict[str, Any] | None = None


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
    username: str = ""
    phone: str = ""
    password: str = Field(min_length=1)


class RegisterRequest(BaseModel):
    phone: str = Field(min_length=1)
    code: str = Field(min_length=4, max_length=10)
    verification_token: str | None = None
    name: str = Field(min_length=1)
    organization: str = Field(min_length=1)
    user_type: str = Field(min_length=1)
    use_case: str = Field(min_length=1)
    password: str = Field(min_length=1)
    department: str | None = None
    title: str | None = None
    email: str | None = None
    display_name: str | None = None


class LoginResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    phone: str = ""
    display_name: str
    name: str = ""
    organization: str = ""
    department: str = ""
    title: str = ""
    user_type: str = ""
    use_case: str = ""
    email: str = ""
    is_admin: bool = False
    expires_in: int


class MeResponse(BaseModel):
    user_id: str
    username: str
    display_name: str
    phone: str = ""
    name: str = ""
    organization: str = ""
    department: str = ""
    title: str = ""
    user_type: str = ""
    use_case: str = ""
    email: str = ""
    is_admin: bool = False


class SendCodeRequest(BaseModel):
    phone: str = Field(min_length=1)
    purpose: Literal["register", "reset_password"] = "register"


class SendCodeResponse(BaseModel):
    ok: bool = True
    expires_in: int
    retry_after: int
    debug_code: str = ""


class PasswordResetSendCodeRequest(BaseModel):
    phone: str = Field(min_length=1)


class PasswordResetConfirmRequest(BaseModel):
    phone: str = Field(min_length=1)
    code: str = Field(min_length=4, max_length=10)
    verification_token: str | None = None
    new_password: str = Field(min_length=1)


class PasswordResetVerifyRequest(BaseModel):
    phone: str = Field(min_length=1)
    code: str = Field(min_length=4, max_length=10)


class RegisterVerifyRequest(BaseModel):
    phone: str = Field(min_length=1)
    code: str = Field(min_length=4, max_length=10)


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = None
    name: str | None = None
    organization: str | None = None
    department: str | None = None
    title: str | None = None
    user_type: str | None = None
    use_case: str | None = None
    email: str | None = None


class UserAdminItem(BaseModel):
    user_id: str
    username: str
    phone: str = ""
    display_name: str
    name: str = ""
    organization: str = ""
    department: str = ""
    title: str = ""
    user_type: str = ""
    use_case: str = ""
    email: str = ""
    is_admin: bool = False
    is_super_admin: bool = False
    active: bool = True
    created_at: int = 0
    last_login_at: int = 0


class UserAdminListResponse(BaseModel):
    items: list[UserAdminItem] = Field(default_factory=list)


class FeedbackSubmitRequest(BaseModel):
    message: str = Field(min_length=5, max_length=5000)
    context: dict[str, Any] = Field(default_factory=dict)


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


class ConversationSource(BaseModel):
    source_id: str = ""
    name: str = ""
    type: str = ""
    checked: bool = False
    file_id: str = ""
    image_url: str = ""
    text: str = ""


class ConversationContext(BaseModel):
    conversation_id: str = ""
    message_id: str = ""
    sources: list[ConversationSource] = Field(default_factory=list)
    has_image: bool = False
    has_xml: bool = False
    has_csv: bool = False
    has_ecg_signal: bool = False
    selected_source_ids: list[str] = Field(default_factory=list)
    last_tool_intent: str = ""
    last_tool_result_id: str = ""
    last_result_kind: str = ""


class ConversationClientHint(BaseModel):
    intent: str = ""
    source_type: str = ""


class ConversationTurnRequest(BaseModel):
    message: str = Field(min_length=1)
    provider: ProviderName = "zhipu"
    model: str = ""
    api_key: str = ""
    max_tokens: int = 1000
    temperature: float = 0.2
    context: ConversationContext = Field(default_factory=ConversationContext)
    history: list[ChatMessage] = Field(default_factory=list)
    client_hint: ConversationClientHint = Field(default_factory=ConversationClientHint)


class ConversationConfirmRequest(BaseModel):
    intent: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    provider: ProviderName = "zhipu"
    model: str = ""
    api_key: str = ""
    context: ConversationContext = Field(default_factory=ConversationContext)
    history: list[ChatMessage] = Field(default_factory=list)


class ConversationResponse(BaseModel):
    type: Literal["tool_result", "need_confirm", "ask_missing", "chat", "plan_options"]
    stage: str = ""
    intent: str = ""
    confidence: float = 0.0
    message: str
    description: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    action: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None
