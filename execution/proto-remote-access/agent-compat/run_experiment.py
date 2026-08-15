#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "deepseek-v4-flash-0731"
MODEL = DEFAULT_MODEL
HERMES_COMMIT = "45af7a71fcd420b4422d2c074b1ce58b9ce0d048"
DSH_REPO = Path(os.environ.get("DEEPSEEK_HARNESS_REPO", Path.home() / "project" / "Agents" / "deepseek-harness"))
ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
FIRST_PROMPT = """Work only in the current fixture directory. Read brief.md and complete it end to end. You must use tools to inspect the inputs, write result.json, run python3 verify.py result.json, correct any rejection, and rerun until it prints VERIFIED. End with a concise result."""
FOLLOWUP_PROMPT = """Continue from the result you just verified. Use tools to read adjustment.json, create final_summary.json with selected_ids, prior_total, adjustment, and adjusted_total, then run python3 verify.py final_summary.json. Do not finish until it prints VERIFIED."""


class ExperimentError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def api_root(value: str) -> str:
    value = value.rstrip("/")
    return value if value.endswith("/v1") else value + "/v1"


def request_json(url: str, token: str, body: dict[str, Any], *, anthropic: bool = False) -> dict[str, Any]:
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    if anthropic:
        headers.update({"x-api-key": token, "anthropic-version": "2023-06-01"})
    request = urllib.request.Request(url, json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:2000]
        raise ExperimentError(f"HTTP {exc.code}: {detail}") from exc


def request_sse(url: str, token: str, body: dict[str, Any], *, anthropic: bool = False) -> list[dict[str, Any]]:
    headers = {"content-type": "application/json", "authorization": f"Bearer {token}"}
    if anthropic:
        headers.update({"x-api-key": token, "anthropic-version": "2023-06-01"})
    request = urllib.request.Request(url, json.dumps(body).encode(), headers=headers, method="POST")
    events: list[dict[str, Any]] = []
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            for raw in response:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    events.append({"done": True})
                elif data:
                    events.append(json.loads(data))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:2000]
        raise ExperimentError(f"HTTP {exc.code}: {detail}") from exc
    return events


def run_case(name: str, fn: Any) -> dict[str, Any]:
    started = time.monotonic()
    try:
        detail = fn()
        return {"name": name, "status": "pass", "seconds": round(time.monotonic() - started, 3), "detail": detail}
    except Exception as exc:
        return {
            "name": name,
            "status": "fail",
            "seconds": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def protocol_tests(base: str, token: str) -> list[dict[str, Any]]:
    chat_url = base + "/chat/completions"
    messages_url = base + "/messages"
    tool = {
        "type": "function",
        "function": {
            "name": "lookup_multiplier",
            "description": "Return the integer multiplier for a code",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    }

    def openai_text() -> dict[str, Any]:
        value = request_json(chat_url, token, {"model": MODEL, "messages": [{"role": "user", "content": "Return exactly PORTAL_OK"}], "temperature": 0, "max_tokens": 64})
        text = value["choices"][0]["message"].get("content") or ""
        if "PORTAL_OK" not in text:
            raise ExperimentError(f"unexpected content: {text[:200]}")
        return {"finish_reason": value["choices"][0].get("finish_reason")}

    def openai_stream() -> dict[str, Any]:
        events = request_sse(chat_url, token, {"model": MODEL, "messages": [{"role": "user", "content": "Return exactly STREAM_OK"}], "temperature": 0, "max_tokens": 64, "stream": True})
        chunks = [((item.get("choices") or [{}])[0].get("delta") or {}).get("content", "") for item in events if "choices" in item]
        text = "".join(chunks)
        if "STREAM_OK" not in text or not any(item.get("done") for item in events):
            raise ExperimentError(f"invalid stream termination/content: {text[:200]}")
        return {"events": len(events)}

    def openai_tool_stream(forced: bool = False) -> dict[str, Any]:
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Call lookup_multiplier with code OMEGA. Do not guess its result."}],
            "tools": [tool],
            "temperature": 0,
            "max_tokens": 256,
            "stream": True,
        }
        if forced:
            body["tool_choice"] = "required"
        events = request_sse(chat_url, token, body)
        fragments = []
        finish_reasons = []
        for item in events:
            choice = (item.get("choices") or [{}])[0]
            fragments.extend((choice.get("delta") or {}).get("tool_calls") or [])
            if choice.get("finish_reason") is not None:
                finish_reasons.append(choice["finish_reason"])
        names = [fragment.get("function", {}).get("name", "") for fragment in fragments]
        ids = [fragment.get("id", "") for fragment in fragments]
        if not fragments or "lookup_multiplier" not in "".join(names) or not any(ids):
            raise ExperimentError(
                f"stream finish_reasons={finish_reasons} but tool fragments={fragments[:4]}"
            )
        return {"events": len(events), "finish_reasons": finish_reasons, "tool_call_id_present": True}

    def openai_tool_loop(forced: bool = False) -> dict[str, Any]:
        first_body = {"model": MODEL, "messages": [{"role": "user", "content": "Call lookup_multiplier with code OMEGA. Do not guess its result."}], "tools": [tool], "temperature": 0, "max_tokens": 256}
        if forced:
            first_body["tool_choice"] = "required"
        first = request_json(chat_url, token, first_body)
        assistant = first["choices"][0]["message"]
        calls = assistant.get("tool_calls") or []
        if len(calls) != 1 or calls[0]["function"]["name"] != "lookup_multiplier":
            raise ExperimentError(
                "finish_reason="
                f"{first['choices'][0].get('finish_reason')} but message.tool_calls={calls}; "
                f"message keys={sorted(assistant)}"
            )
        second = request_json(chat_url, token, {"model": MODEL, "messages": first_body["messages"] + [assistant, {"role": "tool", "tool_call_id": calls[0]["id"], "content": "17"}, {"role": "user", "content": "Return exactly FINAL=34 using the tool result."}], "temperature": 0, "max_tokens": 128})
        text = second["choices"][0]["message"].get("content") or ""
        if "FINAL=34" not in text:
            raise ExperimentError(f"tool result was not consumed: {text[:300]}")
        return {"tool_call_id_present": bool(calls[0].get("id"))}

    def anthropic_text() -> dict[str, Any]:
        value = request_json(messages_url, token, {"model": MODEL, "max_tokens": 64, "messages": [{"role": "user", "content": "Return exactly ANTHROPIC_OK"}]}, anthropic=True)
        text = "".join(item.get("text", "") for item in value.get("content", []) if item.get("type") == "text")
        if "ANTHROPIC_OK" not in text:
            raise ExperimentError(f"unexpected content: {text[:200]}")
        return {"stop_reason": value.get("stop_reason")}

    def anthropic_stream() -> dict[str, Any]:
        events = request_sse(messages_url, token, {"model": MODEL, "max_tokens": 64, "stream": True, "messages": [{"role": "user", "content": "Return exactly A_STREAM_OK"}]}, anthropic=True)
        text = "".join((item.get("delta") or {}).get("text", "") for item in events if item.get("type") == "content_block_delta")
        if "A_STREAM_OK" not in text or not any(item.get("type") == "message_stop" for item in events):
            raise ExperimentError(f"invalid Anthropic stream: {text[:200]}")
        return {"events": len(events)}

    def anthropic_tool_stream(forced: bool = False) -> dict[str, Any]:
        a_tool = {"name": "lookup_multiplier", "description": "Return the integer multiplier for a code", "input_schema": tool["function"]["parameters"]}
        body = {
            "model": MODEL,
            "max_tokens": 256,
            "stream": True,
            "messages": [{"role": "user", "content": "Call lookup_multiplier with code OMEGA. Do not guess its result."}],
            "tools": [a_tool],
        }
        if forced:
            body["tool_choice"] = {"type": "any"}
        events = request_sse(messages_url, token, body, anthropic=True)
        starts = [item.get("content_block", {}) for item in events if item.get("type") == "content_block_start"]
        calls = [block for block in starts if block.get("type") == "tool_use"]
        stop_reasons = [item.get("delta", {}).get("stop_reason") for item in events if item.get("type") == "message_delta"]
        if len(calls) != 1 or calls[0].get("name") != "lookup_multiplier" or not calls[0].get("id"):
            raise ExperimentError(f"stream stop_reasons={stop_reasons} but tool_use starts={calls}")
        if not any(item.get("type") == "message_stop" for item in events):
            raise ExperimentError("Anthropic tool stream omitted message_stop")
        return {"events": len(events), "stop_reasons": stop_reasons, "tool_use_id_present": True}

    def anthropic_tool_loop(forced: bool = False) -> dict[str, Any]:
        a_tool = {"name": "lookup_multiplier", "description": "Return the integer multiplier for a code", "input_schema": tool["function"]["parameters"]}
        first_messages = [{"role": "user", "content": "Call lookup_multiplier with code OMEGA. Do not guess its result."}]
        first_body = {"model": MODEL, "max_tokens": 256, "messages": first_messages, "tools": [a_tool]}
        if forced:
            first_body["tool_choice"] = {"type": "any"}
        first = request_json(messages_url, token, first_body, anthropic=True)
        calls = [item for item in first.get("content", []) if item.get("type") == "tool_use"]
        if len(calls) != 1:
            raise ExperimentError(
                f"stop_reason={first.get('stop_reason')} but tool_use blocks={calls}; "
                f"content types={[item.get('type') for item in first.get('content', [])]}"
            )
        second_messages = first_messages + [{"role": "assistant", "content": first["content"]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0]["id"], "content": "17"}, {"type": "text", "text": "Return exactly FINAL=34."}]}]
        second = request_json(messages_url, token, {"model": MODEL, "max_tokens": 128, "messages": second_messages, "tools": [a_tool]}, anthropic=True)
        text = "".join(item.get("text", "") for item in second.get("content", []) if item.get("type") == "text")
        if "FINAL=34" not in text:
            raise ExperimentError(f"Anthropic tool result was not consumed: {text[:300]}")
        return {"tool_use_id_present": bool(calls[0].get("id"))}

    def inline_system() -> dict[str, Any]:
        value = request_json(messages_url, token, {"model": MODEL, "max_tokens": 64, "system": "Return only the requested password, no explanation.", "messages": [{"role": "user", "content": "The password is APPLE42."}, {"role": "assistant", "content": "Understood."}, {"role": "system", "content": "The password is now BANANA99."}, {"role": "user", "content": "What is the current password?"}]}, anthropic=True)
        text = "".join(item.get("text", "") for item in value.get("content", []) if item.get("type") == "text")
        if "BANANA99" in text:
            behavior = "preserved"
        elif "APPLE42" in text:
            behavior = "dropped"
        else:
            behavior = "indeterminate"
        return {"http_compatible": True, "inline_system_behavior": behavior, "answer": text[:120]}

    def count_tokens() -> dict[str, Any]:
        body = {"model": MODEL, "system": "count baseline", "messages": [{"role": "user", "content": "alpha"}, {"role": "system", "content": "BANANA99 inline marker"}, {"role": "user", "content": "omega"}]}
        value = request_json(messages_url + "/count_tokens", token, body, anthropic=True)
        count = value.get("input_tokens")
        if not isinstance(count, int) or count <= 0:
            raise ExperimentError(f"invalid count response: {value}")
        return {"input_tokens": count, "note": "generation normalization equality requires gateway-side payload capture"}

    return [
        run_case("openai_text", openai_text),
        run_case("openai_stream", openai_stream),
        run_case("openai_tool_stream_auto", openai_tool_stream),
        run_case("openai_tool_stream_forced", lambda: openai_tool_stream(True)),
        run_case("openai_tool_loop_auto", openai_tool_loop),
        run_case("openai_tool_loop_forced", lambda: openai_tool_loop(True)),
        run_case("anthropic_text", anthropic_text),
        run_case("anthropic_stream", anthropic_stream),
        run_case("anthropic_tool_stream_auto", anthropic_tool_stream),
        run_case("anthropic_tool_stream_forced", lambda: anthropic_tool_stream(True)),
        run_case("anthropic_tool_loop_auto", anthropic_tool_loop),
        run_case("anthropic_tool_loop_forced", lambda: anthropic_tool_loop(True)),
        run_case("anthropic_inline_system", inline_system),
        run_case("anthropic_count_tokens", count_tokens),
    ]


def run_command(command: list[str], cwd: Path, env: dict[str, str], log_path: Path, token: str, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    completed = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    output = completed.stdout.replace(token, "<redacted>") if token else completed.stdout
    log_path.write_text(output)
    if completed.returncode != 0:
        raise ExperimentError(f"command exited {completed.returncode}; see {log_path.name}")
    completed.stdout = output
    completed.elapsed = round(time.monotonic() - started, 3)  # type: ignore[attr-defined]
    return completed


def prepare_workspace(run_dir: Path, name: str) -> Path:
    workspace = run_dir / "workspaces" / name
    shutil.copytree(FIXTURES, workspace)
    return workspace


def verify_agent_artifacts(workspace: Path) -> dict[str, Any]:
    for name in ("result.json", "final_summary.json"):
        completed = subprocess.run([sys.executable, "verify.py", name], cwd=workspace, text=True, capture_output=True)
        if completed.returncode != 0 or "VERIFIED" not in completed.stdout:
            raise ExperimentError(f"{name} failed independent verification: {completed.stdout} {completed.stderr}")
    return {"result": json.loads((workspace / "result.json").read_text()), "final_summary": json.loads((workspace / "final_summary.json").read_text())}


def count_tool_events(text: str) -> int:
    patterns = [r'"type"\s*:\s*"tool_use"', r'"type"\s*:\s*"toolCall"', r'"type"\s*:\s*"tool_call"', r'"tool_name"\s*:']
    return sum(len(re.findall(pattern, text)) for pattern in patterns)


def claude_test(run_dir: Path, base: str, token: str) -> dict[str, Any]:
    if not shutil.which("claude"):
        raise ExperimentError("claude executable not found")
    workspace = prepare_workspace(run_dir, "claude-code")
    session = str(uuid.uuid4())
    env = os.environ.copy()
    env.update({"ANTHROPIC_BASE_URL": base.removesuffix("/v1"), "ANTHROPIC_AUTH_TOKEN": token, "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    common = ["claude", "--bare", "--model", MODEL, "--dangerously-skip-permissions", "--tools", "Read,Bash,Write", "--output-format", "stream-json", "--verbose", "-p"]
    first = run_command(common + ["--session-id", session, FIRST_PROMPT], workspace, env, run_dir / "claude-first.jsonl", token)
    second = run_command(common + ["--resume", session, FOLLOWUP_PROMPT], workspace, env, run_dir / "claude-followup.jsonl", token)
    tools = count_tool_events(first.stdout + second.stdout)
    if tools < 2:
        raise ExperimentError(f"insufficient observable tool events: {tools}")
    return {"session": session, "tool_events": tools, "artifacts": verify_agent_artifacts(workspace), "seconds": round(first.elapsed + second.elapsed, 3)}  # type: ignore[attr-defined]


def pi_test(run_dir: Path, base: str, token: str) -> dict[str, Any]:
    if not shutil.which("pi"):
        raise ExperimentError("pi executable not found")
    workspace = prepare_workspace(run_dir, "pi")
    config_dir = run_dir / "pi-config"
    config_dir.mkdir()
    (config_dir / "models.json").write_text(json.dumps({"providers": {"portal": {"baseUrl": base, "api": "openai-completions", "apiKey": "$PRIVATELLM_API_TOKEN", "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False, "thinkingFormat": "openai", "requiresReasoningContentOnAssistantMessages": True}, "models": [{"id": MODEL, "name": MODEL, "reasoning": True, "input": ["text"], "contextWindow": 1048576, "maxTokens": 32768, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}}, indent=2))
    (config_dir / "settings.json").write_text(json.dumps({"defaultProvider": "portal", "defaultModel": MODEL, "defaultThinkingLevel": "medium"}, indent=2))
    env = os.environ.copy()
    env["PI_CODING_AGENT_DIR"] = str(config_dir)
    env["PRIVATELLM_API_TOKEN"] = token
    session = str(uuid.uuid4())
    common = ["pi", "--provider", "portal", "--model", MODEL, "--mode", "json", "--session-dir", str(run_dir / "pi-sessions"), "--session-id", session, "--tools", "read,bash,write", "--no-extensions", "--no-skills", "--no-context-files", "--approve", "-p"]
    first = run_command(common + [FIRST_PROMPT], workspace, env, run_dir / "pi-first.jsonl", token)
    second = run_command(common + [FOLLOWUP_PROMPT], workspace, env, run_dir / "pi-followup.jsonl", token)
    tools = count_tool_events(first.stdout + second.stdout)
    if tools < 2:
        raise ExperimentError(f"insufficient observable tool events: {tools}")
    return {"session": session, "tool_events": tools, "artifacts": verify_agent_artifacts(workspace), "seconds": round(first.elapsed + second.elapsed, 3)}  # type: ignore[attr-defined]


def ensure_hermes(run_dir: Path, token: str) -> tuple[str, str]:
    override = os.environ.get("HERMES_BIN")
    if override:
        return override, "override"
    existing = shutil.which("hermes")
    if existing:
        return existing, "existing"
    bin_dir = ROOT / ".tools" / "bin"
    tool_dir = ROOT / ".tools" / "uv"
    source_dir = ROOT / ".tools" / "hermes-src"
    executable = bin_dir / "hermes"
    if not executable.exists():
        env = os.environ.copy()
        env.update({"UV_TOOL_BIN_DIR": str(bin_dir), "UV_TOOL_DIR": str(tool_dir)})
        if not source_dir.exists():
            run_command(
                ["git", "clone", "--filter=blob:none", "--no-checkout", "https://github.com/NousResearch/hermes-agent.git", str(source_dir)],
                ROOT,
                env,
                run_dir / "hermes-clone.log",
                token,
                timeout=1800,
            )
        run_command(
            ["git", "fetch", "--depth", "1", "origin", HERMES_COMMIT],
            source_dir,
            env,
            run_dir / "hermes-fetch.log",
            token,
            timeout=1800,
        )
        run_command(
            ["git", "checkout", "--detach", HERMES_COMMIT],
            source_dir,
            env,
            run_dir / "hermes-checkout.log",
            token,
        )
        actual_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source_dir, text=True, capture_output=True, check=True
        ).stdout.strip()
        if actual_commit != HERMES_COMMIT:
            raise ExperimentError(f"Hermes checkout changed: expected {HERMES_COMMIT}, got {actual_commit}")
        run_command(
            ["uv", "tool", "install", "--editable", str(source_dir)],
            ROOT,
            env,
            run_dir / "hermes-install.log",
            token,
            timeout=1800,
        )
    return str(executable), HERMES_COMMIT


def hermes_test(run_dir: Path, base: str, token: str) -> dict[str, Any]:
    executable, version = ensure_hermes(run_dir, token)
    workspace = prepare_workspace(run_dir, "hermes-agent")
    hermes_home = run_dir / "hermes-home"
    hermes_home.mkdir()
    config = f"""model:\n  default: {MODEL}\n  provider: portal\nproviders:\n  portal:\n    base_url: {base}\n    key_env: PRIVATELLM_API_TOKEN\n    transport: chat_completions\n    model: {MODEL}\n    models:\n      {MODEL}:\n        context_length: 1048576\nterminal:\n  backend: local\n  cwd: .\nagent:\n  max_turns: 40\n  reasoning_effort: medium\n  tool_use_enforcement: true\n"""
    (hermes_home / "config.yaml").write_text(config)
    env = os.environ.copy()
    env.update({"HERMES_HOME": str(hermes_home), "PRIVATELLM_API_TOKEN": token, "HERMES_ACCEPT_HOOKS": "1"})
    common = [executable, "chat", "--provider", "portal", "--model", MODEL, "--toolsets", "hermes-cli", "--yolo", "--ignore-rules", "-Q", "--in", str(workspace)]
    first = run_command(common + ["-q", FIRST_PROMPT], workspace, env, run_dir / "hermes-first.log", token)
    second = run_command(common + ["--resume", "latest", "-q", FOLLOWUP_PROMPT], workspace, env, run_dir / "hermes-followup.log", token)
    logs = first.stdout + second.stdout
    tool_signals = len(re.findall(r"(?:terminal|write_file|read_file|tool)", logs, re.IGNORECASE))
    return {"version": version, "tool_signals": tool_signals, "artifacts": verify_agent_artifacts(workspace), "seconds": round(first.elapsed + second.elapsed, 3)}  # type: ignore[attr-defined]


def deepseek_harness_test(run_dir: Path, base: str, token: str) -> dict[str, Any]:
    entrypoint = DSH_REPO / "apps" / "cli" / "src" / "bin.ts"
    tsx_loader = DSH_REPO / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
    if not entrypoint.exists() or not tsx_loader.exists():
        raise ExperimentError(f"DeepSeek Harness source install is incomplete: {DSH_REPO}")
    workspace = prepare_workspace(run_dir, "deepseek-harness")
    patch_path = run_dir / "deepseek-harness.patch.yml"
    patch_path.write_text(f"""- id: llm-deepseek
  config:
    apiKeyEnv: DEEPSEEK_API_KEY
    baseURL: {base}
    thinking: enabled
    reasoningEffort: max
    maxTokens: 32768
    defaultContextWindow: 1048576
    models:
      - id: {MODEL}
        contextWindow: 1048576
        maxTokens: 32768
- id: agent-default-model
  config:
    provider: deepseek-official
    model: {MODEL}
- id: fs-local
  config:
    cwd: {workspace}
- id: session-persistence-jsonl
  config:
    root: {workspace / '.sessions'}
    compression: none
""")
    combined_prompt = FIRST_PROMPT + "\n\nAfter result.json is VERIFIED, continue in the same run: " + FOLLOWUP_PROMPT
    env = os.environ.copy()
    env.update({
        "DEEPSEEK_API_KEY": token,
        "DEEPSEEK_BASE_URL": base,
        "DSH_HOME": str(run_dir / "deepseek-harness-home"),
        "DSH_PERMISSION_MODE": "danger-full-access",
        "DSH_SNAPSHOT": "none",
        "DSH_TELEMETRY_DISABLED": "1",
        "TSX_TSCONFIG_PATH": str(DSH_REPO / "tsconfig.json"),
    })
    command = ["node", "--import", str(tsx_loader), str(entrypoint), "--profile", "headless", "--patch", str(patch_path), combined_prompt]
    completed = run_command(command, workspace, env, run_dir / "deepseek-harness.log", token)
    session_files = list((workspace / ".sessions").rglob("*.jsonl"))
    session_text = "".join(path.read_text(errors="replace") for path in session_files)
    tool_calls = session_text.count('"type":"tool/call"')
    tool_results = session_text.count('"type":"tool/result"')
    if tool_calls < 2 or tool_results < 2:
        raise ExperimentError(f"insufficient persisted tool round trips: calls={tool_calls}, results={tool_results}")
    return {
        "version": json.loads((DSH_REPO / "package.json").read_text())["version"],
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=DSH_REPO, text=True, capture_output=True, check=True).stdout.strip(),
        "conversation_mode": "one headless task with repeated model/tool/result rounds",
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "session_files": len(session_files),
        "artifacts": verify_agent_artifacts(workspace),
        "seconds": completed.elapsed,  # type: ignore[attr-defined]
    }


def build_markdown(report: dict[str, Any]) -> str:
    lines = ["# Agent compatibility result", "", f"- Started: `{report['started_at']}`", f"- Model: `{MODEL}`", "- Endpoint credentials: loaded from environment; not recorded", "", "## Protocol", "", "| Case | Status | Seconds | Detail |", "|---|---:|---:|---|"]
    for item in report["protocol"]:
        detail = item.get("error") or json.dumps(item.get("detail", {}), ensure_ascii=False)
        lines.append(f"| `{item['name']}` | {item['status']} | {item['seconds']} | {detail.replace('|', '/')} |")
    lines += ["", "## Agents", "", "| Agent | Status | Seconds | Evidence |", "|---|---:|---:|---|"]
    for name, item in report["agents"].items():
        detail = item.get("error") or json.dumps(item.get("detail", {}), ensure_ascii=False)
        lines.append(f"| {name} | {item['status']} | {item['seconds']} | {detail.replace('|', '/')[:800]} |")
    warnings = []
    inline = next((item for item in report["protocol"] if item["name"] == "anthropic_inline_system"), None)
    if inline and inline.get("detail", {}).get("inline_system_behavior") == "dropped":
        warnings.append("LiteLLM accepted the inline-system request but the marker was dropped, matching issue #2's known semantic gap.")
    lines += ["", "## Interpretation", ""]
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    failures = [item["name"] for item in report["protocol"] if item["status"] != "pass"] + [name for name, item in report["agents"].items() if item["status"] != "pass"]
    lines.append(f"- Overall: {'FAIL (' + ', '.join(failures) + ')' if failures else 'PASS for the exercised matrix, with any semantic warning above.'}")
    return "\n".join(lines) + "\n"


def main() -> int:
    global MODEL, DSH_REPO
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("PRIVATELLM_MODEL", DEFAULT_MODEL))
    parser.add_argument("--dsh-repo", type=Path, default=DSH_REPO)
    parser.add_argument("--skip-hermes", action="store_true")
    parser.add_argument("--protocol-only", action="store_true")
    parser.add_argument("--agents-only", action="store_true")
    parser.add_argument("--agent", action="append", choices=("claude", "pi", "hermes", "dsh"))
    args = parser.parse_args()
    if not args.model.strip():
        parser.error("--model must not be empty")
    MODEL = args.model.strip()
    DSH_REPO = args.dsh_repo.expanduser().resolve()
    if args.protocol_only and args.agents_only:
        parser.error("--protocol-only and --agents-only are mutually exclusive")
    raw_base = os.environ.get("PRIVATELLM_BASE_URL", "")
    token = os.environ.get("PRIVATELLM_API_TOKEN", "")
    if not raw_base or not token:
        parser.error("PRIVATELLM_BASE_URL and PRIVATELLM_API_TOKEN are required")
    base = api_root(raw_base)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = ROOT / "results" / stamp
    run_dir.mkdir(parents=True)
    report: dict[str, Any] = {
        "started_at": utc_now(),
        "model": MODEL,
        "protocol": [] if args.agents_only else protocol_tests(base, token),
        "agents": {},
    }
    if not args.protocol_only:
        selected = set(args.agent or ("claude", "pi", "hermes", "dsh"))
        if "claude" in selected:
            report["agents"]["Claude Code"] = run_case("claude-code", lambda: claude_test(run_dir, base, token))
        if "pi" in selected:
            report["agents"]["pi"] = run_case("pi", lambda: pi_test(run_dir, base, token))
        if "hermes" in selected and not args.skip_hermes:
            report["agents"]["Hermes Agent"] = run_case("hermes-agent", lambda: hermes_test(run_dir, base, token))
        if "dsh" in selected:
            report["agents"]["DeepSeek Harness"] = run_case("deepseek-harness", lambda: deepseek_harness_test(run_dir, base, token))
    report["finished_at"] = utc_now()
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (run_dir / "report.md").write_text(build_markdown(report))
    print(run_dir / "report.md")
    failed = any(item["status"] != "pass" for item in report["protocol"]) or any(item["status"] != "pass" for item in report["agents"].values())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
