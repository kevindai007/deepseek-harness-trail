#!/usr/bin/env python3
"""A deliberately naive troubleshooting agent in about 100 lines (talk Q2-Q4).

It talks to the model through the OpenAI Chat Completions API. Any
OpenAI-compatible endpoint works, DeepSeek included, so switching providers
is only a matter of OPENAI_BASE_URL and OPENAI_MODEL. It has exactly one tool,
`run_command`, and no guard rails: that is the point. Part 2 of the talk ends
by running it against the four "toy breaks" scenarios.

    export OPENAI_API_KEY=...
    export OPENAI_MODEL=gpt-5.5                                # optional
    export PATH="$PWD/mock/bin:$PATH" MOCK_SCENARIO=db-pool   # or a real dev cluster
    python 01-mini-loop/mini_agent.py "dev 环境下单接口报 500，帮我查一下原因"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess

from openai import OpenAI

SYSTEM = (
    "You are an on-call SRE assistant. The Kubernetes namespace is trail-dev. "
    "Use the run_command tool to run kubectl commands, then explain the root cause."
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "Run one shell command (kubectl only) and return its stdout and stderr.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The full command line, e.g. kubectl get pods -n trail-dev"}},
            "required": ["command"],
        },
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
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.5"))
    parser.add_argument("--max-steps", type=int, default=15, help="Q3: a hard stop owned by the harness, not the model")
    parser.add_argument("--quiet", action="store_true", help="hide tool output, keep the per-step summary")
    args = parser.parse_args()

    # Reads OPENAI_API_KEY, and OPENAI_BASE_URL when set (a proxy, or another
    # OpenAI-compatible provider such as DeepSeek).
    client = OpenAI()
    # Q4: the model is stateless. This list IS the agent's memory, and the whole
    # thing is sent again on every step.
    messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": args.task}]

    for step in range(1, args.max_steps + 1):
        response = client.chat.completions.create(model=args.model, tools=TOOLS, messages=messages)
        choice = response.choices[0]
        message = choice.message
        usage = response.usage
        print(f"\n=== step {step}: input {usage.prompt_tokens} tokens, output {usage.completion_tokens} tokens, finish={choice.finish_reason}")

        # Keep the assistant turn, tool calls included: the next request must replay it.
        messages.append(message.model_dump(exclude_none=True))
        if message.content and message.content.strip():
            print(f"[assistant] {message.content.strip()}")

        # Q3 stop condition 1: the model asked for no tool, so it thinks it is done.
        if not message.tool_calls:
            return

        for call in message.tool_calls:
            try:
                command = json.loads(call.function.arguments or "{}").get("command", "")
            except json.JSONDecodeError:
                command = ""
            print(f"[tool_call {call.id}] run_command {json.dumps(command, ensure_ascii=False)}")
            output, is_error = run_command(command)
            if not args.quiet:
                print(output[:2000] + ("\n... (truncated in this printout only)" if len(output) > 2000 else ""))
            # Q3: a failed command is not an exception. It goes back to the model
            # as a tool result so the model can correct itself.
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": ("ERROR: " if is_error else "") + output,
            })

    # Q3 stop condition 2: the harness gives up, whatever the model wanted.
    print(f"\n=== stopped: reached --max-steps={args.max_steps}")


if __name__ == "__main__":
    main()
