import hashlib
import json
import logging
import requests
from collections import OrderedDict
from django import forms
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.functional import lazy
from django.utils.translation import gettext_lazy as _
from lxml import etree
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse
from pretix_xpay.constants import TEST_URL, DOCS_TEST_CARDS_URL, HASH_TAG

logger = logging.getLogger(__name__)


class XPayPaymentProvider(BasePaymentProvider):
    identifier = "xpay"
    verbose_name = _("XPay")
    public_name = _("Pay through XPay")
    abort_pending_allowed = False
    execute_payment_needs_user = True

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "xpay", event)
        self.event : Event = event
        

    @property
    def settings_form_fields(self):
        fields = [
            (
                "alias_key", # Will be used to identify the merchant during api calls
                forms.CharField(
                    label=_("XPay's Alias key"),
                    help_text=_(
                        'Check your backoffice area to recover the Alias value.'
                    ),
                )
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
                        'By default it is set to SHA-1, contact XPay\'s support in order to use SHA-256.'
                    ),
                ),
            ),
            (
                "mac_secret_pass",
                SecretKeySettingsField(
                    label=_("Mac Secret"),
                    help_text=_(
                        'Check your backoffice area to recover the mac secret value. It is used to secure the hash'
                    ),
                ),
            ),
            (
                "poll_pending_timeout",
                forms.IntegerField(
                    label=_("Poll pending timeout (mins)"),
                    min_value = 1,
                    step_size = 1,
                    help_text=_(
                        'Pending and newly created payment orders are refreshed with regular intervals, to check if the user have actually paid, but left the process of returning back to pretix\'s pages.'
                        'This timeout specifies in how much time the payment should be considered over and should be marked as expired.'
                    ),
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
                f"The XPay plugin is operating in test mode. No money will actually be transferred, but BE SURE to check you're redirected to {TEST_URL}."
                f"You can use credit card and configurations avaible at {DOCS_TEST_CARDS_URL} for testing."
            )
        return None
    
    @property
    def identifier(self):
        return "xpay"
    
    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        return False

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        return False
    
    def payment_prepare(self, request, payment):
        return self.checkout_prepare(request, None)
    
    def payment_is_valid_session(self, request: HttpRequest):
        return True
    
    def payment_form_render(self, request) -> str: # Should return an explainatory paragraph
        template = get_template("pretix_xpay/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)
    
    def checkout_confirm_render(self, request) -> str: # (Mandatory to implement)
        template = get_template("pretix_xpay/checkout_payment_confirm.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings, "provider": self}
        return template.render(ctx)
    
    def payment_pending_render(self, request, payment) -> str: # Render customer-facing instructions on how to proceed with a pending payment
        template = get_template("pretix_xpay/pending.html")
        payment_info = json.loads(payment.info) if payment.info else None
        ctx = {"request": request, "event": self.event, "settings": self.settings, "provider": self, "order": payment.order, "payment": payment, "payment_info": payment_info}
        return template.render(ctx)

    def payment_control_render(self, request, payment) -> str: # It should return to admins HTML code containing information regarding the current payment status and, if applicable, next steps. NOT MANDATORY
        template = get_template("pretix_xpay/control.html")
        payment_info = json.loads(payment.info) if payment.info else None
        ctx = {"request": request, "event": self.event, "settings": self.settings, "payment_info": payment_info, "payment": payment, "method": self.method, "provider": self}
        return template.render(ctx)

    # TODO: Controllare se viene cancellata la roba giusta
    def shred_payment_info(self, obj: OrderPayment):
       if not obj.info:
           return
       d = json.loads(obj.info)
       if "details" in d:
           d["details"] = {k: "█" for k in d["details"].keys()}

       d["_shredded"] = True
       obj.info = json.dumps(d)
       obj.save(update_fields=["info"])

    
    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        return eventreverse(
            self.event,
            "plugins:pretix_xpay:redirect",
            kwargs={
                "order": payment.order.code,
                "payment": payment.pk,
                "hash": payment.order.tagged_secret(HASH_TAG),
            },
        )
    

