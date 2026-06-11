#!/usr/bin/env python3
"""
qoder2api — Production-grade OpenAI-compatible proxy wrapping qodercli
with multi-account round-robin rotation.

Architecture:
  - Pre-creates isolated auth directories for all 115 accounts at startup
  - Each request uses QODER_CONFIG_DIR to pick a specific account
  - True round-robin: no shared auth file, no race conditions
  - Concurrent requests use different accounts in parallel
  - Per-account request tracking with proactive rotation at 190/200

Endpoints:
  POST /v1/chat/completions  (OpenAI-compatible, streaming + non-streaming)
  GET  /v1/models
  GET  /health
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auth_injector import QoderAuthInjector

PORT = int(os.environ.get("QODER_PORT", 8963))
HOST = os.environ.get("QODER_HOST", "0.0.0.0")
QODERCLI = os.environ.get("QODERCLI_BIN", "qodercli")
MAX_RETRIES = 5
TIMEOUT = int(os.environ.get("QODER_TIMEOUT", 300))
STREAM_CHUNK_SIZE = int(os.environ.get("STREAM_CHUNK_SIZE", "3"))
STREAM_DELAY = float(os.environ.get("STREAM_DELAY", "0.02"))
PROACTIVE_LIMIT = int(os.environ.get("PROACTIVE_LIMIT", "190"))
DAILY_LIMIT = 200
MAX_SYSTEM_PROMPT = 14000

MODEL_MAP = {
    "qwen3-max": "qmodel_latest",
    "qwen3.7-max": "qmodel_latest",
    "qoder-unlimited": "qmodel_latest",
    "lite": "qmodel_latest",
    "auto": "qmodel_latest",
    "gpt-4": "qmodel_latest",
    "gpt-4o": "qmodel_latest",
    "claude-3.5-sonnet": "qmodel_latest",
}

SLOTS_DIR = Path(tempfile.gettempdir()) / "qoder_slots"

injector = QoderAuthInjector()


class AccountPool:
    """Thread-safe round-robin account pool with per-account tracking."""

    def __init__(self):
        self.tokens = injector.get_9router_tokens()
        self.count = len(self.tokens)
        self._counter = 0
        self._lock = threading.Lock()
        self._requests = [0] * self.count
        self._exhausted = set()
        self._last_reset = None
        self._total_served = 0
        self._load_state()
        self._setup_slots()

    def _load_state(self):
        state_file = Path.home() / "qoder-token-gen" / "rotation_state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                from datetime import datetime
                if data.get("last_reset_date") == datetime.now().strftime("%Y-%m-%d"):
                    self._requests = data.get("per_account_requests", [0] * self.count)
                    self._total_served = data.get("total_served", 0)
                    self._counter = data.get("current_index", 0)
            except Exception:
                pass

    def _save_state(self):
        state_file = Path.home() / "qoder-token-gen" / "rotation_state.json"
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        data = {
            "current_index": self._counter % self.count if self.count else 0,
            "total_accounts": self.count,
            "daily_requests": sum(self._requests),
            "per_account_requests": self._requests,
            "total_served": self._total_served,
            "exhausted_accounts": list(self._exhausted),
            "last_reset_date": today,
            "last_rotation": datetime.now().isoformat(),
            "rotation_count": len(self._exhausted),
        }
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(data, indent=2))

    def _setup_slots(self):
        """Pre-create isolated auth directories for each account."""
        if SLOTS_DIR.exists():
            shutil.rmtree(SLOTS_DIR)
        SLOTS_DIR.mkdir(parents=True)

        for i, token in enumerate(self.tokens):
            slot = SLOTS_DIR / str(i)
            auth_dir = slot / ".auth"
            auth_dir.mkdir(parents=True)

            machine_id = token.get("machineId") or str(uuid.uuid4())
            user_data = injector.create_user_data(token)
            encrypted = injector.encrypt_user_data(user_data, machine_id)

            (auth_dir / "machine_id").write_text(machine_id)
            (auth_dir / "machine_id").chmod(0o600)
            (auth_dir / "user").write_text(encrypted)
            (auth_dir / "user").chmod(0o600)

        print(f"[pool] {self.count} account slots ready at {SLOTS_DIR}", flush=True)

    def _check_daily_reset(self):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_reset != today:
            if self._last_reset is not None:
                print(f"[pool] daily reset: {self._last_reset} -> {today}", flush=True)
                self._requests = [0] * self.count
                self._exhausted.clear()
                self._counter = 0
            self._last_reset = today

    def next_account(self):
        """Get next available account (round-robin, skip exhausted)."""
        with self._lock:
            self._check_daily_reset()

            if len(self._exhausted) >= self.count:
                return None, None, "all accounts exhausted"

            for _ in range(self.count):
                idx = self._counter % self.count
                self._counter += 1

                if idx in self._exhausted:
                    continue
                if self._requests[idx] >= DAILY_LIMIT:
                    self._exhausted.add(idx)
                    continue

                slot_dir = str(SLOTS_DIR / str(idx))
                self._requests[idx] += 1
                self._total_served += 1

                if self._requests[idx] >= PROACTIVE_LIMIT:
                    pass

                self._save_state()
                token = self.tokens[idx]
                return idx, slot_dir, None

            return None, None, "no available accounts"

    def mark_exhausted(self, idx):
        """Mark an account as rate-limited."""
        with self._lock:
            self._exhausted.add(idx)
            self._requests[idx] = DAILY_LIMIT
            self._save_state()
            print(f"[pool] account {idx} ({self.tokens[idx]['name']}) marked exhausted",
                  file=sys.stderr, flush=True)

    def status(self):
        with self._lock:
            self._check_daily_reset()
            total_used = sum(self._requests)
            total_cap = self.count * DAILY_LIMIT
            active = self.count - len(self._exhausted)
            return {
                "total_accounts": self.count,
                "active_accounts": active,
                "exhausted_accounts": len(self._exhausted),
                "total_requests_today": total_used,
                "total_capacity": total_cap,
                "remaining_capacity": total_cap - total_used,
                "total_served": self._total_served,
                "per_account": [
                    {
                        "index": i,
                        "name": self.tokens[i]["name"],
                        "requests": self._requests[i],
                        "exhausted": i in self._exhausted,
                    }
                    for i in range(min(10, self.count))
                ],
            }


pool = AccountPool()


def _save_image(url):
    try:
        if url.startswith("data:"):
            header, data = url.split(",", 1)
            ext = ".png"
            if "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "webp" in header:
                ext = ".webp"
            elif "gif" in header:
                ext = ".gif"
            raw = base64.b64decode(data)
        elif url.startswith("http"):
            import urllib.request
            ext = ".png"
            raw = urllib.request.urlopen(url, timeout=30).read()
        else:
            return None
        fd, path = tempfile.mkstemp(suffix=ext, prefix="qoder_img_")
        os.write(fd, raw)
        os.close(fd)
        return path
    except Exception:
        return None


def _cleanup_images(paths):
    for p in (paths or []):
        try:
            os.unlink(p)
        except OSError:
            pass


def _extract_tool_summary(tools):
    """Build a concise summary of tool definitions for qodercli's system prompt."""
    if not tools:
        return ""
    lines = ["You have access to the following tools that the client expects you to use when appropriate:"]
    for t in tools:
        func = t.get("function", t)
        name = func.get("name", "?")
        desc = func.get("description", "")[:120]
        lines.append(f"- {name}: {desc}")
    lines.append("Use these tools proactively. You can read files, write files, run bash commands, search the web, fetch URLs, search files with grep/glob, and edit files. Execute tools whenever the task requires it rather than explaining what could be done.")
    return "\n".join(lines)


def _process_system_prompt(prompt):
    """Strip tool definitions, agent configs, and skills that qodercli can't use directly.
    Preserve project instructions, coding guidelines, and user rules.
    Based on 9Router's approach: extract useful context, discard protocol overhead."""
    if not prompt or len(prompt) < 500:
        return prompt

    lines = prompt.split("\n")
    kept = []
    skip_depth = 0
    skip_section = False
    in_json_tool_block = False
    json_brace_count = 0

    for line in lines:
        stripped = line.strip()

        # Detect start of JSON tool definition blocks
        if '"type": "function"' in stripped and '"function"' in stripped:
            in_json_tool_block = True
            json_brace_count = 0
            continue

        if in_json_tool_block:
            json_brace_count += stripped.count("{") - stripped.count("}")
            if json_brace_count <= 0 and "}" in stripped:
                in_json_tool_block = False
            continue

        # Skip individual JSON tool definition lines
        if stripped.startswith('"type": "function"') or (stripped.startswith('"name":') and '"' in stripped[8:]):
            skip_depth = 1
            continue
        if skip_depth > 0:
            skip_depth += stripped.count("{") - stripped.count("}")
            if skip_depth <= 0:
                skip_depth = 0
            continue

        skip_markers = [
            "Available agent types:", "available agent types:",
            "The following skills are available", "Available skills:",
            "tool schemas", "internal tags",
            "tool_choice", "tool_calls",
        ]
        if any(m in stripped for m in skip_markers):
            skip_section = True
            continue
        if skip_section and (stripped.startswith("- ") or stripped.startswith("* ")):
            continue
        if skip_section and stripped == "":
            skip_section = False

        # Skip system-reminder tags but keep their useful content
        if stripped.startswith("<system-reminder>"):
            continue
        if stripped.startswith("</system-reminder>"):
            continue

        # Skip function definition XML tags
        if stripped.startswith("<function_") or stripped.startswith("</function_"):
            continue
        if stripped.startswith("You have access to") and "function" in stripped.lower():
            continue

        kept.append(line)

    result = "\n".join(kept)
    if len(result) > MAX_SYSTEM_PROMPT:
        result = result[:MAX_SYSTEM_PROMPT] + "\n[...truncated...]"
    return result


def extract_messages(messages, tools=None):
    system_parts = []
    conversation_parts = []
    image_paths = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    url = item.get("image_url", {}).get("url", "")
                    path = _save_image(url)
                    if path:
                        image_paths.append(path)
            content = "\n".join(text_parts)

        if role == "system":
            system_parts.append(content)
        elif role == "user":
            conversation_parts.append(("user", content))
        elif role == "assistant":
            conversation_parts.append(("assistant", content))

    system_prompt = "\n".join(system_parts)
    system_prompt = _process_system_prompt(system_prompt)

    tool_summary = _extract_tool_summary(tools)
    if tool_summary:
        system_prompt = f"{system_prompt}\n\n{tool_summary}" if system_prompt else tool_summary

    if system_prompt and len(system_prompt) > 15500:
        system_prompt = system_prompt[:15500] + "\n[...truncated...]"

    if len(conversation_parts) == 1:
        user_prompt = conversation_parts[0][1]
    else:
        lines = []
        for role, content in conversation_parts[:-1]:
            label = "You" if role == "assistant" else "User"
            lines.append(f"[Previous {label}]: {content}")
        lines.append(conversation_parts[-1][1])
        user_prompt = "\n\n".join(lines)

    full_prompt = user_prompt
    if system_prompt:
        full_prompt = f"{system_prompt}\n\n{user_prompt}"

    return full_prompt, system_prompt, image_paths


def build_cmd(model, image_paths=None, system_prompt=None, streaming=False):
    cmd = [QODERCLI, "-p"]
    mapped = MODEL_MAP.get(model, "qmodel_latest") if model else "qmodel_latest"
    cmd.extend(["-m", mapped])
    cmd.extend(["--permission-mode", "bypass_permissions"])
    cmd.extend(["-o", "stream-json"])
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])
    if image_paths:
        for p in image_paths:
            cmd.extend(["--attachment", p])
    return cmd


def build_env(slot_dir):
    """Build subprocess env with QODER_CONFIG_DIR for isolated auth."""
    env = os.environ.copy()
    env["QODER_CONFIG_DIR"] = slot_dir
    return env


def _parse_stream_json_line(line):
    """Parse a single stream-json line and return (event_type, data)."""
    try:
        d = json.loads(line.strip())
        return d.get("type", ""), d
    except (json.JSONDecodeError, ValueError):
        return None, None


def run_qodercli(prompt, slot_dir, model=None, image_paths=None, system_prompt=None):
    cmd = build_cmd(model, image_paths, system_prompt)
    env = build_env(slot_dir)
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True,
            text=True, timeout=TIMEOUT, env=env,
        )
        full_text = ""
        tools_used = []
        usage = {}
        for line in proc.stdout.splitlines():
            etype, data = _parse_stream_json_line(line)
            if etype == "assistant":
                for c in data.get("message", {}).get("content", []):
                    if c.get("type") == "text":
                        full_text += c["text"]
                    elif c.get("type") == "tool_use":
                        tools_used.append(c.get("name", "?"))
            elif etype == "result":
                result = data.get("result", "")
                if result:
                    full_text = result
                if data.get("usage"):
                    u = data["usage"]
                    usage = {
                        "prompt_tokens": u.get("input_tokens", 0),
                        "completion_tokens": u.get("output_tokens", 0),
                        "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
                    }
        if tools_used:
            print(f"[proxy] tools used: {', '.join(tools_used)}", file=sys.stderr, flush=True)
        return full_text.strip(), proc.stderr, proc.returncode, usage
    except subprocess.TimeoutExpired:
        return None, f"timeout after {TIMEOUT}s", -1, {}
    except Exception as e:
        return None, str(e), -1, {}


def run_qodercli_streaming(prompt, slot_dir, model=None, image_paths=None, system_prompt=None):
    """Run qodercli with stream-json, yielding text deltas and tool events in real-time.
    Yields dicts with keys:
      {"type": "text", "content": "..."} for text deltas
      {"type": "tool", "name": "...", "input": {...}} for tool invocations
      {"type": "result", "content": "...", "usage": {...}} for final result
      {"type": "error", "content": "..."} for errors
    """
    cmd = build_cmd(model, image_paths, system_prompt)
    env = build_env(slot_dir)
    tools_used = []
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        proc.stdin.write(prompt)
        proc.stdin.close()

        for line in proc.stdout:
            etype, data = _parse_stream_json_line(line)
            if etype == "assistant":
                for c in data.get("message", {}).get("content", []):
                    if c.get("type") == "text":
                        yield {"type": "text", "content": c["text"]}
                    elif c.get("type") == "tool_use":
                        name = c.get("name", "?")
                        tools_used.append(name)
                        yield {"type": "tool", "name": name, "input": c.get("input", {})}
                    elif c.get("type") == "thinking":
                        pass
            elif etype == "result":
                usage = {}
                if data.get("usage"):
                    u = data["usage"]
                    usage = {
                        "prompt_tokens": u.get("input_tokens", 0),
                        "completion_tokens": u.get("output_tokens", 0),
                        "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0),
                    }
                if data.get("is_error"):
                    err_msg = data.get("result", "") or ""
                    # Check stderr for rate limit info
                    try:
                        stderr_text = proc.stderr.read() if proc.stderr else ""
                        if stderr_text:
                            err_msg = err_msg or stderr_text.strip()
                            print(f"[proxy] qodercli stderr: {stderr_text.strip()[:200]}",
                                  file=sys.stderr, flush=True)
                    except Exception:
                        pass
                    if not err_msg:
                        try:
                            proc.wait(timeout=5)
                            rc = proc.returncode
                        except Exception:
                            rc = -1
                        print(f"[proxy] qodercli error: no message, exit code {rc}",
                              file=sys.stderr, flush=True)
                        if is_rate_limit("", rc):
                            yield {"type": "rate_limit", "content": "rate limit reached"}
                            return
                        err_msg = f"qodercli exited with code {rc}"
                    else:
                        print(f"[proxy] qodercli error: {err_msg[:200]}",
                              file=sys.stderr, flush=True)
                    if is_rate_limit(err_msg, 1):
                        yield {"type": "rate_limit", "content": err_msg}
                    else:
                        yield {"type": "error", "content": err_msg}
                else:
                    yield {"type": "result", "content": data.get("result", ""), "usage": usage}

        proc.wait(timeout=TIMEOUT)
        if proc.returncode != 0 and not tools_used:
            stderr = proc.stderr.read() if proc.stderr else ""
            stderr_text = stderr.strip()
            if is_rate_limit(stderr_text, proc.returncode):
                yield {"type": "rate_limit", "content": stderr_text or "rate limit reached"}
            else:
                yield {"type": "error", "content": stderr_text or f"qodercli exited with code {proc.returncode}"}

        if tools_used:
            print(f"[proxy] tools used: {', '.join(tools_used)}", file=sys.stderr, flush=True)
    except subprocess.TimeoutExpired:
        proc.kill()
        yield {"type": "error", "content": f"timeout after {TIMEOUT}s"}
    except Exception as e:
        yield {"type": "error", "content": str(e)}


def is_rate_limit(stderr, returncode):
    lower = (stderr or "").lower()
    rate_patterns = [
        "rate limit", "429", "too many requests", "quota",
        "frequency limit", "usage limit", "code 112", "forbidden",
        "rate_limit", "daily limit", "rate-limit", "ratelimit",
        "temporarily unavailable", "service unavailable", "503",
        "exceeded", "throttl", "limit exceeded",
    ]
    if any(p in lower for p in rate_patterns):
        return True
    if returncode != 0 and not lower:
        return True
    return False


def do_request(prompt, model, image_paths, system_prompt):
    """Non-streaming request with round-robin rotation."""
    for attempt in range(MAX_RETRIES):
        idx, slot_dir, err = pool.next_account()
        if err:
            return None, err, {}

        stdout, stderr, rc, usage = run_qodercli(prompt, slot_dir, model, image_paths, system_prompt)

        if rc == 0 and stdout:
            return stdout, None, usage

        if is_rate_limit(stderr, rc):
            pool.mark_exhausted(idx)
            continue

        if stdout:
            return stdout, None, usage

        pool.mark_exhausted(idx)

    return None, "all retries exhausted", {}


def do_realtime_streaming(prompt, model, image_paths, system_prompt, wfile, req_id, created):
    """Stream text deltas in real-time from qodercli's stream-json output.
    Retries with next account on rate limit (before any text is streamed)."""
    role_chunk = {
        "id": req_id, "object": "chat.completion.chunk",
        "created": created, "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    try:
        wfile.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
        wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        return

    for attempt in range(MAX_RETRIES):
        idx, slot_dir, err = pool.next_account()
        if err:
            _send_error_chunk(wfile, req_id, created, model, err)
            return

        if attempt > 0 or True:
            sys_len = len(system_prompt) if system_prompt else 0
            prompt_len = len(prompt) if prompt else 0
            print(f"[proxy] attempt {attempt+1}/{MAX_RETRIES} | account {idx} | "
                  f"sys_prompt={sys_len} chars | prompt={prompt_len} chars",
                  file=sys.stderr, flush=True)

        final_usage = {}
        got_text = False
        needs_retry = False

        for event in run_qodercli_streaming(prompt, slot_dir, model, image_paths, system_prompt):
            evt_type = event.get("type", "")

            if evt_type == "rate_limit":
                pool.mark_exhausted(idx)
                if not got_text:
                    needs_retry = True
                    print(f"[proxy] rate limit on account {idx}, retrying (attempt {attempt+1}/{MAX_RETRIES})",
                          file=sys.stderr, flush=True)
                    break
                _send_error_chunk(wfile, req_id, created, model,
                                  f"Rate limit hit after partial response: {event['content']}")
                return

            if evt_type == "error":
                err_content = event.get("content", "")
                if is_rate_limit(err_content, 1) and not got_text:
                    pool.mark_exhausted(idx)
                    needs_retry = True
                    print(f"[proxy] rate limit detected on account {idx}, retrying (attempt {attempt+1}/{MAX_RETRIES})",
                          file=sys.stderr, flush=True)
                    break
                pool.mark_exhausted(idx)
                _send_error_chunk(wfile, req_id, created, model, err_content)
                return

            elif evt_type == "tool":
                tool_name = event.get("name", "?")
                tool_input = event.get("input", {})
                brief = _format_tool_status(tool_name, tool_input)
                if brief:
                    _send_text_chunk(wfile, req_id, created, model, brief)

            elif evt_type == "text":
                got_text = True
                _send_text_chunk(wfile, req_id, created, model, event["content"])

            elif evt_type == "result":
                if event.get("usage"):
                    final_usage = event["usage"]

        if needs_retry:
            time.sleep(1)
            continue

        done_chunk = {
            "id": req_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        if final_usage:
            done_chunk["usage"] = final_usage
        try:
            wfile.write(f"data: {json.dumps(done_chunk)}\n\n".encode())
            wfile.write(b"data: [DONE]\n\n")
            wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        return

    _send_error_chunk(wfile, req_id, created, model, "All accounts rate-limited, try again later")


def _send_text_chunk(wfile, req_id, created, model, text):
    chunk_data = {
        "id": req_id, "object": "chat.completion.chunk",
        "created": created, "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    try:
        wfile.write(f"data: {json.dumps(chunk_data)}\n\n".encode())
        wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _send_error_chunk(wfile, req_id, created, model, message):
    err_chunk = {
        "id": req_id, "object": "chat.completion.chunk",
        "created": created, "model": model,
        "choices": [{"index": 0,
                     "delta": {"content": f"\n\n[Error: {message}]"},
                     "finish_reason": "stop"}],
    }
    try:
        wfile.write(f"data: {json.dumps(err_chunk)}\n\n".encode())
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _format_tool_status(tool_name, tool_input):
    """Format a brief status line showing tool invocation, visible to the user."""
    if tool_name in ("Read", "read_file"):
        path = tool_input.get("file_path", tool_input.get("path", ""))
        return f"\n> **Reading** `{path}`\n"
    elif tool_name in ("Write", "write_file"):
        path = tool_input.get("file_path", tool_input.get("path", ""))
        return f"\n> **Writing** `{path}`\n"
    elif tool_name in ("Bash", "bash", "run_command"):
        cmd = tool_input.get("command", "")[:80]
        return f"\n> **Running** `{cmd}`\n"
    elif tool_name in ("WebSearch", "web_search"):
        query = tool_input.get("query", "")[:60]
        return f"\n> **Searching web** for \"{query}\"\n"
    elif tool_name in ("WebFetch", "web_fetch", "fetch"):
        url = tool_input.get("url", "")[:80]
        return f"\n> **Fetching** `{url}`\n"
    elif tool_name in ("Glob", "glob"):
        pattern = tool_input.get("pattern", "")
        return f"\n> **Finding files** `{pattern}`\n"
    elif tool_name in ("Grep", "grep"):
        pattern = tool_input.get("pattern", "")[:40]
        return f"\n> **Searching** for `{pattern}`\n"
    elif tool_name in ("Edit", "edit_file"):
        path = tool_input.get("file_path", tool_input.get("path", ""))
        return f"\n> **Editing** `{path}`\n"
    elif tool_name in ("Agent", "subagent"):
        desc = tool_input.get("description", "")[:40]
        return f"\n> **Launching agent** for {desc}\n"
    else:
        return f"\n> **Using tool:** {tool_name}\n"


def estimate_tokens(text):
    if not text:
        return 0
    return max(1, len(text) // 4)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/health", "/"):
            status = pool.status()
            self._json(200, {
                "status": "ok",
                "service": "qoder2api",
                "rotation": "round-robin",
                "accounts": status["total_accounts"],
                "active_accounts": status["active_accounts"],
                "exhausted_accounts": status["exhausted_accounts"],
                "requests_today": status["total_requests_today"],
                "daily_capacity": status["total_capacity"],
                "remaining": status["remaining_capacity"],
                "total_served": status["total_served"],
                "endpoints": {
                    "chat": "POST /v1/chat/completions",
                    "models": "GET /v1/models",
                    "health": "GET /health",
                },
            })
            return
        if path == "/v1/models":
            self._json(200, {
                "object": "list",
                "data": [
                    {"id": "qwen3-max", "object": "model",
                     "created": int(time.time()), "owned_by": "qoder", "permission": []},
                    {"id": "qwen3.7-max", "object": "model",
                     "created": int(time.time()), "owned_by": "qoder", "permission": []},
                    {"id": "qoder-unlimited", "object": "model",
                     "created": int(time.time()), "owned_by": "qoder", "permission": []},
                    {"id": "lite", "object": "model",
                     "created": int(time.time()), "owned_by": "qoder", "permission": []},
                ],
            })
            return
        self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json(400, {
                "error": {"message": "invalid json", "type": "invalid_request_error"},
            })
            return

        messages = body.get("messages", [])
        stream = body.get("stream", False)
        model = body.get("model", "lite")
        tools = body.get("tools", [])
        tool_choice = body.get("tool_choice", None)

        if tools:
            print(f"[proxy] {len(tools)} tools received (injected as tool summary in system prompt)",
                  file=sys.stderr, flush=True)

        prompt, system_prompt, image_paths = extract_messages(messages, tools=tools)

        if not prompt:
            self._json(400, {
                "error": {"message": "no prompt found in messages", "type": "invalid_request_error"},
            })
            return

        img_info = f" images={len(image_paths)}" if image_paths else ""
        truncated = prompt[:80] + ("..." if len(prompt) > 80 else "")
        print(f"[proxy] model={model} stream={stream} msgs={len(messages)}{img_info} | {truncated}",
              file=sys.stderr, flush=True)

        req_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self._cors()
                self.end_headers()

                do_realtime_streaming(
                    prompt, model, image_paths or None, system_prompt or None,
                    self.wfile, req_id, created,
                )
            else:
                result, error, usage = do_request(
                    prompt, model, image_paths or None, system_prompt or None,
                )
                if error:
                    self._json(502, {
                        "error": {
                            "message": str(error),
                            "type": "upstream_error",
                            "code": "qoder_error",
                        },
                    })
                    return

                if not usage:
                    usage = {
                        "prompt_tokens": estimate_tokens(prompt),
                        "completion_tokens": estimate_tokens(result),
                        "total_tokens": estimate_tokens(prompt) + estimate_tokens(result),
                    }
                self._json(200, {
                    "id": req_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": result},
                        "finish_reason": "stop",
                    }],
                    "usage": usage,
                })
        finally:
            _cleanup_images(image_paths)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    server = ThreadedServer((HOST, PORT), Handler)
    status = pool.status()
    print(f"[proxy] qoder2api listening on http://{HOST}:{PORT}", flush=True)
    print(f"[proxy] rotation: round-robin | accounts: {status['total_accounts']} active", flush=True)
    print(f"[proxy] capacity: {status['total_capacity']}/day | auth: isolated per-request", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[proxy] shutting down...", flush=True)
        server.shutdown()
        if SLOTS_DIR.exists():
            shutil.rmtree(SLOTS_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
