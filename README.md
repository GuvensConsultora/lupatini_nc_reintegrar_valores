# NC + Reversión de medios de pago (Ventas) — Odoo 17

Módulo para Odoo 17 que agrega un botón adicional en el wizard de **Nota de crédito** (`account.move.reversal`) para:

1. **Generar la Nota de Crédito (NC)** (reversión estándar de Odoo).
2. **Revertir los medios de pago asociados** a la factura original **solo por el importe imputado a esa factura**:
   - Desimputa (elimina) las conciliaciones parciales (`account.partial.reconcile`) entre la factura y los cobros.
   - Crea un **pago de devolución** (`account.payment`) en sentido inverso (OUTBOUND).
   - Concilia la devolución contra el cobro original en la cuenta **Cuentas por Cobrar (AR)**.

> Alcance: **Ventas (Customer Invoices)** y **sin multi-moneda** (moneda compañía, típicamente ARS).

---

## Compatibilidad

- Odoo: **17.0+**
- Módulos requeridos: `account`
- Caso de uso: Facturas de cliente (`out_invoice`) **publicadas** (`posted`)

---

## Funcionalidad

### Botón agregado en el wizard de Nota de crédito
En la ventana emergente de **Nota de crédito**, se agrega el botón:

- **“Revertir y revertir pagos”**

Al hacer clic:

1. **Detecta** los cobros (`account.payment`) imputados a la factura.
2. **Toma el monto exacto** aplicado a esa factura (si un pago se repartió entre varias facturas, solo se revierte la parte aplicada a esta).
3. **Desconciliación parcial**: elimina únicamente los `account.partial.reconcile` que vinculan el cobro con la factura.
4. **Crea devolución**: un `account.payment` OUTBOUND por el mismo importe.
5. **Concilia** la devolución contra el cobro original (líneas AR).
6. **Genera la NC** con el mecanismo estándar de Odoo (respeta el `refund_method` del wizard).

---

## Instalación

1. Copiar el directorio `nc_reverse_payments` a tu addons path.
2. Actualizar lista de aplicaciones.
3. Instalar el módulo: **NC + Reversión de medios de pago (Ventas)**

---

## Configuración

### Permisos / Grupo
El botón está limitado al grupo:

- **“NC: Revertir y revertir pagos”**

Este grupo implica por defecto:
- `account.group_account_manager`

Si querés permitirlo a usuarios contables no managers, asignales el grupo o ajustá el `implied_ids`.

Archivo:
- `security/security.xml`

### Requisito de diarios
Para poder generar el pago de devolución, el **diario** usado en el cobro original debe tener configurado al menos un **método de pago OUTBOUND**.

Si el diario no tiene método OUTBOUND, el sistema mostrará error.

---

## Estructura del módulo


---

## Notas técnicas (cómo funciona por dentro)

- Se buscan líneas **receivable** de la factura (`asset_receivable`) y sus conciliaciones parciales:
  - `matched_debit_ids` / `matched_credit_ids` → `account.partial.reconcile`
- De cada parcial se identifica la línea “del otro lado”, y se toma su `move_id.payment_id` para obtener el `account.payment`.
- Se hace `unlink()` del parcial para desimputar **solo** esa parte aplicada.
- Se crea un `account.payment` **OUTBOUND** (devolución) por el importe total aplicado por cada cobro.
- Se `reconcile()` entre las líneas AR del cobro original y la devolución.
- Finalmente se llama al método estándar del wizard para generar la NC:
  - `self.reverse_moves()`

---

## Limitaciones / Fuera de alcance (por diseño actual)

- Multi-moneda: no contemplado (todo se calcula en moneda compañía).
- Pagos provenientes de extractos bancarios sin `payment_id`: no contemplado.
- Compras (`in_invoice`): no contemplado.

---

## Testing rápido recomendado

1. Crear factura de cliente por $X y publicarla.
2. Registrar un cobro parcial o total:
   - Si el cobro se comparte con otras facturas, imputar montos distintos.
3. Ir a **Nota de crédito** → usar el botón:
   - **Revertir y revertir pagos**
4. Verificar:
   - Se crea la NC.
   - La factura queda sin conciliación con los cobros originales.
   - Se crean pagos OUTBOUND por los importes aplicados.
   - La devolución queda conciliada contra el cobro original.
   - No se afecta la imputación del cobro a otras facturas (si existían).

---

## Changelog

### 17.0.1.0.0
- Botón adicional en wizard de Nota de crédito para generar NC + reversión de cobros imputados (ventas, sin multi).

---

## Autor / Soporte
Ajustable a reglas específicas (diario fijo de devoluciones, incluir extractos, etc.) según necesidad del cliente.
