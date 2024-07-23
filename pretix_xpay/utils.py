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
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse

logger = logging.getLogger(__name__)

def encode_order_id(orderPayment: OrderPayment, event: Event, settings: SettingsSandbox):
    data: str = orderPayment.full_id + event.slug + event.organizer.slug + settings.order_id_secret
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:18]

def decode_order_id(order: Order):
    return "ziopera"