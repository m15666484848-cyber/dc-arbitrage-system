# Cloudflare Workers 部署指南

## 步骤 1：注册 Cloudflare
1. 访问 https://dash.cloudflare.com/sign-up
2. 免费注册账号

## 步骤 2：创建 Worker
1. 登录后，点击左侧 "Workers & Pages"
2. 点击 "Create Application"
3. 选择 "Create Worker"
4. 名称填：`dc-arbitrage-proxy`
5. 点击 "Deploy"

## 步骤 3：编辑 Worker 代码
1. 点击 "Edit code"
2. 删除默认代码，粘贴 `cloudflare-worker.js` 的内容
3. 点击 "Save and Deploy"

## 步骤 4：获取 Worker URL
部署成功后，你会得到一个 URL，类似：
`https://dc-arbitrage-proxy.你的用户名.workers.dev`

## 步骤 5：更新 HTML 文件
将 Worker URL 告诉助手，我会帮你更新 HTML 文件使用这个代理。

---

## 使用方式
API 请求格式：
`https://你的worker.workers.dev/?url=https://api.upbit.com/v1/market/all`

免费额度：
- 每天 100,000 次请求
- 完全够用
