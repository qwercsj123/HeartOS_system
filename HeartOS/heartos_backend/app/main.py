from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import mimetypes
import re
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from .auth import (
    UpstreamAuthError,
    change_password,
    get_user_by_id,
    issue_token,
    list_users_for_admin,
    register_user,
    reset_password,
    send_password_reset_code,
    send_verification_code,
    issue_verification_ticket,
    update_profile,
    upstream_login,
    upstream_register,
    verify_registration_code,
    verify_password_reset_code,
    verify_token,
    verify_user,
)
from .chest_pain import predict_image_and_report
from .config import settings

from .llm import build_default_gateway
from .providers import AGENT_SYSTEM_PROMPTS
from .schemas import (
    AIEcgDigitizeRequest,
    AgentAutoRunRequest,
    AgentAutoRunResponse,
    AgentRunRequest,
    AgentRunResponse,
    ChatRequest,
    ChatResponse,
    ConversationConfirmRequest,
    ConversationResponse,
    ConversationTurnRequest,
    ECGOmicsAnalyzeRequest,
    HandEcgSaveRequest,
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetSendCodeRequest,
    PasswordResetVerifyRequest,
    ProfileUpdateRequest,
    RegisterVerifyRequest,
    RegisterRequest,
    MeResponse,
    SendCodeRequest,
    SendCodeResponse,
    UserAdminListResponse,
)


APP_VERSION = "1.4.1"
PLATFORM_NAME = "HeartOS"
DEFAULT_CHAT_SYSTEM = (
    "我是 HeartOS 的智能助手，可协助完成心电图数字化、ECG 心电组学、信号补全和心电数据分析。"
    "当用户询问“你是谁”“你是做什么的”时，请优先使用这句身份介绍，并围绕 HeartOS 的心电相关能力展开回答；"
    "不要提及底层模型、第三方厂商或外部品牌。"
    "回答使用中文，风格专业、清晰、可信。"
)
IDENTITY_REPLY = "我是 HeartOS 的智能助手，可协助完成心电图数字化、ECG 心电组学、信号补全和心电数据分析。"
CAPABILITY_REPLY = "HeartOS 主要支持心电图数字化、ECG 心电组学、信号补全和相关心电数据分析。"
PLATFORM_REPLY = "HeartOS 是一个面向心电数据处理与分析的平台，支持心电图数字化、心电组学分析、信号补全和相关分析任务。"
GUIDE_REPLY = "你可以从上传心电图图片、PDF、XML 或波形 CSV 开始；图片和 PDF 适合数字化，XML 或 CSV 适合心电组学分析与信号补全。"
INPUT_REPLY = "HeartOS 目前支持心电图图片、PDF、ECG XML、CSV，以及部分 TSV、TXT、JSON 等波形数据文件。"
DIFF_REPLY = "手动数字化适合精细框选和人工校正，自动数字化适合快速处理标准心电图图片；如果自动结果不理想，建议切换到手动数字化。"
FEATURE_REPLY = (
    "可以把它们理解成两个不同阶段："
    "ECG 信号补全是先补数据，适合导联缺失、波形不完整，或者当前波形还不满足分析条件的时候使用；"
    "ECG 心电组学是再读结果，在已有波形上提取心率、节律、间期和形态学等结构化指标。"
    "一般来说，波形已经比较完整时，优先做心电组学分析；如果数据不完整，先做信号补全会更合适。"
)
BOUNDARY_REPLY = "HeartOS 可以协助完成心电数据处理与分析，但不能替代医生作出临床诊断。"
CONVERSATION_INTENT_DESCRIPTIONS: dict[str, str] = {
    "ecg_auto_digitize": "将心电图图片或 PDF 自动数字化为波形数据。",
    "ecg_manual_digitize": "打开手动数字化工具，人工校正和提取波形。",
    "ecg_feature_extract": "对 ECG XML、CSV 或波形数据进行 ECG 心电组学分析。",
    "ecg_reconstruct": "对缺失导联或不完整 ECG 波形进行信号补全与重建。",
    "zhunxin_risk_assess": "基于心电图图片进行准心胸痛高风险评估，并生成风险报告。",
    "result_interpretation": "解释已有分析结果、波形表现或 ECG 指标含义。",
    "knowledge_qa": "回答 HeartOS 能力、流程、原理或一般性 ECG 相关问题。",
    "chat": "普通对话或无法明确分类的问题。",
}
CONVERSATION_INTENT_TO_ACTION: dict[str, dict[str, Any]] = {
    "ecg_auto_digitize": {"tool": "ecgsmart", "endpoint": "/api/ai-ecg-digitize", "method": "POST"},
    "ecg_manual_digitize": {"tool": "ecg", "endpoint": "/tool/handecg/manual", "method": "OPEN"},
    "ecg_feature_extract": {"tool": "ecgd", "endpoint": "/api/ecgomics/analyze", "method": "POST"},
    "ecg_reconstruct": {"tool": "ecgrecon", "endpoint": "/api/ecg-reconstruct", "method": "POST"},
    "zhunxin_risk_assess": {"tool": "zhunxin", "endpoint": "/api/chest-pain/predict", "method": "POST"},
}
CONVERSATION_ROUTER_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "ecg_auto_digitize",
            "description": "当用户明确要把心电图图片或 PDF 自动数字化为波形 CSV、提取图像中的心电波形时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number", "description": "0到1之间的置信度"},
                    "reason": {"type": "string", "description": "简短说明为什么判断为该意图"},
                    "source_ids": {"type": "array", "items": {"type": "string"}, "description": "与该任务相关的已选来源ID"},
                    "missing_fields": {"type": "array", "items": {"type": "string"}, "description": "若缺少输入则列出，例如 selected_source 或 image_source"},
                },
                "required": ["confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ecg_manual_digitize",
            "description": "当用户明确要打开手动数字化工具、手工校正心电图波形时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ecg_feature_extract",
            "description": "当用户要对 ECG XML、CSV、TXT、TSV、JSON 波形数据做 ECG 心电组学或 ECGOmics 分析时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ecg_reconstruct",
            "description": "当用户要对缺失导联、不完整波形进行心电信号补全、重建、reconstruct时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "zhunxin_risk_assess",
            "description": "当用户明确希望根据心电图图片评估高风险疾病、看看有没有病、有没有明显异常、做准心胸痛风险评估时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "result_interpretation",
            "description": "当用户是在问已有结果怎么看、结果说明什么、波形意味着什么，而不是要求重新执行工具时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_qa",
            "description": "当用户是在问 HeartOS 能做什么、功能介绍、如何使用、概念原理或一般性说明时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["confidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat",
            "description": "当以上都不明确匹配、只是普通闲聊或无法可靠判定时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["confidence"],
            },
        },
    },
]

app = FastAPI(title=settings.name, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=settings.allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path(settings.upload_dir).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = max(1, settings.max_upload_mb) * 1024 * 1024
FILE_META_PATH = (UPLOAD_DIR / ".meta.json").resolve()
LLM_GATEWAY = build_default_gateway()

NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
AI_CONFIG_PATH = (Path(settings.users_file).resolve().parent / "ai_configs.json").resolve()
NOTEBOOKS_PATH = (Path(settings.users_file).resolve().parent / "notebooks.json").resolve()
NOTEBOOK_SOURCES_PATH = (Path(settings.users_file).resolve().parent / "notebook_sources.json").resolve()
FEEDBACK_PATH = (Path(settings.users_file).resolve().parent / "feedback.json").resolve()
FEEDBACK_MAX_IMAGES = 4
FEEDBACK_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}


def _load_file_meta() -> dict[str, Any]:
    if not FILE_META_PATH.exists():
        return {}
    try:
        return json.loads(FILE_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_file_meta(meta: dict[str, Any]) -> None:
    FILE_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _save_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _normalize_source_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    source_id = str(raw.get("source_id") or raw.get("id") or "").strip()
    if not source_id:
        return None
    item = {
        "id": source_id,
        "source_id": source_id,
        "name": str(raw.get("name") or "未命名来源"),
        "type": str(raw.get("type") or "file").lower(),
        "checked": bool(raw.get("checked")),
    }
    for key in (
        "fileId",
        "serverFileId",
        "fileUrl",
        "content",
        "imageDataUrl",
        "sampleRate",
        "sampleRateSource",
        "generatedBy",
        "source",
        "sourceName",
        "parentSourceId",
        "__fromDigitize",
        "__fromEcgomics",
        "__fromReconstruct",
        "finding",
        "finding_text",
        "mime",
    ):
        if key in raw:
            item[key] = raw.get(key)
    return item


def _load_notebook_sources_db() -> dict[str, Any]:
    return _load_json_file(NOTEBOOK_SOURCES_PATH)


def _save_notebook_sources_db(payload: dict[str, Any]) -> None:
    _save_json_file(NOTEBOOK_SOURCES_PATH, payload)


def _get_user_notebook_sources(uid: str) -> dict[str, list[dict[str, Any]]]:
    db = _load_notebook_sources_db()
    bucket = db.get(uid, {})
    return bucket if isinstance(bucket, dict) else {}


def _get_conversation_sources(uid: str, notebook_id: str) -> list[dict[str, Any]]:
    bucket = _get_user_notebook_sources(uid)
    items = bucket.get(str(notebook_id), [])
    return items if isinstance(items, list) else []


def _set_conversation_sources(uid: str, notebook_id: str, sources: list[dict[str, Any]]) -> None:
    db = _load_notebook_sources_db()
    bucket = db.get(uid, {})
    if not isinstance(bucket, dict):
        bucket = {}
    bucket[str(notebook_id)] = sources
    db[uid] = bucket
    _save_notebook_sources_db(db)


def _delete_conversation_sources(uid: str, notebook_id: str) -> None:
    db = _load_notebook_sources_db()
    bucket = db.get(uid, {})
    if isinstance(bucket, dict) and str(notebook_id) in bucket:
        del bucket[str(notebook_id)]
        db[uid] = bucket
        _save_notebook_sources_db(db)


def _coerce_feedback_context(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _guess_upload_suffix(filename: str, content_type: str, *, default: str = ".bin") -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    guessed = (mimetypes.guess_extension(content_type or "") or "").lower()
    if guessed == ".jpe":
        guessed = ".jpg"
    return guessed or default


def _store_upload_bytes(
    *,
    content: bytes,
    filename: str,
    content_type: str,
    source: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    suffix = _guess_upload_suffix(filename, content_type)
    fid = uuid.uuid4().hex
    safe_name = f"{fid}{suffix}"
    out_path = UPLOAD_DIR / safe_name
    out_path.write_bytes(content)
    meta = _load_file_meta()
    meta[safe_name] = {
        "user_id": user.get("id"),
        "username": user.get("username"),
        "source": source,
        "content_type": content_type,
        "original_name": filename,
    }
    _save_file_meta(meta)
    url = f"{settings.public_base_url}/api/files/{safe_name}"
    return {
        "id": safe_name,
        "name": filename or safe_name,
        "size": len(content),
        "source": source,
        "url": url,
        "fileUrl": url,
        "contentType": content_type,
        "user_id": user.get("id"),
        "username": user.get("username"),
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        sub = s[start : end + 1]
        try:
            obj = json.loads(sub)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _fallback_chest_pain_summary(result: dict[str, Any]) -> dict[str, Any]:
    high_risk = [str(item) for item in (result.get("high_risk") or []) if str(item).strip()]
    low_risk = [str(item) for item in (result.get("low_risk") or []) if str(item).strip()]
    ranking = result.get("ranking") if isinstance(result.get("ranking"), list) else []
    focus_items = [
        str(item.get("class_name") or "").strip()
        for item in ranking[:2]
        if isinstance(item, dict) and str(item.get("class_name") or "").strip()
    ]
    if high_risk:
        headline = "发现需要尽快关注的高危提示"
        plain_summary = "当前结果提示需要优先排查：" + "、".join(high_risk) + "。这不等同于临床诊断，但建议尽快结合症状就医。"
        next_step = "如果你现在有持续胸痛、胸闷、呼吸困难、出汗或明显不适，请尽快前往医院进一步检查。"
        risk_level = "high"
    else:
        headline = "当前未发现明显高危提示"
        if focus_items:
            plain_summary = "本次六分类筛查已经完成。当前没有出现明确的高危提示，但模型相对更关注：" + "、".join(focus_items) + "。"
        else:
            plain_summary = "本次六分类筛查已经完成。当前没有出现明确的高危提示。"
        if low_risk:
            plain_summary += " 当前模型未重点提示：" + "、".join(low_risk) + "。"
        next_step = "如果你只是做常规筛查，可以继续结合症状和医生意见判断；如果目前有明显不适，即使这里没有高危提示，也建议尽快就医。"
        risk_level = "low"
    return {
        "headline": headline,
        "plain_summary": plain_summary,
        "next_step": next_step,
        "risk_level": risk_level,
        "focus_items": focus_items,
        "reassuring_items": low_risk,
        "disclaimer": "这份结果用于辅助风险提示，不替代医生诊断或临床最终判断。",
    }


async def _summarize_chest_pain_result_with_llm(
    *,
    result: dict[str, Any],
    user: dict[str, Any],
) -> dict[str, Any]:
    fallback = _fallback_chest_pain_summary(result)
    if not (settings.llm_zhipu_api_key or "").strip():
        fallback["summary_source"] = "fallback"
        return fallback

    ranking = result.get("ranking") if isinstance(result.get("ranking"), list) else []
    compact_ranking: list[dict[str, Any]] = []
    for item in ranking[:6]:
        if isinstance(item, dict):
            compact_ranking.append(
                {
                    "class_name": str(item.get("class_name") or ""),
                    "score": item.get("score"),
                }
            )
    payload = {
        "high_risk": result.get("high_risk") or [],
        "low_risk": result.get("low_risk") or [],
        "ranking": compact_ranking,
        "report": str(result.get("report") or ""),
    }
    system = (
        "你是 HeartOS 的医学结果解释助手。"
        "你的任务是把心电图胸痛风险六分类结果，翻译成普通用户能看懂的中文。"
        "不能把它写成临床确诊，不能承诺没病，只能说风险提示、优先排查方向和下一步建议。"
        "不要输出概率、分数、阈值、模型排序术语。"
        "请只输出 JSON，不要输出任何额外说明。"
        "JSON 字段固定为：headline, plain_summary, next_step, risk_level, focus_items, reassuring_items, disclaimer。"
        "其中 risk_level 只能是 high、attention、low、uncertain。"
        "headline 要非常短，适合做页面标题。"
        "plain_summary 用 2 到 3 句中文说明这次结果是什么意思。"
        "next_step 只给一段面向用户的下一步建议。"
        "focus_items 和 reassuring_items 都是字符串数组，最多 3 项。"
        "disclaimer 用一句简短提醒，强调不能替代医生诊断。"
    )
    try:
        out = await LLM_GATEWAY.chat(
            provider_key="zhipu",
            model=settings.llm_default_model or "glm-4-flash",
            system=system,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            max_tokens=420,
            temperature=0.2,
            user=user,
            timeout_seconds=min(settings.http_timeout, 25),
            retries=settings.http_retries,
        )
        parsed = _extract_json_object(out.get("reply", ""))
        if not parsed:
            fallback["summary_source"] = "fallback"
            return fallback
        summary = {
            "headline": str(parsed.get("headline") or fallback["headline"]).strip() or fallback["headline"],
            "plain_summary": str(parsed.get("plain_summary") or fallback["plain_summary"]).strip() or fallback["plain_summary"],
            "next_step": str(parsed.get("next_step") or fallback["next_step"]).strip() or fallback["next_step"],
            "risk_level": str(parsed.get("risk_level") or fallback["risk_level"]).strip().lower() or fallback["risk_level"],
            "focus_items": [str(x).strip() for x in (parsed.get("focus_items") or []) if str(x).strip()][:3],
            "reassuring_items": [str(x).strip() for x in (parsed.get("reassuring_items") or []) if str(x).strip()][:3],
            "disclaimer": str(parsed.get("disclaimer") or fallback["disclaimer"]).strip() or fallback["disclaimer"],
            "summary_source": "zhipu",
        }
        if summary["risk_level"] not in {"high", "attention", "low", "uncertain"}:
            summary["risk_level"] = fallback["risk_level"]
        if not summary["focus_items"]:
            summary["focus_items"] = list(fallback.get("focus_items") or [])
        if not summary["reassuring_items"]:
            summary["reassuring_items"] = list(fallback.get("reassuring_items") or [])
        return summary
    except Exception:
        fallback["summary_source"] = "fallback"
        return fallback


def _is_identity_question(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    normalized = re.sub(r"\s+", "", text)
    patterns = (
        "你是谁",
        "您是谁",
        "你是做什么的",
        "您是做什么的",
        "介绍一下你自己",
        "介绍下你自己",
        "你叫什么",
        "你叫啥",
        "whoareyou",
        "whatdoyoudo",
    )
    return any(p in normalized for p in patterns)


def _match_canned_reply(message: str) -> str:
    text = (message or "").strip().lower()
    if not text:
        return ""
    normalized = re.sub(r"\s+", "", text)
    if _is_identity_question(message):
        return IDENTITY_REPLY
    capability_patterns = (
        "你能做什么", "您能做什么", "你可以做什么", "你有哪些功能", "你支持什么", "能做什么", "支持什么功能",
    )
    platform_patterns = (
        "这个平台是干什么的", "heartos是什么", "介绍一下平台", "介绍下平台", "平台是做什么的", "heartos是做什么的",
    )
    guide_patterns = (
        "我该怎么用你", "怎么开始", "如何开始", "怎么使用", "如何使用", "怎么用", "使用方法",
    )
    input_patterns = (
        "支持哪些数据类型", "支持什么文件", "能上传什么文件", "支持哪些文件", "支持什么格式", "上传什么",
    )
    diff_patterns = (
        "手动数字化和自动数字化有什么区别", "手动数字化和自动数字化区别", "手动和自动数字化有什么区别", "手动和自动数字化区别",
    )
    feature_patterns = (
        "特征提取是做什么的", "信号补全是做什么的", "特征提取和信号补全分别是做什么的",
        "特征提取和信号补全有什么区别", "信号补全和特征提取有什么区别", "ecg特征提取是做什么的",
    )
    boundary_patterns = (
        "你是不是医生", "您是不是医生", "你能不能诊断", "能不能诊断", "能做诊断吗", "你是不是专家",
    )
    if any(p in normalized for p in capability_patterns):
        return CAPABILITY_REPLY
    if any(p in normalized for p in platform_patterns):
        return PLATFORM_REPLY
    if any(p in normalized for p in guide_patterns):
        return GUIDE_REPLY
    if any(p in normalized for p in input_patterns):
        return INPUT_REPLY
    if any(p in normalized for p in diff_patterns):
        return DIFF_REPLY
    if any(p in normalized for p in feature_patterns):
        return FEATURE_REPLY
    if any(p in normalized for p in boundary_patterns):
        return BOUNDARY_REPLY
    return ""


def _normalize_route_intent(raw_intent: str) -> str:
    intent = (raw_intent or "").strip().lower()
    alias_map = {
        "ecg_digitize": "ecg_auto_digitize",
        "ecg_auto_digitize": "ecg_auto_digitize",
        "ecg_manual_digitize": "ecg_manual_digitize",
        "ecgomics_analyze": "ecg_feature_extract",
        "ecg_feature_extract": "ecg_feature_extract",
        "ecg_reconstruct": "ecg_reconstruct",
        "zhunxin_risk_assess": "zhunxin_risk_assess",
        "result_interpretation": "result_interpretation",
        "knowledge_qa": "knowledge_qa",
        "chat": "chat",
        "smalltalk": "chat",
    }
    return alias_map.get(intent, "chat")


def _context_source_stats(context: dict[str, Any]) -> dict[str, Any]:
    ctx = context or {}
    sources = ctx.get("sources") or []
    selected = [src for src in sources if isinstance(src, dict) and src.get("checked")]
    selected_ids = [str(src.get("source_id") or src.get("file_id") or "") for src in selected if (src.get("source_id") or src.get("file_id"))]
    return {
        "selected_count": len(selected),
        "selected_source_ids": selected_ids,
        "has_image": bool(ctx.get("has_image")),
        "has_xml": bool(ctx.get("has_xml")),
        "has_csv": bool(ctx.get("has_csv")),
        "has_ecg_signal": bool(ctx.get("has_ecg_signal")),
    }


def _normalized_message_text(message: str) -> str:
    return (message or "").strip().lower()


def _looks_like_capability_question(message: str) -> bool:
    msg_text = _normalized_message_text(message)
    if not msg_text:
        return False
    if "?" not in msg_text and "？" not in msg_text and "吗" not in msg_text:
        return False
    capability_patterns = (
        "可以",
        "可不可以",
        "能不能",
        "能否",
        "是否可以",
        "能",
    )
    topic_patterns = (
        "分析",
        "做",
        "处理",
        "数字化",
        "提取",
        "补全",
        "重建",
        "评估",
        "使用",
        "识别",
    )
    return any(pattern in msg_text for pattern in capability_patterns) and any(
        pattern in msg_text for pattern in topic_patterns
    )


def _looks_like_explicit_action_request(message: str) -> bool:
    msg_text = _normalized_message_text(message)
    if not msg_text or _looks_like_capability_question(message):
        return False
    action_patterns = (
        "帮我",
        "请帮我",
        "请",
        "开始",
        "执行",
        "运行",
        "处理",
        "生成",
        "提取",
        "补全",
        "重建",
        "数字化",
        "评估一下",
        "分析一下",
        "打开",
        "来个",
        "做一下",
    )
    return any(pattern in msg_text for pattern in action_patterns)


def _capability_question_target_intent(message: str, context: dict[str, Any]) -> str:
    msg_text = _normalized_message_text(message)
    if not msg_text or not _looks_like_capability_question(message):
        return ""
    has_image = bool((context or {}).get("has_image"))
    has_waveform = bool((context or {}).get("has_ecg_signal")) or bool((context or {}).get("has_xml")) or bool((context or {}).get("has_csv"))
    if ("自动数字化" in msg_text or "自动提取波形" in msg_text) and has_image:
        return "ecg_auto_digitize"
    if ("手动数字化" in msg_text or "手动提取波形" in msg_text) and has_image:
        return "ecg_manual_digitize"
    if any(pattern in msg_text for pattern in ("风险评估", "准心", "看看有没有病", "有没有异常", "有没有风险")) and has_image:
        return "zhunxin_risk_assess"
    if any(pattern in msg_text for pattern in ("心电组学", "ecgomics", "特征提取", "提取特征")) and has_waveform:
        return "ecg_feature_extract"
    if any(pattern in msg_text for pattern in ("补全", "重建", "reconstruct")) and has_waveform:
        return "ecg_reconstruct"
    return ""


def _is_explicit_zhunxin_action(message: str) -> bool:
    msg_text = _normalized_message_text(message)
    if not msg_text:
        return False
    explicit_patterns = (
        "开始准心评估",
        "开始风险评估",
        "做准心评估",
        "做风险评估",
        "重新评估",
        "再评估一次",
        "再做一次评估",
        "生成风险报告",
        "做胸痛风险评估",
        "准心评估一下",
        "帮我评估",
        "请评估",
        "筛查一下",
        "重新筛查",
        "看看有没有病",
        "看有没有病",
        "检查有没有病",
    )
    return any(pattern in msg_text for pattern in explicit_patterns)


def _looks_like_result_followup_question(message: str) -> bool:
    msg_text = _normalized_message_text(message)
    if not msg_text:
        return False
    followup_patterns = (
        "结果",
        "怎么看",
        "说明什么",
        "意味着什么",
        "靠谱吗",
        "解读",
        "什么意思",
        "危险吗",
        "严重吗",
        "要紧吗",
        "高吗",
        "低吗",
        "中风险",
        "高风险",
        "低风险",
        "风险高吗",
        "风险大吗",
        "需不需要去医院",
        "要不要去医院",
        "要不要紧",
    )
    return any(pattern in msg_text for pattern in followup_patterns)


def _conversation_history_payload(history: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in history[-8:]:
        role = getattr(item, "role", None) or (item.get("role") if isinstance(item, dict) else "")
        content = getattr(item, "content", None) if not isinstance(item, dict) else item.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")
        if role in {"user", "assistant"} and text:
            out.append({"role": str(role), "content": text[:1200]})
    return out


def _latest_assistant_text(history: list[Any]) -> str:
    for item in reversed(history[-8:]):
        role = getattr(item, "role", None) or (item.get("role") if isinstance(item, dict) else "")
        content = getattr(item, "content", None) if not isinstance(item, dict) else item.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if role == "assistant" and text:
            return text[:3000]
    return ""


def _last_result_kind(context: dict[str, Any]) -> str:
    return str((context or {}).get("last_result_kind") or "").strip().lower()


def _source_brief_payload(context: dict[str, Any]) -> list[dict[str, Any]]:
    briefs: list[dict[str, Any]] = []
    for src in (context or {}).get("sources") or []:
        if not isinstance(src, dict):
            continue
        briefs.append(
            {
                "source_id": str(src.get("source_id") or src.get("file_id") or ""),
                "name": str(src.get("name") or ""),
                "type": str(src.get("type") or ""),
                "checked": bool(src.get("checked")),
                "text": str(src.get("text") or "")[:400],
            }
        )
    return briefs[:12]


def _missing_fields_for_intent(intent: str, context: dict[str, Any]) -> list[str]:
    stats = _context_source_stats(context)
    if intent in {"ecg_auto_digitize", "ecg_manual_digitize", "zhunxin_risk_assess"}:
        if not stats["selected_count"]:
            return ["selected_source"]
        if not stats["has_image"]:
            return ["image_source"]
    if intent in {"ecg_feature_extract", "ecg_reconstruct", "result_interpretation"}:
        if not stats["selected_count"]:
            return ["selected_source"]
        if not stats["has_ecg_signal"] and not stats["has_xml"] and not stats["has_csv"]:
            return ["ecg_signal_source"]
    return []


def _message_for_missing_fields(intent: str, missing_fields: list[str]) -> str:
    if "selected_source" in missing_fields:
        return "请先选择来源文件，再告诉我要执行哪一步。图片或 PDF 适合数字化，XML/CSV 波形适合心电组学分析、补全和结果解读。"
    if "image_source" in missing_fields:
        if intent == "zhunxin_risk_assess":
            return "准心风险评估需要心电图图片或 PDF。请选择单个心电图图片或 PDF 后再试。"
        return "当前选中的来源不适合数字化。请选择单个心电图图片或 PDF 后再试。"
    if "ecg_signal_source" in missing_fields:
        return "当前选中的来源不适合 ECG 心电组学、补全或结果解读。请选择 XML、CSV、TXT、TSV 或 JSON 波形数据。"
    return "还缺少执行该任务所需的信息，请补充后再试。"


def _ask_missing_meta(intent: str, context: dict[str, Any], missing_fields: list[str]) -> dict[str, Any]:
    stats = _context_source_stats(context)
    has_selected = bool(stats["selected_count"])
    if "selected_source" in missing_fields:
        return {
            "style": "guided",
            "headline": "还差一步就能继续",
            "body": "我可以帮你完成这项任务。先选择一个来源文件，我再继续处理。",
            "tips": [
                "心电图图片或 PDF 适合先做数字化。",
                "XML、CSV、TXT、TSV、JSON 波形文件适合直接做心电组学分析、补全或结果解读。",
            ],
            "actions": [
                {"label": "去上传文件", "kind": "primary", "action": "upload"},
                {"label": "去选择文件", "kind": "secondary", "action": "toggle_sources"},
            ],
        }
    if "image_source" in missing_fields:
        if intent == "zhunxin_risk_assess":
            return {
                "style": "guided",
                "headline": "准心风险评估需要心电图图片",
                "body": "我可以帮你生成准心风险报告，但需要先选中一张心电图图片或一个 PDF。",
                "tips": [
                    "建议只选择 1 个来源进行本次评估。",
                    "如果你已经有波形 CSV/XML，更适合走心电组学分析或结果解读流程。",
                ],
                "actions": [
                    {"label": "去上传文件", "kind": "primary", "action": "upload"},
                    {"label": "去选择文件", "kind": "secondary", "action": "toggle_sources"},
                ],
            }
        return {
            "style": "guided",
            "headline": "这一步需要心电图图片或 PDF",
            "body": "你当前选中的不是可直接数字化的来源。我可以在你换成图片或 PDF 后继续帮你处理。",
            "tips": [
                "自动数字化更适合标准心电图图片。",
                "如果图片复杂或识别不准，也可以改用手动数字化。",
            ],
            "actions": [
                {"label": "去上传文件", "kind": "primary", "action": "upload"},
                {"label": "去选择文件", "kind": "secondary", "action": "toggle_sources"},
            ],
        }
    if "ecg_signal_source" in missing_fields:
        if stats["has_image"]:
            return {
                "style": "guided",
                "headline": "我理解你想分析这张心电图",
                "body": "当前选中的是图片或 PDF，还不能直接做特征分析。我可以先帮你数字化成波形数据，再继续分析。",
                "tips": [
                    "数字化后会生成 CSV 波形数据。",
                    "生成的波形文件加入来源后，就可以继续做心电组学分析、补全或结果解读。",
                ],
                "actions": [
                    {"label": "先自动数字化", "kind": "primary", "action": "run:ecgsmart"},
                    {"label": "改用手动数字化", "kind": "secondary", "action": "run:ecg"},
                ],
            }
        return {
            "style": "guided",
            "headline": "这一步需要波形数据文件",
            "body": "我可以继续帮你分析，但需要先选中可读取的 ECG 波形数据。",
            "tips": [
                "支持 XML、CSV、TXT、TSV、JSON 波形文件。",
                "如果你手上只有图片或 PDF，可以先做数字化再回来继续。",
            ],
            "actions": [
                {"label": "去上传文件", "kind": "primary", "action": "upload"},
                {"label": "先自动数字化", "kind": "secondary", "action": "run:ecgsmart"},
            ],
        }
    return {
        "style": "guided",
        "headline": "还需要补充一点信息",
        "body": _message_for_missing_fields(intent, missing_fields),
        "tips": [],
        "actions": [{"label": "去上传文件", "kind": "primary", "action": "upload"}],
    }


def _looks_like_disease_check_goal(message: str) -> bool:
    text = re.sub(r"\s+", "", (message or "").strip().lower())
    if not text:
        return False
    if "准心" in text or "风险评估" in text:
        return False
    check_patterns = (
        "有没有病",
        "有没有问题",
        "有没有异常",
        "是否异常",
        "有没有风险",
        "严不严重",
        "看有没有病",
        "帮我看看",
        "帮我看看心电图",
        "帮我看看我的心电图",
        "风险评估",
    )
    ecg_patterns = ("心电", "心电图", "ecg")
    return any(p in text for p in check_patterns) and any(p in text for p in ecg_patterns)


def _build_diagnostic_plan_response(context: dict[str, Any]) -> ConversationResponse:
    selected_ids = _context_source_stats(context).get("selected_source_ids", [])
    return _build_conversation_response(
        response_type="plan_options",
        stage="planned",
        intent="zhunxin_risk_assess",
        confidence=0.9,
        message="我理解你想看看这张心电图是否存在明显异常。我可以先给你几个处理方案，你选一个我再开始。",
        description="目标识别：判断心电图是否存在明显风险或异常。",
        args={"source_ids": selected_ids},
        meta={
            "headline": "我建议这样处理",
            "body": "这类问题通常不是单一步骤。我可以先做风险评估，也可以先把波形提出来再继续深入分析。",
            "options": [
                {
                    "id": "zhunxin",
                    "title": "先做准心风险评估",
                    "description": "直接基于当前心电图图片生成高风险/低风险排序和报告，最快得到初步判断。",
                    "action": "run:zhunxin",
                    "kind": "primary",
                },
                {
                    "id": "ecgsmart",
                    "title": "先自动数字化再分析",
                    "description": "先把图片转成波形 CSV，后续可以继续做心电组学分析、补全和更细致的解读。",
                    "action": "run:ecgsmart",
                    "kind": "secondary",
                },
                {
                    "id": "ecg",
                    "title": "手动精细处理",
                    "description": "适合图片复杂、自动识别可能不稳时，先手动校正再继续分析。",
                    "action": "run:ecg",
                    "kind": "secondary",
                },
            ],
        },
    )


def _build_capability_options_response(context: dict[str, Any]) -> ConversationResponse:
    stats = _context_source_stats(context)
    selected_ids = stats.get("selected_source_ids", [])
    if stats["has_image"]:
        return _build_conversation_response(
            response_type="plan_options",
            stage="planned",
            intent="knowledge_qa",
            confidence=0.92,
            message="这张图片可以继续处理。我整理了几个可选操作，已经为你准备成选项。",
            description="面向心电图图片/PDF的下一步处理建议。",
            args={"source_ids": selected_ids},
            meta={
                "headline": "这张心电图可以这样处理",
                "body": "你可以先做快速风险判断，也可以先提取波形，再进入更深入的 ECG 分析流程。",
                "presentation": "popover",
                "options": [
                    {
                        "id": "zhunxin",
                        "title": "准心风险评估",
                        "description": "直接基于当前图片做初步风险筛查，适合先快速判断是否存在明显异常风险。",
                        "action": "run:zhunxin",
                        "kind": "primary",
                    },
                    {
                        "id": "ecgsmart",
                        "title": "自动数字化提取波形",
                        "description": "把图片转换成波形 CSV，方便后续继续做心电组学分析、补全和结果解读。",
                        "action": "run:ecgsmart",
                        "kind": "secondary",
                    },
                    {
                        "id": "ecg",
                        "title": "手动数字化精细处理",
                        "description": "适合图片复杂、自动识别可能不稳的情况，可以人工校正后再继续分析。",
                        "action": "run:ecg",
                        "kind": "secondary",
                    },
                ],
            },
        )
    if stats["has_ecg_signal"] or stats["has_xml"] or stats["has_csv"]:
        return _build_conversation_response(
            response_type="plan_options",
            stage="planned",
            intent="knowledge_qa",
            confidence=0.9,
            message="当前波形数据可以直接进入分析流程。我整理了几个可选操作。",
            description="面向 ECG 波形/XML/CSV 的下一步处理建议。",
            args={"source_ids": selected_ids},
            meta={
                "headline": "这份波形数据可以这样处理",
                "body": "如果你已经有 XML 或 CSV，可以直接做结构化分析；若波形不完整，也可以先补全。",
                "presentation": "popover",
                "options": [
                    {
                        "id": "ecgd",
                        "title": "ECG 心电组学分析",
                        "description": "提取心率、节律、间期和形态学等结构化指标，适合已有可读波形数据时直接分析。",
                        "action": "run:ecgd",
                        "kind": "primary",
                    },
                    {
                        "id": "ecgrecon",
                        "title": "ECG 信号补全",
                        "description": "适合导联缺失、波形不完整或当前数据质量不足的情况，先补全再继续分析。",
                        "action": "run:ecgrecon",
                        "kind": "secondary",
                    },
                ],
            },
        )
    return _build_conversation_response(
        response_type="chat",
        stage="answered",
        intent="knowledge_qa",
        confidence=0.75,
        message="可以。我可以根据你选择的来源给出下一步建议。图片/PDF 更适合数字化或风险评估，XML/CSV 波形更适合直接做心电组学分析或信号补全。",
        description=CONVERSATION_INTENT_DESCRIPTIONS["knowledge_qa"],
    )


def _build_capability_confirm_response(intent: str, context: dict[str, Any]) -> ConversationResponse:
    selected_ids = _context_source_stats(context).get("selected_source_ids", [])
    prompt_map = {
        "ecg_auto_digitize": "这张图片可以自动数字化。是否现在开始自动数字化？",
        "ecg_manual_digitize": "这张图片可以做手动数字化。是否现在打开手动数字化工具？",
        "zhunxin_risk_assess": "这张图片可以做准心风险评估。是否现在开始评估？",
        "ecg_feature_extract": "当前波形数据可以做 ECG 心电组学分析。是否现在开始？",
        "ecg_reconstruct": "当前波形数据可以做 ECG 信号补全。是否现在开始？",
    }
    return _build_conversation_response(
        response_type="need_confirm",
        stage="classified",
        intent=intent,
        confidence=0.9,
        message=prompt_map.get(intent, "我理解你想执行这个任务，是否继续？"),
        description=CONVERSATION_INTENT_DESCRIPTIONS.get(intent, ""),
        args={"source_ids": selected_ids},
        action=CONVERSATION_INTENT_TO_ACTION.get(intent),
        meta={"reason": "capability_question_confirm"},
    )


def _parse_tool_call_arguments(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        return _extract_json_object(raw_args)
    return {}


def _tool_started_message(intent: str) -> str:
    return {
        "ecg_auto_digitize": "已识别为自动数字化任务，正在处理所选心电图图片，完成后会生成波形 CSV。",
        "ecg_manual_digitize": "已识别为手动数字化任务，正在打开数字化工具。",
        "ecg_feature_extract": "已识别为 ECG 心电组学任务，正在读取所选波形来源并执行分析。",
        "ecg_reconstruct": "已识别为 ECG 信号补全任务，正在标准化导联波形并执行补全，完成后会保存补全 CSV。",
        "zhunxin_risk_assess": "已识别为准心风险评估任务，正在处理所选心电图图片，稍后会生成风险报告。",
    }.get(intent, "已识别到工具任务，正在处理。")


def _build_conversation_response(
    *,
    response_type: str,
    stage: str,
    intent: str,
    confidence: float,
    message: str,
    description: str = "",
    args: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
    action: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
) -> ConversationResponse:
    return ConversationResponse(
        type=response_type,
        stage=stage,
        intent=intent,
        confidence=max(0.0, min(float(confidence), 1.0)),
        message=message,
        description=description,
        args=args or {},
        missing_fields=missing_fields or [],
        action=action,
        meta=meta,
    )


async def _route_conversation_turn(
    *,
    message: str,
    context: dict[str, Any],
    history: list[Any],
    provider: str,
    model: str,
    api_key: str,
    max_tokens: int,
    temperature: float,
    user: dict[str, Any],
    forced_intent: str = "",
) -> ConversationResponse:
    normalized_forced = _normalize_route_intent(forced_intent)
    if normalized_forced != "chat":
        missing_fields = _missing_fields_for_intent(normalized_forced, context)
        if missing_fields:
            missing_meta = _ask_missing_meta(normalized_forced, context, missing_fields)
            return _build_conversation_response(
                response_type="ask_missing",
                stage="validated",
                intent=normalized_forced,
                confidence=1.0,
                message=_message_for_missing_fields(normalized_forced, missing_fields),
                description=CONVERSATION_INTENT_DESCRIPTIONS.get(normalized_forced, ""),
                args={"source_ids": _context_source_stats(context).get("selected_source_ids", [])},
                missing_fields=missing_fields,
                action=CONVERSATION_INTENT_TO_ACTION.get(normalized_forced),
                meta=missing_meta,
            )
        return _build_conversation_response(
            response_type="tool_result",
            stage="dispatched",
            intent=normalized_forced,
            confidence=1.0,
            message=_tool_started_message(normalized_forced),
            description=CONVERSATION_INTENT_DESCRIPTIONS.get(normalized_forced, ""),
            args={"source_ids": _context_source_stats(context).get("selected_source_ids", [])},
            action=CONVERSATION_INTENT_TO_ACTION.get(normalized_forced),
        )

    canned_reply = _match_canned_reply(message)
    if canned_reply:
        return _build_conversation_response(
            response_type="chat",
            stage="guarded",
            intent="knowledge_qa",
            confidence=1.0,
            message=canned_reply,
            description=CONVERSATION_INTENT_DESCRIPTIONS["knowledge_qa"],
        )

    stats = _context_source_stats(context)
    targeted_capability_intent = _capability_question_target_intent(message, context)
    if targeted_capability_intent and stats["selected_count"]:
        return _build_capability_confirm_response(targeted_capability_intent, context)
    if _looks_like_capability_question(message) and stats["selected_count"]:
        return _build_capability_options_response(context)
    if stats["has_image"] and _looks_like_disease_check_goal(message):
        return _build_diagnostic_plan_response(context)

    route_plan = await _classify_conversation_intent(
        message=message,
        context=context,
        history=history,
        user=user,
        provider=provider,
        model=model,
        api_key=api_key,
    )
    intent = _normalize_route_intent(str(route_plan.get("intent") or "chat"))
    confidence = float(route_plan.get("confidence") or 0.0)
    args = route_plan.get("args") if isinstance(route_plan.get("args"), dict) else {}
    if intent in CONVERSATION_INTENT_TO_ACTION and _looks_like_capability_question(message) and not _looks_like_explicit_action_request(message):
        intent = "knowledge_qa"
        confidence = max(confidence, 0.88)
        route_plan["reason"] = "capability_question_guard"
    if (
        intent == "zhunxin_risk_assess"
        and _last_result_kind(context) == "zhunxin_risk"
        and _looks_like_result_followup_question(message)
        and not _is_explicit_zhunxin_action(message)
    ):
        intent = "result_interpretation"
        confidence = max(confidence, 0.9)
        route_plan["reason"] = "zhunxin_result_followup_guard"
    if not args.get("source_ids") and stats["selected_source_ids"]:
        args["source_ids"] = stats["selected_source_ids"]
    missing_fields = route_plan.get("missing_fields") if isinstance(route_plan.get("missing_fields"), list) else []
    if not missing_fields:
        missing_fields = _missing_fields_for_intent(intent, context)

    if intent in CONVERSATION_INTENT_TO_ACTION:
        if missing_fields:
            missing_meta = _ask_missing_meta(intent, context, missing_fields)
            return _build_conversation_response(
                response_type="ask_missing",
                stage="validated",
                intent=intent,
                confidence=max(confidence, 0.65),
                message=_message_for_missing_fields(intent, missing_fields),
                description=CONVERSATION_INTENT_DESCRIPTIONS.get(intent, ""),
                args=args,
                missing_fields=missing_fields,
                action=CONVERSATION_INTENT_TO_ACTION.get(intent),
                meta={"reason": route_plan.get("reason", ""), **missing_meta},
            )
        if confidence >= 0.85 or intent == "ecg_manual_digitize":
            return _build_conversation_response(
                response_type="tool_result",
                stage="dispatched",
                intent=intent,
                confidence=confidence or 0.9,
                message=_tool_started_message(intent),
                description=CONVERSATION_INTENT_DESCRIPTIONS.get(intent, ""),
                args=args,
                action=CONVERSATION_INTENT_TO_ACTION.get(intent),
                meta={"reason": route_plan.get("reason", "")},
            )
        return _build_conversation_response(
            response_type="need_confirm",
            stage="classified",
            intent=intent,
            confidence=max(confidence, 0.6),
            message=f"我理解你想执行“{CONVERSATION_INTENT_DESCRIPTIONS.get(intent, intent)}”，是否继续？",
            description=CONVERSATION_INTENT_DESCRIPTIONS.get(intent, ""),
            args=args,
            action=CONVERSATION_INTENT_TO_ACTION.get(intent),
            meta={"reason": route_plan.get("reason", "")},
        )

    latest_assistant_result = _latest_assistant_text(history)
    system_prompt = (
        DEFAULT_CHAT_SYSTEM
        + (
            "\n\n你同时也是 HeartOS 的结果解释助手。若用户是在问“这个结果什么意思”“这个说明什么”，"
            "优先解释上一条 assistant 结果在表达什么、意味着什么、下一步建议是什么；"
            "不要泛泛讲心电图基础知识，也不要脱离上一条结果另起话题。"
            if intent == "result_interpretation"
            else "\n\n你同时也是 HeartOS 的对话编排助手。若用户是在追问已有分析结果，请结合来源摘要给出专业解释；"
        )
        + "若资料不足，要明确说明依据有限，不要臆造结论。"
    )
    source_briefs = _source_brief_payload(context)
    chat_messages = _conversation_history_payload(history)
    chat_messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question": message,
                    "source_briefs": source_briefs,
                    "latest_assistant_result": latest_assistant_result if intent == "result_interpretation" else "",
                },
                ensure_ascii=False,
            ),
        }
    )
    result = await LLM_GATEWAY.chat(
        provider_key=(provider or settings.llm_default_provider).strip().lower(),
        model=(model or settings.llm_default_model).strip(),
        system=system_prompt,
        messages=chat_messages,
        max_tokens=max_tokens,
        temperature=temperature,
        user=user,
        override_api_key=api_key or "",
        timeout_seconds=settings.http_timeout,
        retries=settings.http_retries,
    )
    return _build_conversation_response(
        response_type="chat",
        stage="answered",
        intent=intent if intent != "chat" else "knowledge_qa",
        confidence=confidence or 0.7,
        message=result["reply"],
        description=CONVERSATION_INTENT_DESCRIPTIONS.get(intent, CONVERSATION_INTENT_DESCRIPTIONS["knowledge_qa"]),
        meta={"provider": result["provider"], "model": result["model"]},
    )


async def _classify_conversation_intent(
    *,
    message: str,
    context: dict[str, Any],
    history: list[Any],
    user: dict[str, Any],
    provider: str,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    provider_key = (provider or "zhipu").strip().lower()
    model_name = (model or settings.llm_default_model or "glm-4-flash").strip()
    system = (
        "你是 HeartOS 后端的对话路由器。"
        "你必须从提供的函数中选择一个最合适的函数调用，不要输出自然语言解释。"
        "如果用户是在询问工具是什么、结果怎么看、如何使用、平台能做什么，优先使用 knowledge_qa 或 result_interpretation。"
        "只有用户明确想执行操作时，才选择 ECG 工具意图。"
        "若用户意图不明确，不要勉强调用专业工具，优先选 chat 或 knowledge_qa。"
        "如果 context.last_result_kind 表示上一条已经是某个工具结果，而用户是在追问风险高不高、危不危险、意味着什么、要不要去医院，优先选择 result_interpretation，不要重新调用工具。"
    )
    payload = {
        "message": message,
        "history": _conversation_history_payload(history),
        "context": {
            "conversation_id": str((context or {}).get("conversation_id") or ""),
            "has_image": bool((context or {}).get("has_image")),
            "has_xml": bool((context or {}).get("has_xml")),
            "has_csv": bool((context or {}).get("has_csv")),
            "has_ecg_signal": bool((context or {}).get("has_ecg_signal")),
            "last_result_kind": _last_result_kind(context),
            "sources": _source_brief_payload(context),
        },
    }
    try:
        out = await LLM_GATEWAY.chat(
            provider_key=provider_key,
            model=model_name,
            system=system,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            max_tokens=360,
            temperature=0.0,
            user=user,
            override_api_key=api_key or "",
            tools=CONVERSATION_ROUTER_TOOLS,
            tool_choice="required",
            timeout_seconds=min(settings.http_timeout, 20),
            retries=settings.http_retries,
        )
        tool_calls = out.get("tool_calls") if isinstance(out.get("tool_calls"), list) else []
        if tool_calls:
            fn = tool_calls[0].get("function") if isinstance(tool_calls[0], dict) else {}
            name = str((fn or {}).get("name") or "chat")
            args = _parse_tool_call_arguments((fn or {}).get("arguments"))
            parsed = {
                "intent": _normalize_route_intent(name),
                "confidence": args.get("confidence", 0.0),
                "reason": args.get("reason", ""),
                "args": {"source_ids": args.get("source_ids") or []},
                "missing_fields": args.get("missing_fields") or [],
            }
            return parsed
        parsed = _extract_json_object(out.get("reply", ""))
        if parsed:
            parsed["intent"] = _normalize_route_intent(str(parsed.get("intent") or "chat"))
            return parsed
    except Exception:
        pass

    msg_text = _normalized_message_text(message)
    last_result_kind = _last_result_kind(context)
    asks_about_tool = any(k in msg_text for k in ("什么是", "是什么", "介绍", "说明", "解释", "原理", "怎么用", "如何用", "支持什么", "区别", "差别", "不同", "用途"))
    asks_result = _looks_like_result_followup_question(message)
    capability_question = _looks_like_capability_question(message)
    wants_action = _looks_like_explicit_action_request(message)
    if asks_result:
        return {"intent": "result_interpretation", "confidence": 0.78, "reason": "result_keywords", "args": {}, "missing_fields": _missing_fields_for_intent("result_interpretation", context)}
    if asks_about_tool or capability_question:
        return {"intent": "knowledge_qa", "confidence": 0.88, "reason": "knowledge_keywords", "args": {}, "missing_fields": []}
    if last_result_kind == "zhunxin_risk" and _looks_like_result_followup_question(message) and not _is_explicit_zhunxin_action(message):
        return {"intent": "result_interpretation", "confidence": 0.9, "reason": "zhunxin_result_followup", "args": {}, "missing_fields": _missing_fields_for_intent("result_interpretation", context)}
    if "手动" in msg_text and "数字化" in msg_text and wants_action:
        return {"intent": "ecg_manual_digitize", "confidence": 0.95, "reason": "manual_digitize_keywords", "args": {}, "missing_fields": _missing_fields_for_intent("ecg_manual_digitize", context)}
    if _is_explicit_zhunxin_action(message) and bool((context or {}).get("has_image")):
        return {"intent": "zhunxin_risk_assess", "confidence": 0.92, "reason": "zhunxin_risk_keywords", "args": {}, "missing_fields": _missing_fields_for_intent("zhunxin_risk_assess", context)}
    if ("补全" in msg_text or "重建" in msg_text) and ("心电" in msg_text or "ecg" in msg_text or "导联" in msg_text or "波形" in msg_text or bool((context or {}).get("has_ecg_signal"))):
        return {"intent": "ecg_reconstruct", "confidence": 0.9, "reason": "reconstruct_keywords", "args": {}, "missing_fields": _missing_fields_for_intent("ecg_reconstruct", context)}
    if ("特征提取" in msg_text or "提取特征" in msg_text or "ecgomics" in msg_text) and wants_action:
        return {"intent": "ecg_feature_extract", "confidence": 0.9, "reason": "feature_keywords", "args": {}, "missing_fields": _missing_fields_for_intent("ecg_feature_extract", context)}
    if ("自动" in msg_text and "数字化" in msg_text and wants_action) or ("数字化" in msg_text and bool((context or {}).get("has_image")) and wants_action):
        return {"intent": "ecg_auto_digitize", "confidence": 0.88, "reason": "auto_digitize_keywords", "args": {}, "missing_fields": _missing_fields_for_intent("ecg_auto_digitize", context)}
    return {"intent": "chat", "confidence": 0.55, "reason": "fallback_chat", "args": {}, "missing_fields": []}


async def _classify_intent_with_zhipu(
    *,
    message: str,
    context: dict[str, Any],
    user: dict[str, Any],
    api_key: str,
) -> dict[str, Any]:
    system = (
        "你是后端路由器。请仅输出 JSON，不要输出其它文本。"
        "字段: intent, route, target, reason, need_fields。"
        "intent 只能是: ecg_digitize, ecg_manual_digitize, ecg_reconstruct, ecgomics_analyze, report_generate, rag_search, agent_run, chat。"
        "route 只能是: api, agent, model。"
        "target 取值示例: /api/ai-ecg-digitize, /api/ecg-reconstruct, /api/ecgomics/analyze, /tool/handecg/manual, /tool/report/generate, /tool/rag/search, ecg, ml, dl, stats, heartos_chat。"
        "如果用户明确要求手动数字化，返回 ecg_manual_digitize + /tool/handecg/manual。"
        "如果用户明确要求自动数字化，返回 ecg_digitize + /api/ai-ecg-digitize。"
        "如果用户要求心电图补全、心电图重建、导联补全、波形重建，返回 ecg_reconstruct + /api/ecg-reconstruct。"
        "如果用户在问模型知识问答，使用 chat。"
    )
    input_payload = {
        "message": message,
        "context": context or {},
    }
    out = await LLM_GATEWAY.chat(
        provider_key="zhipu",
        model=settings.llm_default_model or "glm-4-flash",
        system=system,
        messages=[{"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)}],
        max_tokens=300,
        temperature=0.0,
        user=user,
        override_api_key=api_key or "",
        timeout_seconds=settings.http_timeout,
        retries=settings.http_retries,
    )
    parsed = _extract_json_object(out.get("reply", ""))
    if not parsed:
        return {"intent": "chat", "route": "model", "target": "heartos_chat", "reason": "fallback", "need_fields": []}
    return parsed


def get_current_user(
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None),
) -> dict[str, Any]:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif x_auth_token:
        token = x_auth_token.strip()

    if not token:
        raise HTTPException(status_code=401, detail="missing auth token")

    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    user = get_user_by_id(str(payload.get("uid") or ""))
    if user:
        return {
            "id": user.get("id"),
            "username": user.get("username"),
            "display_name": user.get("display_name"),
            "phone": user.get("phone"),
            "name": user.get("name"),
            "organization": user.get("organization"),
            "department": user.get("department"),
            "title": user.get("title"),
            "user_type": user.get("user_type"),
            "use_case": user.get("use_case"),
            "email": user.get("email"),
            "is_admin": user.get("is_admin"),
        }

    return {
        "id": payload.get("uid"),
        "username": payload.get("username"),
        "display_name": payload.get("name") or payload.get("username"),
        "phone": "",
        "name": payload.get("name") or payload.get("username"),
        "organization": "",
        "department": "",
        "title": "",
        "user_type": "",
        "use_case": "",
        "email": "",
        "is_admin": False,
    }


async def post_with_retry(url: str, headers: dict[str, str], payload: dict[str, Any]) -> httpx.Response:
    timeout = httpx.Timeout(settings.http_timeout)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    last_err: Exception | None = None

    for attempt in range(settings.http_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                return await client.post(url, headers=headers, json=payload)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < settings.http_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            break

    raise HTTPException(status_code=502, detail=f"Upstream request failed: {last_err}")


def _extract_numbers_from_text(text: str) -> list[float]:
    nums = []
    for m in NUM_RE.findall(text or ""):
        try:
            nums.append(float(m))
        except Exception:
            continue
    return nums


def _guess_sample_rate(root: ET.Element) -> int:
    cand_vals: list[int] = []
    for elem in root.iter():
        tag = str(elem.tag).lower()
        txt = (elem.text or "").strip()
        if any(k in tag for k in ["samplerate", "sample_rate", "sampling", "fs", "hz", "frequency"]):
            vals = _extract_numbers_from_text(txt)
            for v in vals:
                iv = int(round(v))
                if 50 <= iv <= 5000:
                    cand_vals.append(iv)
        for k, v in elem.attrib.items():
            kk = str(k).lower()
            if any(x in kk for x in ["samplerate", "sample_rate", "sampling", "fs", "hz", "frequency"]):
                vals = _extract_numbers_from_text(str(v))
                for n in vals:
                    iv = int(round(n))
                    if 50 <= iv <= 5000:
                        cand_vals.append(iv)
    return cand_vals[0] if cand_vals else 500


def _parse_xml_to_raw(xml_text: str) -> tuple[list[float], int] | None:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None

    fs = _guess_sample_rate(root)

    best: list[float] = []
    for elem in root.iter():
        tag = str(elem.tag).lower()
        txt = (elem.text or "").strip()
        if not txt:
            continue
        if not any(k in tag for k in ["wave", "ecg", "lead", "data", "signal"]):
            continue
        vals = _extract_numbers_from_text(txt)
        if len(vals) > len(best):
            best = vals

    if len(best) < 1000:
        vals = _extract_numbers_from_text(xml_text)
        bounded = [x for x in vals if -20 <= x <= 20]
        if len(bounded) >= 1000:
            best = bounded
        elif len(vals) >= 1000:
            best = vals

    if len(best) < max(1000, fs * 10):
        return None

    return best, fs


def _need_xml_fallback(status_code: int, body_json: Any, body_text: str) -> bool:
    if status_code == 501:
        return True
    text = (body_text or "").lower()
    msg = ""
    if isinstance(body_json, dict):
        msg = str(body_json.get("msg", "")).lower()
    merged = f"{text} {msg}"
    return any(k in merged for k in ["ecgdata", "non-empty one-dimensional", "must be longer than 10s", "inputtype"])


def _try_float(value: Any) -> float | None:
    s = str(value if value is not None else "").strip()
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        return None


def _looks_like_index_column(values: list[float | None]) -> bool:
    nums = [v for v in values if v is not None]
    if len(nums) < max(3, int(len(values) * 0.8)):
        return False
    if all(abs(nums[i] - i) < 1e-9 for i in range(len(nums))):
        return True
    if len(nums) >= 3:
        step = nums[1] - nums[0]
        if step > 0 and all(abs((nums[i] - nums[i - 1]) - step) < 1e-9 for i in range(1, len(nums))):
            return True
    return False


RECONSTRUCT_LEAD_ORDER = ("I", "II", "III", "aVR", "aVL", "aVF", "V1", "V2", "V3", "V4", "V5", "V6")


def _canonical_lead_name(name: Any) -> str | None:
    compact = str(name or "").strip().replace("_", "").replace("-", "").replace(" ", "")
    compact = compact.upper().replace("MDCECGLEAD", "").replace("LEAD", "")
    aliases = {lead.upper(): lead for lead in RECONSTRUCT_LEAD_ORDER}
    return aliases.get(compact)


def _format_signal_value(value: float | None) -> str:
    # The reconstruction service accepts a dense float matrix and uses zero
    # as the placeholder for missing lead samples.
    return "0" if value is None else format(value, ".12g")


def _write_reconstruct_matrix(
    lead_map: dict[str, list[float | None]],
    *,
    source_format: str,
) -> tuple[bytes, dict[str, Any]]:
    min_waveform_samples = 100
    canonical_map: dict[str, list[float | None]] = {}
    for name, values in lead_map.items():
        canonical = _canonical_lead_name(name)
        if canonical:
            canonical_map[canonical] = values
    if not canonical_map and len(lead_map) == len(RECONSTRUCT_LEAD_ORDER):
        canonical_map = {
            lead: values for lead, values in zip(RECONSTRUCT_LEAD_ORDER, lead_map.values())
        }

    populated = [
        lead
        for lead, values in canonical_map.items()
        if sum(1 for value in values if value is not None) >= min_waveform_samples
    ]
    if len(populated) < 2:
        raise HTTPException(
            status_code=422,
            detail="心电信号中可用于补全的连续导联少于 2 条，请提供多导联波形数据。",
        )

    row_count = max(len(values) for values in canonical_map.values())
    if row_count < min_waveform_samples:
        raise HTTPException(status_code=422, detail="心电信号连续采样点不足 100 个，无法进行补全。")

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row_index in range(row_count):
        writer.writerow(
            [
                _format_signal_value(canonical_map.get(lead, [])[row_index])
                if row_index < len(canonical_map.get(lead, []))
                else ""
                for lead in RECONSTRUCT_LEAD_ORDER
            ]
        )
    meta = {
        "input_format": source_format,
        "header_removed": False,
        "index_removed": False,
        "skipped_rows": 0,
        "rows": row_count,
        "columns": len(RECONSTRUCT_LEAD_ORDER),
        "lead_order": list(RECONSTRUCT_LEAD_ORDER),
        "provided_leads": populated,
        "missing_value_fill": 0,
        "numeric_columns": [RECONSTRUCT_LEAD_ORDER.index(lead) for lead in populated],
        "waveform_columns": [RECONSTRUCT_LEAD_ORDER.index(lead) for lead in populated],
        "column_stats": [
            {
                "index": index,
                "name": lead,
                "numeric": sum(1 for value in canonical_map.get(lead, []) if value is not None),
                "non_empty": sum(1 for value in canonical_map.get(lead, []) if value is not None),
                "coverage": round(
                    sum(1 for value in canonical_map.get(lead, []) if value is not None) / max(1, row_count),
                    4,
                ),
            }
            for index, lead in enumerate(RECONSTRUCT_LEAD_ORDER)
        ],
    }
    return out.getvalue().encode("utf-8"), meta


def _xml_reconstruct_lead_map(text: str) -> dict[str, list[float]]:
    try:
        root = ET.fromstring(text)
    except Exception:
        raise HTTPException(status_code=422, detail="XML 无法解析，请提供包含心电波形的有效 XML 文件。")

    lead_map: dict[str, list[float | None]] = {}
    for sequence in root.iter():
        if str(sequence.tag).rsplit("}", 1)[-1].lower() != "sequence":
            continue
        lead_name = ""
        digits_text = ""
        for child in sequence.iter():
            local_tag = str(child.tag).rsplit("}", 1)[-1].lower()
            code = str(child.attrib.get("code", ""))
            if not lead_name and "MDC_ECG_LEAD_" in code.upper():
                lead_name = code.upper().split("MDC_ECG_LEAD_", 1)[1]
            if local_tag == "digits" and (child.text or "").strip():
                digits_text = (child.text or "").strip()
                break
        if lead_name and digits_text:
            values = _extract_numbers_from_text(digits_text)
            if values:
                lead_map[lead_name] = values
    if not lead_map:
        raise HTTPException(status_code=422, detail="XML 中未识别到可补全的导联波形序列。")
    return lead_map


def _json_reconstruct_lead_map(content: bytes) -> dict[str, list[float]]:
    try:
        obj: Any = json.loads(content.decode("utf-8-sig", errors="ignore"))
    except Exception:
        raise HTTPException(status_code=422, detail="JSON 无法解析，请提供包含心电导联数组的有效文件。")

    queue: list[Any] = [obj]
    preferred_keys = ("ecgData", "ecgDataRaw", "leads", "waveforms", "signals")
    while queue:
        cur = queue.pop(0)
        if not isinstance(cur, dict):
            continue
        candidates = [cur.get(key) for key in preferred_keys if isinstance(cur.get(key), dict)]
        candidates.append(cur)
        for candidate in candidates:
            lead_map: dict[str, list[float]] = {}
            for name, values in candidate.items():
                if not isinstance(values, list):
                    continue
                numeric = [_try_float(value) for value in values]
                if any(value is not None for value in numeric):
                    lead_map[str(name)] = numeric
            if len(
                [values for values in lead_map.values() if sum(1 for value in values if value is not None) >= 100]
            ) >= 2:
                return lead_map
        queue.extend(value for value in cur.values() if isinstance(value, dict))
    raise HTTPException(status_code=422, detail="JSON 中未识别到至少两条连续心电导联波形。")


def _normalize_reconstruct_csv(content: bytes, delimiter: str = ",") -> tuple[bytes, dict[str, Any]]:
    min_waveform_samples = 100
    text = content.decode("utf-8-sig", errors="ignore").strip()
    if not text:
        raise HTTPException(status_code=422, detail="上传的心电信号文件为空")

    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if any(str(cell).strip() for cell in row)]
    if not rows:
        raise HTTPException(status_code=422, detail="心电信号表格中没有可解析的数据行")

    max_cols = max(len(row) for row in rows)
    rows = [row + [""] * (max_cols - len(row)) for row in rows]
    header_removed = False
    index_removed = False
    header = rows[0]
    lead_order: list[str] = []

    numeric_first_row = sum(1 for cell in header if _try_float(cell) is not None)
    if numeric_first_row < max(1, int(len(header) * 0.8)):
        rows = rows[1:]
        header_removed = True

    if not rows:
        raise HTTPException(status_code=422, detail="uploaded csv has only a header row")

    first_header = str(header[0] if header else "").strip().lower()
    first_col = [_try_float(row[0]) for row in rows if row]
    first_col_named_index = first_header in {"", "index", "idx", "time", "time_ms", "sample", "samples", "row"}
    if len(rows[0]) > 1 and (first_col_named_index or _looks_like_index_column(first_col)):
        rows = [row[1:] for row in rows]
        index_removed = True
        if header_removed:
            lead_order = [str(cell).strip() for cell in header[1:] if str(cell).strip()]
    elif header_removed:
        lead_order = [str(cell).strip() for cell in header if str(cell).strip()]

    # Blank cells are missing waveforms to reconstruct, not removable noise.
    # Keep the fixed 12-lead shape expected by the upstream model.
    data_row_count = len(rows)
    waveform_columns: list[int] = []
    column_stats: list[dict[str, Any]] = []
    for col in range(len(rows[0])):
        numeric_count = 0
        non_empty_count = 0
        for row in rows:
            cell = row[col] if col < len(row) else ""
            if str(cell).strip():
                non_empty_count += 1
            if _try_float(cell) is not None:
                numeric_count += 1
        coverage = numeric_count / max(1, data_row_count)
        column_stats.append(
            {
                "index": col,
                "name": (lead_order[col] if col < len(lead_order) else ""),
                "numeric": numeric_count,
                "non_empty": non_empty_count,
                "coverage": round(coverage, 4),
            }
        )
        if numeric_count >= min_waveform_samples:
            waveform_columns.append(col)

    if len(waveform_columns) < 2:
        raise HTTPException(
            status_code=422,
            detail=(
                "文件中没有足够的数值波形列。请确认所选文件包含可用于补全的"
                "连续心电导联波形，且波形列应主要由数字组成。"
            ),
        )

    selected_columns: dict[str, int] = {}
    for col, name in enumerate(lead_order):
        canonical = _canonical_lead_name(name)
        if canonical:
            selected_columns[canonical] = col
    if not selected_columns and len(rows[0]) == len(RECONSTRUCT_LEAD_ORDER):
        selected_columns = {lead: index for index, lead in enumerate(RECONSTRUCT_LEAD_ORDER)}
    populated = [
        lead for lead, col in selected_columns.items()
        if col in waveform_columns
    ]
    if len(populated) < 2:
        raise HTTPException(
            status_code=422,
            detail="补全输入需要可定位的标准十二导联波形列（I、II、III、aVR、aVL、aVF、V1-V6）。",
        )

    numeric_rows: list[list[str]] = []
    skipped_rows = 0
    for row in rows:
        signal_row = [
            _format_signal_value(_try_float(row[selected_columns[lead]]) if lead in selected_columns else None)
            for lead in RECONSTRUCT_LEAD_ORDER
        ]
        if any(signal_row):
            numeric_rows.append(signal_row)
        else:
            skipped_rows += 1

    if len(numeric_rows) < min_waveform_samples:
        raise HTTPException(status_code=422, detail="心电信号连续采样点不足 100 个，无法进行补全。")

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(numeric_rows)
    meta = {
        "input_format": "tsv" if delimiter == "\t" else "csv",
        "header_removed": header_removed,
        "index_removed": index_removed,
        "skipped_rows": skipped_rows,
        "rows": len(numeric_rows),
        "columns": len(RECONSTRUCT_LEAD_ORDER),
        "lead_order": list(RECONSTRUCT_LEAD_ORDER),
        "provided_leads": populated,
        "missing_value_fill": 0,
        "numeric_columns": [RECONSTRUCT_LEAD_ORDER.index(lead) for lead in populated],
        "waveform_columns": [RECONSTRUCT_LEAD_ORDER.index(lead) for lead in populated],
        "column_stats": column_stats,
    }
    return out.getvalue().encode("utf-8"), meta


def _normalize_reconstruct_input(content: bytes, filename: str, content_type: str = "") -> tuple[bytes, dict[str, Any]]:
    lower_name = str(filename or "").lower()
    lower_type = str(content_type or "").lower()
    decoded = content.decode("utf-8-sig", errors="ignore")
    text_head = decoded.lstrip()[:80].lower()
    if lower_name.endswith(".xml") or "xml" in lower_type or text_head.startswith("<"):
        return _write_reconstruct_matrix(
            _xml_reconstruct_lead_map(decoded),
            source_format="xml",
        )
    if lower_name.endswith(".json") or "json" in lower_type or text_head.startswith("{"):
        return _write_reconstruct_matrix(_json_reconstruct_lead_map(content), source_format="json")
    first_line = next((line for line in decoded.splitlines() if line.strip()), "")
    if lower_name.endswith(".tsv") or "\t" in first_line:
        return _normalize_reconstruct_csv(content, delimiter="\t")
    if lower_name.endswith(".txt") and "," not in first_line and len(re.split(r"\s+", first_line.strip())) >= 2:
        comma_text = "\n".join(",".join(re.split(r"\s+", line.strip())) for line in decoded.splitlines() if line.strip())
        out, meta = _normalize_reconstruct_csv(comma_text.encode("utf-8"))
        meta["input_format"] = "whitespace_text"
        return out, meta
    return _normalize_reconstruct_csv(content)


async def _save_impute_ecg_result(
    *,
    result: dict[str, Any],
    source_filename: str,
    user: dict[str, Any],
) -> dict[str, Any]:
    save_url = (settings.impute_ecg_save_url or "").strip()
    if not save_url:
        return {"ok": False, "skipped": True, "detail": "未配置 APP_IMPUTE_ECG_SAVE_URL"}

    payload_src = result.get("data") if isinstance(result.get("data"), dict) else result
    if not isinstance(payload_src, dict):
        return {"ok": False, "skipped": True, "detail": "重建结果不是 JSON 对象，无法保存"}

    save_payload: dict[str, Any] = {
        "userId": str(user.get("id") or user.get("username") or ""),
        "fsIn": payload_src.get("fs_in"),
        "fsOut": payload_src.get("fs_out"),
        "sourceFilename": source_filename or "ecg.csv",
        "ecgDataRaw": payload_src.get("ecgDataRaw") or {},
        "ecgData": payload_src.get("ecgData") or {},
        "image": payload_src.get("image") or "",
    }

    timeout = httpx.Timeout(settings.http_timeout)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    last_err: Exception | None = None
    for attempt in range(settings.http_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                resp = await client.post(save_url, headers={"Content-Type": "application/json"}, json=save_payload)
            try:
                data: Any = resp.json()
            except Exception:
                data = {"raw": resp.text}
            if resp.status_code >= 400:
                return {"ok": False, "status": resp.status_code, "detail": (resp.text or "")[:800], "payload": save_payload}
            return {"ok": True, "status": resp.status_code, "response": data, "payload": save_payload}
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < settings.http_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
    return {"ok": False, "detail": repr(last_err), "payload": save_payload}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "name": settings.name, "version": APP_VERSION}


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(req: LoginRequest) -> LoginResponse:
    if (settings.auth_mode or "").lower() == "upstream":
        try:
            user = await asyncio.to_thread(upstream_login, req.username or req.phone, req.password)
        except UpstreamAuthError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
    else:
        user = verify_user(req.username, req.password, phone=req.phone)
        if not user:
            raise HTTPException(status_code=401, detail="手机号或密码错误")

    token = issue_token(user)
    return LoginResponse(
        token=token,
        user_id=str(user.get("id")),
        username=str(user.get("username")),
        phone=str(user.get("phone") or ""),
        display_name=str(user.get("display_name") or user.get("username")),
        name=str(user.get("name") or user.get("display_name") or user.get("username")),
        organization=str(user.get("organization") or ""),
        department=str(user.get("department") or ""),
        title=str(user.get("title") or ""),
        user_type=str(user.get("user_type") or ""),
        use_case=str(user.get("use_case") or ""),
        email=str(user.get("email") or ""),
        is_admin=bool(user.get("is_admin")),
        expires_in=max(1, settings.auth_expire_hours) * 3600,
    )


@app.post("/api/auth/send-code", response_model=SendCodeResponse)
async def auth_send_code(req: SendCodeRequest) -> SendCodeResponse:
    try:
        out = send_verification_code(req.phone, req.purpose)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return SendCodeResponse(**out)


@app.post("/api/auth/password/reset/send-code", response_model=SendCodeResponse)
async def auth_password_reset_send_code(req: PasswordResetSendCodeRequest) -> SendCodeResponse:
    try:
        out = send_password_reset_code(req.phone)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return SendCodeResponse(**out)


@app.post("/api/auth/password/reset/verify")
async def auth_password_reset_verify(req: PasswordResetVerifyRequest) -> dict[str, Any]:
    try:
        user = verify_password_reset_code(req.phone, req.code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ok": True,
        "user_id": str(user.get("id") or ""),
        "verification_token": issue_verification_ticket(req.phone, "reset_password"),
    }


@app.post("/api/auth/register/verify")
async def auth_register_verify(req: RegisterVerifyRequest) -> dict[str, Any]:
    try:
        verify_registration_code(req.phone, req.code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "verification_token": issue_verification_ticket(req.phone, "register")}



@app.post("/api/auth/register", response_model=LoginResponse)
async def register(req: RegisterRequest) -> LoginResponse:
    try:
        user = await asyncio.to_thread(upstream_register, req.phone, req.password, req.display_name or req.name)
    except UpstreamAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    try:
        user = register_user(
            phone=req.phone,
            password=req.password,
            code=req.code,
            verification_token=req.verification_token,
            name=req.name,
            organization=req.organization,
            user_type=req.user_type,
            use_case=req.use_case,
            department=req.department,
            title=req.title,
            email=req.email,
            display_name=req.display_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    token = issue_token(user)
    return LoginResponse(
        token=token,
        user_id=str(user.get("id")),
        username=str(user.get("username")),
        phone=str(user.get("phone") or ""),
        display_name=str(user.get("display_name") or user.get("username")),
        name=str(user.get("name") or user.get("display_name") or user.get("username")),
        organization=str(user.get("organization") or ""),
        department=str(user.get("department") or ""),
        title=str(user.get("title") or ""),
        user_type=str(user.get("user_type") or ""),
        use_case=str(user.get("use_case") or ""),
        email=str(user.get("email") or ""),
        is_admin=bool(user.get("is_admin")),
        expires_in=max(1, settings.auth_expire_hours) * 3600,
    )


@app.post("/api/auth/password/reset/confirm")
async def auth_password_reset_confirm(req: PasswordResetConfirmRequest) -> dict[str, Any]:
    try:
        user = reset_password(req.phone, req.code, req.new_password, req.verification_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "user_id": str(user.get("id") or "")}


@app.post("/api/auth/password/change")
async def auth_password_change(
    req: PasswordChangeRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        change_password(str(user.get("id") or ""), req.old_password, req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.get("/api/auth/me", response_model=MeResponse)
async def me(user: dict[str, Any] = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        user_id=str(user.get("id")),
        username=str(user.get("username")),
        display_name=str(user.get("display_name")),
        phone=str(user.get("phone") or ""),
        name=str(user.get("name") or user.get("display_name") or user.get("username")),
        organization=str(user.get("organization") or ""),
        department=str(user.get("department") or ""),
        title=str(user.get("title") or ""),
        user_type=str(user.get("user_type") or ""),
        use_case=str(user.get("use_case") or ""),
        email=str(user.get("email") or ""),
        is_admin=bool(user.get("is_admin")),
    )


@app.post("/api/auth/profile", response_model=MeResponse)
async def save_profile(
    req: ProfileUpdateRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> MeResponse:
    try:
        updated = update_profile(str(user.get("id") or ""), req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return MeResponse(
        user_id=str(updated.get("id")),
        username=str(updated.get("username")),
        display_name=str(updated.get("display_name")),
        phone=str(updated.get("phone") or ""),
        name=str(updated.get("name") or updated.get("display_name") or updated.get("username")),
        organization=str(updated.get("organization") or ""),
        department=str(updated.get("department") or ""),
        title=str(updated.get("title") or ""),
        user_type=str(updated.get("user_type") or ""),
        use_case=str(updated.get("use_case") or ""),
        email=str(updated.get("email") or ""),
        is_admin=bool(updated.get("is_admin")),
    )


@app.get("/api/admin/users", response_model=UserAdminListResponse)
async def admin_users(user: dict[str, Any] = Depends(get_current_user)) -> UserAdminListResponse:
    if not bool(user.get("is_admin")):
        raise HTTPException(status_code=403, detail="仅管理员可查看用户列表")
    return UserAdminListResponse(items=list_users_for_admin())


@app.get("/api/user/ai-config")
async def get_ai_config(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    db = _load_json_file(AI_CONFIG_PATH)
    user_items = db.get(uid, {})
    if not isinstance(user_items, dict):
        user_items = {}

    items = []
    for provider, cfg in user_items.items():
        if not isinstance(cfg, dict):
            continue
        items.append(
            {
                "provider": str(provider),
                "has_key": bool(str(cfg.get("api_key") or "").strip()),
            }
        )
    return {"items": items}


@app.post("/api/user/ai-config")
async def save_ai_config(payload: dict[str, Any], user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    provider = str(payload.get("provider") or "").strip().lower()
    api_key = str(payload.get("api_key") or "").strip()
    if not provider:
        raise HTTPException(status_code=422, detail="provider is required")
    if not api_key:
        raise HTTPException(status_code=422, detail="api_key is required")

    uid = str(user.get("id") or "")
    db = _load_json_file(AI_CONFIG_PATH)
    if not isinstance(db.get(uid), dict):
        db[uid] = {}
    db[uid][provider] = {"api_key": api_key}
    _save_json_file(AI_CONFIG_PATH, db)
    return {"ok": True, "provider": provider, "has_key": True}


@app.post("/api/feedback")
async def submit_feedback(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    message = ""
    context: dict[str, Any] = {}
    attachments: list[dict[str, Any]] = []

    content_type = str(request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        message = str(form.get("message") or "").strip()
        context = _coerce_feedback_context(form.get("context"))
        raw_files = [item for item in form.getlist("images") if hasattr(item, "filename") and hasattr(item, "read")]
        if len(raw_files) > FEEDBACK_MAX_IMAGES:
            raise HTTPException(status_code=422, detail=f"反馈图片最多上传 {FEEDBACK_MAX_IMAGES} 张")
        for file in raw_files:
            filename = str(file.filename or "").strip()
            if not filename:
                continue
            image_type = str(file.content_type or "").strip().lower()
            if image_type and image_type not in FEEDBACK_ALLOWED_IMAGE_TYPES:
                raise HTTPException(status_code=422, detail="反馈图片仅支持 PNG、JPG、WebP、GIF、BMP")
            content = await file.read()
            if not content:
                continue
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"反馈图片过大，单张最大 {settings.max_upload_mb}MB")
            attachments.append(
                _store_upload_bytes(
                    content=content,
                    filename=filename,
                    content_type=image_type or "application/octet-stream",
                    source="feedback",
                    user=user,
                )
            )
    else:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail="反馈请求格式不正确") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="反馈请求格式不正确")
        message = str(payload.get("message") or "").strip()
        context = _coerce_feedback_context(payload.get("context"))

    if len(message) < 5 and not attachments:
        raise HTTPException(status_code=422, detail="请至少填写 5 个字符的反馈内容，或上传反馈图片")
    db = _load_json_file(FEEDBACK_PATH)
    items = db.get("items", [])
    if not isinstance(items, list):
        items = []

    record = {
        "id": uuid.uuid4().hex,
        "createdAt": int(time.time() * 1000),
        "message": message,
        "context": context,
        "attachments": attachments,
        "status": "new",
        "user": {
            "id": str(user.get("id") or ""),
            "username": str(user.get("username") or ""),
            "display_name": str(user.get("display_name") or ""),
        },
    }
    items.insert(0, record)
    db["items"] = items[:1000]
    _save_json_file(FEEDBACK_PATH, db)
    return {"ok": True, "id": record["id"], "attachments": attachments}


def _normalize_notebook_item(raw: dict[str, Any]) -> dict[str, Any]:
    nid = str(raw.get("id") or "").strip()
    if not nid:
        raise HTTPException(status_code=422, detail="notebook id is required")
    msgs = raw.get("msgs")
    events = raw.get("events")
    analysis_files = raw.get("analysisFiles")
    return {
        "id": nid,
        "title": str(raw.get("title") or "New Conversation"),
        "icon": str(raw.get("icon") or "📔"),
        "color": str(raw.get("color") or "#e8f0fe"),
        "date": str(raw.get("date") or ""),
        "sources": [],
        "msgs": msgs if isinstance(msgs, list) else [],
        "events": events if isinstance(events, list) else [],
        "analysisFiles": analysis_files if isinstance(analysis_files, list) else [],
        "sumHtml": str(raw.get("sumHtml") or ""),
        "suggHtml": str(raw.get("suggHtml") or ""),
        "updatedAt": int(raw.get("updatedAt") or 0),
    }


def _notebook_summary(raw: dict[str, Any], source_count: int | None = None) -> dict[str, Any]:
    sources = raw.get("sources")
    msgs = raw.get("msgs")
    analysis_files = raw.get("analysisFiles")
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or "New Conversation"),
        "icon": str(raw.get("icon") or "📔"),
        "color": str(raw.get("color") or "#e8f0fe"),
        "date": str(raw.get("date") or ""),
        "srcs": int(source_count) if source_count is not None else (len(sources) if isinstance(sources, list) else 0),
        "msgCount": len(msgs) if isinstance(msgs, list) else 0,
        "analysisCount": len(analysis_files) if isinstance(analysis_files, list) else 0,
        "updatedAt": int(raw.get("updatedAt") or 0),
    }


@app.get("/api/notebooks")
async def list_notebooks(summary_only: bool = False, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    db = _load_json_file(NOTEBOOKS_PATH)
    items = db.get(uid, [])
    if not isinstance(items, list):
        items = []
    source_bucket = _get_user_notebook_sources(uid)
    if summary_only:
        return {
            "items": [
                _notebook_summary(
                    item,
                    len(source_bucket.get(str(item.get("id") or ""), [])) if isinstance(source_bucket.get(str(item.get("id") or ""), []), list) else None,
                )
                for item in items
                if isinstance(item, dict)
            ]
        }
    return {"items": items}


@app.get("/api/notebooks/{notebook_id}")
async def get_notebook(notebook_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    db = _load_json_file(NOTEBOOKS_PATH)
    items = db.get(uid, [])
    if not isinstance(items, list):
        items = []
    for item in items:
        if isinstance(item, dict) and str(item.get("id")) == str(notebook_id):
            linked_sources = _get_conversation_sources(uid, notebook_id)
            if not linked_sources and isinstance(item.get("sources"), list) and item.get("sources"):
                migrated = [src for src in (_normalize_source_item(raw) for raw in item.get("sources")) if src]
                if migrated:
                    _set_conversation_sources(uid, notebook_id, migrated)
                    linked_sources = migrated
            merged = dict(item)
            merged["sources"] = linked_sources
            return {"item": merged}
    raise HTTPException(status_code=404, detail="notebook not found")


@app.post("/api/notebooks")
async def upsert_notebook(payload: dict[str, Any], user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    raw_payload = payload if isinstance(payload, dict) else {}
    raw_sources = raw_payload.get("sources")
    source_items = [src for src in (_normalize_source_item(raw) for raw in raw_sources) if src] if isinstance(raw_sources, list) else []
    item = _normalize_notebook_item(payload if isinstance(payload, dict) else {})

    db = _load_json_file(NOTEBOOKS_PATH)
    items = db.get(uid, [])
    if not isinstance(items, list):
        items = []

    replaced = False
    for i, existing in enumerate(items):
        if isinstance(existing, dict) and str(existing.get("id")) == item["id"]:
            if item["updatedAt"] and int(existing.get("updatedAt") or 0) > item["updatedAt"]:
                return {"ok": True, "id": item["id"], "stale": True}
            items[i] = item
            replaced = True
            break
    if not replaced:
        items.insert(0, item)

    db[uid] = items
    _save_json_file(NOTEBOOKS_PATH, db)
    if isinstance(raw_sources, list):
        _set_conversation_sources(uid, item["id"], source_items)
    return {"ok": True, "id": item["id"]}


@app.delete("/api/notebooks/{notebook_id}")
async def delete_notebook(notebook_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    db = _load_json_file(NOTEBOOKS_PATH)
    items = db.get(uid, [])
    if not isinstance(items, list):
        items = []
    before = len(items)
    items = [it for it in items if not (isinstance(it, dict) and str(it.get("id")) == str(notebook_id))]
    db[uid] = items
    _save_json_file(NOTEBOOKS_PATH, db)
    _delete_conversation_sources(uid, notebook_id)
    return {"ok": True, "deleted": before - len(items)}


@app.get("/api/notebooks/{notebook_id}/sources")
async def get_notebook_sources(notebook_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    return {"items": _get_conversation_sources(uid, notebook_id)}


@app.put("/api/notebooks/{notebook_id}/sources")
async def replace_notebook_sources(
    notebook_id: str,
    payload: dict[str, Any],
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    raw_items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        raise HTTPException(status_code=422, detail="items must be a list")
    source_items = [src for src in (_normalize_source_item(raw) for raw in raw_items) if src]
    _set_conversation_sources(uid, notebook_id, source_items)
    return {"ok": True, "count": len(source_items)}


@app.post("/api/conversation/turn", response_model=ConversationResponse)
async def conversation_turn(
    req: ConversationTurnRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> ConversationResponse:
    return await _route_conversation_turn(
        message=req.message,
        context=req.context.model_dump(),
        history=req.history,
        provider=req.provider,
        model=req.model,
        api_key=req.api_key,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        user=user,
        forced_intent=req.client_hint.intent,
    )


@app.post("/api/conversation/confirm", response_model=ConversationResponse)
async def conversation_confirm(
    req: ConversationConfirmRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> ConversationResponse:
    intent = _normalize_route_intent(req.intent)
    if intent not in CONVERSATION_INTENT_TO_ACTION:
        return _build_conversation_response(
            response_type="chat",
            stage="fallback",
            intent="chat",
            confidence=0.0,
            message="当前确认的任务类型暂不支持执行，请重新描述你的需求。",
        )
    missing_fields = _missing_fields_for_intent(intent, req.context.model_dump())
    if missing_fields:
        missing_meta = _ask_missing_meta(intent, req.context.model_dump(), missing_fields)
        return _build_conversation_response(
            response_type="ask_missing",
            stage="validated",
            intent=intent,
            confidence=1.0,
            message=_message_for_missing_fields(intent, missing_fields),
            description=CONVERSATION_INTENT_DESCRIPTIONS.get(intent, ""),
            args=req.args,
            missing_fields=missing_fields,
            action=CONVERSATION_INTENT_TO_ACTION.get(intent),
            meta=missing_meta,
        )
    args = dict(req.args or {})
    if not args.get("source_ids"):
        args["source_ids"] = _context_source_stats(req.context.model_dump()).get("selected_source_ids", [])
    return _build_conversation_response(
        response_type="tool_result",
        stage="confirmed",
        intent=intent,
        confidence=1.0,
        message=_tool_started_message(intent),
        description=CONVERSATION_INTENT_DESCRIPTIONS.get(intent, ""),
        args=args,
        action=CONVERSATION_INTENT_TO_ACTION.get(intent),
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: dict[str, Any] = Depends(get_current_user)) -> ChatResponse:
    provider = (req.provider or settings.llm_default_provider).strip().lower()
    model = (req.model or settings.llm_default_model).strip()
    msg_list = [{"role": m.role, "content": m.content} for m in req.messages]
    system_prompt = (req.system or "").strip() or DEFAULT_CHAT_SYSTEM
    latest_user_message = ""
    for item in reversed(msg_list):
        if item.get("role") == "user":
            latest_user_message = str(item.get("content") or "")
            break
    canned_reply = _match_canned_reply(latest_user_message)
    if canned_reply:
        return ChatResponse(
            reply=canned_reply,
            provider=provider,
            model=model or settings.llm_default_model,
            request_id=None,
            raw=None,
            user_id=str(user.get("id")),
            username=str(user.get("username")),
        )

    result = await LLM_GATEWAY.chat(
        provider_key=provider,
        model=model,
        system=system_prompt,
        messages=msg_list,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        user=user,
        override_api_key=req.api_key or "",
        thinking=req.thinking,
        timeout_seconds=settings.http_timeout,
        retries=settings.http_retries,
    )

    return ChatResponse(
        reply=result["reply"],
        provider=provider,
        model=result["model"],
        request_id=result.get("request_id"),
        raw=None,
        user_id=str(user.get("id")),
        username=str(user.get("username")),
    )


@app.post("/api/agents/{agent_id}/run", response_model=AgentRunResponse)
async def run_agent(agent_id: str, req: AgentRunRequest, user: dict[str, Any] = Depends(get_current_user)) -> AgentRunResponse:
    if req.agent_id and req.agent_id != agent_id:
        raise HTTPException(status_code=400, detail="agent_id mismatch")

    system = AGENT_SYSTEM_PROMPTS.get(agent_id, "你是专业助手，请基于输入给出准确建议。")
    chat_req = ChatRequest(
        provider=req.provider,
        model=req.model,
        api_key=req.api_key,
        system=system,
        messages=req.messages,
        max_tokens=req.max_tokens,
        temperature=0.7,
    )
    chat_resp = await chat(chat_req, user)
    return AgentRunResponse(agent_id=agent_id, reply=chat_resp.reply, provider=chat_resp.provider, model=chat_resp.model)


@app.post("/api/agent/auto-run", response_model=AgentAutoRunResponse)
async def auto_run_agent(req: AgentAutoRunRequest, user: dict[str, Any] = Depends(get_current_user)) -> AgentAutoRunResponse:
    routed = await _route_conversation_turn(
        message=req.message,
        context=req.context,
        history=[],
        provider=req.provider,
        model=req.model,
        api_key=req.api_key,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        user=user,
    )
    action = dict(routed.action or {})
    if routed.type == "chat":
        return AgentAutoRunResponse(
            intent="chat",
            route="model",
            target=str((routed.meta or {}).get("provider") or "heartos_chat"),
            reply=routed.message,
            action=action or {"provider": (routed.meta or {}).get("provider"), "model": (routed.meta or {}).get("model")},
        )

    route = "api"
    target = str(action.get("endpoint") or "")
    if routed.type == "need_confirm":
        action["needs_confirmation"] = True
        route = "confirm"
    elif routed.type == "plan_options":
        route = "plan"
    elif routed.type == "ask_missing":
        route = "missing"
    return AgentAutoRunResponse(
        intent=routed.intent,
        route=route,
        target=target or "heartos_chat",
        reply=routed.message,
        action=action or None,
    )


@app.post("/api/ecgomics/analyze")
async def ecgomics_analyze(req: ECGOmicsAnalyzeRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if req.inputType == "raw":
        if not req.ecgData or req.ecgSampleRate is None:
            raise HTTPException(status_code=400, detail="raw 模式需要 ecgData 和 ecgSampleRate")
        min_samples = req.ecgSampleRate * 10
        if len(req.ecgData) < min_samples:
            duration = len(req.ecgData) / req.ecgSampleRate
            raise HTTPException(
                status_code=422,
                detail=(
                    f"ECGOmics raw 模式需要至少 10 秒连续波形（当前采样率需 {min_samples} 点）；"
                    f"当前仅 {duration:.3f} 秒（{len(req.ecgData)} 点）。"
                ),
            )
    else:
        if not req.xmlData:
            raise HTTPException(status_code=400, detail="xml 模式需要 xmlData")

    payload = req.model_dump(exclude_none=True)
    payload["user"] = {"id": user.get("id"), "username": user.get("username")}
    headers = {"Content-Type": "application/json"}
    resp = await post_with_retry(settings.ecgomics_url, headers, payload)

    first_text = resp.text or ""
    first_json: Any = None
    try:
        first_json = resp.json()
    except Exception:
        first_json = None

    if req.inputType == "xml" and _need_xml_fallback(resp.status_code, first_json, first_text):
        parsed = _parse_xml_to_raw(req.xmlData or "")
        if not parsed:
            raise HTTPException(status_code=502, detail="ECGOmics XML 模式失败，且无法从 XML 提取足够的 raw ECG（至少 10 秒）")
        ecg_data, fs = parsed
        raw_payload = {
            "ecgData": ecg_data,
            "ecgSampleRate": fs,
            "inputType": "raw",
            "zero": req.zero,
            "gain": req.gain,
            "filter": req.filter,
            "user": {"id": user.get("id"), "username": user.get("username")},
        }
        raw_resp = await post_with_retry(settings.ecgomics_url, headers, raw_payload)
        if raw_resp.status_code >= 400:
            raise HTTPException(status_code=raw_resp.status_code, detail=raw_resp.text[:500])
        out = raw_resp.json()
        if isinstance(out, dict):
            out.setdefault("_meta", {})
            out["_meta"]["fallback"] = "xml_to_raw"
            out["_meta"]["sampleRate"] = fs
            out["_meta"]["signalLength"] = len(ecg_data)
            out["_meta"]["user_id"] = user.get("id")
            out["_meta"]["username"] = user.get("username")
        return out

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=first_text[:500])

    if first_json is not None:
        if isinstance(first_json, dict):
            first_json.setdefault("_meta", {})
            first_json["_meta"]["user_id"] = user.get("id")
            first_json["_meta"]["username"] = user.get("username")
        return first_json

    raise HTTPException(status_code=502, detail="ECGOmics 返回非 JSON")


@app.post("/api/handecg/save")
async def handecg_save(
    req: HandEcgSaveRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    upstream_url = (settings.handecg_save_url or "").strip()
    if not upstream_url:
        raise HTTPException(status_code=500, detail="未配置手动数字化保存地址 APP_HANDECG_SAVE_URL")

    user_id = (req.user_id or "").strip() or str(user.get("id") or "")

    payload: dict[str, Any] = {
        "user_id": user_id,
        "image_base64": req.image_base64,
        "image_mime": req.image_mime,
        "image_name": req.image_name,
        "file_name": req.file_name,
        "data": req.data or {},
    }

    headers = {"Content-Type": "application/json"}
    resp = await post_with_retry(upstream_url, headers, payload)
    if resp.status_code >= 400:
        detail_text = (resp.text or "")[:500]
        raise HTTPException(status_code=resp.status_code, detail=detail_text or "HandECG 保存失败")

    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    if isinstance(data, dict):
        data.setdefault("_meta", {})
        data["_meta"]["user_id"] = user.get("id")
        data["_meta"]["username"] = user.get("username")
    return data


@app.post("/api/ecg-reconstruct")
@app.post("/api/reconstruct")
async def ecg_reconstruct(
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    upstream_url = (settings.ecg_reconstruct_url or "").strip()
    if not upstream_url:
        raise HTTPException(status_code=500, detail="未配置 ECG 重建地址 APP_ECG_RECONSTRUCT_URL")

    filename = str(file.filename or "ecg.csv")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large, max {settings.max_upload_mb}MB")

    upstream_content, csv_meta = _normalize_reconstruct_input(content, filename, str(file.content_type or ""))
    base_filename = filename.rsplit(".", 1)[0] if "." in filename else filename
    upstream_filename = f"{base_filename or 'ecg'}_signal_only.csv"
    content_type = "text/csv"
    timeout = httpx.Timeout(settings.http_timeout)
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    last_err: Exception | None = None
    last_status = 502
    last_text = ""

    for attempt in range(settings.http_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                resp = await client.post(
                    upstream_url,
                    files={"file": (upstream_filename, upstream_content, content_type or "text/csv")},
                )
            if resp.status_code >= 400:
                last_status = resp.status_code
                last_text = (resp.text or "")[:800]
                if attempt < settings.http_retries:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
                break
            try:
                out: Any = resp.json()
            except Exception:
                raise HTTPException(status_code=502, detail="ECG 重建接口返回非 JSON")

            if isinstance(out, dict):
                out.setdefault("_meta", {})
                out["_meta"]["user_id"] = user.get("id")
                out["_meta"]["username"] = user.get("username")
                out["_meta"]["source_file"] = filename
                out["_meta"]["csv_normalization"] = csv_meta
                payload = out.get("data") if isinstance(out.get("data"), dict) else out
                response: dict[str, Any] = {"ok": True, "upstream": out}
                if isinstance(payload, dict):
                    for key in ("fs_in", "fs_out", "ecgDataRaw", "ecgData", "image"):
                        if key in payload:
                            response[key] = payload[key]
                save_result = await _save_impute_ecg_result(result=out, source_filename=filename, user=user)
                out["_meta"]["saveImputeECGR"] = {
                    "ok": bool(save_result.get("ok")),
                    "status": save_result.get("status"),
                    "detail": save_result.get("detail"),
                }
                response["_saveImputeECGR"] = save_result
                return response
            return {"ok": True, "upstream": out}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < settings.http_retries:
                await asyncio.sleep(0.3 * (attempt + 1))
                continue

    detail = f"ECG 重建失败 upstream={upstream_url}"
    if last_text:
        detail += f" resp={last_text}"
    if last_err:
        detail += f" err={last_err!r}"
    raise HTTPException(status_code=last_status, detail=detail[:1500])


@app.post("/api/ai-ecg-digitize")
async def ai_ecg_digitize(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    upstream_url = (settings.ai_ecg_digitize_url or "").strip()
    if not upstream_url:
        raise HTTPException(status_code=500, detail="未配置 AI 心电图数字化地址 APP_AI_ECG_DIGITIZE_URL")

    image_base64 = ""
    image_mime = "image/png"
    image_name = "ecg_image.png"
    options: dict[str, Any] = {}

    ctype = (request.headers.get("content-type") or "").lower()
    parsed_form = None
    try:
        parsed_form = await request.form()
    except Exception:
        parsed_form = None

    if ("multipart/form-data" in ctype) or (parsed_form is not None and "file" in parsed_form):
        form = parsed_form or await request.form()
        file_item = form.get("file")
        # Be tolerant here: depending on stack, this may be FastAPI UploadFile,
        # Starlette UploadFile, or a bytes-like object.
        if file_item is None:
            raise HTTPException(
                status_code=422,
                detail=f"missing required multipart field: file; got fields={list(form.keys())}",
            )

        raw: bytes
        if hasattr(file_item, "read"):
            raw = await file_item.read()
        elif isinstance(file_item, (bytes, bytearray)):
            raw = bytes(file_item)
        else:
            raise HTTPException(
                status_code=422,
                detail=f"multipart field 'file' has unsupported type: {type(file_item).__name__}",
            )

        if not raw:
            raise HTTPException(status_code=422, detail="uploaded file is empty")
        image_base64 = base64.b64encode(raw).decode("ascii")
        image_mime = str(getattr(file_item, "content_type", "") or "image/png")
        image_name = str(getattr(file_item, "filename", "") or "ecg_image.png")

        if form.get("image_name"):
            image_name = str(form.get("image_name") or image_name)

        raw_options = form.get("options")
        if raw_options:
            try:
                parsed_options = json.loads(str(raw_options))
                if isinstance(parsed_options, dict):
                    options.update(parsed_options)
            except Exception:
                pass

        reserved = {"file", "image_name", "file_name", "user_id", "username", "options"}
        for k, v in form.items():
            key = str(k)
            if key in reserved:
                continue
            sval = str(v)
            low = sval.strip().lower()
            if low in {"true", "false"}:
                options[key] = (low == "true")
            else:
                options[key] = sval
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="invalid json body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="invalid json body")
        image_base64 = str(body.get("image_base64") or "")
        image_mime = str(body.get("image_mime") or "image/png")
        image_name = str(body.get("image_name") or "ecg_image.png")
        options = body.get("options") if isinstance(body.get("options"), dict) else {}

        if not image_base64:
            raise HTTPException(status_code=422, detail="missing image_base64 in json body")

    payload: dict[str, Any] = {
        "image_base64": image_base64,
        "image_mime": image_mime,
        "image_name": image_name,
        "options": options or {},
        "user": {"id": user.get("id"), "username": user.get("username")},
    }
    data_url = f"data:{image_mime or 'image/png'};base64,{image_base64}"
    candidates: list[dict[str, Any]] = [
        payload,
        {
            "image": data_url,
            "image_name": image_name,
            "file_name": image_name,
            "options": options or {},
            "user_id": str(user.get("id") or ""),
        },
        {
            "img_base64": image_base64,
            "mime": image_mime,
            "filename": image_name,
            "options": options or {},
        },
    ]

    last_status = 502
    last_text = ""
    last_err = ""
    out: Any = None

    # Preferred mode: multipart/form-data with required "file" field
    try:
        raw_bytes = base64.b64decode(image_base64.encode("utf-8"), validate=False)
    except Exception:
        raw_bytes = b""
    if raw_bytes:
        timeout = httpx.Timeout(settings.http_timeout)
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        for attempt in range(settings.http_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                    files = {
                        "file": (
                            image_name or "ecg_image.png",
                            raw_bytes,
                            image_mime or "image/png",
                        )
                    }
                    data = {
                        "image_name": image_name or "",
                        "file_name": image_name or "",
                        "user_id": str(user.get("id") or ""),
                        "username": str(user.get("username") or ""),
                        "options": json.dumps(options or {}, ensure_ascii=False),
                    }
                    # Compatibility: many upstream services expect options as top-level form fields.
                    for k, v in (options or {}).items():
                        if k in data:
                            continue
                        if isinstance(v, bool):
                            data[str(k)] = "true" if v else "false"
                        else:
                            data[str(k)] = str(v)
                    resp = await client.post(upstream_url, data=data, files=files)
                if resp.status_code >= 400:
                    last_status = resp.status_code
                    last_text = (resp.text or "")[:800]
                else:
                    try:
                        out = resp.json()
                    except Exception:
                        out = {"raw": resp.text}
                    break
            except Exception as e:  # noqa: BLE001
                last_err = repr(e)
                if attempt < settings.http_retries:
                    await asyncio.sleep(0.3 * (attempt + 1))
                    continue
            if out is not None:
                break

    if out is not None:
        if isinstance(out, dict):
            out.setdefault("_meta", {})
            out["_meta"]["user_id"] = user.get("id")
            out["_meta"]["username"] = user.get("username")
        return {"ok": True, "upstream": out}

    # The current digitize upstream requires multipart/form-data field "file".
    # Avoid falling back to JSON after a multipart failure, because that only
    # produces a misleading upstream "body.file missing" error.
    if raw_bytes:
        detail = f"AI 心电图数字化失败 upstream={upstream_url}"
        if last_text:
            detail += f" resp={last_text}"
        if last_err:
            detail += f" err={last_err}"
        raise HTTPException(status_code=last_status, detail=detail[:1500])

    # Fallback mode: JSON body variants, used only when there is no upload file.
    for body in candidates:
        try:
            headers = {"Content-Type": "application/json"}
            resp = await post_with_retry(upstream_url, headers, body)
            if resp.status_code >= 400:
                last_status = resp.status_code
                last_text = (resp.text or "")[:800]
                continue
            try:
                out = resp.json()
            except Exception:
                out = {"raw": resp.text}
            break
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
            continue

    if out is None:
        detail = f"AI 心电图数字化失败 upstream={upstream_url}"
        if last_text:
            detail += f" resp={last_text}"
        if last_err:
            detail += f" err={last_err}"
        raise HTTPException(status_code=last_status, detail=detail[:1500])

    if isinstance(out, dict):
        out.setdefault("_meta", {})
        out["_meta"]["user_id"] = user.get("id")
        out["_meta"]["username"] = user.get("username")
    return {"ok": True, "upstream": out}


@app.post("/api/chest-pain/predict")
@app.post("/api/zhunxin/predict")
async def chest_pain_predict(
    file: UploadFile = File(...),
    use_optimized_rank: bool = Form(default=True),
    show_score: bool = Form(default=True),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    filename = str(file.filename or "ecg_image.jpg")
    content_type = str(file.content_type or "application/octet-stream")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="uploaded file is empty")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large, max {settings.max_upload_mb}MB")

    result = await predict_image_and_report(
        file_bytes=content,
        filename=filename,
        content_type=content_type,
        url=(settings.chest_pain_predict_url or "").strip(),
        timeout_seconds=settings.http_timeout,
        use_optimized_rank=use_optimized_rank,
        show_score=show_score,
    )
    result["summary"] = await _summarize_chest_pain_result_with_llm(result=result, user=user)
    result["_meta"] = {
        "user_id": user.get("id"),
        "username": user.get("username"),
        "source_file": filename,
        "upstream_url": settings.chest_pain_predict_url,
    }
    return {"ok": True, **result}


@app.post("/api/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    source: str = Form(default="heartos"),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix or ".bin"
    fid = uuid.uuid4().hex
    safe_name = f"{fid}{suffix}"
    out_path = UPLOAD_DIR / safe_name

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large, max {settings.max_upload_mb}MB")

    out_path.write_bytes(content)
    meta = _load_file_meta()
    meta[safe_name] = {"user_id": user.get("id"), "username": user.get("username"), "source": source}
    _save_file_meta(meta)

    url = f"{settings.public_base_url}/api/files/{safe_name}"
    return {
        "id": safe_name,
        "name": file.filename,
        "size": len(content),
        "source": source,
        "url": url,
        "fileUrl": url,
        "user_id": user.get("id"),
        "username": user.get("username"),
    }


@app.post("/api/pdf/first-page-image")
async def pdf_first_page_image(
    file: UploadFile | None = File(default=None),
    file_id: str = Form(default=""),
    scale: float = Form(default=1.5),
    user: dict[str, Any] = Depends(get_current_user),
) -> Response:
    raw = b""
    filename = "source.pdf"

    if file is not None:
        raw = await file.read()
        filename = file.filename or filename
    elif file_id:
        p = (UPLOAD_DIR / file_id).resolve()
        if not str(p).startswith(str(UPLOAD_DIR)):
            raise HTTPException(status_code=400, detail="invalid file path")
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        meta = _load_file_meta().get(file_id)
        if meta and meta.get("user_id") != user.get("id"):
            raise HTTPException(status_code=403, detail="forbidden")
        raw = p.read_bytes()
        filename = p.name
    else:
        raise HTTPException(status_code=422, detail="missing PDF file or file_id")

    if not raw:
        raise HTTPException(status_code=422, detail="PDF file is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large, max {settings.max_upload_mb}MB")
    if not raw.lstrip().startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail=f"{filename} is not a PDF file")

    try:
        import fitz  # PyMuPDF
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="PyMuPDF is not installed on server") from exc

    try:
        zoom = max(1.0, min(float(scale or 2.0), 4.0))
        doc = fitz.open(stream=raw, filetype="pdf")
        if doc.page_count < 1:
            raise HTTPException(status_code=422, detail="PDF has no pages")
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        png = pix.tobytes("png")
        doc.close()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"PDF render failed: {exc}") from exc

    return Response(content=png, media_type="image/png")


@app.get("/api/files/{file_id}")
async def get_file(file_id: str, user: dict[str, Any] = Depends(get_current_user)) -> FileResponse:
    p = (UPLOAD_DIR / file_id).resolve()
    if not str(p).startswith(str(UPLOAD_DIR)):
        raise HTTPException(status_code=400, detail="invalid file path")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    meta = _load_file_meta().get(file_id)
    if meta and meta.get("user_id") != user.get("id"):
        raise HTTPException(status_code=403, detail="forbidden")

    return FileResponse(path=str(p), filename=p.name)


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    p = (UPLOAD_DIR / file_id).resolve()
    if not str(p).startswith(str(UPLOAD_DIR)):
        raise HTTPException(status_code=400, detail="invalid file path")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    meta = _load_file_meta()
    item = meta.get(file_id)
    if item and str(item.get("user_id")) != str(user.get("id")):
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        p.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"delete failed: {e}") from e

    if file_id in meta:
        del meta[file_id]
        _save_file_meta(meta)

    return {"ok": True, "id": file_id}


@app.get("/api/files")
async def list_files(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    owner = str(user.get("id"))
    meta = _load_file_meta()
    items = []
    for p in sorted(UPLOAD_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file() or p.name.startswith('.'):
            continue
        m = meta.get(p.name)
        if m and str(m.get("user_id")) != owner:
            continue
        items.append(
            {
                "id": p.name,
                "name": p.name,
                "size": p.stat().st_size,
                "url": f"{settings.public_base_url}/api/files/{p.name}",
                "user_id": user.get("id"),
                "username": user.get("username"),
            }
        )
    return {"items": items, "user_id": user.get("id"), "username": user.get("username")}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
