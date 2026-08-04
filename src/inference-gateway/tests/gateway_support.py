import base64
import hmac
import json

from app.budget import REDIS_SETTLE_SCRIPT
from app.settings import Settings
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


class FakeRuntimeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.stream_chunks = [b'data: {"choices":[]}\n\n']
        self.payload = None
        self.headers = None
        self.backend = None
        self.health_backends = []
        self.calls = 0

    async def chat_completions(self, payload, headers=None, backend=None):
        self.calls += 1
        self.payload = payload
        self.headers = headers or {}
        self.backend = backend
        if self.error:
            raise self.error
        return self.response

    async def stream_chat_completions(self, payload, headers=None, backend=None):
        self.calls += 1
        self.payload = payload
        self.headers = headers or {}
        self.backend = backend
        if self.error:
            raise self.error
        for chunk in self.stream_chunks:
            yield chunk

    async def embeddings(self, payload, headers=None, backend=None):
        self.calls += 1
        self.payload = payload
        self.headers = headers or {}
        self.backend = backend
        if self.error:
            raise self.error
        return self.response

    async def completions(self, payload, headers=None, backend=None):
        self.calls += 1
        self.payload = payload
        self.headers = headers or {}
        self.backend = backend
        if self.error:
            raise self.error
        return self.response

    async def health(self, backend=None):
        self.health_backends.append(backend)
        if self.error:
            raise self.error
        return {"status": "ok", "backend": backend}


class FakeRedisBudgetStore:
    def __init__(self):
        self.data = {}

    def hgetall(self, key):
        return dict(self.data.get(key, {}))

    def ttl(self, key):
        return 86400 if key in self.data else -2

    def ping(self):
        return True

    def eval(self, script, numkeys, key, *args):
        # The tracker runs two Lua scripts against this client; dispatch on which one so
        # the fake keeps the same reserve/settle split as the real backend.
        if script is REDIS_SETTLE_SCRIPT or "HSET" in script:
            return self._eval_settle(key, *args)
        return self._eval_reserve(key, *args)

    def _eval_settle(self, key, reserved, actual):
        if key not in self.data:
            return [0, 0, 0, 0]
        current = self.data[key]["estimated_tokens"]
        refund = min(max(int(reserved) - int(actual), 0), max(current, 0))
        overrun = max(int(actual) - int(reserved), 0)
        settled = current - refund + overrun
        self.data[key]["estimated_tokens"] = settled
        return [1, refund, overrun, settled]

    def _eval_reserve(
        self,
        key,
        ttl,
        add_requests,
        add_prompt_chars,
        add_estimated_tokens,
        limit_requests,
        limit_prompt_chars,
        limit_estimated_tokens,
    ):
        current = self.data.get(
            key,
            {"requests": 0, "prompt_chars": 0, "estimated_tokens": 0},
        )
        proposed = {
            "requests": current["requests"] + int(add_requests),
            "prompt_chars": current["prompt_chars"] + int(add_prompt_chars),
            "estimated_tokens": current["estimated_tokens"] + int(add_estimated_tokens),
        }
        checks = (
            ("requests", int(limit_requests), "sandbox_request_budget_exceeded", "request"),
            ("prompt_chars", int(limit_prompt_chars), "sandbox_prompt_budget_exceeded", "prompt character"),
            ("estimated_tokens", int(limit_estimated_tokens), "sandbox_token_budget_exceeded", "estimated token"),
        )
        for field, limit, reason, label in checks:
            if limit > 0 and proposed[field] > limit:
                return [0, reason, label, proposed[field], limit]
        self.data[key] = proposed
        return [
            1,
            proposed["requests"],
            proposed["prompt_chars"],
            proposed["estimated_tokens"],
        ]


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _tamper_jwt_signature(token: str) -> str:
    header, claims, signature = token.split(".")
    signature_bytes = bytearray(_b64url_decode(signature))
    signature_bytes[0] ^= 0x01
    return f"{header}.{claims}.{_b64url(bytes(signature_bytes))}"


def _signed_hs256_jwt(secret: bytes, claims: dict, kid: str = "test-key") -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = hmac.new(secret, signing_input, "sha256").digest()
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def _signed_rs256_jwt(private_key, claims: dict, kid: str = "test-rsa") -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def _signed_es256_jwt(private_key, claims: dict, kid: str = "test-ec") -> str:
    header = {"alg": "ES256", "typ": "JWT", "kid": kid}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{encoded_header}.{encoded_claims}.{_b64url(raw_signature)}"


def _rsa_jwk(private_key, kid: str = "test-rsa") -> dict:
    public_numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url(public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")),
    }


def _ec_jwk(private_key, kid: str = "test-ec") -> dict:
    public_numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "kid": kid,
        "use": "sig",
        "alg": "ES256",
        "crv": "P-256",
        "x": _b64url(public_numbers.x.to_bytes(32, "big")),
        "y": _b64url(public_numbers.y.to_bytes(32, "big")),
    }


def _tool_settings(**overrides):
    base = {
        "runtime_backend": "vllm",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
    }
    base.update(overrides)
    return Settings(**base)


def _budget_header_settings(**overrides):
    base = {
        "sandbox_budget_enabled": True,
        "sandbox_request_budget": 5,
        "sandbox_estimated_token_budget": 1000,
        "budget_estimated_chars_per_token": 4,
    }
    base.update(overrides)
    return _tool_settings(**base)


def _retry_settings(**overrides):
    base = {
        "runtime_backend": "ollama",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
        "runtime_max_retries": 1,
        "runtime_retry_backoff_seconds": 0.001,
    }
    base.update(overrides)
    return Settings(**base)
