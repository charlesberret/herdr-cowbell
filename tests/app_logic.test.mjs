// node --test tests/app_logic.test.mjs  (run by tests/test_app_mod.py when node is installed)
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, symlinkSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  CONTRACT, OWNER_LABEL, FIND_COWBELL, cleanupAction, failureNotice, hcRunArgs, lastLines, localId,
  nonce, overlaySpec, owns, ownedLive, parseAddress, parseFound, parseGet, pickerArgv, shouldFocus,
  target, terminalName, tilde,
} from "../app/cowbell/logic.js";

test("the contract names live in one place", () => {
  assert.deepEqual(CONTRACT, { space: "COWBELL_HC_SPACE", cwd: "COWBELL_HC_CWD" });
});

test("localId strips a machine prefix", () => {
  assert.equal(localId("sp1"), "sp1");
  assert.equal(localId("vps/sp1"), "sp1");
  assert.equal(localId(null), null);
});

test("target: a local space uses the focused terminal's folder", () => {
  const r = target({ space: "sp1", cwd: "/w/acme" }, [{ id: "sp1", machine: null, folder: "/w" }], "/h");
  assert.deepEqual(r, { ok: true, space: "sp1", cwd: "/w/acme" });
});

test("target: falls back to the space's folder, then home", () => {
  const spaces = [{ id: "sp1", machine: null, folder: "/w" }];
  assert.equal(target({ space: "sp1", cwd: null }, spaces, "/h").cwd, "/w");
  assert.equal(target({ space: "sp1", cwd: "" }, [{ id: "sp1", machine: null, folder: "" }], "/h").cwd, "/h");
  assert.equal(target({ space: "sp1" }, [], "/h").cwd, "/h");
});

test("target: a space on another machine is refused, never launched here", () => {
  const r = target({ space: "vps/sp9", cwd: "/srv" }, [{ id: "vps/sp9", machine: "vps", folder: "" }], "/h");
  assert.deepEqual(r, { ok: false, reason: "remote", machine: "vps" });
  const r2 = target({ space: "vps/sp9" }, [], "/h");
  assert.equal(r2.ok, false);
  assert.equal(r2.machine, "vps");
});

test("target: nothing focused", () => {
  assert.deepEqual(target(null, [], "/h"), { ok: false, reason: "no-space" });
});

test("nonce and terminal name fit hc's name rule", () => {
  const n = nonce(1795287163937, 0.123456);
  assert.match(n, /^[a-z0-9]{1,14}$/);
  assert.match(terminalName(n), /^[a-z0-9_-]{1,32}$/);
  assert.notEqual(nonce(1, 0.1), nonce(1, 0.2));
});

test("pickerArgv passes the contract through env, not a shell", () => {
  const argv = pickerArgv("/opt/cb/bin/cowbell", "sp1", "/w/it's here");
  assert.deepEqual(argv, [
    "/usr/bin/env", "COWBELL_HC_SPACE=sp1", "COWBELL_HC_CWD=/w/it's here", "/opt/cb/bin/cowbell", "picker",
  ]);
});

test("hcRunArgs: hidden, kept after exit so its output can be read, labelled as ours", () => {
  const args = hcRunArgs({ cwd: "/w", token: "abc", argv: ["x", "picker"] });
  assert.deepEqual(args.slice(0, 5), ["run", "--bg", "--json", "--no-parent", "--no-notify"]);
  assert.ok(!args.includes("--rm"), "the popup removes the terminal itself, after reading it");
  assert.ok(!args.some((a) => a.startsWith("herdr.")), "no herdr.space/herdr.tab labels");
  assert.deepEqual(args.slice(args.indexOf("--label"), args.indexOf("--label") + 2), ["--label", `${OWNER_LABEL}=abc`]);
  assert.deepEqual(args.slice(args.indexOf("--cwd"), args.indexOf("--cwd") + 2), ["--cwd", "/w"]);
  assert.deepEqual(args.slice(args.indexOf("--") + 1), ["x", "picker"]);
});

test("parseAddress reads JSON or plain output", () => {
  assert.equal(parseAddress('{"address":"t12","name":"cowbell-x"}'), "t12");
  assert.equal(parseAddress('{"terminal":"t3"}'), "t3");
  assert.equal(parseAddress("t7\n"), "t7");
  assert.equal(parseAddress(""), null);
  assert.equal(parseAddress('{"error":"nope"}'), null);
});

test("ownedLive kills only our live, labelled terminal", () => {
  const ts = [
    { address: "t1", state: "live", labels: { [OWNER_LABEL]: "tok" } },
    { address: "t2", state: "live", labels: { [OWNER_LABEL]: "other" } },
    { address: "t3", state: "live", labels: {} },
    { address: "t4", state: "finished", labels: { [OWNER_LABEL]: "tok" } },
  ];
  assert.equal(ownedLive(ts, "t1", "tok"), true);
  assert.equal(ownedLive(ts, "t2", "tok"), false);
  assert.equal(ownedLive(ts, "t3", "tok"), false);
  assert.equal(ownedLive(ts, "t4", "tok"), false);
  assert.equal(ownedLive(ts, "t9", "tok"), false);
  assert.equal(ownedLive(ts, "t1", null), false);
});

test("overlaySpec: the bar over the terminal, at the plugin's size", () => {
  const s = overlaySpec("cowbell", { address: "t5", token: "k", cwd: "/w" });
  assert.equal(s.width, 0.64);
  assert.equal(s.height, 0.56);
  const [bar, term] = s.layout.children;
  assert.deepEqual(bar.content, { mod: "cowbell", props: { role: "bar", terminal: "t5", token: "k", cwd: "/w" } });
  assert.deepEqual(term.content, { terminal: "t5" });
});

test("tilde shortens home only on a path boundary", () => {
  assert.equal(tilde("/h/code", "/h"), "~/code");
  assert.equal(tilde("/h", "/h"), "~");
  assert.equal(tilde("/hx/code", "/h"), "/hx/code");
});

// FIND_COWBELL, run by /bin/sh against throwaway folders.
function find(modDir, path) {
  try {
    return execFileSync("/bin/sh", ["-c", FIND_COWBELL, "sh", modDir], { env: { PATH: path }, encoding: "utf8" });
  } catch (e) {
    return { status: e.status };
  }
}
function exe(file) {
  writeFileSync(file, "#!/bin/sh\n");
  chmodSync(file, 0o755);
}

test("find: a mod linked from a checkout runs the checkout's bin/cowbell", () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "cb-")));
  const repo = join(root, "repo");
  mkdirSync(join(repo, "bin"), { recursive: true });
  mkdirSync(join(repo, "app", "cowbell"), { recursive: true });
  exe(join(repo, "bin", "cowbell"));
  writeFileSync(join(repo, "herdr_cowbell.py"), "");
  mkdirSync(join(root, "mods"));
  symlinkSync(join(repo, "app", "cowbell"), join(root, "mods", "cowbell"));
  const out = find(join(root, "mods", "cowbell"), "/usr/bin:/bin");
  assert.deepEqual(parseFound(out), { source: "repo", path: join(repo, "bin", "cowbell") });
});

test("find: a copied mod falls back to cowbell on PATH, then to nothing", () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "cb-")));
  mkdirSync(join(root, "mods", "cowbell"), { recursive: true });
  mkdirSync(join(root, "pathbin"));
  exe(join(root, "pathbin", "cowbell"));
  const out = find(join(root, "mods", "cowbell"), `${join(root, "pathbin")}:/usr/bin:/bin`);
  assert.deepEqual(parseFound(out), { source: "path", path: join(root, "pathbin", "cowbell") });
  assert.deepEqual(find(join(root, "mods", "cowbell"), "/usr/bin:/bin"), { status: 1 });
});

test("parseFound rejects anything but an absolute path", () => {
  assert.equal(parseFound("path\tcowbell\n"), null);
  assert.equal(parseFound("other\t/x\n"), null);
  assert.equal(parseFound(""), null);
});

// Shapes below are as hc 0.1.21 printed them on 2026-11-21.
const RUN_OUT = '{"address":"t11","agent":"env","cwd":"/h","name":"cowbell-x","state":"live","turn":0}';
const failed = { address: "t11", state: "finished", exit_code: 1, signal: null, labels: { [OWNER_LABEL]: "tok" } };
const clean = { ...failed, exit_code: 0 };
const killed = { ...failed, exit_code: null, signal: 1, reason: "killed" };
const running = { ...failed, state: "live", exit_code: null };

test("parseAddress reads hc run's real output", () => {
  assert.equal(parseAddress(RUN_OUT), "t11");
});

test("parseGet reads hc get, and rejects an error or junk", () => {
  assert.equal(parseGet(JSON.stringify(failed)).exit_code, 1);
  assert.equal(parseGet('{"error":{"kind":"not_found","message":"no terminal \\"t11\\""}}'), null);
  assert.equal(parseGet(""), null);
  assert.equal(parseGet("t11"), null);
});

test("owns needs this popup's token on the terminal", () => {
  assert.equal(owns(failed, "tok"), true);
  assert.equal(owns(failed, "other"), false);
  assert.equal(owns({ ...failed, labels: {} }, "tok"), false);
  assert.equal(owns(null, "tok"), false);
  assert.equal(owns(failed, null), false);
});

test("cleanupAction: kill if running, report a failure, stay silent on a clean exit", () => {
  assert.equal(cleanupAction(running, "tok"), "kill");
  assert.equal(cleanupAction(failed, "tok"), "report");
  assert.equal(cleanupAction(killed, "tok"), "report");
  assert.equal(cleanupAction(clean, "tok"), "remove");
});

test("cleanupAction never touches a terminal that is not ours", () => {
  for (const info of [running, failed, clean]) {
    assert.equal(cleanupAction({ ...info, labels: { [OWNER_LABEL]: "someone-else" } }, "tok"), "none");
    assert.equal(cleanupAction({ ...info, labels: {} }, "tok"), "none");
  }
  assert.equal(cleanupAction(null, "tok"), "none");
});

test("lastLines keeps the last non-blank lines, without colour codes", () => {
  const text = "one\n\n\x1b[31mtwo\x1b[0m   \nthree\n\n\n";
  assert.equal(lastLines(text, 2), "two\nthree");
  assert.equal(lastLines(text), "one\ntwo\nthree");
  assert.equal(lastLines(""), "");
});

test("failureNotice carries the code and what the picker printed", () => {
  const text = "\ncowbell: session discovery as 'guest' failed: launch as guest refused; refusing\n\n";
  const n = failureNotice(failed, text);
  assert.equal(n.level, "error");
  assert.equal(n.title, "cowbell exited with code 1");
  assert.equal(n.body, "cowbell: session discovery as 'guest' failed: launch as guest refused; refusing");
  assert.equal(failureNotice(killed, "").title, "cowbell was stopped (signal 1)");
  assert.equal(failureNotice(failed, "  \n").body, "It printed nothing before it ended.");
});

test("shouldFocus: only a live picker of ours that lacks the keyboard", () => {
  const ts = [{ address: "t11", state: "live", labels: { [OWNER_LABEL]: "tok" } }];
  assert.equal(shouldFocus(ts, "t11", "tok", "t6"), true);
  assert.equal(shouldFocus(ts, "t11", "tok", "t11"), false, "already focused");
  assert.equal(shouldFocus([], "t11", "tok", "t6"), false, "gone: no 'no such pane' toast");
  assert.equal(shouldFocus([{ ...ts[0], state: "finished" }], "t11", "tok", "t6"), false, "ended");
  assert.equal(shouldFocus(ts, "t11", "other", "t6"), false);
});
