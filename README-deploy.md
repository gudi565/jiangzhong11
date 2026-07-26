# 降重 (jiangzhong) — 部署到 Render 指引

中文学术改写 SaaS。FastAPI + GLM-4-Flash。

## 一次性部署（约 10 分钟）

1. 注册 Render（免费）：https://render.com（用 GitHub 或邮箱注册）
2. New → Blueprint → 选这个仓库 → 它会自动读 `render.yaml`
3. 在环境变量里填两个值（Render 会提示）：
   - `ZHIPU_API_KEY` = （见桌面「降重-部署密钥.txt」）
   - `JZ_ADMIN_SECRET` = （见同上文件）
4. Create → 等 2–5 分钟构建 → 拿到一个 `https://jiangzhong.onrender.com` 这样的公开链接

## 生成兑换码（卖了才用）

部署后（一个码 = 1 小时）：
```
curl -X POST https://你的链接.onrender.com/api/admin/gen-codes \
  -H 'Content-Type: application/json' \
  -d '{"secret":"你的JZ_ADMIN_SECRET","n":10,"seconds":3600}'
```
`seconds` = 使用秒数（3600=1小时，7200=2小时，86400=1天）。把返回的码作为卡密在淘宝自动发货。买家打开链接、粘贴码、即激活 1 小时。

## 注意

- **免费层会在 15 分钟无访问后休眠**，下一次访问冷启动约 30–60 秒（像卡一下，正常）。常有人用就不会休。
- **免费层磁盘是临时的**：`state.json`（配额/兑换码记录）在每次重新部署或休眠唤醒后会重置。用于测试和免费试用没问题；要正式卖兑换码，升级 Render Disk（约 $7/月）让记录持久化，或接一个数据库。
- 你的 GLM key 只放在 Render 环境变量里，不在代码仓库中。
