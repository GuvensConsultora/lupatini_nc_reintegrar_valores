from odoo import fields, models, _
from odoo.exceptions import UserError

class AccountMoveReversal(models.TransientModel):
    _inherit = "account.move.reversal"

    # ... (helpers _get_receivable_lines / _collect_allocations / _pick_outbound_method_line)

    def action_reverse_and_refund_payments(self):
        self.ensure_one()

        invoices = self.move_ids
        if not invoices:
            raise UserError(_("No hay factura para revertir."))

        bad = invoices.filtered(lambda m: m.move_type != "out_invoice" or m.state != "posted")
        if bad:
            raise UserError(_("Este botón es solo para facturas de cliente publicadas (ventas)."))

        # 1) Recolectar parciales factura<->pagos
        inv_allocs = {inv.id: self._collect_allocations(inv) for inv in invoices}

        # === LOG PREVIO (antes de unlink) ===
        log_pre = {}
        for inv in invoices:
            rows = []
            for a in inv_allocs[inv.id]:
                pay = a["payment"]
                rows.append({
                    "payment_name": pay.name or _("(sin nombre)"),
                    "payment_move": pay.move_id.name or _("(sin asiento)"),
                    "journal": pay.journal_id.display_name,
                    "amount": a["amount_company"],
                })
            log_pre[inv.id] = rows

        # 2) Desconciliar SOLO lo aplicado a esta factura
        for inv in invoices:
            for a in inv_allocs[inv.id]:
                a["partial"].unlink()

        # 3) Generar NC con wizard estándar (en tu Odoo 17 suele ser refund_moves)
        action = self.refund_moves() if hasattr(self, "refund_moves") else self.reverse_moves()

        # Buscar NCs creadas por esta operación (por reversed_entry_id)
        credit_notes = self.env["account.move"].search([
            ("reversed_entry_id", "in", invoices.ids),
        ])

        # 4) Crear devoluciones y conciliar contra cobros originales
        Payment = self.env["account.payment"]
        refund_date = self.date or fields.Date.context_today(self)

        # === LOG POST (devoluciones creadas) ===
        refunds_log = {inv.id: [] for inv in invoices}

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

                refunds_log[inv.id].append({
                    "refund_payment_name": refund.name or _("(sin nombre)"),
                    "refund_move": refund.move_id.name or _("(sin asiento)"),
                    "journal": refund.journal_id.display_name,
                    "amount": amount,
                })

# 5) Postear en chatter por factura Y por cada NC
for inv in invoices:
    inv_cns = credit_notes.filtered(lambda m: m.reversed_entry_id.id == inv.id)

    body = f"""
    <div>
      <p><b>NC + Reversión de medios de pago</b></p>

      <p><b>Factura origen:</b> {inv.name or ''}</p>

      <p><b>Notas de crédito generadas:</b></p>
      <ul>
        {''.join([f"<li><b>{cn.name}</b></li>" for cn in inv_cns]) or "<li>(No se detectaron NCs)</li>"}
      </ul>

      <p><b>Cobros desimputados (según lo aplicado a esta factura):</b></p>
      <ul>
        {''.join([f"<li>Pago: <b>{r['payment_name']}</b> — Asiento: <b>{r['payment_move']}</b> — Diario: {r['journal']} — Importe: {r['amount']}</li>"
                  for r in log_pre.get(inv.id, [])]) or "<li>(Sin cobros imputados)</li>"}
      </ul>

      <p><b>Devoluciones creadas:</b></p>
      <ul>
        {''.join([f"<li>Devolución: <b>{r['refund_payment_name']}</b> — Asiento: <b>{r['refund_move']}</b> — Diario: {r['journal']} — Importe: {r['amount']}</li>"
                  for r in refunds_log.get(inv.id, [])]) or "<li>(No se generaron devoluciones)</li>"}
      </ul>
    </div>
    """

    # En la factura
    inv.message_post(
        body=body,
        subject=_("NC y reversión de pagos ejecutada"),
    )

    # En cada NC creada
    for cn in inv_cns:
        cn.message_post(
            body=body,
            subject=_("NC y reversión de pagos ejecutada"),
        )
