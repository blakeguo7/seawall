"""Invoices."""

from pricing import calc_total


def make_invoice(customer, items, tax_rate=0.2):
    return {
        "customer": customer,
        "lines": len(items),
        "total": calc_total(items, tax_rate),
    }
