"""The FastResearch publish response contract shared by both publishing entries."""


def validate_publish_receipt(receipt):
    if not isinstance(receipt, dict) or receipt.get("ok") is not True:
        raise ValueError("FastResearch publish receipt must be an object with ok=true")
    names = receipt.get("deliveredTo")
    if not isinstance(names, list) or not names or any(
        not isinstance(name, str) or not name.strip() for name in names
    ):
        raise ValueError("FastResearch publish receipt requires a nonempty deliveredTo list of names")
    return receipt
