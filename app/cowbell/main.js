// cowbell in the Herdr desktop app: a popup over the space you are looking at,
// running the cowbell picker (Active / Resume / New Session) for the focused
// terminal's folder. Opened with `mod.open:cowbell` (bind it in keys.json).
//
// Two roles, one folder:
//   launcher  what mod.open shows for a moment: finds cowbell, starts the picker
//             in a hidden herdr-core terminal, opens the popup, closes itself.
//   bar       the strip on top of the popup (props.role === "bar"). When the
//             popup goes, for any reason, it ends that one picker terminal.
// The picker exits after a launch or Esc, and the popup closes with it.
// See logic.js for the environment the picker reads.

import { View, div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import {
  appHome, checking, closeOverlay, exec, focused, hc, home, notify,
  openOverlay, props, run, spaces, terminals, theme,
} from "herdr";
import {
  FIND_COWBELL, cleanupAction, failureNotice, hcRunArgs, nonce, overlaySpec,
  parseAddress, parseFound, parseGet, pickerArgv, shouldFocus, target, tilde,
} from "./logic.js";

// This mod's folder name under mods/. Change it if you install under another name.
const MOD = "cowbell";

export default class Cowbell extends View {
  init(_props, cx) {
    this.role = props()?.role === "bar" ? "bar" : "launcher";
    this.message = "Starting cowbell…";
    this.address = null;
    this.token = null;
    this.handedOff = false;
    if (checking()) return;
    if (this.role === "bar") {
      // Give the picker the keyboard once the popup is up, if it is still
      // running and does not have it already.
      cx.timer.after(60, () => {
        const p = props() ?? {};
        if (shouldFocus(terminals(), p.terminal, p.token, focused()?.terminal)) {
          run("pane.focus", { terminal: p.terminal });
        }
      });
      return;
    }
    cx.spawn(async (cx) => {
      try {
        await this.launch();
      } catch (e) {
        this.fail("cowbell could not start", String(e?.message ?? e));
      }
      cx.notify();
    });
  }

  fail(title, body) {
    notify({ title, body, level: "error" });
    closeOverlay(props()?.overlay);
  }

  async launch() {
    const where = target(focused(), spaces(), home());
    if (!where.ok) {
      if (where.reason === "remote") {
        notify({
          title: "cowbell runs on this computer only",
          body: `This space is on ${where.machine}. Starting agents on another machine from the popup is not supported yet.`,
          level: "warning",
        });
      } else {
        notify({ title: "cowbell needs a space", body: "Open or select a space, then try again.", level: "warning" });
      }
      closeOverlay(props()?.overlay);
      return;
    }

    const found = parseFound((await exec("/bin/sh", ["-c", FIND_COWBELL, "sh", `${appHome()}/mods/${MOD}`])).stdout);
    if (!found) {
      this.fail("cowbell not found", "Link this mod from a herdr-cowbell checkout, or put `cowbell` on your PATH.");
      return;
    }

    this.token = nonce(Date.now(), Math.random());
    const r = await hc(hcRunArgs({ cwd: where.cwd, token: this.token, argv: pickerArgv(found.path, where.space, where.cwd) }));
    if (r.code !== 0) {
      this.fail("cowbell could not start", (r.stderr || r.stdout || `hc exited ${r.code}`).trim());
      return;
    }
    this.address = parseAddress(r.stdout);
    if (!this.address) {
      this.fail("cowbell could not start", "herdr-core did not say which terminal it started.");
      return;
    }

    openOverlay(overlaySpec(MOD, { address: this.address, token: this.token, cwd: where.cwd }));
    this.handedOff = true;
    closeOverlay(props()?.overlay);
  }

  // When the popup goes, the bar settles its picker terminal; the launcher
  // settles one it started but never handed to a popup. Still running (the
  // popup closed first): kill it. Ended with a failure: show its last lines.
  // Then remove it. Only a terminal carrying this popup's own label is ever
  // killed or removed.
  async close(_cx) {
    if (checking()) return;
    const p = props() ?? {};
    const address = this.role === "bar" ? p.terminal : this.handedOff ? null : this.address;
    const token = this.role === "bar" ? p.token : this.token;
    if (!address) return;
    const info = parseGet((await hc(["get", address, "--json"])).stdout);
    const action = cleanupAction(info, token);
    if (action === "none") return;
    if (action === "kill") {
      await hc(["kill", address]);
    } else if (action === "report") {
      const text = (await hc(["read", address, "--lines", "40"])).stdout;
      notify(failureNotice(info, text));
    }
    await hc(["rm", address]);
  }

  render(_cx) {
    return this.role === "bar" ? this.renderBar() : this.renderLauncher();
  }

  renderLauncher() {
    const t = theme();
    return v_flex()
      .id("cowbell-launcher")
      .size_full()
      .items_center()
      .justify_center()
      .bg(t.popover)
      .text_size(13)
      .text_color(t.muted)
      .child(div().child(this.message));
  }

  renderBar() {
    const t = theme();
    const p = props() ?? {};
    return h_flex()
      .id("cowbell-bar")
      .size_full()
      .px(12)
      .gap(8)
      .items_center()
      .bg(t.surface)
      .border_b(1)
      .border_color(t.border)
      .text_size(12)
      .child(div().flex_none().font_semibold().text_color(t.foreground).child("cowbell"))
      .child(div().truncate().text_color(t.dim).child(tilde(p.cwd, home())))
      .child(div().flex_1())
      .child(
        div()
          .id("cowbell-close")
          .px(8)
          .py(2)
          .rounded(5)
          .text_color(t.muted)
          .cursor_pointer()
          .hover((s) => s.bg(t.hover).text_color(t.foreground))
          .on_click(() => closeOverlay(p.overlay))
          .child("✕"),
      );
  }
}
