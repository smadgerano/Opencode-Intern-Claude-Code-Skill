#!/usr/bin/env python
"""
ACP Client for OpenCode - delegates tasks via JSON-RPC over stdio.

Usage:
    python acp_client.py <project_path> "<prompt>" --json
"""

import subprocess
import json
import sys
import os
import threading
import queue
import time
import argparse
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field


@dataclass
class SessionState:
    """Tracks ACP session state."""
    session_id: Optional[str] = None
    is_complete: bool = False
    accumulated_text: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class ACPClient:
    """ACP Client for OpenCode. Handles: launch -> initialize -> session -> prompt -> shutdown"""

    PROTOCOL_VERSION = 1

    def __init__(self, project_path: str, agent: str = "intern", timeout: int = 300):
        self.project_path = os.path.abspath(project_path)
        self.agent = agent
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.pending_requests: Dict[int, queue.Queue] = {}
        self.state = SessionState()
        self.reader_thread: Optional[threading.Thread] = None
        self.running = False
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def _send(self, message: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("OpenCode process not running")
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        req_id = self._next_id()
        response_queue: queue.Queue = queue.Queue()

        with self._lock:
            self.pending_requests[req_id] = response_queue

        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            message["params"] = params
        self._send(message)

        try:
            return response_queue.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError(f"Timeout waiting for {method}")
        finally:
            with self._lock:
                self.pending_requests.pop(req_id, None)

    def _reader_loop(self) -> None:
        if not self.process or not self.process.stdout:
            return
        while self.running:
            try:
                line = self.process.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    self._handle_message(json.loads(line))
                except json.JSONDecodeError as e:
                    self.state.errors.append(f"JSON error: {e}")
            except Exception as e:
                if self.running:
                    self.state.errors.append(f"Reader error: {e}")
                break

    def _handle_message(self, message: Dict[str, Any]) -> None:
        # Route responses to pending requests
        if "id" in message and message["id"] is not None:
            req_id = message["id"]
            with self._lock:
                if req_id in self.pending_requests:
                    self.pending_requests[req_id].put(message)
                    return

        # Handle notifications
        method = message.get("method", "")
        if method == "session/update":
            self._handle_update(message.get("params", {}).get("update", {}))
        elif method == "session/complete":
            self.state.is_complete = True

    def _handle_update(self, update: Dict[str, Any]) -> None:
        update_type = update.get("sessionUpdate", "")

        if update_type == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                text = content.get("text", "")
                if text:
                    self.state.accumulated_text.append(text)

        elif update_type == "tool_call_update":
            tool_id = update.get("toolCallId")
            tool = {"id": tool_id, "name": update.get("title", update.get("kind")), "status": update.get("status")}
            # Update existing or append new
            for tc in self.state.tool_calls:
                if tc.get("id") == tool_id:
                    tc["status"] = tool["status"]
                    return
            self.state.tool_calls.append(tool)

        elif update_type == "status" and update.get("status") == "idle":
            self.state.is_complete = True

    def launch(self) -> bool:
        try:
            self.process = subprocess.Popen(
                ["opencode", "acp", "--cwd", self.project_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=os.environ.copy(),
                cwd=self.project_path,
            )
            self.running = True
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()
            # Brief pause for process to start (reader will handle the rest)
            time.sleep(0.5)
            return self.process.poll() is None
        except FileNotFoundError:
            self.state.errors.append("OpenCode not found in PATH")
            return False
        except Exception as e:
            self.state.errors.append(f"Launch failed: {e}")
            return False

    def initialize(self) -> bool:
        try:
            response = self._send_request("initialize", {
                "protocolVersion": self.PROTOCOL_VERSION,
                "clientCapabilities": {"permissions": True, "fileSystem": {"read": True, "write": True, "list": True}, "terminals": True},
                "clientInfo": {"name": "claude-acp-client", "version": "1.0.0"},
            })
            if "error" in response:
                self.state.errors.append(f"Init error: {response['error']}")
                return False
            return True
        except Exception as e:
            self.state.errors.append(f"Init failed: {e}")
            return False

    def create_session(self) -> bool:
        try:
            response = self._send_request("session/new", {
                "agent": self.agent,
                "cwd": self.project_path,
                "mcpServers": [],
            })
            if "error" in response:
                self.state.errors.append(f"Session error: {response['error']}")
                return False
            self.state.session_id = response.get("result", {}).get("sessionId")
            return self.state.session_id is not None
        except Exception as e:
            self.state.errors.append(f"Session failed: {e}")
            return False

    def send_prompt(self, prompt: str) -> bool:
        if not self.state.session_id:
            self.state.errors.append("No session")
            return False
        try:
            self.state.accumulated_text = []
            self.state.tool_calls = []
            self.state.is_complete = False

            response = self._send_request("session/prompt", {
                "sessionId": self.state.session_id,
                "prompt": [{"type": "text", "text": prompt}],
            })

            if "error" in response:
                self.state.errors.append(f"Prompt error: {response['error']}")
                return False

            # stopReason in response means agent finished
            if response.get("result", {}).get("stopReason"):
                self.state.is_complete = True
                return True

            # Fallback: wait for is_complete
            start = time.time()
            while not self.state.is_complete:
                if time.time() - start > self.timeout:
                    self.state.errors.append("Timeout")
                    return False
                time.sleep(0.1)
            return True
        except Exception as e:
            self.state.errors.append(f"Prompt failed: {e}")
            return False

    def shutdown(self) -> None:
        self.running = False
        if self.process:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
            finally:
                self.process = None
        if self.reader_thread:
            self.reader_thread.join(timeout=2)

    def run(self, prompt: str) -> Dict[str, Any]:
        """Full lifecycle: launch -> initialize -> session -> prompt -> shutdown."""
        result = {"success": False, "response": "", "tools": [], "errors": []}
        try:
            for step, fn in [("launch", self.launch), ("init", self.initialize), ("session", self.create_session)]:
                if not fn():
                    result["errors"] = self.state.errors
                    return result

            if not self.send_prompt(prompt):
                result["errors"] = self.state.errors
                return result

            result["success"] = True
            result["response"] = "".join(self.state.accumulated_text)
            result["tools"] = [{"name": tc.get("name"), "status": tc.get("status")} for tc in self.state.tool_calls]
        finally:
            self.shutdown()
        return result


def main():
    parser = argparse.ArgumentParser(description="Send prompt to OpenCode intern via ACP")
    parser.add_argument("project_path", help="Project directory path")
    parser.add_argument("prompt", help="Task for the intern")
    parser.add_argument("--agent", default="intern", help="Agent name (default: intern)")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout seconds (default: 300)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    client = ACPClient(args.project_path, args.agent, args.timeout)
    result = client.run(args.prompt)

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["success"]:
        print(result["response"])
        if result["tools"]:
            print("\nTools:", ", ".join(f"{t['name']}" for t in result["tools"]))
    else:
        print("Errors:", "; ".join(result["errors"]), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
