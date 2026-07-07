# 遗失的日记

一本会回信的日记：在羊皮纸上手写，停笔片刻，墨迹被纸页吸收，随后日记以手写体逐字写下回信——灵感来自《哈利·波特》中的日记。

- 手写输入（鼠标 / 触屏），墨迹快细慢粗
- AI 辨字（GPT 视觉），可切换为浏览器本地 OCR（tesseract.js）
- GPT 以"日记所属人"的身份与口吻回信，人设由管理员配置
- `/admin` 管理界面：访问统计、每个用户的 token 消耗、日记人设与 AI 接口配置
- 零第三方依赖：后端只用 Python 标准库

## 本地运行

```bash
export OPENAI_API_KEY=sk-xxx        # 可选，不设则回退到本地 OCR + 本地回复
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
   | `OPENAI_MODEL`    | —    | 默认 `gpt-4o-mini`                 |
   | `OPENAI_BASE_URL` | —    | 默认官方地址，使用中转服务时修改   |
   | `DATA_DIR`        | —    | 数据目录，配合 Volume 使用（见下） |

3. **持久化（建议）**：Railway 容器文件系统是临时的，重新部署会清空访问统计与管理界面里保存的设定。给服务挂一个 Volume（例如挂载到 `/data`），并设置 `DATA_DIR=/data` 即可持久保存。
4. 部署完成后：主页即 Railway 分配的域名根路径，管理界面在 `/admin`（主页不提供入口）。

环境变量的优先级高于管理界面 / `config.json` 中的同名设置；由环境变量管理的项在管理界面中显示为只读。

## 管理界面

`/admin`，账号密码登录（会话 24 小时，服务重启后需重新登录）：

- **访问统计**：总访问、今日访问、最近 14 天明细
- **用户 Token 消耗**：按匿名用户标识列出回信/辨字次数、提示与回复 token、合计与最后活跃时间
- **日记设定**：日记所属人及其背景，AI 回信将以此身份书写
- **AI 接口**：模型、Base URL、API Key（只显示尾号）
- **安全**：修改管理账户（若未被环境变量接管）

## 架构

```
index.html   主页：手写画布 → toDataURL → /api/read 辨字 → /api/chat 回信
admin.html   管理界面（/admin）
server.py    零依赖后端：静态页 + OpenAI 代理 + 统计（data.json）+ 配置（config.json）
```

`config.json` / `data.json` 在首次运行时生成，含密钥与统计数据，已被 `.gitignore` 排除。
