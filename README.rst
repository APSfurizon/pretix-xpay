XPay
==========================

This is a plugin for `pretix`_. 

Accept payments through the Nexi's XPay interface

This implements the XPay Italian legacy system (https://ecommerce.nexi.it/specifiche-tecniche/), which should be implemented (as stated by the Nexi's support) if you haven't subscribed a contract with Banca Intesa or ex consorzio triveneto. We started implementing the XPay global/italian system (https://developer.nexi.it/en/servizio-ecommerce), but after a while we discovered we were using the other system so we stopped. You can find the implementation on the other branch (https://github.com/APSfurizon/pretix-xpay/tree/xpay-global). We have implemented everything up to the PREAUTH accept/cancel api endpoint (XPayOrderView.process_result in views.py)

TODO
----
- ✅ Do the TODO
- ✅ Solve the generation of orderId
- ✅ Auto refresh the pending orders
- ✅ Fix parametrized translations raise `python not all arguments converted during string formatting`
- Test everything
- ✅ messages.error()


Flow chart
----------
- Arrive to execute payment https://docs.pretix.eu/en/latest/development/api/payment.html#pretix.base.payment.pretix.base.payment.BasePaymentProvider.BasePaymentProvider.execute_payment
- Post with url to custom view with order id generation, PREAUTH, get hostedPage
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

Stuff to Test
-----------------
- Pagamenti normali
    - ✅ Test accettazione print corretta in caso di esito ko
    - ✅ Test accettazione print corretta in caso di esito non valido
    - process_result(): esito in pending
    - ✅ process_result(): esito in fail
    - process_result(): esito non valido
    - ✅ confirm_payment_and_capture_from_preauth(): test race condition
    - ✅ confirm_payment_and_capture_from_preauth(): confirm_payment_and_capture_from_preauth
    - confirm_payment_and_capture_from_preauth(): quota QuotaExceededException
- Runperiodic:
    - ✅ Payment in timeout
    - ✅ authorized: si testa sopra
    - recorded: conferma
    - recorded: QuotaExceededException
    - pending
    - refund o cancellato
    - non valido
- cancel_payment():
    - authorized o pending
    - recorded
    - refunded o canceled
- Race conditions:
    - confirm_payment_and_capture_from_preauth(): vista sopra
    - runperiodic: visto sopra
    - cancel_payment: visto sopra
- Extra:
    - Se il pagamento è in pending, pretix ci fa ritestare?
    - Se QuotaExceededException, va chiamato a mano payment.fail()?

Debugging\
-----------------

1. Run ``python setup.py develop``, which will install the plugin as a module, linking to the project's folder

2. Setup pretix via ``python -m pretix migrate`` and ``python -m pretix runserver`` (Be sure to be in a different folder location)

3. Configure pretix with a basic organizer and event, be sure to enable XPay both in the ``Plugins`` and ``Payment Providers`` sections of the event's settings.

4. Configure vscode's launch.json like the following:
    ::
    {
        // Use IntelliSense to learn about possible attributes.
        // Hover to view descriptions of existing attributes.
        // For more information, visit: https://go.microsoft.com/fwlink/?linkid=830387
        "version": "0.2.0",
        "configurations": [
            {
                "name": "Python Debugger: Module",
                "type": "debugpy",
                "request": "launch",
                "module": "pretix",
                "cwd": "${workspaceFolder}/../pretix_env/",
                "args": [
                    "runserver"
                ]
            }
        ]
    }
    ::

5. Press F5 or launch debug from the dedicated left panel

License
-------


Copyright 2024 Furizon Team

Released under the terms of the Apache License 2.0



.. _pretix: https://github.com/pretix/pretix
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
