"""Stripe Checkout, Customer Portal y webhooks para suscripciones de Agentgenia."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


PAID_TIERS = frozenset({"basic", "pro"})
ACTIVE_STATUSES = frozenset({"active", "trialing"})
GRACE_STATUSES = frozenset({"past_due"})
TERMINAL_STATUSES = frozenset({"canceled", "incomplete_expired", "paused", "unpaid"})
SUPPORTED_EVENTS = frozenset({
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_action_required",
})


class BillingError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "billing_error"):
        super().__init__(message)
        self.status = status
        self.code = code


class StripeApiError(BillingError):
    pass


def _required_https_url(value: str, label: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ValueError(f"{label} no es una URL válida") from exc
    loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (parsed.scheme != "https" and not loopback) or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{label} debe usar HTTPS (HTTP solo se admite en loopback)")
    return value


@dataclass(frozen=True)
class BillingConfig:
    enabled: bool
    live_mode: bool
    secret_key: str
    webhook_secret: str
    basic_price_id: str
    pro_price_id: str
    success_url: str
    cancel_url: str
    portal_return_url: str
    webhook_tolerance_seconds: int = 300

    @classmethod
    def from_values(
        cls,
        *,
        enabled: bool,
        live_mode: bool,
        secret_key: str | None,
        webhook_secret: str | None,
        basic_price_id: str | None,
        pro_price_id: str | None,
        success_url: str | None,
        cancel_url: str | None,
        portal_return_url: str | None,
        webhook_tolerance_seconds: int = 300,
    ) -> "BillingConfig":
        config = cls(
            enabled=enabled,
            live_mode=live_mode,
            secret_key=(secret_key or "").strip(),
            webhook_secret=(webhook_secret or "").strip(),
            basic_price_id=(basic_price_id or "").strip(),
            pro_price_id=(pro_price_id or "").strip(),
            success_url=(success_url or "").strip(),
            cancel_url=(cancel_url or "").strip(),
            portal_return_url=(portal_return_url or "").strip(),
            webhook_tolerance_seconds=webhook_tolerance_seconds,
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.enabled:
            return
        expected_prefix = "sk_live_" if self.live_mode else "sk_test_"
        if not self.secret_key.startswith(expected_prefix):
            raise ValueError(f"STRIPE_SECRET_KEY debe comenzar con {expected_prefix} para el modo configurado")
        if not self.webhook_secret.startswith("whsec_"):
            raise ValueError("STRIPE_WEBHOOK_SECRET debe comenzar con whsec_")
        if not self.basic_price_id.startswith("price_") or not self.pro_price_id.startswith("price_"):
            raise ValueError("Los price IDs de Stripe deben comenzar con price_")
        if self.basic_price_id == self.pro_price_id:
            raise ValueError("Plus y Pro no pueden usar el mismo price ID")
        _required_https_url(self.success_url, "STRIPE_SUCCESS_URL")
        _required_https_url(self.cancel_url, "STRIPE_CANCEL_URL")
        _required_https_url(self.portal_return_url, "STRIPE_PORTAL_RETURN_URL")
        if not 30 <= self.webhook_tolerance_seconds <= 900:
            raise ValueError("STRIPE_WEBHOOK_TOLERANCE_SECONDS debe estar entre 30 y 900")

    @property
    def tier_prices(self) -> dict[str, str]:
        return {"basic": self.basic_price_id, "pro": self.pro_price_id}

    @property
    def price_tiers(self) -> dict[str, str]:
        return {value: key for key, value in self.tier_prices.items()}


class StripeClient:
    def __init__(self, secret_key: str, *, api_base: str = "https://api.stripe.com/v1"):
        self.secret_key = secret_key
        self.api_base = api_base.rstrip("/")

    def _post(self, path: str, fields: list[tuple[str, str]], *, idempotency_key: str | None = None) -> dict:
        body = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(f"{self.api_base}{path}", data=body, method="POST")
        request.add_header("Authorization", f"Bearer {self.secret_key}")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("User-Agent", "Agentgenia-Wrapper/1.0")
        if idempotency_key:
            request.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
                message = payload.get("error", {}).get("message") or f"Stripe respondió HTTP {exc.code}"
            except Exception:
                message = f"Stripe respondió HTTP {exc.code}"
            raise StripeApiError(message, status=502, code="stripe_api_error") from exc
        except (OSError, TimeoutError) as exc:
            raise StripeApiError(f"No se pudo contactar Stripe: {exc}", status=502, code="stripe_unavailable") from exc

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(f"{self.api_base}{path}", method="GET")
        request.add_header("Authorization", f"Bearer {self.secret_key}")
        request.add_header("User-Agent", "Agentgenia-Wrapper/1.0")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
                message = payload.get("error", {}).get("message") or f"Stripe respondió HTTP {exc.code}"
            except Exception:
                message = f"Stripe respondió HTTP {exc.code}"
            raise StripeApiError(message, status=502, code="stripe_api_error") from exc
        except (OSError, TimeoutError) as exc:
            raise StripeApiError(
                f"No se pudo contactar Stripe: {exc}",
                status=502,
                code="stripe_unavailable",
            ) from exc

    def _delete(self, path: str, *, idempotency_key: str | None = None) -> dict:
        request = urllib.request.Request(f"{self.api_base}{path}", data=b"", method="DELETE")
        request.add_header("Authorization", f"Bearer {self.secret_key}")
        request.add_header("User-Agent", "Agentgenia-Wrapper/1.0")
        if idempotency_key:
            request.add_header("Idempotency-Key", idempotency_key)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
                message = payload.get("error", {}).get("message") or f"Stripe respondió HTTP {exc.code}"
            except Exception:
                message = f"Stripe respondió HTTP {exc.code}"
            raise StripeApiError(message, status=502, code="stripe_api_error") from exc
        except (OSError, TimeoutError) as exc:
            raise StripeApiError(
                f"No se pudo contactar Stripe: {exc}", status=502, code="stripe_unavailable"
            ) from exc

    def create_checkout_session(
        self,
        *,
        user_id: str,
        email: str | None,
        customer_id: str | None,
        tier: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        idempotency_key: str,
    ) -> dict:
        fields = [
            ("mode", "subscription"),
            ("line_items[0][price]", price_id),
            ("line_items[0][quantity]", "1"),
            ("success_url", success_url),
            ("cancel_url", cancel_url),
            ("client_reference_id", user_id),
            ("metadata[user_id]", user_id),
            ("metadata[tier]", tier),
            ("subscription_data[metadata][user_id]", user_id),
            ("subscription_data[metadata][tier]", tier),
            ("billing_address_collection", "auto"),
            ("allow_promotion_codes", "false"),
        ]
        if customer_id:
            fields.append(("customer", customer_id))
        elif email:
            fields.append(("customer_email", email))
        return self._post("/checkout/sessions", fields, idempotency_key=idempotency_key)

    def create_portal_session(self, *, customer_id: str, return_url: str) -> dict:
        return self._post("/billing_portal/sessions", [
            ("customer", customer_id),
            ("return_url", return_url),
        ])

    def retrieve_subscription(self, subscription_id: str) -> dict:
        encoded_id = urllib.parse.quote(subscription_id, safe="")
        return self._get(f"/subscriptions/{encoded_id}")

    def cancel_subscription(self, subscription_id: str, *, idempotency_key: str) -> dict:
        encoded_id = urllib.parse.quote(subscription_id, safe="")
        return self._delete(
            f"/subscriptions/{encoded_id}", idempotency_key=idempotency_key
        )


def verify_webhook_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> dict:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in signature_header.split(","):
        key, separator, value = part.strip().partition("=")
        if not separator:
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                pass
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise BillingError("Firma Stripe incompleta", status=400, code="invalid_stripe_signature")
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise BillingError("Firma Stripe expirada", status=400, code="invalid_stripe_signature")
    signed_payload = str(timestamp).encode() + b"." + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise BillingError("Firma Stripe inválida", status=400, code="invalid_stripe_signature")
    try:
        event = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BillingError("Evento Stripe no contiene JSON válido", status=400, code="invalid_stripe_event") from exc
    if not isinstance(event, dict):
        raise BillingError("Evento Stripe inválido", status=400, code="invalid_stripe_event")
    return event


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _metadata(obj: dict) -> dict:
    value = obj.get("metadata")
    return value if isinstance(value, dict) else {}


def _subscription_from_invoice(obj: dict) -> str | None:
    direct = _string(obj.get("subscription"))
    if direct:
        return direct
    parent = obj.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_details")
        if isinstance(details, dict):
            return _string(details.get("subscription"))
    return None


def _price_from_subscription(obj: dict) -> str | None:
    items = obj.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        if isinstance(price, dict) and _string(price.get("id")):
            return price["id"]
    return None


class BillingService:
    def __init__(self, store, config: BillingConfig, *, client: StripeClient | None = None):
        self.store = store
        self.config = config
        self.client = client or StripeClient(config.secret_key)

    @property
    def configured(self) -> bool:
        return self.config.enabled

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise BillingError("Los pagos todavía no están configurados", status=503, code="billing_not_configured")

    def status(self, user: dict) -> dict:
        current = self.store.get_billing_status(user["id"])
        return {
            "configured": self.config.enabled,
            "tier": user.get("tier") or "free",
            "customer": bool(current.get("customer_id")),
            "subscription": current.get("subscription"),
            "plans": {
                "basic": {"name": "Plus", "amount": 50, "currency": "usd", "interval": "month"},
                "pro": {"name": "Pro", "amount": 200, "currency": "usd", "interval": "month"},
            },
        }

    def create_checkout(self, user: dict, tier: str) -> dict:
        self._require_enabled()
        tier = tier.strip().lower()
        if tier not in PAID_TIERS:
            raise BillingError("El plan debe ser basic o pro", code="invalid_plan")
        current = self.store.get_billing_status(user["id"])
        subscription = current.get("subscription")
        if subscription and subscription.get("status") in ACTIVE_STATUSES | GRACE_STATUSES:
            raise BillingError(
                "Ya tienes una suscripción; usa el portal para administrarla o cambiar de plan",
                status=409,
                code="subscription_already_active",
            )
        bucket = int(time.time() // 3600)
        idempotency_key = hashlib.sha256(f"checkout|{user['id']}|{tier}|{bucket}".encode()).hexdigest()
        session = self.client.create_checkout_session(
            user_id=user["id"],
            email=user.get("email"),
            customer_id=current.get("customer_id"),
            tier=tier,
            price_id=self.config.tier_prices[tier],
            success_url=self.config.success_url,
            cancel_url=self.config.cancel_url,
            idempotency_key=f"agentgenia-{idempotency_key}",
        )
        url = _string(session.get("url"))
        if not url or urlparse(url).scheme != "https" or urlparse(url).hostname not in {
            "checkout.stripe.com", "billing.stripe.com"
        }:
            raise StripeApiError("Stripe no devolvió una URL de Checkout válida", status=502, code="invalid_checkout_url")
        return {"checkout_url": url}

    def create_portal(self, user: dict) -> dict:
        self._require_enabled()
        current = self.store.get_billing_status(user["id"])
        customer_id = current.get("customer_id")
        if not customer_id:
            raise BillingError("Aún no existe una cuenta de facturación", status=404, code="billing_customer_not_found")
        session = self.client.create_portal_session(
            customer_id=customer_id,
            return_url=self.config.portal_return_url,
        )
        url = _string(session.get("url"))
        if not url or urlparse(url).scheme != "https" or urlparse(url).hostname != "billing.stripe.com":
            raise StripeApiError("Stripe no devolvió una URL de portal válida", status=502, code="invalid_portal_url")
        return {"portal_url": url}

    def cancel_for_account_deletion(self, user: dict) -> bool:
        """Cancel an active Stripe subscription before personal data is erased."""
        current = self.store.get_billing_status(user["id"])
        subscription = current.get("subscription")
        if not subscription or subscription.get("status") in TERMINAL_STATUSES:
            return False
        self._require_enabled()
        subscription_id = subscription.get("stripe_subscription_id")
        if not isinstance(subscription_id, str) or not subscription_id:
            raise BillingError(
                "La suscripción guardada no tiene un identificador de Stripe",
                status=500,
                code="billing_state_invalid",
            )
        remote = self.client.retrieve_subscription(subscription_id)
        if _string(remote.get("id")) != subscription_id:
            raise StripeApiError(
                "Stripe devolvió una suscripción distinta a la solicitada",
                status=502,
                code="invalid_stripe_subscription",
            )
        # A previous deletion attempt may have canceled Stripe successfully and
        # then failed while revoking another provider. Treat that retry as a
        # confirmed cancellation instead of trapping the user account forever.
        if _string(remote.get("status")) in TERMINAL_STATUSES:
            return True
        result = self.client.cancel_subscription(
            subscription_id,
            idempotency_key=f"agentgenia-delete-{hashlib.sha256(user['id'].encode()).hexdigest()}",
        )
        if result.get("status") != "canceled":
            raise StripeApiError(
                "Stripe no confirmó la cancelación de la suscripción",
                status=502,
                code="stripe_cancel_unconfirmed",
            )
        return True

    def _refresh_activation_from_stripe(self, action: dict[str, Any]) -> None:
        """Revalida en Stripe antes de conceder acceso pagado."""
        subscription_id = action.get("stripe_subscription_id")
        if not subscription_id:
            action["tier_action"] = "keep"
            return
        subscription = self.client.retrieve_subscription(subscription_id)
        if _string(subscription.get("id")) != subscription_id:
            raise StripeApiError(
                "Stripe devolvió una suscripción distinta a la solicitada",
                status=502,
                code="invalid_stripe_subscription",
            )

        metadata = _metadata(subscription)
        current_user_id = _string(metadata.get("user_id"))
        current_customer_id = _string(subscription.get("customer"))
        if action.get("user_id") and current_user_id and action["user_id"] != current_user_id:
            raise BillingError(
                "La suscripción de Stripe no corresponde al usuario del evento",
                status=400,
                code="stripe_subscription_mismatch",
            )
        if (
            action.get("customer_id")
            and current_customer_id
            and action["customer_id"] != current_customer_id
        ):
            raise BillingError(
                "La suscripción de Stripe no corresponde al customer del evento",
                status=400,
                code="stripe_subscription_mismatch",
            )

        price_id = _price_from_subscription(subscription)
        status = _string(subscription.get("status")) or "unknown"
        tier = self.config.price_tiers.get(price_id or "")
        if status in ACTIVE_STATUSES:
            if not tier:
                raise BillingError(
                    "La suscripción activa usa un price ID no configurado",
                    status=400,
                    code="stripe_price_not_configured",
                )
            tier_action = "activate"
        elif status in TERMINAL_STATUSES:
            tier_action = "free"
        else:
            tier_action = "keep"
        action.update({
            "user_id": current_user_id or action.get("user_id"),
            "customer_id": current_customer_id or action.get("customer_id"),
            "stripe_price_id": price_id or action.get("stripe_price_id"),
            "tier": tier or action.get("tier"),
            "status": status,
            "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
            "current_period_end": (
                subscription.get("current_period_end")
                if isinstance(subscription.get("current_period_end"), int)
                and not isinstance(subscription.get("current_period_end"), bool)
                else None
            ),
            "tier_action": tier_action,
        })

    def process_webhook(self, payload: bytes, signature_header: str) -> dict:
        self._require_enabled()
        event = verify_webhook_signature(
            payload,
            signature_header,
            self.config.webhook_secret,
            tolerance_seconds=self.config.webhook_tolerance_seconds,
        )
        if bool(event.get("livemode")) != self.config.live_mode:
            raise BillingError("El modo del evento Stripe no coincide", status=400, code="stripe_mode_mismatch")
        event_id = _string(event.get("id"))
        event_type = _string(event.get("type"))
        event_created = event.get("created")
        data = event.get("data")
        obj = data.get("object") if isinstance(data, dict) else None
        if (
            not event_id
            or not event_type
            or not isinstance(event_created, int)
            or isinstance(event_created, bool)
            or event_created <= 0
            or not isinstance(obj, dict)
        ):
            raise BillingError("Evento Stripe incompleto", status=400, code="invalid_stripe_event")
        action: dict[str, Any] = {
            "event_id": event_id,
            "event_type": event_type,
            "stripe_event_created": event_created,
            "recognized": event_type in SUPPORTED_EVENTS,
            "user_id": None,
            "customer_id": _string(obj.get("customer")),
            "stripe_subscription_id": None,
            "stripe_price_id": None,
            "tier": None,
            "status": None,
            "cancel_at_period_end": False,
            "current_period_end": None,
            "tier_action": "keep",
        }
        if event_type == "checkout.session.completed":
            metadata = _metadata(obj)
            action.update({
                "user_id": _string(metadata.get("user_id")) or _string(obj.get("client_reference_id")),
                "stripe_subscription_id": _string(obj.get("subscription")),
                "tier": _string(metadata.get("tier")),
                "status": "active" if obj.get("payment_status") == "paid" else "incomplete",
                "tier_action": "activate" if obj.get("payment_status") == "paid" else "keep",
            })
            action["stripe_price_id"] = self.config.tier_prices.get(action["tier"])
        elif event_type.startswith("customer.subscription."):
            metadata = _metadata(obj)
            status = "canceled" if event_type.endswith("deleted") else _string(obj.get("status"))
            price_id = _price_from_subscription(obj)
            action.update({
                "user_id": _string(metadata.get("user_id")),
                "stripe_subscription_id": _string(obj.get("id")),
                "stripe_price_id": price_id,
                "tier": self.config.price_tiers.get(price_id or "") or _string(metadata.get("tier")),
                "status": status,
                "cancel_at_period_end": bool(obj.get("cancel_at_period_end")),
                "current_period_end": obj.get("current_period_end") if isinstance(obj.get("current_period_end"), int) else None,
                "tier_action": "activate" if status in ACTIVE_STATUSES else "free" if status in TERMINAL_STATUSES else "keep",
            })
        elif event_type.startswith("invoice."):
            action.update({
                "stripe_subscription_id": _subscription_from_invoice(obj),
                "status": "active" if event_type == "invoice.paid" else "past_due",
                "tier_action": "activate" if event_type == "invoice.paid" else "keep",
            })
        if action["tier_action"] == "activate":
            self._refresh_activation_from_stripe(action)
        result = self.store.apply_billing_event(action)
        return {"received": True, **result}
