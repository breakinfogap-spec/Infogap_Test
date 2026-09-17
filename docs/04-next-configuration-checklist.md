# 下一步配置清单

更新时间：2026-09-14。

这份清单用于从“账号都准备好了”进入“自动化能跑”。不要把任何真实 API Key 写进本文档或聊天。

## 一、当前结论

用户已确认：

- GitHub 私有仓库已创建。
- 项目邮箱已准备。
- Cloudflare 已登录可用，暂时无域名，先使用 workers.dev 测试。
- Gemini Key 已创建。
- DeepSeek Key 已创建，且账户有余额。
- Google Cloud Billing 已启用。
- Google Cloud Text-to-Speech API 已启用，并有 300 美元 credit。
- Healthchecks.io 已注册。

还不能直接执行任务二，因为流水线还没有获得运行时凭证，也没有完成最小连通测试。

## 二、GitHub Secrets

进入 GitHub repo -> Settings -> Secrets and variables -> Actions -> New repository secret，后续需要放这些值：

- `DEEPSEEK_API_KEY`：DeepSeek 项目专用 Key。
- `GEMINI_API_KEY`：Google AI Studio 项目专用 Key。已使用 `GOOGLE_AI_API` 时也可以，workflow 已兼容这个别名。
- `GOOGLE_CLOUD_PROJECT_ID`：Google Cloud 项目 ID。
- `GOOGLE_APPLICATION_CREDENTIALS_JSON`：Google Cloud TTS 的调用凭证 JSON。若组织策略禁止创建服务账号密钥，则改用 `GOOGLE_WORKLOAD_IDENTITY_PROVIDER` + `GOOGLE_SERVICE_ACCOUNT_EMAIL`。
- `CLOUDFLARE_ACCOUNT_ID`：Cloudflare Account ID。
- `CLOUDFLARE_API_TOKEN`：只给这个项目需要的 Worker、R2、D1 部署权限。
- `CLOUDFLARE_R2_BUCKET`：新闻正文、证据文件和 MP3 的 bucket 名。
- `HEALTHCHECKS_PING_URL`：Healthchecks 里新建 check 后得到的 ping URL。

暂时不要放：

- Brevo Key。邮件召回阶段再做。
- 域名相关配置。workers.dev 测试通过后再绑定。
- 任何个人密码、信用卡、恢复码。

## 三、Cloudflare 资源

Cloudflare 里先创建测试资源：

- Worker：建议名 `daily-impact-news`。
- R2 bucket：建议名 `daily-impact-news-content`。
- D1 database：建议名 `daily_impact_news`。
- workers.dev 子域：先用默认测试地址。

当前阶段不需要买域名，也不需要打开邮件功能。

## 四、Google Cloud TTS

需要确认：

- Text-to-Speech API 在正确项目中已启用。
- Billing 已绑定到这个项目。
- 服务账号只授予 TTS 所需权限。
- 先生成 10-20 秒中文测试音频，确认声音、格式、费用记录正常。

推荐使用 Google Cloud Chirp 3 HD 中文声音，默认 cmn-CN-Chirp3-HD-Achernar。它比 WaveNet 更自然；当前有 Google Cloud trial credit，适合先用于提升朗读体验。

## 五、最小连通测试

在任务二正式取 2026-09-13 新闻前，先做这些测试：

- GitHub Actions 能手动启动。
- DeepSeek 能完成一次 100-200 字摘要测试。
- Gemini 能完成一次事实核查格式测试。
- Google TTS 能生成一个短 MP3。
- Cloudflare Worker 能部署到 workers.dev。
- R2 能写入和读取一个测试 JSON 与测试 MP3。
- Healthchecks 能收到成功 ping。

只有这些通过后，才执行任务二的完整 test run。

## 六、任务二进入条件

任务二允许开始的条件：

- 所有 GitHub Secrets 已设置。
- 测试 Worker 可访问。
- R2 bucket 写入正常。
- TTS 测试音频正常。
- DeepSeek 与 Gemini 小调用正常。
- Healthchecks 收到至少一次测试 ping。
- 新闻源采集脚本能抓到至少一批候选新闻，且不会绕过登录、付费墙、robots 或反爬限制。

任务二仍锁定为：手动模拟 2026-09-13 的真实新闻，按主题输出金融、科技、民生（含政策）、移民，并优先解释对加拿大普通人和温哥华本地人的实际影响。

