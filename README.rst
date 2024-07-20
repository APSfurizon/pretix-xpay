XPay
==========================

This is a plugin for `pretix`_. 

Accept payments through the Nexi's XPay interface

Flow chart
----------
- Arrive to execute payment https://docs.pretix.eu/en/latest/development/api/payment.html#pretix.base.payment.pretix.base.payment.BasePaymentProvider.BasePaymentProvider.execute_payment
- Post /hpp with url to custom view with order id generation, PREAUTH, get hostedPage
- Return hostedPage, so the user is redirected
- Nexi will land user on custom view:

On custom view:

- retrive order using the orderId (reverse the algorithm)
- check if preauth is successfull
- confirm order (order.confirm())
- if not Quota.QuotaExceededException, call /captures
- else, call /refunds


Development setup
-----------------

1. Make sure that you have a working `pretix development setup`_.

2. Clone this repository.

3. Activate the virtual environment you use for pretix development.

4. Execute ``python setup.py develop`` within this directory to register this application with pretix's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local pretix server. You can now use the plugin from this repository for your events by enabling it in
   the 'plugins' tab in the settings.

This plugin has CI set up to enforce a few code style rules. To check locally, you need these packages installed::

    pip install flake8 isort black

To check your plugin for rule violations, run::

    black --check .
    isort -c .
    flake8 .

You can auto-fix some of these issues by running::

    isort .
    black .

To automatically check for these issues before you commit, you can run ``.install-hooks``.


License
-------


Copyright 2024 Furizon Team

Released under the terms of the Apache License 2.0



.. _pretix: https://github.com/pretix/pretix
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
