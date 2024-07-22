import hashlib
import json
import uuid
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
from payment import XPayPaymentProvider

logger = logging.getLogger(__name__)

TEST_URL = "https://xpaysandbox.nexigroup.com/api/phoenix-0.0/psp/api/v1/"
PROD_URL = "https://xpay.nexigroup.com/api/phoenix-0.0/psp/api/v1/"

ENDPOINT_CREATE_ORDER = "orders/hpp"

DOCS_TEST_CARDS_URL = "https://developer.nexi.it/en/area-test/carte-di-pagamento"


def initialize_payment(): # HPP call
    requestBodyData = {
        "order": {
            "orderId": orderId,
            "amount": 1000, # Amount MUST be in CENTS (10 EUR = 1000 EUR CENTS)
            "currency": "EUR",
        },
        "paymentSession": {
            "amount": 1000, # Amount MUST be in CENTS (10 EUR = 1000 EUR CENTS)
            "actionType": "PAY",
            "recurrence": {
                "action": "NO_RECURRING",
            },
            "language": "ENG",
            "resultUrl": "https://www.example.com/payment/result",
            "cancelUrl": "https://www.example.com/payment/cancel",
            "notificationUrl": "https://www.example.com/payment/notification",
        },
    }




def get_xpay_api_url(pp: XPayPaymentProvider):
    return TEST_URL if pp.event.testmode else PROD_URL

def direct_api_call(pp : XPayPaymentProvider, path, params):
    try:
        h = {
            "X-Api-Key": pp.settings.api_key,
            "Content-Type": "application/json",
            "Correlation-Id": str(uuid.uuid4()),
        }
        r = requests.post(f"{get_xpay_api_url(pp)}{path}", json=params, headers=h)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        logger.exception("Could not reach xpay backend")
        raise PaymentException(_("Could not reach payment provider"))