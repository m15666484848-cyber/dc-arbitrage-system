# GitHub Pages 部署指南

本指南将详细介绍如何将DC全币种套利监控系统部署到GitHub Pages，实现远程访问功能。

## 前提条件

1. **GitHub账号**：需要一个GitHub账号
2. **Git安装**：本地安装Git客户端
3. **系统文件**：已准备好完整的系统文件（index.html等）

## 部署步骤

### 步骤1：创建GitHub仓库

1. 登录GitHub账号
2. 点击右上角的"+"按钮，选择"New repository"
3. 填写仓库信息：
   - **Repository name**：建议使用 `dc-arbitrage-system` 或其他有意义的名称
   - **Description**：填写系统描述，如 "DC全币种跨交易所套利监控系统"
   - **Visibility**：选择 "Public"（公开仓库才能使用GitHub Pages）
   - 勾选 "Initialize this repository with a README"
   - 点击 "Create repository"

### 步骤2：克隆仓库到本地

1. 复制仓库的HTTPS或SSH地址
2. 在本地命令行中执行：
   ```bash
   git clone https://github.com/your-username/dc-arbitrage-system.git
   cd dc-arbitrage-system
   ```

### 步骤3：上传系统文件

1. 将以下文件复制到克隆的仓库目录中：
   - `index.html`（系统主文件）
   - `DCKJ LOGO AI.png`（如果系统使用了此图片）
   - 其他必要的资源文件（如果有）

2. 执行Git命令提交文件：
   ```bash
   git add .
   git commit -m "Initial commit: DC套利监控系统"
   git push origin main
   ```

### 步骤4：启用GitHub Pages

1. 进入GitHub仓库页面
2. 点击 "Settings" 选项卡
3. 在左侧菜单中选择 "Pages"
4. 在 "Source" 部分：
   - **Branch**：选择 "main"（或 "master"，取决于你的默认分支）
   - **Folder**：选择 "/ (root)"（根目录）
   - 点击 "Save"

### 步骤5：等待部署完成

- GitHub Pages会自动构建和部署你的网站
- 部署完成后，会显示一个绿色的成功消息和访问URL
- 通常需要1-2分钟完成部署

## 访问系统

部署完成后，你可以通过以下方式访问系统：

- **GitHub Pages URL**：`https://your-username.github.io/dc-arbitrage-system`
- 这个URL可以在任何设备上访问，无需启动本地服务器

## 系统配置说明

### 数据获取

- 系统会自动从交易所API获取实时价格数据
- 首次加载可能需要几秒钟时间获取数据
- 系统会定期自动刷新数据（可在控制面板调整刷新间隔）

### 功能使用

- **手动刷新**：点击"手动刷新"按钮获取最新数据
- **自动刷新**：开启自动刷新开关，设置刷新间隔
- **利润阈值**：设置套利机会的利润阈值
- **手续费**：设置交易手续费率
- **最小挂单量**：过滤掉挂单量较小的交易对
- **飞书通知**：设置飞书Webhook接收套利机会通知

## 注意事项

1. **API限制**：部分交易所API可能有访问频率限制，系统已内置延迟机制
2. **跨域问题**：GitHub Pages使用HTTPS，系统已处理跨域请求
3. **数据准确性**：系统使用真实的买1卖1价，确保数据准确性
4. **隐私安全**：系统运行在客户端，不会存储任何个人信息
5. **性能优化**：系统已优化API请求和数据处理，确保流畅运行

## 故障排查

### 常见问题

1. **页面显示空白**：检查网络连接，刷新页面
2. **数据加载失败**：检查API状态指示器，可能是交易所API暂时不可用
3. **套利机会不显示**：调整利润阈值，可能当前市场波动较小
4. **GitHub Pages部署失败**：检查仓库设置，确保选择了正确的分支和目录

### 解决方法

1. **API失败**：系统会自动使用备用数据，确保系统正常运行
2. **部署问题**：检查GitHub Actions日志，查看具体错误信息
3. **性能问题**：关闭浏览器标签页，重新打开系统页面

## 更新系统

当你对系统进行修改后，只需执行以下步骤更新GitHub Pages：

1. 复制修改后的文件到本地仓库目录
2. 执行Git命令：
   ```bash
   git add .
   git commit -m "Update: 描述你的修改"
   git push origin main
   ```
3. GitHub Pages会自动重新构建和部署

## 高级配置

### 自定义域名

如果你有自己的域名，可以设置自定义域名：

1. 在GitHub Pages设置中，填写 "Custom domain" 字段
2. 在你的域名提供商处添加CNAME记录，指向 `your-username.github.io`
3. 等待DNS解析完成（通常需要1-24小时）

### 启用HTTPS

GitHub Pages默认启用HTTPS，确保数据传输安全。

## 总结

通过GitHub Pages部署DC套利监控系统，你可以：

- ✅ 随时随地访问系统，无需本地服务器
- ✅ 免费托管，无需支付服务器费用
- ✅ 自动部署，简化更新流程
- ✅ 全球访问，支持多设备访问
- ✅ 安全可靠，使用HTTPS加密

部署完成后，你就可以通过GitHub Pages URL远程访问你的套利监控系统了！