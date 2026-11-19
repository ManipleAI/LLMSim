// The console's look, checked in a real browser on every page, at desktop and phone width:
// the neutral Claude-grey page, white cards with neutral lines, flat 6px buttons, no leftover
// beige, text no smaller than 10.5px, nothing running off the side, no script errors.
// Run by tests/test_console_look.py, which starts the LLMSim and passes the folder for pictures.
//
//   node tests/ui/console_look.cjs '{"base": "http://127.0.0.1:…", "shots": "/some/folder"}'

const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const cfg = JSON.parse(process.argv[2]);
let passed = 0, failed = 0;
const BEIGE = ["rgb(239, 236, 230)", "rgb(245, 242, 236)", "rgb(231, 226, 217)", "rgb(237, 232, 224)"];

async function step(name, fn) {
  try { await fn(); passed++; console.log("  ok    " + name); }
  catch (e) { failed++; console.log("  FAIL  " + name + "\n        " + String(e.message || e).split("\n")[0].slice(0, 600)); }
}
const check = (c, what) => { if (!c) throw new Error(what); };

// With real data in the console: a scenario opened on its canvas, a run, a result.
async function deep(page) {
  const shot = n => page.screenshot({ path: path.join(cfg.shots, "deep-" + n + ".png") });
  await step("deep · scenarios list has rows", async () => {
    await page.evaluate(() => show("lab/scenarios")); await page.waitForTimeout(500);
    const rows = await page.$$eval("main tr", r => r.length);
    check(rows > 20, "only " + rows + " rows");
    await shot("scenarios");
  });
  await step("deep · a scenario opens on its canvas", async () => {
    await page.click("main .lab-rows tr:nth-child(3)"); await page.waitForTimeout(800);
    const n = await page.$$eval(".cvb", b => b.length);
    check(n >= 3, "canvas has " + n + " blocks");
    const bg = await page.$eval(".cv", e => getComputedStyle(e).backgroundColor);
    check(bg === "rgb(245, 245, 245)", "canvas is " + bg);
    await shot("canvas");
    await page.click(".cvb:nth-of-type(1)").catch(() => {}); await page.waitForTimeout(300);
    await shot("canvas-block");
  });
  for (const v of ["lab/results", "lab/testruns", "evidence"]) {
    await step("deep · " + v + " with data", async () => {
      await page.evaluate(x => show(x), v); await page.waitForTimeout(600);
      const rows = await page.$$eval("main tbody tr, main .rturn, main .sess-table tr", r => r.length);
      check(rows > 0, "nothing listed");
      await shot(v.replace("/", "-"));
    });
  }
}

(async () => {
  fs.mkdirSync(cfg.shots, { recursive: true });
  const browser = await chromium.launch();
  const sizes = { desktop: { width: 1400, height: 900 }, phone: { width: 390, height: 800 } };
  for (const [label, viewport] of Object.entries(sizes)) {
    const page = await (await browser.newContext({ viewport })).newPage();
    const errors = [];
    page.on("pageerror", e => errors.push(String(e.message)));
    await page.goto(cfg.base + "/ui#overview");
    await page.waitForSelector("nav button[data-view]");
    const views = await page.$$eval("nav button[data-view]", b => b.map(x => x.dataset.view));
    check(views.length >= 15, "the menu has only " + views.length + " pages");
    for (const v of views) {
      await step(label + " · " + v, async () => {
        await page.evaluate(x => show(x), v);
        await page.waitForTimeout(250);
        const r = await page.evaluate((beige) => {
          const vis = e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && getComputedStyle(e).visibility !== "hidden"; };
          const cs = e => getComputedStyle(e);
          const out = { page: cs(document.body).backgroundColor, bad: [], small: [] };
          const main = document.querySelector("main");
          out.overflow = main.scrollWidth - main.clientWidth;
          for (const c of document.querySelectorAll("main .card, main .ov-stat, main .home-space")) {
            if (!vis(c)) continue;
            const s = cs(c);
            if (s.boxShadow !== "none") out.bad.push("shadow on " + c.className);
            if (!c.classList.contains("warn") && !/229, 229, 229/.test(s.borderTopColor)) out.bad.push("card line " + s.borderTopColor);
          }
          for (const b of document.querySelectorAll("main button.btn")) {
            if (!vis(b)) continue;
            const s = cs(b);
            if (s.borderRadius !== "6px") out.bad.push("button radius " + s.borderRadius);
            if (!b.classList.contains("ghost") && s.backgroundColor !== "rgb(178, 58, 34)" && !b.disabled) out.bad.push("primary colour " + s.backgroundColor);
          }
          for (const e of document.querySelectorAll("main *")) {
            if (!vis(e)) continue;
            const bg = cs(e).backgroundColor;
            if (beige.includes(bg)) out.bad.push("beige on " + e.tagName + "." + e.className);
            if ([...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()) && parseFloat(cs(e).fontSize) < 10.5) out.small.push(e.className || e.tagName);
          }
          return out;
        }, BEIGE);
        check(r.page === "rgb(250, 250, 250)", "page colour is " + r.page);
        check(r.overflow <= 1, "the page runs " + r.overflow + "px off the side");
        check(!r.bad.length, r.bad.slice(0, 3).join("; "));
        check(!r.small.length, "text under 10.5px in " + [...new Set(r.small)].slice(0, 4).join(", "));
        await page.screenshot({ path: path.join(cfg.shots, label + "-" + v.replace("/", "-") + ".png") });
      });
    }
    if (cfg.deep && label === "desktop") await deep(page);
    await step(label + " · no script errors", async () => check(!errors.length, errors[0]));
  }
  await browser.close();
  console.log("\n" + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})();
