import hashlib
import logging
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Order, Event, OrderPayment, OrderPosition
from pretix.base.payment import BasePaymentProvider
from datetime import datetime
from pretix_xpay.constants import LANGUAGE_DEFAULT, LANGUAGES_TRANSLATION
from i18nfield.strings import LazyI18nString
from pretix.base.services.mail import mail

logger = logging.getLogger(__name__)

def encode_order_id(orderPayment: OrderPayment, event: Event) -> str:
    data: str = f"{event.organizer.slug}{event.slug}{orderPayment.full_id}gabibbo"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:18]

def generate_mac(data: list, provider: BasePaymentProvider) -> str:
    hash_algo = hashlib.new(provider.settings.hash)
    for el in data:
        hash_algo.update(f"{el[0]}={str(el[1])}".encode("UTF-8"))
    hash_algo.update(provider.settings.mac_secret_pass.encode("UTF-8"))
    return hash_algo.hexdigest()

def send_refund_needed_email(orderPayment: OrderPayment, origin: str = "-") -> None:
    if orderPayment.order.settings.payment_error_email.strip():
        to = [k.strip() for k in orderPayment.order.settings.payment_error_email.split(",")]
        subject = _('Severe error in XPAY payment process')
        body = LazyI18nString.from_gettext(_(
            'A severe error occurred while processing the OrderPayment {op_full_id} with transactionId {transaction_id}.\n'
            'The user has probably paid more than expected (due to a double payment or to a Quota Exceeded problem) and a manual refound is needed.\n\n'
            'Please verify the payment and order status and, in case, proceed with a manual refund.\n\n\n'
            'This email is autogenerated by the XPay plugin by the "{origin}" origin. For more information, contact us on https://github.com/APSfurizon/pretix-xpay/'
        ))
        ctx = {
            "op_full_id": orderPayment.full_id,
            "transaction_id": encode_order_id(orderPayment, orderPayment.order.event),
            "origin" : origin
        }
        mail(to, subject, body, ctx)
    
def translate_language(order: Order) -> str:
    return LANGUAGES_TRANSLATION[order.locale] if order.locale in LANGUAGES_TRANSLATION else LANGUAGE_DEFAULT

def build_order_desc(order: Order) -> str:
    itemNames = []
    p: OrderPosition
    for p in order.positions.all() : itemNames.append(get_translated_text(p.item.name, order))
    return f"[{order.event.organizer.name} / {order.event.name}] Order {order.code}: {', '.join(itemNames)}"

def get_translated_text(value, order: Order) -> str:
    if isinstance(value, LazyI18nString):
        lazy: LazyI18nString = value
        return lazy.localize(order.locale)
    elif isinstance(value, str):
        return value
    else:
        raise ValueError('Unexpected item type')

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
        if not is_valid: raise ValueError(_('Could not parse order %s') % transaction_id)
        report = data["report"][0]

        # Throw if report data is unparseable
        is_valid = is_valid and "codiceTransazione" in report and report["codiceTransazione"] == transaction_id
        is_valid = is_valid and "dettaglio" in report and isinstance(report["dettaglio"], list) and len(report["dettaglio"]) > 0
        if not is_valid: raise ValueError(_('Could not parse order %s') % transaction_id)
        self.transaction_id = report["codiceTransazione"]

        #Throw if detail is unparseable        
        details = report["dettaglio"][0]
        is_valid = is_valid and isinstance(details, dict) and "stato" in details
        if not is_valid: raise ValueError(_('Could not parse order %s') % transaction_id)

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
            
