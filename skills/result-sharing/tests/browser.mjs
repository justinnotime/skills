import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm, mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const packageRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const temporary = await mkdtemp(path.join(os.tmpdir(), "result-browser-test-"));
const fixture = String.raw`
import importlib.util,sys,json,base64
from pathlib import Path
spec=importlib.util.spec_from_file_location('share',sys.argv[1]);s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
base=Path(sys.argv[2]);src=base/'source';src.mkdir(exist_ok=True)
(src/'reports').mkdir(exist_ok=True);(src/'nested').mkdir(exist_ok=True)
(src/'reports/earlier.md').write_text('# Earlier report\n\n| Item | Value |\n| --- | --- |\n| count | 2 |\n\n<script>window.injected=true</script>')
(src/'index.html').write_text('<h1>Interactive report</h1>')
(src/'nested/metrics.csv').write_text('name,value\nexample,3\n')
(src/'quote"&.txt').write_text('Quoted filename works')
(src/'note.txt').write_text('Another task')
(src/'image.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH7sAAAAASUVORK5CYII='))
cfg={'schema':s.SCHEMA,'root':str(base/'site'),'allowed_source_roots':[str(src)],'base_url':'http://localhost:8081'}
def put(project,title,entry,files):return s.publish(cfg,s.bundle(cfg,str(src),project,title,entry,files))
if sys.argv[3]=='initial':
 first=put('alpha','Earlier <img src=x onerror="window.injected=true">','reports/earlier.md',['reports/earlier.md'])
 put('alpha','Current report','index.html',['index.html','nested/metrics.csv','quote"&.txt','image.png'])
 put('beta','Second task','note.txt',['note.txt'])
 print(json.dumps(first))
else:put('gamma','New task','note.txt',['note.txt'])
`;
const runFixture = (mode) =>
  execFileSync(
    process.env.PYTHON || "python3",
    [
      "-B",
      "-c",
      fixture,
      path.join(packageRoot, "scripts/share.py"),
      temporary,
      mode,
    ],
    { encoding: "utf8" },
  );
const first = JSON.parse(runFixture("initial"));
const site = path.join(temporary, "site");
const types = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".png": "image/png",
};
const server = createServer(async (req, res) => {
  try {
    let name = decodeURIComponent(
      new URL(req.url, "http://localhost").pathname,
    );
    if (name.endsWith("/")) name += "index.html";
    const file = path.resolve(site, "." + name);
    if (!file.startsWith(site + path.sep)) throw Error("outside fixture");
    res.setHeader("Content-Type", types[path.extname(file)] || "text/plain");
    res.setHeader(
      "Content-Security-Policy",
      "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'self'",
    );
    res.end(await readFile(file));
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
    ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE }
    : {}),
});
const page = await browser.newPage({ viewport: { width: 1400, height: 940 } });
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
const url = `http://127.0.0.1:${server.address().port}/`;
try {
  await page.goto(url);
  await page.locator("#status").filter({ hasText: "2 个任务" }).waitFor();
  await page.locator("#projects").getByRole("link", { name: /alpha/ }).click();
  await page
    .locator("#files")
    .getByRole("link", { name: "reports", exact: true })
    .click();
  await page.getByRole("button", { name: "earlier.md", exact: true }).click();
  await page
    .locator("#preview-content h1")
    .filter({ hasText: "Earlier report" })
    .waitFor();
  assert.equal(await page.locator("#preview-content table tr").count(), 2);
  assert.equal(await page.evaluate(() => window.injected), undefined);
  assert.equal(
    await page.locator("#preview-content script,#preview-content img").count(),
    0,
  );
  await page.getByRole("button", { name: "关闭预览" }).click();
  await page
    .locator("#breadcrumbs")
    .getByRole("link", { name: "alpha", exact: true })
    .click();
  await page.locator("#search").fill('quote"&');
  await page.getByRole("button", { name: 'quote"&.txt', exact: true }).click();
  await page
    .locator("#preview-content pre")
    .filter({ hasText: "Quoted filename works" })
    .waitFor();
  await page.getByRole("button", { name: "关闭预览" }).click();
  await page.locator("#search").fill("");
  await page.locator("#version").selectOption(first.revision);
  await page
    .locator("#files")
    .getByRole("link", { name: "reports", exact: true })
    .waitFor();
  assert.equal(
    await page.getByRole("button", { name: "index.html", exact: true }).count(),
    0,
  );
  await page.locator("#version").selectOption("");
  await page.locator("#type").selectOption("image");
  await page.getByRole("button", { name: "image.png", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector("#preview-content img")?.naturalWidth === 1,
  );
  await page.getByRole("button", { name: "关闭预览" }).click();
  await page.locator("#type").selectOption("");
  await page.locator("#search").fill("no-such-file");
  assert.equal(await page.locator("#empty").isVisible(), true);
  await page.locator("#search").fill("");
  const opened = page.waitForEvent("popup");
  await page.getByRole("button", { name: "index.html", exact: true }).click();
  await page.locator("#preview-open").click();
  const report = await opened;
  await report.getByRole("heading", { name: "Interactive report" }).waitFor();
  await report.close();
  await page.getByRole("button", { name: "关闭预览" }).click();
  const downloaded = page.waitForEvent("download");
  await page
    .locator("#files tr")
    .filter({ hasText: 'quote"&.txt' })
    .getByRole("link", { name: "下载", exact: true })
    .click();
  assert.ok((await downloaded).suggestedFilename().includes("quote"));
  await page
    .locator("#projects")
    .getByRole("link", { name: /全部任务/ })
    .click();
  runFixture("new");
  await page.locator("#refresh").click();
  await page
    .locator("#projects")
    .getByRole("link", { name: /gamma/ })
    .waitFor();
  const artifacts = process.env.BROWSER_ARTIFACTS;
  if (artifacts) {
    await mkdir(artifacts, { recursive: true });
    await page.screenshot({
      path: path.join(artifacts, "desktop.png"),
      fullPage: true,
    });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  if (artifacts)
    await page.screenshot({
      path: path.join(artifacts, "mobile.png"),
      fullPage: true,
    });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    JSON.stringify(
      await page.evaluate(() =>
        [...document.querySelectorAll("body *")]
          .filter((e) => e.getBoundingClientRect().right > innerWidth)
          .slice(0, 12)
          .map((e) => ({
            tag: e.tagName,
            id: e.id,
            class: e.className,
            width: e.getBoundingClientRect().width,
            right: e.getBoundingClientRect().right,
          })),
      ),
    ),
  );
  await page.route("**/library.json", (route) => route.abort());
  await page.locator("#refresh").click();
  await page.locator("#status").filter({ hasText: "暂时不可用" }).waitFor();
  await page.unroute("**/library.json");
  await page.locator("#refresh").click();
  await page.locator("#status").filter({ hasText: "3 个任务" }).waitFor();
  await page.locator("#projects").getByRole("link", { name: /beta/ }).click();
  await page.locator("#open-entry").click();
  await page
    .locator("#preview-content pre")
    .filter({ hasText: "Another task" })
    .waitFor();
  assert.deepEqual(errors, []);
  console.log(
    "OK browser: folders, history, search, filters, safe previews, downloads, refresh, mobile and error recovery",
  );
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
  await rm(temporary, { recursive: true, force: true });
}
