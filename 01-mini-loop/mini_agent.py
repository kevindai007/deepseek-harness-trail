#!/usr/bin/env python3
"""A deliberately naive troubleshooting agent in about 100 lines (talk Q2-Q4).

It talks to DeepSeek through the Anthropic-compatible Messages API, which is
the same protocol the dsh DeepSeek adapter uses. It has exactly one tool,
`run_command`, and no guard rails: that is the point. Part 2 of the talk ends
by running it against the four "toy breaks" scenarios.

    export DEEPSEEK_API_KEY=...
    export PATH="$PWD/mock/bin:$PATH" MOCK_SCENARIO=db-pool   # or a real dev cluster
    python 01-mini-loop/mini_agent.py "dev 环境下单接口报 500，帮我查一下原因"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

import anthropic

SYSTEM = (
    "You are an on-call SRE assistant. The Kubernetes namespace is trail-dev. "
    "Use the run_command tool to run kubectl commands, then explain the root cause."
)

TOOLS = [{
    "name": "run_command",
    "description": "Run one shell command (kubectl only) and return its stdout and stderr.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The full command line, e.g. kubectl get pods -n trail-dev"}},
        "required": ["command"],
    },
}]


def run_command(command: str) -> tuple[str, bool]:
    """Q2: the model only *proposes* a call; this function is what actually runs."""
    if not command.strip().startswith("kubectl "):
        return "refused: this demo only runs kubectl commands", True
    proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
    output = proc.stdout + proc.stderr
    return output, proc.returncode != 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    parser.add_argument("--max-steps", type=int, default=15, help="Q3: a hard stop owned by the harness, not the model")
    parser.add_argument("--quiet", action="store_true", help="hide tool output, keep the per-step summary")
    args = parser.parse_args()

    client = anthropic.Anthropic(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"),
    )
    # Q4: the model is stateless. This list IS the agent's memory, and the whole
    # thing is sent again on every step.
    messages: list[dict] = [{"role": "user", "content": args.task}]

    for step in range(1, args.max_steps + 1):
        response = client.messages.create(
            model=args.model, max_tokens=4096, system=SYSTEM, tools=TOOLS, messages=messages,
        )
        usage = response.usage
        print(f"\n=== step {step}: input {usage.input_tokens} tokens, output {usage.output_tokens} tokens, stop={response.stop_reason}")

        # Keep every block the model returned, thinking blocks included. They
        # carry a signature and are part of what the next request must replay.
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in response.content]})

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"[assistant] {block.text.strip()}")

        # Q3 stop condition 1: the model asked for no tool, so it thinks it is done.
        if not tool_calls:
            return

        results = []
        for call in tool_calls:
            command = call.input.get("command", "")
            print(f"[tool_call {call.id}] run_command {json.dumps(command, ensure_ascii=False)}")
            output, is_error = run_command(command)
            if not args.quiet:
                print(output[:2000] + ("\n... (truncated in this printout only)" if len(output) > 2000 else ""))
            # Q3: a failed command is not an exception. It goes back to the model
            # as a tool result so the model can correct itself.
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    # Q3 stop condition 2: the harness gives up, whatever the model wanted.
    print(f"\n=== stopped: reached --max-steps={args.max_steps}")


if __name__ == "__main__":
    main()
