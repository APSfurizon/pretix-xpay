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
from pretix.base.models import Event, OrderPayment, OrderRefund, Order, Quota
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri, eventreverse
from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.utils import encode_order_id, generate_mac, OrderStatus
from pretix_xpay.constants import *
from time import time

logger = logging.getLogger(__name__)

def initialize_payment_get_params(provider: XPayPaymentProvider, payment: OrderPayment, order_code: str, order_salted_hash: str, payment_pk) -> dict:
    transaction_code = encode_order_id(payment, provider.event, True)
    amount = int(payment.amount * 100)

    return {
        "alias": provider.settings.alias_key,
        "importo": amount,
        "divisa": "EUR",
        "codTrans": transaction_code,
        "url": build_absolute_uri(
                provider.event,
                "plugins:pretix_xpay:return",
                kwargs={
                    "order": order_code,
                    "payment": payment_pk,
                    "hash": order_salted_hash,
                    "result": "ok",
                },
            ),
        "url_back": build_absolute_uri(
                provider.event,
                "plugins:pretix_xpay:return",
                kwargs={
                    "order": order_code,
                    "payment": payment_pk,
                    "hash": order_salted_hash,
                    "result": "ko",
                },
            ),
        "mac": generate_mac([
                ("codTrans", transaction_code),
                ("divisa", "EUR"),
                ("importo", amount)
            ], provider),
        # "mail": payment.order.email, # Disabled because someone could create an order for somebody else. If this field is specified, xpay forces this email
        "languageId": "ITA", #TODO
        "descrizione": "TODO", #TODO
        "TCONTAB": "D" # Preauthing first. We're gonna finalize the payment after we're sure there's enough quota and the order is marked as paid
    }

def initialize_payment_get_url(provider: XPayPaymentProvider) -> str:
    return get_xpay_api_url(provider) + ENDPOINT_ORDERS_CREATE

def return_page_validate_digest(request: HttpRequest, provider: XPayPaymentProvider) -> bool:
    hmac = generate_mac([
            ("codTrans", request.GET["codTrans"]),
            ("esito", request.GET["esito"]),
            ("importo", request.GET["importo"]),
            ("divisa", "EUR"),
            ("data", request.GET["data"]),
            ("orario", request.GET["orario"]),
            ("codAut", request.GET["codAut"])
        ], provider)
    return hmac == request.GET["mac"]

def confirm_preauth(payment: OrderPayment, provider: XPayPaymentProvider):
    alias_key = provider.settings.alias_key
    transaction_code = encode_order_id(payment, provider.event)
    amount = int(payment.amount * 100)
    timestamp = int(time() * 1000)
    hmac = generate_mac([
            ("apiKey", alias_key),
            ("codiceTransazione", transaction_code),
            ("divisa", "978"),
            ("importo", amount),
            ("timeStamp", timestamp)
        ], provider)
    
    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "importo": amount,
        "divisa": "978",
        "timeStamp": timestamp,
        "mac": hmac
    }
    result = post_api_call(provider, ENDPOINT_ORDERS_CONFIRM, body)

    hmac = generate_mac([
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            
            ("timeStamp", result["timeStamp"])
        ], provider)
    if(hmac != result["mac"]):
        raise PaymentException(_('Unable to validate the preauth confirm. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s') % transaction_code)

    if(result["esito"] == "ko"):
        raise PaymentException(_('Preauth confirm request failed with error code %s: %s. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s') % (result["errore"]["codice"], result["errore"]["messaggio"], transaction_code))
    elif(result["esito"] == "ok"):
        pass # If the process is ok, we're done


def refund_preauth(payment: OrderPayment, provider: XPayPaymentProvider):
    alias_key = provider.settings.alias_key
    transaction_code = encode_order_id(payment, provider.event)
    amount = int(payment.amount * 100)
    timestamp = int(time() * 1000)
    hmac = generate_mac([
            ("apiKey", alias_key),
            ("codiceTransazione", transaction_code),
            ("divisa", "978"),
            ("importo", amount),
            ("timeStamp", timestamp)
        ], provider)
    
    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "importo": amount,
        "divisa": "978",
        "timeStamp": timestamp,
        "mac": hmac
    }
    result = post_api_call(provider, ENDPOINT_ORDERS_CANCEL, body)

    hmac = generate_mac([
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"])
        ], provider)

    if(result["esito"] == "ko"):
        raise PaymentException(_('Preauth refund request failed with error code %s: %s. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s') % result["errore"]["codice"], result["errore"]["messaggio"], transaction_code)
    elif(result["esito"] == "ok"):
        if(hmac != result["mac"]):
            raise PaymentException(_('Unable to validate the preauth refund. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s') % transaction_code)
        pass # If the process is ok, we're done

def get_order_status(payment: OrderPayment, provider: XPayPaymentProvider) -> OrderStatus:
    alias_key = provider.settings.alias_key
    transaction_code = encode_order_id(payment, provider.event)
    timestamp = int(time() * 1000)

    hmac = generate_mac([
            ("apiKey", alias_key),
            ("codiceTransazione", transaction_code),
            ("timeStamp", timestamp)
        ], provider)
    
    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "timeStamp": timestamp,
        "mac": hmac
    }
    result = post_api_call(provider, ENDPOINT_ORDERS_STATUS, body)
    if(result["esito"] == "KO"):
        raise PaymentException(_('Unable to check the order status for %s. Error code: %d. Error message: "%s"') % (transaction_code, result["errore"]["codice"], result["errore"]["messaggio"]))
    if(result["esito"] != "OK"):
        raise PaymentException(_('Invalid parameter "esito" (%s) for %s.') % (result["esito"], transaction_code))

    hmac = generate_mac([
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"])
        ], provider)
    if(hmac != result["mac"]):
        raise PaymentException(_('Unable to validate the order status for %s.') % transaction_code)
    
    return OrderStatus(transaction_code, result)

def confirm_payment_and_capture_from_preauth(payment: OrderPayment, provider: XPayPaymentProvider, order: Order):
    try:
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
            logger.info(f'XPAY [{payment.full_id}]: Payment was already confirmed! Race condition detected.')
            return
        payment.confirm()
        logger.info(f"XPAY [{payment.full_id}]: Payment confirmed!")
        order.refresh_from_db()

        # Payment confirmed, take the preauthorized money
        confirm_preauth(payment, provider)
        logger.info(f"XPAY [{payment.full_id}]: Successfully requested capture operation")
        
    except Quota.QuotaExceededException as e:
        # Payment failed, cancel the preauthorized money
        refund_preauth(payment, provider)
        logger.info(f"XPAY [{payment.full_id}]: Tried confirming payment, but quota was exceeded")
        payment.fail(info="Tried confirming payment, but quota was exceeded") #TODO; Check if manual fail() call is needed

        raise e

def get_xpay_api_url(pp: XPayPaymentProvider):
    return TEST_URL if pp.event.testmode else PROD_URL

def post_api_call(pp : XPayPaymentProvider, path: str, params: dict):
    try:
        r = requests.post(f"{get_xpay_api_url(pp)}{path}", json=params, timeout=31.5) #timeout to slightly more than a multiplel of 3, since it is TCP retrasmission time
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        logger.exception("Could not reach XPay's servers")
        raise PaymentException(_("Could not reach payment provider"))