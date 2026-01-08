# -*- coding: utf-8 -*-
from markupsafe import Markup

from odoo import fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang, html_escape


class AccountMoveReversal(models.TransientModel):
    _inherit = "account.move.reversal"

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    def _get_receivable_lines(self, move):
        """Líneas AR (cuentas por cobrar) de la factura."""
        return move.line_ids.filtered(
            lambda l: not l.display_type and l.account_id.account_type == "asset_receivable"
        )

    def _collect_allocations(self, invoice_move):
        """
        Recolecta imputaciones (account.partial.reconcile) entre factura y cobros (account.payment).
        Solo ventas (customer) y cobros inbound.
        """
        allocations = []
        inv_lines = self._get_receivable_lines(invoice_move)

        for inv_line in inv_lines:
            partials = inv_line.matched_debit_ids | inv_line.matched_credit_ids
            for partial in partials:
                other_line = partial.debit_move_id if partial.credit_move_id == inv_line else partial.credit_move_id
                pay_move = other_line.move_id
                payment = pay_move.payment_id
                if not payment:
                    continue

                if payment.partner_type != "customer" or payment.payment_type != "inbound":
                    continue

                allocations.append(
                    {
                        "partial": partial,
                        "payment": payment,
                        "payment_counterpart_line": other_line,
                        "amount_company": partial.amount,  # moneda compañía (sin multi)
                    }
                )

        return allocations

    def _pick_outbound_method_line(self, journal, fallback_line=None):
        """Elige un método OUTBOUND del diario (si el fallback sirve, lo usa)."""
        if fallback_line and getattr(fallback_line, "payment_type", None) == "outbound":
            return fallback_line
        return journal.payment_method_line_ids.filtered(lambda l: l.payment_type == "outbound")[:1]

    def _link(self, rec, label=None):
        """
        Link más seguro: URL /web#id=... (no depende de data-oe-model/id del chatter).
        """
        if not rec:
            return ""
        txt = html_escape(label or rec.display_name or "")
        return f'<a href="/web#id={rec.id}&model={rec._name}&view_type=form">{txt}</a>'

    def _amt(self, amount, currency):
        return html_escape(formatLang(self.env, amount, currency_obj=currency))

    # ------------------------------------------------------------
    # Action
    # ------------------------------------------------------------

    def action_reverse_and_refund_payments(self):
        """
        Ventas / sin multi:
        1) Desimputa (unlink parciales) solo lo aplicado a esta factura.
        2) Genera NC con wizard estándar (refund_moves / reverse_moves fallback).
        3) Crea pagos OUTBOUND (devolución) por los importes aplicados y concilia contra cobros originales (AR).
        4) Loguea en chatter (factura y NC) con HTML renderizado + links clickeables.
        """
        self.ensure_one()

        invoices = self.move_ids
        if not invoices:
            raise UserError(_("No hay factura para revertir."))

        bad = invoices.filtered(lambda m: m.move_type != "out_invoice" or m.state != "posted")
        if bad:
            raise UserError(_("Este botón es solo para facturas de cliente publicadas (ventas)."))

        # 1) Recolectar imputaciones factura <-> cobros
        inv_allocs = {inv.id: self._collect_allocations(inv) for inv in invoices}

        # Log previo (antes de unlink) guardando records para links
        log_pre = {}
        for inv in invoices:
            rows = []
            for a in inv_allocs[inv.id]:
                pay = a["payment"]
                rows.append(
                    {
                        "payment_rec": pay,
                        "payment_move_rec": pay.move_id,
                        "payment_name": pay.name or _("(sin nombre)"),
                        "payment_move": pay.move_id.name or _("(sin asiento)"),
                        "journal": pay.journal_id.display_name,
                        "amount": a["amount_company"],
                    }
                )
            log_pre[inv.id] = rows

        # 2) Desimputar solo lo aplicado a ESTA factura (parciales)
        for inv in invoices:
            for a in inv_allocs[inv.id]:
                a["partial"].unlink()

        # 3) Generar NC con wizard estándar
        if hasattr(self, "refund_moves"):
            action = self.refund_moves()
        elif hasattr(self, "reverse_moves"):
            action = self.reverse_moves()
        else:
            raise UserError(_("No se encontró el método estándar para generar la nota de crédito en este wizard."))

        # NC(s) creadas (por reversed_entry_id)
        credit_notes = self.env["account.move"].search([("reversed_entry_id", "in", invoices.ids)])

        # 4) Crear devoluciones y conciliar contra cobros originales
        Payment = self.env["account.payment"]
        refund_date = self.date or fields.Date.context_today(self)

        refunds_log = {inv.id: [] for inv in invoices}

        for inv in invoices:
            allocs = inv_allocs[inv.id]
            if not allocs:
                continue

            # Agrupar por payment: 1 devolución por cobro, sumando parciales
            grouped = {}
            for a in allocs:
                pay = a["payment"]
                grouped.setdefault(
                    pay.id,
                    {
                        "payment": pay,
                        "amount": 0.0,
                        "account_id": a["payment_counterpart_line"].account_id.id,
                    },
                )
                grouped[pay.id]["amount"] += a["amount_company"]

            for g in grouped.values():
                pay = g["payment"]
                amount = g["amount"]

                pm_line = self._pick_outbound_method_line(pay.journal_id, fallback_line=pay.payment_method_line_id)
                if not pm_line:
                    raise UserError(
                        _("El diario %s no tiene un método de pago OUTBOUND configurado para devolver.")
                        % pay.journal_id.display_name
                    )

                refund = Payment.create(
                    {
                        "payment_type": "outbound",
                        "partner_type": "customer",
                        "partner_id": pay.partner_id.id,
                        "journal_id": pay.journal_id.id,
                        "payment_method_line_id": pm_line.id,
                        "date": refund_date,
                        "amount": amount,
                        "currency_id": pay.company_currency_id.id,
                        "ref": _("Reversión cobro %s (Factura %s)") % (pay.name or "", inv.name or ""),
                    }
                )
                refund.action_post()

                # Conciliar AR: cobro original vs devolución
                acc_id = g["account_id"]
                orig_ar = pay.move_id.line_ids.filtered(
                    lambda l: not l.display_type and l.account_id.id == acc_id and not l.reconciled
                )
                ref_ar = refund.move_id.line_ids.filtered(
                    lambda l: not l.display_type and l.account_id.id == acc_id and not l.reconciled
                )
                (orig_ar | ref_ar).reconcile()

                refunds_log[inv.id].append(
                    {
                        "refund_payment_rec": refund,
                        "refund_move_rec": refund.move_id,
                        "refund_payment_name": refund.name or _("(sin nombre)"),
                        "refund_move": refund.move_id.name or _("(sin asiento)"),
                        "journal": refund.journal_id.display_name,
                        "amount": amount,
                    }
                )

        # 5) Postear en chatter (Factura y NCs) con HTML renderizado + links seguros
        subject = _("NC y reversión de pagos ejecutada")

        for inv in invoices:
            inv_cns = credit_notes.filtered(lambda m: m.reversed_entry_id.id == inv.id)
            cur = inv.company_currency_id

            inv_link = self._link(inv, inv.name or inv.display_name)

            cn_items = []
            for cn in inv_cns:
                cn_items.append(f"<li>NC: <b>{self._link(cn, cn.name or cn.display_name)}</b></li>")
            cn_html = "".join(cn_items) or "<li>(No se detectaron NCs)</li>"

            pay_items = []
            for r in log_pre.get(inv.id, []):
                pay_items.append(
                    "<li>"
                    f"Pago: <b>{self._link(r['payment_rec'], r['payment_name'])}</b>"
                    f" — Asiento: <b>{self._link(r['payment_move_rec'], r['payment_move'])}</b>"
                    f" — Diario: {html_escape(r['journal'])}"
                    f" — Importe aplicado: {self._amt(r['amount'], cur)}"
                    "</li>"
                )
            pay_html = "".join(pay_items) or "<li>(Sin cobros imputados)</li>"

            ref_items = []
            for r in refunds_log.get(inv.id, []):
                ref_items.append(
                    "<li>"
                    f"Devolución: <b>{self._link(r['refund_payment_rec'], r['refund_payment_name'])}</b>"
                    f" — Asiento: <b>{self._link(r['refund_move_rec'], r['refund_move'])}</b>"
                    f" — Diario: {html_escape(r['journal'])}"
                    f" — Importe: {self._amt(r['amount'], cur)}"
                    "</li>"
                )
            ref_html = "".join(ref_items) or "<li>(No se generaron devoluciones)</li>"

            body = f"""
            <div>
              <p><b>NC + Reversión de medios de pago</b></p>
              <p><b>Factura origen:</b> {inv_link}</p>

              <p><b>Notas de crédito generadas:</b></p>
              <ul>{cn_html}</ul>

              <p><b>Cobros desimputados (solo lo aplicado a esta factura):</b></p>
              <ul>{pay_html}</ul>

              <p><b>Devoluciones creadas:</b></p>
              <ul>{ref_html}</ul>
            </div>
            """

            # Factura
            inv.message_post(
                body=Markup(body),
                subject=subject,
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

            # Cada NC
            for cn in inv_cns:
                cn.message_post(
                    body=Markup(body),
                    subject=subject,
                    message_type="comment",
                    subtype_xmlid="mail.mt_note",
                )

        return action

