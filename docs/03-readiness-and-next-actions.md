# 任务一实际准备状态

更新时间：2026-09-14。此文件只记录已获得的证据，不保存任何密钥内容。

## 一、用户已确认的资源

| 项目 | 账号状态 | 密钥／资源／调用验证 | 下一步 |
|---|---|---|---|
| GitHub | 已有（用户确认） | 私有仓库已创建（用户确认），远程地址未记录 | 后续连接本地项目并设置 Actions Secrets |
| Cloudflare | 已有（用户确认） | 已登录可用；Worker／R2／D1／部署权限未验证 | 建立测试资源与受限部署凭证 |
| Google AI Studio | 已有（用户确认） | Gemini Key 已创建（用户确认），实际调用未验证 | 存入 GitHub Secrets 后做小调用测试 |
| DeepSeek | 已有（用户确认） | DeepSeek Key 已创建且有余额（用户确认），实际调用未验证 | 存入 GitHub Secrets 后做小调用测试 |
| Google Cloud TTS | 已有（用户确认） | Billing 已启用，Text-to-Speech API 已启用，有 300 美元 credit（用户确认），实际调用未验证 | 配置调用身份并做短中文音频测试 |
| Azure | 没有 | 不适用 | 当前默认方案不要求 |
| Brevo | 暂缓 | 未验证 | 邮件召回阶段再注册和验证 |
| 域名 | 暂无 | 未购买／绑定 | 先用 workers.dev 测试；成功后再买 |
| Healthchecks | 已注册（用户确认） | Check URL 未创建／未接入 | GitHub Actions 完成后接入漏跑监测 |
| 运营邮箱 | 已有（用户确认） | 未接入站点联系页 | 后续放入隐私政策、纠错反馈和邮件身份信息 |

不要将密钥粘贴进此表或聊天。已有账号信息由用户本轮回复提供，没有登录私人账号进行核验。

## 二、本轮已经完成的实际工作

- [x] 阅读原始版本并与前一方案比较。
- [x] 按最新意见固定四个主题、加拿大视角、温哥华特殊影响及本地参与机会规则。
- [x] 根据现有账号将默认配音调整为 Google Cloud TTS。
- [x] 创建项目准备文档与凭证变量占位模板。
- [x] 建立 19 个来源入口登记，区分已读入口与仍待验证的采集能力。
- [x] 核查 Google TTS 当前价格和启用条件；汇集本轮已查核的其他官方价格。
- [x] 本地 Python 3.12.14 可执行；2026-11-15 的 America/Vancouver 偏移为 UTC−7。
- [x] Node 和 Git 可定位；未调用 Python 的 Windows Store 占位启动器。
- [x] 检查依赖：PyYAML、Jinja2、requests、feedparser、trafilatura 当前运行时均未安装；后续使用项目隔离环境。

来源检查的限制：网页工具能读取不代表定时脚本也能读取。Vancouver 本地 HTTP 403、BC 新闻本地证书验证失败、Shape Your City 项目内容提取未通过，均已登记。

## 三、接下来按顺序完成

1. 开通／验证 Google Cloud TTS：项目→计费→启用 TTS→调用身份→短中文音频测试。
2. 准备 DeepSeek 和 Gemini 专用 Key，确认模型／配额／余额；存入私密凭证位置。
3. 连接 GitHub 私有仓库，并把 DeepSeek、Gemini、Google TTS、Cloudflare、Healthchecks 凭证放入 GitHub Actions Secrets。
4. 创建项目隔离依赖环境，完成基础连通测试与来源获取检查；不跑目标日新闻。
5. 补域名、Brevo、监测和合规联系信息，并验证选定必需准备项。
6. 完成全部准备验证后才执行任务二。每个验证留日期和结果，但不保留秘密值。

上列步骤是后续操作清单，未在本轮声称已完成或自动安排后台执行。

## 四、任务二执行锁定

- 状态：**NOT_STARTED / WAITING_FOR_SECRET_PLACEMENT_AND_CONNECTIVITY_TESTS**。
- 固定新闻日期：2026-09-13。
- 参考新闻窗口：2026-09-13T00:00:00-07:00 至 2026-09-14T00:00:00-07:00（结束不含）。
- 参考发布时点：2026-09-14 06:00，America/Vancouver。
- 账号与 Key 创建已由用户确认；仍未完成 Secret 放置、实际 API 小调用、Cloudflare 测试资源、来源采集连通和监测接入。
- 仅有本文档或 `.env.example` 不构成一条已可运行的流水线。
- 未调用真实 LLM／TTS，未生成该日新闻，未部署，未发送邮件。
