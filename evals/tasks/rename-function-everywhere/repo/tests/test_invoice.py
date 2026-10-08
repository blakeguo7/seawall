from invoice import make_invoice


def test_invoice_total():
    invoice = make_invoice("Ada", [("pen", 10, 1)], tax_rate=0.1)
    assert invoice["total"] == 11.0
