# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    # Por qué: Marcar facturas/NC que ya fueron procesadas con reversión de cobros
    # Patrón: Campo técnico para controlar visibilidad de botones
    reversed_with_payments = fields.Boolean(
        string="Reversión con cobros ejecutada",
        default=False,
        readonly=True,
        copy=False,
        help="Indica que esta factura/NC fue procesada con el botón 'Revertir y revertir pagos'",
    )
