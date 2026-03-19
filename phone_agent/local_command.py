"""Build local executable command packets for Flutter + Shizuku clients."""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any

from phone_agent.config.apps import APP_PACKAGES
from phone_agent.config.timing import TIMING_CONFIG


def build_local_command_packet(
    *,
    action: dict[str, Any] | None,
    thinking: str,
    message: str | None,
    finished: bool,
    step: int,
    session_id: str,
    screen_width: int,
    screen_height: int,
) -> dict[str, Any]:
    """Convert a parsed action into a client-side executable command packet."""
    packet = {
        "protocol_version": "1.0",
        "packet_id": str(uuid.uuid4()),
        "timestamp_ms": int(time.time() * 1000),
        "session_id": session_id,
        "step": step,
        "finished": finished,
        "thinking": thinking,
        "message": message,
        "agent_action": action,
        "execution": {
            "mode": "local_adb_via_shizuku",
            "requires_user_interaction": False,
            "commands": [],
            "client_actions": [],
            "warnings": [],
        },
    }

    if not action:
        packet["execution"]["warnings"].append("Empty action")
        return packet

    metadata = action.get("_metadata")
    if metadata == "finish":
        return packet

    if metadata != "do":
        packet["execution"]["warnings"].append(f"Unknown metadata: {metadata}")
        return packet

    action_name = action.get("action")
    commands: list[dict[str, Any]] = []
    client_actions: list[dict[str, Any]] = []

    def add_shell(
        cmd: str,
        delay_ms_after: int | None = None,
        capture_output: bool = False,
    ) -> None:
        command_id = f"cmd_{len(commands) + 1}"
        command_payload = {
            "command_id": command_id,
            "type": "adb_shell",
            "command": cmd,
            "capture_output": capture_output,
        }
        if delay_ms_after is not None:
            command_payload["delay_ms_after"] = delay_ms_after
        commands.append(command_payload)

    if action_name == "Launch":
        app_name = action.get("app")
        package = APP_PACKAGES.get(app_name)
        if not app_name:
            packet["execution"]["warnings"].append("Launch action missing app")
        elif not package:
            packet["execution"]["warnings"].append(
                f"Unknown app mapping for: {app_name}"
            )
        else:
            add_shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
                int(TIMING_CONFIG.device.default_launch_delay * 1000),
            )

    elif action_name in {"Tap", "Double Tap", "Long Press"}:
        element = action.get("element")
        if not _is_valid_point(element):
            packet["execution"]["warnings"].append(
                f"{action_name} action missing valid element"
            )
        else:
            x, y = _convert_relative_to_absolute(
                element, screen_width=screen_width, screen_height=screen_height
            )
            if action_name == "Tap":
                add_shell(
                    f"input tap {x} {y}",
                    int(TIMING_CONFIG.device.default_tap_delay * 1000),
                )
            elif action_name == "Double Tap":
                add_shell(f"input tap {x} {y}")
                add_shell(
                    f"input tap {x} {y}",
                    int(TIMING_CONFIG.device.default_double_tap_delay * 1000),
                )
            else:
                duration_ms = 3000
                add_shell(
                    f"input swipe {x} {y} {x} {y} {duration_ms}",
                    int(TIMING_CONFIG.device.default_long_press_delay * 1000),
                )

    elif action_name == "Swipe":
        start = action.get("start")
        end = action.get("end")
        if not _is_valid_point(start) or not _is_valid_point(end):
            packet["execution"]["warnings"].append("Swipe action missing start or end")
        else:
            start_x, start_y = _convert_relative_to_absolute(
                start, screen_width=screen_width, screen_height=screen_height
            )
            end_x, end_y = _convert_relative_to_absolute(
                end, screen_width=screen_width, screen_height=screen_height
            )
            duration_ms = _compute_swipe_duration_ms(start_x, start_y, end_x, end_y)
            add_shell(
                f"input swipe {start_x} {start_y} {end_x} {end_y} {duration_ms}",
                int(TIMING_CONFIG.device.default_swipe_delay * 1000),
            )

    elif action_name == "Back":
        add_shell(
            "input keyevent 4", int(TIMING_CONFIG.device.default_back_delay * 1000)
        )

    elif action_name == "Home":
        add_shell(
            "input keyevent KEYCODE_HOME",
            int(TIMING_CONFIG.device.default_home_delay * 1000),
        )

    elif action_name in {"Type", "Type_Name"}:
        text = action.get("text", "")
        encoded_text = base64.b64encode(text.encode("utf-8")).decode("utf-8")
        add_shell("settings get secure default_input_method", capture_output=True)
        add_shell("ime set com.android.adbkeyboard/.AdbIME")
        add_shell("am broadcast -a ADB_CLEAR_TEXT")
        add_shell(f"am broadcast -a ADB_INPUT_B64 --es msg {encoded_text}")
        client_actions.append(
            {
                "type": "restore_input_method",
                "description": "Restore the original IME captured from the first command output.",
            }
        )

    elif action_name == "Wait":
        duration_sec = _parse_wait_duration(action.get("duration", "1 seconds"))
        client_actions.append(
            {
                "type": "delay",
                "duration_ms": int(duration_sec * 1000),
            }
        )

    elif action_name in {"Take_over", "Interact"}:
        packet["execution"]["requires_user_interaction"] = True
        client_actions.append(
            {
                "type": "user_interaction",
                "message": action.get("message", "User intervention required"),
            }
        )

    elif action_name in {"Note", "Call_API"}:
        client_actions.append(
            {
                "type": "noop",
                "description": f"{action_name} does not require local adb command execution.",
            }
        )

    else:
        packet["execution"]["warnings"].append(f"Unknown action: {action_name}")

    if "message" in action:
        packet["execution"]["requires_user_interaction"] = True
        client_actions.append(
            {
                "type": "sensitive_confirmation",
                "message": action["message"],
            }
        )

    packet["execution"]["commands"] = commands
    packet["execution"]["client_actions"] = client_actions
    return packet


def _convert_relative_to_absolute(
    point: list[int], *, screen_width: int, screen_height: int
) -> tuple[int, int]:
    x = int(point[0] / 1000 * screen_width)
    y = int(point[1] / 1000 * screen_height)
    return x, y


def _compute_swipe_duration_ms(
    start_x: int, start_y: int, end_x: int, end_y: int
) -> int:
    dist_sq = (start_x - end_x) ** 2 + (start_y - end_y) ** 2
    duration_ms = int(dist_sq / 1000)
    return max(1000, min(duration_ms, 2000))


def _parse_wait_duration(duration_text: str) -> float:
    try:
        return float(duration_text.replace("seconds", "").strip())
    except ValueError:
        return 1.0


def _is_valid_point(point: Any) -> bool:
    return (
        isinstance(point, list)
        and len(point) >= 2
        and isinstance(point[0], (int, float))
        and isinstance(point[1], (int, float))
    )
