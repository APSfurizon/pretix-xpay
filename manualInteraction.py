import requests
import hashlib
import sys
from time import time
from pretix_xpay.constants import *

print(f"{sys.argv[0]} ALIAS_KEY SECRET_MAC ORG_SLUG EVENT_SLUG")

ALIAS_KEY = sys.argv[1]
MAC_SECRET_PASS = sys.argv[2]
API_URL = PROD_URL
HASH = "sha-1"
SALT = "gabibbo"
ORG_EVENT_SLUG = f"{sys.argv[3]}{sys.argv[4]}"

def encode_order_id(paymentFullId) -> str:
    data: str = f"{ORG_EVENT_SLUG}{paymentFullId}{SALT}"
    print(data)
    s = hashlib.sha256(data.encode('utf-8')).hexdigest()[:18]
    print(s)
    return s

def generate_mac(data: list) -> str:
    hash_algo = hashlib.new(HASH)
    for el in data:
        hash_algo.update(f"{el[0]}={str(el[1])}".encode("UTF-8"))
    hash_algo.update(MAC_SECRET_PASS.encode("UTF-8"))
    return hash_algo.hexdigest()

def get_order_status(paymentFullId: str):
    """
    Creates a body to requests an order's status, then launches the request and analyzes its response.
    If the response status is valid, it will try parse the response to an OrderStatus object.

    :param OrderPayment payment: The payment from which issue a refund
    :param XPayPaymentProvider provider: The payment provider which holds the XPay logic
    :rtype: OrderStatus
    :raises ValueError: if the status request has its state to anything different than 'OK', if the HMAC verification fails or if it fails parsing the response.
    """
    alias_key = ALIAS_KEY
    transaction_code = encode_order_id(paymentFullId)
    timestamp = int(time() * 1000)

    hmac = generate_mac([
        ("apiKey", alias_key),
        ("codiceTransazione", transaction_code),
        ("timeStamp", timestamp)
    ])

    body = {
        "apiKey": alias_key,
        "codiceTransazione": transaction_code,
        "timeStamp": timestamp,
        "mac": hmac
    }
    try:
        result = post_api_call(ENDPOINT_ORDERS_STATUS, body)
    except Exception as e:
        raise RuntimeError("error api")
    if (result["esito"] == "KO"):
        if result["errore"]["codice"] == 2:  # https://ecommerce.nexi.it/specifiche-tecniche/tabelleecodifiche/codicierroreapirestful.html
            raise RuntimeError("Order not found")
        raise ValueError(('Unable to check the order status for %s. Error code: %d. Error message: "%s"')
                         % (transaction_code, result["errore"]["codice"], result["errore"]["messaggio"]))
    if (result["esito"] != "OK"):
        raise ValueError(('Invalid parameter "esito" (%s) for %s.') % (result["esito"], transaction_code))

    hmac = generate_mac([
        ("esito", result["esito"]),
        ("idOperazione", result["idOperazione"]),
        ("timeStamp", result["timeStamp"])
    ])
    if (hmac != result["mac"]):
        raise ValueError(('Unable to validate the order status for %s.') % transaction_code)

    #try:
    #    to_return = OrderStatus(transaction_code, result)
    #except Exception as e:
    #    print("Shit")
    #    raise e
    #return to_return

def post_api_call(path: str, params: dict):
    '''Launches a POST request to XPay's servers'''
    try:
        #  timeout to slightly more than a multiple of 3, to account for TCP retrasmission time
        r = requests.post(f"{API_URL}{path}", json=params, timeout=31.5)
        r.raise_for_status()
        print(r.text)
        return r.json()
    except requests.RequestException:
        print("error")

if __name__ == "__main__":
    get_order_status("J3DTR-P-3")