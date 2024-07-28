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
from pretix.base.models import Order, Event, OrderPayment, OrderRefund, Quota
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse
from datetime import datetime
from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.constants import XPAY_OPERATION_RECORD, XPAY_OPERATION_REFUND, XPAY_RESULT_AUTHORIZED, XPAY_RESULT_RECORDED, XPAY_RESULT_PENDING, XPAY_RESULT_REFUNDED

logger = logging.getLogger(__name__)

def encode_order_id(orderPayment: OrderPayment, event: Event, newAttempt: bool = False) -> str:
    attempt_id = 0
    if "attempt_counter" not in orderPayment.info_data:
        orderPayment.info_data["attempt_counter"] = 0
        orderPayment.save(update_fields=["info"])
    elif newAttempt:
        attempt_id = int(orderPayment.info_data["attempt_counter"]) + 1
        orderPayment.info_data["attempt_counter"] = attempt_id
        orderPayment.save(update_fields=["info"])
    else:
        attempt_id = int(orderPayment.info_data["attempt_counter"])
    data: str = f"{event.organizer.slug}{event.slug}{orderPayment.full_id}{attempt_id}"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:18]

def generate_mac(data: list, provider: BasePaymentProvider) -> str:
    hash_algo = hashlib.new(provider.settings.hash)
    for el in data:
        hash_algo.update(f"{el[0]}={str(el[1])}".encode("UTF-8"))
    hash_algo.update(provider.settings.mac_secret_pass.encode("UTF-8"))
    return hash_algo.hexdigest()

class OrderOperation:
    def __init__(self, data: dict):
        if "tipoOperazione" not in data or "stato" not in data or "dataOperazione" not in data:
            raise ValueError(_('Could not parse operation.'))
        self.type = data["tipoOperazione"]
        self.status = data["stato"]
        # 2024-07-25 12:41:47.0
        self.timestamp = datetime.strptime(data["dataOperazione"], "%Y-%m-%d %H:%M:%S.%f")

class OrderStatus:
    def __init__(self, transaction_id: str, data: dict):
        # Throw if outside data is unparseable
        is_valid = data and isinstance(data, dict) and "esito" in data and data["esito"] == "OK"
        is_valid = is_valid and "report" in data and isinstance(data["report"], list)
        is_valid = is_valid and len(data["report"]) > 0 and isinstance(data["report"][0], dict)
        if not is_valid: raise ValueError(_('Could not parse order {}' % transaction_id))
        report = data["report"][0]

        # Throw if report data is unparseable
        is_valid = is_valid and "codiceTransazione" in report and report["codiceTransazione"] == transaction_id
        is_valid = is_valid and "dettaglio" in report and isinstance(report["dettaglio"], list) and len(report["dettaglio"]) > 0
        if not is_valid: raise ValueError(_('Could not parse order {}' % transaction_id))
        self.transaction_id = report["codiceTransazione"]

        #Throw if detail is unparseable        
        details = report["dettaglio"][0]
        is_valid = is_valid and isinstance(details, dict) and "stato" in details
        if not is_valid: raise ValueError(_('Could not parse order {}' % transaction_id))

        self.fallback_status = details["stato"]
        self.operations = []
        if "operazioni" in details and isinstance(details["operazioni"], list):
            for op in details["operazioni"]:
                op_to_add = OrderOperation(op)
                self.operations.append(op_to_add)
    
    @property
    def operation_status(self):
        return None if len(self.operations) == 0 else sorted(self.operations, key=lambda os: os.timestamp)[0].status
        
    @property
    def status(self):
        return self.fallback_status if len(self.operations) == 0 else self.operation_status
            
