import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPageViewRecord,
  deviceFromUserAgent,
  isBotUserAgent,
  localDateText,
  routeToKey,
  sectionFromPath,
  shouldRecordPageView
} from "../src/worker.js";

test("routes privacy and topic pages to HTML objects", () => {
  assert.equal(routeToKey("/"), "site/index.html");
  assert.equal(routeToKey("/privacy"), "site/privacy.html");
  assert.equal(routeToKey("/topics/finance"), "site/topics/finance.html");
  assert.equal(routeToKey("/topics/finance.html"), "site/topics/finance.html");
});

test("records only HTML page views", () => {
  const htmlResponse = new Response("ok", { headers: { "content-type": "text/html; charset=utf-8" } });
  const cssResponse = new Response("ok", { headers: { "content-type": "text/css" } });

  assert.equal(
    shouldRecordPageView(new Request("https://example.com/topics/finance"), new URL("https://example.com/topics/finance"), "site/topics/finance.html", htmlResponse),
    true
  );
  assert.equal(
    shouldRecordPageView(new Request("https://example.com/base.css"), new URL("https://example.com/base.css"), "site/base.css", cssResponse),
    false
  );
  assert.equal(
    shouldRecordPageView(new Request("https://example.com/audio/2026-09-17/a.mp3"), new URL("https://example.com/audio/2026-09-17/a.mp3"), "site/audio/2026-09-17/a.mp3", htmlResponse),
    false
  );
  assert.equal(
    shouldRecordPageView(new Request("https://example.com/robots.txt"), new URL("https://example.com/robots.txt"), "site/robots.txt", htmlResponse),
    false
  );
});

test("derives section from page path", () => {
  assert.equal(sectionFromPath("/"), "home");
  assert.equal(sectionFromPath("/topics/local_vancouver"), "local_vancouver");
  assert.equal(sectionFromPath("/topics/finance.html"), "finance");
  assert.equal(sectionFromPath("/privacy"), "other");
});

test("derives device and bot fields without storing user agent", () => {
  assert.equal(deviceFromUserAgent("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"), "mobile");
  assert.equal(deviceFromUserAgent("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)"), "tablet");
  assert.equal(deviceFromUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), "desktop");
  assert.equal(isBotUserAgent("Googlebot/2.1"), true);
  assert.equal(isBotUserAgent("Mozilla/5.0 (Windows NT 10.0; Win64; x64)"), false);
});

test("builds anonymous page view record", () => {
  const request = new Request("https://infogap.example/topics/finance", {
    headers: {
      "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile",
      referer: "https://google.com/search?q=infogap"
    }
  });
  Object.defineProperty(request, "cf", {
    value: { city: "Vancouver", region: "British Columbia", country: "CA" }
  });

  const record = buildPageViewRecord(request, new URL(request.url), new Date("2026-09-17T20:00:00.000Z"));
  assert.equal(record.date_local, "2026-09-17");
  assert.equal(record.path, "/topics/finance");
  assert.equal(record.section, "finance");
  assert.equal(record.city, "Vancouver");
  assert.equal(record.region, "British Columbia");
  assert.equal(record.country, "CA");
  assert.equal(record.referrer_host, "google.com");
  assert.equal(record.device, "mobile");
  assert.equal(record.is_bot, 0);
});

test("drops same-site referrer host", () => {
  const request = new Request("https://infogap.example/topics/finance", {
    headers: { referer: "https://infogap.example/" }
  });
  const record = buildPageViewRecord(request, new URL(request.url), new Date("2026-09-17T20:00:00.000Z"));
  assert.equal(record.referrer_host, null);
});

test("formats local date in America/Vancouver", () => {
  assert.equal(localDateText(new Date("2026-09-17T06:30:00.000Z")), "2026-09-16");
  assert.equal(localDateText(new Date("2026-09-17T20:00:00.000Z")), "2026-09-17");
});
