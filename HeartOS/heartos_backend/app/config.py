from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_DEV_CORS_ORIGINS = (
    "http://127.0.0.1:8080",
    "http://localhost:8080",
    "http://127.0.0.1:8081",
    "http://localhost:8081",
)


class Settings(BaseSettings):
    _BASE_DIR = Path(__file__).resolve().parent.parent
    model_config = SettingsConfigDict(env_file=str(_BASE_DIR / '.env.server.example'), env_prefix='APP_', extra='ignore')  # 线上使用
    # model_config = SettingsConfigDict(env_file=str(_BASE_DIR / '.env.local.example'), env_prefix='APP_', extra='ignore')  # 本地使用

    name: str
    env: str
    host: str
    port: str
    cors_origins: str
    http_timeout: int
    http_retries: int
    upload_dir: str
    public_base_url: str
    max_upload_mb: int
    ecgomics_url: str
    ai_ecg_digitize_url: str
    ecg_reconstruct_url: str
    chest_pain_predict_url: str
    impute_ecg_save_url: str
    llm_default_provider: str
    llm_default_model: str
    llm_zhipu_api_key: str = Field(default="")
    llm_openrouter_api_key: str = Field(default="")
    llm_deepseek_api_key: str = Field(default="")
    llm_qwen_api_key: str = Field(default="")
    llm_groq_api_key: str = Field(default="")

    auth_secret: str
    auth_expire_hours: int
    users_file: str
    default_username: str
    default_password: str
    default_admin_phone: str

    # local: 使用本地 users.json
    # upstream: 转发到外部账号服务（auth_upstream_base）
    auth_mode: str
    auth_upstream_base: str
    auth_upstream_login_path: str
    auth_upstream_register_path: str
    auth_upstream_reset_password_path: str = Field(default="/api/heartos/reset_password")
    phone_send_code_url: str
    phone_login_by_code_url: str

    # HandECG 数字化结果上传转发地址
    handecg_save_url: str

    @property
    def cors_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        values = [x.strip() for x in raw.split(",") if x.strip()]
        seen = set(values)
        for origin in LOCAL_DEV_CORS_ORIGINS:
            if origin not in seen:
                values.append(origin)
                seen.add(origin)
        return values

    @property
    def allow_credentials(self) -> bool:
        return "*" not in self.cors_list


settings = Settings()
