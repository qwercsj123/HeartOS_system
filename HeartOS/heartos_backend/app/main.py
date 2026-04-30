from __future__ import annotations

import asyncio
import json
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
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
from .providers import AGENT_SYSTEM_PROMPTS, PROVIDERS
from .schemas import (
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


app = FastAPI(title=settings.name, version="1.4.0")
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

NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _load_file_meta() -> dict[str, Any]:
    if not FILE_META_PATH.exists():
        return {}
    try:
        return json.loads(FILE_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_file_meta(meta: dict[str, Any]) -> None:
    FILE_META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


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


def build_payload(req: ChatRequest, system_override: str | None = None) -> dict[str, Any]:
    msgs: list[dict[str, str]] = []
    system_text = system_override if system_override is not None else req.system
    if system_text:
        msgs.append({"role": "system", "content": system_text})
    msgs.extend([{"role": m.role, "content": m.content} for m in req.messages])
    return {
        "model": req.model or PROVIDERS[req.provider].default_model,
        "messages": msgs,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
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


def parse_reply(data: dict[str, Any]) -> str:
    return (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )


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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "name": settings.name, "version": "1.4.0"}


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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: dict[str, Any] = Depends(get_current_user)) -> ChatResponse:
    provider = PROVIDERS.get(req.provider)
    if not provider:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {req.provider}")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {req.api_key}"}
    if req.provider == "openrouter":
        headers["HTTP-Referer"] = settings.public_base_url
        headers["X-Title"] = "HeartOS"

    payload = build_payload(req)
    payload["user"] = {"id": user.get("id"), "username": user.get("username")}

    resp = await post_with_retry(provider.url, headers, payload)
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"error": {"message": resp.text}}
        msg = err.get("error", {}).get("message", f"HTTP {resp.status_code}")
        raise HTTPException(status_code=resp.status_code, detail=msg)

    data = resp.json()
    reply = parse_reply(data)
    if not reply:
        raise HTTPException(status_code=502, detail="Empty response from provider")

    return ChatResponse(
        reply=reply,
        provider=req.provider,
        model=req.model,
        request_id=resp.headers.get("x-request-id"),
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
