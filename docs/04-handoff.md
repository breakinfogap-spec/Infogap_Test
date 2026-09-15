# InfoGap Test 项目交接文档

更新时间：2026-09-15  
仓库：https://github.com/breakinfogap-spec/Infogap_Test  
本地目录：`C:\Users\ricky.zhao\OneDrive - Inno Foods Inc\Documents\ChatGPT\Web\Infogap_Test`

## 1. 项目目标

这个项目是一个“打破信息差”的每日新闻分析网页。产品不是新闻聚合站，而是每天自动抓取前一天的重要新闻，交给 AI 提炼和分析，重点回答：

- 这件事对加拿大普通人意味着什么
- 对温哥华本地人有没有特殊影响
- 普通人需要关注什么实际变化
- 分析依据来自哪些真实新闻来源

当前优先级：

1. 温哥华本地 / BC 新闻
2. 加拿大全国新闻
3. 美国新闻
4. 国际新闻也要认真分析，不能只简单带过

当前主题入口按主题组织：

- 金融
- 科技
- 民生 / 政策
- 移民

页面结构保持两页：

- 首页：网站标题 + 主题入口 + 每日文章列表
- 详情页：标题 + TTS 播放器 + AI 分析正文 + Citations

## 2. 当前技术架构

当前 repo 已经搭好一个轻量自动化版本：

```text
GitHub Actions 定时运行
        ↓
Python pipeline 抓取 RSS 新闻候选
        ↓
DeepSeek 生成中文分析文章
        ↓
Gemini 做基础内容审查
        ↓
Google Cloud Text-to-Speech 生成 MP3
        ↓
Jinja2 渲染静态网页
        ↓
Cloudflare Worker / R2 发布
        ↓
Healthchecks 监控成功或失败
```

主要目录：

```text
.github/workflows/daily.yml       每日正式 pipeline
.github/workflows/smoke.yml       连通性测试 workflow
config/site.json                  网站名称、主题、编辑规则
config/source-registry.json       新闻来源登记和启用状态
scripts/check_config.py           检查 GitHub Secrets 是否齐全
scripts/connectivity_smoke.py     检查 API / Cloudflare / Google TTS 连通性
scripts/pipeline.py               抓新闻、AI 生成、TTS、渲染网页
workers/                          Cloudflare Worker
wrangler.toml                     Cloudflare 配置
templates/                        HTML 模板
```

## 3. 已完成的账号和资源

你已经准备或确认过这些：

```text
GitHub repo：已创建
Gemini Key：已创建
DeepSeek Key：已创建
DeepSeek 余额：有
Google Cloud Billing：已启用
Text-to-Speech API：已启用
Cloudflare：已登录可用
Cloudflare R2 bucket：已创建
Healthchecks.io：已注册可用
Google Workload Identity Federation：已配置
Google Service Account：已创建
```

Cloudflare R2 bucket 名称：

```text
daily-impact-news-content
```

Google Cloud 项目：

```text
Project display name: My First Project
Project ID: project-137fab04-e16d-4a9a-892
Project number: 910985851562
```

Google Service Account：

```text
infogap-tts-runner@project-137fab04-e16d-4a9a-892.iam.gserviceaccount.com
```

Workload Identity Pool / Provider：

```text
Pool ID: github-actions
Provider ID: github-actions
Provider type: OIDC
Issuer URL: https://token.actions.githubusercontent.com
```

Workload Identity Provider resource name：

```text
projects/910985851562/locations/global/workloadIdentityPools/github-actions/providers/github-actions
```

## 4. GitHub Secrets 当前应有清单

GitHub 路径：

```text
Repo → Settings → Secrets and variables → Actions → Repository secrets
```

需要有这些 Repository secrets：

```text
CLOUDFLARE_ACCOUNT_ID
CLOUDFLARE_API_TOKEN
CLOUDFLARE_R2_BUCKET
DEEPSEEK_API_KEY
GEMINI_API_KEY
GOOGLE_AI_API
GOOGLE_CLOUD_PROJECT_ID
GOOGLE_WORKLOAD_IDENTITY_PROVIDER
GOOGLE_SERVICE_ACCOUNT_EMAIL
HEALTHCHECKS_PING_URL
```

其中几个关键值：

```text
CLOUDFLARE_R2_BUCKET=daily-impact-news-content
GOOGLE_CLOUD_PROJECT_ID=project-137fab04-e16d-4a9a-892
GOOGLE_SERVICE_ACCOUNT_EMAIL=infogap-tts-runner@project-137fab04-e16d-4a9a-892.iam.gserviceaccount.com
GOOGLE_WORKLOAD_IDENTITY_PROVIDER=projects/910985851562/locations/global/workloadIdentityPools/github-actions/providers/github-actions
```

注意：

- `GOOGLE_APPLICATION_CREDENTIALS_JSON` 不再需要。
- Google Cloud 阻止创建 Service Account JSON key，这是正常的安全策略。
- 我们已经改成更安全的 Workload Identity Federation。
- GitHub secrets 保存后看不到 value，这是 GitHub 正常行为，只能覆盖，不能查看原值。

## 5. Google Cloud 配置已经做过什么

### 5.1 为什么不用 JSON key

Google Cloud 显示过这个错误：

```text
Service account key creation is disabled
Organization Policy: iam.disableServiceAccountKeyCreation
```

这不是公司网络问题，而是 Google Cloud Organization Policy / secure-by-default 限制。它阻止下载长期 JSON key。

因此改用 Workload Identity Federation：

```text
GitHub Actions
        ↓
GitHub OIDC token
        ↓
Google Workload Identity Provider
        ↓
临时 impersonate Service Account
        ↓
调用 Google Text-to-Speech
```

### 5.2 已创建的 WIF 配置

Workload Identity Pool：

```text
github-actions
```

Provider：

```text
github-actions
```

Issuer URL：

```text
https://token.actions.githubusercontent.com
```

Attribute mapping：

```text
google.subject        = assertion.sub
attribute.repository  = assertion.repository
attribute.actor       = assertion.actor
attribute.aud         = assertion.aud
```

Repository 限制：

```text
attribute.repository = breakinfogap-spec/Infogap_Test
```

Connected service account 页面已经看到：

```text
infogap-tts-runner
```

这说明 GitHub Provider 已经连接到 service account。

## 6. 已推送的关键代码提交

### 6.1 支持无 JSON key 的 Google Cloud 登录

提交：

```text
7aa88d9 Support keyless Google Cloud auth
```

做了这些事：

- `daily.yml` 和 `smoke.yml` 增加：

```yaml
permissions:
  contents: read
  id-token: write
```

- 增加 `google-github-actions/auth@v2`
- 支持两种 Google 认证方式：
  - 优先：`GOOGLE_WORKLOAD_IDENTITY_PROVIDER` + `GOOGLE_SERVICE_ACCOUNT_EMAIL`
  - 备用：`GOOGLE_APPLICATION_CREDENTIALS_JSON`
- `scripts/check_config.py` 支持 WIF secrets
- `scripts/connectivity_smoke.py` 支持 GitHub Actions 自动生成的 ADC credentials

### 6.2 启用可用 RSS 来源

提交：

```text
2539981 Enable verified RSS sources
```

解决的问题：

GitHub Actions 之前失败在：

```text
No enabled source produced candidates. Enable verified feeds before the real run.
```

原因是 `config/source-registry.json` 里所有来源都是：

```json
"enabled": false
```

修复内容：

- 启用一批已经验证能访问的 RSS 来源
- 给 RSS 请求加 8 秒超时，避免某个源卡住整个 pipeline
- 如果没有候选，输出每个源的 debug 信息，方便下次定位

当前已启用来源包括：

```text
CBC — British Columbia
CBC — Top Stories
CBC — Business
CBC — Technology
CBC — World
Global News — BC
Global News — Canada
Global News — World
Bank of Canada
Federal Reserve
Ars Technica
BBC World RSS
```

本地验证结果：

```text
python scripts/pipeline.py --date 2026-09-14
→ ok: true
→ candidates: 29
```

## 7. 当前 GitHub Actions 状态

### 7.1 Smoke Test

目标：检查 secrets、Google TTS、Cloudflare、基础 build 是否通。

运行路径：

```text
GitHub repo → Actions → Smoke Test → Run workflow
```

根据对话状态：大部分连接已经跑通。

### 7.2 Daily News Pipeline

目标：完整模拟每日生成。

运行路径：

```text
GitHub repo → Actions → Daily News Pipeline → Run workflow
```

测试参数建议：

```text
date: 2026-09-14
publish: false
```

注意：

- `publish=false` 不会正式发布，只生成和测试。
- 如果这一步通过，再考虑跑 `publish=true`。
- 不建议一开始就发布，先确认文章、引用和音频输出质量。

## 8. 已经遇到并解决的问题

### 问题 1：找不到 Service Account JSON key

原因：Google Cloud 禁止创建 JSON key。

解决：改用 Workload Identity Federation。

### 问题 2：GitHub Actions 缺少 secrets

报错：

```text
Missing required environment variables:
- GEMINI_API_KEY
- CLOUDFLARE_R2_BUCKET
- HEALTHCHECKS_PING_URL
```

解决：补齐 GitHub Repository secrets。

其中：

```text
CLOUDFLARE_R2_BUCKET=daily-impact-news-content
```

### 问题 3：误选 Speech-to-Text 权限

Google IAM 搜索 `Text-to-Speech` 时曾出现：

```text
Cloud Speech-to-Text Service Agent
```

这是错的，它是语音转文字，不是文字转语音。

最终没有保存这个错误角色。

### 问题 4：Daily Pipeline 抓不到新闻候选

报错：

```text
No enabled source produced candidates.
```

原因：新闻源配置还处于 preparation_only，所有 source 都没启用。

解决：启用已验证 RSS，并给 pipeline 加 timeout/debug。

## 9. 现在你下一步要做什么

### 第一步：重新跑 Daily News Pipeline

GitHub 页面：

```text
https://github.com/breakinfogap-spec/Infogap_Test/actions
```

操作：

```text
Actions → Daily News Pipeline → Run workflow
```

填写：

```text
date: 2026-09-14
publish: false
```

预期：

- 不应该再报 `No enabled source produced candidates`
- 会进入 DeepSeek 生成文章
- 会进入 Gemini review
- 会进入 Google TTS
- 会生成静态网页文件

如果失败，把红色日志截图发回来。

### 第二步：如果 2026-09-14 通过，再跑 Smoke Test

```text
Actions → Smoke Test → Run workflow
```

这个用于确认所有外部服务连通性。

### 第三步：如果前两步都过，再跑发布测试

```text
Actions → Daily News Pipeline → Run workflow
```

填写：

```text
date: 2026-09-14
publish: true
```

这一步会：

- deploy Cloudflare Worker
- 上传 generated/site 到 R2
- Healthchecks ping success

## 10. 当前仍需完善的地方

### 10.1 温哥华官方来源还没完全自动化

当前启用的来源以 RSS 媒体源为主，适合先跑通 pipeline。

温哥华官方政策、补贴和公众咨询来源还需要继续补：

```text
City of Vancouver News
City of Vancouver Grants and Awards
Shape Your City Vancouver
BC Gov News
BC consultations / govTogetherBC
TransLink alerts
IRCC Newsroom
BC PNP / WelcomeBC
Statistics Canada
```

之前没有直接启用它们的原因：

- 有些页面没有稳定 RSS
- 有些页面本地请求返回 403
- 有些页面只有目录，需要单独解析项目详情页
- 政策、补贴、资格、截止日期不能靠摘要猜，必须拿到官方页面证据

### 10.2 内容质量还需要验收

pipeline 跑通不等于产品可上线。

需要验收：

- AI 是否真的回答“对加拿大普通人意味着什么”
- 涉及温哥华是否有单独温哥华视角
- Citations 是否都是真实可打开来源
- 是否避免了没有依据的金额、资格、日期、政策结论
- TTS 是否能正常播放
- 首页和详情页是否清楚易读

### 10.3 SEO 和 5 天保留机制后续要补强

产品设定是只保留最近 5 天内容。当前配置里已有：

```text
NEWS_RETENTION_DAYS=5
```

但后续还要确认：

- R2 中旧内容是否自动删除
- 旧文章 URL 是否返回 404 / 410 / archive index
- SEO 是否需要保留轻量摘要页或 sitemap

### 10.4 邮件召回功能还没实现

之前方案结论：

- 不发每日邮件
- 优先后续做“3 天未活跃用户召回”或简化版“每 3 天全量邮件”
- 现阶段先不急着做，因为需要用户系统、行为追踪、退订和隐私政策

合规重点：

```text
CASL
退订功能
隐私政策
邮件同意记录
```

### 10.5 留言功能现阶段不建议加

原因：

- 内容审核成本高
- 垃圾信息风险高
- 法律和平台责任增加
- 当前核心目标是内容生成和自动化跑通

建议以后先做：

```text
反馈表单 / email feedback
```

不要马上做开放留言区。

## 11. 常用命令

本地目录：

```powershell
Set-Location -LiteralPath 'C:\Users\ricky.zhao\OneDrive - Inno Foods Inc\Documents\ChatGPT\Web\Infogap_Test'
```

查看 git 状态：

```powershell
git status --short
```

本地只跑静态生成，不调用 AI / TTS：

```powershell
$py='C:\Users\ricky.zhao\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py scripts\pipeline.py --date 2026-09-14
```

安装依赖：

```powershell
$py='C:\Users\ricky.zhao\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py -m pip install -r requirements.txt
```

推送代码：

```powershell
git add .
git commit -m "message"
git push
```

## 12. 关键提醒

不要把任何 API Key 写进代码或文档。

不要再尝试创建 Google Service Account JSON key。当前项目已经走 WIF，更安全。

如果 GitHub Actions 报错，优先看失败步骤：

```text
check_config.py      多半是 secrets 缺失或名称错
Generate site        多半是新闻源、AI 返回、TTS 权限或内容格式问题
Deploy Worker        多半是 Cloudflare token / wrangler 配置问题
Upload to R2         多半是 bucket 名、R2 权限或 Cloudflare token 权限问题
Healthchecks         多半是 ping URL 错
```

当前最重要的下一步是重新跑：

```text
Daily News Pipeline
 date: 2026-09-14
 publish: false
```
