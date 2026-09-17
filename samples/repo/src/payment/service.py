"""Payment processing."""

from decimal import Decimal

DEFAULT_CURRENCY = "USD"


class PaymentService:
    """Charges orders."""

    def __init__(self, gateway):
        self.gateway = gateway

    def capture(self, order, amount: Decimal) -> bool:
        """Capture an authorised payment."""
        return self.gateway.charge(order.id, amount)


def process_payment(order, amount: Decimal, currency: str = DEFAULT_CURRENCY) -> bool:
    """Process a single payment for an order."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    service = PaymentService(order.gateway)
    return service.capture(order, amount)


def refund(order, amount: Decimal) -> bool:
    """Refund a captured payment."""
    return process_payment(order, -amount)
