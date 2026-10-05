// Exercise the real settings event handlers with a delayed HTTP response.
// Only the DOM and HTTP transport are substituted; no browser dependency.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const script = fs.readFileSync(path.join(__dirname, "../webui/static/app.js"), "utf8");

function fixture(enabled = false) {
  const fields = {};
  const card = {
    dataset: { app: "radarr" },
    querySelector: (selector) => fields[selector] || null,
    querySelectorAll: (selector) => selector === ".f-url, .f-api_key"
      ? [fields[".f-url"], fields[".f-api_key"]] : [],
  };
  for (const selector of [".f-enabled", ".f-url", ".f-api_key", ".test-btn", ".test-result", ".status-dot", ".app-card-body"]) {
    fields[selector] = Object.assign(new EventTarget(), {
      value: "", dataset: {}, closest: () => card,
      classList: { toggle() {} },
    });
  }
  const checkbox = fields[".f-enabled"];
  checkbox.checked = enabled;
  fields[".f-url"].value = "http://radarr.example";
  fields[".f-api_key"].value = "fixture-key";
  let respond;
  const requests = [];
  const response = new Promise((resolve) => { respond = resolve; });
  const context = vm.createContext({
    document: {
      body: { dataset: { authDisabled: "true" } },
      addEventListener() {},
      getElementById: () => new EventTarget(),
      querySelector: () => card,
      querySelectorAll: (selector) => {
        if (selector === ".f-enabled") return [checkbox];
        if (selector === ".test-btn") return [fields[".test-btn"]];
        return [];
      },
    },
    fetch: (url, options) => { requests.push({ url, options }); return response; },
  });
  vm.runInContext(script, context);
  vm.runInContext("initSettingsEvents()", context);
  const pending = vm.runInContext('testApp("radarr")', context);
  return {
    fields, checkbox, requests,
    toggle(value) {
      checkbox.checked = value;
      checkbox.dispatchEvent(new Event("change"));
    },
    async finish(ok = true) {
      respond({ status: 200, ok: true, json: async () => ({ ok, message: "Test result" }) });
      await pending;
      assert.equal(fields[".test-btn"].disabled, false);
      // Testing must not trigger discovery or save changes implicitly.
      assert.deepEqual(requests.map((request) => request.url), ["/api/test/radarr"]);
    },
  };
}

test("successful test enables an unchanged app", async () => {
  const ui = fixture();
  await ui.finish();
  assert.equal(ui.checkbox.checked, true);
  assert.match(ui.fields[".test-result"].textContent, /Enabled; save settings/);
});

test("delayed success preserves an OFF → ON → OFF edit", async () => {
  const ui = fixture();
  ui.toggle(true);
  ui.toggle(false);
  await ui.finish();
  assert.equal(ui.checkbox.checked, false);
  assert.match(ui.fields[".test-result"].textContent, /Settings changed/);
  assert.equal(ui.fields[".status-dot"].dataset.state, "idle");
});

test("delayed success preserves a manual switch-off", async () => {
  const ui = fixture(true);
  ui.toggle(false);
  await ui.finish();
  assert.equal(ui.checkbox.checked, false);
});

test("delayed success does not enable edited credentials", async () => {
  const ui = fixture();
  ui.fields[".f-api_key"].value = "changed-key";
  await ui.finish();
  assert.equal(ui.checkbox.checked, false);
});

for (const enabled of [false, true]) {
  test(`failed test preserves enabled=${enabled}`, async () => {
    const ui = fixture(enabled);
    await ui.finish(false);
    assert.equal(ui.checkbox.checked, enabled);
  });
}
