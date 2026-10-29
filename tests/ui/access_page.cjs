// Settings › Sign-in and API key, driven in a real browser: what tests/test_access.py checks
// through the API, done here by clicking, so the page's own mistakes show (it once showed
// "[object Object]" as a made key). Run by tests/test_access_page.py, which starts the LLMSims.
//
//   node tests/ui/access_page.cjs '{"open": "http://127.0.0.1:…", "env": "http://127.0.0.1:…",
//                                   "envPassword": "…", "envKey": "…"}'

const { chromium } = require("playwright");

const cfg = JSON.parse(process.argv[2]);
const KEY_SHAPE = /^llmsim-[A-Za-z0-9_-]{40}$/;
let passed = 0, failed = 0;

function check(cond, what) {
  if (!cond) throw new Error(what);
}

async function step(name, fn) {
  try {
    await fn();
    passed++;
    console.log("  ok    " + name);
  } catch (e) {
    failed++;
    console.log("  FAIL  " + name + "\n        " + String(e.message || e).split("\n")[0].slice(0, 600));
  }
}

// From outside the browser, with no cookie: what a script sees.
async function status(base, path, headers) {
  return (await fetch(base + path, { headers: headers || {} })).status;
}
async function signInStatus(base, user, password) {
  const r = await fetch(base + "/login", { method: "POST", headers: { "content-type": "application/json" },
                                           body: JSON.stringify({ user, password }) });
  return r.status;
}

// One change on the page: it posts /api/access, reads it again, then draws. Wait for the read.
async function act(page, click) {
  await Promise.all([page.waitForResponse((r) => r.url().endsWith("/api/access") && r.request().method() === "GET"), click()]);
  await page.waitForTimeout(50);
}
const note = (page) => page.locator("#acc-note").innerText();
const button = (page, name) => page.getByRole("button", { name, exact: true });
const signingIn = (page) => page.evaluate(() => document.body.classList.contains("signing-in"));
const madeKey = async (page) => ((await page.locator("#acc-made").count()) ? page.locator("#acc-made").innerText() : null);
// The page asks before replacing or removing a key and before turning the sign-in off: say yes, and keep what it asked.
function confirmsInto(page, asked) {
  page.on("dialog", (d) => { asked.push(d.message()); d.accept(); });
}
// textContent, not innerText: a tag is drawn in capitals, and innerText reads it that way.
const shownText = (page) => page.locator("#access-body").textContent();
const noObjectShown = async (page) => check(!(await page.locator("#access-body").innerText()).includes("[object"),
                                            "the page shows a JavaScript object");

async function signInOnTheCard(page, user, password) {
  await page.fill("#si-user", user);
  await page.fill("#si-pw", password);
  await Promise.all([page.waitForEvent("load"), page.click("#si-go")]);
  await page.waitForSelector("#access-body .card");
}

async function fromOpen(browser) {
  const base = cfg.open, page = await browser.newPage(), errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const asked = [];
  confirmsInto(page, asked);
  let key = null;
  console.log("Settings, starting open");

  await step("open: the console has no sign-in in front of it", async () => {
    await page.goto(base + "/ui#access");
    await page.waitForSelector("#access-body .card");
    check(!(await signingIn(page)), "a sign-in card is drawn on an open LLMSim");
    check(await button(page, "Turn on the sign-in").isVisible(), "no Turn on the sign-in button");
    check(await page.locator("#who").isHidden(), "says somebody is signed in");
  });

  await step("two passwords that differ are refused on the page", async () => {
    await page.fill("#acc-user", "owner");
    await page.fill("#acc-pw", "first-password");
    await page.fill("#acc-pw2", "something-else");
    await button(page, "Turn on the sign-in").click();
    check((await note(page)).includes("not the same"), "note: " + (await note(page)));
    check((await status(base, "/api/rules")) === 200, "it was turned on anyway");
  });

  await step("set a password: the sign-in is on, and this browser stays signed in", async () => {
    check((await page.inputValue("#acc-pw")) === "first-password", "the refusal emptied what was typed");
    await page.fill("#acc-pw2", "first-password");
    await act(page, () => button(page, "Turn on the sign-in").click());
    check((await note(page)).startsWith("Saved"), "note: " + (await note(page)));
    check((await status(base, "/api/rules")) === 401, "/api is still open to a script");
    check((await page.locator("#who-name").innerText()) === "owner", "the menu does not say signed in as owner");
    check(await page.locator("#acc-current").isVisible(), "a change does not ask for the current password");
    check(await button(page, "Change the password").isVisible(), "no Change the password button");
  });

  await step("make an API key: shown once, and it works", async () => {
    await page.fill("#acc-current", "first-password");
    await act(page, () => button(page, "Make a key").click());
    key = await madeKey(page);
    check(KEY_SHAPE.test(key || ""), "made key: " + key);
    check((await status(base, "/api/rules", { "X-LLMSim-Key": key })) === 200, "the key is refused");
    check((await shownText(page)).includes("ends " + key.slice(-4)), "the key's end is not shown");
    await noObjectShown(page);
  });

  await step("change the password: the old one stops, the new one works, the key is untouched", async () => {
    await page.fill("#acc-current", "first-password");
    await page.fill("#acc-pw", "second-password");
    await page.fill("#acc-pw2", "second-password");
    await act(page, () => button(page, "Change the password").click());
    check((await note(page)).startsWith("Saved"), "note: " + (await note(page)));
    await noObjectShown(page);
    const shown = await madeKey(page);
    check(shown === null || shown === key, "the key box shows something else: " + shown);
    check((await signInStatus(base, "owner", "first-password")) === 401, "the old password still signs in");
    check((await signInStatus(base, "owner", "second-password")) === 200, "the new password does not");
    check((await status(base, "/api/rules", { "X-LLMSim-Key": key })) === 200, "the key stopped working");
    check(!(await signingIn(page)) && (await page.locator("#who-name").innerText()) === "owner", "this browser was signed out");
  });

  await step("a wrong current password changes nothing", async () => {
    await page.fill("#acc-current", "not-it");
    await page.fill("#acc-pw", "third-password");
    await page.fill("#acc-pw2", "third-password");
    await act(page, () => button(page, "Change the password").click());
    check((await note(page)).includes("current password"), "note: " + (await note(page)));
    check((await signInStatus(base, "owner", "second-password")) === 200, "the password changed anyway");
  });

  await step("make a new key: it replaces the old one at once", async () => {
    await page.fill("#acc-current", "second-password");
    await act(page, () => button(page, "Make a new key").click());
    check(asked.pop().startsWith("Make a new key?"), "it did not ask before replacing the key");
    const next = await madeKey(page);
    check(KEY_SHAPE.test(next || "") && next !== key, "new key: " + next);
    check((await status(base, "/api/rules", { "X-LLMSim-Key": key })) === 401, "the old key still works");
    check((await status(base, "/api/rules", { "X-LLMSim-Key": next })) === 200, "the new key does not");
    key = next;
  });

  await step("remove the key: refused from then on, and no key is shown", async () => {
    await page.fill("#acc-current", "second-password");
    await act(page, () => button(page, "Remove the key").click());
    check(asked.pop().startsWith("Remove the key?"), "it did not ask before removing the key");
    check((await note(page)).startsWith("Removed"), "note: " + (await note(page)));
    check((await madeKey(page)) === null, "a key is still shown");
    check((await status(base, "/api/rules", { "X-LLMSim-Key": key })) === 401, "the removed key still works");
    check(await button(page, "Make a key").isVisible(), "no Make a key button");
    await noObjectShown(page);
  });

  await step("sign out, then back in on the card, onto the same screen", async () => {
    await Promise.all([page.waitForEvent("load"), page.locator("#who button").click()]);
    check(await signingIn(page), "no sign-in card after signing out");
    await page.fill("#si-user", "owner");
    await page.fill("#si-pw", "first-password");
    await page.click("#si-go");
    await page.locator("#si-err").filter({ hasText: "do not match" }).waitFor();
    await signInOnTheCard(page, "owner", "second-password");
    check(!(await signingIn(page)), "still behind the card");
    check(new URL(page.url()).hash === "#access", "on " + page.url());
  });

  await step("turn off the sign-in: open again", async () => {
    await page.fill("#acc-current", "second-password");
    await act(page, () => button(page, "Turn off the sign-in").click());
    check(asked.pop().startsWith("Turn off the sign-in?"), "it did not ask before turning it off");
    check((await note(page)).startsWith("Turned off"), "note: " + (await note(page)));
    check((await status(base, "/api/rules")) === 200, "/api still needs a sign-in");
    check(await button(page, "Turn on the sign-in").isVisible(), "no Turn on the sign-in button");
  });

  await step("no errors on the page", async () => check(errors.length === 0, errors.join(" | ")));
  await page.close();
}

async function fromTheEnvironment(browser) {
  const base = cfg.env, page = await browser.newPage(), errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const asked = [];
  confirmsInto(page, asked);
  const P = cfg.envPassword, K = cfg.envKey;
  let key = null;
  console.log("Settings, started with LLMSIM_ADMIN_PASSWORD and LLMSIM_API_KEY");

  await step("the card asks for the environment's password", async () => {
    await page.goto(base + "/ui#access");
    check(await signingIn(page), "no sign-in card");
    await signInOnTheCard(page, "admin", P);
    const text = await shownText(page);
    check(text.includes("Now LLMSIM_ADMIN_PASSWORD"), "does not say the password is the environment's");
    check(text.includes("ends " + K.slice(-4)), "does not show the environment key's end");
    check((await button(page, "Remove the key").count()) === 0, "offers to remove the environment's key");
    check((await button(page, "Turn off the sign-in").count()) === 0, "offers to turn off what the environment set");
  });

  await step("change the password in Settings: it replaces the environment's", async () => {
    await page.fill("#acc-current", P);
    await page.fill("#acc-pw", "set-in-settings");
    await page.fill("#acc-pw2", "set-in-settings");
    await act(page, () => button(page, "Change the password").click());
    check((await note(page)).startsWith("Saved"), "note: " + (await note(page)));
    check((await shownText(page)).includes("Set here, replacing LLMSIM_ADMIN_PASSWORD"), "not said");
    check((await signInStatus(base, "admin", P)) === 401, "the environment's password still signs in");
    check((await signInStatus(base, "admin", "set-in-settings")) === 200, "the new one does not");
  });

  await step("make a key in Settings: it replaces the environment's", async () => {
    await page.fill("#acc-current", "set-in-settings");
    await act(page, () => button(page, "Make a new key").click());
    key = await madeKey(page);
    check(KEY_SHAPE.test(key || ""), "made key: " + key);
    check((await status(base, "/api/rules", { "X-LLMSim-Key": K })) === 401, "the environment's key still works");
    check((await status(base, "/api/rules", { "X-LLMSim-Key": key })) === 200, "the made key does not");
  });

  await step("remove that key: the environment's works again", async () => {
    await page.fill("#acc-current", "set-in-settings");
    await act(page, () => button(page, "Remove the key").click());
    check((await note(page)).includes("LLMSIM_API_KEY works again"), "note: " + (await note(page)));
    check((await status(base, "/api/rules", { "X-LLMSim-Key": K })) === 200, "the environment's key is refused");
    check((await status(base, "/api/rules", { "X-LLMSim-Key": key })) === 401, "the removed key still works");
    await noObjectShown(page);
  });

  await step("no errors on the page", async () => check(errors.length === 0, errors.join(" | ")));
  await page.close();
}

(async () => {
  const browser = await chromium.launch();
  try {
    await fromOpen(browser);
    await fromTheEnvironment(browser);
  } finally {
    await browser.close();
  }
  console.log("\n%d passed, %d failed", passed, failed);
  process.exit(failed ? 1 : 0);
})().catch((e) => {
  console.log("  FAIL  the browser could not be driven\n        " + e.message.split("\n")[0]);
  console.log("\n%d passed, %d failed", passed, failed + 1);
  process.exit(1);
});
