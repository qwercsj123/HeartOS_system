from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from .config import settings


USERS_PATH = Path(settings.users_file).resolve()
USERS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode((s + pad).encode())


def _sha256_hex(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def _hash_password(password: str, salt: str) -> str:
    return _sha256_hex(f"{salt}:{password}")


def _load_users() -> dict[str, Any]:
    if not USERS_PATH.exists():
        _init_default_user()
    try:
        return json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        _init_default_user()
        return json.loads(USERS_PATH.read_text(encoding="utf-8"))


def _save_users(data: dict[str, Any]) -> None:
    USERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _init_default_user() -> None:
    salt = secrets.token_hex(8)
    data = {
        "users": [
            {
                "id": "u_admin",
                "username": settings.default_username,
                "display_name": "Administrator",
                "salt": salt,
                "password_hash": _hash_password(settings.default_password, salt),
                "active": True,
            }
        ]
    }
    _save_users(data)


def verify_user(username: str, password: str) -> dict[str, Any] | None:
    db = _load_users()
    for u in db.get("users", []):
        if not u.get("active", True):
            continue
        if u.get("username") != username:
            continue
        salt = str(u.get("salt", ""))
        if not salt:
            continue
        if hmac.compare_digest(_hash_password(password, salt), str(u.get("password_hash", ""))):
            return {"id": u.get("id"), "username": u.get("username"), "display_name": u.get("display_name") or u.get("username")}
    return None



def register_user(username: str, password: str, display_name: str | None = None) -> dict[str, Any]:
    username = (username or "").strip()
    if len(username) < 3:
        raise ValueError("用户名至少 3 位")
    if len(password or "") < 6:
        raise ValueError("密码至少 6 位")

    db = _load_users()
    users = db.setdefault("users", [])
    for u in users:
        if str(u.get("username", "")).lower() == username.lower():
            raise ValueError("用户名已存在")

    uid = "u_" + secrets.token_hex(6)
    salt = secrets.token_hex(8)
    user = {
        "id": uid,
        "username": username,
        "display_name": (display_name or username).strip() or username,
        "salt": salt,
        "password_hash": _hash_password(password, salt),
        "active": True,
    }
    users.append(user)
    _save_users(db)
    return {"id": user["id"], "username": user["username"], "display_name": user["display_name"]}

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
        if isinstance(data, dict):
            inner = _extract_error_message(data, "")
            if inner:
                return inner
    elif isinstance(payload, str) and payload.strip():
        return payload.strip()[:300]
    return fallback


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
        with httpx.Client(timeout=httpx.Timeout(settings.http_timeout)) as client:
            resp = client.post(url, json=body, headers={"Content-Type": "application/json"})
    except httpx.RequestError as e:
        raise UpstreamAuthError(502, f"无法连接外部账号服务: {e}") from e

    text = resp.text or ""
    try:
        data = resp.json()
    except Exception:
        data = None
    return resp.status_code, data, text


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
