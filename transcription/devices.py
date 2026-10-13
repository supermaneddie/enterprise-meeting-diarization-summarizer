from __future__ import annotations

import re
from contextlib import contextmanager
from threading import Condition, Event


class DeviceConfigurationError(ValueError):
    pass


class DeviceWaitCancelled(RuntimeError):
    pass


def parse_devices(specification: str) -> list[str]:
    """Parse local devices or a single SSH target, preserving username case."""
    remote = parse_remote_device(specification)
    if remote:
        device, host = remote
        return [f"remote-{device}:{host}"]
    value = specification.strip().lower().replace(" ", "")
    if value == "cpu":
        return ["cpu"]
    if value == "cuda":
        return ["cuda:0"]
    if not re.fullmatch(r"cuda:\d+(?:,\d+)*", value):
        raise DeviceConfigurationError(
            "Invalid diarization device. Use 'cpu', 'cuda', 'cuda:0', or multiple "
            "CUDA indexes such as 'cuda:0,2'."
        )
    devices = [f"cuda:{int(index)}" for index in value.removeprefix("cuda:").split(",")]
    if len(devices) != len(set(devices)):
        raise DeviceConfigurationError("Diarization device list contains duplicate GPU indexes")
    return devices


def parse_remote_device(specification: str) -> tuple[str, str] | None:
    value = specification.strip().replace("\\@", "@")
    if not value.lower().startswith(("remote-", "remove-")):
        return None
    match = re.fullmatch(
        r"(?:remote|remove)-(cpu|cuda(?::?\d+)?):"
        r"([A-Za-z0-9_][A-Za-z0-9_.-]*)[@:]([A-Za-z0-9][A-Za-z0-9.-]*)",
        value, re.IGNORECASE,
    )
    if not match:
        raise DeviceConfigurationError(
            "Invalid remote diarization device. Use remote-cpu:user@host, "
            "remote-cuda:user@host, or remote-cuda:0:user@host."
        )
    device, user, host = match.groups()
    device = device.lower()
    if device.startswith("cuda"):
        device = f"cuda:{int(device[4:].lstrip(':') or '0')}"
    return device, f"{user}@{host.lower()}"


class DevicePool:
    """Process-local exclusive leases for configured CUDA devices."""

    def __init__(self):
        self._condition = Condition()
        self._busy: set[str] = set()
        self._last_assigned: dict[tuple[str, ...], int] = {}

    @contextmanager
    def acquire(self, specification: str, cancel_event: Event):
        devices = parse_devices(specification)
        if devices == ["cpu"] or devices[0].startswith("remote-cpu:"):
            yield devices[0]
            return

        key = tuple(devices)
        selected = None
        with self._condition:
            while selected is None:
                if cancel_event.is_set():
                    raise DeviceWaitCancelled("Job stopped while waiting for a diarization GPU")
                start = (self._last_assigned.get(key, -1) + 1) % len(devices)
                ordered = devices[start:] + devices[:start]
                for device in ordered:
                    if device not in self._busy:
                        selected = device
                        self._busy.add(device)
                        self._last_assigned[key] = devices.index(device)
                        break
                if selected is None:
                    self._condition.wait(timeout=0.5)
        try:
            yield selected
        finally:
            with self._condition:
                self._busy.discard(selected)
                self._condition.notify_all()
