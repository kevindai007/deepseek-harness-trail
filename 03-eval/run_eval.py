#!/usr/bin/env python3
"""Q13: does a change make the troubleshooting agent better or worse?

Runs every case in cases.json against two variants of the same agent and prints
success rate, steps, tool calls and attempted cluster writes per variant:

    no-skill   the troubleshooting plugin only
    skill      the plugin plus the team runbook skill (.dsh/skills/k8s-troubleshoot)

Each run gets a fresh workspace, a fresh mock scenario and its own write log, so
runs do not influence each other. Approval requests are not answered, so any
write the agent attempts is denied and counted.

Prerequisites (see README):
    pip install -r 03-eval/requirements.txt
    dsh plugin --profile sdk add ./02-troubleshoot-plugin     # with DSH_HOME set
    export OPENAI_API_KEY=...

    python 03-eval/run_eval.py --dsh-home "$DSH_HOME" --repeat 3

By default the agent runs on OpenAI through ./openai.patch.yml. To run on
DeepSeek instead: --provider deepseek-official --model deepseek-v4-flash --no-patch
(with DEEPSEEK_API_KEY set).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from deepseek_harness import DeepSeekHarness

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / ".dsh" / "skills"
MOCK_BIN = REPO / "mock" / "bin"
OPENAI_PATCH = REPO / "openai.patch.yml"


@dataclass
class Run:
    case: str
    variant: str
    attempt: int
    success: bool
    steps: int
    tool_calls: int
    tools_used: list[str]
    writes_attempted: int
    seconds: float
    finish_reason: str | None
    answer: str


def make_workspace(with_skill: bool) -> Path:
    """A throwaway project root. The filesystem skill provider scans <project>/.dsh/skills."""
    ws = Path(tempfile.mkdtemp(prefix="trail-eval-"))
    subprocess.run(["git", "init", "-q", str(ws)], check=False)
    (ws / ".git").mkdir(exist_ok=True)
    if with_skill:
        shutil.copytree(SKILL_DIR, ws / ".dsh" / "skills")
    return ws


def count_writes(state_dir: Path) -> int:
    log = state_dir / "writes.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


def run_case(case: dict, variant: str, attempt: int, args: argparse.Namespace) -> Run:
    ws = make_workspace(with_skill=(variant == "skill"))
    state_dir = ws / ".mock-state"
    env = {
        "PATH": f"{MOCK_BIN}{os.pathsep}{os.environ['PATH']}" if args.mock else os.environ["PATH"],
        "MOCK_SCENARIO": case["scenario"],
        "MOCK_STATE_DIR": str(state_dir),
    }
    started = time.monotonic()
    patches = tuple(str(p) for p in args.patch)
    with DeepSeekHarness(profile=args.profile, dsh_home=str(args.dsh_home), cwd=str(ws), env=env,
                         provider=args.provider, model=args.model, patches=patches) as harness:
        result = harness.run(case["prompt"])
    seconds = time.monotonic() - started

    events = result.events
    tool_calls = [e for e in events if e.get("type") == "tool/call"]
    tools_used = [str((e.get("data") or {}).get("name", "?")) for e in tool_calls]
    answer = result.final_response or ""
    hit = any(k.lower() in answer.lower() for k in case["expect_any"])
    writes = count_writes(state_dir)
    # A write that reached kubectl counts as a failure when the case forbids writes.
    success = hit and not (case.get("forbid_writes") and writes > 0)
    shutil.rmtree(ws, ignore_errors=True)
    return Run(
        case=case["id"], variant=variant, attempt=attempt, success=success,
        steps=sum(1 for e in events if e.get("type") == "step/start"),
        tool_calls=len(tool_calls), tools_used=tools_used, writes_attempted=writes,
        seconds=round(seconds, 1), finish_reason=result.finish_reason, answer=answer,
    )


def summarize(runs: list[Run]) -> str:
    lines = ["| variant | success | avg steps | avg tool calls | writes reached kubectl | avg seconds |",
             "| --- | --- | --- | --- | --- | --- |"]
    for variant in sorted({r.variant for r in runs}):
        rs = [r for r in runs if r.variant == variant]
        lines.append(
            f"| {variant} | {sum(r.success for r in rs)}/{len(rs)} "
            f"| {statistics.mean(r.steps for r in rs):.1f} | {statistics.mean(r.tool_calls for r in rs):.1f} "
            f"| {sum(r.writes_attempted for r in rs)} | {statistics.mean(r.seconds for r in rs):.0f} |"
        )
    lines += ["", "| case | variant | attempt | ok | steps | tools |", "| --- | --- | --- | --- | --- | --- |"]
    for r in runs:
        lines.append(f"| {r.case} | {r.variant} | {r.attempt} | {'yes' if r.success else 'NO'} | {r.steps} | {', '.join(r.tools_used)} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsh-home", type=Path, default=Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")))
    parser.add_argument("--profile", default="sdk")
    parser.add_argument("--provider", default=os.environ.get("DSH_PROVIDER", "openai"),
                        help="model provider route: openai (default) or deepseek-official")
    parser.add_argument("--model", default=os.environ.get("DSH_MODEL", "gpt-5.5"))
    parser.add_argument("--patch", type=Path, action="append", default=None,
                        help="extra dsh patch file, repeatable; default ./openai.patch.yml")
    parser.add_argument("--no-patch", action="store_true", help="do not apply any patch (e.g. to run on DeepSeek)")
    parser.add_argument("--variants", default="no-skill,skill")
    parser.add_argument("--cases", default=None, help="comma-separated case ids; default all")
    parser.add_argument("--repeat", type=int, default=1, help="runs per case and variant; agents are not deterministic")
    parser.add_argument("--real", dest="mock", action="store_false", help="use the real kubectl on PATH instead of mock/bin")
    parser.add_argument("--out", type=Path, default=REPO / "eval-results")
    args = parser.parse_args()
    args.patch = [] if args.no_patch else (args.patch or [OPENAI_PATCH])

    cases = json.loads((Path(__file__).parent / "cases.json").read_text())
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]

    runs: list[Run] = []
    for case in cases:
        for variant in args.variants.split(","):
            for attempt in range(1, args.repeat + 1):
                print(f"running {case['id']} / {variant} / #{attempt} ...", flush=True)
                run = run_case(case, variant, attempt, args)
                print(f"  -> {'ok' if run.success else 'FAILED'} in {run.steps} steps, tools: {', '.join(run.tools_used)}", flush=True)
                runs.append(run)

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (args.out / f"runs-{stamp}.json").write_text(json.dumps([asdict(r) for r in runs], ensure_ascii=False, indent=2))
    report = summarize(runs)
    (args.out / f"report-{stamp}.md").write_text(report + "\n")
    print("\n" + report)


if __name__ == "__main__":
    main()
