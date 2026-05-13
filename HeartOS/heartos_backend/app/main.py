from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .auth import (
    UpstreamAuthError,
    issue_token,
    register_user,
    upstream_login,
    upstream_register,
    verify_token,
    verify_user,
)
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
    ECGOmicsAnalyzeRequest,
    HandEcgSaveRequest,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    MeResponse,
)


APP_VERSION = "1.4.1"

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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        "target 取值示例: /api/ai-ecg-digitize, /api/ecg-reconstruct, /api/ecgomics/analyze, /tool/handecg/manual, /tool/report/generate, /tool/rag/search, ecg, ml, dl, stats, zhipu。"
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
        return {"intent": "chat", "route": "model", "target": "zhipu", "reason": "fallback", "need_fields": []}
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

    return {
        "id": payload.get("uid"),
        "username": payload.get("username"),
        "display_name": payload.get("name") or payload.get("username"),
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


def _normalize_reconstruct_csv(content: bytes) -> tuple[bytes, dict[str, Any]]:
    text = content.decode("utf-8-sig", errors="ignore").strip()
    if not text:
        raise HTTPException(status_code=422, detail="uploaded csv is empty")

    rows = [row for row in csv.reader(io.StringIO(text)) if any(str(cell).strip() for cell in row)]
    if not rows:
        raise HTTPException(status_code=422, detail="uploaded csv has no rows")

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

    numeric_rows: list[list[str]] = []
    skipped_rows = 0
    for row in rows:
        nums: list[str] = []
        ok = True
        for cell in row:
            v = _try_float(cell)
            if v is None:
                ok = False
                break
            nums.append(format(v, ".12g"))
        if ok and nums:
            numeric_rows.append(nums)
        else:
            skipped_rows += 1

    if not numeric_rows:
        raise HTTPException(status_code=422, detail="csv normalization produced no numeric rows")

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(numeric_rows)
    meta = {
        "header_removed": header_removed,
        "index_removed": index_removed,
        "skipped_rows": skipped_rows,
        "rows": len(numeric_rows),
        "columns": len(numeric_rows[0]) if numeric_rows else 0,
        "lead_order": lead_order,
    }
    return out.getvalue().encode("utf-8"), meta


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
            user = await asyncio.to_thread(upstream_login, req.username, req.password)
        except UpstreamAuthError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
    else:
        user = verify_user(req.username, req.password)
        if not user:
            raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = issue_token(user)
    return LoginResponse(
        token=token,
        user_id=str(user.get("id")),
        username=str(user.get("username")),
        display_name=str(user.get("display_name") or user.get("username")),
        expires_in=max(1, settings.auth_expire_hours) * 3600,
    )



@app.post("/api/auth/register", response_model=LoginResponse)
async def register(req: RegisterRequest) -> LoginResponse:
    if (settings.auth_mode or "").lower() == "upstream":
        try:
            user = await asyncio.to_thread(upstream_register, req.username, req.password, req.display_name)
        except UpstreamAuthError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
    else:
        try:
            user = register_user(req.username, req.password, req.display_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    token = issue_token(user)
    return LoginResponse(
        token=token,
        user_id=str(user.get("id")),
        username=str(user.get("username")),
        display_name=str(user.get("display_name") or user.get("username")),
        expires_in=max(1, settings.auth_expire_hours) * 3600,
    )

@app.get("/api/auth/me", response_model=MeResponse)
async def me(user: dict[str, Any] = Depends(get_current_user)) -> MeResponse:
    return MeResponse(user_id=str(user.get("id")), username=str(user.get("username")), display_name=str(user.get("display_name")))


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


def _normalize_notebook_item(raw: dict[str, Any]) -> dict[str, Any]:
    nid = str(raw.get("id") or "").strip()
    if not nid:
        raise HTTPException(status_code=422, detail="notebook id is required")
    sources = raw.get("sources")
    msgs = raw.get("msgs")
    return {
        "id": nid,
        "title": str(raw.get("title") or "New Conversation"),
        "icon": str(raw.get("icon") or "📔"),
        "color": str(raw.get("color") or "#e8f0fe"),
        "date": str(raw.get("date") or ""),
        "sources": sources if isinstance(sources, list) else [],
        "msgs": msgs if isinstance(msgs, list) else [],
    }


@app.get("/api/notebooks")
async def list_notebooks(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    db = _load_json_file(NOTEBOOKS_PATH)
    items = db.get(uid, [])
    if not isinstance(items, list):
        items = []
    return {"items": items}


@app.post("/api/notebooks")
async def upsert_notebook(payload: dict[str, Any], user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    uid = str(user.get("id") or "")
    item = _normalize_notebook_item(payload if isinstance(payload, dict) else {})

    db = _load_json_file(NOTEBOOKS_PATH)
    items = db.get(uid, [])
    if not isinstance(items, list):
        items = []

    replaced = False
    for i, existing in enumerate(items):
        if isinstance(existing, dict) and str(existing.get("id")) == item["id"]:
            items[i] = item
            replaced = True
            break
    if not replaced:
        items.insert(0, item)

    db[uid] = items
    _save_json_file(NOTEBOOKS_PATH, db)
    return {"ok": True, "id": item["id"]}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: dict[str, Any] = Depends(get_current_user)) -> ChatResponse:
    provider = (req.provider or settings.llm_default_provider).strip().lower()
    model = (req.model or settings.llm_default_model).strip()
    msg_list = [{"role": m.role, "content": m.content} for m in req.messages]

    result = await LLM_GATEWAY.chat(
        provider_key=provider,
        model=model,
        system=req.system or "",
        messages=msg_list,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        user=user,
        override_api_key=req.api_key or "",
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
    msg_text = (req.message or "").strip().lower()
    has_image = bool((req.context or {}).get("has_image"))
    has_xml = bool((req.context or {}).get("has_xml"))
    has_csv = bool((req.context or {}).get("has_csv"))

    wants_action = any(k in msg_text for k in ("帮我", "请", "开始", "进行", "执行", "处理", "生成", "提取", "分析", "补全", "重建", "跑一下", "run", "do"))
    asks_concept = any(k in msg_text for k in ("什么是", "是什么", "解释", "介绍", "原理", "概念", "why", "what is", "how"))
    wants_ecg_features = (
        ("特征提取" in msg_text or "提取特征" in msg_text or "特征分析" in msg_text)
        and ("心电" in msg_text or "ecg" in msg_text)
    )
    wants_ecg_reconstruct = (
        ("补全" in msg_text or "重建" in msg_text or "reconstruct" in msg_text)
        and ("心电" in msg_text or "ecg" in msg_text or "导联" in msg_text or "波形" in msg_text or has_csv)
    )

    if "手动" in msg_text and "数字化" in msg_text and wants_action and not asks_concept:
        route_plan = {"intent": "ecg_manual_digitize", "route": "api", "target": "/tool/handecg/manual"}
    elif wants_ecg_reconstruct and wants_action and not asks_concept:
        route_plan = {"intent": "ecg_reconstruct", "route": "api", "target": "/api/ecg-reconstruct"}
    elif "自动" in msg_text and "数字化" in msg_text and wants_action and not asks_concept:
        route_plan = {"intent": "ecg_digitize", "route": "api", "target": "/api/ai-ecg-digitize"}
    elif ("ecgomics" in msg_text or has_xml or wants_ecg_features) and wants_action and not asks_concept:
        route_plan = {"intent": "ecgomics_analyze", "route": "api", "target": "/api/ecgomics/analyze"}
    else:
        # Route B: perform LLM intent extraction for chat input, then fall back
        # to deterministic routing only if the LLM router itself is unavailable.
        try:
            route_plan = await _classify_intent_with_zhipu(
                message=req.message,
                context=req.context,
                user=user,
                api_key=req.api_key,
            )
        except Exception:
            if "手动" in msg_text and "数字化" in msg_text:
                route_plan = {"intent": "ecg_manual_digitize", "route": "api", "target": "/tool/handecg/manual"}
            elif wants_ecg_reconstruct:
                route_plan = {"intent": "ecg_reconstruct", "route": "api", "target": "/api/ecg-reconstruct"}
            elif "自动" in msg_text and "数字化" in msg_text:
                route_plan = {"intent": "ecg_digitize", "route": "api", "target": "/api/ai-ecg-digitize"}
            elif "ecgomics" in msg_text or has_xml:
                route_plan = {"intent": "ecgomics_analyze", "route": "api", "target": "/api/ecgomics/analyze"}
            else:
                route_plan = {"intent": "chat", "route": "model", "target": "zhipu"}

    intent = str(route_plan.get("intent") or "chat")
    route = str(route_plan.get("route") or "model")
    target = str(route_plan.get("target") or "zhipu")

    if intent == "ecg_manual_digitize" or target == "/tool/handecg/manual":
        return AgentAutoRunResponse(
            intent="ecg_manual_digitize",
            route="api",
            target="/tool/handecg/manual",
            reply="已识别为手动数字化任务，准备打开 HandECG 手动数字化工具。",
            action={"endpoint": "/tool/handecg/manual", "method": "OPEN"},
        )

    if intent == "ecg_digitize" or target == "/api/ai-ecg-digitize":
        return AgentAutoRunResponse(
            intent="ecg_digitize",
            route="api",
            target="/api/ai-ecg-digitize",
            reply="已识别为自动数字化任务，请调用 /api/ai-ecg-digitize 并提交图像文件或 image_base64。",
            action={"endpoint": "/api/ai-ecg-digitize", "method": "POST", "required": ["file 或 image_base64"]},
        )

    if intent == "ecg_reconstruct" or target == "/api/ecg-reconstruct":
        return AgentAutoRunResponse(
            intent="ecg_reconstruct",
            route="api",
            target="/api/ecg-reconstruct",
            reply="已识别为心电图重建任务，请调用 /api/ecg-reconstruct 并提交 CSV 文件。",
            action={"endpoint": "/api/ecg-reconstruct", "method": "POST", "required": ["CSV file"]},
        )

    if intent == "ecgomics_analyze" or target == "/api/ecgomics/analyze":
        return AgentAutoRunResponse(
            intent="ecgomics_analyze",
            route="api",
            target="/api/ecgomics/analyze",
            reply="已识别为 ECGOmics 分析任务，请调用 /api/ecgomics/analyze 并提交 raw/xml 数据。",
            action={"endpoint": "/api/ecgomics/analyze", "method": "POST", "required": ["inputType + 数据体"]},
        )

    if route == "agent" or intent == "agent_run":
        agent_id = target if target in AGENT_SYSTEM_PROMPTS else "ecg"
        agent_req = AgentRunRequest(
            agent_id=agent_id,
            api_key=req.api_key,
            provider=req.provider,
            model=req.model,
            messages=[{"role": "user", "content": req.message}],
            max_tokens=req.max_tokens,
        )
        ran = await run_agent(agent_id, agent_req, user)
        return AgentAutoRunResponse(
            intent="agent_run",
            route="agent",
            target=agent_id,
            reply=ran.reply,
            action={"agent_id": agent_id, "provider": ran.provider, "model": ran.model},
        )

    chat_req = ChatRequest(
        provider=req.provider,
        model=req.model,
        api_key=req.api_key,
        system="",
        messages=[{"role": "user", "content": req.message}],
        max_tokens=req.max_tokens,
        temperature=req.temperature,
    )
    chat_resp = await chat(chat_req, user)
    return AgentAutoRunResponse(
        intent="chat",
        route="model",
        target=chat_resp.provider,
        reply=chat_resp.reply,
        action={"provider": chat_resp.provider, "model": chat_resp.model},
    )


@app.post("/api/ecgomics/analyze")
async def ecgomics_analyze(req: ECGOmicsAnalyzeRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if req.inputType == "raw":
        if not req.ecgData or req.ecgSampleRate is None:
            raise HTTPException(status_code=400, detail="raw 模式需要 ecgData 和 ecgSampleRate")
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
        raise HTTPException(status_code=500, detail="未配置 HandECG 上传地址")

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

    upstream_content, csv_meta = _normalize_reconstruct_csv(content)
    upstream_filename = filename if filename.lower().endswith(".csv") else f"{filename}.csv"
    content_type = str(file.content_type or "text/csv")
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

    # Fallback mode: JSON body variants
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
