import json
import logging
from collections import OrderedDict
from datetime import datetime
from django import forms
from django.http import Http404, HttpRequest
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider
from pretix.multidomain.urlreverse import eventreverse

import pretix_xpay.xpay_api as xpay
from pretix_xpay.constants import (
    DOCS_TEST_CARDS_URL,
    HASH_TAG,
    TEST_URL,
    XPAY_RESULT_AUTHORIZED,
    XPAY_RESULT_CANCELED,
    XPAY_RESULT_CAPTURED,
    XPAY_RESULT_PENDING,
    XPAY_RESULT_REFUNDED,
)
from pretix_xpay.utils import get_settings_object, send_refund_needed_email

logger = logging.getLogger(__name__)


class XPayPaymentProvider(BasePaymentProvider):
    identifier = "xpay"
    verbose_name = _("XPay")
    public_name = _("Pay through XPay")
    abort_pending_allowed = False
    execute_payment_needs_user = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = get_settings_object(event)
        self.event: Event = event

    @property
    def settings_form_fields(self):
        fields = [
            (
                "alias_key",  # Will be used to identify the merchant during api calls
                forms.CharField(
                    label=_("XPay's Alias key"),
                    help_text=_(
                        "Check your backoffice area to recover the Alias value."
                    ),
                    required=True,
                ),
            ),
            (
                "hash",
                forms.ChoiceField(
                    label=_("Mac's hash algorithm"),
                    choices=(
                        ("sha1", "SHA-1"),
                        ("sha256", "SHA-256"),
                    ),
                    help_text=_(
                        "By default it is set to SHA-1, contact XPay's support in order to use SHA-256."
                    ),
                ),
            ),
            (
                "mac_secret_pass",
                SecretKeySettingsField(
                    label=_("Mac Secret"),
                    help_text=_(
                        "Check your backoffice area to recover the mac secret value. It is used to secure the hash"
                    ),
                    required=True,
                ),
            ),
            (
                "test_alias_key",  # Will be used to identify the merchant during api calls
                forms.CharField(
                    label=_("TEST - XPay's Alias key"),
                    help_text=_(
                        "Check your backoffice area to recover the Alias value. "
                        "This is used ONLY when the event is in test mode. "
                        "If not set, it will fallback to the default Alias key."
                    ),
                    required=False,
                ),
            ),
            (
                "test_mac_secret_pass",
                forms.CharField(
                    label=_("TEST - Mac Secret"),
                    help_text=_(
                        "Check your backoffice area to recover the mac secret value. It is used to secure the hash"
                        "This is used ONLY when the event is in test mode. "
                        "If not set, it will fallback to the default Mac secret."
                    ),
                    required=False,
                ),
            ),
            (
                "order_id_salt",
                forms.CharField(
                    label=_("Order id salt"),
                    help_text=_(
                        "To generate xpay order ids we need a random, secret string to prevent "
                        "malicious users from generating fake order ids. "
                    ),
                    required=True,
                ),
            ),
            (
                "poll_pending_timeout",
                forms.IntegerField(
                    label=_("Pending order timeout (mins)"),
                    min_value=1,
                    max_value=50000000,
                    step_size=1,
                    help_text=_(
                        "Pending and newly created payment orders are refreshed with regular intervals, "
                        "to check if the user have actually paid, but left the process of returning back to pretix's pages. "
                        "This timeout specifies in how much time the payment should be considered over and should be marked as expired."
                    ),
                ),
            ),
            (
                "payment_error_email",  # Email address to send manual refund requests to
                forms.EmailField(
                    label=_("Failed payments email address"),
                    help_text=_(
                        "Enter an email address recipient for manual verification requests. "
                        "It might happen because of a failed refund request, or an already charged payment."
                    ),
                ),
            ),
            (
                "enable_test_endpoints",
                forms.BooleanField(
                    label=_("Enable test endpoints"),
                    help_text=_(
                        "This enables the endpoints /poll_pending_payments and /test_manual_refund_email for events in testmode"
                    ),
                    required=False,
                ),
            ),
        ] + list(super().settings_form_fields.items())
        d = OrderedDict(fields)
        d.move_to_end("_enabled", last=False)
        return d

    @property
    def test_mode_message(self):
        if self.event.testmode:
            return _(
                f"The XPay plugin is operating in test mode. No money will actually be transferred, but BE SURE to check you're redirected to {TEST_URL}. "
                f"You can use credit card and configurations avaible at {DOCS_TEST_CARDS_URL} for testing."
            )
        return None

    def cancel_payment(self, payment: OrderPayment):
        """
        Overrides the default cancel_payment to add a couple of checks.

        :param OrderPayment payment: the order's payment
        :raises Exception: if the payment is not found or already accounted
        """
        logger.info(
            f"XPAY_cancel_payment [{payment.full_id}]: Trying to cancel a payment"
        )
        try:
            try:
                order_status = xpay.get_order_status(payment=payment, provider=self)
            except Http404:
                logger.error(
                    f"XPAY_cancel_payment [{payment.full_id}]: Order not found"
                )
                super().cancel_payment(payment)
                raise Exception("Payment not found")

            if (
                order_status.status in XPAY_RESULT_AUTHORIZED
                or order_status.status in XPAY_RESULT_PENDING
            ):
                logger.error(
                    f"XPAY_cancel_payment [{payment.full_id}]: Refunding preauth payment"
                )
                xpay.refund_preauth(payment, self)
                super().cancel_payment(payment)

            elif order_status.status in XPAY_RESULT_CAPTURED:
                logger.error(
                    f"XPAY_cancel_payment [{payment.full_id}]: Preauthorized payment was already captured!"
                )
                super().cancel_payment(payment)
                send_refund_needed_email(
                    payment, origin="XPayPaymentProvider.cancel_payment"
                )
                raise Exception("Pre-authorized payment was already captured")

            elif (
                order_status.status in XPAY_RESULT_REFUNDED
                or order_status.status in XPAY_RESULT_CANCELED
            ):
                logger.error(
                    f"XPAY_cancel_payment [{payment.full_id}]: Payment was already in refunded or canceled state"
                )
                super().cancel_payment(payment)

            else:
                logger.error(
                    f"XPAY_cancel_payment [{payment.full_id}]: Unknown state {order_status.status}. Cancling the payment anyway"
                )
                super().cancel_payment(payment)
                raise Exception(f"Unknown state: {order_status.status}")

        except BaseException as e:
            logger.warning(
                f"A warning occurred while trying to cancel the payment {payment.full_id}: {repr(e)}"
            )

    def payment_form_render(self, request) -> str:
        """Renders an explainatory paragraph"""
        template = get_template("pretix_xpay/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)

    def checkout_confirm_render(self, request) -> str:
        """Renders the checkout confirm form"""
        template = get_template("pretix_xpay/checkout_payment_confirm.html")
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
        }
        return template.render(ctx)

    def payment_pending_render(self, request, payment) -> str:
        """Renders ustomer-facing instructions on how to proceed with a pending payment"""
        template = get_template("pretix_xpay/pending.html")
        payment_info = json.loads(payment.info) if payment.info else None
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "provider": self,
            "order": payment.order,
            "payment": payment,
            "payment_info": payment_info,
        }
        return template.render(ctx)

    def payment_control_render(self, request, payment) -> str:
        """Returns to admins the HTML code containing information regarding the current payment status and, if applicable, next steps. NOT MANDATORY"""
        template = get_template("pretix_xpay/control.html")
        payment_info = json.loads(payment.info) if payment.info else None
        date = None
        amount = None
        cardInfo = None
        if payment_info is not None:
            if payment_info['data'] is not None and payment_info['orario'] is not None:
                d = int(payment_info['data'])
                o = int(payment_info['orario'])
                date = datetime(
                    (d // 10000) % 10000, (d // 100) % 100, (d // 1) % 100,
                    (o // 10000) % 100, (o // 100) % 100, (o // 1) % 100
                ).strftime("%Y-%m-%d %H:%M:%S")
            if payment_info['divisa'] is not None and payment_info['importo'] is not None:
                i = int(payment_info['importo'])
                d = payment_info['divisa']
                amount = f"{d} {i // 100}.{i % 100}" 
            if payment_info["pan"] is not None and payment_info["scadenza_pan"] is not None:
                p = payment_info["pan"]
                e = payment_info["scadenza_pan"]
                cardInfo = f"{p} - {e[:4]}/{e[4:6]}"
        ctx = {
            "request": request,
            "event": self.event,
            "settings": self.settings,
            "payment_info": payment_info,
            "payment": payment,
            "provider": self,
            "date": date,
            "amount": amount,
            "cardInfo": cardInfo
        }
        return template.render(ctx)

    def shred_payment_info(self, obj: OrderPayment):
        """Shred payment info for enhanceh anonymization"""
        logger.info(f"XPAY_shred_payment_info [{obj.full_id}]: Shredding payment info")
        if not obj.info:
            return

        d = json.loads(obj.info)
        if "descrizione" in d:
            d["descrizione"] = "█"
        if "nome" in d:
            d["nome"] = "█"
        if "cognome" in d:
            d["cognome"] = "█"
        if "mail" in d:
            d["mail"] = "█"
        if "regione" in d:
            d["regione"] = "█"
        if "nazionalita" in d:
            d["nazionalita"] = "█"
        if "languageId" in d:
            d["languageId"] = "█"
        if "tipoProdotto" in d:
            d["tipoProdotto"] = "█"
        if "pan" in d:
            d["pan"] = "█"
        if "scadenza_pan" in d:
            d["scadenza_pan"] = "█"
        if "selectedcard" in d:
            d["selectedcard"] = "█"

        d["_shredded"] = True
        obj.info = json.dumps(d)
        obj.save(update_fields=["info"])

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        """Will redirect user to the payment creation view"""
        logger.info(
            f"XPAY_execute_payment [{payment.full_id}]: Redirecting user to internal redirect page"
        )
        return eventreverse(
            self.event,
            "plugins:pretix_xpay:redirect",
            kwargs={
                "order": payment.order.code,
                "payment": payment.pk,
                "hash": payment.order.tagged_secret(HASH_TAG),
            },
        )

    def execute_refund(self, refund: OrderRefund):
        """Executes a partial or full refund request"""
        xpay.refund(refund, self)
        refund.save()
        refund.done()

    # Mandatory properties for the plugin to work
    # @property
    # def identifier(self):
    #    return "xpay"

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return True

    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, None)

    def payment_is_valid_session(self, request: HttpRequest):
        return True
