"""Record the required interruption/resume demonstration as a real terminal session.

    python tools/record_resume_demo.py --run full --input data/raw/spotify_reviews_18months.csv [--workers 8]

What it does (all output captured byte-for-byte with real timestamps, asciinema v2 format):
  1. `python -m pipeline status`  - saved state before
  2. starts `python -m pipeline run ... --stages enrich` in a pseudo-terminal
  3. as soon as the first Message Batch is collected (completed records saved), sends SIGINT - exactly what
     Ctrl-C sends - so the run stops admitting work and leaves its open batches recorded
  4. `python -m pipeline status` + the checkpoint snapshot counts - saved state after the interruption
  5. reruns the identical command - the resume collects the open batches (no resubmission), skips completed
     IDs and continues to the end of enrichment
  6. `python -m pipeline status`  - final state

Outputs (runs/<run>/recording/): session.cast (asciinema v2; `asciinema play session.cast` if installed),
replay.html (self-contained player, idle gaps compressed), demo_summary.json. No API keys are ever printed:
only pipeline logs are captured, and the recorder scrubs anything that looks like a key as a safeguard.
"""
import argparse
import json
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_RE = re.compile(rb"(sk-ant-[A-Za-z0-9_\-]{8,}|postgres(?:ql)?://\S+@\S+|tsk_[A-Za-z0-9]{8,})")


class Recorder:
    def __init__(self, path, title):
        self.f = open(path, "w", encoding="utf-8")
        self.t0 = time.time()
        self.f.write(json.dumps({"version": 2, "width": 160, "height": 40, "timestamp": int(self.t0), "title": title,
                                 "env": {"SHELL": "/bin/zsh", "TERM": "xterm-256color"}}) + "\n")

    def out(self, data: bytes):
        data = KEY_RE.sub(b"[REDACTED]", data)
        txt = data.decode("utf-8", "replace").replace("\n", "\r\n")
        self.f.write(json.dumps([round(time.time() - self.t0, 3), "o", txt]) + "\n")
        self.f.flush()
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()

    def say(self, text):
        self.out(f"\n\x1b[1;36m# {text}\x1b[0m\n".encode())

    def prompt(self, cmd):
        self.out(f"\x1b[1;32m$\x1b[0m {cmd}\n".encode())


def run_pty(rec, argv, interrupt_when=None, env=None):
    """Run argv in a pseudo-terminal, streaming output into the recording. If interrupt_when(line) becomes true,
    send SIGINT once. Returns (exit code, interrupted_at)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.execvpe(argv[0], argv, env or os.environ)
    buf, sent = b"", None
    while True:
        r, _, _ = select.select([fd], [], [], 1.0)
        if r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            rec.out(data)
            buf = (buf + data)[-20000:]
            if interrupt_when and sent is None and interrupt_when(buf.decode("utf-8", "replace")):
                time.sleep(1.0)
                rec.out(b"^C\n")
                os.kill(pid, signal.SIGINT)
                sent = time.time() - rec.t0
        else:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return os.waitstatus_to_exitcode(status), sent
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), sent


def checkpoint_counts(run):
    d = ROOT / "runs" / run / "checkpoints"
    out = {}
    for p in sorted(d.glob("inv*_*.json"), key=lambda p: p.stat().st_mtime):
        j = json.loads(p.read_text())
        out[p.stem] = {"taken_at": j["taken_at"], "completed": j["completed"], "pending": j["pending"],
                       "quarantined": j["quarantined"]}
    return out


PLAYER = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resume demo recording</title><style>
:root{--bg:#0f1115;--fg:#e6e6e6;--mute:#9aa0a6;--acc:#1db954}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-monospace,Menlo,monospace}
header{padding:12px 16px;border-bottom:1px solid #222;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
button,select{background:#1a1d23;color:var(--fg);border:1px solid #333;border-radius:6px;padding:6px 10px;font:inherit}
#t{color:var(--mute)}pre{margin:0;padding:12px 16px;white-space:pre-wrap;word-break:break-word}
.k{color:var(--acc)}</style></head><body>
<header><b>Interrupt / resume demo</b><button id="play">Play</button><button id="end">Show all</button>
<label>speed <select id="sp"><option>1</option><option selected>4</option><option>16</option></select></label>
<span id="t"></span><span id="meta"></span></header><pre id="o"></pre>
<script>
const CAST = __CAST__; const IDLE = 1.5;
const head = CAST[0], ev = CAST.slice(1).filter(e => e[1] === "o");
document.getElementById("meta").textContent = " recorded " + new Date(head.timestamp*1000).toISOString().replace("T"," ").slice(0,19) + " UTC";
const strip = s => s.replace(/\x1b\[[0-9;]*m/g, "").replace(/\r\n/g, "\n");
let times = [], acc = 0, prev = 0; for (const e of ev) { acc += Math.min(e[0]-prev, IDLE); prev = e[0]; times.push(acc); }
const o = document.getElementById("o"), T = document.getElementById("t"); let timer = null;
function render(n){ o.textContent = ev.slice(0,n).map(e=>strip(e[2])).join(""); window.scrollTo(0, document.body.scrollHeight);
  T.textContent = n ? ("t = " + ev[n-1][0].toFixed(0) + " s real time") : ""; }
document.getElementById("end").onclick = () => { clearTimeout(timer); render(ev.length); };
document.getElementById("play").onclick = () => { clearTimeout(timer); let i = 0; const sp = +document.getElementById("sp").value;
  const step = () => { i++; render(i); if (i < ev.length) timer = setTimeout(step, (times[i]-times[i-1])*1000/sp); }; step(); };
render(ev.length);
</script></body></html>"""


def build_player(cast_path, html_path):
    lines = [json.loads(line) for line in open(cast_path, encoding="utf-8")]
    html_path.write_text(PLAYER.replace("__CAST__", json.dumps(lines)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="full")
    ap.add_argument("--input", default="data/raw/spotify_reviews_18months.csv")
    ap.add_argument("--workers", default="8")
    ap.add_argument("--batch-requests", default="200")
    ap.add_argument("--extra", default="", help="extra args for the pipeline command (e.g. --poll-seconds 20)")
    a = ap.parse_args()
    out = ROOT / "runs" / a.run / "recording"
    out.mkdir(parents=True, exist_ok=True)
    py = str(ROOT / ".venv" / "bin" / "python")
    cmd = [py, "-m", "pipeline", "run", "--input", a.input, "--run", a.run, "--stages", "enrich",
           "--workers", a.workers, "--batch-requests", a.batch_requests, "--budget-usd", "4.40"] + a.extra.split()
    shown = " ".join(["python"] + cmd[1:])
    status = [py, "-m", "pipeline", "status", "--run", a.run]
    rec = Recorder(out / "session.cast", f"pipeline interrupt/resume demo - run '{a.run}'")
    rec.say(f"Interrupt/resume demo, recorded {time.strftime('%Y-%m-%d %H:%M:%S %Z')} on the real pipeline and real APIs")
    rec.say("1. Saved state before the run")
    rec.prompt(" ".join(["python"] + status[1:]))
    run_pty(rec, status)
    rec.say("2. Start enrichment. SIGINT (= Ctrl-C) is sent as soon as the first Message Batch has been collected and saved")
    rec.prompt(shown)
    rc1, t_int = run_pty(rec, cmd, interrupt_when=lambda s: "collected msgbatch" in s)
    rec.say(f"3. Interrupted (exit code {rc1}). Saved state after the interruption")
    rec.prompt(" ".join(["python"] + status[1:]))
    run_pty(rec, status)
    cps = checkpoint_counts(a.run)
    rec.out(("checkpoint snapshots: " + json.dumps(cps, indent=1) + "\n").encode())
    rec.say("4. Resume: the identical command. Open batches are collected (not resubmitted); completed IDs are skipped")
    rec.prompt(shown)
    rc2, _ = run_pty(rec, cmd)
    rec.say(f"5. Resume finished (exit code {rc2}). Final saved state")
    rec.prompt(" ".join(["python"] + status[1:]))
    run_pty(rec, status)
    rec.f.close()
    build_player(out / "session.cast", out / "replay.html")
    summary = {"run": a.run, "command": shown, "interrupt_sent_at_s": t_int, "exit_codes": {"interrupted": rc1, "resume": rc2},
               "checkpoints": checkpoint_counts(a.run), "recording": "session.cast", "player": "replay.html",
               "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S %Z")}
    (out / "demo_summary.json").write_text(json.dumps(summary, indent=1))
    print("\nrecording saved:", out)


if __name__ == "__main__":
    main()
