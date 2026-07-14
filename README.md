# 墨页

一本会回信的日记：在羊皮纸上手写，停笔片刻，墨迹被纸页吸收，随后日记以手写体逐字写下回信——灵感来自《哈利·波特》中的日记。

- 手写输入（鼠标 / 触屏），墨迹快细慢粗
- AI 辨字（GPT 视觉），管理员可关闭并回退到浏览器本地 OCR（tesseract.js）
- 支持中文、英文、日文识别；识别到什么语言就用同语言回信，默认中文
- 如果用户画的是图，AI 会根据图像内容回信
- 访客界面只保留纸页；停笔后自动读取，没有阅读/新页/AI 开关按钮
- GPT 以"日记所属人"的身份与口吻回信，人设由管理员配置
- `/admin` 管理界面：访问统计、每个用户的 token 消耗、日记人设、AI 接口、回信特效与速度配置
- 持久化：默认本地 JSON；设置 `DATABASE_URL` 后使用 PostgreSQL

## 本地运行

```bash
export OPENAI_API_KEY=sk-xxx        # 可选，不设则回退到本地 OCR + 本地回复
pip install -r requirements.txt      # PostgreSQL 支持依赖；不设 DATABASE_URL 时运行时不会连接数据库
python3 server.py                    # 默认 http://localhost:8765
```

管理入口 `http://localhost:8765/admin`，本地默认账户 `admin` / `admin`。

## 部署到 Railway

1. Railway → New Project → **Deploy from GitHub repo**，选择本仓库。
   Nixpacks 会自动识别为 Python 项目并按 `Procfile` 启动（`PORT` 由 Railway 注入）。
2. 在 **Variables** 里配置环境变量：

   | 变量              | 必填 | 说明                               |
   | ----------------- | ---- | ---------------------------------- |
   | `OPENAI_API_KEY`  | ✅   | OpenAI Key，只存在服务端           |
   | `ADMIN_USER`      | ✅   | 管理员初始账户名                   |
   | `ADMIN_PASSWORD`  | ✅   | 管理员初始密码                     |
   | `DATABASE_URL`    | —    | PostgreSQL 连接串；也兼容 `POSTGRES_URL` / `POSTGRESQL_URL` |
   | `OPENAI_MODEL`    | —    | 默认 `gpt-4o-mini`                 |
   | `OPENAI_BASE_URL` | —    | 默认官方地址，使用中转服务时修改   |
   | `DATA_DIR`        | —    | JSON 文件模式的数据目录，配合 Volume 使用 |

3. **持久化（建议）**：Railway 加 Postgres 插件并把 `DATABASE_URL` 配到服务后，访问统计、用户 token 消耗和管理界面配置会写入 PostgreSQL，重新部署不丢数据。未设置数据库时会使用 `config.json` / `data.json`；Railway 文件系统是临时的，JSON 模式需挂 Volume（例如 `/data`）并设置 `DATA_DIR=/data`。
4. 部署完成后：主页即 Railway 分配的域名根路径，管理界面在 `/admin`（主页不提供入口）。

环境变量的优先级高于管理界面 / `config.json` 中的同名设置；由环境变量管理的项在管理界面中显示为只读。

## 管理界面

`/admin`，账号密码登录（会话 24 小时，服务重启后需重新登录）：

- **访问统计**：总访问、今日访问、最近 14 天明细
- **用户 Token 消耗**：按匿名用户标识列出回信/辨字次数、提示与回复 token、合计与最后活跃时间
- **日记设定**：日记所属人及其背景，AI 回信将以此身份书写
- **AI 接口**：模型、Base URL、API Key（只显示尾号）、AI 辨字开关
- **回信显示**：设置一笔一画、渐变浮现、墨水显现，以及书写速度
- **安全**：修改管理账户（若未被环境变量接管）

## 架构

```
index.html   主页：手写画布 → toDataURL → /api/read 辨字 → /api/chat 回信
admin.html   管理界面（/admin）
server.py    后端：静态页 + OpenAI 代理 + JSON/PostgreSQL 存储 + 管理接口
```

`config.json` / `data.json` 在首次运行时生成，含密钥与统计数据，已被 `.gitignore` 排除。
