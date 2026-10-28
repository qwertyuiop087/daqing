# DQFB 管理后台（webadmin）部署说明

管理后台用 FastAPI 写，**直接读写机器人同一个数据库**（`app_config` / `users` / `lottery_records` 等表），
所以网页上改抽奖/套餐/定价，机器人立即生效，两边不会格式不一致。

## 部署到 Railway（与机器人同项目）

1. 本目录（`webadmin`）位于机器人仓库根目录下，随仓库一起推送。
2. Railway 打开机器人所在项目 → **New Service** → Deploy from GitHub，选同一个仓库。
3. 关键设置：
   - **Root Directory**：填 `webadmin`
   - 环境变量：
     - `DATABASE_URL`：填**与机器人完全相同**的 Postgres 连接串（重要，必须是同一个库）
     - `ADMIN_PASSWORD`：后台登录密码（必填，不填服务会拒绝启动）
4. Deploy 完成后，Railway 会给这个服务一个域名（形如 `xxx.up.railway.app`）：
   - `/`：介绍页（读取仓库根目录 `website/index.html`）
   - `/admin`：管理后台登录页

## 把 dqfb.net 指过来

- 把 `dqfb.net` 的 DNS 解析到 Railway 服务的域名（Railway 域名面板里有 Generate Domain，或直接在域名商 CNAME 到 `xxx.up.railway.app`）。
- 如果介绍页想单独放静态托管（Vercel/Netlify 等），直接部署 `website/` 文件夹即可，后台保持在 Railway。

## 本地调试

```bash
cd webadmin
pip install -r requirements.txt
set DATABASE_URL=sqlite:///./test.db     # Windows；Linux/Mac 用 export
set ADMIN_PASSWORD=123456
uvicorn main:app --reload --port 8080
```

浏览器打开 http://127.0.0.1:8080/admin

## 说明

- 登录有效期 24 小时（HMAC 签名 cookie）。
- 后台改「欢迎语」会清掉消息实体（会员表情等），机器人按 HTML 渲染；如需带会员表情的欢迎语，请继续用机器人管理面板编辑。
- 抽奖通知模板占位符：`{username}` 用户名字、`{user_id}` 用户 ID、`{source}` 来源、`{prize}` 奖品。
