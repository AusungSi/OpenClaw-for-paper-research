# MemoMate V1

MemoMate 是本地部署的企业微信智能备忘录服务。

## 一次性解决 URL 变化问题

你的痛点是 `cloudflared tunnel --url ...` 每次都会变 `trycloudflare.com` 地址。  
正确方案是使用 **Named Tunnel + 固定域名**，只配一次企业微信回调地址。

前提：你有一个接入 Cloudflare 的域名（例如 `example.com`）。

## 快速开始

1. 安装依赖

```powershell
pip install -r requirements.txt
```

2. 创建配置

```powershell
copy .env.example .env
```

3. 首次一键初始化并启动（会要求你输入固定域名）

```powershell
.\scripts\one_click_test.ps1
```

脚本会自动做这些事：
- 如果没配置过固定隧道：执行 Cloudflare 登录、创建 tunnel、绑定 DNS、写入 `.env`
- 启动后端服务
- 启动隧道服务
- 输出固定回调地址 `https://<你的固定域名>/wechat`

4. 在企业微信后台把回调地址配置为上面的固定地址（只需一次）

后续每次启动都只需要：

```powershell
.\scripts\one_click_test.ps1
```

## 其他常用脚本

- 仅启动后端：`.\scripts\start_backend.ps1`
- 启动隧道（自动优先固定隧道，缺失时退回临时隧道）：`.\scripts\start_tunnel.ps1`
- 手动初始化固定隧道：`.\scripts\setup_named_tunnel.ps1 -Hostname memomate.yourdomain.com`
- 本地流程 smoke（不依赖微信）：`python .\scripts\smoke_intent_flow.py`
- 一键先测再启：`.\scripts\one_click_start_and_test.ps1`

## API

- `GET /wechat`: 企业微信 URL 验证
- `POST /wechat`: 企业微信消息入口
- `GET /api/v1/health`: 健康检查
- `POST /api/v1/auth/pair`: 移动端配对换 token
- `POST /api/v1/auth/refresh`: 刷新 token
- `GET/POST/PATCH/DELETE /api/v1/reminders`
- `GET /api/v1/calendar`
- `POST /api/v1/asr/transcribe`: 本地语音转文字 API（Bearer + multipart）
- `GET /api/v1/capabilities`: 当前能力与 provider 映射

## 测试

```powershell
python -m pytest -q
```

## 开发演示

- 详细步骤：`docs/DEMO_STEPS.md`

## 上传 GitHub（首次）

```powershell
git init
git branch -M main
git add .
git commit -m "chore: initialize memomate project"
git remote add origin <你的仓库地址>
git push -u origin main
```

说明：
- `.env`、本地数据库、`cloudflared.exe`、缓存目录已在 `.gitignore` 中排除，不会被上传。
