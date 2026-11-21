// The parts of the cowbell mod that are plain data in, data out: no `herdr`
// import, so tests run them under node. main.js wires them to the app.

// What the core picker reads when it runs inside a Herdr desktop popup.
// When these are set, the picker starts the agent in a new tab of that space
// (through herdr-core) instead of replacing itself, then exits.
export const CONTRACT = {
  space: "COWBELL_HC_SPACE", // the space's id on its own machine (no "machine/" prefix)
  cwd: "COWBELL_HC_CWD", // the folder the picker lists and launches in
};

// The label that marks a picker terminal as this mod's own. The popup only ever
// kills a terminal carrying this label with the value it chose itself.
export const OWNER_LABEL = "cowbell.popup";

// The popup's size, as shares of the window (the herdr TUI plugin uses 64% x 56%).
export const SIZE = { width: 0.64, height: 0.56 };

// "eco/sp12" -> "sp12"; "sp12" -> "sp12".
export function localId(id) {
  if (typeof id !== "string") return null;
  const i = id.lastIndexOf("/");
  return i === -1 ? id : id.slice(i + 1);
}

// Where to open the picker, from the app's live data.
//   focused: focused() ; spaces: spaces() ; home: home()
// Returns { ok: true, space, cwd } for a space on this computer,
// { ok: false, reason: "remote", machine } for a space on another machine,
// { ok: false, reason: "no-space" } when nothing is focused.
export function target(focused, spaces, home) {
  if (!focused || !focused.space) return { ok: false, reason: "no-space" };
  const space = (spaces || []).find((s) => s.id === focused.space) || null;
  const machine = space ? space.machine : null;
  const prefixed = focused.space.includes("/");
  if (machine || prefixed) {
    return { ok: false, reason: "remote", machine: machine || focused.space.split("/")[0] };
  }
  const cwd = nonEmpty(focused.cwd) || nonEmpty(space && space.folder) || home;
  return { ok: true, space: localId(focused.space), cwd };
}

function nonEmpty(s) {
  return typeof s === "string" && s.trim() !== "" ? s : null;
}

// A short token for this one popup: names the terminal and marks it as ours.
export function nonce(now, rand) {
  const a = Math.floor(now).toString(36);
  const b = Math.floor(rand * 36 ** 6).toString(36).padStart(6, "0");
  return `${a}${b}`.slice(-14);
}

// hc terminal names: 1-32 of a-z 0-9 - _
export function terminalName(token) {
  return `cowbell-${token}`.toLowerCase().replace(/[^a-z0-9_-]/g, "").slice(0, 32);
}

// The program the hidden terminal runs. `env` sets the contract variables
// without a shell, so no folder name is ever parsed as shell text.
export function pickerArgv(cowbell, space, cwd) {
  return ["/usr/bin/env", `${CONTRACT.space}=${space}`, `${CONTRACT.cwd}=${cwd}`, cowbell, "picker"];
}

// Arguments to `hc` that start the picker, hidden (no herdr.space/herdr.tab
// labels), removed once it exits, with no parent to notify.
export function hcRunArgs({ cwd, token, argv }) {
  return [
    "run", "--bg", "--json", "--rm", "--no-parent", "--no-notify",
    "--name", terminalName(token),
    "--cwd", cwd,
    "--label", `${OWNER_LABEL}=${token}`,
    "--", ...argv,
  ];
}

// The terminal's address from `hc run --bg --json` output, or null.
export function parseAddress(stdout) {
  const text = (stdout || "").trim();
  if (!text) return null;
  try {
    const j = JSON.parse(text);
    const a = j && (j.address || j.terminal || (j.terminals && j.terminals[0] && j.terminals[0].address));
    if (typeof a === "string" && a) return a;
  } catch {
    // not JSON: fall through to the plain form
  }
  const m = text.match(/\b(?:[a-z0-9_.-]+\/)?t\d+\b/i);
  return m ? m[0] : null;
}

// True only for a live terminal at `address` that carries our label with `token`.
// `terminals` is terminals() from the app (each with address, state, labels).
export function ownedLive(terminals, address, token) {
  if (!address || !token) return false;
  const t = (terminals || []).find((x) => x && x.address === address);
  if (!t) return false;
  if (t.state && t.state !== "live") return false;
  return Boolean(t.labels) && t.labels[OWNER_LABEL] === token;
}

// The overlay that shows the picker: a slim bar (this mod, role "bar") over the terminal.
export function overlaySpec(mod, { address, token, cwd }) {
  return {
    kind: "dialog",
    width: SIZE.width,
    height: SIZE.height,
    layout: {
      type: "column",
      children: [
        { type: "block", size: { px: 28 }, content: { mod, props: { role: "bar", terminal: address, token, cwd } } },
        { type: "block", content: { terminal: address } },
      ],
    },
  };
}

// Which cowbell to run. One /bin/sh call, argument $1 = this mod's folder as
// Herdr sees it (appHome()/mods/<name>). Prints "<source>\t<path>".
//   repo: the mod folder is a link into a herdr-cowbell checkout; run its
//         bin/cowbell, which itself prefers a newer `cowbell` on PATH and
//         otherwise runs the pinned copy in vendor/.
//   path: a `cowbell` on the person's PATH (the mod folder was copied).
// Exits 1 when there is neither.
export const FIND_COWBELL = [
  'd=$(cd -P "$1" 2>/dev/null && pwd -P) || d=""',
  'if [ -n "$d" ]; then',
  '  r=$(dirname "$(dirname "$d")")',
  '  if [ -x "$r/bin/cowbell" ] && [ -f "$r/herdr_cowbell.py" ]; then printf "repo\\t%s\\n" "$r/bin/cowbell"; exit 0; fi',
  "fi",
  'p=$(command -v cowbell 2>/dev/null) || p=""',
  'case "$p" in /*) printf "path\\t%s\\n" "$p"; exit 0 ;; esac',
  "exit 1",
].join("\n");

// "repo\t/x/bin/cowbell\n" -> { source: "repo", path: "/x/bin/cowbell" }, else null.
export function parseFound(stdout) {
  const line = (stdout || "").split("\n").find((l) => l.includes("\t"));
  if (!line) return null;
  const [source, path] = line.split("\t");
  if ((source !== "repo" && source !== "path") || !path || !path.startsWith("/")) return null;
  return { source, path };
}

// "~/code/acme" for a folder under home, else the folder.
export function tilde(folder, home) {
  if (!folder) return "";
  if (home && (folder === home || folder.startsWith(home + "/"))) return "~" + folder.slice(home.length);
  return folder;
}
