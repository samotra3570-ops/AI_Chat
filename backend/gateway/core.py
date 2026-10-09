from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class GatewayError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise GatewayError("invalid_payload") from error


class GatewayCore:
    """SQLite serializes reservation decisions; unknown billing retains its hold."""
    def __init__(self, path, key: bytes, config: dict, clock=time.time):
        if len(key) != 32:
            raise ValueError("A dedicated 32-byte server secret is required")
        self.path, self.key, self.config, self.clock = Path(path), key, config, clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cipher = AESGCM(key)
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS pairing(hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(family TEXT PRIMARY KEY, access TEXT UNIQUE NOT NULL,
              access_expires REAL NOT NULL, refresh TEXT UNIQUE NOT NULL, refresh_expires REAL NOT NULL, revoked INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS refresh_history(hash TEXT PRIMARY KEY, family TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rate(bucket TEXT PRIMARY KEY, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS owner_settings(id INTEGER PRIMARY KEY CHECK(id=1),
              revision INTEGER NOT NULL, budgets TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, model TEXT NOT NULL,
              room TEXT NOT NULL, character TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
              day TEXT NOT NULL, month TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER NOT NULL DEFAULT 0,
              price TEXT NOT NULL, input_limit INTEGER NOT NULL, output_limit INTEGER NOT NULL,
              usage TEXT, result BLOB, receipt TEXT, reconciliation_reference TEXT);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA secure_delete=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if write and db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def digest(self, value):
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def _settings(self, db):
        row = db.execute('SELECT revision,budgets FROM owner_settings WHERE id=1').fetchone()
        return {'revision': row['revision'] if row else 0,
                'budgets': json.loads(row['budgets']) if row else dict(self.config.get('budgets', {}))}

    def settings(self):
        with self.connection() as db:
            return self._settings(db)

    def update_settings(self, payload, subject):
        # All operator-paired sessions belong to this single-owner gateway.
        fields = {'request_micro_usd', 'day_micro_usd', 'month_micro_usd'}
        if not isinstance(payload, dict) or set(payload) != {'budgets', 'expected_revision'}:
            raise GatewayError('invalid_settings')
        budgets, revision = payload['budgets'], payload['expected_revision']
        if (type(revision) is not int or revision < 0 or not isinstance(budgets, dict)
                or set(budgets) != fields or any(type(v) is not int or not 0 <= v <= 1000000000000
                                               for v in budgets.values())):
            raise GatewayError('invalid_settings')
        if all(budgets.values()) and not (budgets['request_micro_usd'] <= budgets['day_micro_usd'] <= budgets['month_micro_usd']):
            raise GatewayError('budget_order')
        with self.connection(write=True) as db:
            current = self._settings(db)
            if revision != current['revision']:
                raise GatewayError('settings_conflict', 409)
            self._rate(db, 'settings', subject, 10, self.clock())
            updated = {'revision': revision + 1, 'budgets': dict(budgets)}
            db.execute('INSERT INTO owner_settings VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,budgets=excluded.budgets',
                       (updated['revision'], canonical(budgets)))
            return updated

    def _rate(self, db, action, subject, limit, now):
        bucket = f"{action}:{self.digest(subject)}:{int(now // 60)}"
        row = db.execute("SELECT count FROM rate WHERE bucket=?", (bucket,)).fetchone()
        if row and row[0] >= limit:
            raise GatewayError("rate_limit", 429)
        db.execute("INSERT INTO rate(bucket,count) VALUES(?,1) ON CONFLICT(bucket) DO UPDATE SET count=count+1", (bucket,))

    def pairing_code(self):
        code = secrets.token_urlsafe(32)
        with self.connection(write=True) as db:
            db.execute("INSERT INTO pairing VALUES(?,?,0)", (self.digest(code), self.clock() + 600))
        return code

    def _tokens(self, db, family, now):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,0)",
                   (family, self.digest(access), now + 600, self.digest(refresh), now + 604800))
        return {"access_token": access, "refresh_token": refresh, "access_expires": now + 600, "refresh_expires": now + 604800}

    def pair(self, code, subject="local"):
        now = self.clock()
        # Failed attempts also consume a durable rate slot.
        with self.connection(write=True) as db:
            self._rate(db, "pair", subject, 5, now)
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM pairing WHERE hash=?", (self.digest(code),)).fetchone()
            if row is None or row["used"] or row["expires"] < now:
                raise GatewayError("invalid_pairing", 401)
            db.execute("UPDATE pairing SET used=1 WHERE hash=?", (self.digest(code),))
            return self._tokens(db, secrets.token_hex(16), now)

    def authenticate(self, access):
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE access=?", (self.digest(access),)).fetchone()
        if row is None or row["revoked"] or row["access_expires"] <= self.clock():
            raise GatewayError("unauthorized", 401)
        return row["family"]

    def refresh(self, token):
        now, digest = self.clock(), self.digest(token)
        with self.connection(write=True) as db:
            self._rate(db, "refresh", digest, 20, now)
            used = db.execute("SELECT family FROM refresh_history WHERE hash=?", (digest,)).fetchone()
            if used:
                db.execute("UPDATE sessions SET revoked=1 WHERE family=?", (used[0],))
                outcome = None
            else:
                row = db.execute("SELECT * FROM sessions WHERE refresh=?", (digest,)).fetchone()
                if row is None or row["revoked"] or row["refresh_expires"] <= now:
                    outcome = None
                else:
                    db.execute("INSERT INTO refresh_history VALUES(?,?)", (digest, row["family"]))
                    outcome = self._tokens(db, row["family"], now)
        if outcome is None:
            raise GatewayError("unauthorized", 401)
        return outcome

    def revoke(self, token):
        digest = self.digest(token)
        with self.connection(write=True) as db:
            self._rate(db, "revoke", digest, 20, self.clock())
            db.execute("UPDATE sessions SET revoked=1 WHERE refresh=? OR family IN (SELECT family FROM refresh_history WHERE hash=?)", (digest, digest))

    def _model(self, model, now):
        cfg = self.config.get("models", {}).get(model)
        if not isinstance(cfg, dict):
            raise GatewayError("model_unavailable", 503)
        verified = cfg.get("price_verified_at")
        if type(verified) not in (int, float) or not math.isfinite(verified) or not 0 <= now - verified <= 7 * 86400:
            raise GatewayError("price_stale", 503)
        for k in ("input_price", "cached_input_price", "output_price"):
            try: value = Decimal(str(cfg[k]))
            except Exception: raise GatewayError("invalid_server_price", 503)
            if not value.is_finite() or value < 0:
                raise GatewayError("invalid_server_price", 503)
        if Decimal(str(cfg["cached_input_price"])) > Decimal(str(cfg["input_price"])): raise GatewayError("invalid_server_price", 503)
        for k in ("max_input_tokens", "max_output_tokens"):
            if type(cfg.get(k)) is not int or not 1 <= cfg[k] <= 200000:
                raise GatewayError("invalid_server_limit", 503)
        return cfg

    @staticmethod
    def charge(price, input_tokens, output_tokens, cached=0):
        if any(type(x) is not int or x < 0 for x in (input_tokens, output_tokens, cached)) or cached > input_tokens:
            raise GatewayError("invalid_usage", 503)
        # USD per million tokens converts directly to micro-USD per token.
        total = (Decimal(str(price["input_price"])) * (input_tokens - cached)
                 + Decimal(str(price["cached_input_price"])) * cached
                 + Decimal(str(price["output_price"])) * output_tokens)
        return int(total.to_integral_value(rounding=ROUND_CEILING))

    def validate(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"request_id", "model", "room_id", "character_id", "message", "context", "history", "regenerate_of", "approved_micro_usd", "purpose"}:
            raise GatewayError("invalid_payload")
        purpose = payload.get('purpose', 'reply')
        if purpose not in ('reply', 'proactive'):
            raise GatewayError('invalid_purpose')
        for k in ("request_id", "room_id", "character_id", "model"):
            if not isinstance(payload.get(k), str) or not 1 <= len(payload[k]) <= 200:
                raise GatewayError("invalid_payload")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", payload["request_id"]):
            raise GatewayError("invalid_request_id")
        message, context, history = payload.get("message"), payload.get("context"), payload.get("history", [])
        if 'approved_micro_usd' in payload and (type(payload['approved_micro_usd']) is not int or payload['approved_micro_usd'] < 0):
            raise GatewayError('invalid_cost_confirmation')
        if not isinstance(message, dict) or set(message) - {"text", "image_base64"}:
            raise GatewayError("invalid_message")
        if not isinstance(message.get("text", ""), str) or len(message.get("text", "")) > 4000:
            raise GatewayError("invalid_message")
        image = message.get("image_base64")
        if image is not None:
            try:
                raw = base64.b64decode(image, validate=True)
                if not raw.startswith(b"\xff\xd8") or len(raw) > 512 * 1024:
                    raise ValueError()
            except (TypeError, ValueError) as error:
                raise GatewayError("invalid_image") from error
        if purpose == 'proactive' and (message.get('text', '') or image is not None or 'regenerate_of' in payload):
            raise GatewayError('invalid_proactive_message')
        if purpose != 'proactive' and not message.get("text", "").strip() and image is None:
            raise GatewayError("empty_message")
        if not isinstance(context, dict) or len(canonical(context).encode()) > 128000:
            raise GatewayError("invalid_context")
        examples = context.get("styleExamples", [])
        if not isinstance(examples, list) or len(examples) > 3:
            raise GatewayError("invalid_examples")
        for example in examples:
            if not isinstance(example, dict) or any(example.get(k) is not False for k in ("isHistory", "isCanon", "isMemory", "isEvent")):
                raise GatewayError("example_isolation_required")
            sequence = example.get("characterBubbleSequence")
            if not isinstance(sequence, list) or not 1 <= len(sequence) <= 100 or any(not isinstance(s, str) or not s.strip() for s in sequence):
                raise GatewayError("invalid_examples")
        if not isinstance(history, list) or len(history) > 60 or len(canonical(history).encode()) > 128000:
            raise GatewayError("invalid_history")
        for message in history:
            if not isinstance(message, dict) or set(message) != {"role", "text"} or message["role"] not in ("user", "assistant") or not isinstance(message["text"], str) or len(message["text"]) > 4000:
                raise GatewayError("invalid_history")
        if purpose == 'proactive' and not any(m['role'] == 'user' and m['text'].strip() for m in history):
            raise GatewayError('proactive_history_required')

    def _periods(self, now):
        instant = datetime.fromtimestamp(now, timezone.utc)
        return instant.strftime("%Y-%m-%d"), instant.strftime("%Y-%m")

    def _totals(self, db, day, month):
        return {
            "day_spent": db.execute("SELECT coalesce(sum(actual),0) FROM requests WHERE day=?", (day,)).fetchone()[0],
            "month_spent": db.execute("SELECT coalesce(sum(actual),0) FROM requests WHERE month=?", (month,)).fetchone()[0],
            # Holds from earlier days/months continue to count against all caps.
            "held": db.execute("SELECT coalesce(sum(reserved),0) FROM requests WHERE status IN ('reserved','uncertain')").fetchone()[0],
        }

    def quote(self, payload):
        self.validate(payload)
        now = self.clock()
        cfg = self._model(payload["model"], now)
        estimate = len(canonical({"context": payload["context"], "history": payload.get("history", []), "text": payload["message"].get("text", "")}).encode())
        estimate += 1024 + len(payload.get("history", [])) * 64  # Developer/role/framing overhead upper allowance.
        if payload["message"].get("image_base64"):
            if cfg.get("vision") is not True:
                raise GatewayError("vision_unavailable", 503)
            estimate += 6144
        if estimate > cfg["max_input_tokens"]:
            raise GatewayError("context_too_large")
        reserve = self.charge(cfg, cfg["max_input_tokens"], cfg["max_output_tokens"])
        return {"reserved_micro_usd": reserve, "input_limit": cfg["max_input_tokens"], "output_limit": cfg["max_output_tokens"], "model": payload["model"],
                **({'purpose': 'proactive'} if payload.get('purpose') == 'proactive' else {})}

    def reserve(self, payload, subject="owner"):
        self.validate(payload)
        received = self.clock()  # Freeze one timestamp for period/cap/write consistency.
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        day, month = self._periods(received)
        with self.connection(write=True) as db:
            existing = db.execute("SELECT * FROM requests WHERE id=?", (payload["request_id"],)).fetchone()
            if existing is not None:
                if not hmac.compare_digest(existing["input_hash"], digest):
                    raise GatewayError("idempotency_conflict", 409)
                return False, self._receipt(existing)
            cfg = self._model(payload["model"], received)
            if db.execute("SELECT 1 FROM requests WHERE status='billing_limit_violation' LIMIT 1").fetchone():
                raise GatewayError("billing_limit_violation", 503)
            quote = self.quote(payload)
            totals = self._totals(db, day, month)
            amount = quote["reserved_micro_usd"]
            if amount > payload.get('approved_micro_usd', amount):
                raise GatewayError('cost_changed', 409)
            caps = self._settings(db)['budgets']
            if any(type(caps.get(k)) is not int or caps[k] <= 0 for k in ("day_micro_usd", "month_micro_usd", "request_micro_usd")):
                raise GatewayError("budget_unconfigured", 503)
            if amount > caps["request_micro_usd"] or totals["day_spent"] + totals["held"] + amount > caps["day_micro_usd"] or totals["month_spent"] + totals["held"] + amount > caps["month_micro_usd"]:
                raise GatewayError("hard_cap", 402)
            parent = payload.get("regenerate_of")
            if parent is not None:
                row = db.execute("SELECT * FROM requests WHERE id=?", (parent,)).fetchone()
                if row is None or row["status"] != "completed" or row["room"] != payload["room_id"] or row["character"] != payload["character_id"]:
                    raise GatewayError("invalid_regeneration")
            self._rate(db, "generation", "owner", 30, received)
            db.execute("INSERT INTO requests(id,input_hash,model,room,character,status,created,day,month,reserved,price,input_limit,output_limit) VALUES(?,?,?,?,?,'reserved',?,?,?,?,?,?,?)",
                       (payload["request_id"], digest, payload["model"], payload["room_id"], payload["character_id"], received, day, month, amount, canonical(cfg), cfg["max_input_tokens"], cfg["max_output_tokens"]))
            return True, {"request_id": payload["request_id"], "status": "reserved", **quote}

    def uncertain(self, request_id):
        with self.connection(write=True) as db:
            db.execute("UPDATE requests SET status='uncertain' WHERE id=? AND status='reserved'", (request_id,))

    def settle(self, request_id, result, usage, receipt, reconciliation_reference=None):
        if not isinstance(receipt, str) or not 1 <= len(receipt) <= 200:
            raise GatewayError("invalid_provider_receipt", 503)
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise GatewayError("request_not_found", 404)
            if row["status"] not in ("reserved", "uncertain"):
                return self._receipt(row)
            if row["status"] == "uncertain" and reconciliation_reference is None:
                raise GatewayError("billing_evidence_required", 409)
            actual = self.charge(json.loads(row["price"]), usage["input_tokens"], usage["output_tokens"], usage.get("cached_input_tokens", 0))
            if usage.get("reasoning_tokens", 0) > usage["output_tokens"] or usage.get("reasoning_tokens", 0) < 0:
                raise GatewayError("invalid_usage", 503)
            messages = result.get("messages", [])
            if not isinstance(messages, list) or len(messages) > 12 or any(not isinstance(s, str) or not s.strip() or len(s) > 4000 for s in messages):
                raise GatewayError("invalid_provider_output", 503)
            status = "completed" if messages else "refused"
            if actual > row["reserved"] or usage["input_tokens"] > row["input_limit"] or usage["output_tokens"] > row["output_limit"]:
                status = "billing_limit_violation"
                messages = []
            nonce = secrets.token_bytes(12)
            encrypted = nonce + self.cipher.encrypt(nonce, canonical({"messages": messages}).encode(), request_id.encode())
            db.execute("UPDATE requests SET status=?,actual=?,usage=?,result=?,receipt=?,reconciliation_reference=? WHERE id=?",
                       (status, actual, canonical(usage), encrypted, receipt, reconciliation_reference, request_id))
            return self._receipt(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def _receipt(self, row):
        value = {"request_id": row["id"], "status": row["status"], "reserved_micro_usd": row["reserved"], "actual_micro_usd": row["actual"], "model": row["model"]}
        if row["result"] is not None:
            data = row["result"]
            value.update(json.loads(self.cipher.decrypt(data[:12], data[12:], row["id"].encode())))
        return value

    def lookup(self, request_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise GatewayError("request_not_found", 404)
            return self._receipt(row)

    def costs(self):
        now = self.clock()
        day, month = self._periods(now)
        with self.connection() as db:
            totals = self._totals(db, day, month)
            history = [dict(row) for row in db.execute("SELECT id,model,status,created,reserved,actual,usage FROM requests ORDER BY created DESC LIMIT 50")]
            violation = db.execute("SELECT 1 FROM requests WHERE status='billing_limit_violation' LIMIT 1").fetchone() is not None
            budgets = self._settings(db)['budgets']
        warnings = []
        for scope in ("day", "month"):
            cap = budgets.get(f"{scope}_micro_usd", 0)
            percent = 100 if cap <= 0 else math.floor((totals[f"{scope}_spent"] + totals["held"]) * 100 / cap)
            warnings.extend({"scope": scope, "threshold": threshold} for threshold in (70, 85, 95, 100) if percent >= threshold)
        return {"currency": "USD", "unit": "micro_usd", "day": day, "month": month, **totals,
                "budgets": budgets, "warnings": warnings, "history": history, "billing_limit_violation": violation}
