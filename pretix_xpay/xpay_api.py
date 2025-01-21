import logging
import requests
from django.http import Http404, HttpRequest
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Order, OrderPayment, OrderRefund, Quota
from pretix.base.payment import PaymentException
from pretix.multidomain.urlreverse import build_absolute_uri
from time import time

from pretix_xpay.constants import (
    ENDPOINT_ORDERS_CANCEL,
    ENDPOINT_ORDERS_CONFIRM,
    ENDPOINT_ORDERS_CREATE,
    ENDPOINT_ORDERS_REFUND,
    ENDPOINT_ORDERS_STATUS,
)
from pretix_xpay.payment import XPayPaymentProvider
from pretix_xpay.utils import (
    OrderStatus,
    build_order_desc,
    encode_order_id,
    generate_mac,
    get_alias_key,
    get_xpay_api_url,
    send_refund_needed_email,
    translate_language,
)

logger = logging.getLogger(__name__)


def initialize_payment_get_params(
    payment: OrderPayment,
    provider: XPayPaymentProvider,
    order_code: str,
    order_salted_hash: str,
    payment_pk,
) -> dict:
    """
    Initializes the payment creation parameters

    :param OrderPayment payment: The payment from which issue the order accounting
    :param XPayPaymentProvider provider: The payment provider which holds the XPay logic
    :param str order_code: the order's code
    :param str order_salted_hash: the order's secure hash
    :param int payment_pk: the payment's primary key
    :rtype: dict
    """
    transaction_code = encode_order_id(payment, provider.event, provider)
    amount = int(payment.amount * 100)

    return {
        "alias": get_alias_key(provider),
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
        "mac": generate_mac(
            [("codTrans", transaction_code), ("divisa", "EUR"), ("importo", amount)],
            provider,
        ),
        # "mail": payment.order.email, # Disabled because someone could create an order for somebody else. If this field is specified, xpay forces this email
        "languageId": translate_language(payment.order),
        "descrizione": build_order_desc(payment.order),
        "TCONTAB": "D",  # Preauthing first. We're gonna finalize the payment after we're sure there's enough quota and the order is marked as paid,
        "pretixOrder": order_code,
        "pretixPayment": payment.full_id,
        "pretixEvent": f"{provider.event.organizer.slug}/{provider.event.slug}",
    }


def initialize_payment_get_url(provider: XPayPaymentProvider) -> str:
    return get_xpay_api_url(provider) + ENDPOINT_ORDERS_CREATE


def return_page_validate_digest(
    request: HttpRequest, provider: XPayPaymentProvider
) -> bool:
    """
    Validates the HMAC hash after successfully paying for the order.
    """
    hmac = generate_mac(
        [
            ("codTrans", request.GET["codTrans"]),
            ("esito", request.GET["esito"]),
            ("importo", request.GET["importo"]),
            ("divisa", "EUR"),
            ("data", request.GET["data"]),
            ("orario", request.GET["orario"]),
            ("codAut", request.GET["codAut"]),
        ],
        provider,
    )
    return hmac == request.GET["mac"]


def confirm_preauth(payment: OrderPayment, provider: XPayPaymentProvider):
    """
    Creates the body for a POST request to issue a capture after preauthorization, launches it and analyzes the returned data.

    :param OrderPayment payment: The payment from which issue the capture
    :param XPayPaymentProvider provider: The payment provider which holds the XPay logic
    :rtype: None
    :raises PaymentException: if the capture request returns its state to anything different than 'OK' or if the HMAC verification fails.
    """
    logger.info(f"XPAY_confirm_preauth [{payment.full_id}]: Trying to capture preauth")
    alias_key = get_alias_key(provider)
    transaction_code = encode_order_id(payment, provider.event, provider)
    amount = int(payment.amount * 100)
    timestamp = int(time() * 1000)
    hmac = generate_mac(
        [
            ("apiKey", alias_key),
            ("codiceTransazione", transaction_code),
            ("divisa", "978"),
            ("importo", amount),
            ("timeStamp", timestamp),
        ],
        provider,
    )

    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "importo": amount,
        "divisa": "978",
        "timeStamp": timestamp,
        "mac": hmac,
    }
    try:
        result = post_api_call(provider, ENDPOINT_ORDERS_CONFIRM, body)
    except Exception as e:
        logger.error(
            f"XPAY_confirm_preauth [{payment.full_id}]: POST call failed: {repr(e)}"
        )
        raise PaymentException(
            _(
                "An error occurred with the XPay's servers while capturing the order. "
                "Contact the event organizer and check if your order is successfull and the correct amount of "
                "money has been trasferred from your account. "
                "Be sure to remember the transaction code #%s. Exception: %s"
            )
            % (f"{payment.order.code}-{transaction_code}", repr(e))
        )

    hmac = generate_mac(
        [
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"]),
        ],
        provider,
    )

    if result["esito"] == "KO":
        logger.error(
            f"XPAY_confirm_preauth [{payment.full_id}]: confirm preauth request failed gracefully."
        )
        raise PaymentException(
            _(
                "Preauth confirm request failed with error code %d: %s. "
                "Contact the event organizer and check if your order is successfull and the correct amount of "
                "money has been trasferred from your account. "
                "Be sure to remember the transaction code "
                "#%s"
            )
            % (
                result["errore"]["codice"],
                result["errore"]["messaggio"],
                f"{payment.order.code}-{transaction_code}",
            )
        )
    elif result["esito"] == "OK":
        if hmac != result["mac"]:
            logger.error(
                f"XPAY_confirm_preauth [{payment.full_id}]: HMAC verification failed."
            )
            raise PaymentException(
                _(
                    "Unable to validate the preauth confirm. "
                    "Contact the event organizer and check if your order is successfull and the correct amount of "
                    "money has been trasferred from your account. "
                    "Be sure to remember the transaction code #%s"
                )
                % f"{payment.order.code}-{transaction_code}"
            )
        logger.info(
            f"XPAY_confirm_preauth [{payment.full_id}]: Preauth captured succeesfully!"
        )
        pass  # If the process is ok, we're done
    else:
        logger.error(
            f'XPAY_confirm_preauth [{payment.full_id}]: Unknown result \'{result["esito"]}\'.'
        )
        raise PaymentException(
            _(
                "Unknown server response (%s) in the preauth confirm process. "
                "Contact the event organizer and check if your order is successfull and the correct amount of "
                "money has been trasferred from your account. "
                "Be sure to remember the transaction code #%s"
            )
            % (result["esito"], f"{payment.order.code}-{transaction_code}")
        )


def refund_preauth(payment: OrderPayment, provider: XPayPaymentProvider):
    """
    Creates the body for a POST request to issue a refund of a preauthorized transaction, launches it and analyzes the returned data.

    :param OrderPayment payment: The payment from which issue a preauth refund
    :param XPayPaymentProvider provider: The payment provider which holds the XPay logic
    :rtype: None
    :raises PaymentException: if the refund request returns its state to anything different than 'OK' or if the HMAC verification fails.
    """
    logger.info(f"XPAY_refund_preauth [{payment.full_id}]: Trying to refund preauth")
    alias_key = get_alias_key(provider)
    transaction_code = encode_order_id(payment, provider.event, provider)
    amount = int(payment.amount * 100)
    timestamp = int(time() * 1000)
    hmac = generate_mac(
        [
            ("apiKey", alias_key),
            ("codiceTransazione", transaction_code),
            ("divisa", "978"),
            ("importo", amount),
            ("timeStamp", timestamp),
        ],
        provider,
    )

    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "importo": amount,
        "divisa": "978",
        "timeStamp": timestamp,
        "mac": hmac,
    }
    try:
        result = post_api_call(provider, ENDPOINT_ORDERS_CANCEL, body)
    except Exception as e:
        send_refund_needed_email(payment, "xpay.refund_preauth-expPost")
        logger.error(
            f"XPAY_refund_preauth [{payment.full_id}]: POST call failed: {repr(e)}"
        )
        raise PaymentException(
            _(
                "An error occurred with the XPay's servers while issuing a refund of a preauth. "
                "Contact the event organizer to execute the refund manually. "
                "Be sure to remember the transaction code #%s. Exception: %s"
            )
            % (f"{payment.order.code}-{transaction_code}", repr(e))
        )

    hmac = generate_mac(
        [
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"]),
        ],
        provider,
    )

    if result["esito"] == "KO":
        logger.error(
            f"XPAY_refund_preauth [{payment.full_id}]: preauth refund request failed gracefully."
        )
        send_refund_needed_email(payment, "xpay.refund_preauth-ko")
        raise PaymentException(
            _(
                "Preauth refund request failed with error code %d: %s. Contact the event organizer to execute the refund manually. "
                "Be sure to remember the transaction code "
                "#%s"
            )
            % (
                result["errore"]["codice"],
                result["errore"]["messaggio"],
                f"{payment.order.code}-{transaction_code}",
            )
        )
    elif result["esito"] == "OK":
        if hmac != result["mac"]:
            logger.error(
                f"XPAY_refund_preauth [{payment.full_id}]: HMAC verification failed."
            )
            send_refund_needed_email(payment, "xpay.refund_preauth-hmac")
            raise PaymentException(
                _(
                    "Unable to validate the preauth refund. Contact the event organizer to execute the refund manually. "
                    "Be sure to remember the transaction code #%s"
                )
                % f"{payment.order.code}-{transaction_code}"
            )
        logger.info(
            f"XPAY_refund_preauth [{payment.full_id}]: Preauth refunded successfully!"
        )
        pass  # If the process is ok, we're done
    else:
        logger.error(
            f'XPAY_refund_preauth [{payment.full_id}]: Unknown result \'{result["esito"]}\'.'
        )
        send_refund_needed_email(payment, "xpay.refund_preauth-unknown")
        raise PaymentException(
            _(
                "Unknown server response (%s) in the preauth confirm process. Contact the event organizer to execute the refund manually. "
                "Be sure to remember the transaction code #%s"
            )
            % (result["esito"], f"{payment.order.code}-{transaction_code}")
        )


def get_order_status(
    payment: OrderPayment, provider: XPayPaymentProvider
) -> OrderStatus:
    """
    Creates a body to requests an order's status, then launches the request and analyzes its response.
    If the response status is valid, it will try parse the response to an OrderStatus object.

    :param OrderPayment payment: The payment from which issue a refund
    :param XPayPaymentProvider provider: The payment provider which holds the XPay logic
    :rtype: OrderStatus
    :raises ValueError: if the status request has its state to anything different than 'OK', if the HMAC verification fails or if it fails parsing the response.
    """
    alias_key = get_alias_key(provider)
    transaction_code = encode_order_id(payment, provider.event, provider)
    timestamp = int(time() * 1000)

    hmac = generate_mac(
        [
            ("apiKey", alias_key),
            ("codiceTransazione", transaction_code),
            ("timeStamp", timestamp),
        ],
        provider,
    )

    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "timeStamp": timestamp,
        "mac": hmac,
    }
    try:
        result = post_api_call(provider, ENDPOINT_ORDERS_STATUS, body)
    except Exception as e:
        raise RuntimeError(
            _("XPay server error while checking the status for %s. Exception: %s")
            % (transaction_code, repr(e))
        )
    if result["esito"] == "KO":
        if (
            result["errore"]["codice"] == 2
        ):  # https://ecommerce.nexi.it/specifiche-tecniche/tabelleecodifiche/codicierroreapirestful.html
            raise Http404("Order not found")
        raise ValueError(
            _(
                'Unable to check the order status for %s. Error code: %d. Error message: "%s"'
            )
            % (
                transaction_code,
                result["errore"]["codice"],
                result["errore"]["messaggio"],
            )
        )
    if result["esito"] != "OK":
        raise ValueError(
            _('Invalid parameter "esito" (%s) for %s.')
            % (result["esito"], transaction_code)
        )

    hmac = generate_mac(
        [
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"]),
        ],
        provider,
    )
    if hmac != result["mac"]:
        raise ValueError(
            _("Unable to validate the order status for %s.") % transaction_code
        )

    try:
        to_return = OrderStatus(transaction_code, result)
    except Exception as e:
        logger.error(
            f"XPAY_get_order_status [{payment.full_id}]: Could not parse OrderStatus: {repr(e)}"
        )
        raise e
    return to_return


def confirm_payment_and_capture_from_preauth(
    payment: OrderPayment, provider: XPayPaymentProvider, order: Order
):
    """
    Tries to confirm a payment and if it success, it captures the relative preauth, otherwise if a QuotaExceededException is met, it refunds it

    :param OrderPayment payment: The payment to confirm
    :param XPayPaymentProvider provider: The payment provider which holds the XPay logic
    :rtype: None
    """
    logger.info(
        f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Trying to confirm payment"
    )
    try:
        if (
            payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
        ):  # Manual detect for race conditions for skip the double confirm/refund
            logger.info(
                f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Payment was already confirmed! Race condition detected."
            )
            return
        payment.confirm()
        logger.info(
            f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Payment confirmed!"
        )
        order.refresh_from_db()

        # Payment confirmed, take the preauthorized money
        confirm_preauth(payment, provider)
        logger.info(
            f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Successfully requested capture operation."
        )

    except Quota.QuotaExceededException as e:
        # Payment failed, cancel the preauthorized money
        logger.info(
            f"XPAY_confirm_payment_and_capture_from_preauth [{payment.full_id}]: Tried confirming payment, but quota was exceeded."
        )
        refund_preauth(payment, provider)

        raise e


def refund(
    refund: OrderRefund, provider: XPayPaymentProvider
):  # Same endpoint of preauth refund, but slightly different logic
    """
    Creates the body for a POST request to issue a refund, launches it and analyzes the returned data.

    :param OrderPayment payment: The payment from which issue a refund
    :param XPayPaymentProvider provider: The payment provider which holds the XPay logic
    :rtype: None
    :raises PaymentException: if the refund request returns its state to anything different than 'OK' or if the HMAC verification fails.
    """
    logger.info(
        f"XPAY_refund [{refund.payment.full_id}@{refund.full_id}]: Trying to refund payment by {refund.amount}€"
    )
    alias_key = get_alias_key(provider)
    transaction_code = encode_order_id(refund.payment, provider.event, provider)
    amount = int(refund.amount * 100)
    timestamp = int(time() * 1000)

    hmac = generate_mac(
        [
            ("apiKey", alias_key),
            ("codiceTransazione", transaction_code),
            ("divisa", "978"),
            ("importo", amount),
            ("timeStamp", timestamp),
        ],
        provider,
    )

    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "importo": amount,
        "divisa": "978",
        "timeStamp": timestamp,
        "mac": hmac,
    }

    try:
        result = post_api_call(provider, ENDPOINT_ORDERS_REFUND, body)
    except Exception as e:
        logger.error(
            f"XPAY_refund [{refund.payment.full_id}@{refund.full_id}]: POST call failed: {repr(e)}"
        )
        raise PaymentException(
            _(
                "An error occurred with the XPay's servers while refunding the order. "
                "Contact the event organizer and check if your refund is successfull and "
                "the correct amount of money has been trasferred back to your account. "
                "Be sure to remember the transaction code #%s. Exception: %s"
            )
            % (f"{refund.order.code}-{transaction_code}", repr(e))
        )
    if result["esito"] == "KO":
        logger.error(
            f"XPAY_refund [{refund.payment.full_id}@{refund.full_id}]: refund request failed gracefully."
        )
        raise ValueError(
            _('Unable to refund payment %s. Error code: %d. Error message: "%s"')
            % (
                transaction_code,
                result["errore"]["codice"],
                result["errore"]["messaggio"],
            )
        )
    if result["esito"] != "OK":
        logger.error(
            f'XPAY_refund [{refund.payment.full_id}@{refund.full_id}]: Unknown result \'{result["esito"]}\'.'
        )
        raise ValueError(
            _('Invalid parameter "esito" (%s) for %s.')
            % (result["esito"], transaction_code)
        )

    hmac = generate_mac(
        [
            ("esito", result["esito"]),
            ("idOperazione", result["idOperazione"]),
            ("timeStamp", result["timeStamp"]),
        ],
        provider,
    )
    if hmac != result["mac"]:
        logger.error(
            f"XPAY_refund [{refund.payment.full_id}@{refund.full_id}]: HMAC verification failed."
        )
        raise ValueError(
            _("Unable to validate refund response for %s.") % transaction_code
        )

    logger.info(
        f"XPAY_refund [{refund.payment.full_id}@{refund.full_id}]: Refund of {refund.amount}€ was successfull!"
    )


def post_api_call(provider: XPayPaymentProvider, path: str, params: dict):
    """Launches a POST request to XPay's servers"""
    try:
        #  timeout to slightly more than a multiple of 3, to account for TCP retrasmission time
        r = requests.post(
            f"{get_xpay_api_url(provider)}{path}", json=params, timeout=31.5
        )
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        logger.exception("POST: Could not reach XPay's servers.")
        raise PaymentException(_("Could not reach payment provider."))
