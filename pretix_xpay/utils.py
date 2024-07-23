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

X_SUCCESS_RESULTS: Annotated[list, "All the success values operationResult might contain"] = [
    'AUTHORIZED',
    'EXECUTED',
    'PENDING',
    'REFUNDED'
]

X_FAIL_RESULTS: Annotated[list, "All the failing values operationResult might contain"] = [
    'DECLINED',
    'DENIED_BY_RISK',
    'THREEDS_VALIDATED',
    'THREEDS_FAILED',
    'CANCELED',
    'VOIDED',
    'FAILED',
]

X_TYPE_AUTHORIZATION = 'AUTHORIZATION'
X_TYPE_CAPTURE = 'CAPTURE'
X_TYPE_VOID = 'VOID'
X_TYPE_REFUND = 'REFUND'
X_TYPE_CANCEL = 'CANCEL'
X_TYPE_CARD_VERIFICATION = 'CARD_VERIFICATION'


X_STATE_MULTIPLE_OPERATIONS = 'MULTIPLE'
X_STATE_SINGLE_OPERATION = 'SINGLE'

class XOrderOperation:
        def __init__(self, operation_data: dict):
            self.operation_id = operation_data["operationId"] if "operationId" in operation_data else None
            self.operation_type = operation_data["operationType"] if "operationType" in operation_data else None
            self.operation_result = operation_data["operationResult"] if "operationResult" in operation_data else None
        
        @property
        def success (self):
            return self.operation_result in X_SUCCESS_RESULTS

class XOrderStatus:
    def __init__(self, data: dict | None):
        self.operations = []
        if data is not None:
            if "operations" in data and isinstance(data["operations"], list):
                for single_operation in data["operations"]:
                    op_to_add = XOrderOperation(single_operation)
                    self.operations.append(op_to_add)

    @property
    def type(self):
        return X_STATE_MULTIPLE_OPERATIONS if len(self.operations) > 1 else X_STATE_SINGLE_OPERATION

    @property
    def operation(self) -> XOrderOperation | None:
        if self.type == X_STATE_MULTIPLE_OPERATIONS:
            raise ValueError('There are multiple operations.')
        else:
            return self.operations[0] if len(self.operations) > 0 else None
        
    @property
    def success(self):
        result = True
        result = result and len(self.operation) > 0
        if result:
            for operation in self.operations:
                op: XOrderOperation = operation
                result = result and op.success
                if not result: break
        return result