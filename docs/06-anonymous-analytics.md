# InfoGap 匿名访问统计设置说明

本功能用于回答两个问题：读者大概来自哪里，尤其是大温地区占比；哪些 Section 和页面真的被浏览。统计在 Cloudflare Worker 里完成，不使用 cookie，不存 IP，不存完整 User-Agent，不引入第三方脚本。

## 已实现内容

- Worker 只统计 HTML 页面请求。
- CSS、JS、图片、音频、favicon、robots.txt、JSON 等静态资源不计入浏览。
- D1 写入通过 `ctx.waitUntil()` 异步执行，不阻塞页面返回。
- 写入逻辑有 `try/catch`，D1 出错不会影响页面 200 返回。
- 记录字段：UTC 时间、本地日期、页面路径、Section、城市、地区、国家、来源 host、设备类型、bot 标记。
- 新增 `/privacy` 页面和页脚链接。
- 新增查询脚本 `scripts/analytics_report.py`，可输出城市分布、Section 排名、每日趋势和大温地区占比。

## Cloudflare D1 设置步骤

在本地或 GitHub Actions 可用的 Cloudflare 登录环境里执行：

```bash
npx wrangler d1 create infogap-analytics
```

Wrangler 会返回一段类似下面的配置：

```toml
[[d1_databases]]
binding = "ANALYTICS_DB"
database_name = "infogap-analytics"
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

把返回的 `database_id` 复制到 `wrangler.toml`，替换：

```toml
database_id = "REPLACE_WITH_D1_DATABASE_ID"
```

然后建表：

```bash
npx wrangler d1 execute infogap-analytics --remote --file migrations/0001_analytics.sql
```

建完表后再部署 Worker：

```bash
npx wrangler deploy
```

如果用 GitHub Actions 发布，确认仓库里的 `wrangler.toml` 已经有真实 `database_id`，否则 deploy 会失败。


## Cloudflare Web Analytics

D1 负责城市级和 Section 自定义查询。Cloudflare Web Analytics 负责现成面板，包括页面浏览量、来源、设备和 Core Web Vitals。

启用路径通常是 Cloudflare Dashboard → Analytics & Logs → Web Analytics → Add a site。优先选择 Cloudflare 自动接入。如果 Cloudflare 要求手动接入，再把它提供的官方 beacon 脚本加入模板。当前代码没有加入第三方脚本，符合“先不引入额外脚本”的隐私约束。

## 查询访问数据

近 30 天报表：

```bash
python scripts/analytics_report.py
```

或者：

```bash
npm run analytics
```

默认查远程 D1。如果要查本地 Wrangler D1：

```bash
python scripts/analytics_report.py --local
```

## 验证标准

1. 部署后访问首页和任意 Section 页，D1 的 `page_views` 应出现记录。
2. 通过真实 Cloudflare 部署访问时，`city` 通常会有值。Cloudflare 后台 Playground 或本地 dev 环境可能没有 `request.cf.city`。
3. 访问 `/base.css` 或任意 `.mp3` 文件，不应新增浏览记录。
4. 临时把 D1 绑定改错再访问页面，页面仍应正常返回，不应因为统计报错。
5. `python scripts/analytics_report.py` 能打印三张表和“大温地区占比”。

## 大温地区口径

查询脚本把以下城市计入大温地区：Vancouver、Burnaby、Richmond、Surrey、Coquitlam、Port Coquitlam、Port Moody、New Westminster、North Vancouver、West Vancouver、Delta、Langley、Maple Ridge、Pitt Meadows、White Rock、Bowen Island、Lions Bay、Anmore、Belcarra。

## 不在本次范围

- 不统计独立访客，只统计页面浏览次数。
- 不统计音频播放率，因为音频从 R2 直接提供，当前 Worker 看不到播放行为。
- 不涉及邮箱订阅和用户系统。
