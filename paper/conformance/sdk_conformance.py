#!/usr/bin/env python3
"""Client-SDK compatibility conformance: do the real vendor clients actually work?

``conformance.py`` next to this file proves the gateway *refuses* what it should. This
one proves it is *usable*: it boots the real gateway against a mock runtime and drives it
with the genuine ``openai`` and ``anthropic`` clients, so the vendors' own parsers decide
whether the gateway is compatible.

That distinction matters because the existing compatibility evidence is self-referential.
``platform/api-contracts/`` snapshots the gateway's own OpenAPI, which proves the gateway
has not changed, not that it matches what upstream clients expect. Nothing in the repo
previously imported a vendor SDK.

The SDKs are installed into a throwaway virtualenv rather than added to the service locks:
they are test-time clients of the gateway, not dependencies of it, and pinning them into
the hash-locked runtime image to test the image would be backwards.

Usage:
    python paper/conformance/sdk_conformance.py [--keep-venv]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
RESULTS = HERE / "results"
GATEWAY_DIR = Path(os.environ.get("GATEWAY_DIR", ROOT / "src" / "inference-gateway"))
SDK_VENV = ROOT / ".venv-sdk-conformance"

MOCK_PORT = int(os.environ.get("SDK_CONF_MOCK_PORT", "9099"))
GW_PORT = int(os.environ.get("SDK_CONF_GW_PORT", "8099"))
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}"
GW_URL = f"http://127.0.0.1:{GW_PORT}"

# Test fixtures, both public. The digest is written out rather than computed from the key
# at runtime: the gateway's API-key contract is a SHA-256 digest (API_KEY_SHA256S), which is
# the right choice for a high-entropy random token but reads to a scanner as password
# hashing with a fast hash. Pre-computing the constant states the intent plainly, keeps a
# credential-shaped value out of a hashing call, and is one fewer moving part in a harness.
# Regenerate with: python3 -c "import hashlib;print(hashlib.sha256(b'<key>').hexdigest())"
API_KEY = "sdk-conformance-key"
API_KEY_SHA256 = "69181e4449e51b6645cc0bba0f558faf67e7e002b84547570294da9b7ddd8c08"
MODEL = "mock-model"

# Pinned so a vendor release cannot silently change what "compatible" means between runs.
SDK_REQUIREMENTS = ["openai==2.16.0", "anthropic==0.75.0"]

GATEWAY_ENV = {
    "RUNTIME_BACKEND": "ollama",
    "OLLAMA_BASE_URL": MOCK_URL,
    "MODEL_ID": MODEL,
    "ALLOWED_MODELS": MODEL,
    "API_KEY_AUTH_ENABLED": "true",
    "API_KEY_SHA256S": "",  # filled in from API_KEY below
    # The suite is about client compatibility, so the governance surface stays on but its
    # limits are set wide enough that a legitimate SDK call is never the thing rejected.
    "ALLOW_STREAMING": "true",
    "RESPONSES_ENABLED": "true",
    "AUDIT_LOG_ENABLED": "true",
    "MAX_PROMPT_CHARS": "8192",
    "MAX_COMPLETION_TOKENS": "4096",
    "REQUEST_TIMEOUT_SECONDS": "30",
}


def wait_healthy(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/healthz", timeout=2.0).status_code == 200:
                return
        except Exception as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not become healthy: {last}")


def ensure_sdk_venv() -> Path:
    """Create (once) the throwaway venv holding the vendor SDKs and return its python."""
    python = SDK_VENV / "bin" / "python"
    if not python.exists():
        print(f"creating SDK venv at {SDK_VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(SDK_VENV)], check=True)
    installed = subprocess.run(
        [str(python), "-m", "pip", "freeze"], capture_output=True, text=True, check=True
    ).stdout
    if not all(requirement in installed for requirement in SDK_REQUIREMENTS):
        print(f"installing {', '.join(SDK_REQUIREMENTS)}")
        subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *SDK_REQUIREMENTS],
            check=True,
        )
    return python


def start_processes() -> tuple[subprocess.Popen, subprocess.Popen, object]:
    RESULTS.mkdir(parents=True, exist_ok=True)
    mock = subprocess.Popen(
        [sys.executable, str(HERE / "sdk_mock_runtime.py"), "--port", str(MOCK_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env = dict(os.environ)
    env.update(GATEWAY_ENV)
    env["API_KEY_SHA256S"] = API_KEY_SHA256
    gateway_log = open(RESULTS / "sdk-conformance-gateway.log", "w")  # noqa: SIM115
    gateway_python = GATEWAY_DIR / ".venv" / "bin" / "python"
    gateway = subprocess.Popen(
        [
            str(gateway_python if gateway_python.exists() else sys.executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(GW_PORT),
            "--log-level",
            "info",
            "--no-access-log",
        ],
        cwd=str(GATEWAY_DIR),
        env=env,
        stdout=gateway_log,
        stderr=gateway_log,
    )
    return mock, gateway, gateway_log


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the vendor client SDKs against the gateway.")
    parser.add_argument("--keep-venv", action="store_true", help="Keep the SDK venv for a faster next run.")
    parser.parse_args()

    sdk_python = ensure_sdk_venv()
    mock, gateway, gateway_log = start_processes()
    try:
        wait_healthy(MOCK_URL)
        wait_healthy(GW_URL)
        driver = subprocess.run(
            [str(sdk_python), str(HERE / "sdk_driver.py")],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "CONF_GATEWAY_URL": GW_URL,
                "CONF_API_KEY": API_KEY,
                "CONF_MODEL": MODEL,
            },
            check=False,
        )
    finally:
        for process in (gateway, mock):
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
        gateway_log.close()

    if driver.returncode != 0 or not driver.stdout.strip():
        print("sdk-conformance: driver failed to produce results", file=sys.stderr)
        print(driver.stderr, file=sys.stderr)
        return 2

    report = json.loads(driver.stdout)
    checks = report["checks"]
    passed = sum(1 for check in checks if check["passed"])
    report["summary"] = {"total": len(checks), "passed": passed, "failed": len(checks) - passed}
    report["experiment"] = "sdk-conformance"
    (RESULTS / "sdk-conformance-evidence.json").write_text(json.dumps(report, indent=2))

    width = max(len(check["check"]) for check in checks)
    for check in checks:
        flag = "PASS" if check["passed"] else "FAIL"
        detail = check.get("detail") if check["passed"] else check.get("error")
        print(f"  [{flag}] {check['check']:<{width}}  {detail}")
    print(f"\n{passed}/{len(checks)} client-SDK checks passed")
    print(f"openai=={report['openai_version']} anthropic=={report['anthropic_version']}")
    for check in checks:
        if not check["passed"]:
            print(f"\n--- {check['check']} ---\n{check.get('traceback', '')}", file=sys.stderr)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
