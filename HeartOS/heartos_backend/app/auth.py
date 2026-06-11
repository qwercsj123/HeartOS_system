from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import subprocess
import time
from typing import Any

import httpx

from .config import settings
from . import db as store
from .db import normalize_phone as _normalize_phone
from .db import normalize_user_record as _normalize_user_record


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


def _ensure_default_admin() -> None:
    """users 表为空时初始化默认管理员（与旧版 users.json 不存在时的行为一致）。"""
    if store.user_count() > 0:
        return
    salt = secrets.token_hex(8)
    admin_phone = _normalize_phone(str(getattr(settings, "default_admin_phone", "") or ""))
    now = _now_ts()
    record = _normalize_user_record(
        {
            "id": "u_admin",
            "username": settings.default_username,
            "phone": admin_phone,
            "phone_verified": bool(admin_phone),
            "phone_verified_at": now if admin_phone else 0,
            "display_name": "Administrator",
            "name": "Administrator",
            "organization": "HeartOS",
            "user_type": "admin",
            "use_case": "system",
            "salt": salt,
            "password_hash": _hash_password(settings.default_password, salt),
            "active": True,
            "is_admin": True,
            "created_at": now,
            "last_login_at": 0,
        }
    )
    store.user_upsert(record)


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    _ensure_default_admin()
    user = store.user_get(user_id=user_id)
    if not user or not user.get("active", True):
        return None
    return _public_user(user)


def _touch_last_login(user: dict[str, Any]) -> None:
    target = store.user_get(user_id=str(user.get("id") or ""))
    if not target:
        return
    target["last_login_at"] = _now_ts()
    store.user_upsert(target)


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
    _ensure_default_admin()
    user = store.user_get(username=username, phone=phone)
    if not user or not user.get("active", True):
        return None
    if not _verify_plain_password(user, password):
        return None
    user["last_login_at"] = _now_ts()
    store.user_upsert(user)
    return _public_user(user)


def _is_upstream_auth_mode() -> bool:
    return (settings.auth_mode or "").strip().lower() == "upstream"


def _minimal_public_user(username: str, *, user_id: str = "", phone: str = "", display_name: str = "") -> dict[str, Any]:
    username_value = str(username or "").strip()
    phone_value = _normalize_phone(phone or username_value)
    display_value = str(display_name or username_value or phone_value or "用户").strip() or "用户"
    return {
        "id": str(user_id or ""),
        "username": username_value or phone_value,
        "phone": phone_value,
        "display_name": display_value,
        "name": display_value,
        "organization": "",
        "department": "",
        "title": "",
        "user_type": "",
        "use_case": "",
        "email": "",
        "is_admin": False,
        "active": True,
        "created_at": 0,
        "last_login_at": 0,
    }


def sync_local_user(user: dict[str, Any], password: str | None = None, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = dict(user or {})
    profile_data = dict(profile or {})
    username = str(raw.get("username") or raw.get("phone") or profile_data.get("phone") or "").strip()
    phone = _normalize_phone(str(raw.get("phone") or profile_data.get("phone") or username))
    if not username and phone:
        username = phone
    if not username:
        raise ValueError("缺少用户名")

    upstream_id = str(raw.get("id") or "").strip()
    _ensure_default_admin()
    target = store.user_get(user_id=upstream_id) if upstream_id else None
    if not target:
        target = store.user_get(phone=phone, username=username)

    now = _now_ts()
    if not target:
        target = _normalize_user_record(
            {
                "id": upstream_id or f"u_{secrets.token_hex(6)}",
                "username": username,
                "phone": phone,
                "phone_verified": bool(phone),
                "phone_verified_at": now if phone else 0,
                "display_name": str(raw.get("display_name") or profile_data.get("display_name") or profile_data.get("name") or username).strip() or username,
                "name": str(raw.get("name") or profile_data.get("name") or raw.get("display_name") or username).strip() or username,
                "organization": str(profile_data.get("organization") or raw.get("organization") or "").strip(),
                "department": str(profile_data.get("department") or raw.get("department") or "").strip(),
                "title": str(profile_data.get("title") or raw.get("title") or "").strip(),
                "user_type": str(profile_data.get("user_type") or raw.get("user_type") or "").strip(),
                "use_case": str(profile_data.get("use_case") or raw.get("use_case") or "").strip(),
                "email": str(profile_data.get("email") or raw.get("email") or "").strip(),
                "active": True,
                "created_at": now,
                "last_login_at": now,
            }
        )
    else:
        if upstream_id and str(target.get("id") or "") != upstream_id:
            # 主键变更：删掉旧行，避免残留两条同一用户
            store.user_delete(str(target.get("id") or ""))
            target["id"] = upstream_id
        target["username"] = username
        if phone:
            target["phone"] = phone
            target["phone_verified"] = True
            target["phone_verified_at"] = int(target.get("phone_verified_at") or now)
        target["active"] = True
        target["last_login_at"] = now

    for source in (raw, profile_data):
        if not isinstance(source, dict):
            continue
        for key in ("display_name", "name", "organization", "department", "title", "user_type", "use_case", "email"):
            value = source.get(key)
            if value is None:
                continue
            value_text = str(value).strip()
            if value_text:
                target[key] = value_text

    target["display_name"] = str(target.get("display_name") or target.get("name") or phone or username).strip() or username
    target["name"] = str(target.get("name") or target.get("display_name") or phone or username).strip() or username
    if password:
        salt = str(target.get("salt") or secrets.token_hex(8))
        target["salt"] = salt
        target["password_hash"] = _hash_password(password, salt)
    store.user_upsert(target)
    return _public_user(target)



def _validate_password(password: str, username: str = "") -> None:
    if not _is_client_password_digest(password) and not _is_client_password_heartos(password):
        plain_password = _strip_password_suffix(password)
        if len(plain_password or "") < 6:
            raise ValueError("密码至少 6 位")
        if not any(c.isalpha() for c in plain_password) or not any(c.isdigit() for c in plain_password):
            raise ValueError("密码需同时包含字母和数字")


def _issue_verification_code(phone: str, purpose: str) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    purpose_key = str(purpose or "").strip() or "register"
    now = _now_ts()
    store.codes_cleanup()
    latest = store.code_latest(phone_norm, purpose_key)
    if latest and now - int(latest.get("created_at") or 0) < SEND_INTERVAL_SECONDS:
        raise ValueError(f"验证码发送过于频繁，请在 {SEND_INTERVAL_SECONDS} 秒后再试")
    if store.code_count_since(phone_norm, purpose_key, now - 3600) >= SEND_LIMIT_PER_HOUR:
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
    store.code_insert(entry)
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
    _ensure_default_admin()
    existing_user = store.user_get(phone=phone_norm)
    if purpose_key == "register" and existing_user and existing_user.get("active", True):
        raise ValueError("该手机号已注册，请直接登录或找回密码")
    if purpose_key == "reset_password" and (not existing_user or not existing_user.get("active", True)) and not _is_upstream_auth_mode():
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
    store.codes_cleanup()
    target = store.code_latest(phone_norm, str(purpose or ""))
    if not target or bool(target.get("used")):
        raise ValueError("验证码不存在或已失效")
    now = _now_ts()
    if int(target.get("expires_at") or 0) < now:
        raise ValueError("验证码已过期，请重新获取")
    target["attempts"] = int(target.get("attempts") or 0) + 1
    if target["attempts"] > VERIFY_ATTEMPT_LIMIT:
        target["used"] = True
        store.code_update(target)
        raise ValueError("验证码输入次数过多，请重新获取")
    if _sha256_hex(code_text) != str(target.get("code_hash") or ""):
        store.code_update(target)
        raise ValueError("验证码错误")
    if consume:
        target["used"] = True
    store.code_update(target)


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

    _ensure_default_admin()
    if store.user_get(phone=phone_norm):
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
    store.user_upsert(user)
    return _public_user(user)


def send_password_reset_code(phone: str) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    _ensure_default_admin()
    user = store.user_get(phone=phone_norm)
    if (not user or not user.get("active", True)) and not _is_upstream_auth_mode():
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
    _ensure_default_admin()
    user = store.user_get(phone=phone_norm)
    if not verify_verification_ticket(str(verification_token or ""), phone_norm, "reset_password"):
        _consume_verification_code(phone_norm, "reset_password", code)
    if _is_upstream_auth_mode():
        upstream_reset_password(phone_norm, new_password)
        seed = _public_user(user) if user and user.get("active", True) else _minimal_public_user(phone_norm, phone=phone_norm)
        return sync_local_user(seed, new_password)
    if not user or not user.get("active", True):
        raise ValueError("账号不存在")
    salt = str(user.get("salt") or secrets.token_hex(8))
    user["salt"] = salt
    user["password_hash"] = _hash_password(new_password, salt)
    store.user_upsert(user)
    return _public_user(user)


def verify_password_reset_code(phone: str, code: str) -> dict[str, Any]:
    phone_norm = _validate_phone(phone)
    _ensure_default_admin()
    user = store.user_get(phone=phone_norm)
    if not user or not user.get("active", True):
        if not _is_upstream_auth_mode():
            raise ValueError("账号不存在")
        verify_verification_code(phone_norm, "reset_password", code)
        return _minimal_public_user(phone_norm, phone=phone_norm)
    verify_verification_code(phone_norm, "reset_password", code)
    return _public_user(user)


def verify_registration_code(phone: str, code: str) -> None:
    phone_norm = _validate_phone(phone)
    _ensure_default_admin()
    if store.user_get(phone=phone_norm):
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


def change_password(user_id: str, old_password: str, new_password: str, *, username: str = "", phone: str = "") -> dict[str, Any]:
    login_name = _normalize_phone(phone) or str(username or "").strip()
    _validate_password(new_password, login_name)
    if _is_upstream_auth_mode():
        if not login_name:
            local_user = store.user_get(user_id=user_id)
            login_name = _normalize_phone(str(local_user.get("phone") or "")) if local_user else ""
            if not login_name and local_user:
                login_name = str(local_user.get("username") or "").strip()
        if not login_name:
            raise ValueError("账号不存在")
        upstream_reset_password(login_name, new_password, old_password=old_password)
        seed = get_user_by_id(user_id) or _minimal_public_user(login_name, user_id=user_id, phone=phone or login_name, display_name=username or login_name)
        return sync_local_user(seed, new_password)

    _ensure_default_admin()
    user = store.user_get(user_id=user_id)
    if not user or not user.get("active", True):
        raise ValueError("账号不存在")
    if not _verify_plain_password(user, old_password):
        raise ValueError("原密码错误")
    _validate_password(new_password, str(user.get("username") or user.get("phone") or ""))
    user["password_hash"] = _hash_password(new_password, str(user.get("salt") or ""))
    store.user_upsert(user)
    return _public_user(user)


def update_profile(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    user = store.user_get(user_id=user_id)
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
    store.user_upsert(user)
    return _public_user(user)


def list_users_for_admin() -> list[dict[str, Any]]:
    _ensure_default_admin()
    items = []
    for item in store.user_all():
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


def _normalize_register_error_message(message: str, account: str) -> str:
    text = str(message or "").strip()
    lowered = text.lower()
    duplicate_markers = (
        "重复",
        "已注册",
        "已存在",
        "already exists",
        "already registered",
        "duplicate",
        "exists",
        "taken",
    )
    if any(marker in lowered for marker in duplicate_markers):
        normalized_account = _normalize_phone(account)
        if normalized_account and normalized_account == str(account or "").strip():
            return "注册失败，该手机号已注册"
        return "注册失败，账号已存在"
    return text or "注册失败，请稍后重试"


def _is_duplicate_register_message(message: str, account: str) -> bool:
    normalized = _normalize_register_error_message(message, account)
    return normalized in {"注册失败，该手机号已注册", "注册失败，账号已存在"}


def _normalize_upstream_user(raw: Any, fallback_username: str) -> tuple[dict[str, Any], str | None]:
    """从外部接口的响应里抽取出 (user_dict, upstream_token)。"""
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


def _is_upstream_success_payload(data: Any) -> bool:
    if data is None:
        return True
    if isinstance(data, dict) and "code" in data:
        try:
            return int(data.get("code") or 0) == 200
        except Exception:
            return False
    return True
def upstream_register(username: str, password: str, display_name: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"username": username, "password": password}
    if display_name:
        body["display_name"] = display_name

    try:
        status, data, text = _post_upstream(settings.auth_upstream_register_path, body)
    except UpstreamAuthError as e:
        raise UpstreamAuthError(e.status_code, _normalize_register_error_message(e.message, username)) from e

    if status >= 400 or not _has_valid_user_payload(data):
        message = _finalize_upstream_error_message(data, text, "注册失败，请稍后重试")
        status_code = 409 if _is_duplicate_register_message(message, username) else (status or 500)
        raise UpstreamAuthError(status_code, _normalize_register_error_message(message, username))

    user, _upstream_token = _normalize_upstream_user(data, fallback_username=username)
    if not user.get("display_name") and display_name:
        user["display_name"] = display_name
    return user


def upstream_reset_password(username: str, new_password: str, old_password: str | None = None) -> None:
    body: dict[str, Any] = {"username": username, "newPassword": new_password}
    if old_password:
        body["oldPassword"] = old_password
    try:
        status, data, text = _post_upstream(settings.auth_upstream_reset_password_path, body)
    except UpstreamAuthError as e:
        raise UpstreamAuthError(e.status_code, e.message) from e

    if status >= 400 or not _is_upstream_success_payload(data):
        message = _finalize_upstream_error_message(data, text, "密码重置失败，请稍后重试")
        raise UpstreamAuthError(400 if status < 500 else status, message)
