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
from pretix.base.models import Order, Event, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse
from typing import Annotated

logger = logging.getLogger(__name__)

def encode_order_id(orderPayment: OrderPayment, event: Event) -> str:
    #TODO: problema con vecchio metodo: l'orderId non può essere lo stesso, anche per riprovare lo stesso pagamento
    data: str = orderPayment.full_id + event.slug + event.organizer.slug
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:18]

HASH_TAG = "plugins:pretix_xpay"

XPAY_STATUS_SUCCESS = ["OK"]
XPAY_STATUS_FAILS = ["KO", "ANNULLO", "ERRORE"]
XPAY_STATUS_PENDING = ["PEN"]

def generate_mac(data: list, provider: BasePaymentProvider) -> str:
    to_encode = "" 
    for el in data:
        to_encode += f"{el[0]}={str(el[1])}"
    to_encode += provider.settings.mac_secret_pass
    hash_algo = hashlib.new(provider.settings.hash)
    #TODO: complete