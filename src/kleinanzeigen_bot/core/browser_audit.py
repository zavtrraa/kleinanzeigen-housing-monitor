"""Local consistency diagnostics. No site probing and no fingerprint mutation."""

import json
import re
from datetime import UTC, datetime

LOCAL_URL = "http://127.0.0.1/browser-check"

AUDIT_HTML = r"""<!doctype html>
<meta charset="utf-8"><title>Kleinbot local browser check</title>
<h1>Local browser consistency check</h1>
<p>This page is supplied by request interception. No Kleinanzeigen request is made.</p>
<pre id="report"></pre>
<script>
(async () => {
  const main = {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    language: navigator.language,
    languages: Array.from(navigator.languages),
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory ?? null,
    webdriver: navigator.webdriver,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    pluginsCount: navigator.plugins.length,
    screen: {width:screen.width,height:screen.height,colorDepth:screen.colorDepth},
    window: {innerWidth,innerHeight,outerWidth,outerHeight,devicePixelRatio},
    userAgentData: navigator.userAgentData?.toJSON() ?? null
  };
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl");
  if (gl) {
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    main.webgl = {
      vendor: gl.getParameter(ext ? ext.UNMASKED_VENDOR_WEBGL : gl.VENDOR),
      renderer: gl.getParameter(ext ? ext.UNMASKED_RENDERER_WEBGL : gl.RENDERER)
    };
  } else main.webgl = null;
  const source = [
    "postMessage({userAgent:navigator.userAgent, platform:navigator.platform,",
    "language:navigator.language,languages:Array.from(navigator.languages),",
    "hardwareConcurrency:navigator.hardwareConcurrency,",
    "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone});"
  ].join("");
  const blobUrl = URL.createObjectURL(new Blob([source], {type:"text/javascript"}));
  const worker = new Worker(blobUrl);
  try {
    const data = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Worker timed out")), 5000);
      worker.onmessage = event => {clearTimeout(timer);resolve(event.data);};
      worker.onerror = () => {clearTimeout(timer);reject(new Error("Worker failed"));};
    });
    // Let the native window finish attaching before measuring geometry.
    for (let i=0; i<20 && (outerWidth===0 || outerHeight===0); i++) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    main.window = {innerWidth,innerHeight,outerWidth,outerHeight,devicePixelRatio};
    document.getElementById("report").textContent = JSON.stringify({main,worker:data});
  } catch (error) {
    document.getElementById("report").textContent = JSON.stringify({error:error.message});
  } finally {
    worker.terminate(); URL.revokeObjectURL(blobUrl);
  }
})();
</script>
"""


def assess_consistency(data: dict, headers: dict) -> dict[str, bool]:
    main, worker = data["main"], data["worker"]
    checks = {
        "worker_user_agent": main["userAgent"] == worker["userAgent"],
        "worker_platform": main["platform"] == worker["platform"],
        "worker_language": main["language"] == worker["language"],
        "worker_languages": main["languages"] == worker["languages"],
        "worker_cpu_count": main["hardwareConcurrency"] == worker["hardwareConcurrency"],
        "worker_timezone": main["timezone"] == worker["timezone"],
        "http_user_agent": headers.get("user-agent") == main["userAgent"],
    }
    if "accept-language" in headers:
        http_language = headers["accept-language"].split(",")[0].split(";")[0].strip()
        checks["http_language"] = http_language.casefold() == main["language"].casefold()
    geometry = main.get("window")
    if geometry:
        checks["native_window_dimensions"] = all(
            geometry[key] > 0 for key in ("innerWidth", "innerHeight", "outerWidth", "outerHeight")
        )
    user_agent_data = main.get("userAgentData")
    if user_agent_data and "sec-ch-ua-platform" in headers:
        checks["http_platform_hint"] = (
            headers["sec-ch-ua-platform"].strip('"') == user_agent_data["platform"]
        )
    if user_agent_data and "sec-ch-ua" in headers:
        http_brands = set(re.findall(r'"([^"]+)";v="([^"]+)"', headers["sec-ch-ua"]))
        page_brands = {(brand["brand"], brand["version"]) for brand in user_agent_data["brands"]}
        checks["http_browser_brand_hints"] = http_brands == page_brands
    version = re.search(r"(?:Chrome|Chromium)/(\d+)", main["userAgent"])
    brands = user_agent_data.get("brands", []) if user_agent_data else []
    chromium = next((brand for brand in brands if brand["brand"] == "Chromium"), None)
    if version and chromium:
        checks["user_agent_client_hint_version"] = version[1] == chromium["version"]
    return checks


async def collect_browser_report(context) -> dict:
    headers = {}
    blocked_external = []

    async def intercept(route):
        if route.request.url == LOCAL_URL:
            request_headers = await route.request.all_headers()
            for key in (
                "user-agent",
                "accept-language",
                "sec-ch-ua",
                "sec-ch-ua-platform",
                "sec-ch-ua-mobile",
            ):
                if key in request_headers:
                    headers[key] = request_headers[key]
            await route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=AUDIT_HTML,
            )
        else:
            blocked_external.append(route.request.resource_type)
            await route.abort()

    await context.route("**/*", intercept)
    page = await context.new_page()
    try:
        await page.bring_to_front()
        await page.goto(LOCAL_URL, wait_until="domcontentloaded")
        await page.wait_for_function(
            "document.getElementById('report')?.textContent.length > 0",
            timeout=10000,
        )
        data = json.loads(await page.locator("#report").inner_text())
        if "error" in data:
            raise RuntimeError("Local browser diagnostics failed")
        return {
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "local consistency only; Kleinanzeigen access not tested",
            "consistency": assess_consistency(data, headers),
            "automation_flag_exposed": data["main"]["webdriver"],
            "blocked_external_page_requests": len(blocked_external),
            "not_checked": (
                []
                if "accept-language" in headers
                else ["Accept-Language is not exposed before native network dispatch"]
            ),
            "headers": headers,
            **data,
        }
    finally:
        await page.close()
        await context.unroute("**/*", intercept)
