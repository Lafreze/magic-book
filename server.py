#!/usr/bin/env python3
"""遗失的日记 —— 服务器（零依赖，Python 标准库）

本地运行:  python3 server.py [端口]        默认 8765
Railway:   自动读取 PORT 环境变量

环境变量（Railway 的 Variables 里配置）:
  OPENAI_API_KEY    必填，OpenAI Key（只存在服务端，绝不下发前端）
  ADMIN_USER        管理员初始账户名（默认 admin）
  ADMIN_PASSWORD    管理员初始密码（默认 admin，生产环境务必设置）
  OPENAI_MODEL      可选，默认 gpt-4o-mini
  OPENAI_BASE_URL   可选，默认 https://api.openai.com/v1，中转服务时修改
  DATA_DIR          可选，config.json / data.json 的存放目录。
                    Railway 文件系统是临时的，想让统计和设定在重新部署后
                    保留，请挂一个 Volume（如 /data）并设 DATA_DIR=/data

环境变量优先级高于 config.json / 管理界面里的同名设置。
"""

import datetime
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or BASE
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DATA_PATH = os.path.join(DATA_DIR, "data.json")
LOCK = threading.Lock()

SESSIONS = {}                 # token -> 过期时间戳（进程内，重启即失效）
SESSION_TTL = 24 * 3600

DEFAULT_CONFIG = {
    "owner_name": "一位无名的书写者",
    "owner_background": (
        "这本日记被人遗落在旧书店最深处的架子上，封皮上的名字早已磨去。"
        "没有人知道它的主人是谁，只知道里面的墨水从未干涸。"
    ),
    "admin_user": "admin",
    "admin_password": "admin",
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
}

DEFAULT_DATA = {"total": 0, "days": {}, "users": {}}

READ_PROMPT = (
    "这张图片是羊皮纸上的手写文字。请原样转录出其中的文字，"
    "只输出转录内容本身，不要任何解释、引号或标点补全。"
    "如果完全无法辨认，输出空字符串。"
)


# ── 存取 ─────────────────────────────────────────────

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(default)
        merged.update(data)
        return merged
    except Exception:
        return dict(default)


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_config():
    with LOCK:
        return load_json(CONFIG_PATH, DEFAULT_CONFIG)


def set_config(cfg):
    with LOCK:
        save_json(CONFIG_PATH, cfg)


def bump_visit():
    with LOCK:
        data = load_json(DATA_PATH, DEFAULT_DATA)
        today = datetime.date.today().isoformat()
        data["total"] = int(data.get("total", 0)) + 1
        days = data.setdefault("days", {})
        days[today] = int(days.get(today, 0)) + 1
        for k in sorted(days)[:-60]:  # 只保留最近 60 天
            del days[k]
        save_json(DATA_PATH, data)


def record_usage(cid, kind, usage):
    """按用户记录一次 AI 调用的 token 消耗。kind: chats | reads"""
    with LOCK:
        data = load_json(DATA_PATH, DEFAULT_DATA)
        users = data.setdefault("users", {})
        u = users.setdefault(cid, {
            "requests": 0, "chats": 0, "reads": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0,
            "last": "",
        })
        u["requests"] = u.get("requests", 0) + 1
        u[kind] = u.get(kind, 0) + 1
        u["prompt_tokens"] = u.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
        u["completion_tokens"] = u.get("completion_tokens", 0) + int(usage.get("completion_tokens") or 0)
        u["tokens"] = u.get("tokens", 0) + int(usage.get("total_tokens") or 0)
        u["last"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        save_json(DATA_PATH, data)


# ── 配置读取（环境变量优先） ──────────────────────────

def api_key(cfg):
    return os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key") or ""


def eff_base_url(cfg):
    return os.environ.get("OPENAI_BASE_URL") or cfg.get("openai_base_url") or "https://api.openai.com/v1"


def eff_model(cfg):
    return os.environ.get("OPENAI_MODEL") or cfg.get("model") or "gpt-4o-mini"


def admin_credentials(cfg):
    user = os.environ.get("ADMIN_USER") or cfg.get("admin_user") or "admin"
    pwd = os.environ.get("ADMIN_PASSWORD") or cfg.get("admin_password") or "admin"
    return user, pwd


# ── 会话 ─────────────────────────────────────────────

def new_session():
    token = secrets.token_hex(24)
    now = time.time()
    with LOCK:
        for t in [t for t, exp in SESSIONS.items() if exp < now]:
            del SESSIONS[t]
        SESSIONS[token] = now + SESSION_TTL
    return token


def valid_session(token):
    if not token:
        return False
    with LOCK:
        exp = SESSIONS.get(token)
        if not exp:
            return False
        if time.time() > exp:
            del SESSIONS[token]
            return False
        return True


# ── OpenAI ───────────────────────────────────────────

def openai_chat(cfg, messages, max_tokens=300):
    """返回 (回复文本, usage 字典)"""
    key = api_key(cfg)
    if not key:
        raise PermissionError("no_key")
    url = eff_base_url(cfg).rstrip("/") + "/chat/completions"
    payload = {
        "model": eff_model(cfg),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.9,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode())
    text = (data["choices"][0]["message"]["content"] or "").strip()
    usage = data.get("usage") or {}
    return text, usage


def persona_prompt(cfg):
    owner = cfg.get("owner_name") or "一位无名的书写者"
    bg = (cfg.get("owner_background") or "").strip()
    return (
        f"你是一本被遗忘多年的日记，属于「{owner}」。{bg}\n"
        "有人正用墨水在你的书页上写字，你以日记主人的口吻亲笔回信。\n"
        "要求：使用第一人称；语气克制、神秘、有旧信件的质感；"
        "绝不提及自己是AI、程序或模型；回信不超过60个字；"
        "可以用换行分段；只输出回信正文，不要任何解释。"
    )


# ── HTTP ─────────────────────────────────────────────

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/admin": ("admin.html", "text/html; charset=utf-8"),
    "/riddle-diary.html": ("riddle-diary.html", "text/html; charset=utf-8"),
    "/magic-book.html": ("magic-book.html", "text/html; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 20 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def _authed(self):
        return valid_session(self.headers.get("X-Admin-Token", ""))

    def _client_id(self):
        cid = self.headers.get("X-Client-Id", "")
        cid = "".join(c for c in cid if c.isalnum() or c == "-")[:64]
        if cid:
            return cid
        fwd = (self.headers.get("X-Forwarded-For", "").split(",")[0]).strip()
        return fwd or self.client_address[0]

    # ── GET ──
    def do_GET(self):
        path = self.path.split("?")[0]

        if path in STATIC:
            name, ctype = STATIC[path]
            full = os.path.join(BASE, name)
            if os.path.isfile(full):
                with open(full, "rb") as f:
                    return self._send(200, f.read(), ctype)
            return self._send(404, {"error": "not_found"})

        if path == "/api/status":
            return self._send(200, {"ai": bool(api_key(get_config()))})

        if path == "/api/admin/stats":
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            with LOCK:
                data = load_json(DATA_PATH, DEFAULT_DATA)
            today = datetime.date.today().isoformat()
            days = data.get("days", {})
            users = data.get("users", {})
            rows = [{"id": k, **v} for k, v in users.items()]
            rows.sort(key=lambda r: -r.get("tokens", 0))
            return self._send(200, {
                "total": data.get("total", 0),
                "today": days.get(today, 0),
                "days": [{"date": d, "count": days[d]} for d in sorted(days)[-14:]],
                "tokens_total": sum(u.get("tokens", 0) for u in users.values()),
                "users_count": len(users),
                "users": rows[:200],
            })

        if path == "/api/admin/config":
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            cfg = get_config()
            key = cfg.get("openai_api_key") or ""
            return self._send(200, {
                "owner_name": cfg.get("owner_name"),
                "owner_background": cfg.get("owner_background"),
                "model": eff_model(cfg),
                "openai_base_url": eff_base_url(cfg),
                "api_key_masked": ("…" + key[-4:]) if key else "",
                "env_key": bool(os.environ.get("OPENAI_API_KEY")),
                "env_model": bool(os.environ.get("OPENAI_MODEL")),
                "env_base": bool(os.environ.get("OPENAI_BASE_URL")),
                "env_admin": bool(os.environ.get("ADMIN_PASSWORD")),
            })

        return self._send(404, {"error": "not_found"})

    # ── POST ──
    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/visit":
                bump_visit()
                return self._send(200, {"ok": True})

            if path == "/api/chat":
                text = (self._json_body().get("text") or "").strip()[:200]
                if not text:
                    return self._send(400, {"error": "empty"})
                cfg = get_config()
                reply, usage = openai_chat(cfg, [
                    {"role": "system", "content": persona_prompt(cfg)},
                    {"role": "user", "content": text},
                ])
                record_usage(self._client_id(), "chats", usage)
                return self._send(200, {"reply": reply})

            if path == "/api/read":
                image = self._json_body().get("image") or ""
                if not image.startswith("data:image/"):
                    return self._send(400, {"error": "bad_image"})
                cfg = get_config()
                text, usage = openai_chat(cfg, [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": READ_PROMPT},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }], max_tokens=100)
                record_usage(self._client_id(), "reads", usage)
                return self._send(200, {"text": text})

            if path == "/api/admin/login":
                body = self._json_body()
                user, pwd = admin_credentials(get_config())
                given_user = str(body.get("user") or "")
                given_pwd = str(body.get("password") or "")
                if secrets.compare_digest(given_user, user) and secrets.compare_digest(given_pwd, pwd):
                    return self._send(200, {"token": new_session()})
                time.sleep(0.8)  # 轻微延迟，增加爆破成本
                return self._send(401, {"error": "bad_credentials"})

            if path == "/api/admin/config":
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                body = self._json_body()
                cfg = get_config()
                for k in ("owner_name", "owner_background", "model", "openai_base_url"):
                    if k in body and isinstance(body[k], str):
                        cfg[k] = body[k].strip()
                if body.get("openai_api_key"):
                    cfg["openai_api_key"] = str(body["openai_api_key"]).strip()
                if body.get("admin_user"):
                    cfg["admin_user"] = str(body["admin_user"]).strip()
                if body.get("admin_password"):
                    cfg["admin_password"] = str(body["admin_password"]).strip()
                set_config(cfg)
                return self._send(200, {"ok": True})

            return self._send(404, {"error": "not_found"})

        except PermissionError:
            return self._send(503, {"error": "no_key", "message": "未配置 API Key"})
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            return self._send(502, {
                "error": "upstream",
                "message": f"AI 接口返回 {e.code}",
                "detail": detail,
            })
        except Exception as e:
            return self._send(500, {"error": "server", "message": str(e)[:200]})


def main():
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    cfg = get_config()
    if not os.path.isfile(CONFIG_PATH):
        set_config(cfg)
    user, _ = admin_credentials(cfg)
    print(f"遗失的日记   http://localhost:{port}/", flush=True)
    print(f"管理入口     /admin  账户 {user}"
          f"{'（来自环境变量）' if os.environ.get('ADMIN_USER') else '（默认，可用 ADMIN_USER/ADMIN_PASSWORD 覆盖）'}", flush=True)
    print(f"AI 状态      {'已配置' if api_key(cfg) else '未配置：设置 OPENAI_API_KEY 或在管理界面填写'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
