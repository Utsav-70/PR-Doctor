"""Client-side retry wrapper."""

from src.payment.service import process_payment


def retry_payment(order, amount, attempts: int = 3) -> bool:
    for _ in range(attempts):
        if process_payment(order, amount):
            return True
    return False


# A string and a comment that mention process_payment but are not references.
NOTE = "call process_payment here"
# process_payment is also named in this comment
