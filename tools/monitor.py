"""Small camera-agent status window (no camera pixels)."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import tomllib


STATE_RE = re.compile(
    r"State=(?P<state>[A-Z_]+) active_avg=(?P<score>[0-9.]+) votes=(?P<votes>[^ ]+)"
)
ACCEPTED_RE = re.compile(r"accepted (?P<state>[A-Z_]+)")
DEVICE_RE = re.compile(r"\[(?P<device>[a-z0-9_-]+)\]")
RULE_RE = re.compile(r"RULE EVENT: Facebook usage active for (?P<seconds>[0-9.]+)s")


@dataclass
class DeviceStatus:
    state: str = "STARTING"
    score: str = "-"
    votes: str = "-"
    mainflux: str = "-"
    rule: str = "none"
    fb_since: float | None = None
    last_event: float | None = None


class MonitorApp:
    def __init__(self, root: tk.Tk, command: list[str], devices: list[str], project: Path) -> None:
        self.root = root
        self.root.title("Camera Agent")
        self.root.geometry("370x240")
        self.root.minsize(320, 180)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.command = command
        self.project = project
        self.devices = devices
        self.status = {device: DeviceStatus() for device in devices}
        self.lines: deque[str] = deque(maxlen=3)
        self.events: queue.Queue[str] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.labels: dict[str, ttk.Label] = {}

        frame = ttk.Frame(root, padding=8)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Camera Agent", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Chỉ hiển thị trạng thái — không hiển thị pixel camera").pack(anchor="w")
        self.table = ttk.Frame(frame)
        self.table.pack(fill="x", pady=(8, 4))
        for column, title in enumerate(("Thiết bị", "State", "Score", "Rule")):
            ttk.Label(self.table, text=title, font=("Segoe UI", 9, "bold")).grid(
                row=0, column=column, sticky="w", padx=(0, 8)
            )
        for row, device in enumerate(devices, start=1):
            ttk.Label(self.table, text=device).grid(row=row, column=0, sticky="w", padx=(0, 8))
            self.labels[f"{device}:state"] = ttk.Label(self.table, text="STARTING")
            self.labels[f"{device}:state"].grid(row=row, column=1, sticky="w", padx=(0, 8))
            self.labels[f"{device}:score"] = ttk.Label(self.table, text="-")
            self.labels[f"{device}:score"].grid(row=row, column=2, sticky="w", padx=(0, 8))
            self.labels[f"{device}:rule"] = ttk.Label(self.table, text="none")
            self.labels[f"{device}:rule"].grid(row=row, column=3, sticky="w")
        self.mainflux_label = ttk.Label(frame, text="Mainflux: waiting")
        self.mainflux_label.pack(anchor="w", pady=(5, 0))
        self.rule_label = ttk.Label(frame, text="Rule: loading config...")
        self.rule_label.pack(anchor="w", pady=(2, 0))
        self.log_label = ttk.Label(frame, text="", wraplength=350)
        self.log_label.pack(anchor="w", pady=(3, 0))
        self.start()
        self.load_rule_config()
        self.root.after(150, self.tick)

    def load_rule_config(self) -> None:
        try:
            with (self.project / "config" / "rules.toml").open("rb") as handle:
                rules = tomllib.load(handle)
            facebook = rules.get("facebook_usage", {})
            self.minimum_confidence = float(facebook.get("minimum_confidence", 0.72))
            self.trigger_after = float(facebook.get("trigger_after_seconds", 3.0))
            self.cooldown = float(facebook.get("cooldown_seconds", 60.0))
            self.rule_label.configure(
                text=(
                    f"Rule: score >= {self.minimum_confidence:.2f} | "
                    f"giữ {self.trigger_after:.1f}s | cooldown {self.cooldown:.0f}s"
                )
            )
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            self.minimum_confidence, self.trigger_after, self.cooldown = 0.72, 3.0, 60.0

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            cwd=self.project,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=os.environ.copy(),
        )
        threading.Thread(target=self.read_output, daemon=True).start()

    def read_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self.events.put(line.strip())
        self.events.put("Agent đã dừng")

    def tick(self) -> None:
        while True:
            try:
                line = self.events.get_nowait()
            except queue.Empty:
                break
            self.consume(line)
        for device, status in self.status.items():
            self.labels[f"{device}:state"].configure(text=status.state)
            self.labels[f"{device}:score"].configure(text=status.score)
            self.labels[f"{device}:rule"].configure(text=status.rule)
            self.update_rule_text(device, status)
        self.root.after(150, self.tick)

    def update_rule_text(self, device: str, status: DeviceStatus) -> None:
        now = time.monotonic()
        if status.last_event is not None and now - status.last_event < self.cooldown:
            remaining = self.cooldown - (now - status.last_event)
            self.labels[f"{device}:rule"].configure(
                text=f"COOLDOWN {remaining:.0f}s"
            )
        elif status.state == "FACEBOOK_DETECTED" and status.fb_since is not None:
            active = now - status.fb_since
            if active < self.trigger_after:
                self.labels[f"{device}:rule"].configure(
                    text=f"PENDING {active:.1f}/{self.trigger_after:.1f}s"
                )
            else:
                self.labels[f"{device}:rule"].configure(text="READY / chờ event")
        elif status.state == "FACEBOOK_DETECTED":
            self.labels[f"{device}:rule"].configure(text="FACEBOOK")

    def consume(self, line: str) -> None:
        self.lines.append(line[-180:])
        self.log_label.configure(text="\n".join(self.lines))
        device_match = DEVICE_RE.search(line)
        device = device_match.group("device") if device_match else None
        target = self.status.get(device) if device else (
            next(iter(self.status.values())) if len(self.status) == 1 else None
        )
        state_match = STATE_RE.search(line)
        if state_match and target:
            target.state = state_match.group("state")
            target.score = state_match.group("score")
            target.votes = state_match.group("votes")
            if target.state == "FACEBOOK_DETECTED":
                target.fb_since = target.fb_since or time.monotonic()
            else:
                target.fb_since = None
        accepted = ACCEPTED_RE.search(line)
        if accepted and target:
            target.state = accepted.group("state")
            target.mainflux = "accepted"
            self.mainflux_label.configure(text="Mainflux: accepted")
        rule = RULE_RE.search(line)
        if rule and target:
            target.rule = f"ALARM ({rule.group('seconds')}s)"
            target.last_event = time.monotonic()
        if "Mainflux accepted" in line:
            self.mainflux_label.configure(text="Mainflux: accepted")

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Small status-only Camera Agent window")
    parser.add_argument("--devices", default="config/devices.yaml")
    parser.add_argument("--preview-mode", choices=("off", "compact"), default="off")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project = Path(os.getenv("CAMERA_AGENT_PROJECT", Path.cwd())).resolve()
    if not (project / "agent.py").exists():
        project = Path(__file__).resolve().parents[1]
    agent_python = os.getenv("CAMERA_AGENT_PYTHON")
    if not agent_python:
        candidate = project / ".venv" / "Scripts" / "python.exe"
        agent_python = str(candidate if candidate.exists() else sys.executable)
    command = [
        agent_python,
        "-u",
        str(project / "agent.py"),
        "--devices",
        args.devices,
        "--preview-mode",
        args.preview_mode,
    ]
    root = tk.Tk()
    MonitorApp(root, command, ["yume-1"], project)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
