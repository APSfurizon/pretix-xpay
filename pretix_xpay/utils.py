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

def encode_order_id(orderPayment: OrderPayment, event: Event):
    data: str = orderPayment.full_id + event.slug + event.organizer.slug
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:18]

HASH_TAG = "plugins:pretix_xpay"

SUCCESS_TYPES: Annotated[list, "All the success values operationResult might contain"] = [
    'AUTHORIZED',
    'EXECUTED',
    'PENDING'
]

FAIL_TYPES: Annotated[list, "All the failing values operationResult might contain"] = [
    'DECLINED',
    'DENIED_BY_RISK',
    'THREEDS_VALIDATED',
    'THREEDS_FAILED',
    'CANCELED',
    'VOIDED',
    'FAILED',
]

def parse_order_result(result: dict) -> dict:
    key_status = 'orderStatus'
    key_result = 'result'
    key_errors = 'errors'
    to_return = {
        'result': False,
        'errors': []
    }
    
    if key_status not in result:
        to_return[key_result] = False
        return to_return