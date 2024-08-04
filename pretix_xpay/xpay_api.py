import logging
import requests 
from django.http import HttpRequest, Http404
from django.utils.translation import gettext_lazy as _
from pretix.base.models import OrderPayment, Order, Quota
from pretix.base.payment import PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri
from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.utils import encode_order_id, generate_mac, build_order_desc, translate_language
from pretix_xpay.utils import OrderStatus, send_refund_needed_email
from pretix_xpay.constants import *
from time import time

logger = logging.getLogger(__name__)

def initialize_payment_get_params(provider: XPayPaymentProvider, payment: OrderPayment, order_code: str, order_salted_hash: str, payment_pk) -> dict:
    transaction_code = encode_order_id(payment, provider.event)
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
        "languageId": translate_language(payment.order),
        "descrizione": build_order_desc(payment.order),
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
    try:
        result = post_api_call(provider, ENDPOINT_ORDERS_CONFIRM, body)
    except Exception as e:
        raise PaymentException(_("An error occurred with the XPay's servers while settling the order. Contact the event organizer and check if your order is successfull and the correct amount of money has been trasferred from your account. Be sure to remember the transaction code #%s. Exception: %s") % (f"{payment.order.code}-{transaction_code}", repr(e)))

    hmac = generate_mac([
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"])
        ], provider)

    if(result["esito"] == "KO"):
        logger.error(f"XPAY_confirm_preauth [{payment.full_id}]: refund request failed gracefully.")
        raise PaymentException(_('Preauth confirm request failed with error code %d: %s. Contact the event organizer and check if your order is successfull and the correct amount of money has been trasferred from your account. Be sure to remember the transaction code #%s') % (result["errore"]["codice"], result["errore"]["messaggio"], f"{payment.order.code}-{transaction_code}"))
    elif(result["esito"] == "OK"):
        if(hmac != result["mac"]):
            logger.error(f"XPAY_confirm_preauth [{payment.full_id}]: HMAC verification failed.")
            raise PaymentException(_('Unable to validate the preauth confirm. Contact the event organizer and check if your order is successfull and the correct amount of money has been trasferred from your account. Be sure to remember the transaction code #%s') % f"{payment.order.code}-{transaction_code}")
        pass # If the process is ok, we're done
    else:
        logger.error(f"XPAY_confirm_preauth [{payment.full_id}]: Unknown result '{result["esito"]}'.")
        raise PaymentException(_('Unknown server response (%s) in the preauth confirm process. Contact the event organizer and check if your order is successfull and the correct amount of money has been trasferred from your account. Be sure to remember the transaction code #%s') % (result["esito"], f"{payment.order.code}-{transaction_code}"))


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
    try:
        result = post_api_call(provider, ENDPOINT_ORDERS_CANCEL, body)
    except Exception as e:
        send_refund_needed_email(payment, "xpay.refund_preauth-expPost")
        logger.error(f"XPAY_refund_preauth [{payment.full_id}]: POST call failed: {repr(e)}")
        raise PaymentException(_("An error occurred with the XPay's servers while issuing a refund. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s. Exception: %s") % (f"{payment.order.code}-{transaction_code}", repr(e)))

    hmac = generate_mac([
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"])
        ], provider)

    if(result["esito"] == "KO"):
        logger.error(f"XPAY_refund_preauth [{payment.full_id}]: refund request failed gracefully.")
        send_refund_needed_email(payment, "xpay.refund_preauth-ko")
        raise PaymentException(_('Preauth refund request failed with error code %d: %s. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s') % (result["errore"]["codice"], result["errore"]["messaggio"], f"{payment.order.code}-{transaction_code}"))
    elif(result["esito"] == "OK"):
        if(hmac != result["mac"]):
            logger.error(f"XPAY_refund_preauth [{payment.full_id}]: HMAC verification failed.")
            send_refund_needed_email(payment, "xpay.refund_preauth-hmac")
            raise PaymentException(_('Unable to validate the preauth refund. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s') % f"{payment.order.code}-{transaction_code}")
        pass # If the process is ok, we're done
    else:
        logger.error(f"XPAY_refund_preauth [{payment.full_id}]: Unknown result '{result["esito"]}'.")
        send_refund_needed_email(payment, "xpay.refund_preauth-unknown")
        raise PaymentException(_('Unknown server response (%s) in the preauth confirm process. Contact the event organizer to execute the refund manually. Be sure to remember the transaction code #%s') % (result["esito"], f"{payment.order.code}-{transaction_code}"))

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
        if result["errore"]["codice"] == 2:
            raise Http404("Order not found")
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
    
    try: 
        to_return = OrderStatus(transaction_code, result)
    except Exception as e:
        logger.error(f"XPAY_get_order_status [{payment.full_id}]: Could not parse OrderStatus: {repr(e)}")
        raise e
    return to_return

def confirm_payment_and_capture_from_preauth(payment: OrderPayment, provider: XPayPaymentProvider, order: Order):
    try:
        if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED: # Manual detect for race conditions for skip the double confirm/refund
            logger.info(f'XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Payment was already confirmed! Race condition detected.')
            return
        payment.confirm()
        logger.info(f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Payment confirmed!")
        order.refresh_from_db()

        # Payment confirmed, take the preauthorized money
        confirm_preauth(payment, provider)
        logger.info(f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Successfully requested capture operation.")
        
    except Quota.QuotaExceededException as e:
        # Payment failed, cancel the preauthorized money
        logger.info(f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Tried confirming payment, but quota was exceeded.")
        refund_preauth(payment, provider)

        raise e

def get_xpay_api_url(pp: XPayPaymentProvider):
    return TEST_URL if pp.event.testmode else PROD_URL

def post_api_call(pp : XPayPaymentProvider, path: str, params: dict):
    try:
        r = requests.post(f"{get_xpay_api_url(pp)}{path}", json=params, timeout=31.5) #timeout to slightly more than a multiplel of 3, since it is TCP retrasmission time
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        logger.exception("POST: Could not reach XPay's servers.")
        raise PaymentException(_("Could not reach payment provider."))