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
        """
        Líneas AR (cuentas por cobrar).
        NO filtramos por display_type porque la AR suele ser 'payment_term'.
        """
        return move.line_ids.filtered(lambda l: l.account_id.account_type == "asset_receivable")

    def _collect_allocations(self, invoice_move):
        """
        Lee imputaciones (account.partial.reconcile) entre factura y cobros (account.payment).
        Devuelve una lista con:
        - partial: parcial que une factura <-> pago
        - payment: account.payment (si existe)
        - payment_counterpart_line: línea AR del pago que concilió contra la factura
        - amount_company: monto aplicado a esta factura (moneda compañía, sin multi)
        """
        allocations = []
        inv_ar_lines = self._get_receivable_lines(invoice_move)

        for inv_line in inv_ar_lines:
            partials = inv_line.matched_debit_ids | inv_line.matched_credit_ids
            for partial in partials:
                other_line = partial.debit_move_id if partial.credit_move_id == inv_line else partial.credit_move_id
                pay_move = other_line.move_id
                payment = pay_move.payment_id
                if not payment:
                    # Si no hay account.payment (ej: statement/manual), no lo tratamos como "recibo".
                    continue

                # Ventas (customer) y cobros inbound
                if payment.partner_type != "customer" or payment.payment_type != "inbound":
                    continue

                allocations.append(
                    {
                        "partial": partial,
                        "payment": payment,
                        "payment_counterpart_line": other_line,
                        "amount_company": partial.amount,
                    }
                )

        return allocations

    def _pick_outbound_method_line(self, journal, fallback_line=None):
        """Elige un método OUTBOUND del diario (si el fallback sirve, lo usa)."""
        if fallback_line and getattr(fallback_line, "payment_type", None) == "outbound":
            return fallback_line
        return journal.payment_method_line_ids.filtered(lambda l: l.payment_type == "outbound")[:1]

    def _link(self, rec, label=None):
        """Link estable en chatter."""
        if not rec:
            return ""
        txt = html_escape(label or rec.display_name or "")
        return f'<a href="/web#id={rec.id}&model={rec._name}&view_type=form">{txt}</a>'

    def _amt(self, amount, currency):
        return html_escape(formatLang(self.env, amount, currency_obj=currency))

    def _reconcile_by_account(self, lines_a, lines_b):
        """
        Concilia líneas por cuenta para evitar mezclar cuentas distintas.
        """
        all_lines = (lines_a | lines_b).filtered(lambda l: not l.reconciled)
        if not all_lines:
            return

        for acc in all_lines.mapped("account_id"):
            to_rec = all_lines.filtered(lambda l: l.account_id == acc and not l.reconciled)
            if len(to_rec) >= 2:
                to_rec.reconcile()

    # ------------------------------------------------------------
    # Action
    # ------------------------------------------------------------

    def action_reverse_and_refund_payments(self):
        """
        Flujo:
        1) Leer cobros aplicados a la factura (allocations).
        2) Deslinkear pagos desde la factura: unlink de account.partial.reconcile (solo lo aplicado a esa factura).
        3) Crear Nota de Crédito (wizard estándar) -> queda linkeada por reversed_entry_id.
        4) Conciliar Factura ↔ NC (en AR).
        5) Por cada cobro aplicado: crear un pago outbound (reversión) y conciliarlo contra el cobro original.
        6) Registrar TODO en chatter (factura y NC) SIEMPRE.
        """
        self.ensure_one()

        invoices = self.move_ids
        if not invoices:
            raise UserError(_("No hay factura para procesar."))

        bad = invoices.filtered(lambda m: m.move_type != "out_invoice" or m.state != "posted")
        if bad:
            raise UserError(_("Este botón es solo para facturas de cliente publicadas (ventas)."))

        # 1) Leer imputaciones antes de tocar nada
        inv_allocs = {inv.id: self._collect_allocations(inv) for inv in invoices}

        # Log previo (pagos/cobros detectados)
        pre_logs = {}
        for inv in invoices:
            rows = []
            for a in inv_allocs[inv.id]:
                pay = a["payment"]
                rows.append(
                    {
                        "payment": pay,
                        "payment_move": pay.move_id,
                        "journal": pay.journal_id.display_name,
                        "amount": a["amount_company"],
                        "partial_id": a["partial"].id,
                        "acc_id": a["payment_counterpart_line"].account_id.id,
                    }
                )
            pre_logs[inv.id] = rows

        # 2) Deslinkear pagos desde la factura (romper conciliación factura↔cobros)
        for inv in invoices:
            for a in inv_allocs[inv.id]:
                a["partial"].unlink()

        # 3) Crear NC con wizard estándar
        if hasattr(self, "refund_moves"):
            action = self.refund_moves()
        elif hasattr(self, "reverse_moves"):
            action = self.reverse_moves()
        else:
            raise UserError(_("No se encontró el método estándar para generar la nota de crédito en este wizard."))

        # Buscar NCs creadas (linkeadas por reversed_entry_id)
        credit_notes = self.env["account.move"].search([("reversed_entry_id", "in", invoices.ids)])

        # 4) Conciliar Factura ↔ NC (AR)
        cn_by_inv = {}
        for inv in invoices:
            inv_cns = credit_notes.filtered(lambda m: m.reversed_entry_id.id == inv.id)
            cn_by_inv[inv.id] = inv_cns

            # Posteamos todas (si hubiera más de una), conciliamos por cada una
            for cn in inv_cns:
                if cn.state != "posted":
                    cn.action_post()

                inv_ar = self._get_receivable_lines(inv)
                cn_ar = self._get_receivable_lines(cn)
                self._reconcile_by_account(inv_ar, cn_ar)

        # 5) Crear reversiones (outbound) por cada cobro y conciliarlas contra el cobro original
        Payment = self.env["account.payment"]
        refund_date = self.date or fields.Date.context_today(self)

        refunds_log_by_inv = {inv.id: [] for inv in invoices}

        for inv in invoices:
            allocs = inv_allocs[inv.id]
            if not allocs:
                continue

            # Agrupar por pago original: 1 reversión por cada cobro, sumando lo aplicado a esta factura
            grouped = {}
            for a in allocs:
                pay = a["payment"]
                grouped.setdefault(
                    pay.id,
                    {
                        "orig_payment": pay,
                        "amount": 0.0,
                        "acc_id": a["payment_counterpart_line"].account_id.id,
                        "journal": pay.journal_id,
                        "fallback_method_line": pay.payment_method_line_id,
                    },
                )
                grouped[pay.id]["amount"] += a["amount_company"]

            for g in grouped.values():
                orig_pay = g["orig_payment"]
                amount = g["amount"]
                acc_id = g["acc_id"]

                pm_line = self._pick_outbound_method_line(g["journal"], fallback_line=g["fallback_method_line"])
                if not pm_line:
                    raise UserError(
                        _("El diario %s no tiene un método de pago OUTBOUND configurado para revertir.")
                        % g["journal"].display_name
                    )

                # Asociamos explícitamente la reversión a la(s) NC(s) por referencia
                inv_cns = cn_by_inv.get(inv.id, self.env["account.move"])
                cn_names = ", ".join(inv_cns.mapped("name")) if inv_cns else ""
                ref_txt = _("Reversión %s / NC %s / Factura %s") % (orig_pay.name or "", cn_names, inv.name or "")

                refund = Payment.create(
                    {
                        "payment_type": "outbound",
                        "partner_type": "customer",
                        "partner_id": orig_pay.partner_id.id,
                        "journal_id": g["journal"].id,
                        "payment_method_line_id": pm_line.id,
                        "date": refund_date,
                        "amount": amount,
                        "currency_id": orig_pay.company_currency_id.id,  # sin multi
                        "ref": ref_txt,
                    }
                )
                refund.action_post()

                # Conciliar cobro original ↔ reversión (en AR)
                orig_ar = orig_pay.move_id.line_ids.filtered(
                    lambda l: not l.display_type and l.account_id.id == acc_id and not l.reconciled
                )
                ref_ar = refund.move_id.line_ids.filtered(
                    lambda l: not l.display_type and l.account_id.id == acc_id and not l.reconciled
                )
                (orig_ar | ref_ar).reconcile()

                refunds_log_by_inv[inv.id].append(
                    {
                        "orig_payment": orig_pay,
                        "orig_move": orig_pay.move_id,
                        "refund_payment": refund,
                        "refund_move": refund.move_id,
                        "journal": refund.journal_id.display_name,
                        "amount": amount,
                    }
                )

        # 6) Chatter SIEMPRE (factura + NC)
        for inv in invoices:
            cur = inv.company_currency_id
            inv_link = self._link(inv, inv.name or inv.display_name)

            inv_cns = cn_by_inv.get(inv.id, self.env["account.move"])
            cn_items = []
            for cn in inv_cns:
                cn_items.append(f"<li>NC: <b>{self._link(cn, cn.name or cn.display_name)}</b></li>")
            cns_html = "".join(cn_items) or "<li>(No se detectaron NCs creadas)</li>"

            pay_items = []
            for r in pre_logs.get(inv.id, []):
                pay = r["payment"]
                pay_items.append(
                    "<li>"
                    f"Pago original: <b>{self._link(pay, pay.name or pay.display_name)}</b>"
                    f" — Asiento: <b>{self._link(r['payment_move'], r['payment_move'].name)}</b>"
                    f" — Diario: {html_escape(r['journal'])}"
                    f" — Importe aplicado a esta factura: {self._amt(r['amount'], cur)}"
                    f" — Partial unlink: {html_escape(str(r['partial_id']))}"
                    "</li>"
                )
            pays_html = "".join(pay_items) or "<li>(Sin cobros detectados con account.payment)</li>"

            ref_items = []
            for r in refunds_log_by_inv.get(inv.id, []):
                ref_items.append(
                    "<li>"
                    f"Reversión creada: <b>{self._link(r['refund_payment'], r['refund_payment'].name or r['refund_payment'].display_name)}</b>"
                    f" — Asiento: <b>{self._link(r['refund_move'], r['refund_move'].name)}</b>"
                    f" — Conciliada contra cobro: <b>{self._link(r['orig_payment'], r['orig_payment'].name or r['orig_payment'].display_name)}</b>"
                    f" — Importe: {self._amt(r['amount'], cur)}"
                    f" — Diario: {html_escape(r['journal'])}"
                    "</li>"
                )
            refs_html = "".join(ref_items) or "<li>(No se crearon reversiones)</li>"

            body = f"""
            <div>
              <p><b>NC + reversión de cobros</b></p>

              <p><b>Factura origen:</b> {inv_link}</p>

              <p><b>Notas de crédito generadas (linkeadas por reversed_entry_id):</b></p>
              <ul>{cns_html}</ul>

              <p><b>Cobros deslinkeados desde la factura (parciales eliminados):</b></p>
              <ul>{pays_html}</ul>

              <p><b>Reversiones de cobros (outbound) y conciliación contra cobros originales:</b></p>
              <ul>{refs_html}</ul>
            </div>
            """

            subject = _("NC + reversión de cobros ejecutada")

            # Post en factura
            inv.message_post(
                body=Markup(body),
                subject=subject,
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

            # Post también en cada NC
            for cn in inv_cns:
                cn.message_post(
                    body=Markup(body),
                    subject=subject,
                    message_type="comment",
                    subtype_xmlid="mail.mt_note",
                )

        return action
