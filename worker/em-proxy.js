/**
 * 东财转发代理（Cloudflare Worker）
 *
 * 用途：GitHub runner 的数据中心 IP 会被东财 WAF 整段掐断（TLS 中途 EOF /
 * 502），而 Cloudflare 的出口对东财是友好的。这个 Worker 把
 * `GET /em?url=<目标URL>` 中继到东财，让 runner 借道 Cloudflare 取数。
 *
 * 安全（务必配置 PROXY_TOKEN，否则 Worker 直接拒绝所有请求）：
 *   1. 共享密钥：请求须带 `x-proxy-token` 头（或 ?token= 查询参数），
 *      与 Worker Secret `PROXY_TOKEN` 一致才放行 —— 防止被当成公开代理刷流量；
 *   2. 域名白名单：目标只允许 eastmoney.com 及其子域。
 *
 * 部署：
 *   cd worker && wrangler secret put PROXY_TOKEN && wrangler deploy
 * 部署后在本仓库配两个 Secrets：
 *   EM_PROXY_URL   = https://<name>.<account>.workers.dev/em
 *   EM_PROXY_TOKEN = 与 PROXY_TOKEN 相同的值
 *
 * 也可以并入现有的 gh-actions-scheduler Worker：把 fetch 的 /em 分支抄过去，
 * 在 env 里加同一个 PROXY_TOKEN 即可。
 */

const ALLOWED_SUFFIX = "eastmoney.com";

// 只转发这几个请求头：东财 WAF 会看 UA/Referer，其余头（含 cookie）一律不带
const PASSTHROUGH_HEADERS = ["user-agent", "accept"];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname !== "/em") {
      return new Response("not found\n", { status: 404 });
    }
    if (request.method !== "GET") {
      return new Response("method not allowed\n", { status: 405 });
    }

    // 1) 共享密钥校验：未配置 PROXY_TOKEN 时一律 403（宁可不服务，不做开放代理）
    const token =
      request.headers.get("x-proxy-token") || url.searchParams.get("token") || "";
    if (!env.PROXY_TOKEN || token !== env.PROXY_TOKEN) {
      return new Response("forbidden\n", { status: 403 });
    }

    // 2) 目标校验：仅允许东财域名
    const target = url.searchParams.get("url");
    if (!target) {
      return new Response("missing url\n", { status: 400 });
    }
    let targetUrl;
    try {
      targetUrl = new URL(target);
    } catch {
      return new Response("bad url\n", { status: 400 });
    }
    if (targetUrl.protocol !== "https:" ||
        !(targetUrl.hostname === ALLOWED_SUFFIX ||
          targetUrl.hostname.endsWith("." + ALLOWED_SUFFIX))) {
      return new Response("host not allowed\n", { status: 403 });
    }

    // 3) 中继：带上调用方的 UA/Accept，固定 Referer 提高通过率
    const headers = new Headers();
    for (const name of PASSTHROUGH_HEADERS) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    headers.set("referer", "https://quote.eastmoney.com/");

    const upstream = await fetch(targetUrl.toString(), {
      method: "GET",
      headers,
      redirect: "follow",
      cf: { cacheTtl: 0, cacheEverything: false },  // 行情数据不能缓存
    });

    // fetch() 已解压响应体，但保留了 content-encoding 头；
    // 不删掉的话客户端会拿明文当 gzip 解，必然报错。
    const out = new Headers(upstream.headers);
    out.delete("content-encoding");
    out.delete("content-length");
    out.set("cache-control", "no-store");

    return new Response(upstream.body, {
      status: upstream.status,
      headers: out,
    });
  },
};
