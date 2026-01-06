# -*- coding: utf-8 -*-
from odoo import fields, models, _
from odoo.exceptions import UserError


class AccountMoveReversal(models.TransientModel):
    _inherit = "account.move.reversal"

    def _get_receivable_lines(self, move):
        """Líneas AR de la factura."""
        return move.line_ids.filtered(lambda l: (
            not l.display_type
            and l.account_id.account_type == "asset_receivable"
        ))

    def _collect_allocations(self, invoice_move):
        """
        Recolecta imputaciones (account.partial.reconcile) entre factura y cobros (account.payment).
        Devuelve lista de dicts con:
          - partial
          - payment
          - payment_counterpart_line (línea AR del pago)
          - amount_company (monto imputado en moneda compañía)
        """
        allocations = []
        inv_lines = self._get_receivable_lines(invoice_move)

        for inv_line in inv_lines:
            partials = inv_line.matched_debit_ids | inv_line.matched_credit_ids
            for partial in partials:
                other_line = partial.debit_move_id if partial.credit_move_id == inv_line else partial.credit_move_id
                payment_move = other_line.move_id
                payment = payment_move.payment_id
                if not payment:
                    # Si querés incluir bank statement sin payment_id, se agrega acá.
                    continue

                # Ventas: cobros de cliente
                if payment.payment_type != "inbound" or payment.partner_type != "customer":
                    continue

                allocations.append({
                    "partial": partial,
                    "payment": payment,
                    "payment_counterpart_line": other_line,
                    "amount_company": partial.amount,  # ARS (moneda compañía)
                })

        return allocations

    def _pick_outbound_method_line(self, journal, fallback_line=None):
        """
        Elige método outbound para el diario.
        Intenta usar el mismo método del cobro si tiene outbound; si no, toma el primero outbound del diario.
        """
        if fallback_line and getattr(fallback_line, "payment_type", None) == "outbound":
            return fallback_line
        return journal.payment_method_line_ids.filtered(lambda l: l.payment_type == "outbound")[:1]

    def action_reverse_and_refund_payments(self):
        """
        1) Desimputa factura<->cobros solo por lo aplicado a esa factura (unlink parciales).
        2) Genera NC (reverse_moves estándar).
        3) Crea pagos de devolución outbound por importes imputados y concilia contra el cobro original (AR).
        """
        self.ensure_one()

        invoices = self.move_ids
        if not invoices:
            raise UserError(_("No hay factura para revertir."))

        bad = invoices.filtered(lambda m: m.move_type != "out_invoice" or m.state != "posted")
        if bad:
            raise UserError(_("Este botón es solo para facturas de cliente publicadas (ventas)."))

        # 1) Recolectar parciales factura<->pagos
        inv_allocs = {inv.id: self._collect_allocations(inv) for inv in invoices}

        # 2) Desconciliar SOLO lo aplicado a esta factura
        for inv in invoices:
            for a in inv_allocs[inv.id]:
                a["partial"].unlink()

        # 3) Generar NC con wizard estándar (respeta refund_method del wizard)
        action = self.reverse_moves()

        # 4) Crear devoluciones y conciliar contra cobros originales
        Payment = self.env["account.payment"]
        refund_date = self.date or fields.Date.context_today(self)

        for inv in invoices:
            allocs = inv_allocs[inv.id]
            if not allocs:
                continue

            # Agrupar por payment: 1 devolución por cada cobro (sumando parciales)
            grouped = {}
            for a in allocs:
                pay = a["payment"]
                grouped.setdefault(pay.id, {
                    "payment": pay,
                    "amount": 0.0,
                    "account_id": a["payment_counterpart_line"].account_id.id,
                })
                grouped[pay.id]["amount"] += a["amount_company"]

            for g in grouped.values():
                pay = g["payment"]
                amount = g["amount"]

                pm_line = self._pick_outbound_method_line(pay.journal_id, fallback_line=pay.payment_method_line_id)
                if not pm_line:
                    raise UserError(_(
                        "El diario %s no tiene un método de pago OUTBOUND configurado para devolver."
                    ) % pay.journal_id.display_name)

                refund = Payment.create({
                    "payment_type": "outbound",
                    "partner_type": "customer",
                    "partner_id": pay.partner_id.id,
                    "journal_id": pay.journal_id.id,
                    "payment_method_line_id": pm_line.id,
                    "date": refund_date,
                    "amount": amount,
                    "currency_id": pay.company_currency_id.id,
                    "ref": _("Reversión cobro %s (Factura %s)") % (pay.name or "", inv.name or ""),
                })
                refund.action_post()

                # Conciliar AR (cuenta receivable): cobro original vs devolución
                acc_id = g["account_id"]
                orig_ar = pay.move_id.line_ids.filtered(lambda l: not l.display_type and l.account_id.id == acc_id and not l.reconciled)
                ref_ar = refund.move_id.line_ids.filtered(lambda l: not l.display_type and l.account_id.id == acc_id and not l.reconciled)
                (orig_ar | ref_ar).reconcile()

        return action
