#!/usr/bin/env python3
"""tmux-flash: flash.nvim-style incremental search-and-jump for tmux copy-mode.

Runs inside a replica pane swapped over the original. Owns the keyboard,
renders the captured screen with a dimmed backdrop, highlights matches as
the pattern grows, and assigns jump labels the way flash.nvim does:
labels that could also continue the search are skipped, so typing is never
ambiguous. Selecting a label swaps the original pane back and moves its
copy-mode cursor there — extending any active selection.

Mirrors flash.nvim defaults: exact-match mode, smartcase, labels shown from
the first typed character, label drawn after the match, jump to match start,
matches labeled in order of distance from the cursor.
"""
import argparse
import os
import select
import signal
import subprocess
import sys
import termios
import tty

# Every style begins with SGR 0 (full reset) so a style change never inherits
# attributes from the previous cell — otherwise a match's background color
# would bleed across the rest of the line.
BACKDROP = "\x1b[0;38;5;242m"               # dimmed text, like flash's backdrop
MATCH = "\x1b[0;38;5;231;48;5;24m"          # white on dark blue, like FlashMatch
CURRENT = "\x1b[0;38;5;231;48;5;29m"        # closest match, like FlashCurrent
LABEL = "\x1b[0;1;38;5;16;48;5;212m"        # black on pink, like FlashLabel
STATUS = "\x1b[0;38;5;250m"
STATUS_ERR = "\x1b[0;1;38;5;203m"
RESET = "\x1b[0m"
HIDE_CUR = "\x1b[?25l"
SHOW_CUR = "\x1b[?25h"


def tmux(*args):
    subprocess.run(["tmux", *args], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tmux_out(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return r.stdout.strip()


class Flash:
    def __init__(self, opts):
        self.orig = opts.orig
        self.me = os.environ.get("TMUX_PANE", "")
        self.cx, self.cy = opts.cx, opts.cy
        self.w, self.h = opts.width, opts.height
        self.alphabet = opts.labels
        with open(opts.capture) as f:
            self.lines = f.read().split("\n")
        os.unlink(opts.capture)
        # capture-pane may include a trailing empty line from the final newline
        while len(self.lines) > self.h:
            self.lines.pop()
        self.pattern = ""
        self.restored = False

    # --- matching (flash: mode="exact" with smartcase) -------------------

    def smartcase(self):
        return self.pattern == self.pattern.lower()

    def find_matches(self):
        if not self.pattern:
            return []
        ci = self.smartcase()
        needle = self.pattern.lower() if ci else self.pattern
        found = []
        for row, line in enumerate(self.lines):
            hay = line.lower() if ci else line
            i = hay.find(needle)
            while i >= 0:
                found.append((row, i, i + len(needle)))
                i = hay.find(needle, i + 1)
        # flash labels matches nearest-to-cursor first
        found.sort(key=lambda m: (abs(m[0] - self.cy), abs(m[1] - self.cx)))
        return found

    def assign_labels(self, matches):
        """Skip labels that could continue the search (flash's skip logic)."""
        ci = self.smartcase()
        nxt = set()
        for row, s, e in matches:
            line = self.lines[row]
            if e < len(line):
                c = line[e]
                nxt.add(c.lower() if ci else c)
        usable = [c for c in self.alphabet if c not in nxt]
        return {usable[i]: m for i, m in enumerate(matches[: len(usable)])}

    # --- rendering --------------------------------------------------------

    def render(self, matches, labels):
        by_pos = {}   # (row, col) -> (style, char) overlay
        for idx, (row, s, e) in enumerate(matches):
            style = CURRENT if idx == 0 else MATCH
            for col in range(s, e):
                by_pos[(row, col)] = (style, None)
        for ch, (row, s, e) in labels.items():
            by_pos[(row, e)] = (LABEL, ch)

        out = ["\x1b[H\x1b[2J", HIDE_CUR]
        rows = min(len(self.lines), self.h - 1)  # bottom row is the status line
        for row in range(rows):
            line = self.lines[row]
            # A label may sit one column past the end of the text (match at
            # end of line), so the drawn width can exceed the line length.
            width = max(len(line), 1 + max((c for r, c in by_pos if r == row), default=-1))
            cells = []
            prev_style = None
            for col in range(width):
                base = line[col] if col < len(line) else " "
                style, repl = by_pos.get((row, col), (BACKDROP, None))
                if style != prev_style:  # only emit SGR codes on style changes
                    cells.append(style)
                    prev_style = style
                cells.append(repl if repl is not None else base)
            out.append("\x1b[%d;1H" % (row + 1) + "".join(cells) + RESET)
        n = len(matches)
        style = STATUS if (n or not self.pattern) else STATUS_ERR
        counter = (" [%d]" % n) if self.pattern else ""
        out.append("\x1b[%d;1H%s flash> %s%s%s" % (self.h, style, self.pattern, counter, RESET))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # --- jump / restore ---------------------------------------------------

    def restore(self):
        """Swap the original pane back into its slot (idempotent — called on
        every exit path, including the finally block, so a crash can never
        leave the user staring at a dead replica)."""
        if not self.restored:
            self.restored = True
            tmux("swap-pane", "-s", self.me, "-t", self.orig)
            tmux("select-pane", "-t", self.orig)
            # swap-pane only repaints the region it thinks changed. Some
            # terminals (Windows Terminal) are left with stale cells, so force
            # every client on that session to redraw from the tmux grid.
            for client in tmux_out("list-clients", "-F", "#{client_name}",
                                   "-t", self.orig).split("\n"):
                if client:
                    tmux("refresh-client", "-t", client)

    def jump(self, row, col):
        """Move the original pane's copy-mode cursor to (row, col) of the
        visible viewport. Done with plain cursor motions rather than a
        goto command because motions extend an active selection — that is
        what makes v -> flash -> label behave like flash.nvim in visual mode.
        """
        self.restore()
        if tmux_out("display-message", "-p", "-t", self.orig, "#{pane_in_mode}") != "1":
            return  # copy-mode ended underneath us; nothing safe to do
        sk = ["send-keys", "-X", "-t", self.orig]
        # top-line goes to the top of the *visible* (scrolled) view, verified
        # on tmux 3.6b, so captured row/col map directly onto motions.
        tmux(*sk, "top-line")
        tmux(*sk, "start-of-line")
        if row > 0:
            tmux(*sk, "-N", str(row), "cursor-down")
            tmux(*sk, "start-of-line")
        if col > 0:
            tmux(*sk, "-N", str(col), "cursor-right")

    # --- input loop -------------------------------------------------------

    def run(self):
        """Raw-tty key loop. The replica is spawned at full window size and
        only takes the real pane's dimensions when swapped in, so we also
        watch SIGWINCH and re-render after the resize lands."""
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        winch = {"hit": False}
        signal.signal(signal.SIGWINCH, lambda *a: winch.update(hit=True))
        tty.setraw(fd)
        try:
            while True:
                matches = self.find_matches()
                labels = self.assign_labels(matches) if self.pattern else {}
                self.render(matches, labels)
                while True:
                    r, _, _ = select.select([fd], [], [], 0.25)
                    if r:
                        break
                    if winch["hit"]:
                        winch["hit"] = False
                        self.render(matches, labels)
                data = os.read(fd, 64)
                if not data:
                    return self.restore()
                if data == b"\x1b" or data == b"\x03":       # Esc / Ctrl-C
                    return self.restore()
                if data.startswith(b"\x1b"):                  # arrow keys etc.
                    continue
                if data in (b"\r", b"\n"):                    # Enter -> closest match
                    if matches:
                        row, s, _ = matches[0]
                        return self.jump(row, s)
                    return self.restore()
                if data in (b"\x7f", b"\x08"):                # Backspace
                    self.pattern = self.pattern[:-1]
                    continue
                try:
                    ch = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if len(ch) == 1 and ch in labels:
                    row, s, _ = labels[ch]
                    return self.jump(row, s)
                if ch.isprintable():
                    self.pattern += ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write(SHOW_CUR + RESET)
            sys.stdout.flush()
            self.restore()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--orig", required=True)
    p.add_argument("--capture", required=True)
    p.add_argument("--cx", type=int, default=0)
    p.add_argument("--cy", type=int, default=0)
    p.add_argument("--width", type=int, required=True)
    p.add_argument("--height", type=int, required=True)
    p.add_argument("--labels", default="asdfghjklqwertyuiopzxcvbnm")
    Flash(p.parse_args()).run()


if __name__ == "__main__":
    main()
