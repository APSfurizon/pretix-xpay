TEST_URL = "https://int-ecommerce.nexi.it/ecomm/"
PROD_URL = "https://ecommerce.nexi.it/ecomm/"

ENDPOINT_ORDERS_CREATE = f"ecomm/DispatcherServlet"
ENDPOINT_ORDERS_CONFIRM = f"api/bo/contabilizza"
ENDPOINT_ORDERS_CANCEL = f"api/bo/storna"

DOCS_TEST_CARDS_URL = "https://ecommerce.nexi.it/area-test"

HASH_TAG = "plugins:pretix_xpay"

XPAY_STATUS_SUCCESS = ["OK"]
XPAY_STATUS_FAILS = ["KO", "ANNULLO", "ERRORE"]
XPAY_STATUS_PENDING = ["PEN"]