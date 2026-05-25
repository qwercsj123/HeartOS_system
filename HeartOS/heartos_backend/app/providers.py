from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderDef:
    name: str
    url: str
    default_model: str


PROVIDERS: dict[str, ProviderDef] = {
    "openrouter": ProviderDef(
        name="OpenRouter",
        url="https://openrouter.ai/api/v1/chat/completions",
        default_model="deepseek/deepseek-chat-v3-5",
    ),
    "deepseek": ProviderDef(
        name="DeepSeek",
        url="https://api.deepseek.com/v1/chat/completions",
        default_model="deepseek-chat",
    ),
    "zhipu": ProviderDef(
        name="Zhipu GLM",
        url="https://open.bigmodel.cn/api/paas/v4/chat/completions",
        default_model="glm-4-flash",
    ),
    "qwen": ProviderDef(
        name="Qwen",
        url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        default_model="qwen-turbo",
    ),
    "groq": ProviderDef(
        name="Groq",
        url="https://api.groq.com/openai/v1/chat/completions",
        default_model="llama-3.3-70b-versatile",
    ),
}


AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    "ml": "你是机器学习专家，专注医学数据分析与建模建议。",
    "dl": "你是深度学习专家，专注医学信号与图像任务。",
    "stats": "你是生物统计学专家，提供严谨统计推断建议。",
    "agent": "你是智能体架构师，输出可落地的执行方案。",
    "ecg": "你是心电图专家，进行 ECG 波形与临床指标分析。",
    "ecgd": "你是 ECG 数字化专家，擅长信号提取与质量控制。",
}
