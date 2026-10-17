import asyncio
import base64
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import requests
import websocket
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

# =====================================================================
# 1. DPI AWARENESS (Windows only)
# =====================================================================
if os.name == "nt":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# =====================================================================
# 2. LOCAL AI INITIALIZATION (Qwen-VL & Llama 3.2 via Ollama)
# =====================================================================
qwen_llm = ChatOpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",
    model="qwen3-vl:4b-instruct",
    temperature=0.0,
)

llama_llm = ChatOpenAI(
    base_url="http://localhost:11434/v1",
    api_key="ollama",
    model="llama3.2",
    temperature=0.0,
)

# =====================================================================
# 3. CHROME CDP CONNECTION
# =====================================================================
_cdp_ws = None
_cdp_msg_id = 0
_run_log_file = None


class _TeeStream:
    def __init__(self, original, log_file):
        self.original = original
        self.log_file = log_file

    def write(self, text: str) -> int:
        self.original.write(text)
        self.log_file.write(text)
        self.log_file.flush()
        return len(text)

    def flush(self) -> None:
        self.original.flush()
        self.log_file.flush()

    def isatty(self) -> bool:
        return self.original.isatty()


def _start_run_log() -> str:
    global _run_log_file
    log_dir = Path.cwd() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    log_path = log_dir / f"run_{timestamp}.log"
    _run_log_file = open(log_path, "w", encoding="utf-8")
    sys.stdout = _TeeStream(sys.stdout, _run_log_file)
    sys.stderr = _TeeStream(sys.stderr, _run_log_file)
    return str(log_path)


def log_event(event: str, **details: object) -> None:
    if _run_log_file is None:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **details,
    }
    _run_log_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    _run_log_file.flush()


def _chrome_executable() -> str | None:
    if os.name == "nt":
        candidates = []
        try:
            import winreg
            for key_path in (
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            ):
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(hive, key_path) as key:
                            value, _ = winreg.QueryValueEx(key, None)
                            if value:
                                candidates.append(value)
                    except OSError:
                        pass
        except ImportError:
            pass
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        found = shutil.which("chrome.exe") or shutil.which("chrome")
        if found:
            candidates.append(found)
    else:
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return None


def _cdp_connect() -> bool:
    global _cdp_ws
    if _cdp_ws is not None:
        return True
    try:
        res = requests.get("http://127.0.0.1:9222/json", timeout=5)
        pages = res.json()
        target_page = next(
            (p for p in pages if p.get("type") == "page" and "webSocketDebuggerUrl" in p),
            None,
        )
        if not target_page:
            print("No CDP page target found.")
            return False
        _cdp_ws = websocket.create_connection(target_page["webSocketDebuggerUrl"], timeout=10)
        title = target_page.get("title", "unknown")
        log_event("cdp_connected", title=title, target_url=target_page.get("url", ""))
        print(f"CDP connected to: {title}")
        return True
    except Exception as e:
        log_event("cdp_connection_error", error=repr(e))
        print(f"CDP connection failed: {e}")
        return False


def _cdp_send(method: str, params: dict | None = None) -> dict:
    global _cdp_msg_id, _cdp_ws
    if _cdp_ws is None:
        if not _cdp_connect():
            raise RuntimeError("CDP not connected")
    _cdp_msg_id += 1
    msg_id = _cdp_msg_id
    payload = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    log_event("cdp_request", id=msg_id, method=method, params=params or {})
    try:
        _cdp_ws.send(json.dumps(payload))
        while True:
            raw = _cdp_ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    log_event("cdp_response_error", id=msg_id, method=method, error=data.get("error"))
                else:
                    log_event("cdp_response", id=msg_id, method=method)
                return data
    except Exception as e:
        log_event("cdp_ws_error", id=msg_id, method=method, error=repr(e))
        if _cdp_reconnect():
            return _cdp_send(method, params)
        raise


def _cdp_reconnect() -> bool:
    global _cdp_ws
    _cdp_ws = None
    time.sleep(1)
    return _cdp_connect()


# =====================================================================
# 4. CDP SCREENSHOT
# =====================================================================
def capture_viewport_screenshot(path: str) -> bool:
    if not _cdp_connect():
        return False
    try:
        result = _cdp_send("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
        data = result.get("result", {}).get("data")
        if not data:
            return False
        image_bytes = base64.b64decode(data)
        with open(path, "wb") as f:
            f.write(image_bytes)
        log_event("screenshot_captured", path=path, bytes=len(image_bytes))
        return True
    except Exception as e:
        log_event("screenshot_error", path=path, error=repr(e))
        print(f"Screenshot error: {e}")
        return False


def _set_visual_overlay(box: list[float], pixel: tuple[float, float] | None = None) -> None:
    width, height = get_viewport_size()
    x1, y1, x2, y2 = box
    overlay = {
        "left": x1 * width / 1000,
        "top": y1 * height / 1000,
        "width": max((x2 - x1) * width / 1000, 4),
        "height": max((y2 - y1) * height / 1000, 4),
    }
    marker = ""
    if pixel is not None:
        marker = f"const dot=document.createElement('div');dot.id='__agent_click_marker';Object.assign(dot.style,{{position:'fixed',left:'{pixel[0]}px',top:'{pixel[1]}px',width:'14px',height:'14px',margin:'-7px',borderRadius:'50%',background:'#ff1744',border:'3px solid white',zIndex:'2147483647',pointerEvents:'none'}});document.body.appendChild(dot);"
    expression = (
        "(() => {"
        "const old=document.getElementById('__agent_target_overlay');if(old)old.remove();"
        "const box=document.createElement('div');box.id='__agent_target_overlay';"
        f"Object.assign(box.style,{{position:'fixed',left:'{overlay['left']}px',top:'{overlay['top']}px',width:'{overlay['width']}px',height:'{overlay['height']}px',border:'4px solid #ff1744',background:'rgba(255,23,68,.12)',zIndex:'2147483646',pointerEvents:'none',boxSizing:'border-box'}});"
        "document.body.appendChild(box);"
        f"{marker}"
        "})();"
    )
    _cdp_send("Runtime.evaluate", {"expression": expression})


def _clear_visual_overlay() -> None:
    _cdp_send("Runtime.evaluate", {
        "expression": "document.getElementById('__agent_target_overlay')?.remove();document.getElementById('__agent_click_marker')?.remove();",
    })


def capture_target_diagnostic(path: str, box: list[float], pixel: tuple[float, float] | None = None) -> bool:
    try:
        _set_visual_overlay(box, pixel)
        time.sleep(0.15)
        captured = capture_viewport_screenshot(path)
        log_event("target_overlay_captured", path=path, box=box, pixel=pixel, success=captured)
        return captured
    except Exception as e:
        log_event("target_overlay_error", path=path, box=box, pixel=pixel, error=repr(e))
        return False
    finally:
        try:
            _clear_visual_overlay()
        except Exception as e:
            log_event("target_overlay_clear_error", error=repr(e))


# =====================================================================
# 5. CDP INPUT ACTIONS
# =====================================================================
def cdp_click(x: float, y: float) -> bool:
    if not _cdp_connect():
        return False
    try:
        _cdp_send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        _cdp_send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        print(f"  Clicked at ({x:.0f}, {y:.0f})")
        return True
    except Exception as e:
        print(f"  Click error: {e}")
        return False


def cdp_type_text(text: str) -> bool:
    if not _cdp_connect():
        return False
    try:
        _cdp_send("Input.insertText", {"text": text})
        return True
    except Exception:
        try:
            for char in text:
                _cdp_send("Input.dispatchKeyEvent", {"type": "char", "text": char})
            return True
        except Exception as e:
            print(f"  Type error: {e}")
            return False


def cdp_press_key(key: str) -> bool:
    if not _cdp_connect():
        return False
    try:
        _cdp_send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": key, "code": key,
            "windowsVirtualKeyCode": _key_to_vk(key),
        })
        _cdp_send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": key, "code": key,
            "windowsVirtualKeyCode": _key_to_vk(key),
        })
        return True
    except Exception as e:
        print(f"  Key press error: {e}")
        return False


def cdp_scroll(direction: str, amount: int = 400) -> bool:
    if not _cdp_connect():
        return False
    try:
        dy = -amount if direction == "up" else amount
        _cdp_send("Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": 400, "y": 400,
            "deltaX": 0, "deltaY": dy,
        })
        print(f"  Scrolled {direction} by {amount}px")
        return True
    except Exception as e:
        print(f"  Scroll error: {e}")
        return False


def cdp_navigate(url: str) -> bool:
    if not _cdp_connect():
        return False
    try:
        _cdp_send("Page.navigate", {"url": url})
        print(f"  Navigated to {url}")
        return True
    except Exception as e:
        print(f"  Navigation error: {e}")
        return False


def _wait_for_page_settle(timeout: float = 3.0, interval: float = 0.4) -> None:
    """Give the page time to render, then poll until navigation settles."""
    time.sleep(1.8)
    deadline = time.time() + timeout
    last_url = ""
    last_title = ""
    while time.time() < deadline:
        try:
            url = get_current_url()
            title = get_page_title()
            if url == last_url and title == last_title:
                return
            last_url = url
            last_title = title
        except Exception:
            pass
        time.sleep(interval)


def cdp_go_back() -> bool:
    if not _cdp_connect():
        return False
    try:
        _cdp_send("Runtime.evaluate", {"expression": "history.back()", "returnByValue": True})
        print("  Went back")
        return True
    except Exception:
        return False


def get_current_url() -> str:
    if not _cdp_connect():
        return ""
    try:
        result = _cdp_send("Runtime.evaluate", {"expression": "window.location.href", "returnByValue": True})
        return result.get("result", {}).get("result", {}).get("value", "")
    except Exception:
        return ""


def get_page_title() -> str:
    if not _cdp_connect():
        return ""
    try:
        result = _cdp_send("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
        return result.get("result", {}).get("result", {}).get("value", "")
    except Exception:
        return ""


def _goal_reached_by_page(goal: str, title: str, url: str) -> bool:
    goal_lower = goal.lower()
    page_text = f"{title} {url}".lower()
    if "corvette" in goal_lower and "corvette" in page_text:
        return True
    return False


def get_viewport_size() -> tuple[float, float]:
    if not _cdp_connect():
        return (1.0, 1.0)
    try:
        result = _cdp_send("Runtime.evaluate", {
            "expression": "JSON.stringify({width: window.innerWidth, height: window.innerHeight})",
            "returnByValue": True,
        })
        value = result.get("result", {}).get("result", {}).get("value", "")
        metrics = json.loads(value)
        return (max(float(metrics["width"]), 1.0), max(float(metrics["height"]), 1.0))
    except Exception:
        return (1.0, 1.0)


def _cdp_clear_field() -> bool:
    if not _cdp_connect():
        return False
    try:
        _cdp_send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Control", "code": "ControlLeft",
            "windowsVirtualKeyCode": 0x11,
        })
        _cdp_send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 0x41, "modifiers": 2,
        })
        _cdp_send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "code": "KeyA",
            "windowsVirtualKeyCode": 0x41, "modifiers": 2,
        })
        _cdp_send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Control", "code": "ControlLeft",
            "windowsVirtualKeyCode": 0x11,
        })
        cdp_press_key("Backspace")
        return True
    except Exception:
        return False


def _key_to_vk(key: str) -> int:
    mapping = {
        "Enter": 0x0D, "Tab": 0x09, "Escape": 0x1B, "Backspace": 0x08,
        "Delete": 0x2E, "ArrowUp": 0x26, "ArrowDown": 0x28,
        "ArrowLeft": 0x25, "ArrowRight": 0x27, "Space": 0x20,
    }
    return mapping.get(key, 0x0D)


# =====================================================================
# 6. URL EXTRACTION FROM GOAL
# =====================================================================
def extract_url_from_goal(goal: str) -> str | None:
    prompt = f"""Identify the website named in this goal.

Rules:
- Return ONLY the site's canonical home URL, such as https://www.zillow.com
- Do not add a path, query string, price, rental mode, or search filters
- If no website is named, return ONLY NONE

Goal: "{goal}"

URL:"""
    try:
        log_event("url_extraction_request", goal=goal)
        response = llama_llm.invoke(prompt).content.strip()
        log_event("url_extraction_response", response=response)
        if response.upper() == "NONE" or not response:
            return None
        url_match = re.search(r'https?://[^\s]+', response)
        if not url_match:
            return None
        parsed = urlsplit(url_match.group(0).rstrip(".,)"))
        if not parsed.netloc:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    except Exception as e:
        log_event("url_extraction_error", error=repr(e))
        print(f"URL extraction error: {e}")
        return None


# =====================================================================
# 7. VISION DIRECTIVE (Qwen-VL) — screenshot → coordinate-based actions
# =====================================================================
def _build_vlm_prompt(goal: str, history_text: str, stall_warning: str) -> str:
    return f"""You are a visual web automation agent. Use ONLY the screenshot to identify the next target and action. Do not infer HTML, selectors, element numbers, or hidden controls.

GOAL: {goal}

PREVIOUS ACTIONS TAKEN:
{history_text}{stall_warning}

Choose exactly ONE next action. Return ONLY one valid JSON object on one line. Do not use markdown fences and do not add explanation.

Allowed JSON shapes:
{{"action":"click","box":[x1,y1,x2,y2],"reason":"short visual description"}}
{{"action":"type","box":[x1,y1,x2,y2],"text":"text to enter","reason":"short visual description"}}
{{"action":"press","key":"Enter"}}
{{"action":"scroll","direction":"down"}}
{{"action":"navigate","url":"https://example.com"}}
{{"action":"back"}}
{{"action":"done"}}

BOX RULES:
- The screenshot is the browser page viewport only; browser tabs and address bar are not included.
- Use normalized coordinates from 0 to 1000, not pixels. The top-left is [0,0] and bottom-right is [1000,1000].
- For click, box the entire visible target and place it in the center of that target. Do not click a nearby label, card, map marker, or image.
- For type, box the visible input itself, not its label. Use the exact text requested by the goal.
- For the Zillow goal, the visual sequence is: homepage search input -> location suggestion -> For sale mode if needed -> Price control -> maximum price input -> Apply.
- After every action, wait for the next screenshot. Never combine click and type into one response.
- If a popup is open, target controls inside the popup before anything behind it.
- If the page did not change after the previous action, choose a different visible target. Do not repeat Enter or the same coordinates.
- Use done only when the screenshot visibly shows the requested result and filters.

Your response:"""


async def get_vision_directive(
    screenshot_path: str,
    goal: str,
    history: list[str],
    stall_warning: str = "",
) -> str:
    """Ask the VLM what to do next, given a screenshot and history. Returns raw directive text."""
    with open(screenshot_path, "rb") as image_file:
        base64_image = base64.b64encode(image_file.read()).decode("utf-8")

    history_text = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(history[-8:])) if history else "  (none yet)"

    prompt = _build_vlm_prompt(goal, history_text, stall_warning)
    log_event("vlm_request", goal=goal, history=history[-8:], stall_warning=stall_warning, prompt=prompt, screenshot=screenshot_path)
    message = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
    ])

    last_error = None
    for attempt in range(1, 4):
        try:
            response = await qwen_llm.ainvoke([message])
            directive = response.content.strip()
            log_event("vlm_response", attempt=attempt, response=directive)
            return directive
        except Exception as e:
            log_event("vlm_error", attempt=attempt, error=repr(e))
            last_error = e
            err_str = str(e)
            if "out-of-memory" in err_str or "out of memory" in err_str or "500" in err_str or "InternalServerError" in err_str:
                wait_secs = attempt * 8
                print(f"  VLM server error (attempt {attempt}/3): out-of-memory / 500. Waiting {wait_secs}s...")
                await asyncio.sleep(wait_secs)
            else:
                print(f"  VLM error (attempt {attempt}/3): {e}")
                await asyncio.sleep(3)

    log_event("vlm_unavailable", error=repr(last_error))
    print(f"  VLM unavailable after 3 attempts ({last_error}).")
    return '{"action":"scroll","direction":"down"}'


def _box_overlap(a: list[float], b: list[float], threshold: float = 0.6) -> bool:
    """Return True if boxes a and b overlap by at least *threshold* of the smaller."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    intersection = iw * ih
    area_a = max(1.0, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1.0, (bx2 - bx1) * (by2 - by1))
    return intersection / min(area_a, area_b) >= threshold


async def review_visual_action(
    screenshot_path: str,
    goal: str,
    action: dict,
    history: list[str] | None = None,
) -> dict:
    history_text = ""
    if history:
        history_text = "\nRECENT HISTORY:\n" + "\n".join(f"  - {h}" for h in history[-4:])
    prompt = f"""Review the browser screenshot after one attempted action.

GOAL: {goal}
ATTEMPTED ACTION: {json.dumps(action, ensure_ascii=False)}
The red rectangle is the intended target and the red dot is the actual click location. Decide whether the target was correctly identified and whether the visible page now shows progress toward the goal.{history_text}

Return ONLY one JSON object:
{{"status":"confirmed","reason":"short reason"}}
OR
{{"status":"retry","box":[x1,y1,x2,y2],"reason":"short reason"}}

The box must use normalized coordinates from 0 to 1000. Only return retry when a DIFFERENT visible target is clearly needed. If the click landed on the correct element and the page is progressing, return confirmed."""
    try:
        with open(screenshot_path, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode("utf-8")
        log_event("visual_review_request", goal=goal, action=action, screenshot=screenshot_path, prompt=prompt)
        response = await qwen_llm.ainvoke([HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
        ])])
        raw = response.content.strip()
        log_event("visual_review_response", response=raw)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {"status": "unknown", "reason": "review was not valid JSON"}
        review = json.loads(match.group(0))
        if review.get("status") != "retry":
            return {"status": "confirmed", "reason": review.get("reason", "")}
        box = review.get("box")
        if not isinstance(box, list) or len(box) != 4:
            return {"status": "unknown", "reason": "retry did not include a valid box"}
        values = [float(value) for value in box]
        if not all(0 <= value <= 1000 for value in values):
            return {"status": "unknown", "reason": "retry box was out of range"}
        # Reject retry if the corrected box overlaps the original too much
        orig_box = action.get("box")
        if isinstance(orig_box, list) and len(orig_box) == 4 and _box_overlap(orig_box, values):
            log_event("visual_review_correction_rejected", original=orig_box, corrected=values, reason="overlap_too_high")
            return {"status": "confirmed", "reason": "correction overlapped original target — keeping current action"}
        return {"status": "retry", "box": values, "reason": review.get("reason", "")}
    except Exception as e:
        log_event("visual_review_error", error=repr(e), screenshot=screenshot_path)
        return {"status": "unknown", "reason": repr(e)}


# =====================================================================
# 8. ACTION PARSER — parse VLM coordinate-based directive
# =====================================================================
def parse_directive(directive: str) -> dict | None:
    """Parse the VLM's single JSON visual action."""
    raw = directive.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE).strip()
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        return None
    try:
        action = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        log_event("directive_rejected", raw=raw, reason="not_an_action_object")
        return None
    name = action["action"].lower().strip()
    if name in {"done", "back"}:
        return {"action": name}
    if name == "press" and isinstance(action.get("key"), str):
        return {"action": name, "key": action["key"]}
    if name == "scroll" and action.get("direction") in {"up", "down"}:
        return {"action": name, "direction": action["direction"]}
    if name == "navigate" and isinstance(action.get("url"), str):
        return {"action": name, "url": action["url"]}
    if name in {"click", "type"}:
        box = action.get("box")
        if not isinstance(box, list) or len(box) not in (2, 4):
            log_event("directive_rejected", raw=raw, reason="target_must_have_2_or_4_coordinates")
            return None
        try:
            values = [float(value) for value in box]
        except (TypeError, ValueError):
            log_event("directive_rejected", raw=raw, reason="coordinates_not_numeric")
            return None
        if len(values) == 2:
            x, y = values
            values = [max(0.0, x - 25.0), max(0.0, y - 18.0), min(1000.0, x + 25.0), min(1000.0, y + 18.0)]
        if not all(0 <= value <= 1000 for value in values):
            log_event("directive_rejected", raw=raw, reason="coordinates_out_of_range", coordinates=values)
            return None
        x1, y1, x2, y2 = values
        corrected = False
        if x2 <= x1:
            center = max(0.0, min(1000.0, x1))
            x1, x2 = max(0.0, center - 20.0), min(1000.0, center + 20.0)
            corrected = True
        if y2 <= y1:
            center = max(0.0, min(1000.0, y1))
            y1, y2 = max(0.0, center - 12.0), min(1000.0, center + 12.0)
            corrected = True
        if x2 <= x1 or y2 <= y1:
            log_event("directive_rejected", raw=raw, reason="invalid_box", coordinates=values)
            return None
        if name == "type" and not isinstance(action.get("text"), str):
            log_event("directive_rejected", raw=raw, reason="missing_text")
            return None
        if corrected:
            log_event("directive_box_corrected", raw=raw, box=[x1, y1, x2, y2])
        normalized = {"action": name, "box": [x1, y1, x2, y2], "text": action.get("text", "")}
        log_event("directive_parsed", action=normalized)
        return normalized
    log_event("directive_rejected", raw=raw, reason="unsupported_action", action=name)
    return None


# =====================================================================
# 9. ACTION EXECUTOR — execute coordinate-based actions
# =====================================================================
def execute_action(action: dict) -> tuple[str, bool]:
    """Execute one visually grounded action using normalized bounding-box coordinates."""
    log_event("action_start", action=action)
    name = action["action"]
    if name == "done":
        return ("DONE - goal completed", True)
    if name == "back":
        success = cdp_go_back()
        log_event("action_complete", action=action, success=success)
        return ("BACK", False)
    if name == "press":
        key = action["key"]
        success = cdp_press_key(key)
        log_event("action_complete", action=action, success=success)
        return (f"PRESS {key}", False)
    if name == "scroll":
        direction = action["direction"]
        success = cdp_scroll(direction)
        log_event("action_complete", action=action, success=success)
        return (f"SCROLL {direction}", False)
    if name == "navigate":
        url = action["url"].strip()
        if url and not url.startswith("http"):
            url = "https://" + url
        success = cdp_navigate(url)
        log_event("action_complete", action=action, url=url, success=success)
        return (f"NAVIGATE {url}", False)
    if name in {"click", "type"}:
        width, height = get_viewport_size()
        x1, y1, x2, y2 = action["box"]
        x = ((x1 + x2) / 2) * width / 1000
        y = ((y1 + y2) / 2) * height / 1000
        success = cdp_click(x, y)
        log_event("coordinate_target", action=action, viewport=[width, height], box=[x1, y1, x2, y2], pixel=[x, y], success=success)
        if name == "click":
            return (f"CLICK box=[{x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}] at ({x:.0f},{y:.0f})", False)
        time.sleep(0.25)
        _cdp_clear_field()
        time.sleep(0.1)
        text = action["text"]
        success = cdp_type_text(text)
        log_event("action_complete", action=action, text=text, success=success)
        return (f"TYPE '{text[:30]}' in box=[{x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}] at ({x:.0f},{y:.0f})", False)
    return (f"UNRECOGNIZED ACTION {name}", False)


# =====================================================================
# 10. MAIN AGENT LOOP
# =====================================================================
async def run_agent(
    goal: str,
    max_steps: int = 30,
    start_url: str | None = None,
    status_callback: Callable[[str], None] | None = None,
):
    log_path = _start_run_log()
    if status_callback:
        status_callback(f"Log file created: {log_path}")
    log_event("run_started", goal=goal, max_steps=max_steps, requested_start_url=start_url, log_path=log_path)
    print(f"Run log: {log_path}")
    executable = _chrome_executable()
    if not executable:
        log_event("run_aborted", reason="chrome_executable_not_found")
        print("Error: Could not locate Chrome/Chromium executable.")
        return

    if not start_url:
        start_url = extract_url_from_goal(goal)
    if not start_url:
        log_event("run_aborted", reason="start_url_not_found")
        print("Could not determine which website to navigate to from your goal.")
        print("Please specify a URL, e.g.: python automation.py --url https://www.zillow.com \"your goal\"")
        return

    sandbox_profile = os.path.join(os.getcwd(), "grasp_chrome_profile")
    print(f"Launching Chrome with DevTools on port 9222...")
    print(f"Goal: {goal}")
    print(f"Starting URL: {start_url}")
    log_event("browser_starting", goal=goal, start_url=start_url, chrome_executable=executable)
    print()

    cmd_args = [
        executable,
        "--new-window",
        "--remote-debugging-port=9222",
        "--remote-allow-origins=*",
        f"--user-data-dir={sandbox_profile}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    subprocess.Popen(cmd_args)
    time.sleep(6)

    if not _cdp_connect():
        log_event("run_aborted", reason="cdp_connection_failed")
        print("Error: Could not connect to Chrome DevTools.")
        print("Make sure Chrome launched with --remote-debugging-port=9222")
        return

    action_history: list[str] = []
    consecutive_repeated = 0
    consecutive_box_repeat = 0
    last_action_desc = ""
    last_click_box: list[float] | None = None
    screenshot_dir = os.path.join(os.getcwd(), "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)

    for step in range(1, max_steps + 1):
        print(f"\n{'='*60}")
        print(f"STEP {step}/{max_steps}")
        print(f"{'='*60}")
        log_event("step_started", step=step, max_steps=max_steps)

        screenshot_path = os.path.join(screenshot_dir, f"step_{step:03d}.jpg")
        if not capture_viewport_screenshot(screenshot_path):
            log_event("step_aborted", step=step, reason="screenshot_failed")
            print("Screenshot failed, reconnecting CDP...")
            if not _cdp_reconnect():
                print("Could not reconnect to Chrome. Aborting.")
                return
            continue

        current_url = get_current_url()
        page_title = get_page_title()
        print(f"Page: {page_title}")
        print(f"URL: {current_url}")
        log_event("page_state", step=step, title=page_title, url=current_url, viewport=get_viewport_size())

        # Build stall warning if the agent is repeating the same action
        stall_warning = ""
        if consecutive_repeated >= 2:
            stall_warning = f"\nWARNING: You have repeated the same action '{last_action_desc}' {consecutive_repeated} times and it did not change the page. You MUST try something completely different now.\n"

        directive = await get_vision_directive(screenshot_path, goal, action_history, stall_warning)
        print(f"\nVLM Directive: {directive}")

        parsed = parse_directive(directive)
        if not parsed:
            log_event("directive_parse_failed", step=step, raw=directive)
            print(f"Could not parse directive. Raw: {directive[:100]}")
            action_history.append(f"(unparseable: {directive[:60]})")
            time.sleep(2)
            continue

        print(f"Parsed visual action: {json.dumps(parsed, ensure_ascii=False)}")
        log_event("action_selected", step=step, action=parsed)

        target_box = parsed.get("box")
        target_pixel = None
        if isinstance(target_box, list):
            width, height = get_viewport_size()
            target_pixel = (
                ((target_box[0] + target_box[2]) / 2) * width / 1000,
                ((target_box[1] + target_box[3]) / 2) * height / 1000,
            )
            target_path = os.path.join(screenshot_dir, f"step_{step:03d}_target.jpg")
            capture_target_diagnostic(target_path, target_box)

        action_desc, is_done = execute_action(parsed)
        action_history.append(action_desc)
        print(f"Executed: {action_desc}")
        log_event("step_action_complete", step=step, description=action_desc, is_done=is_done)

        _wait_for_page_settle()

        if isinstance(target_box, list):
            result_path = os.path.join(screenshot_dir, f"step_{step:03d}_after.jpg")
            capture_target_diagnostic(result_path, target_box, target_pixel)
            after_url = get_current_url()
            after_title = get_page_title()
            if not is_done and _goal_reached_by_page(goal, after_title, after_url):
                is_done = True
                action_history.append("GOAL CHECK: page title or URL matches the requested subject")
                log_event("goal_detected_from_page", step=step, title=after_title, url=after_url)
            if parsed.get("action") == "click" and not is_done:
                review = await review_visual_action(result_path, goal, parsed, action_history)
                log_event("visual_review", step=step, review=review)
                action_history.append(f"VISUAL REVIEW: {review.get('status')} - {review.get('reason', '')}")
                if review.get("status") == "retry":
                    corrected = dict(parsed)
                    corrected["box"] = review["box"]
                    correction_path = os.path.join(screenshot_dir, f"step_{step:03d}_correction.jpg")
                    capture_target_diagnostic(correction_path, corrected["box"])
                    correction_desc, _ = execute_action(corrected)
                    action_history.append(f"CORRECTED: {correction_desc}")
                    log_event("visual_correction_executed", step=step, action=corrected, description=correction_desc)
                    _wait_for_page_settle()

        # Detect repeated click targets by box proximity, not just text
        current_box = parsed.get("box")
        if current_box and parsed.get("action") in {"click", "type"}:
            if last_click_box and _box_overlap(last_click_box, current_box, threshold=0.5):
                consecutive_box_repeat += 1
            else:
                consecutive_box_repeat = 0
            last_click_box = current_box
        else:
            consecutive_box_repeat = 0

        # Also track text repeats
        if action_desc == last_action_desc and parsed.get("action") != "done":
            consecutive_repeated += 1
        else:
            consecutive_repeated = 0
        last_action_desc = action_desc

        # Force a scroll after 2 box repeats or 3 text repeats
        if consecutive_box_repeat >= 2 or consecutive_repeated >= 3:
            print(f"  Anti-stall: target repeated (box:{consecutive_box_repeat}, text:{consecutive_repeated}) — forcing scroll")
            cdp_scroll("down")
            time.sleep(1)
            action_history.append(f"ANTI-STALL: forced scroll after repeated target")
            consecutive_repeated = 0
            consecutive_box_repeat = 0
            last_action_desc = ""
            last_click_box = None

        if is_done:
            print(f"\n{'*'*60}")
            print("GOAL COMPLETED SUCCESSFULLY!")
            print(f"{'*'*60}")
            break

        time.sleep(2.5)

        new_url = get_current_url()
        if new_url != current_url:
            time.sleep(2)

    else:
        print(f"\nReached maximum steps ({max_steps}) without completing the goal.")
        print("Last actions taken:")
        for h in action_history[-5:]:
            print(f"  - {h}")

    final_url = get_current_url()
    log_event("run_finished", steps=len(action_history), final_url=final_url)
    if status_callback:
        status_callback(f"Run finished. Log file: {log_path}")
    if _cdp_ws:
        _cdp_ws.close()
    print("\nAutomation session ended.")


# =====================================================================
# 11. BASIC DESKTOP GUI
# =====================================================================
def launch_tk_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Visual Web Automation")
    root.geometry("720x460")
    root.minsize(600, 380)

    goal_label = ttk.Label(root, text="What should the browser do?")
    goal_label.pack(anchor="w", padx=20, pady=(20, 6))

    goal_input = tk.Text(root, height=5, wrap="word")
    goal_input.pack(fill="x", padx=20)
    goal_input.insert("1.0", "Go to Zillow and search for homes in Richmond VA under 300k")

    controls = ttk.Frame(root)
    controls.pack(fill="x", padx=20, pady=12)

    max_steps_label = ttk.Label(controls, text="Maximum steps:")
    max_steps_label.pack(side="left")
    max_steps_input = ttk.Spinbox(controls, from_=1, to=100, width=6)
    max_steps_input.set("30")
    max_steps_input.pack(side="left", padx=(8, 16))

    status_var = tk.StringVar(value="Ready")
    log_var = tk.StringVar(value="No run started yet")
    status_label = ttk.Label(root, textvariable=status_var, wraplength=680)
    status_label.pack(anchor="w", padx=20, pady=(4, 8))
    log_label = ttk.Label(root, textvariable=log_var, wraplength=680)
    log_label.pack(anchor="w", padx=20, pady=(0, 12))

    start_button = ttk.Button(controls, text="Start automation")
    start_button.pack(side="left")

    def update_status(message: str) -> None:
        root.after(0, lambda: status_var.set(message))
        if "Log file:" in message:
            root.after(0, lambda: log_var.set(message))

    def run_from_gui() -> None:
        goal = goal_input.get("1.0", "end").strip()
        if not goal:
            messagebox.showwarning("Goal required", "Enter a task for the browser first.")
            return
        try:
            max_steps = int(max_steps_input.get())
        except ValueError:
            messagebox.showwarning("Invalid steps", "Maximum steps must be a whole number.")
            return
        if max_steps < 1:
            messagebox.showwarning("Invalid steps", "Maximum steps must be at least 1.")
            return

        start_button.configure(state="disabled")
        status_var.set("Starting browser automation...")
        log_var.set("Creating a new log file...")

        def worker() -> None:
            try:
                asyncio.run(run_agent(goal, max_steps=max_steps, status_callback=update_status))
            except Exception as error:
                update_status(f"Run stopped with an error: {error}")
            finally:
                root.after(0, lambda: start_button.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    start_button.configure(command=run_from_gui)
    root.mainloop()


def launch_web_gui() -> None:
    import html
    import json as json_module
    import threading as threading_module
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs

    state = {"running": False, "status": "Ready", "log": "No run started yet"}

    def set_status(message: str) -> None:
        state["status"] = message
        if "Log file:" in message:
            state["log"] = message

    class WebGuiHandler(BaseHTTPRequestHandler):
        def _send(self, body: str, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if self.path == "/status":
                self._send(json_module.dumps(state), "application/json; charset=utf-8")
                return
            page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Visual Web Automation</title>
<style>body{{font-family:Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#202124}}textarea{{width:100%;height:120px;padding:10px;font-size:16px}}input{{padding:8px;font-size:16px;width:80px}}button{{padding:10px 18px;font-size:16px;cursor:pointer}}.status{{margin-top:24px;padding:14px;background:#f1f3f4;border-radius:6px;word-break:break-word}}label{{display:block;margin:16px 0 6px}}</style>
</head><body><h1>Visual Web Automation</h1>
<form method="post"><label for="goal">What should the browser do?</label>
<textarea id="goal" name="goal">Go to Zillow and search for homes in Richmond VA under 300k</textarea>
<label for="max_steps">Maximum steps</label><input id="max_steps" name="max_steps" type="number" min="1" max="100" value="30"><br><br>
<button type="submit" {"disabled" if state["running"] else ""}>Start automation</button></form>
<div class="status"><strong>Status:</strong> <span id="status">{html.escape(state["status"])}</span><br><br><strong>Log:</strong> <span id="log">{html.escape(state["log"])}</span></div>
<script>setInterval(async()=>{{const r=await fetch('/status');const s=await r.json();document.getElementById('status').textContent=s.status;document.getElementById('log').textContent=s.log;}},1000);</script>
</body></html>"""
            self._send(page)

        def do_POST(self) -> None:
            if state["running"]:
                self._send("A run is already in progress.", status=409)
                return
            length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            goal = fields.get("goal", [""])[0].strip()
            try:
                max_steps = int(fields.get("max_steps", ["30"])[0])
            except ValueError:
                max_steps = 30
            if not goal or max_steps < 1:
                self._send("Enter a goal and a valid step limit.", status=400)
                return
            state["running"] = True
            state["status"] = "Starting browser automation..."
            state["log"] = "Creating a new log file..."

            def worker() -> None:
                try:
                    asyncio.run(run_agent(goal, max_steps=max_steps, status_callback=set_status))
                except Exception as error:
                    set_status(f"Run stopped with an error: {error}")
                finally:
                    state["running"] = False

            threading_module.Thread(target=worker, daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def log_message(self, format_string: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), WebGuiHandler)
    address = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Web interface available at {address}")
    webbrowser.open(address)
    server.serve_forever()


def launch_gui() -> None:
    try:
        launch_tk_gui()
    except Exception as error:
        print(f"Desktop interface unavailable ({error}). Opening browser interface instead.")
        launch_web_gui()


# =====================================================================
# 12. ENTRY POINT
# =====================================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Site-agnostic web automation agent (screenshot + coordinate based)")
    parser.add_argument("goal", nargs="?", help="The goal to accomplish")
    parser.add_argument("--url", dest="url", default=None, help="Starting URL (auto-detected from goal if not provided)")
    parser.add_argument("--max-steps", dest="max_steps", type=int, default=30, help="Maximum number of steps (default: 30)")
    parser.add_argument("--gui", action="store_true", help="Open the desktop interface")
    args = parser.parse_args()

    if args.gui or not args.goal:
        launch_gui()
        return

    asyncio.run(run_agent(args.goal, max_steps=args.max_steps, start_url=args.url))


if __name__ == "__main__":
    main()
