#!/usr/bin/env python
"""
ACP Client - delegates tasks to the intern agent via JSON-RPC over stdio.

Usage:
    python acp_client.py <project_path> "<prompt>" --json
    python acp_client.py <project_path> "<prompt>" --json --debug  # Enable debug logging
"""

import subprocess
import json
import sys
import os
import re
import threading
import queue
import time
import argparse
import uuid
import signal
import tempfile
import glob as glob_module
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    """Status constants for background tasks."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    """Status constants for agent state."""
    UNKNOWN = "unknown"
    IDLE = "idle"
    ERROR = "error"
    END_TURN = "end_turn"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"


def normalize_windows_paths(text: str) -> str:
    """
    Convert Windows-style file paths in text to use forward slashes.

    Handles:
    - Absolute paths: C:\\Users\\name\\file.txt -> C:/Users/name/file.txt
    - UNC paths: \\\\server\\share\\file.txt -> //server/share/file.txt

    Preserves escape sequences, regex patterns, and other backslash uses
    by only targeting patterns that clearly look like file paths.

    The patterns are designed to be conservative:
    - Absolute paths must start with drive letter + colon + backslash
    - UNC paths must start with \\\\ followed by server\\share
    - Path characters exclude quotes, wildcards, pipes, and control chars
    """

    def replace_backslashes(match: re.Match) -> str:
        return match.group(0).replace("\\", "/")

    # Windows absolute path pattern: C:\path\to\file
    # Greedy pattern that stops at whitespace, quotes, pipes, wildcards, angle brackets
    abs_path_pattern = r"[A-Za-z]:\\(?:[^\s\"'<>|*?\r\n]+)"

    # UNC path pattern: \\server\share[\path...]
    # - \\\\\\\\ matches the leading \\ (escaped for regex + Python string)
    # - Server and share names follow Windows naming rules
    unc_path_pattern = (
        r"\\\\"  # Leading \\
        r"[^\s\"'<>|*?\r\n\\]+"  # Server name
        r"(?:\\[^\s\"'<>|*?\r\n\\]+)+"  # \share and additional segments
    )

    # Apply UNC pattern first (more specific, starts with \\)
    result = re.sub(unc_path_pattern, replace_backslashes, text)
    # Then absolute paths (starts with X:\)
    result = re.sub(abs_path_pattern, replace_backslashes, result)

    return result


@dataclass
class SessionState:
    """Tracks ACP session state."""
    session_id: Optional[str] = None
    is_complete: bool = False
    has_error: bool = False
    accumulated_text: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    last_activity: float = field(default_factory=time.time)
    agent_status: str = AgentStatus.UNKNOWN


class ACPClient:
    """ACP Client for OpenCode. Handles: launch -> initialize -> session -> prompt -> shutdown

    Features:
    - Thread-safe request handling
    - Process health monitoring
    - Activity timeout detection (detects hung agents)
    - Error status detection from agent updates
    - Session cancellation support
    - Debug logging option
    """

    PROTOCOL_VERSION = 1
    DEFAULT_ACTIVITY_TIMEOUT = 120  # Max seconds without any activity before considering agent hung
    MAX_PROMPT_LENGTH = 100000  # 100KB max prompt size

    def __init__(
        self,
        project_path: str,
        agent: str = "intern",
        timeout: int = 300,
        activity_timeout: Optional[int] = None,
        debug: bool = False,
    ):
        self.project_path = os.path.abspath(project_path)
        self.agent = agent
        self.timeout = timeout
        self.activity_timeout = activity_timeout or self.DEFAULT_ACTIVITY_TIMEOUT
        self.debug = debug
        self.process: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._id_lock = threading.Lock()
        self.pending_requests: Dict[int, queue.Queue] = {}
        self.state = SessionState()
        self.reader_thread: Optional[threading.Thread] = None
        self.stderr_thread: Optional[threading.Thread] = None
        self.running = False
        self._lock = threading.Lock()  # For pending_requests
        self._state_lock = threading.Lock()  # For state modifications (thread safety)
        self._stderr_buffer: List[str] = []

    def _log(self, message: str) -> None:
        """Debug logging."""
        if self.debug:
            print(f"[ACP DEBUG] {message}", file=sys.stderr)

    def _next_id(self) -> int:
        """Thread-safe request ID generation."""
        with self._id_lock:
            self._request_id += 1
            return self._request_id

    def _send(self, message: Dict[str, Any]) -> None:
        """Send a JSON-RPC message to the agent."""
        if not self.process or not self.process.stdin:
            raise RuntimeError("OpenCode process not running")
        msg_str = json.dumps(message)
        self._log(f"SEND: {msg_str[:200]}...")
        self.process.stdin.write(msg_str + "\n")
        self.process.stdin.flush()

    def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Send a request and wait for response."""
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

    def _send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a notification (no response expected)."""
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        self._send(message)

    def _stderr_reader_loop(self) -> None:
        """Read and buffer stderr for debugging."""
        if not self.process or not self.process.stderr:
            return
        while self.running:
            try:
                line = self.process.stderr.readline()
                if not line:
                    break
                line = line.rstrip()
                if line:
                    self._stderr_buffer.append(line)
                    self._log(f"STDERR: {line[:200]}")
                    # Detect error patterns in stderr
                    if "ERROR" in line or "error" in line.lower():
                        if "API" in line or "llm" in line.lower():
                            self.state.has_error = True
            except Exception:
                break

    def _reader_loop(self) -> None:
        """Read and process stdout messages from the agent."""
        if not self.process or not self.process.stdout:
            return
        while self.running:
            try:
                line = self.process.stdout.readline()
                if not line:
                    self._log("Reader: EOF on stdout")
                    break
                line = line.strip()
                if not line:
                    continue
                self._log(f"RECV: {line[:200]}...")
                # Update activity timestamp
                self.state.last_activity = time.time()
                try:
                    self._handle_message(json.loads(line))
                except json.JSONDecodeError as e:
                    self.state.errors.append(f"JSON error: {e}")
            except Exception as e:
                if self.running:
                    self.state.errors.append(f"Reader error: {e}")
                break

    def _handle_message(self, message: Dict[str, Any]) -> None:
        # Check if this is a request from the agent (has id and method)
        method = message.get("method", "")
        msg_id = message.get("id")

        # Handle permission requests from agent (requires response)
        if method == "session/request_permission" and msg_id is not None:
            self._handle_permission_request(message)
            return

        # Route responses to our pending requests
        if msg_id is not None:
            with self._lock:
                if msg_id in self.pending_requests:
                    self.pending_requests[msg_id].put(message)
                    return

        # Handle notifications (no id)
        if method == "session/update":
            self._handle_update(message.get("params", {}).get("update", {}))
        elif method == "session/complete":
            with self._state_lock:
                self.state.is_complete = True

    def _handle_permission_request(self, message: Dict[str, Any]) -> None:
        """Auto-approve permission requests from the agent."""
        msg_id = message.get("id")
        params = message.get("params", {})
        permission = params.get("permission", "unknown")
        resource = params.get("resource", params.get("path", "unknown"))

        self._log(f"Permission request: {permission} for {resource} - auto-approving")

        # Send approval response
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"granted": True}
        }
        try:
            self._send(response)
        except Exception as e:
            self._log(f"Failed to send permission response: {e}")

    def _handle_update(self, update: Dict[str, Any]) -> None:
        """Handle session/update notifications from the agent (thread-safe)."""
        update_type = update.get("sessionUpdate", "")
        self._log(f"Update type: {update_type}")

        with self._state_lock:
            if update_type == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text = content.get("text", "")
                    if text:
                        self.state.accumulated_text.append(text)

            elif update_type == "tool_call_update":
                tool_id = update.get("toolCallId")
                tool_status = update.get("status", "unknown")
                tool = {"id": tool_id, "name": update.get("title", update.get("kind")), "status": tool_status}
                # Update existing or append new
                found = False
                for tc in self.state.tool_calls:
                    if tc.get("id") == tool_id:
                        tc["status"] = tool_status
                        found = True
                        break
                if not found:
                    self.state.tool_calls.append(tool)

            elif update_type == "status":
                status = update.get("status", "")
                self.state.agent_status = status
                self._log(f"Agent status: {status}")
                if status == AgentStatus.IDLE:
                    self.state.is_complete = True
                elif status == AgentStatus.ERROR:
                    self.state.has_error = True
                    error_msg = update.get("error", "Unknown agent error")
                    self.state.errors.append(f"Agent error: {error_msg}")

            elif update_type == "error":
                # Direct error updates
                self.state.has_error = True
                error_msg = update.get("message", update.get("error", "Unknown error"))
                self.state.errors.append(f"Update error: {error_msg}")

    def launch(self) -> bool:
        """Launch the OpenCode ACP server process."""
        self._log(f"Launching OpenCode ACP in {self.project_path}")
        try:
            popen_kwargs = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.process = subprocess.Popen(
                ["opencode", "acp", "--cwd", self.project_path],
                **popen_kwargs,
            )
            self.running = True
            self.state.last_activity = time.time()

            # Start reader threads
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()
            self.stderr_thread = threading.Thread(target=self._stderr_reader_loop, daemon=True)
            self.stderr_thread.start()

            # Brief pause for process to start
            time.sleep(0.5)
            if self.process.poll() is not None:
                self.state.errors.append(f"Process exited immediately with code {self.process.returncode}")
                return False
            self._log("OpenCode process started successfully")
            return True
        except FileNotFoundError:
            self.state.errors.append("OpenCode not found in PATH. Ensure 'opencode' is installed and in PATH.")
            return False
        except Exception as e:
            self.state.errors.append(f"Launch failed: {e}")
            return False

    def _check_process_health(self) -> bool:
        """Check if the agent process is still running."""
        if not self.process:
            return False
        poll = self.process.poll()
        if poll is not None:
            self._log(f"Process exited with code {poll}")
            return False
        return True

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
            # Use "." for cwd in session params - works cross-platform (Linux/Windows)
            # Absolute paths cause issues on Windows; "." is portable since
            # the --cwd flag to opencode already sets the working directory
            response = self._send_request("session/new", {
                "agent": self.agent,
                "cwd": ".",
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

    def cancel_session(self) -> None:
        """Send cancellation notification to abort current operation."""
        if self.state.session_id:
            self._log("Sending session/cancel notification")
            try:
                self._send_notification("session/cancel", {"sessionId": self.state.session_id})
            except Exception as e:
                self._log(f"Cancel failed: {e}")

    def send_prompt(self, prompt: str) -> bool:
        """Send a prompt to the agent and wait for completion."""
        if not self.state.session_id:
            self.state.errors.append("No session")
            return False

        # Validate prompt length to prevent DoS
        if len(prompt) > self.MAX_PROMPT_LENGTH:
            self.state.errors.append(f"Prompt exceeds maximum length of {self.MAX_PROMPT_LENGTH} characters")
            return False

        try:
            # Reset state for new prompt
            self.state.accumulated_text = []
            self.state.tool_calls = []
            self.state.is_complete = False
            self.state.has_error = False
            self.state.last_activity = time.time()

            # Normalize Windows paths in the prompt to forward slashes
            normalized_prompt = normalize_windows_paths(prompt)
            if normalized_prompt != prompt:
                self._log("Normalized Windows paths in prompt")

            self._log(f"Sending prompt: {normalized_prompt[:100]}...")
            response = self._send_request("session/prompt", {
                "sessionId": self.state.session_id,
                "prompt": [{"type": "text", "text": normalized_prompt}],
            })

            if "error" in response:
                error_detail = response.get("error", {})
                if isinstance(error_detail, dict):
                    error_msg = error_detail.get("message", str(error_detail))
                else:
                    error_msg = str(error_detail)
                self.state.errors.append(f"Prompt error: {error_msg}")
                return False

            # stopReason in response means agent finished
            result = response.get("result", {})
            stop_reason = result.get("stopReason")
            if stop_reason:
                self._log(f"Agent finished with stopReason: {stop_reason}")
                with self._state_lock:
                    self.state.is_complete = True
                    self.state.agent_status = stop_reason

                # Handle different stop reasons appropriately
                if stop_reason == AgentStatus.END_TURN:
                    return True  # Normal successful completion
                elif stop_reason == AgentStatus.REFUSAL:
                    self.state.errors.append("Agent refused to complete the task")
                    return False
                elif stop_reason == AgentStatus.CANCELLED:
                    self.state.errors.append("Session was cancelled")
                    return False
                elif stop_reason in (AgentStatus.MAX_TOKENS, AgentStatus.MAX_TURN_REQUESTS):
                    # Partial completion - response may be truncated
                    self._log(f"Warning: Agent stopped due to {stop_reason} - response may be incomplete")
                    return True  # Still return success, but with warning logged
                else:
                    # Unknown stop reason - treat as success but log
                    self._log(f"Unknown stopReason: {stop_reason}")
                    return True

            # No stopReason - this shouldn't happen with ACP protocol
            self._log("Warning: Response had no stopReason")
            return True
        except TimeoutError as e:
            self.state.errors.append(f"Prompt failed: {e}")
            self.cancel_session()
            return False
        except Exception as e:
            self.state.errors.append(f"Prompt failed: {e}")
            return False

    def shutdown(self) -> None:
        """Gracefully shutdown the agent process and close all resources."""
        self._log("Shutting down...")
        self.running = False
        if self.process:
            try:
                # Close stdin to signal EOF to the process
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.wait(timeout=5)
                self._log(f"Process exited with code {self.process.returncode}")
            except subprocess.TimeoutExpired:
                self._log("Process didn't exit, killing...")
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception as e:
                self._log(f"Shutdown exception: {e}")
                try:
                    self.process.kill()
                except Exception:
                    pass
            finally:
                # Close remaining pipes to prevent resource leaks
                try:
                    if self.process and self.process.stdout:
                        self.process.stdout.close()
                except Exception:
                    pass
                try:
                    if self.process and self.process.stderr:
                        self.process.stderr.close()
                except Exception:
                    pass
                self.process = None
        if self.reader_thread:
            self.reader_thread.join(timeout=2)
        if self.stderr_thread:
            self.stderr_thread.join(timeout=1)
        self._log("Shutdown complete")

    def get_stderr_log(self) -> List[str]:
        """Return captured stderr output for debugging."""
        return self._stderr_buffer.copy()

    def get_pid(self) -> Optional[int]:
        """Return the process ID of the opencode process."""
        return self.process.pid if self.process else None

    def run(self, prompt: str) -> Dict[str, Any]:
        """Full lifecycle: launch -> initialize -> session -> prompt -> shutdown.

        Returns:
            Dict with keys:
            - success: bool
            - response: str (agent's text response)
            - tools: List[Dict] (tool calls made)
            - errors: List[str] (any errors encountered)
            - agent_status: str (final agent status)
        """
        result = {
            "success": False,
            "response": "",
            "tools": [],
            "errors": [],
            "agent_status": AgentStatus.UNKNOWN,
        }
        try:
            steps = [
                ("launch", self.launch),
                ("init", self.initialize),
                ("session", self.create_session),
            ]
            for step_name, step_fn in steps:
                self._log(f"Executing step: {step_name}")
                if not step_fn():
                    self._log(f"Step {step_name} failed")
                    with self._state_lock:
                        result["errors"] = self.state.errors.copy()
                    return result

            if not self.send_prompt(prompt):
                self._log("Prompt execution failed")
                with self._state_lock:
                    result["errors"] = self.state.errors.copy()
                    result["agent_status"] = self.state.agent_status
                    # Include partial response if any
                    if self.state.accumulated_text:
                        result["response"] = "".join(self.state.accumulated_text)
                return result

            with self._state_lock:
                result["success"] = True
                result["response"] = "".join(self.state.accumulated_text)
                result["tools"] = [
                    {"name": tc.get("name"), "status": tc.get("status")}
                    for tc in self.state.tool_calls
                ]
                result["agent_status"] = self.state.agent_status
        finally:
            self.shutdown()
            # Include any errors that occurred during shutdown
            with self._state_lock:
                if self.state.errors and not result["errors"]:
                    result["errors"] = self.state.errors.copy()
        return result


# =============================================================================
# Task Manager for Background/Async Execution
# =============================================================================

@dataclass
class TaskInfo:
    """Information about a background task."""
    task_id: str
    status: str  # See TaskStatus enum for valid values
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    project_path: str = ""
    prompt: str = ""
    agent: str = "intern"
    timeout: int = 300
    activity_timeout: int = 120
    pid: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    debug: bool = False


class TaskManager:
    """Manages background task lifecycle for parallel intern execution.

    Features:
    - Launch tasks in background processes
    - Track task status via JSON state files
    - Query and retrieve task results
    - Cancel running tasks
    - Clean up old tasks

    Usage:
        manager = TaskManager()

        # Launch a background task
        task_id = manager.launch_background(".", "Fix the bug", timeout=300)

        # Check status
        info = manager.get_task_status(task_id)

        # Get result when complete
        if info.status == TaskStatus.COMPLETED:
            result = info.result

        # List all tasks
        tasks = manager.list_tasks()

        # Cancel a running task
        manager.cancel_task(task_id)
    """

    DEFAULT_TASKS_DIR = os.path.join(tempfile.gettempdir(), "acp_tasks")
    # Task IDs must be alphanumeric with hyphens only (UUID format)
    TASK_ID_PATTERN = re.compile(r'^[a-zA-Z0-9\-]+$')
    MAX_PROMPT_LENGTH = 100000  # 100KB max prompt size

    def __init__(self, tasks_dir: Optional[str] = None):
        self.tasks_dir = tasks_dir or self.DEFAULT_TASKS_DIR
        os.makedirs(self.tasks_dir, exist_ok=True)

    def _validate_task_id(self, task_id: str) -> bool:
        """Validate task ID format to prevent path traversal attacks.

        Task IDs must be alphanumeric with hyphens only, and reasonable length.
        This prevents malicious IDs like '../../../etc/passwd' or '..\\Windows\\System32'.
        """
        if not task_id or len(task_id) > 64:
            return False
        if not self.TASK_ID_PATTERN.match(task_id):
            return False
        # Additional check: ensure no path separators after pattern match
        if os.path.sep in task_id or '/' in task_id or '\\' in task_id:
            return False
        return True

    def _task_file(self, task_id: str) -> str:
        """Get the path to a task's state file.

        Raises ValueError if task_id is invalid (security check).
        """
        if not self._validate_task_id(task_id):
            raise ValueError(f"Invalid task ID format: {task_id!r}")
        return os.path.join(self.tasks_dir, f"{task_id}.json")

    def _lock_file(self, task_id: str) -> str:
        """Get the path to a task's lock file.

        Raises ValueError if task_id is invalid (security check).
        """
        if not self._validate_task_id(task_id):
            raise ValueError(f"Invalid task ID format: {task_id!r}")
        return os.path.join(self.tasks_dir, f"{task_id}.lock")

    def _read_task(self, task_id: str) -> Optional[TaskInfo]:
        """Read task info from disk.

        Returns None if task doesn't exist or task_id is invalid.
        """
        try:
            task_file = self._task_file(task_id)
        except ValueError:
            # Invalid task_id format - treat as non-existent
            return None
        if not os.path.exists(task_file):
            return None
        try:
            with open(task_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return TaskInfo(**data)
        except Exception:
            return None

    def _write_task(self, info: TaskInfo) -> None:
        """Write task info to disk (atomic)."""
        task_file = self._task_file(info.task_id)
        temp_file = task_file + ".tmp"
        try:
            # Convert dataclass to dict, handling nested dicts
            data = {
                "task_id": info.task_id,
                "status": info.status,
                "created_at": info.created_at,
                "started_at": info.started_at,
                "completed_at": info.completed_at,
                "project_path": info.project_path,
                "prompt": info.prompt,
                "agent": info.agent,
                "timeout": info.timeout,
                "activity_timeout": info.activity_timeout,
                "pid": info.pid,
                "result": info.result,
                "debug": info.debug,
            }
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            # Atomic rename
            os.replace(temp_file, task_file)
        except Exception:
            if os.path.exists(temp_file):
                os.remove(temp_file)
            raise

    def _generate_task_id(self) -> str:
        """Generate a unique task ID."""
        return str(uuid.uuid4())[:8]

    def launch_background(
        self,
        project_path: str,
        prompt: str,
        agent: str = "intern",
        timeout: int = 300,
        activity_timeout: int = 120,
        debug: bool = False,
    ) -> str:
        """Launch a task in the background.

        Returns:
            task_id: Unique identifier for tracking the task

        Raises:
            ValueError: If prompt exceeds maximum length
        """
        # Validate prompt length to prevent DoS via huge prompts
        if len(prompt) > self.MAX_PROMPT_LENGTH:
            raise ValueError(f"Prompt exceeds maximum length of {self.MAX_PROMPT_LENGTH} characters")

        task_id = self._generate_task_id()
        now = datetime.now().isoformat()

        # Create initial task state
        info = TaskInfo(
            task_id=task_id,
            status=TaskStatus.PENDING,
            created_at=now,
            project_path=os.path.abspath(project_path),
            prompt=prompt,
            agent=agent,
            timeout=timeout,
            activity_timeout=activity_timeout,
            debug=debug,
        )
        self._write_task(info)

        # Launch background process
        # We spawn a new Python process that runs this same script with --run-task
        script_path = os.path.abspath(__file__)
        cmd = [
            sys.executable,
            script_path,
            "--run-task", task_id,
            "--tasks-dir", self.tasks_dir,
        ]

        # Use appropriate flags for background process
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }

        if sys.platform == "win32":
            # Windows: use CREATE_NEW_PROCESS_GROUP, DETACHED_PROCESS, and CREATE_NO_WINDOW
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP |
                subprocess.DETACHED_PROCESS |
                subprocess.CREATE_NO_WINDOW
            )
        else:
            # Unix: start new session
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(cmd, **kwargs)
            # Update task with PID
            info.pid = proc.pid
            info.status = TaskStatus.RUNNING
            info.started_at = datetime.now().isoformat()
            self._write_task(info)
        except Exception as e:
            info.status = TaskStatus.FAILED
            info.result = {"success": False, "errors": [f"Failed to launch: {e}"]}
            info.completed_at = datetime.now().isoformat()
            self._write_task(info)

        return task_id

    def get_task_status(self, task_id: str) -> Optional[TaskInfo]:
        """Get the current status of a task."""
        info = self._read_task(task_id)
        if info and info.status == TaskStatus.RUNNING:
            # Check if process is still alive
            if info.pid and not self._is_process_alive(info.pid):
                # Process died without updating status - mark as failed
                info.status = TaskStatus.FAILED
                if not info.result:
                    info.result = {"success": False, "errors": ["Process terminated unexpectedly"]}
                info.completed_at = datetime.now().isoformat()
                self._write_task(info)
        return info

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            if sys.platform == "win32":
                # Windows: use tasklist
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True
                )
                return str(pid) in result.stdout
            else:
                # Unix: send signal 0
                os.kill(pid, 0)
                return True
        except (OSError, subprocess.SubprocessError):
            return False

    def list_tasks(self, status_filter: Optional[str] = None) -> List[TaskInfo]:
        """List all tasks, optionally filtered by status."""
        tasks = []
        pattern = os.path.join(self.tasks_dir, "*.json")
        for task_file in glob_module.glob(pattern):
            task_id = os.path.basename(task_file).replace(".json", "")
            if task_id.endswith(".tmp"):
                continue
            info = self.get_task_status(task_id)
            if info:
                if status_filter is None or info.status == status_filter:
                    tasks.append(info)
        # Sort by creation time, newest first
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task."""
        info = self._read_task(task_id)
        if not info:
            return False
        if info.status != TaskStatus.RUNNING:
            return False
        if not info.pid:
            return False

        try:
            if sys.platform == "win32":
                # Windows: use taskkill
                subprocess.run(["taskkill", "/F", "/PID", str(info.pid)], capture_output=True)
            else:
                # Unix: send SIGTERM, then SIGKILL
                os.kill(info.pid, signal.SIGTERM)
                time.sleep(1)
                if self._is_process_alive(info.pid):
                    os.kill(info.pid, signal.SIGKILL)

            # Update task status
            info.status = TaskStatus.CANCELLED
            info.completed_at = datetime.now().isoformat()
            if not info.result:
                info.result = {"success": False, "errors": ["Task cancelled by user"]}
            self._write_task(info)
            return True
        except Exception:
            return False

    def get_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get the result of a completed task."""
        info = self.get_task_status(task_id)
        if info and info.result:
            return info.result
        return None

    def cleanup_tasks(self, max_age_hours: int = 24, keep_running: bool = True) -> int:
        """Remove old task files.

        Args:
            max_age_hours: Remove tasks older than this many hours
            keep_running: If True, don't remove running tasks

        Returns:
            Number of tasks removed
        """
        removed = 0
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

        for info in self.list_tasks():
            try:
                created = datetime.fromisoformat(info.created_at).timestamp()
                if created < cutoff:
                    if keep_running and info.status == TaskStatus.RUNNING:
                        continue
                    task_file = self._task_file(info.task_id)
                    if os.path.exists(task_file):
                        os.remove(task_file)
                        removed += 1
            except Exception:
                continue
        return removed

    def wait_for_task(self, task_id: str, poll_interval: float = 0.5, timeout: Optional[float] = None) -> Optional[TaskInfo]:
        """Wait for a task to complete.

        Args:
            task_id: The task to wait for
            poll_interval: How often to check status (seconds)
            timeout: Maximum time to wait (None = wait forever)

        Returns:
            Final TaskInfo, or None if timeout
        """
        start = time.time()
        while True:
            info = self.get_task_status(task_id)
            if not info:
                return None
            if info.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                return info
            if timeout and (time.time() - start) > timeout:
                return None
            time.sleep(poll_interval)


def run_task_worker(task_id: str, tasks_dir: str) -> None:
    """Worker function that runs a task (called by background process)."""
    manager = TaskManager(tasks_dir)
    info = manager._read_task(task_id)

    if not info:
        return

    # Update status to running
    info.status = TaskStatus.RUNNING
    info.started_at = datetime.now().isoformat()
    info.pid = os.getpid()
    manager._write_task(info)

    # Run the actual task
    try:
        client = ACPClient(
            info.project_path,
            agent=info.agent,
            timeout=info.timeout,
            activity_timeout=info.activity_timeout,
            debug=info.debug,
        )
        result = client.run(info.prompt)

        # Update task with result
        info.status = TaskStatus.COMPLETED if result.get("success") else TaskStatus.FAILED
        info.result = result
        info.completed_at = datetime.now().isoformat()
        manager._write_task(info)

    except Exception as e:
        info.status = TaskStatus.FAILED
        info.result = {"success": False, "errors": [f"Task execution error: {e}"]}
        info.completed_at = datetime.now().isoformat()
        manager._write_task(info)


def main():
    parser = argparse.ArgumentParser(
        description="Send prompt to OpenCode intern via ACP with background task support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Synchronous execution
  python acp_client.py . "List all Python files" --json
  python acp_client.py . "Fix the bug in main.py" --json --timeout 600

  # Background execution
  python acp_client.py . "Fix the bug" --background --json
  python acp_client.py --status abc123 --json
  python acp_client.py --list-tasks --json
  python acp_client.py --result abc123 --json
  python acp_client.py --cancel abc123
  python acp_client.py --wait abc123 --json
  python acp_client.py --cleanup
        """,
    )

    # Positional args (optional for management commands)
    parser.add_argument("project_path", nargs="?", default=".", help="Project directory path")
    parser.add_argument("prompt", nargs="?", help="Task for the intern")

    # Execution options
    parser.add_argument("--agent", default="intern", help="Agent name (default: intern)")
    parser.add_argument("--timeout", type=int, default=300, help="Overall timeout in seconds (default: 300)")
    parser.add_argument("--activity-timeout", type=int, default=120,
                        help="Max seconds without activity before considering agent hung (default: 120)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging to stderr")

    # Background execution (default) vs synchronous
    parser.add_argument("--background", action="store_true", default=True, dest="background",
                        help="Run task in background, return task ID (default)")
    parser.add_argument("--sync", action="store_false", dest="background",
                        help="Run task synchronously, wait for completion")

    # Task management commands
    parser.add_argument("--status", metavar="TASK_ID", help="Check status of a background task")
    parser.add_argument("--list-tasks", action="store_true", help="List all background tasks")
    parser.add_argument("--result", metavar="TASK_ID", help="Get the result of a completed task")
    parser.add_argument("--cancel", metavar="TASK_ID", help="Cancel a running task")
    parser.add_argument("--wait", metavar="TASK_ID", help="Wait for a task to complete")
    parser.add_argument("--cleanup", action="store_true", help="Remove old completed tasks")
    parser.add_argument("--filter", choices=[s.value for s in TaskStatus],
                        help="Filter tasks by status (use with --list-tasks)")

    # Internal worker arguments
    parser.add_argument("--run-task", metavar="TASK_ID", help=argparse.SUPPRESS)
    parser.add_argument("--tasks-dir", help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Determine tasks directory
    tasks_dir = args.tasks_dir if args.tasks_dir else TaskManager.DEFAULT_TASKS_DIR
    manager = TaskManager(tasks_dir)

    # Handle internal worker mode
    if args.run_task:
        run_task_worker(args.run_task, tasks_dir)
        return

    # Handle task management commands
    if args.status:
        info = manager.get_task_status(args.status)
        if not info:
            print(json.dumps({"error": f"Task {args.status} not found"}), file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps({
                "task_id": info.task_id,
                "status": info.status,
                "created_at": info.created_at,
                "started_at": info.started_at,
                "completed_at": info.completed_at,
                "prompt": info.prompt[:100] + "..." if len(info.prompt) > 100 else info.prompt,
            }, indent=2))
        else:
            print(f"Task: {info.task_id}")
            print(f"Status: {info.status}")
            print(f"Created: {info.created_at}")
            if info.started_at:
                print(f"Started: {info.started_at}")
            if info.completed_at:
                print(f"Completed: {info.completed_at}")
        return

    if args.list_tasks:
        tasks = manager.list_tasks(status_filter=args.filter)
        if args.json:
            print(json.dumps([{
                "task_id": t.task_id,
                "status": t.status,
                "created_at": t.created_at,
                "prompt": t.prompt[:50] + "..." if len(t.prompt) > 50 else t.prompt,
            } for t in tasks], indent=2))
        else:
            if not tasks:
                print("No tasks found")
            else:
                for t in tasks:
                    prompt_preview = t.prompt[:40] + "..." if len(t.prompt) > 40 else t.prompt
                    print(f"{t.task_id}  {t.status:10}  {prompt_preview}")
        return

    if args.result:
        result = manager.get_result(args.result)
        if not result:
            info = manager.get_task_status(args.result)
            if not info:
                print(json.dumps({"error": f"Task {args.result} not found"}), file=sys.stderr)
            else:
                print(json.dumps({"error": f"Task {args.result} is {info.status}, no result yet"}), file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("success"):
                print(result.get("response", ""))
            else:
                print("Errors:", "; ".join(result.get("errors", [])), file=sys.stderr)
                sys.exit(1)
        return

    if args.cancel:
        if manager.cancel_task(args.cancel):
            print(json.dumps({"cancelled": args.cancel}) if args.json else f"Cancelled task {args.cancel}")
        else:
            print(json.dumps({"error": f"Could not cancel {args.cancel}"}) if args.json else f"Could not cancel {args.cancel}", file=sys.stderr)
            sys.exit(1)
        return

    if args.wait:
        info = manager.wait_for_task(args.wait, timeout=args.timeout)
        if not info:
            print(json.dumps({"error": f"Task {args.wait} not found or timed out"}), file=sys.stderr)
            sys.exit(1)
        result = info.result or {"success": False, "errors": ["No result"]}
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("success"):
                print(result.get("response", ""))
            else:
                print("Errors:", "; ".join(result.get("errors", [])), file=sys.stderr)
                sys.exit(1)
        return

    if args.cleanup:
        removed = manager.cleanup_tasks()
        print(json.dumps({"removed": removed}) if args.json else f"Removed {removed} old tasks")
        return

    # Validate prompt is provided for execution
    if not args.prompt:
        parser.error("prompt is required for task execution")

    # Handle background execution
    if args.background:
        task_id = manager.launch_background(
            args.project_path,
            args.prompt,
            agent=args.agent,
            timeout=args.timeout,
            activity_timeout=args.activity_timeout,
            debug=args.debug,
        )
        if args.json:
            print(json.dumps({"task_id": task_id, "status": "launched"}))
        else:
            print(f"Task launched: {task_id}")
        return

    # Synchronous execution (original behavior)
    client = ACPClient(
        args.project_path,
        agent=args.agent,
        timeout=args.timeout,
        activity_timeout=args.activity_timeout,
        debug=args.debug,
    )
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
