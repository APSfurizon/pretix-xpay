from django.utils.translation import gettext_lazy

from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_xpay"
    verbose_name = "XPay"

    class PretixPluginMeta:
        name = gettext_lazy("XPay")
        author = "Furizon Team"
        description = gettext_lazy("Accept payments through the Nexi's XPay interface")
        visible = True
        version = __version__
        category = "PAYMENT"
        picture = 'pretix_xpay/XPay-logo.png'
        compatibility = "pretix>=2024.7.0"

    def ready(self):
        from . import signals  # NOQA
