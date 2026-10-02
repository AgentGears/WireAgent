"""M7 Layer 2 post-merge live qualification (no mutation, no approval).

Proves the exact premise Layer 3 builds on: a live production owner
excludes a second real production runtime BEFORE that loser creates
browser authority, and clean ownership transfer works after the owner
terminates.

Phases:
  0. record environment + commit identities (7ad21ad, 372bf0d, exact HEAD)
  1. snapshot the REAL state dir (journal/effects/reconciliations/rules/
     session.json/authority.lock) and a browser-process census baseline
  2. start the real production runtime against the real logged-in state
     dir; bounded whoami retry (known ~1-in-3 hydration flakiness)
  3. loser #1 (fresh process): raw production Dispatcher.start() must
     return authority_busy with session never started, sb None, no lock
  4. loser #2 (fresh process): the real `m8 card` CLI must surface the
     controlled authority_busy error — exit 1, no traceback, no card
  5. prove the loser created no browser processes (PID-set census deltas)
     and performed NO durable mutation (snapshot byte-equality over the
     loser window)
  6. stop the real owner cleanly
  7. successor (fresh process): same raw start must now SUCCEED — clean
     ownership transfer, not permanent exclusion — then stop cleanly

No likes/posts/bookmarks or any remote effects. The CLI check stops at
the ownership refusal; even an unexpected startup could not approve a
card (no --yes flag exists; the decision reader reads piped-stdin EOF as
a deny).
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = str(REPO / "src")
STATE_DIR = (REPO / ".webwire").resolve()

RESULTS: list[dict] = []


def record(phase: str, ok: bool, evidence: str) -> bool:
    RESULTS.append({"phase": phase, "ok": ok, "evidence": evidence})
    print(f"{'PASS' if ok else 'FAIL'}  {phase}: {evidence}")
    return ok


# -- child scripts -----------------------------------------------------------

_CHILD_OWNER = "\n".join([
    "import asyncio, os, sys, time",
    "from pathlib import Path",
    "sys.path.insert(0, sys.argv[2])",
    "from webwire.config import WebWireConfig",
    "from webwire.dispatcher import Dispatcher",
    "",
    "async def main():",
    "    cfg = WebWireConfig(state_dir=Path(sys.argv[1]), kill_env_var=None)",
    "    d = Dispatcher(cfg)",
    "    print(f'OWNER_PID {os.getpid()}', flush=True)",
    "    r = await d.start()",
    "    if not r.ok:",
    "        print(f'OWNER_START_FAILED {getattr(r.error, chr(109)+chr(101)+chr(115)+chr(115)+chr(97)+chr(103)+chr(101), r)}', flush=True)",
    "        return 1",
    "    print('OWNER_STARTED', flush=True)",
    "    handle = None",
    "    for attempt in range(4):",
    "        who = await d.invoke('whoami', {})",
    "        if who.ok and isinstance(who.data, dict) and who.data.get('handle'):",
    "            handle = who.data['handle']",
    "            break",
    "        await asyncio.sleep(8)",
    "    if handle is None:",
    "        print('WHOAMI_FAILED', flush=True)",
    "        await d.stop()",
    "        return 1",
    "    print(f'WHOAMI_OK {handle} attempts={attempt + 1}', flush=True)",
    "    release = Path(sys.argv[3])",
    "    deadline = time.monotonic() + 600",
    "    while not release.exists():",
    "        if time.monotonic() > deadline:",
    "            print('OWNER_RELEASE_TIMEOUT', flush=True)",
    "            await d.stop()",
    "            return 1",
    "        time.sleep(0.2)",
    "    stop = await d.stop()",
    "    print(f'OWNER_STOPPED ok={stop.ok}', flush=True)",
    "    return 0 if stop.ok else 1",
    "",
    "raise SystemExit(asyncio.run(main()))",
])

_CHILD_LOSER = "\n".join([
    "import asyncio, sys",
    "from pathlib import Path",
    "sys.path.insert(0, sys.argv[2])",
    "from webwire.config import WebWireConfig",
    "from webwire.dispatcher import Dispatcher",
    "",
    "async def main():",
    "    cfg = WebWireConfig(state_dir=Path(sys.argv[1]), kill_env_var=None)",
    "    d = Dispatcher(cfg)",
    "    r = await d.start()",
    "    msg = getattr(r.error, 'message', '') if r.error else ''",
    "    started = getattr(d._session, '_started', None)",
    "    sb = getattr(d._session, 'sb', None)",
    "    lock = d._owner_lock is not None",
    "    busy = (not r.ok) and ('authority_busy' in msg)",
    "    print(f'LOSER_RESULT ok={r.ok} busy={busy}', flush=True)",
    "    print(f'LOSER_MSG {msg[:140]}', flush=True)",
    "    print(f'LOSER_STATE session_started={started} sb={sb} owner_lock_held={lock}', flush=True)",
    "    if busy and started is False and sb is None and not lock:",
    "        print('LOSER_BUSY_OK', flush=True)",
    "        return 0",
    "    print('LOSER_BUSY_FAIL', flush=True)",
    "    return 1",
    "",
    "raise SystemExit(asyncio.run(main()))",
])

_CHILD_SUCCESSOR = "\n".join([
    "import asyncio, sys",
    "from pathlib import Path",
    "sys.path.insert(0, sys.argv[2])",
    "from webwire.config import WebWireConfig",
    "from webwire.dispatcher import Dispatcher",
    "",
    "async def main():",
    "    cfg = WebWireConfig(state_dir=Path(sys.argv[1]), kill_env_var=None)",
    "    d = Dispatcher(cfg)",
    "    r = await d.start()",
    "    if not r.ok:",
    "        print(f'SUCCESSOR_START_FAILED {getattr(r.error, chr(109)+chr(101)+chr(115)+chr(115)+chr(97)+chr(103)+chr(101), r)}', flush=True)",
    "        return 1",
    "    print('SUCCESSOR_STARTED', flush=True)",
    "    who = await d.invoke('whoami', {})",
    "    ok_who = who.ok and isinstance(who.data, dict) and bool(who.data.get('handle'))",
    "    print(f'SUCCESSOR_WHOAMI ok={ok_who}', flush=True)",
    "    stop = await d.stop()",
    "    print(f'SUCCESSOR_STOPPED ok={stop.ok}', flush=True)",
    "    return 0 if (ok_who and stop.ok) else 1",
    "",
    "raise SystemExit(asyncio.run(main()))",
])


# -- observation helpers -----------------------------------------------------

def child_env() -> dict:
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = SRC + __import__("os").pathsep + env.get("PYTHONPATH", "")
    return env


def snapshot() -> dict:
    snaps = {}
    for name in [
        "session.json", "rules.json", "authority.lock",
        "journal.ndjson", "effects.ndjson", "reconciliations.ndjson",
    ]:
        p = STATE_DIR / name
        if not p.exists():
            snaps[name] = None
            continue
        st = p.stat()
        entry: dict = {"size": st.st_size, "mtime": round(st.st_mtime, 3)}
        try:
            b = p.read_bytes()
            entry["sha256_16"] = hashlib.sha256(b).hexdigest()[:16]
        except PermissionError:
            # Windows mandatory byte-range lock: the live owner holds
            # authority.lock's first byte, so content reads are lock
            # violations while held — size+mtime still observe it.
            entry["sha256_16"] = "<lock-held>"
        snaps[name] = entry
    return snaps


_PS_CENSUS = (
    "Get-CimInstance Win32_Process | Where-Object { "
    "($_.Name -match 'chrome|chromium|headless' -or "
    "$_.CommandLine -match 'playwright|patchright') -and "
    # The census's own powershell process matches the filter (its command
    # text names chromium/headless); exclude self-observation.
    "-not ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'Win32_Process') "
    "} | ForEach-Object { "
    "$cl = if ($_.CommandLine) { $_.CommandLine } else { '' }; "
    "$cl = $cl -replace '\\s+',' '; "
    "if ($cl.Length -gt 130) { $cl = $cl.Substring(0,130) }; "
    "'{0}|{1}|{2}' -f $_.ProcessId, $_.Name, $cl }"
)


def browser_census() -> dict[int, str]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _PS_CENSUS],
        capture_output=True, text=True, timeout=60,
    )
    census: dict[int, str] = {}
    for line in out.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].strip().isdigit():
            census[int(parts[0])] = f"{parts[1]}|{parts[2]}"
    return census


def main() -> int:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()

    env = {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "canonical_state_dir": str(STATE_DIR),
        "layer2_squash": "7ad21ad",
        "merge_record": "372bf0d",
        "exact_main_exercised": head,
    }
    print("ENV:", json.dumps(env, indent=1))

    with tempfile.TemporaryDirectory() as td:
        release = Path(td) / "release-owner"

        # -- 1. baselines ---------------------------------------------------
        s0 = snapshot()
        c0 = browser_census()
        print(f"baseline: state={ {k: (v['sha256_16'] if v else None) for k, v in s0.items()} }")
        print(f"baseline browser census: {len(c0)} processes")

        # -- 2. real owner ----------------------------------------------------
        owner = subprocess.Popen(
            [sys.executable, "-c", _CHILD_OWNER, str(STATE_DIR), SRC, str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=child_env(), cwd=REPO,
        )
        owner_pid: str | None = None
        handle: str | None = None
        try:
            deadline = time.monotonic() + 240
            owner_lines: list[str] = []
            started_ok = False
            while time.monotonic() < deadline:
                line = owner.stdout.readline()
                if not line:
                    break
                line = line.strip()
                owner_lines.append(line)
                print(f"  [owner] {line}")
                if line.startswith("OWNER_PID"):
                    owner_pid = line.split()[1]
                if line == "OWNER_STARTED":
                    started_ok = True
                if line.startswith("WHOAMI_OK"):
                    handle = line.split()[1]
                    break
                if line.startswith("OWNER_START_FAILED") or line == "WHOAMI_FAILED":
                    owner.kill()
                    record("2-owner-ready", False, "; ".join(owner_lines))
                    return 1
            if handle is None:
                owner.kill()
                record("2-owner-ready", False, f"no identity; lines={owner_lines}")
                return 1
            record(
                "2-owner-ready", started_ok,
                f"live owner READY, whoami handle={handle}, owner_pid={owner_pid}",
            )

            # -- state after owner is up (owner-attributable deltas allowed) --
            s1 = snapshot()
            c1 = browser_census()
            new_browser = {p: c1[p] for p in set(c1) - set(c0)}
            lost = set(c0) - set(c1)
            record(
                "2b-owner-browser", len(new_browser) > 0,
                f"owner spawned {len(new_browser)} browser/driver process(es); "
                f"baseline {len(c0)} -> {len(c1)} (baseline churn: {len(lost)} gone); "
                f"sample={list(new_browser.values())[:2]}",
            )
            owner_procs = set(c1) - set(c0)

            # -- 3. loser #1: raw production start -----------------------------
            loser1 = subprocess.run(
                [sys.executable, "-c", _CHILD_LOSER, str(STATE_DIR), SRC],
                capture_output=True, text=True, env=child_env(), cwd=REPO,
                timeout=120,
            )
            l1_lines = loser1.stdout.splitlines()
            print("  [loser1]", " | ".join(l1_lines))
            record(
                "3-loser-raw-start", loser1.returncode == 0 and any(
                    l == "LOSER_BUSY_OK" for l in l1_lines
                ),
                f"exit={loser1.returncode}; "
                + next((l for l in l1_lines if l.startswith("LOSER_MSG")), "")
                + "; " + next((l for l in l1_lines if l.startswith("LOSER_STATE")), ""),
            )

            # -- 4. loser #2: the real m8 card CLI -----------------------------
            cli = subprocess.run(
                [sys.executable, "-m", "webwire.m8_card_cli",
                 "card", "like_post", '{"post_id": "1"}'],
                capture_output=True, text=True, env=child_env(), cwd=REPO,
                timeout=180,
            )
            combined = cli.stdout + cli.stderr
            cli_ok = (
                cli.returncode == 1
                and "authority_busy" in cli.stderr
                and "Traceback" not in combined
                and "approve like_post" not in combined  # no card was rendered
            )
            record(
                "4-loser-cli", cli_ok,
                f"exit={cli.returncode}; busy_on_stderr={'authority_busy' in cli.stderr}; "
                f"no_traceback={'Traceback' not in combined}; "
                f"no_card_rendered={'approve like_post' not in combined}; "
                f"relative_warning={'relative state_dir' in cli.stderr}",
            )

            # -- 5. no loser-side browser, no loser-side mutation ---------------
            c2 = browser_census()
            spawned_by_losers = set(c2) - set(c1)
            record(
                "5a-no-loser-browser", len(spawned_by_losers) == 0,
                f"census after losers {len(c2)} vs after owner {len(c1)}; "
                f"new_pids={ {p: c2[p][:60] for p in spawned_by_losers} or 'none' }; "
                f"owner_procs_still_alive={len(owner_procs & set(c2))}/{len(owner_procs)}",
            )
            s2 = snapshot()
            changed = [n for n in s1 if s1[n] != s2[n]]
            record(
                "5b-no-loser-mutation", not changed,
                f"state dir byte-identical across the loser window: changed={changed or 'none'}; "
                f"journal={s2['journal.ndjson']}",
            )

            # -- 6. stop the real owner cleanly ---------------------------------
            release.touch()
            stop_line = ""
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                line = owner.stdout.readline()
                if not line:
                    break
                line = line.strip()
                print(f"  [owner] {line}")
                if line.startswith("OWNER_STOPPED"):
                    stop_line = line
                    break
            owner.wait(timeout=60)
            record(
                "6-owner-clean-stop",
                owner.returncode == 0 and "ok=True" in stop_line,
                f"owner exit={owner.returncode}; {stop_line}",
            )
            c3 = browser_census()
            owner_gone = len(owner_procs & set(c3))
            record(
                "6b-owner-browser-gone", owner_gone == 0,
                f"of {len(owner_procs)} owner-attributable processes, "
                f"{owner_gone} remain after stop",
            )
        finally:
            if owner.poll() is None:
                release.touch(exist_ok=True)
                try:
                    owner.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    owner.kill()

        # -- 7. successor ------------------------------------------------------
        succ = subprocess.run(
            [sys.executable, "-c", _CHILD_SUCCESSOR, str(STATE_DIR), SRC],
            capture_output=True, text=True, env=child_env(), cwd=REPO, timeout=240,
        )
        s_lines = succ.stdout.splitlines()
        print("  [successor]", " | ".join(s_lines))
        record(
            "7-successor-clean-transfer",
            succ.returncode == 0
            and any(l == "SUCCESSOR_STARTED" for l in s_lines)
            and any(l.startswith("SUCCESSOR_WHOAMI ok=True") for l in s_lines)
            and any(l.startswith("SUCCESSOR_STOPPED ok=True") for l in s_lines),
            f"exit={succ.returncode}; "
            + "; ".join(l for l in s_lines if l.startswith("SUCCESSOR")),
        )

    verdict = all(r["ok"] for r in RESULTS)
    print("\n=== QUALIFICATION RECORD ===")
    print(json.dumps({
        "environment": env,
        "owner_pid": owner_pid,
        "identity_handle": handle,
        "results": RESULTS,
        "verdict": "PASS" if verdict else "FAIL",
    }, indent=1))
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
