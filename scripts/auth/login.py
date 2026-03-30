#!/usr/bin/env python3
"""
统一鉴权解析模块。

对外只做三件事：
- resolve_app_key(context, session_id)
- ensure_token(context, session_id)
- build_auth_headers(auth_mode, context, session_id)

约定：
- login.py 不自己读取环境变量
- 上层先确定鉴权方式，再把可用参数整理到 context
- 支持通过 session_id 缓存 appKey/token 到 cms-auth/auth.json
- 所有 API 调用记录到 cms-auth/logs/ 目录
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
import warnings

# 禁用 InsecureRequestWarning (因为 verify=False)
warnings.filterwarnings("ignore", category=requests.packages.urllib3.exceptions.InsecureRequestWarning)
import urllib.parse

TOKEN_AUTH_URL = "https://sg-cwork-web.mediportal.com.cn/user/login/appkey"
APP_CODE = "cms_gpt"

APPKEY_API_URL = "https://sg-al-cwork-web.mediportal.com.cn/user/appkey/getAppKeyByDingUserId/nologin"
APPKEY_REQUEST_KEY = "MTrBkZsNFFghxH5SmKxWWc93KJqe0"


DING_CORP_ID_RULES = (
    ("xgjk", "ding0b8223c4cde4879dee0f45d8e4f7c288"),
    ("kangzhe", "dingf57b758b536306eea1320dcb25e91351"),
    ("demei", "ding452d1e907f3ae594f2c783f7214b6d69"),
    ("xgjkrtest", "dingf019ee9a572ee7daa39a90f97fcb1e09"),
)

# ──────────────────────────────── 路径常量 ────────────────────────────────
# login.py 位于 cms-auth-skills/scripts/auth/login.py
_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_DIR = _SCRIPT_DIR.parent.parent


def _move_missing_items(source_dir: Path, target_dir: Path) -> None:
    """把旧目录中的缺失文件迁移到新目录，不覆盖新目录已有内容。"""
    target_dir.mkdir(parents=True, exist_ok=True)

    for child in source_dir.iterdir():
        target_child = target_dir / child.name
        if target_child.exists():
            if child.is_dir():
                _move_missing_items(child, target_child)
            continue
        child.rename(target_child)

    try:
        source_dir.rmdir()
    except OSError:
        pass


def _prepare_auth_dir(auth_dir: Path, skills_dir: Path | None = None) -> Path:
    """
    创建目标目录，并在检测到错误旧路径时自动迁移已有缓存/日志。
    """
    misplaced_auth_dir = skills_dir / "cms-auth" if skills_dir else None

    if misplaced_auth_dir and misplaced_auth_dir.exists() and misplaced_auth_dir != auth_dir:
        auth_dir.parent.mkdir(parents=True, exist_ok=True)
        if not auth_dir.exists():
            misplaced_auth_dir.rename(auth_dir)
            return auth_dir
        _move_missing_items(misplaced_auth_dir, auth_dir)

    auth_dir.mkdir(parents=True, exist_ok=True)
    return auth_dir


def _find_auth_dir(script_dir: Path | None = None, skill_dir: Path | None = None) -> Path:
    """
    通过相对路径解析 cms-auth 目录，不写死绝对路径。

    优先支持当前推荐结构：
        <workspace>/
        ├── skills/
        │   └── cms-auth-skills/scripts/auth/login.py
        └── cms-auth/

    兼容旧结构：
        <workspace>/
        └── cms-auth/
            ├── auth.json
            ├── logs/
            └── skills/
                └── cms-auth-skills/scripts/auth/login.py

    解析逻辑：
    1. 从 skill 目录向上查找祖先中的 skills/
    2. 如果 skills 的父目录本身就是 cms-auth，则直接复用该目录
    3. 否则固定把 cms-auth 放到 skills 的同级（即 skills 父目录下）
    4. 如果整条路径中没有 skills/，才退回到 skill 目录的父级创建 cms-auth/
    """
    script_dir = script_dir or Path(__file__).resolve().parent
    skill_dir = skill_dir or script_dir.parent.parent

    for current in (skill_dir, *skill_dir.parents):
        if current.name != "skills":
            continue

        auth_dir = current.parent if current.parent.name == "cms-auth" else current.parent / "cms-auth"
        return _prepare_auth_dir(auth_dir, skills_dir=current)

    fallback = skill_dir.parent / "cms-auth"
    return _prepare_auth_dir(fallback)


_AUTH_DIR = _find_auth_dir()
_AUTH_JSON = _AUTH_DIR / "auth.json"
_LOGS_DIR = _AUTH_DIR / "logs"
_CLI_LOGGING_ENABLED = False

# 日志保留天数
_LOG_RETENTION_DAYS = 30

# 时区（东八区）
_TZ_CN = timezone(timedelta(hours=8))


# ──────────────────────────────── 日志模块 ────────────────────────────────

def _mask_sensitive(value: str | None) -> str:
    """对 token/appKey 做脱敏处理，只保留前 6 位 + ***"""
    if not value or len(value) <= 6:
        return "***"
    return value[:6] + "***"


def _ensure_logs_dir():
    """确保日志目录存在"""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)


def _log_file_path() -> Path:
    """获取当天日志文件路径"""
    today = datetime.now(_TZ_CN).strftime("%Y-%m-%d")
    return _LOGS_DIR / f"{today}.log"


def _write_log(level: str, message: str):
    """写入日志到文件"""
    try:
        _ensure_logs_dir()
        now = datetime.now(_TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{now}] [{level}] {message}\n"
        with open(_log_file_path(), "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception:
        # 日志写入失败不影响主流程
        pass


def _iter_log_lines(message: str) -> list[str]:
    lines = str(message).splitlines()
    return lines or [str(message)]


def _stderr_log(level: str, message: str, quiet: bool = False):
    if quiet or not _CLI_LOGGING_ENABLED:
        return
    now = datetime.now(_TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
    for line in _iter_log_lines(message):
        print(f"[{now}] [{level}] {line}", file=sys.stderr, flush=True)


def _log_step(message: str, quiet: bool = False, level: str = "INFO", persist: bool = True):
    if persist:
        _write_log(level, message)
    _stderr_log(level, message, quiet=quiet)


def _cleanup_old_logs():
    """清理超过 30 天的日志文件"""
    try:
        if not _LOGS_DIR.exists():
            return
        cutoff = datetime.now(_TZ_CN) - timedelta(days=_LOG_RETENTION_DAYS)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        for log_file in _LOGS_DIR.glob("*.log"):
            # 文件名格式: YYYY-MM-DD.log
            date_part = log_file.stem
            if len(date_part) == 10 and date_part < cutoff_str:
                log_file.unlink(missing_ok=True)
    except Exception:
        pass


# ──────────────────────────────── 缓存模块 ────────────────────────────────

def _load_auth_cache() -> dict:
    """从 auth.json 加载缓存"""
    try:
        if _AUTH_JSON.exists():
            with open(_AUTH_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        _write_log("WARN", f"读取缓存文件失败: {_AUTH_JSON}")
    return {"sessions": {}}


def _save_auth_cache(cache: dict):
    """写入缓存到 auth.json"""
    try:
        _AUTH_DIR.mkdir(parents=True, exist_ok=True)
        with open(_AUTH_JSON, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        _write_log("ERROR", f"写入缓存文件失败: {e}")


def _get_cached_value(session_id: str | None, key: str) -> str | None:
    """从缓存中获取指定 session 的值"""
    if not session_id:
        return None
    cache = _load_auth_cache()
    sessions = cache.get("sessions", {})
    session_data = sessions.get(session_id, {})
    value = session_data.get(key)
    if value and isinstance(value, str) and value.strip():
        _write_log("INFO", f"从缓存命中 {key} (session: {session_id[:8]}***)")
        return value.strip()
    return None


def _update_cache(session_id: str | None, key: str, value: str):
    """更新缓存中的值"""
    if not session_id or not value:
        return
    cache = _load_auth_cache()
    sessions = cache.setdefault("sessions", {})
    session_data = sessions.setdefault(session_id, {})
    session_data[key] = value
    session_data["updated_at"] = datetime.now(_TZ_CN).isoformat()
    _save_auth_cache(cache)
    _write_log("INFO", f"缓存已更新 {key} (session: {session_id[:8]}***)")


# ──────────────────────────────── 基础工具 ────────────────────────────────

def _ssl_context():
    # 使用 requests 时不再直接使用 ssl context，但保留此函数名
    # 以防止其他地方有引用（虽然 grep 显示没有，但为了稳妥保留声明）
    return None


def _log(message: str, quiet: bool, level: str = "INFO"):
    _stderr_log(level, message, quiet=quiet)


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _normalize_log_key(key: str | None) -> str:
    if not key:
        return ""
    return key.strip().lower().replace("_", "-")


def _is_sensitive_log_key(key: str | None) -> bool:
    return _normalize_log_key(key) in {
        "appkey",
        "app-key",
        "access-token",
        "token",
        "authorization",
        "requestkey",
        "request-key",
    }


def _mask_log_value(key: str | None, value: Any) -> Any:
    stringified = _stringify(value)
    if _is_sensitive_log_key(key) and stringified is not None:
        return _mask_sensitive(stringified)
    return value


def _sanitize_for_log(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize_for_log(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_log(item, key) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_for_log(item, key) for item in value]
    return _mask_log_value(key, value)


def _json_for_log(value: Any) -> str:
    return json.dumps(_sanitize_for_log(value), ensure_ascii=False)


def _sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url

    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    sanitized_pairs = [
        (key, _mask_sensitive(value) if _is_sensitive_log_key(key) else value)
        for key, value in query_pairs
    ]
    sanitized_query = urllib.parse.urlencode(sanitized_pairs, doseq=True)
    return urllib.parse.urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        sanitized_query,
        parsed.fragment,
    ))


def _masked_label(value: Any) -> str:
    stringified = _stringify(value)
    return _mask_sensitive(stringified) if stringified else "N/A"


def _parse_context(context: Any) -> dict[str, Any]:
    if isinstance(context, dict):
        return context
    if not isinstance(context, str):
        return {}

    text = context.strip()
    if not text:
        return {}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def _context_summary(context: Any) -> str:
    payload = _parse_context(context)
    return _json_for_log(payload) if payload else "{}"


def _context_get(context: Any, *keys: str) -> str | None:
    payload = _parse_context(context)
    for key in keys:
        value = _stringify(payload.get(key))
        if value:
            return value
    return None


def extract_appkey_from_context(context: Any) -> str | None:
    return _context_get(context, "appKey", "app_key")


def extract_access_token_from_context(context: Any) -> str | None:
    return _context_get(context, "access-token", "access_token", "token")


def extract_account_id_from_context(context: Any) -> str | None:
    return _context_get(context, "account_id")


def extract_send_id_from_context(context: Any) -> str | None:
    return _context_get(context, "send_id")


def _raise_need_user_app_key() -> None:
    raise RuntimeError("未能自动获取 appKey，请向用户索要工作协同 key。")


def _raise_need_user_token() -> None:
    raise RuntimeError("未能自动获取 access-token，请先向用户索要工作协同 key。")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    request_headers = dict(headers or {})

    # 记录完整请求日志（输入）
    log_headers = _json_for_log(request_headers)
    log_body = _json_for_log(body) if body is not None else "N/A"
    sanitized_url = _sanitize_url(url)
    request_log = (
        f">>> API 请求\n"
        f"    Method : {method}\n"
        f"    URL    : {sanitized_url}\n"
        f"    Headers: {log_headers}\n"
        f"    Body   : {log_body}"
    )
    _write_log("INFO", request_log)
    _stderr_log("INFO", request_log, quiet=quiet)

    try:
        response = requests.request(
            method,
            url,
            json=body,
            headers=request_headers,
            verify=False,
            allow_redirects=True,
            timeout=30,
        )
        status_code = response.status_code
        resp_headers = dict(response.headers)
        payload = response.json()
    except requests.exceptions.HTTPError as e:
        error_body = e.response.text if e.response else str(e)
        error_log = (
            f"<<< API 响应失败\n"
            f"    Method : {method}\n"
            f"    URL    : {sanitized_url}\n"
            f"    Status : {e.response.status_code if e.response else 'N/A'}\n"
            f"    Body   : {error_body}"
        )
        _write_log("ERROR", error_log)
        _stderr_log("ERROR", error_log, quiet=quiet)
        raise RuntimeError(f"请求失败 (HTTP {e.response.status_code if e.response else 'N/A'}): {error_body}") from e
    except requests.exceptions.RequestException as e:
        error_log = (
            f"<<< API 请求失败\n"
            f"    Method : {method}\n"
            f"    URL    : {sanitized_url}\n"
            f"    Error  : {str(e)}"
        )
        _write_log("ERROR", error_log)
        _stderr_log("ERROR", error_log, quiet=quiet)
        raise RuntimeError(f"请求失败: {str(e)}") from e
    except json.JSONDecodeError as e:
        error_log = (
            f"<<< API 响应 JSON 解析失败\n"
            f"    Method : {method}\n"
            f"    URL    : {sanitized_url}\n"
            f"    Raw    : {response.text[:2000] if 'response' in locals() else 'N/A'}"
        )
        _write_log("ERROR", error_log)
        _stderr_log("ERROR", error_log, quiet=quiet)
        raise RuntimeError("请求失败：接口返回了无法解析的 JSON") from e

    if not isinstance(payload, dict):
        error_log = (
            f"<<< API 响应格式异常\n"
            f"    Method : {method}\n"
            f"    URL    : {sanitized_url}\n"
            f"    Raw    : {response.text[:2000]}"
        )
        _write_log("ERROR", error_log)
        _stderr_log("ERROR", error_log, quiet=quiet)
        raise RuntimeError("请求失败：接口返回格式异常")

    # 记录完整响应日志（输出）
    response_log = (
        f"<<< API 响应成功\n"
        f"    Method  : {method}\n"
        f"    URL     : {sanitized_url}\n"
        f"    Status  : {status_code}\n"
        f"    Headers : {_json_for_log(resp_headers)}\n"
        f"    Body    : {_json_for_log(payload)}"
    )
    _write_log("INFO", response_log)
    _stderr_log("INFO", response_log, quiet=quiet)

    return payload


def _resolve_ding_corp_id(account_id: str) -> str:
    normalized_account_id = account_id.strip().lower()
    if not normalized_account_id:
        raise RuntimeError("无法自动获取 appKey")

    account_prefix = normalized_account_id.split("_", 1)[0].split("-", 1)[0]
    for keyword, ding_corp_id in DING_CORP_ID_RULES:
        if keyword == account_prefix:
            return ding_corp_id

    raise RuntimeError("无法自动获取 appKey")


def _extract_app_key_from_payload(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if isinstance(data, dict):
        return (
            _stringify(data.get("appKey"))
            or _stringify(data.get("app_key"))
            or _stringify(data.get("appkey"))
        )

    return (
        _stringify(payload.get("appKey"))
        or _stringify(payload.get("app_key"))
        or _stringify(payload.get("appkey"))
    )


def _extract_token_from_payload(payload: dict[str, Any]) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    return (
        _stringify(data.get("xgToken"))
        or _stringify(data.get("token"))
        or _stringify(data.get("access-token"))
    )


def get_app_key_by_ding_user(account_id: str, send_id: str, quiet: bool = False) -> str:
    ding_corp_id = _resolve_ding_corp_id(account_id)
    ding_user_id = send_id.strip()
    if not ding_user_id:
        raise RuntimeError("无法自动获取 appKey")

    _log(
        "开始调用 appKey 自动获取接口\n"
        f"    URL        : {APPKEY_API_URL}\n"
        f"    account_id : {account_id}\n"
        f"    dingCorpId : {ding_corp_id}\n"
        f"    dingUserId : {_masked_label(ding_user_id)}",
        quiet,
    )

    try:
        data = _request_json(
            APPKEY_API_URL,
            method="POST",
            body={
                "requestKey": APPKEY_REQUEST_KEY,
                "dingCorpId": ding_corp_id,
                "dingUserId": ding_user_id,
            },
            quiet=quiet,
        )
    except RuntimeError as exc:
        raise RuntimeError(f"获取 appKey 失败: {exc}") from exc

    app_key = _extract_app_key_from_payload(data)
    if app_key:
        _log(f"appKey 自动获取成功: {_masked_label(app_key)}", quiet)
        return app_key

    message = (
        _stringify(data.get("resultMsg"))
        or _stringify(data.get("detailMsg"))
        or _stringify(data.get("message"))
        or "未知错误"
    )
    raise RuntimeError(f"获取 appKey 失败: {message}")


def _login_with_app_key(app_key: str, quiet: bool = False) -> dict[str, Any]:
    normalized_key = app_key.strip()
    if not normalized_key:
        raise RuntimeError("登录失败：appKey 不能为空")

    params = {"appCode": APP_CODE, "appKey": normalized_key}
    _log(
        "开始调用 access-token 接口\n"
        f"    URL    : {TOKEN_AUTH_URL}\n"
        f"    appKey : {_masked_label(normalized_key)}",
        quiet,
    )
    try:
        # 使用 requests 的 params 自动处理 query string
        full_url = f"{TOKEN_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return _request_json(full_url, quiet=quiet)
    except RuntimeError as exc:
        raise RuntimeError(f"登录失败: {exc}") from exc


def get_token(app_key: str, quiet: bool = False) -> str:
    data = _login_with_app_key(app_key, quiet=quiet)
    token = _extract_token_from_payload(data)
    if token:
        _log(f"access-token 获取成功: {_masked_label(token)}", quiet)
        return token

    message = (
        _stringify(data.get("resultMsg"))
        or _stringify(data.get("detailMsg"))
        or _stringify(data.get("message"))
        or "未知错误"
    )
    raise RuntimeError(f"登录失败: {message}")


def resolve_app_key(
    context: Any = None,
    quiet: bool = False,
    session_id: str | None = None,
    force_update: bool = False,
) -> str:
    _log_step(
        "开始解析 appKey\n"
        f"    session_id : {_masked_label(session_id)}\n"
        f"    force      : {force_update}\n"
        f"    context    : {_context_summary(context)}",
        quiet=quiet,
    )

    # 1. 优先从缓存读取（非强制刷新时）
    if not force_update:
        _log("检查 appKey 缓存", quiet)
        cached = _get_cached_value(session_id, "appKey")
        if cached:
            _log(f"已获取 appKey（缓存）: {_masked_label(cached)}", quiet)
            return cached
        _log("appKey 缓存未命中", quiet)
    else:
        _log("已启用强制刷新，跳过 appKey 缓存", quiet)

    # 2. 从 context 取
    app_key = extract_appkey_from_context(context)
    if app_key:
        _log(f"从 context 获取 appKey: {_masked_label(app_key)}", quiet)
        _update_cache(session_id, "appKey", app_key)
        return app_key
    _log("context 中未提供 appKey", quiet)

    # 3. 通过 send_id + account_id 自动获取
    account_id = extract_account_id_from_context(context)
    send_id = extract_send_id_from_context(context)
    if account_id and send_id:
        _log(
            "检测到 account_id + send_id，准备自动获取 appKey\n"
            f"    account_id : {account_id}\n"
            f"    send_id    : {_masked_label(send_id)}",
            quiet,
        )
        try:
            app_key = get_app_key_by_ding_user(account_id=account_id, send_id=send_id, quiet=quiet)
        except RuntimeError as exc:
            _log_step("自动获取 appKey 失败，准备回退到用户输入", quiet=quiet, level="WARN")
            _log(str(exc), quiet, level="WARN")
            _raise_need_user_app_key()
        _log(f"已获取 appKey: {_masked_label(app_key)}", quiet)
        _update_cache(session_id, "appKey", app_key)
        return app_key

    _log_step("缺少 account_id 或 send_id，无法自动获取 appKey", quiet=quiet, level="WARN")
    _raise_need_user_app_key()


def ensure_token(
    context: Any = None,
    quiet: bool = False,
    session_id: str | None = None,
    force_update: bool = False,
) -> str:
    _log_step(
        "开始解析 access-token\n"
        f"    session_id : {_masked_label(session_id)}\n"
        f"    force      : {force_update}\n"
        f"    context    : {_context_summary(context)}",
        quiet=quiet,
    )

    # 1. 优先从缓存读取（非强制刷新时）
    if not force_update:
        _log("检查 access-token 缓存", quiet)
        cached = _get_cached_value(session_id, "token")
        if cached:
            _log(f"已获取 access-token（缓存）: {_masked_label(cached)}", quiet)
            return cached
        _log("access-token 缓存未命中", quiet)
    else:
        _log("已启用强制刷新，跳过 access-token 缓存", quiet)

    # 2. 从 context 取
    token = extract_access_token_from_context(context)
    if token:
        _log(f"从 context 获取 access-token: {_masked_label(token)}", quiet)
        _update_cache(session_id, "token", token)
        return token
    _log("context 中未提供 access-token，开始解析 appKey", quiet)

    # 3. 通过 appKey 换 token
    try:
        app_key = resolve_app_key(
            context=context, quiet=True,
            session_id=session_id, force_update=force_update,
        )
    except RuntimeError as exc:
        _log_step("解析 appKey 失败，无法继续换取 access-token", quiet=quiet, level="WARN")
        _log(str(exc), quiet, level="WARN")
        _raise_need_user_token()

    _log(f"开始使用 appKey 换取 access-token: {_masked_label(app_key)}", quiet)
    token = get_token(app_key, quiet=quiet)
    _log(f"已获取 access-token: {_masked_label(token)}", quiet)
    _update_cache(session_id, "token", token)
    # 同时缓存 appKey
    _update_cache(session_id, "appKey", app_key)
    return token


def build_auth_headers(
    auth_mode: str,
    context: Any = None,
    session_id: str | None = None,
    force_update: bool = False,
) -> dict[str, str]:
    mode = (auth_mode or "none").strip().lower().replace("_", "-")
    headers: dict[str, str] = {}
    _write_log("INFO", f"开始构建鉴权 headers auth_mode={mode} session={_masked_label(session_id)}")

    if mode in ("none", "nologin", "no-auth"):
        return headers
    if mode in ("appkey", "app-key"):
        headers["appKey"] = resolve_app_key(
            context=context, quiet=True,
            session_id=session_id, force_update=force_update,
        )
        return headers
    if mode in ("access-token", "token"):
        headers["access-token"] = ensure_token(
            context=context, quiet=True,
            session_id=session_id, force_update=force_update,
        )
        return headers

    raise ValueError(f"不支持的 auth_mode: {auth_mode}")


def _merge_context_app_key(context: Any, app_key: str) -> dict[str, Any]:
    payload = _parse_context(context)
    payload["appKey"] = app_key
    return payload


def main():
    global _CLI_LOGGING_ENABLED
    _CLI_LOGGING_ENABLED = True

    # 启动时清理旧日志
    _cleanup_old_logs()

    parser = argparse.ArgumentParser(description="统一鉴权解析：appKey / access-token")
    parser.add_argument("--app-key", "-k", type=str, help="显式传入 CWork AppKey")
    parser.add_argument("--context-json", type=str, default="", help="显式传入上下文 JSON")
    parser.add_argument("--resolve-app-key", action="store_true", help="输出一个可用的 appKey")
    parser.add_argument("--ensure", action="store_true", help="输出一个可用的 access-token")
    parser.add_argument("--headers", action="store_true", help="按 --auth-mode 输出鉴权 headers（JSON）")
    parser.add_argument(
        "--auth-mode",
        type=str,
        default="access-token",
        help="header 模式：none / appKey / access-token（默认 access-token）",
    )
    parser.add_argument("--session-id", type=str, default=None, help="会话 ID，用于缓存索引")
    parser.add_argument("--update", action="store_true", help="强制刷新缓存（跳过已有缓存，重新获取）")
    args = parser.parse_args()

    context: Any = args.context_json
    if args.app_key:
        context = _merge_context_app_key(context, args.app_key.strip())

    session_id = args.session_id
    force_update = args.update

    execute_mode = (
        "headers" if args.headers else
        "ensure" if args.ensure else
        "resolve-app-key" if args.resolve_app_key else
        "login"
    )
    _log_step(
        "脚本启动\n"
        f"    mode      : {execute_mode}\n"
        f"    session   : {_masked_label(session_id)}\n"
        f"    update    : {force_update}\n"
        f"    auth_dir  : {_AUTH_DIR}\n"
        f"    auth_json : {_AUTH_JSON}\n"
        f"    log_file  : {_log_file_path()}\n"
        f"    context   : {_context_summary(context)}",
        quiet=False,
    )

    try:
        if args.headers:
            headers = build_auth_headers(
                args.auth_mode, context=context,
                session_id=session_id, force_update=force_update,
            )
            _log("鉴权 headers 已生成，准备输出到 stdout", quiet=False)
            print(json.dumps(headers, ensure_ascii=False, indent=2), flush=True)
            return

        if args.ensure:
            token = ensure_token(
                context=context, session_id=session_id,
                force_update=force_update,
            )
            _log("access-token 已生成，准备输出到 stdout", quiet=False)
            print(token, flush=True)
            return

        if args.resolve_app_key:
            app_key = resolve_app_key(
                context=context, session_id=session_id,
                force_update=force_update,
            )
            _log("appKey 已生成，准备输出到 stdout", quiet=False)
            print(app_key, flush=True)
            return

        app_key = args.app_key.strip() if args.app_key else resolve_app_key(
            context=context, session_id=session_id,
            force_update=force_update,
        )
        token = get_token(app_key, quiet=False)
        _update_cache(session_id, "token", token)
        _update_cache(session_id, "appKey", app_key)
        _log("默认登录流程完成，准备输出 access-token 到 stdout", quiet=False)
        print(token, flush=True)
    except (RuntimeError, ValueError) as e:
        _log_step("脚本执行失败", quiet=False, level="ERROR")
        _log_step(str(e), quiet=False, level="ERROR")
        print(f"错误: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
