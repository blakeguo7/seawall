"""Invoices."""

from pricing import compute_total


def make_invoice(customer, items, tax_rate=0.2):
    return {
        "customer": customer,
        "lines": len(items),
        "total": compute_total(items, tax_rate),
    }
