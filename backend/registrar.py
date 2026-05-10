"""
Refactored Novaex AI account registrar.

Adapted from the original ``novaex_register.py`` script so it can be driven
programmatically (e.g. from a Telegram bot) with:

  * a progress callback that receives structured events
  * a cooperative ``cancel_event`` that stops new work but keeps every
    account that has already been written to the CSV

Per account, on success a row is appended to the CSV immediately, so a
mid-run cancel never loses already-created accounts.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import queue
import random
import re
import secrets
import string
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from dataclasses import dataclass, field
from typing import Callable, Optional

import pyotp
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from faker import Faker


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NOVAEX_BASE = "https://m.novaexai.com/prod-api"
MAILTM_BASE = "https://api.mail.tm"
TOTP_APP_BASE = "https://totp.app/"
DEFAULT_INVITE = os.environ.get("DEFAULT_INVITE_CODE", "3tb84z")
MAX_THREADS = 10

USER_AGENT_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.2; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
]

HTTP_TIMEOUT = (8, 25)
HTTP_TIMEOUT_FAST = (8, 15)

PROXY_EXC = (
    requests.exceptions.ProxyError,
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.SSLError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
)
PROXY_BAD_STATUS = {407, 408, 502, 503, 504, 522, 523, 524, 525, 526}

CSV_HEADER = [
    "created_at", "novaex_username", "novaex_password",
    "email", "mailtm_password",
    "ga_secret", "otpauth_url", "totp_app_url",
    "invite_code", "user_code", "token",
    "device_id", "user_agent", "proxy", "cookies",
]

CSV_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Errors / helpers
# ---------------------------------------------------------------------------
class ProxyDeadError(Exception):
    pass


class CancelledError(Exception):
    pass


def _maybe_proxy_error_from_status(status_code: int):
    if status_code in PROXY_BAD_STATUS:
        raise ProxyDeadError(f"proxy returned HTTP {status_code}")


def _is_proxy_failure(exc: BaseException) -> bool:
    if isinstance(exc, ProxyDeadError):
        return True
    if isinstance(exc, PROXY_EXC):
        return True
    msg = str(exc)
    if any(s in msg for s in ("unexpected IV length", "AES-GCM", "non-JSON", "InvalidTag")):
        return True
    return False


def pick_user_agent() -> str:
    return random.choice(USER_AGENT_POOL)


def decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
    except Exception:
        return {}


def proxy_label(p: Optional[str]) -> str:
    if not p:
        return "(no-proxy)"
    try:
        u = urllib.parse.urlparse(p)
        return f"{u.hostname}:{u.port}"
    except Exception:
        return p


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------
class InvitePool:
    def __init__(self, codes=None):
        self._lock = threading.Lock()
        self._codes = list(codes or [])

    def add(self, code: str):
        if not code:
            return
        with self._lock:
            self._codes.append(code)

    def pick(self) -> Optional[str]:
        with self._lock:
            return random.choice(self._codes) if self._codes else None

    def size(self) -> int:
        with self._lock:
            return len(self._codes)


class ProxyPool:
    def __init__(self, lines):
        self._all = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self._all.append(self._normalize(line))
        if not self._all:
            raise ValueError("proxy file is empty")
        random.shuffle(self._all)
        self._q: "queue.Queue[str]" = queue.Queue()
        for p in self._all:
            self._q.put(p)

    @staticmethod
    def _normalize(line: str) -> str:
        if line.startswith("http://") or line.startswith("https://"):
            return line
        if "@" in line:
            return f"http://{line}"
        parts = line.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            return f"http://{user}:{pwd}@{host}:{port}"
        return f"http://{line}"

    def acquire(self) -> str:
        return self._q.get()

    def release(self, proxy: str):
        self._q.put(proxy)

    def size(self) -> int:
        return len(self._all)


# ---------------------------------------------------------------------------
# Crypto / HTTP
# ---------------------------------------------------------------------------
def parse_pub(b64_der: str):
    return serialization.load_der_public_key(base64.b64decode(b64_der))


def encode_pub_der(public_key) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def derive_aes_key(shared_secret: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=b"ECIES-AES-KEY").derive(shared_secret)


def encrypt_request(server_pub_b64: str, payload: dict):
    server_pub = parse_pub(server_pub_b64)
    eph_priv = ec.generate_private_key(ec.SECP256R1())
    aes_key = derive_aes_key(eph_priv.exchange(ec.ECDH(), server_pub))
    iv = os.urandom(12)
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(aes_key).encrypt(iv, plaintext, None)
    return {
        "ephemeralPublicKey": base64.b64encode(encode_pub_der(eph_priv.public_key())).decode(),
        "encryptedData": base64.b64encode(ct).decode(),
        "iv": base64.b64encode(iv).decode(),
        "authTag": "",
    }, eph_priv


def parse_iv_from_x509(data: bytes) -> bytes:
    n = 0
    if data[n] == 0x04:
        n += 1
        length = data[n]; n += 1
        if length & 0x80:
            cnt = length & 0x7F; length = 0
            for _ in range(cnt):
                length = (length << 8) | data[n]; n += 1
        return data[n:n + length]
    if data[n] == 0x30:
        n += 1
        length = data[n]; n += 1
        if length & 0x80:
            cnt = length & 0x7F; length = 0
            for _ in range(cnt):
                length = (length << 8) | data[n]; n += 1
        if data[n] == 0x04:
            n += 1
            l2 = data[n]; n += 1
            if l2 & 0x80:
                cnt = l2 & 0x7F; l2 = 0
                for _ in range(cnt):
                    l2 = (l2 << 8) | data[n]; n += 1
            return data[n:n + l2]
    return data


def _b64_clean(s: str) -> str:
    s = re.sub(r"\s+", "", s).replace("-", "+").replace("_", "/")
    while len(s) % 4:
        s += "="
    return s


def decrypt_response(eph_priv, server_pub_b64, body: dict) -> dict:
    server_pub = parse_pub(server_pub_b64)
    aes_key = derive_aes_key(eph_priv.exchange(ec.ECDH(), server_pub))
    iv = parse_iv_from_x509(base64.b64decode(_b64_clean(body["iv"])))
    if len(iv) != 12:
        raise ValueError(f"unexpected IV length {len(iv)}")
    ct = base64.b64decode(_b64_clean(body["encryptedData"]))
    if body.get("authTag"):
        ct += base64.b64decode(_b64_clean(body["authTag"]))
    return json.loads(AESGCM(aes_key).decrypt(iv, ct, None).decode("utf-8"))


def base_headers(device_id: str, user_agent: str):
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://m.novaexai.com",
        "Referer": "https://m.novaexai.com/register",
        "User-Agent": user_agent,
        "lang": "en",
        "deviceid": device_id,
        "X-App-Version": "1.0",
    }


def make_session(device_id: str, user_agent: str, proxy: Optional[str]) -> requests.Session:
    s = requests.Session()
    s.headers.update(base_headers(device_id, user_agent))
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def get_server_public_key(session) -> str:
    try:
        r = session.get(f"{NOVAEX_BASE}/security/ecies-public-key", timeout=HTTP_TIMEOUT_FAST)
    except PROXY_EXC as e:
        raise ProxyDeadError(str(e))
    _maybe_proxy_error_from_status(r.status_code)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 200:
        raise RuntimeError(f"fetch public key failed: {body}")
    return body["data"]["publicKey"]


def encrypted_post(session, path, payload, server_pub, *, token=None):
    enc, eph = encrypt_request(server_pub, payload)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = session.post(f"{NOVAEX_BASE}{path}",
                         data=json.dumps(enc, separators=(",", ":")),
                         headers=headers, timeout=HTTP_TIMEOUT)
    except PROXY_EXC as e:
        raise ProxyDeadError(str(e))
    _maybe_proxy_error_from_status(r.status_code)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"non-JSON [{r.status_code}]: {r.text[:200]}")
    if isinstance(body, dict) and "encryptedData" in body and "iv" in body:
        srv_pub = body.get("ephemeralPublicKey") or server_pub
        return decrypt_response(eph, srv_pub, body)
    return body


def plain_get(session, path, *, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = session.get(f"{NOVAEX_BASE}{path}", headers=headers, timeout=HTTP_TIMEOUT_FAST)
    except PROXY_EXC as e:
        raise ProxyDeadError(str(e))
    _maybe_proxy_error_from_status(r.status_code)
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"non-JSON [{r.status_code}]: {r.text[:200]}")


def mailtm_pick_domain(session) -> str:
    try:
        r = session.get(f"{MAILTM_BASE}/domains?page=1", timeout=HTTP_TIMEOUT_FAST,
                        headers={"Accept": "application/ld+json"})
    except PROXY_EXC as e:
        raise ProxyDeadError(str(e))
    _maybe_proxy_error_from_status(r.status_code)
    r.raise_for_status()
    members = r.json().get("hydra:member", [])
    active = [d["domain"] for d in members if d.get("isActive")]
    if not active:
        raise RuntimeError("no active mail.tm domains")
    return random.choice(active)


def mailtm_create_account(session, address: str, password: str):
    try:
        r = session.post(f"{MAILTM_BASE}/accounts",
                         json={"address": address, "password": password},
                         headers={"Content-Type": "application/json"},
                         timeout=HTTP_TIMEOUT_FAST)
    except PROXY_EXC as e:
        raise ProxyDeadError(str(e))
    _maybe_proxy_error_from_status(r.status_code)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"mail.tm create [{r.status_code}]: {r.text[:120]}")
    return r.json()


# ---------------------------------------------------------------------------
# Credential generation
# ---------------------------------------------------------------------------
def gen_username(fake: Faker) -> str:
    base = re.sub(r"[^A-Za-z]", "", fake.first_name()).lower()
    while len(base) < 5:
        base += secrets.choice(string.ascii_lowercase)
    digits = "".join(secrets.choice(string.digits) for _ in range(3))
    username = base + digits
    while len(username) < 8:
        username += secrets.choice(string.digits)
    return username


def gen_password(length: int = 10) -> str:
    letters = [secrets.choice(string.ascii_lowercase) for _ in range(length - 4)]
    letters += [secrets.choice(string.ascii_uppercase) for _ in range(2)]
    digits = [secrets.choice(string.digits) for _ in range(2)]
    chars = letters + digits
    random.shuffle(chars)
    return "".join(chars)


def make_totp_app_url(secret: str, label: str) -> str:
    qs = urllib.parse.urlencode({"secret": secret, "issuer": "NovaEX AI", "account": label})
    return f"{TOTP_APP_BASE}?{qs}"


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def ensure_csv_header(path: str):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
        return
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), [])
    except Exception:
        existing_header = []
    if existing_header != CSV_HEADER:
        backup = path + ".legacy.csv"
        n = 1
        while os.path.exists(backup):
            backup = f"{path}.legacy.{n}.csv"
            n += 1
        os.rename(path, backup)
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)


def append_csv(path: str, row: list):
    with CSV_LOCK:
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)


def load_invite_codes_from_csv(path: str) -> list:
    if not os.path.exists(path):
        return []
    codes = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = (row.get("user_code") or "").strip()
                if not code:
                    payload = decode_jwt_payload(row.get("token") or "")
                    code = (payload.get("userCode") or "").strip()
                if code:
                    codes.append(code)
    except Exception:
        return []
    return codes


def count_csv_rows(path: str) -> int:
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Per-account workflow
# ---------------------------------------------------------------------------
def register_one(session, fake, server_pub, mail_domain, invite, retries):
    last_err = None
    for _ in range(1, retries + 1):
        username = gen_username(fake)
        password = gen_password()
        email = f"{username}@{mail_domain}"
        try:
            avail = encrypted_post(session, "/user/username/exist",
                                   {"username": username}, server_pub)
            if avail.get("code") == 200 and avail.get("data") is True:
                continue
        except Exception as e:
            if _is_proxy_failure(e):
                raise
        try:
            mailtm_create_account(session, email, password)
        except Exception as e:
            if _is_proxy_failure(e):
                raise
            last_err = e
            time.sleep(1)
            continue
        payload = {
            "username": username, "password": password,
            "passwordConfirm": password, "inviteCode": invite,
            "fbp": "", "fbc": "", "fbclid": "", "route_code": "",
        }
        try:
            res = encrypted_post(session, "/user/registered", payload, server_pub)
        except Exception as e:
            if _is_proxy_failure(e):
                raise
            last_err = e
            continue
        code = res.get("code")
        if code == 200:
            return {"username": username, "password": password, "email": email,
                    "token": res.get("data") or ""}
        if code == 1003:
            continue
        if code == 1001:
            raise RuntimeError(f"invite code {invite!r} rejected by server")
        last_err = res
        time.sleep(1)
    raise RuntimeError(f"register failed: {last_err}")


def bind_google_auth(session, server_pub, account):
    token = account["token"]
    cred = plain_get(session, "/validator/getCredential", token=token)
    if cred.get("code") != 200 or not cred.get("data"):
        raise RuntimeError(f"getCredential failed: {cred}")
    secret = cred["data"]["secret"]
    otpauth = cred["data"].get("otpAuthURL", "")
    totp = pyotp.TOTP(secret)
    remaining = totp.interval - (int(time.time()) % totp.interval)
    if remaining <= 2:
        time.sleep(remaining + 1)
    code = totp.now()
    res = encrypted_post(session, "/validator/bound", {"code": int(code)},
                         server_pub, token=token)
    if res.get("code") != 200:
        time.sleep(31)
        code = pyotp.TOTP(secret).now()
        res = encrypted_post(session, "/validator/bound", {"code": int(code)},
                             server_pub, token=token)
    if res.get("code") != 200:
        raise RuntimeError(f"validator/bound failed: {res}")
    return secret, otpauth


# ---------------------------------------------------------------------------
# Job orchestrator
# ---------------------------------------------------------------------------
@dataclass
class JobConfig:
    count: int
    threads: int = 3
    invite: str = DEFAULT_INVITE
    invite_mode: str = "random"          # "random" or "fixed"
    no_ga: bool = False
    retries: int = 4
    max_proxy_swaps: int = 3
    proxy_file: str = "data/proxies.txt"
    csv_path: str = "data/accounts.csv"


@dataclass
class JobStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.time)
    cancelled: bool = False
    last_message: str = ""


def _emit(progress_cb: Optional[Callable], event: dict):
    if progress_cb:
        try:
            progress_cb(event)
        except Exception:
            pass


def _worker(idx, cfg: JobConfig, fake, pool: Optional[ProxyPool], invite_pool: InvitePool,
            mail_domain, server_pub_initial, cancel_event: threading.Event,
            progress_cb: Optional[Callable], stats: JobStats):
    if cancel_event.is_set():
        return False, "cancelled"

    proxy = pool.acquire() if pool else None
    device_id = str(uuid.uuid4())
    user_agent = pick_user_agent()
    session = make_session(device_id, user_agent, proxy)

    if cfg.invite_mode == "fixed":
        invite = cfg.invite
    else:
        invite = invite_pool.pick() or cfg.invite

    swaps_left = cfg.max_proxy_swaps
    server_pub = server_pub_initial

    account = None
    while account is None:
        if cancel_event.is_set():
            if pool and proxy:
                pool.release(proxy)
            return False, "cancelled"
        try:
            try:
                server_pub = get_server_public_key(session)
            except Exception as e:
                if _is_proxy_failure(e):
                    raise
            account = register_one(session, fake, server_pub, mail_domain,
                                   invite, cfg.retries)
        except Exception as e:
            if _is_proxy_failure(e) and pool and swaps_left > 0:
                pool.release(proxy)
                proxy = pool.acquire()
                session = make_session(device_id, user_agent, proxy)
                swaps_left -= 1
                continue
            if pool and proxy:
                pool.release(proxy)
            return False, f"register: {e}"

    user_code = (decode_jwt_payload(account["token"]).get("userCode") or "").strip()
    if user_code:
        invite_pool.add(user_code)

    secret, otpauth = "", ""
    if not cfg.no_ga:
        ga_done = False
        while not ga_done:
            if cancel_event.is_set():
                break
            try:
                secret, otpauth = bind_google_auth(session, server_pub, account)
                ga_done = True
            except Exception as e:
                if _is_proxy_failure(e) and pool and swaps_left > 0:
                    pool.release(proxy)
                    proxy = pool.acquire()
                    session = make_session(device_id, user_agent, proxy)
                    swaps_left -= 1
                    continue
                break

    totp_url = make_totp_app_url(secret, account["email"]) if secret else ""
    try:
        cookies_json = json.dumps(
            {c.name: c.value for c in session.cookies},
            ensure_ascii=False, separators=(",", ":"),
        )
    except Exception:
        cookies_json = "{}"

    append_csv(cfg.csv_path, [
        int(time.time()),
        account["username"], account["password"],
        account["email"], account["password"],
        secret, otpauth, totp_url,
        invite, user_code, account["token"],
        device_id, user_agent, proxy_label(proxy), cookies_json,
    ])

    if pool and proxy:
        pool.release(proxy)

    stats.success += 1
    _emit(progress_cb, {
        "type": "account_created",
        "idx": idx,
        "username": account["username"],
        "email": account["email"],
        "password": account["password"],
        "ga": bool(secret),
        "stats": stats_snapshot(stats),
    })
    return True, account["email"]


def stats_snapshot(s: JobStats) -> dict:
    return {
        "success": s.success,
        "failed": s.failed,
        "total": s.total,
        "elapsed": int(time.time() - s.started_at),
        "cancelled": s.cancelled,
    }


def run_job(cfg: JobConfig, cancel_event: threading.Event,
            progress_cb: Optional[Callable] = None) -> JobStats:
    """Run a registration job. Returns final stats."""
    stats = JobStats(total=cfg.count)

    # Load proxies (optional - if file empty/missing run direct)
    pool: Optional[ProxyPool] = None
    if os.path.exists(cfg.proxy_file):
        try:
            with open(cfg.proxy_file, "r", encoding="utf-8") as f:
                pool = ProxyPool(f.readlines())
        except Exception:
            pool = None

    _emit(progress_cb, {"type": "phase", "name": "bootstrap",
                        "message": f"Memuat {pool.size() if pool else 0} proxy, mengambil ECIES key & domain mail.tm..."})

    fake = Faker()
    invite_pool = InvitePool(
        load_invite_codes_from_csv(cfg.csv_path) if cfg.invite_mode == "random" else []
    )

    # Bootstrap pubkey + mail domain (with proxy auto-swap)
    swaps_left = max(cfg.max_proxy_swaps, 5)
    boot_proxy = pool.acquire() if pool else None
    boot_session = None
    server_pub_initial = None
    while server_pub_initial is None:
        if cancel_event.is_set():
            stats.cancelled = True
            return stats
        boot_session = make_session(str(uuid.uuid4()), pick_user_agent(), boot_proxy)
        try:
            server_pub_initial = get_server_public_key(boot_session)
        except Exception as e:
            if _is_proxy_failure(e) and pool and swaps_left > 0:
                pool.release(boot_proxy)
                boot_proxy = pool.acquire()
                swaps_left -= 1
                continue
            if pool and boot_proxy:
                pool.release(boot_proxy)
            _emit(progress_cb, {"type": "error", "message": f"Bootstrap pubkey gagal: {e}"})
            return stats

    mail_domain = None
    while mail_domain is None:
        if cancel_event.is_set():
            stats.cancelled = True
            if pool and boot_proxy:
                pool.release(boot_proxy)
            return stats
        try:
            mail_domain = mailtm_pick_domain(boot_session)
        except Exception as e:
            if _is_proxy_failure(e) and pool and swaps_left > 0:
                pool.release(boot_proxy)
                boot_proxy = pool.acquire()
                boot_session = make_session(str(uuid.uuid4()), pick_user_agent(), boot_proxy)
                swaps_left -= 1
                continue
            if pool and boot_proxy:
                pool.release(boot_proxy)
            _emit(progress_cb, {"type": "error", "message": f"Pick mail.tm domain gagal: {e}"})
            return stats
    if pool and boot_proxy:
        pool.release(boot_proxy)

    ensure_csv_header(cfg.csv_path)

    _emit(progress_cb, {"type": "phase", "name": "running",
                        "message": f"Memulai {cfg.count} pendaftaran ({cfg.threads} thread)..."})

    # Run with thread pool, but check cancel between completions
    threads = max(1, min(cfg.threads, MAX_THREADS, cfg.count))
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futures = {
            ex.submit(_worker, i, cfg, fake, pool, invite_pool,
                      mail_domain, server_pub_initial, cancel_event,
                      progress_cb, stats): i
            for i in range(1, cfg.count + 1)
        }
        pending = set(futures.keys())
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    ok, info = fut.result()
                except Exception as e:
                    ok, info = False, f"worker error: {e}"
                if not ok:
                    if info != "cancelled":
                        stats.failed += 1
                        _emit(progress_cb, {
                            "type": "account_failed",
                            "idx": futures[fut],
                            "reason": info,
                            "stats": stats_snapshot(stats),
                        })
            if cancel_event.is_set() and not stats.cancelled:
                stats.cancelled = True
                _emit(progress_cb, {"type": "phase", "name": "cancelling",
                                    "message": "Cancel diterima — menunggu worker berhenti..."})

    if cancel_event.is_set():
        stats.cancelled = True
    return stats
