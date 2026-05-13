from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    _BASE_DIR = Path(__file__).resolve().parent.parent
    model_config = SettingsConfigDict(env_file=str(_BASE_DIR / '.env'), env_prefix='APP_', extra='ignore')

    name: str = Field(default="HeartOS Backend")
    env: str = Field(default="prod")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=9000)
    cors_origins: str = Field(default="*")
    http_timeout: int = Field(default=45)
    http_retries: int = Field(default=2)
    upload_dir: str = Field(default="./data/uploads")
    public_base_url: str = Field(default="http://127.0.0.1:9000")
    max_upload_mb: int = Field(default=20)
    ecgomics_url: str = Field(default="http://110.157.241.24:18023/ECGOmics")
    ai_ecg_digitize_url: str = Field(default="")
    ecg_reconstruct_url: str = Field(default="http://219.147.100.43:18007/reconstruct")
    llm_default_provider: str = Field(default="zhipu")
    llm_default_model: str = Field(default="glm-4-flash")
    llm_zhipu_api_key: str = Field(default="")
    llm_openrouter_api_key: str = Field(default="")
    llm_deepseek_api_key: str = Field(default="")
    llm_qwen_api_key: str = Field(default="")
    llm_groq_api_key: str = Field(default="")

    auth_secret: str = Field(default="heartos-dev-secret-change-me")
    auth_expire_hours: int = Field(default=24)
    users_file: str = Field(default="./data/users.json")
    default_username: str = Field(default="admin")
    default_password: str = Field(default="admin123")

    # local: 使用本地 users.json
    # upstream: 转发到外部账号服务（auth_upstream_base）
    auth_mode: str = Field(default="upstream")
    auth_upstream_base: str = Field(default="https://www.heartvoice.com.cn/dcs")
    auth_upstream_login_path: str = Field(default="/api/heartos/login")
    auth_upstream_register_path: str = Field(default="/api/heartos/register")

    # HandECG 数字化结果上传转发地址
    handecg_save_url: str = Field(default="https://www.heartvoice.com.cn/dcs/api/heartos/saveHandECG")

    @property
    def cors_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [x.strip() for x in raw.split(",") if x.strip()]

    @property
    def allow_credentials(self) -> bool:
        return "*" not in self.cors_list


settings = Settings()


