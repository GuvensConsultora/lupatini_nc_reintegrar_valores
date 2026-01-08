# -*- coding: utf-8 -*-
{
    "name": "NC + Reversión de medios de pago (Ventas)",
    "version": "17.0.1.0.0",
    "category": "Accounting",
    "summary": "Agrega botón en Nota de crédito para generar NC y revertir cobros imputados (ventas, sin multi).",
    "depends": ["account"],
    "data": [
        # "security/security.xml",
        "views/account_move_reversal_views.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
