#!/usr/bin/env python3
"""遗失的日记 —— 服务器

本地运行:  python3 server.py [端口]        默认 8765
Railway:   自动读取 PORT 环境变量

数据存储（访问统计、用户 token 消耗、管理界面配置）:
  - 设置了 DATABASE_URL  → 存 PostgreSQL（Railway 加 Postgres 插件即可，重新部署不丢数据）
  - 未设置              → 存本地 JSON 文件（config.json / data.json，零依赖）

环境变量（Railway 的 Variables 里配置）:
  OPENAI_API_KEY    必填，OpenAI Key（只存在服务端，绝不下发前端）
  ADMIN_USER        管理员初始账户名（默认 admin）
  ADMIN_PASSWORD    管理员初始密码（默认 admin，生产环境务必设置）
  DATABASE_URL      可选，PostgreSQL 连接串（Railway 引用 ${{Postgres.DATABASE_URL}}）
                    也兼容 POSTGRES_URL / POSTGRESQL_URL
  OPENAI_MODEL      可选，默认 gpt-4o-mini
  OPENAI_BASE_URL   可选，默认 https://api.openai.com/v1，中转服务时修改
  DATA_DIR          可选，仅 JSON 文件模式使用的数据目录

环境变量优先级高于管理界面 / 存储中的同名设置。
"""

import datetime
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or BASE
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
    "reply_effect": "stroke",     # stroke | fade | ink
    "reply_speed": "fast",        # normal | fast | very_fast
}

ALLOWED_REPLY_EFFECTS = {"stroke", "fade", "ink"}
ALLOWED_REPLY_SPEEDS = {"normal", "fast", "very_fast"}

EMPTY_USER = {
    "requests": 0, "chats": 0, "reads": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0, "last": "",
}

READ_PROMPT = """
你会看到一张羊皮纸图片。用户可能在上面手写了中文、英文、日文，也可能画了一幅图。
只输出一个 JSON 对象，不要 Markdown，不要解释：
{
  "type": "text" | "drawing" | "empty",
  "text": "如果有可辨认文字，原样转录；没有文字则为空字符串",
  "language": "zh" | "en" | "ja",
  "description": "如果是图画，用一句话描述图中内容；否则为空字符串"
}
规则：
- 文字必须保持原语言，不要翻译，不要补全。
- 识别到英文时 language=en；识别到日文时 language=ja；识别到中文时 language=zh。
- 如果既有文字又有图，type=text，并把图像内容放入 description。
- 如果没有文字但看得出图画内容，type=drawing。
- 如果无法判断，type=empty，language=zh。默认语言是中文。
""".strip()


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def clean_text(value, limit=500):
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # OCR often inserts spaces between CJK characters; keep English word spaces.
    text = re.sub(r"([\u3400-\u9fff\u3040-\u30ff])\s+(?=[\u3400-\u9fff\u3040-\u30ff])", r"\1", text)
    return text[:limit]


def detect_language(text, fallback="zh"):
    text = text or ""
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    has_latin = bool(re.search(r"[A-Za-z]", text))
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", text))
    if has_latin and not has_cjk:
        return "en"
    return fallback if fallback in {"zh", "en", "ja"} else "zh"


def normalize_language(value, text="", fallback="zh"):
    lang = str(value or "").strip().lower()
    if lang in {"zh", "zh-cn", "cn", "chinese", "中文", "汉语"}:
        return "zh"
    if lang in {"en", "eng", "english", "英语"}:
        return "en"
    if lang in {"ja", "jp", "jpn", "japanese", "日语", "日本語"}:
        return "ja"
    return detect_language(text, fallback)


def normalize_reply_effect(value):
    value = str(value or "").strip().lower()
    return value if value in ALLOWED_REPLY_EFFECTS else DEFAULT_CONFIG["reply_effect"]


def normalize_reply_speed(value):
    value = str(value or "").strip().lower()
    return value if value in ALLOWED_REPLY_SPEEDS else DEFAULT_CONFIG["reply_speed"]


def public_reply_config(cfg):
    return {
        "reply_effect": normalize_reply_effect(cfg.get("reply_effect")),
        "reply_speed": normalize_reply_speed(cfg.get("reply_speed")),
    }


def parse_read_result(raw):
    raw = (raw or "").strip()
    result = {
        "type": "empty",
        "text": "",
        "language": "zh",
        "description": "",
    }
    if not raw:
        return result

    payload = raw
    if payload.startswith("```"):
        payload = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", payload)
        if payload.endswith("```"):
            payload = payload[:-3].strip()

    start = payload.find("{")
    end = payload.rfind("}")
    if start != -1 and end != -1 and end > start:
        payload = payload[start:end + 1]

    try:
        data = json.loads(payload)
    except Exception:
        text = clean_text(raw, 400)
        result["text"] = text
        result["type"] = "text" if text else "empty"
        result["language"] = detect_language(text, "zh")
        return result

    text = clean_text(data.get("text"), 400)
    description = clean_text(
        data.get("description") or data.get("image_description") or data.get("drawing"),
        400,
    )
    kind = str(data.get("type") or "").strip().lower()
    if kind not in {"text", "drawing", "empty"}:
        kind = "text" if text else ("drawing" if description else "empty")
    if text:
        kind = "text"
    elif kind == "text":
        kind = "empty"
    elif description and kind == "empty":
        kind = "drawing"

    result.update({
        "type": kind,
        "text": text,
        "description": description,
        "language": normalize_language(data.get("language"), text or description, "zh"),
    })
    return result


# ════════════════════════════════════════════════════
# 存储层：JsonStore（本地文件） / PgStore（PostgreSQL）
# 两者提供相同接口：get_config / set_config / bump_visit /
#                  record_usage / stats
# ════════════════════════════════════════════════════

class JsonStore:
    """零依赖：config.json + data.json"""

    name = "JSON 文件"

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.config_path = os.path.join(DATA_DIR, "config.json")
        self.data_path = os.path.join(DATA_DIR, "data.json")
        if not os.path.isfile(self.config_path):
            self.set_config(dict(DEFAULT_CONFIG))

    @staticmethod
    def _load(path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(default)
            merged.update(data)
            return merged
        except Exception:
            return dict(default)

    @staticmethod
    def _save(path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def get_config(self):
        with LOCK:
            return self._load(self.config_path, DEFAULT_CONFIG)

    def set_config(self, cfg):
        with LOCK:
            self._save(self.config_path, cfg)

    def _data(self):
        return self._load(self.data_path, {"total": 0, "days": {}, "users": {}})

    def bump_visit(self):
        with LOCK:
            data = self._data()
            today = datetime.date.today().isoformat()
            data["total"] = int(data.get("total", 0)) + 1
            days = data.setdefault("days", {})
            days[today] = int(days.get(today, 0)) + 1
            for k in sorted(days)[:-60]:
                del days[k]
            self._save(self.data_path, data)

    def record_usage(self, cid, kind, usage):
        with LOCK:
            data = self._data()
            users = data.setdefault("users", {})
            u = users.setdefault(cid, dict(EMPTY_USER))
            u["requests"] = u.get("requests", 0) + 1
            u[kind] = u.get(kind, 0) + 1
            u["prompt_tokens"] = u.get("prompt_tokens", 0) + int(usage.get("prompt_tokens") or 0)
            u["completion_tokens"] = u.get("completion_tokens", 0) + int(usage.get("completion_tokens") or 0)
            u["tokens"] = u.get("tokens", 0) + int(usage.get("total_tokens") or 0)
            u["last"] = now_str()
            self._save(self.data_path, data)

    def stats(self):
        with LOCK:
            data = self._data()
        today = datetime.date.today().isoformat()
        days = data.get("days", {})
        users = data.get("users", {})
        rows = [{"id": k, **v} for k, v in users.items()]
        rows.sort(key=lambda r: -r.get("tokens", 0))
        return {
            "total": data.get("total", 0),
            "today": days.get(today, 0),
            "days": [{"date": d, "count": days[d]} for d in sorted(days)[-14:]],
            "tokens_total": sum(u.get("tokens", 0) for u in users.values()),
            "users_count": len(users),
            "users": rows[:200],
        }


class PgStore:
    """PostgreSQL：访问统计、用户 token、配置全部入库"""

    name = "PostgreSQL"

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS visits (
        day   date PRIMARY KEY,
        count integer NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS diary_users (
        id                text PRIMARY KEY,
        requests          integer NOT NULL DEFAULT 0,
        chats             integer NOT NULL DEFAULT 0,
        reads             integer NOT NULL DEFAULT 0,
        prompt_tokens     bigint  NOT NULL DEFAULT 0,
        completion_tokens bigint  NOT NULL DEFAULT 0,
        tokens            bigint  NOT NULL DEFAULT 0,
        last              text    NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS kv (
        key   text PRIMARY KEY,
        value text NOT NULL
    );
    """

    def __init__(self, dsn):
        import psycopg2  # 需要 psycopg2-binary（见 requirements.txt）
        self.psycopg2 = psycopg2
        self.dsn = re.sub(r"^postgres://", "postgresql://", dsn)
        self._conn = None
        self._exec(self.SCHEMA)

    def _connect(self):
        conn = self.psycopg2.connect(self.dsn)
        conn.autocommit = True
        return conn

    def _exec(self, sql, params=None, fetch=False):
        """单连接 + 失败重连一次；流量很小，无需连接池。"""
        with LOCK:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._conn = self._connect()
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        return cur.fetchall() if fetch else None
                except self.psycopg2.Error:
                    try:
                        if self._conn:
                            self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                    if attempt == 2:
                        raise

    def get_config(self):
        rows = self._exec("SELECT value FROM kv WHERE key = 'config'", fetch=True)
        cfg = dict(DEFAULT_CONFIG)
        if rows:
            try:
                cfg.update(json.loads(rows[0][0]))
            except Exception:
                pass
        return cfg

    def set_config(self, cfg):
        self._exec(
            "INSERT INTO kv (key, value) VALUES ('config', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (json.dumps(cfg, ensure_ascii=False),),
        )

    def bump_visit(self):
        self._exec(
            "INSERT INTO visits (day, count) VALUES (CURRENT_DATE, 1) "
            "ON CONFLICT (day) DO UPDATE SET count = visits.count + 1"
        )

    def record_usage(self, cid, kind, usage):
        chats = 1 if kind == "chats" else 0
        reads = 1 if kind == "reads" else 0
        self._exec(
            "INSERT INTO diary_users AS u "
            "(id, requests, chats, reads, prompt_tokens, completion_tokens, tokens, last) "
            "VALUES (%s, 1, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET "
            "  requests = u.requests + 1, "
            "  chats = u.chats + EXCLUDED.chats, "
            "  reads = u.reads + EXCLUDED.reads, "
            "  prompt_tokens = u.prompt_tokens + EXCLUDED.prompt_tokens, "
            "  completion_tokens = u.completion_tokens + EXCLUDED.completion_tokens, "
            "  tokens = u.tokens + EXCLUDED.tokens, "
            "  last = EXCLUDED.last",
            (
                cid, chats, reads,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                now_str(),
            ),
        )

    def stats(self):
        total = self._exec("SELECT COALESCE(SUM(count), 0) FROM visits", fetch=True)[0][0]
        today_rows = self._exec("SELECT count FROM visits WHERE day = CURRENT_DATE", fetch=True)
        days = self._exec(
            "SELECT day::text, count FROM visits ORDER BY day DESC LIMIT 14", fetch=True
        )
        user_rows = self._exec(
            "SELECT id, requests, chats, reads, prompt_tokens, completion_tokens, tokens, last "
            "FROM diary_users ORDER BY tokens DESC LIMIT 200",
            fetch=True,
        )
        agg = self._exec(
            "SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM diary_users", fetch=True
        )[0]
        return {
            "total": int(total),
            "today": int(today_rows[0][0]) if today_rows else 0,
            "days": [{"date": d, "count": c} for d, c in reversed(days)],
            "tokens_total": int(agg[1]),
            "users_count": int(agg[0]),
            "users": [
                {
                    "id": r[0], "requests": r[1], "chats": r[2], "reads": r[3],
                    "prompt_tokens": int(r[4]), "completion_tokens": int(r[5]),
                    "tokens": int(r[6]), "last": r[7],
                }
                for r in user_rows
            ],
        }


def database_url():
    for name in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def make_store():
    dsn = database_url()
    if dsn:
        try:
            return PgStore(dsn)
        except Exception as e:
            print(f"⚠ PostgreSQL 连接失败（{e}），回退到 JSON 文件存储", flush=True)
    return JsonStore()


STORE = make_store()


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


def language_instruction(language):
    language = normalize_language(language)
    if language == "en":
        return "Reply in English. Do not translate the user's words into Chinese or Japanese."
    if language == "ja":
        return "日本語で返事を書いてください。中国語や英語に翻訳しないでください。"
    return "请用中文回信。默认语言是中文。"


def user_prompt(text, image_context="", input_type="text"):
    text = clean_text(text, 500)
    image_context = clean_text(image_context, 500)
    input_type = str(input_type or "").strip().lower()
    if input_type == "drawing" and image_context:
        return f"书写者没有写字，而是在纸上画了图。图像内容：{image_context}\n请根据这幅图回信。"
    if text and image_context:
        return f"书写者写下：{text}\n旁边的图像内容：{image_context}"
    if text:
        return f"书写者写下：{text}"
    return f"纸页上的图像内容：{image_context}"


def persona_prompt(cfg, language="zh"):
    owner = cfg.get("owner_name") or "一位无名的书写者"
    bg = (cfg.get("owner_background") or "").strip()
    return (
        f"你是一本被遗忘多年的日记，属于「{owner}」。{bg}\n"
        "有人正用墨水在你的书页上写字或画图，你以日记主人的口吻亲笔回信。\n"
        "要求：使用第一人称；语气克制、神秘、有旧信件的质感；"
        "绝不提及自己是AI、程序或模型；回信不超过60个字；"
        "可以用换行分段；只输出回信正文，不要任何解释。\n"
        f"{language_instruction(language)}"
    )


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
            cfg = STORE.get_config()
            return self._send(200, {
                "ai": bool(api_key(cfg)),
                **public_reply_config(cfg),
            })

        if path == "/api/admin/stats":
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, STORE.stats())

        if path == "/api/admin/config":
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            cfg = STORE.get_config()
            key = cfg.get("openai_api_key") or ""
            return self._send(200, {
                "owner_name": cfg.get("owner_name"),
                "owner_background": cfg.get("owner_background"),
                "model": eff_model(cfg),
                "openai_base_url": eff_base_url(cfg),
                **public_reply_config(cfg),
                "api_key_masked": ("…" + key[-4:]) if key else "",
                "env_key": bool(os.environ.get("OPENAI_API_KEY")),
                "env_model": bool(os.environ.get("OPENAI_MODEL")),
                "env_base": bool(os.environ.get("OPENAI_BASE_URL")),
                "env_admin": bool(os.environ.get("ADMIN_PASSWORD")),
                "storage": STORE.name,
            })

        return self._send(404, {"error": "not_found"})

    # ── POST ──
    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/visit":
                STORE.bump_visit()
                return self._send(200, {"ok": True})

            if path == "/api/chat":
                body = self._json_body()
                text = clean_text(body.get("text"), 500)
                image_context = clean_text(
                    body.get("image_context") or body.get("description"),
                    500,
                )
                if not text and not image_context:
                    return self._send(400, {"error": "empty"})
                language = normalize_language(body.get("language"), text or image_context, "zh")
                input_type = str(body.get("input_type") or ("text" if text else "drawing"))
                cfg = STORE.get_config()
                reply, usage = openai_chat(cfg, [
                    {"role": "system", "content": persona_prompt(cfg, language)},
                    {"role": "user", "content": user_prompt(text, image_context, input_type)},
                ])
                STORE.record_usage(self._client_id(), "chats", usage)
                return self._send(200, {"reply": reply, "language": language})

            if path == "/api/read":
                body = self._json_body()
                image = body.get("image") or ""
                if not image.startswith("data:image/"):
                    return self._send(400, {"error": "bad_image"})
                cfg = STORE.get_config()
                raw, usage = openai_chat(cfg, [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": READ_PROMPT},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }], max_tokens=220)
                STORE.record_usage(self._client_id(), "reads", usage)
                return self._send(200, parse_read_result(raw))

            if path == "/api/admin/login":
                body = self._json_body()
                user, pwd = admin_credentials(STORE.get_config())
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
                cfg = STORE.get_config()
                for k in ("owner_name", "owner_background", "model", "openai_base_url"):
                    if k in body and isinstance(body[k], str):
                        cfg[k] = body[k].strip()
                if "reply_effect" in body:
                    cfg["reply_effect"] = normalize_reply_effect(body["reply_effect"])
                if "reply_speed" in body:
                    cfg["reply_speed"] = normalize_reply_speed(body["reply_speed"])
                if body.get("openai_api_key"):
                    cfg["openai_api_key"] = str(body["openai_api_key"]).strip()
                if body.get("admin_user"):
                    cfg["admin_user"] = str(body["admin_user"]).strip()
                if body.get("admin_password"):
                    cfg["admin_password"] = str(body["admin_password"]).strip()
                STORE.set_config(cfg)
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
    cfg = STORE.get_config()
    user, _ = admin_credentials(cfg)
    print(f"遗失的日记   http://localhost:{port}/", flush=True)
    print(f"数据存储     {STORE.name}", flush=True)
    print(f"管理入口     /admin  账户 {user}"
          f"{'（来自环境变量）' if os.environ.get('ADMIN_USER') else '（默认，可用 ADMIN_USER/ADMIN_PASSWORD 覆盖）'}", flush=True)
    print(f"AI 状态      {'已配置' if api_key(cfg) else '未配置：设置 OPENAI_API_KEY 或在管理界面填写'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
