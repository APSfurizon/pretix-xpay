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
from utils import encode_order_id, HASH_TAG

logger = logging.getLogger(__name__)

TEST_URL = "https://xpaysandbox.nexigroup.com/api/phoenix-0.0/psp/api/v1/"
PROD_URL = "https://xpay.nexigroup.com/api/phoenix-0.0/psp/api/v1/"

ENDPOINT_ORDERS = "orders/"
ENDPOINT_ORDERS_CREATE = f"{ENDPOINT_ORDERS}hpp"

DOCS_TEST_CARDS_URL = "https://developer.nexi.it/en/area-test/carte-di-pagamento"


def initialize_payment(provider: XPayPaymentProvider, payment: OrderPayment): # HPP call
    order_salted_hash = payment.order.tagged_secret(HASH_TAG)
    order_code = payment.order.code
    payment_pk = payment.pk
    amount = int(str(int(payment.amount * 100))) # Amount MUST be in CENTS (10 EUR = 1000 EUR CENTS)

    params = {
        "order": {
            "orderId": encode_order_id(payment, provider.event, provider.settings),
            "amount": amount,
            "currency": provider.event.currency,
            "description": "TODO" # TODO
        },
        "paymentSession": {
            "amount": amount,
            "actionType": "PREAUTH", # Preauthing first. We're gonna finalize the payment after we're sure there's enough quota and the order is marked as paid
            "recurrence": {
                "action": "NO_RECURRING",
            },
            "language": "ENG", # TODO: da standard IETF a ISO 369-3 il codice lingua
            "resultUrl": build_absolute_uri( #TODO: Check if this return a pending /. If yes, remove it
                provider.event,
                "plugins:pretix_xpay:return",
                kwargs={
                    "order": order_code,
                    "payment": payment_pk,
                    "hash": order_salted_hash,
                    "result": "ok",
                },
            ),
            "cancelUrl": build_absolute_uri(
                provider.event,
                "plugins:pretix_xpay:return",
                kwargs={
                    "order": payment.order.code,
                    "payment": payment_pk,
                    "hash": order_salted_hash,
                    "result": "ko",
                },
            ),
        },
    }

    response = direct_api_call(provider, "hpp", params)
    return response["hostedPage"]

def verify_order_status():
    # TODO call to /orders/
    pass


def get_xpay_api_url(pp: XPayPaymentProvider):
    return TEST_URL if pp.event.testmode else PROD_URL

def direct_api_call(pp : XPayPaymentProvider, path, params):
    try:
        h = {
            "X-Api-Key": pp.settings.api_key,
            "Content-Type": "application/json",
            "Correlation-Id": str(uuid.uuid4()),
        }
        r = requests.post(f"{get_xpay_api_url(pp)}{path}", json=params, headers=h, timeout=31.5) #timeout to slightly more than a multiplel of 3, since it is TCP retrasmission time
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        logger.exception("Could not reach XPay's servers")
        raise PaymentException(_("Could not reach payment provider"))