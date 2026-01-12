# -*- coding: utf-8 -*-
from markupsafe import Markup

from odoo import fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang, html_escape


class AccountMoveReversal(models.TransientModel):
    _inherit = "account.move.reversal"

    # ------------------------------------------------------------
    # Helpers (solo para levantar info y formatear)
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
        NO toca nada: solo devuelve info.
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
                    # Si no existe payment_id, no lo reportamos como "recibo" (solo como parcial).
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

    def _link(self, rec, label=None):
        """Link estable en chatter (no depende de data-oe-model)."""
        if not rec:
            return ""
        txt = html_escape(label or rec.display_name or "")
        return f'<a href="/web#id={rec.id}&model={rec._name}&view_type=form">{txt}</a>'

    def _amt(self, amount, currency):
        return html_escape(formatLang(self.env, amount, currency_obj=currency))

    # ------------------------------------------------------------
    # Action (SOLO LEVANTAR INFO + POSTEAR EN CHATTER)
    # ------------------------------------------------------------

    def action_reverse_and_refund_payments(self):
        """
        SOLO INFO:
        - No genera NC
        - No rompe conciliación
        - No crea devoluciones
        Solo deja en chatter un análisis legible de:
        - Factura
        - Líneas AR
        - Parciales (account.partial.reconcile) que la vinculan con pagos
        - Pagos/recibos detectados (account.payment) y su asiento
        """
        self.ensure_one()

        invoices = self.move_ids
        if not invoices:
            raise UserError(_("No hay factura para analizar."))

        bad = invoices.filtered(lambda m: m.move_type != "out_invoice" or m.state != "posted")
        if bad:
            raise UserError(_("Este botón es solo para facturas de cliente publicadas (ventas)."))

        # Levantamos NC existentes (si las hubiera) SOLO para replicar el mismo análisis (no crea nada)
        credit_notes = self.env["account.move"].search([("reversed_entry_id", "in", invoices.ids)])

        # Por cada factura: levantar info y postear
        for inv in invoices:
            cur = inv.company_currency_id

            # 1) Líneas AR y parciales (aunque no haya payment_id)
            ar_lines = self._get_receivable_lines(inv)
            partials = ar_lines.matched_debit_ids | ar_lines.matched_credit_ids

            # 2) Pagos detectables (solo donde existe payment_id)
            allocs = self._collect_allocations(inv)

            # 3) Construimos resumen de pagos agrupados (solo informativo)
            payments_summary = {}
            for a in allocs:
                pay = a["payment"]
                payments_summary.setdefault(pay.id, {"payment": pay, "amount": 0.0})
                payments_summary[pay.id]["amount"] += a["amount_company"]

            # -------------------------
            # HTML legible para chatter
            # -------------------------

            inv_link = self._link(inv, inv.name or inv.display_name)

            # Sección AR
            ar_items = []
            for l in ar_lines:
                ar_items.append(
                    "<li>"
                    f"Line {l.id} — Cuenta: {html_escape(l.account_id.display_name)}"
                    f" — Débito: {self._amt(l.debit, cur)}"
                    f" — Crédito: {self._amt(l.credit, cur)}"
                    f" — Residual: {self._amt(l.amount_residual, cur)}"
                    f" — Reconciled: {html_escape(str(bool(l.reconciled)))}"
                    "</li>"
                )
            ar_html = "".join(ar_items) or "<li>(Sin líneas AR)</li>"

            # Sección parciales (mostrar todos, aunque no tengan payment_id)
            partial_items = []
            for p in partials:
                d_move = p.debit_move_id.move_id
                c_move = p.credit_move_id.move_id
                partial_items.append(
                    "<li>"
                    f"Partial {p.id}"
                    f" — Amount: {self._amt(p.amount, cur)}"
                    f" — Debit Move: {self._link(d_move, d_move.name)}"
                    f" — Credit Move: {self._link(c_move, c_move.name)}"
                    "</li>"
                )
            partials_html = "".join(partial_items) or "<li>(Sin parciales / sin conciliación)</li>"

            # Sección pagos detectados (solo donde payment_id existe)
            pay_items = []
            for pid, data in payments_summary.items():
                pay = data["payment"]
                amt = data["amount"]
                pay_items.append(
                    "<li>"
                    f"Pago: <b>{self._link(pay, pay.name or pay.display_name)}</b>"
                    f" — Asiento: <b>{self._link(pay.move_id, pay.move_id.name)}</b>"
                    f" — Diario: {html_escape(pay.journal_id.display_name)}"
                    f" — Importe aplicado a esta factura: {self._amt(amt, cur)}"
                    "</li>"
                )
            pays_html = "".join(pay_items) or "<li>(No se detectaron pagos con payment_id)</li>"

            body = f"""
            <div>
              <p><b>ANÁLISIS (solo lectura) — Factura y cobros vinculados</b></p>

              <p><b>Factura:</b> {inv_link}</p>
              <p>
                <b>Partner:</b> {html_escape(inv.partner_id.display_name)}<br/>
                <b>Estado:</b> {html_escape(inv.state)} — <b>Tipo:</b> {html_escape(inv.move_type)}<br/>
                <b>Total:</b> {self._amt(inv.amount_total, cur)} — <b>Residual:</b> {self._amt(inv.amount_residual, cur)} — <b>Payment state:</b> {html_escape(inv.payment_state)}
              </p>

              <p><b>Líneas AR (asset_receivable):</b></p>
              <ul>{ar_html}</ul>

              <p><b>Conciliaciones parciales (account.partial.reconcile):</b></p>
              <ul>{partials_html}</ul>

              <p><b>Pagos/Recibos detectados (account.payment con payment_id):</b></p>
              <ul>{pays_html}</ul>
            </div>
            """

            inv.message_post(
                body=Markup(body),
                subject=_("ANÁLISIS cobros vinculados (solo lectura)"),
                message_type="comment",
                subtype_xmlid="mail.mt_note",
            )

            # Replicamos el mismo análisis en NC existentes (si ya existen)
            inv_cns = credit_notes.filtered(lambda m: m.reversed_entry_id.id == inv.id)
            for cn in inv_cns:
                cn.message_post(
                    body=Markup(body),
                    subject=_("ANÁLISIS cobros vinculados (solo lectura)"),
                    message_type="comment",
                    subtype_xmlid="mail.mt_note",
                )

        # Cerrar wizard sin hacer acciones contables
        return {"type": "ir.actions.act_window_close"}
