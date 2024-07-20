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

logger = logging.getLogger(__name__)


class XPayPaymentProvider(BasePaymentProvider):
    identifier = "xpay"
    verbose_name = _("XPay")
    public_name = _("Credit / Debit card")
    abort_pending_allowed = False

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "xpay", event)
        self.event = event
        

    @property
    def settings_form_fields(self):
        fields = [

        ] + list(super().settings_form_fields.items())
        d = OrderedDict(fields)
        d.move_to_end("_enabled", last=False)
        return d
    
    @property
    def test_mode_message(self):
        if "/test/" in self.settings.backend:
            return _(
                "The Ogone plugin is operating in test mode. No money will actually be transferred. You can use credit "
                "card 4111111111111111 for testing."
            )
        return None
    
    def payment_form_render(self, request) -> str:
        template = get_template("pretix_xpay/checkout_payment_form.html")
        ctx = {"request": request, "event": self.event, "settings": self.settings}
        return template.render(ctx)
    
    