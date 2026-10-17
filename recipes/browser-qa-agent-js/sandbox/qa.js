// QA pass over one site: three viewports, load time, console errors, broken links, a screenshot each.
// Usage: node qa.js URL [HOST...]   (links are checked only on the URL's host and the extra HOSTs)
const fs = require("node:fs");
const { chromium, request } = require("@playwright/test");

const VIEWPORTS = { phone: [390, 844], tablet: [820, 1180], desktop: [1440, 900] };
const MAX_PAGES = 5;
const MAX_LINKS = 25;
const PAGE_BUDGET_MS = 420_000; // stops visiting pages in time to report well inside the caller's 10-minute limit
const OUT = "qa-out";

const [arg, ...extraHosts] = process.argv.slice(2);
const { href: start, origin, hostname } = new URL(arg); // href ends a bare host with "/", as links to it do
// Links to hosts the sandbox may not reach would only time out, so they are left unchecked.
const checkable = new Set([hostname, ...extraHosts]);

async function visit(context, url) {
  const page = await context.newPage();
  const errors = [];
  page.on("console", (msg) => msg.type() === "error" && errors.push(msg.text()));
  page.on("pageerror", (err) => errors.push(err.message));
  const began = Date.now();
  let status = 0;
  try {
    // Status comes from the first response, so a page whose assets stall still reports one.
    status = (await page.goto(url, { waitUntil: "commit", timeout: 15_000 }))?.status() ?? 0;
    await page.waitForLoadState("load", { timeout: 30_000 })
      .catch(() => errors.push("did not finish loading in 30 s: it may load from a host the sandbox cannot reach"));
  } catch (err) {
    errors.push(err.message.split("\n")[0]);
  }
  const ms = Date.now() - began;
  const links = await page.$$eval("a[href]", (as) => as.map((a) => a.href)).catch(() => []);
  return { page, row: { url, status, ms, errors }, links: links.map((l) => l.split("#")[0]) };
}

async function linkStatus(api, url) {
  try {
    return (await api.get(url, { timeout: 10_000 })).status();
  } catch {
    return 0; // unreachable
  }
}

const path = (url) => new URL(url).pathname + new URL(url).search;

function markdown(report) {
  const lines = [`# QA report: ${report.url}`, "", "| Viewport | Page | Status | Load | Console errors |", "|---|---|---|---|---|"];
  for (const p of report.pages) lines.push(`| ${p.viewport} | ${path(p.url)} | ${p.status || "failed"} | ${p.ms} ms | ${p.errors.length} |`);
  lines.push("", "## Console errors", "");
  const errors = new Set(report.pages.flatMap((p) => p.errors.map((e) => `- ${path(p.url)}: ${e}`)));
  lines.push(...(errors.size ? errors : ["None."]));
  const { checked, not_checked, broken } = report.links;
  lines.push("", `## Broken links (${broken.length} of ${checked} checked, ${not_checked} not checked)`, "");
  lines.push(...(broken.length ? broken.map((b) => `- ${b.url} (${b.status || "unreachable"})`) : ["None."]));
  return lines.join("\n") + "\n";
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const pages = [];
  const screenshots = [];
  const found = new Set();
  const targets = [start];
  const began = Date.now();
  for (const [viewport, [width, height]] of Object.entries(VIEWPORTS)) {
    const context = await browser.newContext({ viewport: { width, height } });
    for (const url of targets) {
      if (Date.now() - began > PAGE_BUDGET_MS) break;
      // The label goes out before the page loads: those bytes keep the exec stream from idling out.
      process.stdout.write(viewport.padEnd(8));
      const { page, row, links } = await visit(context, url);
      console.log(`${String(row.status || "failed").padEnd(7)}${String(row.ms).padStart(6)} ms  console errors: ${row.errors.length}  ${url}`);
      pages.push({ viewport, ...row });
      links.forEach((l) => found.add(l));
      if (url === start) {
        await page.screenshot({ path: `${OUT}/${viewport}.png`, fullPage: true, timeout: 15_000 })
          .then(() => screenshots.push(`${viewport}.png`), (err) => row.errors.push(`screenshot failed: ${err.message.split("\n")[0]}`));
        // Appending while the loop runs is deliberate: every viewport visits the same-site pages found here.
        for (const l of links) if (new URL(l).origin === origin && !targets.includes(l) && targets.length < MAX_PAGES) targets.push(l);
      }
      await page.close();
    }
    await context.close();
  }
  if (Date.now() - began > PAGE_BUDGET_MS) console.log("time limit reached: the remaining pages were skipped");
  await browser.close();

  const web = [...found].filter((l) => /^https?:/.test(l));
  const toCheck = web.filter((l) => checkable.has(new URL(l).hostname)).slice(0, MAX_LINKS);
  console.log(`links: checking ${toCheck.length}, not checking ${web.length - toCheck.length}...`);
  const api = await request.newContext();
  const statuses = await Promise.all(toCheck.map((l) => linkStatus(api, l)));
  await api.dispose();
  const broken = toCheck.map((url, i) => ({ url, status: statuses[i] })).filter((l) => l.status === 0 || l.status >= 400);

  const report = { url: start, pages, screenshots, links: { checked: toCheck.length, not_checked: web.length - toCheck.length, broken } };
  fs.writeFileSync(`${OUT}/report.json`, JSON.stringify(report, null, 2));
  fs.writeFileSync(`${OUT}/report.md`, markdown(report));
  console.log("done");
})().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
