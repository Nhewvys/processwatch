"""Test-only Turnstile interaction helpers for the official localhost demo.

These helpers reject non-loopback pages and non-interactive-test sitekeys. They
must not be used by ProcessWatch connectors or against tribunal portals.
"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit

TEST_SITEKEY = "3x00000000000000000000FF"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_test_target(url: str, sitekey: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("O adaptador de teste aceita somente HTTP em loopback.")
    if sitekey != TEST_SITEKEY:
        raise ValueError("O adaptador aceita somente a sitekey interativa oficial de teste.")


def classify_test_result(*, widget: bool, interaction: bool, callback: bool,
                         http_status: int | None, success: bool, test_mode: bool) -> str:
    if not widget:
        return "A_widget_ausente"
    if interaction and callback and http_status == 200 and success and test_mode:
        return "D_interacao_e_validacao_confirmadas_em_teste_oficial"
    if interaction:
        return "C_interacao_sem_validacao_confirmada"
    if callback and test_mode:
        return "B_chave_de_teste_sem_interacao_confirmada"
    return "A_widget_sem_interacao"


def _value(value):
    return getattr(value, "value", value)


async def interact_nodriver_test_widget(tab, current_url: str, *, timeout: float = 15) -> dict:
    """Exercise Nodriver only against the official interactive localhost fixture."""
    raw = _value(await tab.evaluate(
        "JSON.stringify(document.querySelector('.cf-turnstile')?.getAttribute('data-sitekey')||'')",
        return_by_value=True))
    sitekey = json.loads(raw) if raw else ""
    validate_test_target(current_url, sitekey)
    widget = bool(_value(await tab.evaluate(
        "Boolean(document.querySelector('.cf-turnstile'))", return_by_value=True)))
    if not widget:
        return {"widget": False, "interaction": False, "callback": False,
                "http_status": None, "success": False, "test_mode": False,
                "outcome": "A_widget_ausente"}

    interaction = False
    pointer_click = False
    api_error = None
    try:
        await asyncio.wait_for(tab.verify_cf(), timeout=timeout)
        interaction = True
    except Exception as exc:
        api_error = type(exc).__name__

    callback = bool(_value(await tab.evaluate(
        "Boolean(window.turnstileTestToken)", return_by_value=True)))
    if not callback:
        rect_json = _value(await tab.evaluate(
            "JSON.stringify((()=>{const r=document.querySelector('.cf-turnstile').getBoundingClientRect();return {x:r.x,y:r.y}})())",
            return_by_value=True))
        rect = json.loads(rect_json)
        await asyncio.wait_for(tab.mouse_click(rect["x"] + 26, rect["y"] + 34), timeout=timeout)
        interaction = True
        pointer_click = True

    deadline = asyncio.get_running_loop().time() + timeout
    result = None
    while asyncio.get_running_loop().time() < deadline:
        raw_result = _value(await tab.evaluate(
            "JSON.stringify(window.turnstileTestResult)", return_by_value=True))
        if raw_result and raw_result != "null":
            result = json.loads(raw_result)
            break
        await asyncio.sleep(0.15)
    callback = bool(_value(await tab.evaluate(
        "Boolean(window.turnstileTestToken)", return_by_value=True)))
    result = result if isinstance(result, dict) else {}
    safe = {"widget": True, "interaction": interaction, "callback": callback,
            "pointer_click": pointer_click,
            "http_status": result.get("http_status"), "success": result.get("success") is True,
            "test_mode": result.get("test_mode") is True, "api_error": api_error}
    safe["outcome"] = classify_test_result(**{key: safe[key] for key in (
        "widget", "interaction", "callback", "http_status", "success", "test_mode")})
    return safe


def interact_seleniumbase_test_widget(driver, *, timeout: float = 15) -> dict:
    """Use ordinary WebDriver pointer input on the official localhost fixture."""
    from selenium.webdriver import ActionChains

    current_url = driver.current_url
    sitekey = driver.execute_script(
        "return document.querySelector('.cf-turnstile')?.getAttribute('data-sitekey')||''")
    validate_test_target(current_url, sitekey)
    widget = bool(driver.execute_script("return Boolean(document.querySelector('.cf-turnstile'))"))
    if not widget:
        return {"widget": False, "interaction": False, "callback": False,
                "http_status": None, "success": False, "test_mode": False,
                "outcome": "A_widget_ausente"}
    rect = driver.execute_script(
        "const r=document.querySelector('.cf-turnstile').getBoundingClientRect();return {x:r.x,y:r.y}")
    ActionChains(driver).move_by_offset(round(rect["x"] + 26), round(rect["y"] + 34)).click().perform()
    callback = False
    result = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        callback, result = driver.execute_script(
            "return [Boolean(window.turnstileTestToken),window.turnstileTestResult]")
        if result:
            break
        time.sleep(0.15)
    result = result if isinstance(result, dict) else {}
    safe = {"widget": True, "interaction": True, "pointer_click": True, "callback": bool(callback),
            "http_status": result.get("http_status"), "success": result.get("success") is True,
            "test_mode": result.get("test_mode") is True}
    safe["outcome"] = classify_test_result(**{key: safe[key] for key in (
        "widget", "interaction", "callback", "http_status", "success", "test_mode")})
    return safe
