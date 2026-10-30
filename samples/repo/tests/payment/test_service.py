from src.payment.service import process_payment


def test_process_payment():
    assert process_payment(_order(), 10) is True


def test_process_payment_rejects_negative():
    pass


def _order():
    return None
