# Billing helpers

`pricing.compute_total(items, tax_rate=0.0)` returns the total of a list of
`(name, unit_price, quantity)` tuples.

`invoice.make_invoice` and `report.revenue` both build on `compute_total`.
