from setuptools import setup


setup(entry_points="""
[pretix.plugin]
pretix_xpay=pretix_xpay:PretixPluginMeta
"""
)
