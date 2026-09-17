const RETENTION_DAYS = 5;
const DEFAULT_KEY = "site/index.html";
const TRACKABLE_TOPICS = new Set([
  "finance",
  "technology",
  "living",
  "immigration",
  "local_vancouver"
]);
const STATIC_EXTENSIONS = new Set([
  ".css",
  ".js",
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".svg",
  ".webp",
  ".ico",
  ".mp3",
  ".wav",
  ".m4a",
  ".json",
  ".xml",
  ".txt",
  ".map",
  ".woff",
  ".woff2",
  ".ttf"
]);
const BOT_UA_RE = /(bot|crawler|spider|slurp|headless|preview|facebookexternalhit|twitterbot|linkedinbot|whatsapp|telegrambot|discordbot|bingpreview|pagespeed|lighthouse)/i;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const key = routeToKey(url.pathname);

    if (url.pathname === "/health") {
      return json({ ok: true, service: "daily-impact-news" });
    }

    if (isExpiredKey(key)) {
      const response = new Response("This article has expired.", { status: 410 });
      scheduleAnalytics(request, env, ctx, url, key, response);
      return response;
    }

    const object = await env.NEWS_BUCKET.get(key);
    if (!object && key !== DEFAULT_KEY) {
      const fallback = await env.NEWS_BUCKET.get(DEFAULT_KEY);
      const response = fallback ? objectResponse(fallback, "text/html; charset=utf-8") : setupResponse();
      scheduleAnalytics(request, env, ctx, url, DEFAULT_KEY, response);
      return response;
    }

    if (!object) {
      const response = setupResponse();
      scheduleAnalytics(request, env, ctx, url, DEFAULT_KEY, response);
      return response;
    }

    const response = objectResponse(object, contentTypeForKey(key));
    scheduleAnalytics(request, env, ctx, url, key, response);
    return response;
  }
};

export function routeToKey(pathname) {
  const cleanPath = decodeURIComponent(pathname).replace(/^\/+/, "");

  if (!cleanPath) return DEFAULT_KEY;
  if (cleanPath === "privacy") return "site/privacy.html";
  if (cleanPath === "data/index.json") return "site/data/index.json";
  if (cleanPath.startsWith("audio/")) return `site/${cleanPath}`;
  if (cleanPath.startsWith("articles/")) return cleanPath.endsWith(".html") ? `site/${cleanPath}` : `site/${cleanPath}.html`;
  if (cleanPath.startsWith("topics/")) return cleanPath.endsWith(".html") ? `site/${cleanPath}` : `site/${cleanPath}.html`;
  if (cleanPath.endsWith(".html") || cleanPath.endsWith(".json")) return `site/${cleanPath}`;

  return `site/${cleanPath}`;
}

function isExpiredKey(key) {
  const match = key.match(/site\/(?:articles|audio)\/(\d{4}-\d{2}-\d{2})\//);
  if (!match) return false;

  const vancouverDate = localDateText(new Date());
  const ageMs = Date.parse(`${vancouverDate}T00:00:00Z`) - Date.parse(`${match[1]}T00:00:00Z`);
  return ageMs >= RETENTION_DAYS * 24 * 60 * 60 * 1000;
}

function objectResponse(object, fallbackContentType) {
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("content-type", headers.get("content-type") || fallbackContentType);
  headers.set("cache-control", "public, max-age=300");
  return new Response(object.body, { headers });
}

function contentTypeForKey(key) {
  if (key.endsWith(".html")) return "text/html; charset=utf-8";
  if (key.endsWith(".json")) return "application/json; charset=utf-8";
  if (key.endsWith(".mp3")) return "audio/mpeg";
  if (key.endsWith(".css")) return "text/css; charset=utf-8";
  if (key.endsWith(".js")) return "text/javascript; charset=utf-8";
  return "application/octet-stream";
}

function scheduleAnalytics(request, env, ctx, url, key, response) {
  try {
    if (!shouldRecordPageView(request, url, key, response)) return;
    if (!env?.ANALYTICS_DB || !ctx?.waitUntil) return;
    ctx.waitUntil(recordPageView(request, env, url).catch(() => undefined));
  } catch {
    // Analytics must never affect page delivery.
  }
}

export function shouldRecordPageView(request, url, key, response) {
  if (request.method !== "GET") return false;
  if (url.pathname === "/health") return false;
  if (isStaticAssetPath(url.pathname)) return false;
  if (!isHtmlKey(key)) return false;
  const contentType = response.headers.get("content-type") || "";
  return contentType.toLowerCase().includes("text/html");
}

function isHtmlKey(key) {
  return key === DEFAULT_KEY || key.endsWith(".html");
}

function isStaticAssetPath(pathname) {
  const lowerPath = pathname.toLowerCase();
  if (lowerPath === "/favicon.ico" || lowerPath === "/robots.txt") return true;
  return [...STATIC_EXTENSIONS].some((extension) => lowerPath.endsWith(extension));
}

async function recordPageView(request, env, url) {
  try {
    const row = buildPageViewRecord(request, url);
    await env.ANALYTICS_DB.prepare(
      `INSERT INTO page_views (
        ts, date_local, path, section, city, region, country, referrer_host, device, is_bot
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(
        row.ts,
        row.date_local,
        row.path,
        row.section,
        row.city,
        row.region,
        row.country,
        row.referrer_host,
        row.device,
        row.is_bot
      )
      .run();
  } catch {
    // Analytics must be best-effort only.
  }
}

export function buildPageViewRecord(request, url, now = new Date()) {
  const userAgent = request.headers.get("user-agent") || "";
  return {
    ts: now.toISOString(),
    date_local: localDateText(now),
    path: normalizeAnalyticsPath(url.pathname),
    section: sectionFromPath(url.pathname),
    city: request.cf?.city || null,
    region: request.cf?.region || null,
    country: request.cf?.country || null,
    referrer_host: referrerHost(request, url),
    device: deviceFromUserAgent(userAgent),
    is_bot: isBotUserAgent(userAgent) ? 1 : 0
  };
}

function normalizeAnalyticsPath(pathname) {
  if (!pathname || pathname === "/") return "/";
  return pathname.replace(/\/$/, "") || "/";
}

export function sectionFromPath(pathname) {
  const cleanPath = pathname.replace(/^\/+|\/+$/g, "");
  if (!cleanPath || cleanPath === "index.html") return "home";

  const parts = cleanPath.split("/");
  if (parts[0] === "topics") {
    const topic = (parts[1] || "").replace(/\.html$/, "");
    return TRACKABLE_TOPICS.has(topic) ? topic : "other";
  }

  if (parts[0] === "articles") {
    const topic = (parts[2] || "").replace(/\.html$/, "");
    return TRACKABLE_TOPICS.has(topic) ? topic : "other";
  }

  return "other";
}

export function deviceFromUserAgent(userAgent) {
  const ua = userAgent || "";
  if (/ipad|tablet|kindle|silk|playbook|android(?!.*mobile)/i.test(ua)) return "tablet";
  if (/mobi|iphone|ipod|android.*mobile|blackberry|phone/i.test(ua)) return "mobile";
  return "desktop";
}

export function isBotUserAgent(userAgent) {
  return BOT_UA_RE.test(userAgent || "");
}

function referrerHost(request, url) {
  const referrer = request.headers.get("referer") || request.headers.get("referrer");
  if (!referrer) return null;
  try {
    const referrerUrl = new URL(referrer);
    return referrerUrl.hostname === url.hostname ? null : referrerUrl.hostname;
  } catch {
    return null;
  }
}

export function localDateText(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Vancouver",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function setupResponse() {
  return new Response(
    `<!doctype html>
<html lang="zh-Hans">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>信息差日报</title>
<body style="font-family: system-ui, sans-serif; max-width: 760px; margin: 56px auto; padding: 0 20px; line-height: 1.7;">
  <h1>信息差日报</h1>
  <p>Worker 已启动，R2 中还没有发布内容。下一步是运行 GitHub Actions 的 smoke test 和 daily pipeline。</p>
</body>
</html>`,
    { headers: { "content-type": "text/html; charset=utf-8" } }
  );
}

function json(data) {
  return new Response(JSON.stringify(data), {
    headers: { "content-type": "application/json; charset=utf-8" }
  });
}
