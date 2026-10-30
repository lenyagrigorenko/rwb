"""Video proof: a headless browser logs into the generated admin, runs the demo scenario and records the screen."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .builder import run
from .gen.backend import admin_roles, login_of
from .ir import AppSpec
from .runtime import DEMO_PASSWORD

PLAYWRIGHT_VERSION = "1.64.1"
IMAGE = f"mcr.microsoft.com/playwright:v{PLAYWRIGHT_VERSION}-noble"

SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const web = process.env.WEB, login = process.env.LOGIN, password = process.env.PASSWORD;
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'ru-RU',
    recordVideo: { dir: '/out/video', size: { width: 1440, height: 900 } } });
  const page = await context.newPage();
  await page.goto(web + '/login');
  await page.getByLabel('Логин').fill(login);
  await page.getByLabel('Пароль').fill(password);
  await page.getByRole('button', { name: 'Войти' }).click();
  await page.waitForURL(/dashboard/, { timeout: 30000 });
  await page.waitForTimeout(3000);
  await page.screenshot({ path: '/out/dashboard.png' });
  await page.goto(web + '/demo');
  await page.waitForTimeout(2500);
  await page.getByRole('button', { name: 'Запустить' }).click();
  const started = Date.now();
  while (Date.now() - started < 240000) {
    await page.waitForTimeout(1500);
    const pending = await page.locator('.step-pending, .step-running').count();
    const total = await page.locator('.step').count();
    if (total > 0 && pending === 0) break;
  }
  await page.waitForTimeout(3000);
  await page.screenshot({ path: '/out/demo.png', fullPage: true });
  await page.goto(web + '/events');
  await page.waitForTimeout(3000);
  await page.screenshot({ path: '/out/events.png' });
  await context.close();
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


async def record_demo(spec: AppSpec, project: Path, web_port: int) -> tuple[bool, str]:
    """Writes docs/demo.webm and screenshots into the project; returns (ok, log)."""
    work = project / "docs" / "media"
    if work.exists():
        shutil.rmtree(work)
    (work / "video").mkdir(parents=True)
    (work / "record.js").write_text(SCRIPT, encoding="utf-8")
    if sys.platform == "darwin":
        net, web = [], f"http://host.docker.internal:{web_port}"
    else:
        net, web = ["--network", "host"], f"http://localhost:{web_port}"
    cmd = ["docker", "run", "--rm", *net, "-v", f"{work.resolve()}:/out", "-w", "/tmp/rec",
           "-e", f"WEB={web}", "-e", f"LOGIN={login_of(spec, admin_roles(spec)[0])}", "-e", f"PASSWORD={DEMO_PASSWORD}",
           IMAGE, "bash", "-c",
           f"npm init -y >/dev/null && npm i --silent playwright@{PLAYWRIGHT_VERSION} >/dev/null && cp /out/record.js . && node record.js"]
    code, out = await run(cmd, timeout=600)
    videos = sorted((work / "video").glob("*.webm"))
    if code == 0 and videos:
        videos[-1].rename(project / "docs" / "demo.webm")
    shutil.rmtree(work / "video", ignore_errors=True)
    (work / "record.js").unlink(missing_ok=True)
    return code == 0 and (project / "docs" / "demo.webm").exists(), out[-2000:]
