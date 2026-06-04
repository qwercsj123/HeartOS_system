from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .config import settings


USERS_PATH = Path(settings.users_file).resolve()
USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
PHONE_RE = re.compile(r"^1\d{10}$")
CODE_TTL_SECONDS = 300
SEND_INTERVAL_SECONDS = 60
SEND_LIMIT_PER_HOUR = 5
VERIFY_ATTEMPT_LIMIT = 5
FLOW_TICKET_TTL_SECONDS = 600
PASSWORD_SUFFIX = "HeartOS123++--**"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode((s + pad).encode())


def _sha256_hex(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def _hash_password(password: str, salt: str) -> str:
    return _sha256_hex(f"{salt}:{password}")


def _strip_password_suffix(password: str) -> str:
    value = str(password or "")
    if value.endswith(PASSWORD_SUFFIX):
        return value[: -len(PASSWORD_SUFFIX)]
    return value


def _is_client_password_digest(password: str) -> bool:
    value = str(password or "")
    prefix = "heartos:v1:sha256:"
    if not value.startswith(prefix):
        return False
    digest = value[len(prefix):]
    return len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower())


def _client_password_digest(username: str, password: str) -> str:
    normalized_user = str(username or "").strip().lower()
    digest = _sha256_hex(f"heartos-auth-v1:{normalized_user}:{password or ''}")
    return f"heartos:v1:sha256:{digest}"


def _is_client_password_heartos(password: str) -> bool:
    value = str(password or "")
    return value.startswith("HeartOSheartos-auth-v1:")


def _client_password_heartos(username: str, password: str) -> str:
    normalized_user = str(username or "").strip().lower()
    return "HeartOSheartos-auth-v1:" + normalized_user + ":" + str(password or "")


def _now_ts() -> int:
    return int(time.time())


def _normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


def _mask_phone(phone: str) -> str:
    value = _normalize_phone(phone)
    if len(value) >= 7:
        return value[:3] + "****" + value[-4:]
    return value


def _validate_phone(phone: str) -> str:
    normalized = _normalize_phone(phone)
    if not PHONE_RE.fullmatch(normalized):
        raise ValueError("请输入有效的 11 位手机号")
    return normalized


def _public_user(u: dict[str, Any]) -> dict[str, Any]:
    username = str(u.get("username") or u.get("phone") or "")
    display_name = str(u.get("display_name") or u.get("name") or username)
    return {
        "id": str(u.get("id") or ""),
        "username": username,
        "phone": str(u.get("phone") or ""),
        "display_name": display_name,
        "name": str(u.get("name") or display_name),
        "organization": str(u.get("organization") or ""),
        "department": str(u.get("department") or ""),
        "title": str(u.get("title") or ""),
        "user_type": str(u.get("user_type") or ""),
        "use_case": str(u.get("use_case") or ""),
        "email": str(u.get("email") or ""),
        "is_admin": bool(u.get("is_admin")),
        "active": bool(u.get("active", True)),
        "created_at": int(u.get("created_at") or 0),
        "last_login_at": int(u.get("last_login_at") or 0),
    }


def _normalize_user_record(raw: dict[str, Any]) -> dict[str, Any]:
    now = _now_ts()
    phone = _normalize_phone(str(raw.get("phone") or ""))
    username = str(raw.get("username") or phone or "").strip()
    display_name = str(raw.get("display_name") or raw.get("name") or username).strip() or username
    record = {
        "id": str(raw.get("id") or f"u_{secrets.token_hex(6)}"),
        "username": username,
        "phone": phone,
        "phone_verified": bool(raw.get("phone_verified", bool(phone))),
        "phone_verified_at": int(raw.get("phone_verified_at") or (now if phone else 0)),
        "display_name": display_name,
        "name": str(raw.get("name") or display_name),
        "organization": str(raw.get("organization") or ""),
        "department": str(raw.get("department") or ""),
        "title": str(raw.get("title") or ""),
        "user_type": str(raw.get("user_type") or ""),
        "use_case": str(raw.get("use_case") or ""),
        "email": str(raw.get("email") or ""),
        "salt": str(raw.get("salt") or ""),
        "password_hash": str(raw.get("password_hash") or ""),
        "active": bool(raw.get("active", True)),
        "is_admin": bool(raw.get("is_admin", str(raw.get("id") or "") == "u_admin")),
        "created_at": int(raw.get("created_at") or now),
        "last_login_at": int(raw.get("last_login_at") or 0),
    }
    return record


def _load_users() -> dict[str, Any]:
    if not USERS_PATH.exists():
        _init_default_user()
    try:
        raw = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        _init_default_user()
        raw = json.loads(USERS_PATH.read_text(encoding="utf-8"))

    if not isinstance(raw, dict):
        raw = {}
    users = raw.get("users")
    codes = raw.get("verification_codes")
    if not isinstance(users, list):
        users = []
    if not isinstance(codes, list):
        codes = []

    normalized_users = [_normalize_user_record(item) for item in users if isinstance(item, dict)]
    normalized_codes = [item for item in codes if isinstance(item, dict)]
    data = {"users": normalized_users, "verification_codes": normalized_codes}
    if data != raw:
        _save_users(data)
    return data


def _save_users(data: dict[str, Any]) -> None:
    USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_default_user() -> None:
    salt = secrets.token_hex(8)
    data = {
        "users": [
            {
                "id": "u_admin",
                "username": settings.default_username,
                "phone": _normalize_phone(str(getattr(settings, "default_admin_phone", "") or "")),
                "phone_verified": bool(_normalize_phone(str(getattr(settings, "default_admin_phone", "") or ""))),
                "phone_verified_at": _now_ts() if _normalize_phone(str(getattr(settings, "default_admin_phone", "") or "")) else 0,
                "display_name": "Administrator",
                "name": "Administrator",
                "organization": "HeartOS",
                "user_type": "admin",
                "use_case": "system",
                "salt": salt,
                "password_hash": _hash_password(settings.default_password, salt),
                "active": True,
                "is_admin": True,
                "created_at": _now_ts(),
                "last_login_at": 0,
            }
        ],
        "verification_codes": [],
    }
    _save_users(data)


def _find_user(db: dict[str, Any], *, username: str = "", phone: str = "", user_id: str = "") -> dict[str, Any] | None:
    username_lc = str(username or "").strip().lower()
    phone_norm = _normalize_phone(phone)
    target_id = str(user_id or "").strip()
    for u in db.get("users", []):
        if not isinstance(u, dict):
            continue
        if target_id and str(u.get("id") or "") == target_id:
            return u
        if phone_norm and _normalize_phone(str(u.get("phone") or "")) == phone_norm:
            return u
        if username_lc and str(u.get("username") or "").strip().lower() == username_lc:
            return u
    return None


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    db = _load_users()
    user = _find_user(db, user_id=user_id)
    if not user or not user.get("active", True):
        return None
    return _public_user(user)


def _touch_last_login(user: dict[str, Any]) -> None:
    db = _load_users()
    target = _find_user(db, user_id=str(user.get("id") or ""))
    if not target:
        return
    target["last_login_at"] = _now_ts()
    _save_users(db)


def _verify_plain_password(user: dict[str, Any], password: str) -> bool:
    username = str(user.get("username") or user.get("phone") or "")
    salt = str(user.get("salt", ""))
    if not salt:
        return False
    stored_hash = str(user.get("password_hash", ""))
    if hmac.compare_digest(_hash_password(password, salt), stored_hash):
        return True
    plain_password = _strip_password_suffix(password)
    if plain_password != password and hmac.compare_digest(_hash_password(plain_password, salt), stored_hash):
        user["password_hash"] = _hash_password(password, salt)
        return True
    derived_password = _client_password_digest(username, password)
    if hmac.compare_digest(_hash_password(derived_password, salt), stored_hash):
        user["password_hash"] = _hash_password(password, salt)
        return True
    derived_password_v2 = _client_password_heartos(username, password)
    if hmac.compare_digest(_hash_password(derived_password_v2, salt), stored_hash):
        user["password_hash"] = _hash_password(password, salt)
        return True
    return False


def verify_user(username: str, password: str, *, phone: str = "") -> dict[str, Any] | None:
    db = _load_users()
    user = _find_user(db, username=username, phone=phone)
    if not user or not user.get("active", True):
        return None
    if not _verify_plain_password(user, password):
        return None
    user["last_login_at"] = _now_ts()
    _save_users(db)
    return _public_user(user)



def _validate_password(password: str, username: str = "") -> None:
    if not _is_client_password_digest(password) and not _is_client_password_heartos(password):
        plain_password = _strip_password_suffix(password)
        if len(plain_password or "") < 6:
            raise ValueError("密码至少 6 位")
        if not any(c.isalpha() for c in plain_password) or not any(c.isdigit() for c in plain_password):
            raise ValueError("密码需同时包含字母和数字")


def _cleanup_codes(codes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = _now_ts()
    cleaned: list[dict[str, Any]] = []
    for item in codes:
        if not isinstance(item, dict):
            continue
        created_at = int(item.get("created_at") or 0)
        expires_at = int(item.get("expires_at") or 0)
        if expires_at and expires_at < now - 3600:
            continue
        if created_at and created_at < now - 86400:
            continue
        cleaned.append(item)
    return cleaned


def _issue_verification_code(phone: str, purpose: str) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    purpose_key = str(purpose or "").strip() or "register"
    db = _load_users()
    now = _now_ts()
    db["verification_codes"] = _cleanup_codes(list(db.get("verification_codes") or []))
    same_scope = [
        item for item in db["verification_codes"]
        if _normalize_phone(str(item.get("phone") or "")) == phone_norm and str(item.get("purpose") or "") == purpose_key
    ]
    latest = max(same_scope, key=lambda item: int(item.get("created_at") or 0), default=None)
    if latest and now - int(latest.get("created_at") or 0) < SEND_INTERVAL_SECONDS:
        raise ValueError(f"验证码发送过于频繁，请在 {SEND_INTERVAL_SECONDS} 秒后再试")
    recent_hour = [item for item in same_scope if now - int(item.get("created_at") or 0) < 3600]
    if len(recent_hour) >= SEND_LIMIT_PER_HOUR:
        raise ValueError("该手机号验证码发送次数过多，请稍后再试")

    code = f"{secrets.randbelow(1000000):06d}"
    entry = {
        "id": "vc_" + secrets.token_hex(8),
        "phone": phone_norm,
        "purpose": purpose_key,
        "code_hash": _sha256_hex(code),
        "created_at": now,
        "expires_at": now + CODE_TTL_SECONDS,
        "used": False,
        "attempts": 0,
    }
    db["verification_codes"].append(entry)
    _save_users(db)
    return {
        "ok": True,
        "expires_in": CODE_TTL_SECONDS,
        "retry_after": SEND_INTERVAL_SECONDS,
        "debug_code": code,
        "masked_phone": _mask_phone(phone_norm),
    }


def send_verification_code(phone: str, purpose: str) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    purpose_key = str(purpose or "").strip() or "register"
    db = _load_users()
    existing_user = _find_user(db, phone=phone_norm)
    if purpose_key == "register" and existing_user and existing_user.get("active", True):
        raise ValueError("该手机号已注册，请直接登录或找回密码")
    if purpose_key == "reset_password" and (not existing_user or not existing_user.get("active", True)):
        raise ValueError("该手机号尚未注册")
    if (settings.phone_send_code_url or "").strip():
        status, data, text = _post_form_url(settings.phone_send_code_url, {"phone": phone_norm})
        if status >= 400 or not _is_phone_service_ok(data):
            message = _finalize_upstream_error_message(data, text, "验证码发送失败，请稍后重试")
            raise ValueError(message)
        return {"ok": True, "expires_in": CODE_TTL_SECONDS, "retry_after": SEND_INTERVAL_SECONDS, "debug_code": ""}
    return _issue_verification_code(phone_norm, purpose_key)


def _check_verification_code(phone: str, purpose: str, code: str, *, consume: bool) -> None:
    phone_norm = _validate_phone(phone)
    code_text = str(code or "").strip()
    if not code_text:
        raise ValueError("请输入验证码")
    if (settings.phone_login_by_code_url or "").strip():
        status, data, text = _post_form_url(settings.phone_login_by_code_url, {"phone": phone_norm, "code": code_text})
        if status >= 400 or not _is_phone_service_ok(data):
            message = _finalize_upstream_error_message(data, text, "验证码错误或已失效，请重新获取")
            raise ValueError(message)
        return
    db = _load_users()
    db["verification_codes"] = _cleanup_codes(list(db.get("verification_codes") or []))
    candidates = [
        item for item in db["verification_codes"]
        if _normalize_phone(str(item.get("phone") or "")) == phone_norm and str(item.get("purpose") or "") == str(purpose or "")
    ]
    candidates.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    target = candidates[0] if candidates else None
    if not target or bool(target.get("used")):
        raise ValueError("验证码不存在或已失效")
    now = _now_ts()
    if int(target.get("expires_at") or 0) < now:
        raise ValueError("验证码已过期，请重新获取")
    target["attempts"] = int(target.get("attempts") or 0) + 1
    if target["attempts"] > VERIFY_ATTEMPT_LIMIT:
        target["used"] = True
        _save_users(db)
        raise ValueError("验证码输入次数过多，请重新获取")
    if _sha256_hex(code_text) != str(target.get("code_hash") or ""):
        _save_users(db)
        raise ValueError("验证码错误")
    if consume:
        target["used"] = True
    _save_users(db)


def _consume_verification_code(phone: str, purpose: str, code: str) -> None:
    _check_verification_code(phone, purpose, code, consume=True)


def verify_verification_code(phone: str, purpose: str, code: str) -> None:
    _check_verification_code(phone, purpose, code, consume=False)


def register_user(
    *,
    phone: str,
    password: str,
    code: str,
    verification_token: str | None = None,
    name: str,
    organization: str,
    user_type: str,
    use_case: str,
    department: str | None = None,
    title: str | None = None,
    email: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    _validate_password(password, phone_norm)

    db = _load_users()
    if _find_user(db, phone=phone_norm):
        raise ValueError("该手机号已注册")
    if not verify_verification_ticket(str(verification_token or ""), phone_norm, "register"):
        _consume_verification_code(phone_norm, "register", code)

    uid = "u_" + secrets.token_hex(6)
    salt = secrets.token_hex(8)
    now = _now_ts()
    user = _normalize_user_record(
        {
            "id": uid,
            "username": phone_norm,
            "phone": phone_norm,
            "phone_verified": True,
            "phone_verified_at": now,
            "display_name": (display_name or name or phone_norm).strip() or phone_norm,
            "name": str(name or "").strip() or phone_norm,
            "organization": str(organization or "").strip(),
            "department": str(department or "").strip(),
            "title": str(title or "").strip(),
            "user_type": str(user_type or "").strip(),
            "use_case": str(use_case or "").strip(),
            "email": str(email or "").strip(),
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "active": True,
            "created_at": now,
            "last_login_at": now,
        }
    )
    db.setdefault("users", []).append(user)
    _save_users(db)
    return _public_user(user)


def send_password_reset_code(phone: str) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    db = _load_users()
    user = _find_user(db, phone=phone_norm)
    if not user or not user.get("active", True):
        raise ValueError("该手机号尚未注册")
    if (settings.phone_send_code_url or "").strip():
        status, data, text = _post_form_url(settings.phone_send_code_url, {"phone": phone_norm})
        if status >= 400 or not _is_phone_service_ok(data):
            message = _finalize_upstream_error_message(data, text, "验证码发送失败，请稍后重试")
            raise ValueError(message)
        return {"ok": True, "expires_in": CODE_TTL_SECONDS, "retry_after": SEND_INTERVAL_SECONDS, "debug_code": ""}
    return _issue_verification_code(phone_norm, "reset_password")


def reset_password(phone: str, code: str, new_password: str, verification_token: str | None = None) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    _validate_password(new_password, phone_norm)
    db = _load_users()
    user = _find_user(db, phone=phone_norm)
    if not user or not user.get("active", True):
        raise ValueError("账号不存在")
    if not verify_verification_ticket(str(verification_token or ""), phone_norm, "reset_password"):
        _consume_verification_code(phone_norm, "reset_password", code)
    salt = str(user.get("salt") or secrets.token_hex(8))
    user["salt"] = salt
    user["password_hash"] = _hash_password(new_password, salt)
    _save_users(db)
    return _public_user(user)


def verify_password_reset_code(phone: str, code: str) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    db = _load_users()
    user = _find_user(db, phone=phone_norm)
    if not user or not user.get("active", True):
        raise ValueError("账号不存在")
    verify_verification_code(phone_norm, "reset_password", code)
    return _public_user(user)


def verify_registration_code(phone: str, code: str) -> None:
    phone_norm = _validate_phone(phone)
    db = _load_users()
    if _find_user(db, phone=phone_norm):
        raise ValueError("该手机号已注册，请直接登录或找回密码")
    verify_verification_code(phone_norm, "register", code)


def issue_verification_ticket(phone: str, purpose: str) -> str:
    now = _now_ts()
    payload = {
        "phone": _validate_phone(phone),
        "purpose": str(purpose or "").strip() or "register",
        "iat": now,
        "exp": now + FLOW_TICKET_TTL_SECONDS,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(settings.auth_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_verification_ticket(ticket: str, phone: str, purpose: str) -> bool:
    token = str(ticket or "").strip()
    if not token:
        return False
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return False
    calc = _b64e(hmac.new(settings.auth_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(calc, sig):
        return False
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return False
    if int(payload.get("exp", 0)) < _now_ts():
        return False
    return (
        _normalize_phone(str(payload.get("phone") or "")) == _validate_phone(phone)
        and str(payload.get("purpose") or "") == (str(purpose or "").strip() or "register")
    )


def change_password(user_id: str, old_password: str, new_password: str) -> dict[str, Any]:
    db = _load_users()
    user = _find_user(db, user_id=user_id)
    if not user or not user.get("active", True):
        raise ValueError("账号不存在")
    if not _verify_plain_password(user, old_password):
        raise ValueError("原密码错误")
    _validate_password(new_password, str(user.get("username") or user.get("phone") or ""))
    user["password_hash"] = _hash_password(new_password, str(user.get("salt") or ""))
    _save_users(db)
    return _public_user(user)


def update_profile(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    db = _load_users()
    user = _find_user(db, user_id=user_id)
    if not user or not user.get("active", True):
        raise ValueError("账号不存在")
    editable_keys = ("display_name", "name", "organization", "department", "title", "user_type", "use_case", "email")
    for key in editable_keys:
        if key in payload and payload.get(key) is not None:
            user[key] = str(payload.get(key) or "").strip()
    user["display_name"] = str(user.get("display_name") or user.get("name") or user.get("phone") or user.get("username") or "").strip()
    if not user["display_name"]:
        user["display_name"] = str(user.get("phone") or user.get("username") or "用户")
    if not str(user.get("name") or "").strip():
        user["name"] = user["display_name"]
    _save_users(db)
    return _public_user(user)


def list_users_for_admin() -> list[dict[str, Any]]:
    db = _load_users()
    items = []
    for item in db.get("users", []):
        if not isinstance(item, dict):
            continue
        pub = _public_user(item)
        pub["user_id"] = pub.get("id") or ""
        items.append(pub)
    items.sort(key=lambda item: (item.get("created_at") or 0), reverse=True)
    return items

def issue_token(user: dict[str, Any]) -> str:
    now = int(time.time())
    exp = now + max(1, settings.auth_expire_hours) * 3600
    payload = {"uid": user.get("id"), "username": user.get("username"), "name": user.get("display_name"), "iat": now, "exp": exp}
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(settings.auth_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_token(token: str) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    calc = _b64e(hmac.new(settings.auth_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(calc, sig):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


class UpstreamAuthError(Exception):
    """外部账号服务调用失败。携带 HTTP 状态码与可读消息。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _pick(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _extract_error_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("detail", "message", "msg", "error", "error_message", "errmsg"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                inner = v.get("message") or v.get("msg")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
        data = payload.get("data")
        if isinstance(data, str) and data.strip():
            return data.strip()
        if isinstance(data, dict):
            inner = _extract_error_message(data, "")
            if inner:
                return inner
    elif isinstance(payload, str) and payload.strip():
        return payload.strip()[:300]
    return fallback


def _finalize_upstream_error_message(payload: Any, text: str, fallback: str) -> str:
    message = _extract_error_message(payload if payload is not None else text, fallback).strip()
    if message.lower() in {"fail", "error", "false"}:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, str) and data.strip():
                return data.strip()
        return fallback
    return message or fallback


def _normalize_upstream_user(raw: Any, fallback_username: str) -> tuple[dict[str, Any], str | None]:
    """从外部接口的响应里抽取出 (user_dict, upstream_token)。

    兼容常见字段命名：
      - token / access_token / accessToken / jwt / authorization
      - user_id / userId / id / uid
      - username / user_name / account / name
      - display_name / displayName / nickname / realName
      - 外层可能套一层 {code, message, data: {...}}；user 信息可能在 data.user
    """
    if not isinstance(raw, dict):
        return {"id": "", "username": fallback_username, "display_name": fallback_username}, None

    container: dict[str, Any] = raw
    if isinstance(raw.get("data"), dict):
        container = {**raw, **raw["data"]}

    upstream_token = _pick(
        container,
        "token",
        "access_token",
        "accessToken",
        "jwt",
        "id_token",
        "authorization",
    )

    user_obj: dict[str, Any] = container
    if isinstance(container.get("user"), dict):
        user_obj = {**container, **container["user"]}
    elif isinstance(container.get("userInfo"), dict):
        user_obj = {**container, **container["userInfo"]}

    uid = _pick(user_obj, "user_id", "userId", "uid", "id", default="")
    username = _pick(user_obj, "username", "user_name", "account", "loginName", default=fallback_username)
    display_name = _pick(
        user_obj,
        "display_name",
        "displayName",
        "nickname",
        "nick_name",
        "realName",
        "real_name",
        "name",
        default=username,
    )

    return (
        {
            "id": str(uid) if uid else f"u_ext_{_sha256_hex(str(username))[:12]}",
            "username": str(username),
            "display_name": str(display_name or username),
        },
        str(upstream_token) if upstream_token else None,
    )


def _post_upstream(path: str, body: dict[str, Any]) -> tuple[int, Any, str]:
    base = (settings.auth_upstream_base or "").rstrip("/")
    if not base:
        raise UpstreamAuthError(500, "未配置外部账号服务地址 APP_AUTH_UPSTREAM_BASE")
    url = base + path
    try:
        with httpx.Client(timeout=httpx.Timeout(settings.http_timeout), trust_env=False) as client:
            resp = client.post(url, json=body, headers={"Content-Type": "application/json"})
    except httpx.RequestError as e:
        raise UpstreamAuthError(502, f"无法连接外部账号服务: {e}") from e

    text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = None
    return resp.status_code, data, text


def _post_form_url(url: str, body: dict[str, Any]) -> tuple[int, Any, str]:
    target = (url or "").strip()
    if not target:
        raise UpstreamAuthError(500, "未配置手机号验证码服务地址")
    httpx_error: str | None = None
    try:
        with httpx.Client(timeout=httpx.Timeout(settings.http_timeout), trust_env=False) as client:
            resp = client.post(target, data=body)
        text = resp.text or ""
        try:
            data = resp.json()
        except Exception:
            data = None
        if resp.status_code < 400 and _is_phone_service_ok(data):
            return resp.status_code, data, text
        httpx_error = text.strip() or f"http {resp.status_code}"
    except httpx.RequestError as e:
        httpx_error = str(e)

    try:
        return _post_form_url_via_curl(target, body)
    except Exception as e:
        detail = httpx_error or str(e) or "unknown error"
        raise UpstreamAuthError(502, f"无法连接手机号验证码服务: {detail}") from e


def _post_form_url_via_curl(url: str, body: dict[str, Any]) -> tuple[int, Any, str]:
    cmd = ["/usr/bin/curl", "-sS", "-X", "POST", url, "--max-time", str(max(5, int(settings.http_timeout)))]
    for key, value in body.items():
        cmd.extend(["--data-urlencode", f"{key}={value}"])
    cmd.extend(["-w", "\n__CURL_HTTP_STATUS__:%{http_code}"])
    resp = subprocess.run(cmd, capture_output=True, text=True, timeout=max(5, int(settings.http_timeout) + 2), check=False)
    output = (resp.stdout or "") + (resp.stderr or "")
    marker = "\n__CURL_HTTP_STATUS__:"
    if marker in output:
        text, status_text = output.rsplit(marker, 1)
        try:
            status = int(status_text.strip())
        except Exception:
            status = 0
    else:
        text = output
        status = 0
    text = text.strip()
    try:
        data = json.loads(text) if text else None
    except Exception:
        data = None
    return status, data, text


def _is_phone_service_ok(data: Any) -> bool:
    return isinstance(data, dict) and int(data.get("code") or 0) == 200


def _has_valid_user_payload(data: Any) -> bool:
    """判断外部接口返回的 body 里是否真的带了有效的用户信息。

    用于防御外部接口"HTTP 200 但 data 是 null / 空 / 没有用户字段"这种伪成功响应。
    """
    if not isinstance(data, dict):
        return False
    container: dict[str, Any] = data
    if isinstance(data.get("data"), dict):
        container = {**data, **data["data"]}
    elif "data" in data and data["data"] is None:
        # 外部明确给了 data: null，视为没有用户载荷
        return False

    user_obj: dict[str, Any] = container
    if isinstance(container.get("user"), dict):
        user_obj = {**container, **container["user"]}
    elif isinstance(container.get("userInfo"), dict):
        user_obj = {**container, **container["userInfo"]}

    uid = _pick(user_obj, "user_id", "userId", "uid", "id", default="")
    username = _pick(user_obj, "username", "user_name", "account", "loginName", default="")
    return bool(uid) or bool(username)


def upstream_login(username: str, password: str) -> dict[str, Any]:
    try:
        status, data, _text = _post_upstream(
            settings.auth_upstream_login_path,
            {"username": username, "password": password},
        )
    except UpstreamAuthError:
        raise UpstreamAuthError(500, "账号或密码错误")

    if status >= 400 or not _has_valid_user_payload(data):
        raise UpstreamAuthError(500, "账号或密码错误")

    user, _upstream_token = _normalize_upstream_user(data, fallback_username=username)
    return user


def upstream_register(username: str, password: str, display_name: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"username": username, "password": password}
    if display_name:
        body["display_name"] = display_name

    try:
        status, data, _text = _post_upstream(settings.auth_upstream_register_path, body)
    except UpstreamAuthError:
        raise UpstreamAuthError(500, "注册失败，用户名重复")

    if status >= 400 or not _has_valid_user_payload(data):
        raise UpstreamAuthError(500, "注册失败，用户名重复")

    user, _upstream_token = _normalize_upstream_user(data, fallback_username=username)
    if not user.get("display_name") and display_name:
        user["display_name"] = display_name
    return user
